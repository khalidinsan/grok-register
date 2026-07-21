"""
Browser engine selector: chromium | camoufox

Both use native proxy auth (server + username + password) like flash-grok-farm.

  chromium  — Playwright Chromium (optional DrissionPage CDP attach)
  camoufox  — Camoufox (Firefox anti-detect, Playwright-compatible API)

Env / config:
  GROK_BROWSER_ENGINE=chromium|camoufox
  GROK_BROWSER_PROXY=http://user:pass@host:port
  GROK_DISPLAY=headed|offscreen|headless|virtual
  GROK_HEADLESS=true|false   (flash-compatible; true → headless)

Display defaults (flash-aligned):
  macOS   → offscreen   (work-friendly; park window, no focus steal)
  Linux   → headless    (VPS / server farm — flash default)
  Windows → headless    (flash default; use --headed to debug)
"""

from __future__ import annotations

import asyncio
import os
import re
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from proxy_util import mask_proxy, playwright_proxy_dict

# All accepted display tokens after normalization
DISPLAY_MODES = ("headed", "offscreen", "headless", "virtual")


class _ActionsProxy:
    """DrissionPage page.actions.move_to(el).click() shim."""

    def __init__(self, page_adapter: "PwPageAdapter"):
        self._page = page_adapter
        self._target = None

    def move_to(self, el):
        self._target = el
        return self

    def click(self, *a, **k):
        if self._target is not None:
            return self._target.click()
        return None


def resolve_engine(explicit: str = "") -> str:
    v = (
        explicit
        or os.environ.get("GROK_BROWSER_ENGINE")
        or os.environ.get("BROWSER_ENGINE")
        or "camoufox"  # flash default: Camoufox (anti-detect + headless-friendly)
    ).strip().lower()
    if v in ("camoufox", "fox", "firefox", "cf"):
        return "camoufox"
    if v in ("chromium", "chrome", "pw", "playwright"):
        return "chromium"
    return "camoufox"


def platform_default_display() -> str:
    """
    Flash-aligned platform defaults:
      macOS   → offscreen  (desktop ergonomics while farming)
      Linux   → headless   (VPS / no GUI — flash production)
      Windows → headless   (flash production)
    """
    sysname = (sys.platform or "").lower()
    if sysname == "darwin":
        return "offscreen"
    # linux, win32, cygwin, …
    return "headless"


def _truthy_headless(raw: str) -> Optional[bool]:
    """Parse GROK_HEADLESS-style flags. None = unset / unknown."""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s in ("1", "true", "yes", "on", "headless"):
        return True
    if s in ("0", "false", "no", "off", "headed"):
        return False
    if s in ("virtual", "xvfb", "vd"):
        return None  # handled as display=virtual separately
    return None


def normalize_display(raw: str) -> str:
    """Map aliases → headed|offscreen|headless|virtual."""
    d = (raw or "").strip().lower()
    if d in ("bg", "background", "minimized", "minimise", "minimize", "hidden"):
        return "offscreen"
    if d in ("hl", "no-window", "nowindow", "server"):
        return "headless"
    if d in ("xvfb", "vd", "virtual-display", "virtual_display", "vdisplay"):
        return "virtual"
    if d in ("gui", "window", "visible", "show"):
        return "headed"
    if d in DISPLAY_MODES:
        return d
    return ""


def resolve_display(explicit: str = "") -> str:
    """
    Resolve display mode with flash-compatible precedence:

      1. explicit arg / CLI
      2. GROK_DISPLAY / DISPLAY_MODE env
      3. GROK_HEADLESS env (true→headless, false→headed, virtual token→virtual)
      4. platform default (Linux/Win headless, Mac offscreen)

    Returns one of: headed | offscreen | headless | virtual
    """
    # 1) explicit
    if explicit:
        n = normalize_display(explicit)
        if n:
            return n

    # 2) GROK_DISPLAY / DISPLAY_MODE
    for key in ("GROK_DISPLAY", "DISPLAY_MODE"):
        env_d = os.environ.get(key) or ""
        n = normalize_display(env_d)
        if n:
            return n

    # 3) GROK_HEADLESS (flash .env flag)
    gh = (os.environ.get("GROK_HEADLESS") or "").strip().lower()
    if gh in ("virtual", "xvfb", "vd"):
        return "virtual"
    th = _truthy_headless(gh)
    if th is True:
        return "headless"
    if th is False:
        # flash: GROK_HEADLESS=false → headed (not offscreen)
        return "headed"

    # 4) platform default
    return platform_default_display()


def camoufox_headless_arg(display: str) -> Union[bool, str]:
    """
    Map our display mode → Camoufox `headless=` kwarg.

      headless  → True          (native Firefox headless — flash default)
      virtual   → 'virtual'     (Camoufox starts Xvfb, browser headed on it)
      offscreen → False         (headed; caller parks window if possible)
      headed    → False
    """
    d = normalize_display(display) or "headless"
    if d == "headless":
        return True
    if d == "virtual":
        return "virtual"
    return False


@dataclass
class BrowserSession:
    engine: str
    page: Any
    proxy: str = ""
    display: str = "offscreen"
    # backend handles for cleanup
    _pw: Any = None
    _context: Any = None
    _camoufox_manager: Any = None
    _loop: Any = None
    _profile_dir: str = ""
    _debug_port: int = 0
    extra: dict = field(default_factory=dict)

    def close(self) -> None:
        # Playwright context
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None
        # Camoufox async manager
        if self._camoufox_manager is not None and self._loop is not None:
            try:
                self._loop.run_until_complete(self._camoufox_manager.__aexit__(None, None, None))
            except Exception:
                pass
            self._camoufox_manager = None
        if self._profile_dir and os.path.isdir(self._profile_dir):
            try:
                import shutil

                shutil.rmtree(self._profile_dir, ignore_errors=True)
            except Exception:
                pass
            self._profile_dir = ""


def _pick_free_port(preferred: int = 9330) -> int:
    for port in range(max(1024, preferred), preferred + 80):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def launch_chromium_session(
    *,
    proxy: str = "",
    display: str = "",
    profile_dir: str = "",
    debug_port: int = 0,
    browser_path: str = "",
    extension_path: str = "",
) -> BrowserSession:
    from playwright.sync_api import sync_playwright

    display = resolve_display(display)
    # Chromium has no Camoufox-style 'virtual'; map to real headless (or need Xvfb outside)
    headless = display in ("headless", "virtual")
    if extension_path and os.path.isdir(extension_path) and headless:
        # MV2/MV3 extensions generally need non-headless Chromium
        headless = False

    if not profile_dir:
        profile_dir = tempfile.mkdtemp(prefix="grok_chromium_")
    if not debug_port:
        debug_port = _pick_free_port(9330)

    proxy_cfg = playwright_proxy_dict(proxy) if proxy else None
    args = [
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--no-sandbox",
    ]
    if display == "offscreen":
        args.extend(["--window-size=1100,800", "--window-position=-32000,-32000"])
    else:
        args.append("--window-size=1100,800")
    if extension_path and os.path.isdir(extension_path):
        args.append(f"--disable-extensions-except={extension_path}")
        args.append(f"--load-extension={extension_path}")

    pw = sync_playwright().start()
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": profile_dir,
        "headless": headless,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
        "viewport": {"width": 1100, "height": 800},
        "locale": "en-US",
    }
    if browser_path and os.path.isfile(browser_path):
        launch_kwargs["executable_path"] = browser_path
    if proxy_cfg:
        launch_kwargs["proxy"] = proxy_cfg

    context = pw.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else context.new_page()
    return BrowserSession(
        engine="chromium",
        page=page,
        proxy=proxy,
        display=display,
        _pw=pw,
        _context=context,
        _profile_dir=profile_dir,
        _debug_port=debug_port,
        extra={"proxy_cfg": proxy_cfg, "mask": mask_proxy(proxy) if proxy else ""},
    )


class _ScrollHelper:
    def __init__(self, loc, loop, is_async):
        self._loc = loc
        self._loop = loop
        self._async = is_async

    def to_see(self, *a, **k):
        if self._async and self._loop is not None:
            return self._loop.run_until_complete(self._loc.scroll_into_view_if_needed())
        return self._loc.scroll_into_view_if_needed()


class _PwElementAdapter:
    """Minimal DrissionPage-like element (click / input / scroll)."""

    def __init__(self, locator: Any, loop: Any = None, is_async: bool = False):
        self._loc = locator
        self._loop = loop
        self._async = is_async
        self.scroll = _ScrollHelper(locator, loop, is_async)

    def _run(self, coro):
        if self._async and self._loop is not None:
            return self._loop.run_until_complete(coro)
        return coro

    def click(self, *args, **kwargs):
        # by_js=True → still locator click (Playwright can't pass DOM node easily)
        timeout = kwargs.get("timeout", 5000)
        if isinstance(timeout, float) and timeout < 100:
            timeout = int(timeout * 1000)
        if self._async:
            return self._run(self._loc.click(timeout=timeout))
        return self._loc.click(timeout=timeout)

    def input(self, text: str):
        if self._async:
            return self._run(self._loc.fill(str(text)))
        return self._loc.fill(str(text))


class PwPageAdapter:
    """
    Make Playwright / Camoufox Page look enough like DrissionPage for the farm:
      page.get / page.ele / page.run_js / page.url / page.run_cdp / clear_cache
    """

    def __init__(self, page: Any, *, loop: Any = None, is_async: bool = False, browser_ref: Any = None):
        self._page = page
        self._loop = loop
        self._async = is_async
        self._browser_ref = browser_ref

    def _run(self, coro_or_val):
        if self._async and self._loop is not None and asyncio.iscoroutine(coro_or_val):
            return self._loop.run_until_complete(coro_or_val)
        return coro_or_val

    @property
    def url(self) -> str:
        try:
            return str(self._page.url or "")
        except Exception:
            return ""

    @property
    def title(self) -> str:
        try:
            t = self._page.title()
            return str(self._run(t) if self._async else t)
        except Exception:
            return ""

    def get(self, url: str, **kwargs):
        timeout = int(kwargs.get("timeout", 60) * 1000) if kwargs.get("timeout", 60) < 1000 else int(kwargs.get("timeout", 60000))
        if self._async:
            return self._run(
                self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            )
        return self._page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    def _to_pw_selector(self, sel: str) -> str:
        s = (sel or "").strip()
        if s.startswith("tag:") and "@@text()" in s:
            m = re.search(r"@@text\(\)\s*[:=]\s*(.+)$", s)
            if m:
                return f"text={m.group(1).strip().strip(chr(39) + chr(34))}"
        if s.startswith("@"):
            body = s[1:]
            if "=" in body:
                k, _, v = body.partition("=")
                return f'[{k}="{v}"]'
            return f"[{body}]"
        if s.startswith("text:"):
            return f"text={s[5:]}"
        if s.startswith("css:"):
            return s[4:]
        if s.startswith("xpath:"):
            return f"xpath={s[6:]}"
        if s.startswith("//") or s.startswith("(//"):
            return f"xpath={s}"
        return s

    def ele(self, selector: str, timeout: float = 2.0, **kwargs):
        ms = int(max(0.1, float(timeout or 0.5)) * 1000)
        try:
            s = selector or ""
            if "Complete sign" in s or "complete sign" in s.lower():
                loc = self._page.get_by_role(
                    "button", name=re.compile(r"complete\s*sign", re.I)
                ).first
            elif "Create Account" in s or "create account" in s.lower():
                loc = self._page.get_by_role(
                    "button", name=re.compile(r"create\s*account", re.I)
                ).first
            else:
                sel = self._to_pw_selector(s)
                loc = self._page.locator(sel).first
            if self._async:
                try:
                    self._run(loc.wait_for(state="visible", timeout=ms))
                except Exception:
                    return None
                return _PwElementAdapter(loc, self._loop, True)
            try:
                loc.wait_for(state="visible", timeout=ms)
            except Exception:
                return None
            return _PwElementAdapter(loc, None, False)
        except Exception:
            return None

    def run_js(self, script: str, *args):
        """
        DrissionPage: run_js(body, arg0, arg1, ...)
        Playwright evaluate(fn, ONE_arg) — pack args into a single list.
        """
        src = (script or "").strip()
        clean = [a for a in args if not isinstance(a, _PwElementAdapter)]
        wrapped = (
            "(__args) => { "
            "const arguments = Array.isArray(__args) ? __args : [__args]; "
            + src
            + " }"
        )
        try:
            if self._async:
                return self._run(self._page.evaluate(wrapped, clean))
            return self._page.evaluate(wrapped, clean)
        except Exception as e1:
            expr = (
                "(__args) => { "
                "const arguments = Array.isArray(__args) ? __args : [__args]; "
                f"return ({src}); }}"
            )
            try:
                if self._async:
                    return self._run(self._page.evaluate(expr, clean))
                return self._page.evaluate(expr, clean)
            except Exception:
                raise e1

    def run_cdp(self, method: str, **params):
        try:
            if self._async:
                return None
            cdp = self._page.context.new_cdp_session(self._page)
            return cdp.send(method, params or {})
        except Exception:
            return None

    def clear_cache(self, **kwargs):
        try:
            if self._async:
                self._run(self._page.evaluate(
                    "() => { try{localStorage.clear();sessionStorage.clear();}catch(e){} }"
                ))
                self._run(self._page.context.clear_cookies())
            else:
                self._page.evaluate(
                    "() => { try{localStorage.clear();sessionStorage.clear();}catch(e){} }"
                )
                self._page.context.clear_cookies()
        except Exception:
            pass

    def cookies(self, all_domains: bool = True, all_info: bool = True, **kwargs):
        """
        DrissionPage-compatible cookie list for SSO harvest.
        Playwright: context.cookies() → [{name, value, domain, ...}, ...]
        """
        try:
            if self._async:
                raw = self._run(self._page.context.cookies())
            else:
                raw = self._page.context.cookies()
            out = []
            for c in raw or []:
                if isinstance(c, dict):
                    out.append(
                        {
                            "name": c.get("name", ""),
                            "value": c.get("value", ""),
                            "domain": c.get("domain", "") or c.get("host", ""),
                            "path": c.get("path", "/"),
                            "expires": c.get("expires"),
                            "httpOnly": c.get("httpOnly"),
                            "secure": c.get("secure"),
                        }
                    )
            return out
        except Exception:
            return []

    @property
    def actions(self):
        return _ActionsProxy(self)

    @property
    def raw(self):
        return self._page


class PwBrowserAdapter:
    """DrissionPage-like browser: get_tabs / new_tab / quit / latest_tab."""

    def __init__(
        self,
        browser: Any,
        page: Any,
        *,
        loop: Any = None,
        is_async: bool = False,
        session: Any = None,
    ):
        self._browser = browser
        self._loop = loop
        self._async = is_async
        self._session = session
        self._page = PwPageAdapter(page, loop=loop, is_async=is_async, browser_ref=self)
        self.latest_tab = self._page

    def _run(self, coro):
        if self._async and self._loop is not None:
            return self._loop.run_until_complete(coro)
        return coro

    def get_tabs(self):
        pages = []
        try:
            if self._async:
                # camoufox browser.contexts[0].pages
                ctxs = self._browser.contexts
                for ctx in ctxs:
                    pages.extend(ctx.pages)
            else:
                for ctx in self._browser.contexts:
                    pages.extend(ctx.pages)
        except Exception:
            pages = [self._page.raw]
        return [
            PwPageAdapter(p, loop=self._loop, is_async=self._async, browser_ref=self)
            for p in pages
        ]

    def new_tab(self, url: str = "about:blank"):
        try:
            if self._async:
                ctx = self._browser.contexts[0]
                p = self._run(ctx.new_page())
                if url:
                    self._run(p.goto(url, wait_until="domcontentloaded", timeout=60000))
            else:
                ctx = self._browser.contexts[0]
                p = ctx.new_page()
                if url:
                    p.goto(url, wait_until="domcontentloaded", timeout=60000)
            adapted = PwPageAdapter(p, loop=self._loop, is_async=self._async, browser_ref=self)
            self.latest_tab = adapted
            self._page = adapted
            return adapted
        except Exception:
            # reuse existing
            if url and url != "about:blank":
                self._page.get(url)
            return self._page

    def quit(self):
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass


def launch_camoufox_session(
    *,
    proxy: str = "",
    display: str = "",
    humanize: Optional[float] = None,
) -> BrowserSession:
    """
    Camoufox (async) wrapped with a dedicated event loop for sync farm code.

    Flash-aligned headless mapping:
      display=headless  → headless=True          (Linux VPS default)
      display=virtual   → headless='virtual'     (Camoufox Xvfb — headed on virtual X)
      display=offscreen → headless=False         (headed; Mac park window)
      display=headed    → headless=False

    Returns BrowserSession whose .page / extra['browser_adapter'] speak Drission-like API.
    """
    try:
        from camoufox.async_api import AsyncCamoufox
    except ImportError as e:
        raise RuntimeError(
            "camoufox not installed. Run: pip install 'camoufox[geoip]' && python -m camoufox fetch"
        ) from e

    display = resolve_display(display)
    headless = camoufox_headless_arg(display)
    proxy_cfg = playwright_proxy_dict(proxy) if proxy else None

    # humanize: env GROK_HUMANIZE wins; flash turbo uses ~0.05
    if humanize is None:
        try:
            humanize = float(os.environ.get("GROK_HUMANIZE") or "0.08")
        except (TypeError, ValueError):
            humanize = 0.08

    # geoip: flash turbo defaults off; we enable when proxy present unless forced
    geoip_env = (os.environ.get("GROK_GEOIP") or "").strip().lower()
    if geoip_env in ("1", "true", "yes", "on"):
        use_geoip = True
    elif geoip_env in ("0", "false", "no", "off"):
        use_geoip = False
    else:
        use_geoip = bool(proxy_cfg)

    # OS fingerprint pool like flash (random windows/macos/linux)
    fox_os = (os.environ.get("GROK_CAMOUFOX_OS") or "").strip().lower()
    if fox_os not in ("windows", "macos", "linux"):
        import random

        fox_os = random.choice(["windows", "macos", "linux"])

    kwargs: dict[str, Any] = {
        "headless": headless,
        "humanize": max(0.0, float(humanize)),
        "os": fox_os,
        "locale": "en-US",
        "geoip": use_geoip,
        "block_webrtc": True,
    }
    if proxy_cfg:
        kwargs["proxy"] = proxy_cfg

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _start():
        manager = AsyncCamoufox(**kwargs)
        browser = await manager.__aenter__()
        page = await browser.new_page()
        page.set_default_timeout(60000)
        return manager, browser, page

    try:
        manager, browser, page = loop.run_until_complete(_start())
    except Exception as e:
        # virtual needs Xvfb / pyvirtualdisplay — give a clear hint
        if display == "virtual" or headless == "virtual":
            raise RuntimeError(
                f"Camoufox virtual display failed: {e}\n"
                "  Linux: apt install xvfb && pip install pyvirtualdisplay\n"
                "  or use: GROK_DISPLAY=headless / --headless (no Xvfb)\n"
                "  or:     xvfb-run -a python farm_tui.py -u -c 2"
            ) from e
        raise

    session = BrowserSession(
        engine="camoufox",
        page=None,  # set after adapter
        proxy=proxy,
        display=display,
        _camoufox_manager=manager,
        _loop=loop,
        extra={
            "browser": browser,
            "proxy_cfg": proxy_cfg,
            "mask": mask_proxy(proxy) if proxy else "",
            "headless": headless,
            "camoufox_os": fox_os,
        },
    )
    adapter = PwBrowserAdapter(
        browser, page, loop=loop, is_async=True, session=session
    )
    session.page = adapter.latest_tab
    session.extra["browser_adapter"] = adapter
    session.extra["raw_page"] = page
    return session


def launch_session(
    engine: str = "",
    *,
    proxy: str = "",
    display: str = "",
    **kwargs: Any,
) -> BrowserSession:
    eng = resolve_engine(engine)
    proxy = (proxy or os.environ.get("GROK_BROWSER_PROXY") or "").strip()
    display = resolve_display(display)
    if eng == "camoufox":
        return launch_camoufox_session(proxy=proxy, display=display)
    return launch_chromium_session(proxy=proxy, display=display, **kwargs)

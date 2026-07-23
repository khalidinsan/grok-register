"""
Grok Build / CLI OAuth via browser PKCE (flash-grok-farm path).

NOT device-code from SSO cookie. Same session as signup + referrer=grok-build.

  GET auth.x.ai/oauth2/authorize?...&referrer=grok-build
  → capture code at http://127.0.0.1:56121/callback
  → POST auth.x.ai/oauth2/token (authorization_code + code_verifier)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_AUTHORIZE = "https://auth.x.ai/oauth2/authorize"
XAI_TOKEN = "https://auth.x.ai/oauth2/token"
XAI_REDIRECT_URI = "http://127.0.0.1:56121/callback"
_CALLBACK_HOST = "127.0.0.1"
_CALLBACK_PORT = 56121
# Match flash / 9router Grok Build (narrower than full conversations scopes)
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"
DEFAULT_REFERRER = "grok-build"


@dataclass
class BuildTokens:
    access_token: str
    refresh_token: str
    id_token: str = ""
    expires_at: str = ""
    expires_in: int = 21600
    email: str = ""
    user_id: str = ""
    team_id: str = ""
    name: str = ""
    scope: str = XAI_SCOPE
    referrer: str = DEFAULT_REFERRER
    bot_flag_source: Any = None
    jwt_claims: Dict[str, Any] = field(default_factory=dict)
    auth_mode: str = "oidc_pkce"


def generate_pkce_pair() -> tuple[str, str]:
    raw = secrets.token_bytes(96)
    verifier = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def extract_code_from_url(url: str) -> Optional[str]:
    """
    ONLY accept OAuth authorization codes from the local PKCE callback.
    Never parse accounts.x.ai/consent URLs (they contain code_challenge=, not code=).
    """
    try:
        parsed = urlparse(url or "")
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost"):
        return None
    # require callback path or explicit code= query on localhost
    path = parsed.path or ""
    if "/callback" not in path and "code=" not in (parsed.query or ""):
        return None
    params = parse_qs(parsed.query)
    vals = params.get("code")
    if not vals or not vals[0]:
        return None
    code = vals[0].strip()
    # reject empty / tiny garbage; real auth codes are reasonably long
    if len(code) < 8:
        return None
    return code


# Cross-process lock so concurrent Chromium workers don't fight for :56121
# (OAuth redirect_uri is fixed — cannot use alternate ports).
_PKCE_PORT_LOCK_PATH = os.environ.get(
    "GROK_PKCE_PORT_LOCK",
    os.path.join(
        os.environ.get("TMPDIR") or os.environ.get("TMP") or "/tmp",
        "grok_pkce_56121.lock",
    ),
)


class _PkcePortLock:
    """Exclusive file lock for the fixed OAuth callback port (multi-worker safe)."""

    def __init__(self, path: str = _PKCE_PORT_LOCK_PATH, timeout: float = 120.0):
        self.path = path
        self.timeout = timeout
        self._fh: Any = None

    def acquire(self) -> bool:
        import os as _os

        _os.makedirs(_os.path.dirname(self.path) or ".", exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                if _os.name == "nt":
                    import msvcrt

                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(f"{_os.getpid()}\n")
                self._fh.flush()
                return True
            except (OSError, BlockingIOError):
                time.sleep(0.4)
        return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            import os as _os

            if _os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


class _PkceCallbackServer:
    """
    Real HTTP listener on 127.0.0.1:56121 ONLY for Chromium/Drission PKCE.

    Camoufox/Playwright intercepts the redirect with page.route() (fake fulfill).
    Drission MixTab has NO route() — browser does a real navigation to localhost.
    Port MUST be 56121 (matches registered redirect_uri). Alternate ports break OAuth.
    Use _PkcePortLock so concurrent workers serialize on this port.
    """

    def __init__(self, captured: Dict[str, Optional[str]]):
        self.captured = captured
        self._httpd: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = _CALLBACK_PORT
        self._lock = _PkcePortLock()

    def start(self, *, wait_lock_sec: float = 120.0) -> bool:
        if not self._lock.acquire():
            return False
        captured = self.captured
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return  # quiet

            def do_GET(self) -> None:  # noqa: N802
                try:
                    full = f"http://127.0.0.1:{outer.port}{self.path}"
                    c = extract_code_from_url(full)
                    if c:
                        captured["code"] = c
                    body = (
                        b"<html><body><h3>OAuth callback OK</h3>"
                        b"<p>You can close this tab.</p></body></html>"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except Exception:
                    try:
                        self.send_response(500)
                        self.end_headers()
                    except Exception:
                        pass

            def do_POST(self) -> None:  # noqa: N802
                self.do_GET()

        # ONLY :56121 — must match XAI_REDIRECT_URI exactly
        try:

            class _ReuseHTTPServer(HTTPServer):
                allow_reuse_address = True

            httpd = _ReuseHTTPServer((_CALLBACK_HOST, _CALLBACK_PORT), Handler)
            self._httpd = httpd
            self.port = _CALLBACK_PORT
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            self._thread = t
            return True
        except OSError:
            self._lock.release()
            return False

    def stop(self) -> None:
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None
        self._lock.release()


def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    try:
        parts = (token or "").split(".")
        if len(parts) < 2:
            return {}
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def exchange_code_for_tokens(
    code: str,
    verifier: str,
    *,
    proxy: str = "",
) -> BuildTokens:
    form = {
        "grant_type": "authorization_code",
        "client_id": XAI_CLIENT_ID,
        "code": code,
        "redirect_uri": XAI_REDIRECT_URI,
        "code_verifier": verifier,
    }
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    resp = requests.post(
        XAI_TOKEN,
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=45,
        proxies=proxies,
    )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"token exchange HTTP {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json() if resp.content else {}
    access = data.get("access_token") or ""
    refresh = data.get("refresh_token") or ""
    if not access or not refresh:
        raise RuntimeError(f"token response missing tokens: {list(data.keys())}")
    expires_in = int(data.get("expires_in") or 21600)
    from datetime import datetime, timezone, timedelta

    exp_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    claims = _decode_jwt_claims(access)
    id_token = data.get("id_token") or ""
    email = ""
    if id_token:
        idc = _decode_jwt_claims(id_token)
        email = str(idc.get("email") or "")
    if not email:
        email = str(claims.get("email") or "")
    return BuildTokens(
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        expires_at=exp_at.isoformat().replace("+00:00", "Z"),
        expires_in=expires_in,
        email=email,
        user_id=str(claims.get("sub") or claims.get("principal_id") or ""),
        team_id=str(claims.get("team_id") or ""),
        name=str(claims.get("name") or ""),
        scope=str(data.get("scope") or XAI_SCOPE),
        referrer=str(claims.get("referrer") or ""),
        bot_flag_source=claims.get("bot_flag_source"),
        jwt_claims={
            k: claims.get(k)
            for k in (
                "bot_flag_source",
                "referrer",
                "sub",
                "team_id",
                "principal_id",
                "client_id",
                "scope",
            )
            if k in claims
        },
        auth_mode="oidc_pkce",
    )


def build_authorize_url(
    *,
    email: str = "",
    referrer: str = DEFAULT_REFERRER,
    verifier: Optional[str] = None,
    challenge: Optional[str] = None,
) -> tuple[str, str, str]:
    """Returns (auth_url, verifier, state)."""
    if not verifier or not challenge:
        verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_hex(16)
    params = {
        "response_type": "code",
        "client_id": XAI_CLIENT_ID,
        "redirect_uri": XAI_REDIRECT_URI,
        "scope": XAI_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": (referrer or DEFAULT_REFERRER).strip(),
    }
    if email:
        params["login_hint"] = email
    return f"{XAI_AUTHORIZE}?{urlencode(params)}", verifier, state


# ── Page adapters (DrissionPage / Playwright / Camoufox) ───────────────────


def _unwrap_page(page: Any) -> tuple[Any, Any, bool]:
    """
    Return (raw_playwright_page, event_loop_or_None, is_async).

    Camoufox farm wraps Page in PwPageAdapter (.raw, ._loop, ._async).
    PKCE MUST use the raw page for route() / get_by_role().
    """
    raw = getattr(page, "raw", None) or page
    loop = getattr(page, "_loop", None)
    is_async = bool(getattr(page, "_async", False))
    # also check browser adapter parent
    if loop is None and getattr(page, "_browser_ref", None) is not None:
        br = page._browser_ref
        loop = getattr(br, "_loop", None)
        is_async = bool(getattr(br, "_async", False)) or is_async
    return raw, loop, is_async


def _run(loop: Any, is_async: bool, coro_or_val: Any) -> Any:
    if is_async and loop is not None:
        import asyncio

        if asyncio.iscoroutine(coro_or_val):
            return loop.run_until_complete(coro_or_val)
    return coro_or_val


def _page_url(page: Any) -> str:
    raw, loop, is_async = _unwrap_page(page)
    try:
        return str(getattr(raw, "url", "") or "")
    except Exception:
        pass
    try:
        u = getattr(page, "url", None)
        if callable(u):
            return str(u() or "")
        if u:
            return str(u)
    except Exception:
        pass
    try:
        if hasattr(raw, "evaluate"):
            return str(
                _run(loop, is_async, raw.evaluate("() => location.href")) or ""
            )
    except Exception:
        pass
    return ""


def _page_goto(page: Any, url: str, timeout_ms: int = 25000) -> None:
    raw, loop, is_async = _unwrap_page(page)
    if hasattr(raw, "goto"):
        _run(
            loop,
            is_async,
            raw.goto(url, wait_until="domcontentloaded", timeout=timeout_ms),
        )
        return
    if hasattr(page, "get"):
        page.get(url)
        return
    raise TypeError(f"unsupported page type: {type(page)}")


def _capture_code_from_pages(page: Any) -> Optional[str]:
    """ONLY localhost callback URLs — never scrape consent page DOM (false codes)."""
    raw, loop, is_async = _unwrap_page(page)
    urls = []
    for obj in (raw, page):
        try:
            u = getattr(obj, "url", None)
            if callable(u):
                u = u()
            if u:
                urls.append(str(u))
        except Exception:
            pass
    try:
        href = _run_page_js(page, "() => location.href")
        if href:
            urls.append(str(href))
    except Exception:
        pass
    try:
        ctx = getattr(raw, "context", None)
        if ctx is not None:
            for p in getattr(ctx, "pages", []) or []:
                try:
                    urls.append(str(getattr(p, "url", "") or ""))
                except Exception:
                    pass
    except Exception:
        pass
    for u in urls:
        c = extract_code_from_url(u)
        if c:
            return c
    return None


# STRICT: only real OAuth consent actions. Never cookies / sign-out / Google login.
_ALLOW_CLICK_JS = r"""
() => {
  // exact labels only — 'accept' alone matched "Accept all cookies" and killed session
  const exact = new Set(['allow', 'authorize', 'approve', '允许', '授权']);
  const nodes = [...document.querySelectorAll(
    'button, [role="button"], input[type="submit"]'
  )];
  const visible = (b) => {
    try {
      const r = b.getBoundingClientRect();
      const st = window.getComputedStyle(b);
      if (r.width <= 0 || r.height <= 0) return false;
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
      if (b.disabled || b.getAttribute('aria-disabled') === 'true') return false;
      return true;
    } catch (e) { return false; }
  };
  const label = (b) => (b.innerText || b.textContent || b.value || b.getAttribute('aria-label') || '')
    .replace(/\s+/g, ' ').trim().toLowerCase();
  const banned = /cookie|sign out|sign-out|log out|logout|google|apple|microsoft|github|deny|cancel|reject|go back|continue with/;
  for (const b of nodes) {
    if (!visible(b)) continue;
    const txt = label(b);
    if (!txt || banned.test(txt)) continue;
    if (!exact.has(txt)) continue;
    b.scrollIntoView({block: 'center', inline: 'center'});
    b.click();
    return txt;
  }
  return '';
}
"""


def _run_page_js(page: Any, script: str) -> Any:
    """Run JS on Playwright (evaluate) or DrissionPage MixTab (run_js)."""
    raw, loop, is_async = _unwrap_page(page)
    # Playwright / Camoufox adapter
    if hasattr(raw, "evaluate"):
        try:
            return _run(loop, is_async, raw.evaluate(script))
        except Exception:
            pass
    # DrissionPage Chromium MixTab / PwPageAdapter
    for target in (page, raw):
        if hasattr(target, "run_js"):
            try:
                # Drission: run_js body without outer function sometimes preferred
                body = script.strip()
                if body.startswith("() =>") or body.startswith("()=>"):
                    body = body.split("{", 1)[-1].rsplit("}", 1)[0]
                    return target.run_js(body)
                return target.run_js(script)
            except Exception:
                try:
                    return target.run_js(script)
                except Exception:
                    pass
    return None


def _click_allow_hard(page: Any, log: Optional[Callable[[str], None]] = None) -> bool:
    """
    Aggressively click Allow / Authorize on consent.

    Supports:
      - Camoufox / Playwright async (get_by_role + evaluate)
      - Chromium DrissionPage MixTab (run_js + text: selectors)
    """
    raw, loop, is_async = _unwrap_page(page)

    def _log(m: str) -> None:
        if log:
            log(m)

    # 1) exact role Allow / Authorize (async Camoufox) — NEVER "Accept" (cookies)
    if hasattr(raw, "get_by_role") and is_async and loop is not None:
        for kw in ("Allow", "Authorize", "Approve"):
            try:
                async def _click_kw(k=kw):
                    loc = raw.get_by_role(
                        "button", name=re.compile(rf"^{re.escape(k)}$", re.I)
                    )
                    n = await loc.count()
                    if n <= 0:
                        return False
                    btn = loc.first
                    try:
                        await btn.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    try:
                        await btn.click(timeout=3000)
                    except Exception:
                        await btn.click(timeout=3000, force=True)
                    return True

                if loop.run_until_complete(_click_kw()):
                    _log(f"clicked Allow via get_by_role({kw!r})")
                    return True
            except Exception as e:
                _log(f"role click {kw!r} warn: {e}")

    # 2) JS click — works on Playwright evaluate AND Drission run_js
    try:
        clicked = _run_page_js(page, _ALLOW_CLICK_JS)
        if clicked:
            _log(f"clicked Allow via JS ({clicked!r})")
            return True
    except Exception as e:
        _log(f"JS Allow click warn: {e}")

    # 3) sync playwright path
    if hasattr(raw, "get_by_role") and not is_async:
        for kw in ("Allow", "Authorize", "Approve"):
            try:
                loc = raw.get_by_role(
                    "button", name=re.compile(rf"^{re.escape(kw)}$", re.I)
                ).first
                n = loc.count() if hasattr(loc, "count") else 1
                if n and n > 0:
                    loc.click(timeout=3000, force=True)
                    _log(f"clicked Allow sync role({kw!r})")
                    return True
            except Exception:
                continue

    # 4) DrissionPage — exact text only (no Accept / no primary submit)
    for target in (page, raw):
        if not hasattr(target, "ele"):
            continue
        for sel in (
            "text:Allow",
            "text:Authorize",
            "text:Approve",
            "text:允许",
            "text:授权",
            "tag:button@@text():Allow",
            "xpath://button[normalize-space(translate(., 'ALLOW', 'allow'))='allow']",
            "xpath://*[@role='button' and normalize-space(translate(., 'ALLOW', 'allow'))='allow']",
        ):
            try:
                el = target.ele(sel, timeout=0.6)
                if el:
                    try:
                        el.click()
                    except Exception:
                        try:
                            el.click(by_js=True)
                        except Exception:
                            continue
                    _log(f"clicked Allow via Drission ele({sel!r})")
                    return True
            except Exception:
                continue
        break

    return False


def obtain_tokens_via_browser_pkce(
    page: Any,
    *,
    email: str = "",
    referrer: str = DEFAULT_REFERRER,
    timeout_sec: float = 90.0,
    proxy: str = "",
    log: Optional[Callable[[str], None]] = None,
    try_consent_click: bool = True,
) -> BuildTokens:
    """
    Browser PKCE (flash path) — STRICT:

      1. route 127.0.0.1:56121 BEFORE goto
      2. goto authorize
      3. on /oauth2/consent → CLICK Allow (must succeed)
      4. wait for callback?code=  (ONLY source of auth code)
      5. exchange code

    NEVER scrape consent-page DOM for fake codes (caused invalid_grant in 0.0s).
    The "copy code into Grok Build" UI still needs Allow so redirect fires.
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    raw, loop, is_async = _unwrap_page(page)
    auth_url, verifier, _state = build_authorize_url(email=email, referrer=referrer)
    _log(f"OAuth PKCE authorize referrer={referrer} email={email or '-'}")
    _log(f"OAuth unwrap raw={type(raw).__name__} async={is_async}")

    captured: Dict[str, Optional[str]] = {"code": None}
    cb_server: Optional[_PkceCallbackServer] = None
    used_pw_route = False

    # Install route BEFORE goto (Camoufox/Playwright intercepts redirect in-browser)
    if hasattr(raw, "route"):
        try:
            if is_async and loop is not None:
                async def _handle_async(route):
                    req_url = route.request.url
                    c = extract_code_from_url(req_url)
                    if c:
                        captured["code"] = c
                        await route.fulfill(
                            status=200,
                            content_type="text/html",
                            body="<html><body>OAuth callback captured</body></html>",
                        )
                    else:
                        await route.continue_()

                loop.run_until_complete(
                    raw.route("http://127.0.0.1:56121/**", _handle_async)
                )
                loop.run_until_complete(
                    raw.route("http://localhost:56121/**", _handle_async)
                )
            else:
                def _handle_sync(route):
                    req_url = route.request.url
                    c = extract_code_from_url(req_url)
                    if c:
                        captured["code"] = c
                        route.fulfill(
                            status=200,
                            content_type="text/html",
                            body="<html><body>OAuth callback captured</body></html>",
                        )
                    else:
                        route.continue_()

                raw.route("http://127.0.0.1:56121/**", _handle_sync)
                raw.route("http://localhost:56121/**", _handle_sync)
            used_pw_route = True
            _log("OAuth callback route installed (127.0.0.1:56121) [playwright]")
        except Exception as e:
            _log(f"route setup warn: {e}")
            used_pw_route = False

    # Chromium/Drission has no page.route() — need real HTTP on EXACT :56121
    # (multi-worker: file lock serializes; never use alternate ports)
    if not used_pw_route:
        _log("OAuth waiting for exclusive :56121 callback lock (Chromium path)…")
        cb_server = _PkceCallbackServer(captured)
        if cb_server.start():
            _log(
                "OAuth local callback server on "
                "http://127.0.0.1:56121/callback (Chromium/Drission path)"
            )
        else:
            _log(
                "WARN: could not bind 127.0.0.1:56121 after lock wait — "
                "Chromium PKCE will fail (another process holds the port)"
            )
            cb_server = None

    try:
        _page_goto(page, auth_url)
        # Chromium/Drission consent paint slower than Camoufox
        raw0, loop0, asy0 = _unwrap_page(page)
        time.sleep(1.5 if not asy0 else 0.9)

        t0 = time.time()
        last = ""
        allow_attempts = 0
        last_allow_try = 0.0

        while time.time() - t0 < timeout_sec:
            if captured.get("code"):
                break

            code = _capture_code_from_pages(page) or captured.get("code")
            if code:
                captured["code"] = code
                _log("OAuth code from page URL / callback server")
                break

            cur = _page_url(page)
            if not cur:
                try:
                    cur = str(_run_page_js(page, "() => location.href") or "")
                except Exception:
                    cur = ""
            if cur != last:
                _log(f"OAuth page (+{time.time() - t0:.0f}s): {cur[:140]}")
                last = cur

            on_consent = (
                "/oauth2/consent" in cur
                or "auth.x.ai" in cur
                or ("accounts.x.ai" in cur and "consent" in cur)
            )
            if try_consent_click and on_consent:
                # Retry Allow every ~1.2s until redirect
                if time.time() - last_allow_try >= 1.2:
                    last_allow_try = time.time()
                    allow_attempts += 1
                    if allow_attempts == 1:
                        try:
                            conf = _run_page_js(
                                page,
                                """() => {
                                  const nodes = [...document.querySelectorAll('button,[role="button"]')];
                                  for (const b of nodes) {
                                    const t = (b.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                                    if (t === 'confirm' || t === 'verify' || t === 'continue') {
                                      const r = b.getBoundingClientRect();
                                      if (r.width>0 && r.height>0) { b.click(); return t; }
                                    }
                                  }
                                  return '';
                                }""",
                            )
                            if conf:
                                _log(f"OAuth identity: {conf}")
                                time.sleep(0.5)
                        except Exception:
                            pass

                    # Abort if session already lost (redirect to sign-in)
                    cur_now = _page_url(page) or cur
                    if "sign-in" in cur_now or "sign-up" in cur_now:
                        _log(
                            f"OAuth session lost (url has sign-in/up) — stop Allow spam"
                        )
                        break

                    ok = _click_allow_hard(page, log=_log)
                    if ok:
                        _log(
                            f"OAuth Allow attempt #{allow_attempts} OK — wait callback…"
                        )
                        try:
                            raw2, loop2, asy2 = _unwrap_page(page)
                            if hasattr(raw2, "wait_for_url"):
                                _run(
                                    loop2,
                                    asy2,
                                    raw2.wait_for_url(
                                        re.compile(
                                            r"https?://(127\.0\.0\.1|localhost):56121/"
                                        ),
                                        timeout=10000,
                                    ),
                                )
                                c2 = (
                                    _capture_code_from_pages(page)
                                    or captured.get("code")
                                )
                                if c2:
                                    captured["code"] = c2
                                    break
                            else:
                                # Drission: wait for real HTTP callback server or URL
                                for _ in range(40):
                                    time.sleep(0.35)
                                    if captured.get("code"):
                                        break
                                    c2 = _capture_code_from_pages(page)
                                    if c2:
                                        captured["code"] = c2
                                        break
                                    u2 = _page_url(page) or str(
                                        _run_page_js(page, "() => location.href") or ""
                                    )
                                    if "56121" in u2 or "code=" in u2:
                                        c2 = extract_code_from_url(u2)
                                        if c2:
                                            captured["code"] = c2
                                            break
                                    if "sign-in" in u2:
                                        _log("OAuth bounced to sign-in after Allow")
                                        break
                                if captured.get("code"):
                                    break
                        except Exception:
                            time.sleep(0.4)
                    else:
                        if allow_attempts <= 3 or allow_attempts % 5 == 0:
                            try:
                                dump = _run_page_js(
                                    page,
                                    """() => [...document.querySelectorAll('button,[role=button],input[type=submit]')]
                                      .slice(0,12).map(b => (b.innerText||b.value||'').replace(/\\s+/g,' ').trim())
                                      .filter(Boolean).join(' | ')""",
                                )
                                if dump:
                                    _log(f"OAuth buttons visible: {str(dump)[:160]}")
                            except Exception:
                                pass
                        _log(
                            f"OAuth Allow attempt #{allow_attempts} MISS "
                            f"(no button found / click failed)"
                        )
                        # After a few misses on consent, don't thrash forever
                        if allow_attempts >= 8:
                            _log("OAuth Allow giving up after 8 misses on consent")
                            break

            time.sleep(0.35)

        if not captured.get("code"):
            captured["code"] = _capture_code_from_pages(page)

        code = captured.get("code")
        if not code:
            hint = ""
            try:
                hint = str(
                    _run_page_js(
                        page,
                        """() => (document.body && document.body.innerText || '')
                            .replace(/\\s+/g, ' ').trim().slice(0, 160)""",
                    )
                    or ""
                )
            except Exception:
                pass
            raise RuntimeError(
                f"OAuth code not captured in {timeout_sec:.0f}s "
                f"(allow_attempts={allow_attempts} last_url={_page_url(page)[:140]!r} "
                f"hint={hint[:100]!r} route={used_pw_route} "
                f"cb_server={'on' if cb_server else 'off'}). "
                f"Need Allow → redirect 127.0.0.1:56121."
            )

        _log(
            f"OAuth code OK in {time.time() - t0:.1f}s "
            f"(len={len(code)} prefix={code[:8]}…) → token exchange"
        )
        tokens = exchange_code_for_tokens(code, verifier, proxy=proxy)
        if not tokens.email and email:
            tokens.email = email
        _log(
            f"tokens OK email={tokens.email or '-'} bot_flag={tokens.bot_flag_source!r} "
            f"referrer={tokens.referrer!r} mode={tokens.auth_mode}"
        )
        return tokens
    finally:
        # cleanup playwright routes
        try:
            raw_c, loop_c, asy_c = _unwrap_page(page)
            if used_pw_route and hasattr(raw_c, "unroute"):
                if asy_c and loop_c is not None:
                    loop_c.run_until_complete(raw_c.unroute("http://127.0.0.1:56121/**"))
                    loop_c.run_until_complete(raw_c.unroute("http://localhost:56121/**"))
                else:
                    raw_c.unroute("http://127.0.0.1:56121/**")
                    raw_c.unroute("http://localhost:56121/**")
        except Exception:
            pass
        if cb_server is not None:
            try:
                cb_server.stop()
            except Exception:
                pass


def jwt_gate_decision(
    tokens: BuildTokens,
    *,
    reject_bot_flag: bool = False,
    require_referrer: str = DEFAULT_REFERRER,
    enforce_referrer: bool = True,
) -> Dict[str, Any]:
    """Soft bot_flag by default; hard referrer=grok-build (flash policy)."""
    access = tokens.access_token or ""
    bot = tokens.bot_flag_source
    ref = (tokens.referrer or tokens.jwt_claims.get("referrer") or "").strip()
    out: Dict[str, Any] = {
        "ok": True,
        "reason": "ok",
        "bot_flag_source": bot,
        "referrer": ref,
    }
    if not access:
        out["ok"] = False
        out["reason"] = "missing_access_token"
        return out
    if reject_bot_flag and bot not in (None, "", 0, "0", False):
        out["ok"] = False
        out["reason"] = f"bot_flag_source={bot!r}"
        return out
    if enforce_referrer and require_referrer:
        if ref != require_referrer.strip():
            out["ok"] = False
            out["reason"] = f"referrer={ref!r} want={require_referrer!r}"
            return out
    return out

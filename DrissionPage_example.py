from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import json
import shutil
import tempfile
import datetime
import logging
import time
import os
import platform
import secrets
import sys

from email_register import get_email_and_token, get_oai_code

# Set by main() / run_pool.py
WORKER_ID = os.environ.get("GROK_WORKER_ID", "").strip()
# Progress context for multi-worker logs
WORKER_TOTAL = int(os.environ.get("GROK_WORKER_SHARE", "0") or "0")  # accounts this worker runs
POOL_TOTAL = int(os.environ.get("GROK_POOL_TOTAL", "0") or "0")  # global total
POOL_OFFSET = int(os.environ.get("GROK_POOL_OFFSET", "0") or "0")  # 0-based global start index
_progress_current = 0  # 1-based index within this worker
_progress_ok = 0
_progress_fail = 0
_account_t0 = 0.0  # time.time() when current account started


def setup_run_logger() -> logging.Logger:
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    wid = WORKER_ID or f"p{os.getpid()}"
    log_path = os.path.join(log_dir, f"run_{ts}_w{wid}.log")

    logger = logging.getLogger(f"grok_register.{wid}")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    # Compact time only — progress tag carries worker/account identity
    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info("log_file=%s", log_path)
    return logger


run_logger: logging.Logger = None


def _progress_tag() -> str:
    """
    Compact multi-worker tag, easy to scan when 3–5 workers interleave:

      W2 3/33 · #70/100 · remW 30 · ✓2 ✗0

    Meaning:
      W2 3/33     worker 2, account 3 of this worker's share (33)
      #70/100     global account index / pool total
      remW 30     remaining on THIS worker (incl. current)
      ✓2 ✗0       success / fail counts for this worker so far
    """
    wid = WORKER_ID or "?"
    cur = _progress_current
    wtot = WORKER_TOTAL or 0
    if wtot > 0 and cur > 0:
        local = f"W{wid} {cur}/{wtot}"
        rem_w = max(0, wtot - cur + 1)
    elif cur > 0:
        local = f"W{wid} #{cur}"
        rem_w = None
    else:
        local = f"W{wid}"
        rem_w = wtot if wtot > 0 else None

    parts = [local]
    if POOL_TOTAL > 0 and cur > 0:
        gidx = POOL_OFFSET + cur
        parts.append(f"#{gidx}/{POOL_TOTAL}")
    if rem_w is not None:
        parts.append(f"remW {rem_w}")
    parts.append(f"✓{_progress_ok} ✗{_progress_fail}")
    return " · ".join(parts)


def slog(phase: str, message: str, level: str = "info") -> None:
    """Structured progress log for multi-worker readability."""
    phase_s = (phase or "run").upper().replace(" ", "_")[:14]
    # Fixed-width phase column so lines align when grepping
    phase_col = f"{phase_s:<14}"
    line = f"[{_progress_tag()}] {phase_col} {message}"
    if run_logger is not None:
        if level == "error":
            run_logger.error("%s", line)
        elif level == "warn":
            run_logger.warning("%s", line)
        else:
            run_logger.info("%s", line)
    else:
        print(line, flush=True)


def progress_begin_account(index: int) -> None:
    global _progress_current, _account_t0
    _progress_current = index
    _account_t0 = time.time()
    wtot = WORKER_TOTAL or 0
    gidx = (POOL_OFFSET + index) if POOL_TOTAL > 0 else index
    bar = "═" * 28
    slog(
        "START",
        f"{bar} START account "
        f"{index}" + (f"/{wtot}" if wtot else "")
        + (f"  global#{gidx}/{POOL_TOTAL}" if POOL_TOTAL > 0 else "")
        + f"  {bar}",
    )


def progress_end_account(ok: bool, detail: str = "") -> None:
    global _progress_ok, _progress_fail
    elapsed = (time.time() - _account_t0) if _account_t0 else 0
    elapsed_s = f"{elapsed:.0f}s" if elapsed else "?"
    if ok:
        _progress_ok += 1
        slog("OK", f"done in {elapsed_s}  {detail}".strip())
    else:
        _progress_fail += 1
        slog("FAIL", f"failed after {elapsed_s}  {detail}".strip(), level="error")
    # Quick worker scoreboard after each account
    wtot = WORKER_TOTAL or 0
    done = _progress_ok + _progress_fail
    left = max(0, wtot - done) if wtot else "?"
    slog(
        "SCORE",
        f"worker tally  ✓{_progress_ok}  ✗{_progress_fail}  "
        f"done {done}" + (f"/{wtot}" if wtot else "") + f"  left≈{left}",
    )



def ensure_stable_python_runtime():
    # Prefer Python 3.12/3.13 for better TLS compatibility (Mail.tm edge cases on 3.14).
    if sys.version_info < (3, 14) or os.environ.get("DPE_REEXEC_DONE") == "1":
        return

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Programs", "Python", "Python312", "python.exe"),
        os.path.join(local_app_data, "Programs", "Python", "Python313", "python.exe"),
    ]

    current_python = os.path.normcase(os.path.abspath(sys.executable))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        if os.path.normcase(os.path.abspath(candidate)) == current_python:
            return

        print(f"[*] Detected Python {sys.version.split()[0]}, switching to more stable interpreter: {candidate}")
        env = os.environ.copy()
        env["DPE_REEXEC_DONE"] = "1"
        os.execve(candidate, [candidate, os.path.abspath(__file__), *sys.argv[1:]], env)


def warn_runtime_compatibility():
    # Tip: TLS issues on 3.14 are often runtime-related, not farm logic bugs.
    if sys.version_info >= (3, 14):
        print("[Tip] Python 3.14+ detected; if you hit TLS errors, use Python 3.12 or 3.13.")


ensure_stable_python_runtime()
warn_runtime_compatibility()

# Linux headless server: optional Xvfb. Skip on macOS (no Xvfb; use headed display).
_virtual_display = None
if platform.system() == "Linux" and (
    not os.environ.get("DISPLAY") or os.environ.get("USE_XVFB") == "1"
):
    try:
        from pyvirtualdisplay import Display
        _virtual_display = Display(visible=0, size=(1920, 1080))
        _virtual_display.start()
        print(f"[*] Xvfb virtual display started: {os.environ.get('DISPLAY')}")
    except Exception as e:
        print(f"[Warn] Xvfb failed to start: {e}; continuing without it")

import shutil
import glob as _glob_mod
import socket

# turnstile extension
EXTENSION_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "turnstilePatch"))

# Browser proxy: env (per-worker pool) overrides config.json
_browser_proxy = (
    os.environ.get("GROK_BROWSER_PROXY")
    or os.environ.get("BROWSER_PROXY")
    or ""
).strip()
if not _browser_proxy:
    try:
        import json as _json_mod
        _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
        if os.path.isfile(_cfg_path):
            with open(_cfg_path, "r") as _f:
                _cfg = _json_mod.load(_f)
            _browser_proxy = str(_cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or "")
    except Exception:
        pass
if _browser_proxy:
    print(f"[*] Browser proxy: {_browser_proxy}")

# Global browser handles — options rebuilt per start_browser() for multi-worker isolation.
co = None
_chrome_temp_dir: str = ""
_chrome_debug_port: int = 0
browser = None
page = None

# Display mode for Mac multitasking (env / CLI / config):
#   headed    — normal windows (steals focus on macOS)
#   offscreen — headed but parked off-screen + defocus (best balance on Mac)
#   headless  — no window; Turnstile may fail more often
DISPLAY_MODE = (
    os.environ.get("GROK_DISPLAY")
    or os.environ.get("DISPLAY_MODE")
    or "headed"
).strip().lower()
if DISPLAY_MODE in ("bg", "background", "minimized", "minimise", "minimize"):
    DISPLAY_MODE = "offscreen"
if DISPLAY_MODE not in ("headed", "offscreen", "headless"):
    DISPLAY_MODE = "headed"

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

_sso_dir = os.path.join(os.path.dirname(__file__), "sso")
_sso_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
DEFAULT_SSO_FILE = os.path.join(_sso_dir, f"sso_{_sso_ts}.txt")


def _playwright_cache_roots() -> list[str]:
    """Locations where `playwright install chromium` stores browsers."""
    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, "Library", "Caches", "ms-playwright"),  # macOS
        os.path.join(home, ".cache", "ms-playwright"),  # Linux
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),  # Windows
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""),
    ]
    return [r for r in roots if r and os.path.isdir(r)]


def _resolve_browser_path() -> str:
    """
    Prefer Playwright Chromium / Chrome for Testing — NEVER Google Chrome.app.

    Isolation from the user's daily Chrome:
    - different binary (Chromium / Chrome for Testing)
    - unique user-data-dir per worker (set in start_browser)
    - unique CDP port per worker
    """
    env_path = (os.environ.get("GROK_BROWSER_PATH") or "").strip()
    if env_path and os.path.isfile(env_path):
        return env_path

    patterns = [
        # macOS — newer Playwright: Chrome for Testing
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        # macOS — older Playwright: Chromium.app
        "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
        # Linux
        "chromium-*/chrome-linux*/chrome",
        # Windows
        "chromium-*/chrome-win*/chrome.exe",
    ]
    found: list[str] = []
    for root in _playwright_cache_roots():
        for pat in patterns:
            found.extend(_glob_mod.glob(os.path.join(root, pat)))
    # Prefer highest chromium-* build (sort reverse path)
    found = sorted({p for p in found if os.path.isfile(p)}, reverse=True)
    if found:
        return found[0]

    # Optional system Chromium (not Google Chrome) if user installed it
    system = platform.system()
    if system == "Darwin":
        for cand in (
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        ):
            if os.path.isfile(cand):
                return cand
    elif system == "Linux":
        for cand in (
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            # google-chrome last resort only if env allows
        ):
            if os.path.isfile(cand):
                return cand

    if os.environ.get("GROK_ALLOW_SYSTEM_CHROME", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        if system == "Darwin":
            mac_chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            if os.path.isfile(mac_chrome):
                return mac_chrome
        elif system == "Linux":
            for cand in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
                if os.path.isfile(cand):
                    return cand

    return ""


def _pick_free_port(preferred: int) -> int:
    """Bind a free local port, preferring `preferred` then nearby, then OS-assigned."""
    for port in range(max(1024, preferred), max(1024, preferred) + 80):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _worker_port_base() -> int:
    """Stable base port per worker so concurrent pool never shares CDP port."""
    env_port = (os.environ.get("GROK_DEBUG_PORT") or "").strip()
    if env_port.isdigit():
        return int(env_port)
    wid = (WORKER_ID or os.environ.get("GROK_WORKER_ID") or "").strip()
    if wid.isdigit():
        # worker 1 → 9310, worker 2 → 9330, ... leave room for retries
        return 9300 + int(wid) * 20
    # single-process fallback: avoid default 9222 which collides with other tools
    return 9400 + (os.getpid() % 500)


def build_chromium_options(profile_dir: str, debug_port: int) -> ChromiumOptions:
    """
    Fresh ChromiumOptions per browser start.

    IMPORTANT: do NOT use co.auto_port() for concurrent workers.
    DrissionPage PortFinder cleans ~/tmp/DrissionPage/autoPortData and can
    rmtree another worker's profile if the port check races — that kills worker 1
    when worker 2 starts. Use explicit set_local_port + unique user-data-dir.
    """
    # read_file=False → ignore package configs.ini (default address 127.0.0.1:9222)
    opts = ChromiumOptions(read_file=False)
    opts.set_local_port(debug_port)
    opts.set_user_data_path(profile_dir)
    opts.set_argument("--no-sandbox")
    opts.set_argument("--disable-gpu")
    opts.set_argument("--disable-dev-shm-usage")
    opts.set_argument("--disable-software-rasterizer")
    opts.set_argument("--no-first-run")
    opts.set_argument("--no-default-browser-check")
    opts.set_argument("--disable-popup-blocking")
    opts.set_timeouts(base=1)

    mode = (DISPLAY_MODE or "headed").lower()
    if mode == "headless":
        # True headless — no Dock focus, but Turnstile/captcha harder.
        opts.headless(True)
        opts.set_argument("--window-size", "1280,900")
    elif mode == "offscreen":
        # Headed engine (better for Turnstile) but park window off-screen.
        # On macOS still may flash once; we defocus/hide process right after start.
        opts.set_argument("--window-size", "1100,800")
        # Far off primary display — user won't see it while working.
        opts.set_argument("--window-position", "-32000,-32000")
        opts.set_argument("--start-maximized", False)  # remove if set
    else:
        opts.set_argument("--window-size", "1100,800")

    browser_path = _resolve_browser_path()
    if browser_path:
        opts.set_browser_path(browser_path)
    else:
        print(
            "[Warn] Playwright Chromium not found. Run: "
            "pip install playwright && playwright install chromium\n"
            "       Or set GROK_BROWSER_PATH=/path/to/chromium. "
            "Refusing to use daily Google Chrome unless GROK_ALLOW_SYSTEM_CHROME=1."
        )
    if os.path.isdir(EXTENSION_PATH):
        opts.add_extension(EXTENSION_PATH)
    if _browser_proxy:
        opts.set_proxy(_browser_proxy)
    return opts


_window_policy_logged = False


def _apply_window_policy(quiet: bool = True):
    """
    Keep the farm window out of the way for the whole process lifetime.
    - offscreen: minimize + park far off-screen (CDP only; never hide Google Chrome)
    Re-call after navigations / soft reset so OS focus steals get undone.
    """
    global _window_policy_logged
    if DISPLAY_MODE != "offscreen":
        return
    if browser is None or page is None:
        return
    try:
        if not hasattr(page, "run_cdp"):
            return
        win = page.run_cdp("Browser.getWindowForTarget")
        window_id = win.get("windowId") if isinstance(win, dict) else None
        if window_id is None:
            return
        # minimized stays in Dock and should not pop to foreground on every nav
        page.run_cdp(
            "Browser.setWindowBounds",
            windowId=window_id,
            bounds={
                "left": -32000,
                "top": -32000,
                "width": 1100,
                "height": 800,
                "windowState": "minimized",
            },
        )
        if not quiet or not _window_policy_logged:
            print("[*] Farm window minimized+off-screen (stays for this browser process)")
            _window_policy_logged = True
    except Exception as e:
        if not quiet:
            print(f"[Warn] window policy failed: {e}")


def start_browser():
    # One Playwright Chromium process per worker: unique profile + CDP port.
    global browser, page, _chrome_temp_dir, _chrome_debug_port, co, _window_policy_logged
    wid = WORKER_ID or os.environ.get("GROK_WORKER_ID") or str(os.getpid())
    _chrome_temp_dir = tempfile.mkdtemp(prefix=f"grok_pw_w{wid}_")
    _chrome_debug_port = _pick_free_port(_worker_port_base())
    co = build_chromium_options(_chrome_temp_dir, _chrome_debug_port)
    browser_path = _resolve_browser_path()
    binary_label = browser_path or "(DrissionPage default — install Playwright Chromium!)"
    if browser_path and "Google Chrome.app" in browser_path and "Testing" not in browser_path:
        print("[Warn] Using Google Chrome.app — may affect your daily browser. Prefer Playwright Chromium.")
    print(
        f"[*] Browser start worker={wid} port={_chrome_debug_port} "
        f"display={DISPLAY_MODE}\n"
        f"    binary={binary_label}\n"
        f"    profile={_chrome_temp_dir}"
    )
    if not browser_path:
        raise RuntimeError(
            "Playwright Chromium not found. Install with:\n"
            "  pip install playwright && playwright install chromium\n"
            "Or set GROK_BROWSER_PATH to a Chromium / Chrome for Testing binary."
        )
    _window_policy_logged = False
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    # Minimize immediately so the single flash (if any) is only on first launch.
    _apply_window_policy(quiet=False)
    return browser, page


def stop_browser():
    # Full quit + cleanup temp profile for this worker only.
    global browser, page, _chrome_temp_dir, _chrome_debug_port, co, _window_policy_logged
    if browser is not None:
        try:
            browser.quit()
        except Exception:
            pass
    browser = None
    page = None
    co = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""
    _chrome_debug_port = 0
    _window_policy_logged = False


def soft_reset_browser():
    """
    Reset session for the next account WITHOUT relaunching the process.
    Avoids focus blink / new window on top every round.
    """
    global page
    if browser is None:
        start_browser()
        return
    try:
        tabs = list(browser.get_tabs() or [])
        # Keep one tab, close the rest
        if tabs:
            page = tabs[-1]
            for t in tabs[:-1]:
                try:
                    t.close()
                except Exception:
                    pass
        else:
            page = browser.new_tab()

        try:
            page.run_js(
                "try{localStorage.clear();sessionStorage.clear();}catch(e){}"
            )
        except Exception:
            pass
        try:
            page.clear_cache(session_storage=True, cookies=True)
        except Exception:
            pass
        # CDP cookie/cache wipe (best effort)
        if hasattr(page, "run_cdp"):
            try:
                page.run_cdp("Network.clearBrowserCookies")
            except Exception:
                pass
            try:
                page.run_cdp("Network.clearBrowserCache")
            except Exception:
                pass
        try:
            page.get("about:blank")
        except Exception:
            pass
        _apply_window_policy(quiet=True)
        print("[*] Soft reset browser (same process, no new window)")
    except Exception as e:
        print(f"[Warn] Soft reset failed ({e}); full restart")
        stop_browser()
        start_browser()


def restart_browser(force_full: bool = False):
    """
    Default: soft reset (reuse process). Full relaunch only when force_full=True
    or soft path cannot recover.
    """
    if force_full or browser is None:
        stop_browser()
        start_browser()
    else:
        soft_reset_browser()


def refresh_active_page():
    # After OTP confirm the page may navigate; refresh the active tab handle.
    global browser, page
    if browser is None:
        start_browser()
    try:
        tabs = browser.get_tabs()
        if tabs:
            page = tabs[-1]
        else:
            page = browser.new_tab()
        _apply_window_policy(quiet=True)
    except Exception:
        restart_browser(force_full=True)
    return page


def open_signup_page():
    # Open signup page and switch to email registration flow.
    global page
    refresh_active_page()
    try:
        page.get(SIGNUP_URL)
    except Exception:
        refresh_active_page()
        page = browser.new_tab(SIGNUP_URL)
    _apply_window_policy(quiet=True)
    click_email_signup_button()
    _apply_window_policy(quiet=True)


def close_current_page():
    # Legacy name: soft reset between rounds (not a full process restart).
    restart_browser(force_full=False)


def has_profile_form():
    # Final signup form is ready when name + password fields are visible.
    refresh_active_page()
    try:
        return bool(page.run_js(
            """
const givenInput = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
return !!(givenInput && familyInput && passwordInput);
            """
        ))
    except Exception:
        return False


def click_email_signup_button(timeout=10):
    # Click the "Sign up with email" control after the page loads.
    deadline = time.time() + timeout
    while time.time() < deadline:
        clicked = page.run_js(r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return text.includes('使用邮箱注册') || text.includes('signupwithemail') || text.includes('signupemail') || text.includes('continuewith email') || text.includes('email');
});

if (!target) {
    return false;
}

target.click();
return true;
        """)

        if clicked:
            return True

        time.sleep(0.5)

    raise Exception('Could not find "Sign up with email" button')


def fill_email_and_submit(timeout=15):
    # Create catch-all email via email_register; keep token for OTP step.
    slog("EMAIL", "generating catch-all alias…")
    email, dev_token = get_email_and_token()
    if not email or not dev_token:
        raise Exception("Failed to create email")
    slog("EMAIL", f"alias={email}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        filled = page.run_js(
            """
const email = arguments[0];

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input) {
    return 'not-ready';
}

input.focus();
input.click();

// Setting input.value alone is not enough for React controlled inputs.
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) {
    tracker.setValue('');
}
if (valueSetter) {
    valueSetter.call(input, email);
} else {
    input.value = email;
}

input.dispatchEvent(new InputEvent('beforeinput', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: email,
    inputType: 'insertText',
}));
input.dispatchEvent(new Event('change', { bubbles: true }));

if ((input.value || '').trim() !== email || !input.checkValidity()) {
    return false;
}

input.blur();
return 'filled';
            """,
            email,
        )

        if filled == 'not-ready':
            time.sleep(0.5)
            continue

        if filled != 'filled':
            print(f"[Debug] Email input visible but fill failed: {filled}")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(0.8)
            clicked = page.run_js(
                r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const input = Array.from(document.querySelectorAll('input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly;
}) || null;

if (!input || !input.checkValidity() || !(input.value || '').trim()) {
    return false;
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '注册' || text.includes('注册') || t === 'signup' || t === 'sign up' || t.includes('sign up');
});

if (!submitButton || submitButton.disabled) {
    return false;
}

submitButton.click();
return true;
                """
            )

            if clicked:
                slog("EMAIL", f"submitted sign-up form for {email}")
                return email, dev_token

        time.sleep(0.5)

    raise Exception("Email input or sign-up button not found")



def fill_code_and_submit(email, dev_token, timeout=60):
    # Poll IMAP for OTP via email_register, then fill the code.
    slog("OTP", f"waiting IMAP code for {email}…")
    code = get_oai_code(dev_token, email, timeout=120)
    if not code:
        raise Exception("Failed to get verification code")
    slog("OTP", f"got code={code} — filling…")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();

function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

function setNativeValue(input, value) {
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) {
        tracker.setValue('');
    }
    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(input, '');
        nativeInputValueSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
}

function dispatchInputEvents(input, value) {
    input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        cancelable: true,
        data: value,
        inputType: 'insertText',
    }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}

const input = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || code.length || 6) > 1;
}) || null;

const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) {
        return false;
    }
    const maxLength = Number(node.maxLength || 0);
    const autocomplete = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || autocomplete === 'one-time-code';
});

if (!input && otpBoxes.length < code.length) {
    return 'not-ready';
}

if (input) {
    input.focus();
    input.click();
    setNativeValue(input, code);
    dispatchInputEvents(input, code);

    const normalizedValue = String(input.value || '').trim();
    const expectedLength = Number(input.maxLength || code.length || 6);
    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;

    if (normalizedValue !== code) {
        return 'aggregate-mismatch';
    }

    if (expectedLength > 0 && normalizedValue.length !== expectedLength) {
        return 'aggregate-length-mismatch';
    }

    if (slots.length && filledSlots && filledSlots !== normalizedValue.length) {
        return 'aggregate-slot-mismatch';
    }

    input.blur();
    return 'filled';
}

const orderedBoxes = otpBoxes.slice(0, code.length);
for (let i = 0; i < orderedBoxes.length; i += 1) {
    const box = orderedBoxes[i];
    const char = code[i] || '';
    box.focus();
    box.click();
    setNativeValue(box, char);
    dispatchInputEvents(box, char);
    box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: char }));
    box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: char }));
    box.blur();
}

const merged = orderedBoxes.map((node) => String(node.value || '').trim()).join('');
return merged === code ? 'filled' : 'box-mismatch';
                """,
                code,
            )
        except PageDisconnectedError:
            # After confirm, navigation may disconnect the old handle — switch to the new page.
            refresh_active_page()
            if has_profile_form():
                slog("OTP", "navigated to final signup after OTP submit")
                return code
            time.sleep(1)
            continue

        if filled == 'not-ready':
            if has_profile_form():
                slog("OTP", "already on final signup; skip OTP confirm")
                return code
            time.sleep(0.5)
            continue

        if filled != 'filled':
            slog("OTP", f"fill retry status={filled}", level="warn")
            time.sleep(0.5)
            continue

        if filled == 'filled':
            time.sleep(1.2)
            try:
                clicked = page.run_js(
                    r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const aggregateInput = Array.from(document.querySelectorAll('input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]')).find((node) => {
    return isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 0) > 1;
}) || null;

let value = '';
if (aggregateInput) {
    value = String(aggregateInput.value || '').trim();
    const expectedLength = Number(aggregateInput.maxLength || value.length || 6);
    if (!value || (expectedLength > 0 && value.length !== expectedLength)) {
        return false;
    }

    const slots = Array.from(document.querySelectorAll('[data-input-otp-slot="true"]'));
    if (slots.length) {
        const filledSlots = slots.filter((slot) => (slot.textContent || '').trim()).length;
        if (filledSlots && filledSlots !== value.length) {
            return false;
        }
    }
} else {
    const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
        if (!isVisible(node) || node.disabled || node.readOnly) {
            return false;
        }
        const maxLength = Number(node.maxLength || 0);
        const autocomplete = String(node.autocomplete || '').toLowerCase();
        return maxLength === 1 || autocomplete === 'one-time-code';
    });
    value = otpBoxes.map((node) => String(node.value || '').trim()).join('');
    if (!value || value.length < 6) {
        return false;
    }
}

const buttons = Array.from(document.querySelectorAll('button[type="submit"], button')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const confirmButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const t = text.toLowerCase(); return text === '确认邮箱' || text.includes('确认邮箱') || text === '继续' || text.includes('继续') || text === '下一步' || text.includes('下一步') || t.includes('confirm') || t.includes('continue') || t.includes('next') || t.includes('verify');
});

if (!confirmButton) {
    return 'no-button';
}

confirmButton.focus();
confirmButton.click();
return 'clicked';
                    """
                )
            except PageDisconnectedError:
                refresh_active_page()
                if has_profile_form():
                    slog("OTP", "email confirmed → final signup page")
                    return code
                clicked = 'disconnected'

            if clicked == 'clicked':
                slog("OTP", f"confirmed code={code}")
                time.sleep(2)
                refresh_active_page()
                if has_profile_form():
                    slog("OTP", "final signup page ready")
                return code

            if clicked == 'no-button':
                current_url = page.url
                if 'sign-up' in current_url or 'signup' in current_url:
                    slog("OTP", f"auto-nav next step  url={current_url[:100]}")
                    return code

            if clicked == 'disconnected':
                time.sleep(1)
                continue

        time.sleep(0.5)

    debug_snapshot = page.run_js(
        r"""
function isVisible(node) {
    if (!node) {
        return false;
    }
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible).map((node) => ({
    type: node.type || '',
    name: node.name || '',
    testid: node.getAttribute('data-testid') || '',
    autocomplete: node.autocomplete || '',
    maxLength: Number(node.maxLength || 0),
    value: String(node.value || ''),
}));

const buttons = Array.from(document.querySelectorAll('button')).filter(isVisible).map((node) => ({
    text: String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim(),
    disabled: !!node.disabled,
    ariaDisabled: node.getAttribute('aria-disabled') || '',
}));

return { url: location.href, inputs, buttons };
        """
    )
    print(f"[Debug] OTP page DOM snapshot: {debug_snapshot}")
    raise Exception("OTP input or confirm-email button not found")


def _read_turnstile_token() -> str:
    """Read existing CF token from widget/input without resetting."""
    try:
        tok = page.run_js(
            r"""
try {
  const r = (window.turnstile && turnstile.getResponse) ? turnstile.getResponse() : null;
  if (r && String(r).trim()) return String(r).trim();
} catch (e) {}
const inp = document.querySelector('input[name="cf-turnstile-response"]');
if (inp && String(inp.value || '').trim()) return String(inp.value).trim();
return '';
            """
        )
        return str(tok or "").strip()
    except Exception:
        return ""


def getTurnstileToken(reset: bool = False):
    """
    Solve Turnstile on the final form.
    IMPORTANT: do NOT reset by default — reset destroys a natural Success and
    forces a new challenge; injected tokens often don't wire into React state.
    """
    # Already solved?
    existing = _read_turnstile_token()
    if existing and len(existing) > 20:
        return existing

    if reset:
        try:
            page.run_js("try { turnstile.reset() } catch(e) { }")
        except Exception:
            pass

    for _ in range(0, 18):
        try:
            turnstileResponse = _read_turnstile_token()
            if turnstileResponse:
                return turnstileResponse

            # Click the checkbox inside the turnstile iframe (human path)
            challengeSolution = page.ele("@name=cf-turnstile-response", timeout=0.5)
            if not challengeSolution:
                time.sleep(0.8)
                continue
            challengeWrapper = challengeSolution.parent()
            challengeIframe = challengeWrapper.shadow_root.ele("tag:iframe")

            challengeIframe.run_js(
                """
window.dtp = 1
function getRandomInt(min, max) {
    return Math.floor(Math.random() * (max - min + 1)) + min;
}
let screenX = getRandomInt(800, 1200);
let screenY = getRandomInt(400, 600);
Object.defineProperty(MouseEvent.prototype, 'screenX', { value: screenX });
Object.defineProperty(MouseEvent.prototype, 'screenY', { value: screenY });
                """
            )

            challengeIframeBody = challengeIframe.ele("tag:body").shadow_root
            challengeButton = challengeIframeBody.ele("tag:input")
            challengeButton.click()
        except Exception:
            pass
        time.sleep(1)

    # Last resort: reset once and try a few more clicks
    if not reset:
        slog("TURNSTILE", "no token yet — one soft reset + retry", level="warn")
        return getTurnstileToken(reset=True)

    raise Exception("failed to solve turnstile")


_NAME_FILE = os.path.join(os.path.dirname(__file__), "name.txt")
_name_pool: list = []
_name_pool_mtime: float = 0.0
_name_rr_lock = None  # lazy threading.Lock for sequential rotate


def _load_name_pool(force: bool = False) -> list:
    """Load Given|Family or 'Given Family' lines from name.txt (hot-reload on mtime)."""
    global _name_pool, _name_pool_mtime
    try:
        mtime = os.path.getmtime(_NAME_FILE) if os.path.isfile(_NAME_FILE) else 0.0
    except OSError:
        mtime = 0.0
    if not force and _name_pool and mtime == _name_pool_mtime:
        return _name_pool

    pool = []
    if os.path.isfile(_NAME_FILE):
        try:
            with open(_NAME_FILE, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "|" in line:
                        given, family = line.split("|", 1)
                    else:
                        parts = line.split()
                        if len(parts) == 1:
                            given, family = parts[0], parts[0]
                        else:
                            given, family = parts[0], " ".join(parts[1:])
                    given, family = given.strip(), family.strip()
                    if given and family:
                        pool.append((given, family))
        except Exception as e:
            print(f"[Warn] Failed to read name.txt: {e}")

    if not pool:
        pool = [("Neo", "Lin")]
        print("[Warn] name.txt empty/missing — fallback Neo Lin")

    _name_pool = pool
    _name_pool_mtime = mtime
    return pool


def _next_display_name() -> tuple:
    """
    Rotate names from name.txt.
    - Default: round-robin with a small on-disk counter (name_rr.index) so workers
      don't all pick the same name.
    - GROK_NAME_RANDOM=1 → pure random pick.
    """
    import threading

    global _name_rr_lock
    pool = _load_name_pool()
    if os.environ.get("GROK_NAME_RANDOM", "").strip().lower() in ("1", "true", "yes"):
        return secrets.choice(pool)

    if _name_rr_lock is None:
        _name_rr_lock = threading.Lock()
    index_path = os.path.join(os.path.dirname(__file__), "name_rr.index")
    with _name_rr_lock:
        idx = 0
        try:
            if os.path.isfile(index_path):
                idx = int(open(index_path, "r", encoding="utf-8").read().strip() or "0")
        except Exception:
            idx = 0
        given, family = pool[idx % len(pool)]
        try:
            with open(index_path, "w", encoding="utf-8") as f:
                f.write(str(idx + 1))
        except Exception:
            pass
        return given, family


def build_profile():
    # Generate signup profile; display name rotated from name.txt.
    given_name, family_name = _next_display_name()
    password = "N" + secrets.token_hex(4) + "!a7#" + secrets.token_urlsafe(6)
    return given_name, family_name, password


def _dismiss_cookie_banner() -> bool:
    """Click Accept All Cookies if the consent bar is blocking the form."""
    try:
        return bool(
            page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function norm(s) { return String(s || '').toLowerCase().replace(/\s+/g, ' ').trim(); }
const nodes = Array.from(document.querySelectorAll('button, [role="button"], a'));
// Prefer accept; never click reject (can break analytics/CF)
const accept = nodes.find((n) => {
  if (!isVisible(n)) return false;
  const t = norm(n.innerText || n.textContent || n.getAttribute('aria-label'));
  return (
    t === 'accept all cookies' ||
    t === 'accept all' ||
    t === 'accept cookies' ||
    t === 'allow all' ||
    t === 'i agree' ||
    t.includes('accept all cookie')
  );
});
if (!accept) return false;
accept.click();
return true;
                """
            )
        )
    except Exception:
        return False


def _page_error_snip() -> str:
    """Grab visible error / alert text on the signup form (if any)."""
    try:
        msg = page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
const sels = [
  '[role="alert"]', '[data-testid*="error"]', '.error', '.text-red-500',
  '[class*="error"]', '[class*="Error"]', '[class*="destructive"]'
];
const out = [];
for (const sel of sels) {
  for (const n of document.querySelectorAll(sel)) {
    if (!isVisible(n)) continue;
    const t = String(n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
    if (t && t.length > 2 && t.length < 180) out.push(t);
  }
}
// also scan short red-ish paragraphs
return [...new Set(out)].slice(0, 4).join(' | ');
            """
        )
        return str(msg or "").strip()
    except Exception:
        return ""


def _diagnose_signup_form() -> dict:
    """Snapshot form state when Complete sign up is sticky (for logs)."""
    try:
        d = page.run_js(
            r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function norm(s) { return String(s || '').toLowerCase().replace(/\s+/g, ' ').trim(); }
const challengeInput = document.querySelector('input[name="cf-turnstile-response"]');
const token = challengeInput ? String(challengeInput.value || '').trim() : '';
let tsResp = '';
try { tsResp = (window.turnstile && turnstile.getResponse) ? String(turnstile.getResponse() || '') : ''; } catch (e) {}
const body = (document.body && document.body.innerText) || '';
const successUi = /success!/i.test(body);
const given = document.querySelector('input[data-testid="givenName"], input[name="givenName"]');
const family = document.querySelector('input[data-testid="familyName"], input[name="familyName"]');
const pass = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
const submit = Array.from(document.querySelectorAll('button')).find((b) => {
  if (!isVisible(b)) return false;
  return norm(b.innerText || b.textContent).includes('complete sign');
});
const cookieBanner = Array.from(document.querySelectorAll('button')).some((b) => {
  if (!isVisible(b)) return false;
  const t = norm(b.innerText || b.textContent);
  return t.includes('accept all cookie') || t.includes('reject all');
});
return {
  url: location.href.slice(0, 100),
  tokenLen: token.length,
  tsRespLen: tsResp.length,
  successUi: successUi,
  given: given ? String(given.value || '').slice(0, 20) : null,
  family: family ? String(family.value || '').slice(0, 20) : null,
  passLen: pass ? String(pass.value || '').length : 0,
  btnDisabled: submit ? !!(submit.disabled || submit.getAttribute('aria-disabled') === 'true') : null,
  cookieBanner: cookieBanner,
};
            """
        ) or {}
        err = _page_error_snip()
        if err:
            d["err"] = err[:160]
        return d
    except Exception as e:
        return {"error": str(e)}


def _fill_profile_fields(given_name: str, family_name: str, password: str) -> str:
    """Fill name/password once. Returns filled | not-ready | filled-failed | verify-failed."""
    return page.run_js(
        """
const givenName = arguments[0];
const familyName = arguments[1];
const password = arguments[2];

function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function pickInput(selector) {
    return Array.from(document.querySelectorAll(selector)).find((node) => {
        return isVisible(node) && !node.disabled && !node.readOnly;
    }) || null;
}
function setInputValue(input, value) {
    if (!input) return false;
    input.focus();
    input.click();
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) {
        nativeSetter.call(input, '');
        nativeSetter.call(input, value);
    } else {
        input.value = '';
        input.value = value;
    }
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, cancelable: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    input.dispatchEvent(new Event('blur', { bubbles: true }));
    return String(input.value || '') === String(value || '');
}

const givenInput = pickInput('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
const familyInput = pickInput('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
const passwordInput = pickInput('input[data-testid="password"], input[name="password"], input[type="password"]');
if (!givenInput || !familyInput || !passwordInput) return 'not-ready';

// Skip rewrite if already correct (avoids thrashing React state on retries)
const already =
  String(givenInput.value || '').trim() === String(givenName || '').trim()
  && String(familyInput.value || '').trim() === String(familyName || '').trim()
  && String(passwordInput.value || '') === String(password || '');
if (already) return 'filled';

const givenOk = setInputValue(givenInput, givenName);
const familyOk = setInputValue(familyInput, familyName);
const passwordOk = setInputValue(passwordInput, password);
if (!givenOk || !familyOk || !passwordOk) return 'filled-failed';

return (
  String(givenInput.value || '').trim() === String(givenName || '').trim()
  && String(familyInput.value || '').trim() === String(familyName || '').trim()
  && String(passwordInput.value || '') === String(password || '')
) ? 'filled' : 'verify-failed';
        """,
        given_name,
        family_name,
        password,
    )


def _click_complete_signup(strategy: str = "mouse") -> str:
    """
    Click Complete sign up.

    strategy:
      mouse  — real DrissionPage element click (preferred; trusted)
      js     — element.click() only (no form.requestSubmit spam)
      form   — form.requestSubmit last resort
    """
    # Ensure token present before clicking
    token_ok = page.run_js(
        r"""
const inp = document.querySelector('input[name="cf-turnstile-response"]');
const v = inp ? String(inp.value || '').trim() : '';
if (v.length > 20) return true;
try {
  const r = (window.turnstile && turnstile.getResponse) ? String(turnstile.getResponse() || '') : '';
  if (r.length > 20 && inp) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (nativeSetter) nativeSetter.call(inp, r); else inp.value = r;
    inp.dispatchEvent(new Event('input', { bubbles: true }));
    inp.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
  }
} catch (e) {}
const body = (document.body && document.body.innerText) || '';
return /success!/i.test(body);
        """
    )
    if not token_ok:
        return "no-token"

    # Check button state
    btn_state = page.run_js(
        r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function norm(s) { return String(s || '').toLowerCase().replace(/\s+/g, ' ').trim(); }
const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
const btn = buttons.find((n) => {
  if (!isVisible(n)) return false;
  const t = norm(n.innerText || n.textContent || n.value);
  return t.includes('complete sign') || t === 'create account' || t.includes('完成注册');
});
if (!btn) return 'no-button';
const disabled = !!(btn.disabled || btn.getAttribute('aria-disabled') === 'true' || btn.classList.contains('disabled'));
return disabled ? 'disabled' : 'ready';
        """
    )
    if btn_state == "no-button":
        return "no-button"
    if btn_state == "disabled":
        return "disabled"

    if strategy == "form":
        try:
            st = page.run_js(
                r"""
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function norm(s) { return String(s || '').toLowerCase().replace(/\s+/g, ' ').trim(); }
const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
const btn = buttons.find((n) => {
  if (!isVisible(n)) return false;
  const t = norm(n.innerText || n.textContent || n.value);
  return t.includes('complete sign') || t === 'create account';
});
if (!btn) return 'no-button';
const form = btn.closest('form') || document.querySelector('form');
if (form && typeof form.requestSubmit === 'function') {
  try { form.requestSubmit(btn); return 'clicked:form'; } catch (e) {}
}
try { btn.click(); return 'clicked:js'; } catch (e) {}
return 'fail';
                """
            )
            return st or "fail"
        except Exception as e:
            return f"error:{e}"

    # mouse / js via element
    selectors = [
        "tag:button@@text():Complete sign up",
        "tag:button@@text():Complete signup",
        "tag:button@@text():Create Account",
        "tag:button@@text()=完成注册",
        'xpath://button[contains(translate(normalize-space(.), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "complete sign")]',
        'xpath://button[@type="submit"]',
    ]
    for sel in selectors:
        try:
            btn = page.ele(sel, timeout=0.5)
        except Exception:
            btn = None
        if not btn:
            continue
        try:
            # scroll into view
            try:
                btn.scroll.to_see()
            except Exception:
                pass
            if strategy == "js":
                try:
                    page.run_js("arguments[0].click()", btn)
                except Exception:
                    btn.click(by_js=True)
                return f"clicked:js:{sel[:40]}"
            # preferred: real mouse click
            try:
                # actions chain if available
                page.actions.move_to(btn).click()
                return f"clicked:actions:{sel[:40]}"
            except Exception:
                btn.click()
                return f"clicked:mouse:{sel[:40]}"
        except Exception:
            continue
    return "no-button-mouse"


def _signup_still_on_profile() -> bool:
    """True if we are still stuck on the final name/password form."""
    try:
        if has_profile_form():
            # Confirm the submit button is still there (not mid-transition)
            still = page.run_js(
                r"""
const t = ((document.body && document.body.innerText) || '').toLowerCase();
return t.includes('complete sign up') || t.includes('create a free account');
                """
            )
            return bool(still)
        return False
    except Exception:
        return True


def fill_profile_and_submit(timeout=75):
    """
    Final signup form: name + password + Turnstile + Complete sign up.

    Design (learned from flaky runs):
      - Fill fields ONCE (re-fill thrashing kills React state)
      - NEVER turnstile.reset() unless totally stuck
      - Dismiss cookie banner first
      - Prefer real mouse click over form.requestSubmit spam
      - Only re-click on retry; verify we actually leave the form
    """
    given_name, family_name, password = build_profile()
    slog("PROFILE", f"fill form  name={given_name} {family_name}")
    deadline = time.time() + timeout
    fields_filled = False
    turnstile_ready = False
    last_click_status = ""
    click_attempts = 0
    last_heartbeat = 0.0
    cookie_dismissed = False

    # Strategy rotation for clicks
    strategies = ["mouse", "mouse", "js", "actions_via_mouse", "form"]

    while time.time() < deadline:
        now = time.time()
        if now - last_heartbeat >= 10:
            rem = max(0, int(deadline - now))
            slog(
                "PROFILE",
                f"…waiting  filled={fields_filled}  cf={turnstile_ready}  "
                f"click_try={click_attempts}  last={last_click_status or '-'}  t-{rem}s",
            )
            last_heartbeat = now

        # Cookie bar blocks real clicks on some viewports
        if not cookie_dismissed:
            if _dismiss_cookie_banner():
                slog("PROFILE", "dismissed cookie banner (Accept All)")
                time.sleep(0.4)
            cookie_dismissed = True  # try once early; re-try later if needed

        # ── 1) fill fields (only if not already correct) ──
        if not fields_filled:
            filled = _fill_profile_fields(given_name, family_name, password)
            if filled == "not-ready":
                time.sleep(0.6)
                continue
            if filled != "filled":
                slog("PROFILE", f"fill status={filled}", level="warn")
                time.sleep(0.5)
                continue
            fields_filled = True
            slog("PROFILE", "fields OK — waiting Turnstile")
            time.sleep(0.5)

        # ── 2) wait / solve turnstile WITHOUT reset ──
        token = _read_turnstile_token()
        if token and len(token) > 20:
            if not turnstile_ready:
                slog("TURNSTILE", f"ready  token_len={len(token)}")
            turnstile_ready = True
        else:
            turnstile_ready = False
            # First try: wait briefly for natural / auto solve (no reset)
            if click_attempts == 0:
                slog("TURNSTILE", "waiting natural solve…")
                natural_deadline = time.time() + 5
                clicked_box = False
                while time.time() < natural_deadline:
                    token = _read_turnstile_token()
                    if token and len(token) > 20:
                        turnstile_ready = True
                        slog("TURNSTILE", f"natural ok  token_len={len(token)}")
                        break
                    # mid-wait: one interactive checkbox click (still no reset)
                    if not clicked_box and time.time() > natural_deadline - 2.5:
                        clicked_box = True
                        try:
                            getTurnstileToken(reset=False)
                        except Exception:
                            pass
                    time.sleep(0.6)

            if not turnstile_ready:
                slog("TURNSTILE", "solving (no reset)…")
                try:
                    token = getTurnstileToken(reset=False)
                except Exception as e:
                    slog("TURNSTILE", f"solve error: {e}", level="warn")
                    token = ""
                if token and len(token) > 20:
                    turnstile_ready = True
                    slog("TURNSTILE", f"solved  token_len={len(token)}")
                else:
                    time.sleep(1.0)
                    continue

            # beat for React to enable Complete sign up after CF Success
            time.sleep(1.2)

        # Re-dismiss cookie bar if it reappeared
        if _dismiss_cookie_banner():
            slog("PROFILE", "cookie banner again — accepted", level="warn")
            time.sleep(0.3)

        # Ensure fields still correct (don't thrash — helper skips if OK)
        chk = _fill_profile_fields(given_name, family_name, password)
        if chk != "filled":
            fields_filled = False
            slog("PROFILE", f"fields lost ({chk}) — will re-fill", level="warn")
            continue

        # ── 3) click Complete sign up ──
        strat = strategies[click_attempts % len(strategies)]
        if strat == "actions_via_mouse":
            strat = "mouse"
        click_attempts += 1
        try:
            status = _click_complete_signup(strategy=strat) or ""
        except Exception as e:
            status = f"error:{e}"
        last_click_status = status

        if status == "disabled":
            if click_attempts % 2 == 1:
                slog("SUBMIT", f"button still disabled  try={click_attempts}  diag={_diagnose_signup_form()}", level="warn")
            # wait for React to enable after CF
            time.sleep(1.2)
            continue

        if status == "no-token":
            turnstile_ready = False
            slog("SUBMIT", "token missing at click time — re-solve", level="warn")
            time.sleep(0.8)
            continue

        if status in ("no-button", "no-button-mouse"):
            slog("SUBMIT", f"{status}  try={click_attempts}  diag={_diagnose_signup_form()}", level="warn")
            time.sleep(0.8)
            continue

        if "clicked" not in str(status) and not str(status).startswith("error"):
            time.sleep(0.6)
            continue

        slog("SUBMIT", f"click ({status})  name={given_name} {family_name}  try={click_attempts}")

        # Wait for leave / network — do NOT re-fill during this wait
        left = False
        for i in range(14):  # ~7s
            time.sleep(0.5)
            if not _signup_still_on_profile():
                left = True
                break
            err = _page_error_snip()
            if err and i in (3, 8):
                slog("SUBMIT", f"page error: {err}", level="warn")

        if left:
            slog("SUBMIT", "left profile form ✓")
            _wait_post_register_settle(timeout=25)
            return {
                "given_name": given_name,
                "family_name": family_name,
                "password": password,
            }

        # Still stuck — diagnose, maybe cookie, maybe need different click strat
        diag = _diagnose_signup_form()
        slog("SUBMIT", f"still on form after click  status={status}  diag={diag}", level="warn")
        if diag.get("cookieBanner"):
            cookie_dismissed = False  # force re-dismiss next loop
        # If token vanished, re-solve
        if not diag.get("tokenLen") and not diag.get("tsRespLen"):
            turnstile_ready = False
        time.sleep(0.6)

    diag = _diagnose_signup_form()
    raise Exception(
        f"Stuck on Complete sign up (last_click={last_click_status}, "
        f"attempts={click_attempts}, diag={diag})"
    )


def _wait_post_register_settle(timeout=25):
    """
    Setelah klik Create Account: tunggu auth settle di accounts.x.ai.
    Jangan open grok.com di sini — itu tugas wait_for_sso_cookie setelah SSO ada.
    """
    deadline = time.time() + timeout
    last_url = ""
    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(0.5)
                continue
            try:
                cur = str(getattr(page, "url", "") or "")
            except Exception:
                cur = ""
            if cur != last_url:
                slog("SETTLE", f"url={cur[:120]}")
                last_url = cur

            wanted, _ = _collect_grok_session_cookies()
            # Success signals:
            #  - already on grok.com (natural redirect)
            #  - left sign-up form and SSO cookie present
            #  - profile form gone + sso cookie
            on_grok = "grok.com" in cur
            left_signup = "sign-up" not in cur and "sign_up" not in cur
            has_sso = bool(wanted.get("sso"))

            if on_grok and has_sso:
                slog("SETTLE", "ok — grok.com + SSO")
                time.sleep(1.5)
                return
            if has_sso and left_signup:
                slog("SETTLE", "ok — SSO + left signup")
                time.sleep(1.5)
                return
            if has_sso and not has_profile_form():
                slog("SETTLE", "ok — SSO + profile form gone")
                time.sleep(1.5)
                return
        except PageDisconnectedError:
            refresh_active_page()
        except Exception:
            pass
        time.sleep(0.8)
    slog("SETTLE", "timeout — lanjut collect SSO di halaman sekarang", level="warn")


def extract_visible_numbers(timeout=60):
    # After login/signup, extract visible numeric text only (no sensitive cookies).
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = page.run_js(
            r"""
function isVisible(el) {
    if (!el) {
        return false;
    }
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
        return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}

const selector = [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'div', 'span', 'p', 'strong', 'b', 'small',
    '[data-testid]', '[class]', '[role="heading"]'
].join(',');

const seen = new Set();
const matches = [];
for (const node of document.querySelectorAll(selector)) {
    if (!isVisible(node)) {
        continue;
    }
    const text = String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text) {
        continue;
    }
    const found = text.match(/\d+(?:\.\d+)?/g);
    if (!found) {
        continue;
    }
    for (const value of found) {
        const key = `${value}@@${text}`;
        if (seen.has(key)) {
            continue;
        }
        seen.add(key);
        matches.push({ value, text });
    }
}

return matches.slice(0, 30);
            """
        )

        if result:
            print("[*] Visible numeric text on page:")
            for item in result:
                try:
                    print(f"    - number: {item['value']} | context: {item['text']}")
                except Exception:
                    pass
            return result

        time.sleep(1)

    raise Exception("No visible numeric text found after login")


# Cookie names we harvest for grok-web (aligned with 9router grokWebAuth / grok2api)
_GROK_SSO_NAMES = ("sso", "sso-rw")
_GROK_CF_NAMES = ("cf_clearance", "__cf_bm", "cf_chl_rc_i", "cf_chl_2", "cf_chl_prog", "cf_chl_seq")


def _iter_browser_cookies():
    """Yield (name, value, domain) from all domains."""
    refresh_active_page()
    if page is None:
        return
    cookies = page.cookies(all_domains=True, all_info=True) or []
    for item in cookies:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            value = str(item.get("value", "")).strip()
            domain = str(item.get("domain", "") or item.get("host", "") or "").strip()
        else:
            name = str(getattr(item, "name", "")).strip()
            value = str(getattr(item, "value", "")).strip()
            domain = str(getattr(item, "domain", "") or "").strip()
        if name and value:
            yield name, value, domain


def _collect_grok_session_cookies():
    """
    Collect sso / sso-rw / Cloudflare cookies from browser.
    Prefer values bound to grok.com or x.ai.
    """
    wanted = {}
    names_seen = set()
    for name, value, domain in _iter_browser_cookies() or []:
        names_seen.add(name)
        low = name.lower()
        low_dom = domain.lower()
        if low not in _GROK_SSO_NAMES and low not in _GROK_CF_NAMES and not low.startswith("cf_"):
            continue
        prefer = "grok.com" in low_dom or "x.ai" in low_dom
        if low not in wanted or prefer:
            wanted[low] = value
    return wanted, names_seen


def normalize_grok_web_credential(raw) -> dict:
    """
    Normalize farmed cookie material into the format expected by 9router grok-web
    (open-sse/utils/grokWebAuth.js buildSSOCookie):

      apiKey:  "sso=<jwt>; sso-rw=<jwt|sso-rw>"   # explicit grok2api-style header
      providerSpecificData.cloudflareCookies: "cf_clearance=...; __cf_bm=..."  (optional)

    Accepts:
      - bare JWT
      - "sso=JWT"
      - "sso=JWT; sso-rw=...; cf_clearance=..."
      - dict with sso / sso_rw / cloudflare_cookies / cookie_header keys
    """
    sso = ""
    sso_rw = ""
    cf_parts: list[str] = []

    if isinstance(raw, dict):
        # Prefer already-normalized / header fields, then explicit sso pieces
        header = str(raw.get("apiKey") or raw.get("cookie_header") or "").strip()
        if header and ("sso=" in header.lower() or header.count(".") >= 2):
            parsed = normalize_grok_web_credential(header)
            # Explicit overrides from structured farm result
            if raw.get("sso") or raw.get("sso_token"):
                parsed["sso_token"] = str(raw.get("sso") or raw.get("sso_token")).strip()
            if raw.get("sso_rw") or raw.get("sso-rw"):
                parsed["sso_rw"] = str(raw.get("sso_rw") or raw.get("sso-rw")).strip()
            cf_extra = str(raw.get("cloudflare_cookies") or raw.get("cloudflareCookies") or "").strip()
            if cf_extra:
                existing = parsed.get("cloudflare_cookies") or ""
                # merge unique pairs
                bag = {}
                for part in (existing + ";" + cf_extra).split(";"):
                    part = part.strip()
                    if "=" in part:
                        k, v = part.split("=", 1)
                        bag[k.strip().lower()] = f"{k.strip().lower()}={v.strip()}"
                parsed["cloudflare_cookies"] = "; ".join(bag.values())
            sso_tok = parsed.get("sso_token") or ""
            rw_tok = parsed.get("sso_rw") or sso_tok
            if sso_tok:
                parsed["apiKey"] = f"sso={sso_tok}; sso-rw={rw_tok}"
            psd = {}
            if parsed.get("cloudflare_cookies"):
                psd["cloudflareCookies"] = parsed["cloudflare_cookies"]
            parsed["providerSpecificData"] = psd
            return parsed

        sso = str(raw.get("sso") or raw.get("sso_token") or raw.get("token") or "").strip()
        sso_rw = str(raw.get("sso_rw") or raw.get("sso-rw") or "").strip()
        cf_raw = str(raw.get("cloudflare_cookies") or raw.get("cloudflareCookies") or "").strip()
        if cf_raw:
            for part in cf_raw.split(";"):
                part = part.strip()
                if part and "=" in part:
                    cf_parts.append(part)
    else:
        text = str(raw or "").strip()
        if not text:
            return {"apiKey": "", "sso_token": "", "sso_rw": "", "cloudflare_cookies": "", "providerSpecificData": {}}

        if "=" in text and ("sso" in text.lower() or "cf_" in text.lower() or "__cf" in text.lower()):
            for part in text.split(";"):
                part = part.strip()
                if not part or "=" not in part:
                    continue
                k, v = part.split("=", 1)
                k, v = k.strip().lower(), v.strip()
                if not v:
                    continue
                if k == "sso":
                    sso = v
                elif k == "sso-rw":
                    sso_rw = v
                elif k in _GROK_CF_NAMES or k.startswith("cf_"):
                    cf_parts.append(f"{k}={v}")
        elif text.lower().startswith("sso="):
            sso = text[4:].split(";", 1)[0].strip()
        else:
            # bare JWT
            sso = text.split(";", 1)[0].strip()

    sso = sso.replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    sso_rw = sso_rw.replace("\r", "").replace("\n", "").replace("\x00", "").strip()
    if not sso:
        return {"apiKey": "", "sso_token": "", "sso_rw": "", "cloudflare_cookies": "", "providerSpecificData": {}}

    # grok2api BuildSSOCookie: always set sso-rw (mirror sso when missing)
    if not sso_rw:
        sso_rw = sso

    api_key = f"sso={sso}; sso-rw={sso_rw}"
    cf_header = "; ".join(dict.fromkeys(cf_parts))  # dedupe preserve order
    psd = {}
    if cf_header:
        psd["cloudflareCookies"] = cf_header

    return {
        "apiKey": api_key,
        "sso_token": sso,
        "sso_rw": sso_rw,
        "cloudflare_cookies": cf_header,
        "providerSpecificData": psd,
    }


def wait_for_sso_cookie(timeout=90):
    """
    Collect SSO after register — JANGAN buru-buru buka grok.com.

    Phases:
      1) wait_sso  — poll cookie di session sekarang (accounts.x.ai).
                     Tunggu sso muncul / redirect natural dulu.
      2) settle    — baru optional buka grok.com (setelah sso ada)
                     biar cf_clearance + binding domain settle.

    Returns normalized credential dict for Build convert / storage.
    """
    global page
    deadline = time.time() + timeout
    last_seen_names = set()
    phase = "wait_sso"
    sso_ready_at = None
    visited_grok = False
    last_url = ""

    while time.time() < deadline:
        try:
            refresh_active_page()
            if page is None:
                time.sleep(1)
                continue

            try:
                cur = str(getattr(page, "url", "") or "")
            except Exception:
                cur = ""
            if cur and cur != last_url:
                slog("SSO", f"url={cur[:140]}")
                last_url = cur

            wanted, names_seen = _collect_grok_session_cookies()
            last_seen_names |= names_seen
            has_sso = bool(wanted.get("sso"))

            # ── Phase 1: tunggu SSO tanpa navigasi paksa ──
            if phase == "wait_sso":
                if has_sso:
                    slog("SSO", "cookie muncul — settle session…")
                    phase = "settle"
                    sso_ready_at = time.time()
                else:
                    # Kalau natural redirect ke grok.com, lanjut collect di sana
                    if "grok.com" in cur:
                        slog("SSO", "natural redirect → grok.com")
                        phase = "settle"
                        visited_grok = True
                        sso_ready_at = time.time()
                    time.sleep(1)
                    continue

            # ── Phase 2: settle + optional grok.com ──
            if phase == "settle":
                # Minimal settle time setelah SSO ketemu (auth finish)
                if sso_ready_at and (time.time() - sso_ready_at) < 3.0:
                    time.sleep(0.5)
                    continue

                # Baru buka grok.com SETELAH sso ada, dan hanya jika belum di grok.com
                if has_sso and not visited_grok and "grok.com" not in cur:
                    slog("SSO", "open https://grok.com/ to settle cookies")
                    try:
                        page.get("https://grok.com/")
                    except Exception:
                        refresh_active_page()
                        try:
                            page.get("https://grok.com/")
                        except Exception:
                            pass
                    visited_grok = True
                    # kasih waktu load + set cf cookies
                    time.sleep(4)
                    wanted, names_seen = _collect_grok_session_cookies()
                    last_seen_names |= names_seen
                    has_sso = bool(wanted.get("sso"))

                if has_sso:
                    # extra beat after grok.com load
                    if visited_grok:
                        time.sleep(1.5)
                        wanted, _ = _collect_grok_session_cookies()
                    raw = {
                        "sso": wanted.get("sso"),
                        "sso_rw": wanted.get("sso-rw") or "",
                        "cloudflare_cookies": "; ".join(
                            f"{k}={wanted[k]}"
                            for k in list(_GROK_CF_NAMES)
                            + [x for x in wanted if x.startswith("cf_")]
                            if k in wanted
                        ),
                    }
                    cred = normalize_grok_web_credential(raw)
                    parts = [cred["apiKey"]]
                    if cred.get("cloudflare_cookies"):
                        parts.append(cred["cloudflare_cookies"])
                    cred["cookie_header"] = "; ".join(parts)
                    slog(
                        "SSO",
                        f"ready  sso=yes  sso-rw={'yes' if wanted.get('sso-rw') else 'mirrored'}  "
                        f"cf={'yes' if cred.get('cloudflare_cookies') else 'no'}  "
                        f"key={cred['apiKey'][:40]}…",
                    )
                    return cred

        except PageDisconnectedError:
            refresh_active_page()
        except Exception as e:
            slog("SSO", f"wait error: {e}", level="warn")

        time.sleep(1)

    raise Exception(
        f"No sso cookie after signup; cookies seen: {sorted(last_seen_names)}"
    )


def append_sso_to_txt(sso_value, output_path=DEFAULT_SSO_FILE):
    # One line per account. Prefer full cookie header string.
    if isinstance(sso_value, dict):
        normalized = str(
            sso_value.get("cookie_header")
            or sso_value.get("apiKey")
            or sso_value.get("sso")
            or ""
        ).strip()
    else:
        normalized = str(sso_value or "").strip()
    if not normalized:
        raise Exception("SSO value is empty; nothing to write")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "a", encoding="utf-8") as file:
        file.write(normalized + "\n")

    print(f"[*] Appended SSO to file: {output_path}")


def _load_config() -> dict:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _compute_9router_cli_token(data_dir: str | None = None) -> str | None:
    """
    Replicate 9router getConsistentMachineId('9r-cli-auth'):
      sha256(machine-id + '9r-cli-auth' + cli-secret).hex[:16]
    """
    import hashlib
    from pathlib import Path

    base = Path(data_dir or os.path.expanduser("~/.9router"))
    mid_path = base / "machine-id"
    secret_path = base / "auth" / "cli-secret"
    if not mid_path.is_file() or not secret_path.is_file():
        return None
    raw = mid_path.read_text(encoding="utf-8").strip()
    secret = secret_path.read_text(encoding="utf-8").strip()
    if not raw or not secret:
        return None
    return hashlib.sha256(f"{raw}9r-cli-auth{secret}".encode()).hexdigest()[:16]


def push_sso_to_9router(accounts: list) -> None:
    """
    Auto-import farmed SSO cookies into 9router as provider `grok-web`.

    Payload (matches 9router POST /api/providers + grokWebAuth.buildSSOCookie):
      {
        "provider": "grok-web",
        "apiKey": "sso=<jwt>; sso-rw=<jwt>",
        "name": "<email>",
        "email": "<email>",
        "providerSpecificData": {
          "cloudflareCookies": "cf_clearance=..."   // optional
        },
        "testStatus": "unknown"
      }

    Config (config.json → ninerouter):
      enabled: true
      base_url: "http://127.0.0.1:20127"
      provider: "grok-web"
      data_dir: "~/.9router"
      cli_token: ""           # optional override
    """
    import requests

    conf = _load_config()
    nr = conf.get("ninerouter") or conf.get("nine_router") or {}
    if not isinstance(nr, dict):
        # No ninerouter block → try defaults (enabled)
        nr = {"enabled": True}
    if nr.get("enabled") is False:
        print("[*] 9router import disabled (ninerouter.enabled=false)")
        return

    base_url = str(nr.get("base_url") or "http://127.0.0.1:20127").rstrip("/")
    provider = str(nr.get("provider") or "grok-web").strip() or "grok-web"
    data_dir = os.path.expanduser(str(nr.get("data_dir") or "~/.9router"))
    cli_token = str(nr.get("cli_token") or "").strip() or _compute_9router_cli_token(data_dir)

    if not cli_token:
        print("[Warn] 9router CLI token tidak ditemukan (~/.9router/machine-id + auth/cli-secret). Skip import.")
        return

    # Normalize every account into grok-web credential shape
    items = []
    for a in accounts or []:
        if isinstance(a, dict):
            email = str(a.get("email") or "").strip()
            name = str(a.get("name") or email or "").strip()
            # Prefer structured fields from wait_for_sso_cookie
            if a.get("apiKey") or a.get("sso_token") or a.get("cookie_header") or a.get("sso"):
                cred = normalize_grok_web_credential(a)
            else:
                cred = normalize_grok_web_credential(
                    a.get("token") or a.get("apiKey") or ""
                )
        else:
            email, name = "", ""
            cred = normalize_grok_web_credential(a)

        if not cred.get("apiKey"):
            continue
        items.append({
            "apiKey": cred["apiKey"],
            "email": email,
            "name": name,
            "providerSpecificData": cred.get("providerSpecificData") or {},
            "sso_token": cred.get("sso_token") or "",
        })

    if not items:
        print("[Warn] 9router import: no valid SSO credentials to push")
        return

    headers = {
        "Content-Type": "application/json",
        "x-9r-cli-token": cli_token,
    }

    # Existing connection names for light dedupe (apiKey is redacted on GET)
    existing_names = set()
    try:
        get_resp = requests.get(f"{base_url}/api/providers", headers=headers, timeout=15)
        if get_resp.status_code == 200:
            for c in (get_resp.json() or {}).get("connections") or []:
                if c.get("provider") == provider and c.get("name"):
                    existing_names.add(str(c["name"]))
        else:
            print(f"[Warn] 9router list connections HTTP {get_resp.status_code}")
    except Exception as e:
        print(f"[Warn] 9router list connections gagal (lanjut import): {e}")

    ok, skipped, fail = 0, 0, 0
    for item in items:
        api_key = item["apiKey"]
        email = item["email"]
        # Stable name: farm email (unique per registration)
        name = item["name"] or (email if email else f"farm-sso-{(item.get('sso_token') or api_key)[:12]}")
        if name in existing_names:
            print(f"[*] 9router skip (name exists): {name}")
            skipped += 1
            continue

        payload = {
            "provider": provider,
            "apiKey": api_key,  # "sso=...; sso-rw=..."
            "name": name,
            "email": email or None,
            "testStatus": "unknown",
        }
        psd = item.get("providerSpecificData") or {}
        if psd:
            payload["providerSpecificData"] = psd

        print(f"[*] 9router POST {provider} name={name}")
        print(f"    apiKey={api_key[:56]}{'...' if len(api_key) > 56 else ''}")
        if psd.get("cloudflareCookies"):
            print(f"    cloudflareCookies={str(psd['cloudflareCookies'])[:48]}...")

        try:
            resp = requests.post(
                f"{base_url}/api/providers",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                conn_id = (resp.json() or {}).get("connection", {}).get("id", "")
                print(f"[*] 9router imported → {provider} name={name} id={conn_id}")
                existing_names.add(name)
                ok += 1
            else:
                print(f"[Warn] 9router import gagal HTTP {resp.status_code}: {resp.text[:240]}")
                fail += 1
        except Exception as e:
            print(f"[Warn] 9router import error: {e}")
            fail += 1

    print(f"[*] 9router import done: ok={ok} skipped={skipped} fail={fail} @ {base_url} provider={provider}")


def _grok2api_login(base_url: str, username: str, password: str) -> str | None:
    """Login admin console, return accessToken or None."""
    import requests

    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/admin/v1/auth/login",
            json={"username": username, "password": password},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[Warn] grok2api login HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        data = resp.json()
        token = (
            (data.get("data") or {}).get("tokens", {}).get("accessToken")
            or (data.get("tokens") or {}).get("accessToken")
        )
        if not token:
            print(f"[Warn] grok2api login: no accessToken in response")
            return None
        return str(token)
    except Exception as e:
        print(f"[Warn] grok2api login failed: {e}")
        return None


def _parse_sse_complete(text: str) -> dict | None:
    """Extract last `event: complete` JSON payload from grok2api SSE body."""
    event = None
    last_complete = None
    for line in (text or "").splitlines():
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:") and event == "complete":
            raw = line[len("data:"):].strip()
            try:
                last_complete = json.loads(raw)
            except Exception:
                pass
        elif line.startswith("data:") and event == "error":
            raw = line[len("data:"):].strip()
            try:
                err = json.loads(raw)
                print(f"[Warn] grok2api import SSE error: {err}")
            except Exception:
                print(f"[Warn] grok2api import SSE error: {raw[:200]}")
    return last_complete


def push_sso_to_grok2api(accounts: list) -> None:
    """
    Auto-import farmed Web SSO accounts into modern grok2api.

    Uses admin API (NOT the old ssoBasic app_key endpoint from original repo):
      1) POST /api/admin/v1/auth/login
      2) POST /api/admin/v1/accounts/web/import  (multipart file)

    Import document (matches backend/internal/infra/provider/web/import.go):
      {
        "provider": "grok_web",
        "accounts": [
          {
            "name": "user@domain",
            "sso_token": "<raw JWT>",
            "tier": "auto",
            "cloudflare_cookies": "cf_clearance=..."
          }
        ]
      }

    Config (config.json → grok2api or legacy api.*):
      grok2api.enabled / base_url / username / password / tier
    """
    import io
    import requests

    conf = _load_config()
    g2a = conf.get("grok2api") if isinstance(conf.get("grok2api"), dict) else {}
    api_legacy = conf.get("api") if isinstance(conf.get("api"), dict) else {}

    # Prefer grok2api block; fall back to legacy api.* fields where sensible
    enabled = g2a.get("enabled")
    if enabled is None:
        # auto-enable if credentials present
        enabled = bool(
            g2a.get("base_url") or g2a.get("password")
            or api_legacy.get("endpoint") or api_legacy.get("username")
        )
    if enabled is False:
        print("[*] grok2api import disabled")
        return

    base_url = str(
        g2a.get("base_url")
        or api_legacy.get("base_url")
        or api_legacy.get("endpoint")
        or "http://127.0.0.1:8000"
    ).rstrip("/")
    # If legacy endpoint was a full token URL, strip path to origin
    if "/api/" in base_url:
        base_url = base_url.split("/api/")[0].rstrip("/")

    username = str(g2a.get("username") or api_legacy.get("username") or "admin").strip()
    password = str(g2a.get("password") or api_legacy.get("password") or api_legacy.get("token") or "").strip()
    tier = str(g2a.get("tier") or "auto").strip().lower() or "auto"
    if tier not in ("auto", "basic", "super", "heavy"):
        tier = "auto"

    if not password:
        print("[Warn] grok2api password/token kosong di config — skip import")
        return

    # Build account list
    entries = []
    for a in accounts or []:
        if isinstance(a, dict):
            cred = normalize_grok_web_credential(a)
            email = str(a.get("email") or "").strip()
            name = str(a.get("name") or email or "").strip()
        else:
            cred = normalize_grok_web_credential(a)
            email, name = "", ""

        sso_token = (cred.get("sso_token") or "").strip()
        if not sso_token:
            # last resort: try parse apiKey
            sso_token = (cred.get("apiKey") or "").replace("sso=", "").split(";")[0].strip()
        if not sso_token:
            continue
        if not name:
            name = email or f"Grok Web {sso_token[:8]}"

        entry = {
            "name": name,
            "sso_token": sso_token,
            "tier": tier,
        }
        cf = (cred.get("cloudflare_cookies") or "").strip()
        if cf:
            entry["cloudflare_cookies"] = cf
        entries.append(entry)

    if not entries:
        print("[Warn] grok2api import: no valid SSO tokens")
        return

    document = {"provider": "grok_web", "accounts": entries}
    doc_bytes = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")

    print(f"[*] grok2api login @ {base_url} as {username} ({len(entries)} account(s))...")
    access = _grok2api_login(base_url, username, password)
    if not access:
        return

    # Multipart file upload (field name: file)
    files = {
        "file": ("farm-import.json", io.BytesIO(doc_bytes), "application/json"),
    }
    headers = {"Authorization": f"Bearer {access}"}
    try:
        resp = requests.post(
            f"{base_url}/api/admin/v1/accounts/web/import",
            headers=headers,
            files=files,
            timeout=120,
            stream=True,
        )
        body = resp.text
        if resp.status_code != 200:
            print(f"[Warn] grok2api import HTTP {resp.status_code}: {body[:300]}")
            return
        complete = _parse_sse_complete(body)
        if complete:
            print(
                f"[*] grok2api import complete: "
                f"created={complete.get('created', 0)} "
                f"updated={complete.get('updated', 0)} "
                f"skipped={complete.get('skipped', 0)} "
                f"synced={complete.get('synced', 0)} "
                f"syncFailed={complete.get('syncFailed', 0)}"
            )
        else:
            print(f"[*] grok2api import finished (no complete event). body snip: {body[:240]}")
    except Exception as e:
        print(f"[Warn] grok2api import error: {e}")


def push_sso_to_api(new_tokens: list) -> None:
    """
    Backward-compatible entry used by main().

    Original ReinerBRO/grok-register pushed to old grok2api ssoBasic endpoint.
    Current chenyme/grok2api uses admin web import instead — route there.
    Accepts bare token strings or account dicts.
    """
    accounts = []
    for t in new_tokens or []:
        if isinstance(t, dict):
            accounts.append(t)
        elif t:
            accounts.append({"sso": str(t)})
    push_sso_to_grok2api(accounts)


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # One round: open signup -> register -> capture SSO -> write file -> push 9router.
    slog("FLOW", "① open sign-up")
    open_signup_page()
    slog("FLOW", "② email step")
    email, dev_token = fill_email_and_submit()
    slog("FLOW", "③ OTP / IMAP")
    fill_code_and_submit(email, dev_token)
    slog("FLOW", "④ profile + Turnstile + Complete sign up")
    profile = fill_profile_and_submit()
    slog("FLOW", "⑤ wait SSO cookies")
    sso_cred = wait_for_sso_cookie()  # dict: apiKey / sso_token / providerSpecificData
    append_sso_to_txt(sso_cred, output_path)

    if extract_numbers:
        extract_visible_numbers()

    # Flatten for logs + 9router push (structured credential)
    if isinstance(sso_cred, dict):
        result = {
            "email": email,
            "sso": sso_cred.get("sso_token") or sso_cred.get("apiKey") or "",
            "apiKey": sso_cred.get("apiKey") or "",
            "sso_token": sso_cred.get("sso_token") or "",
            "sso_rw": sso_cred.get("sso_rw") or "",
            "cookie_header": sso_cred.get("cookie_header") or sso_cred.get("apiKey") or "",
            "cloudflare_cookies": sso_cred.get("cloudflare_cookies") or "",
            "providerSpecificData": sso_cred.get("providerSpecificData") or {},
            **profile,
        }
    else:
        # backward compat if someone returns a plain string
        cred = normalize_grok_web_credential(sso_cred)
        result = {
            "email": email,
            "sso": cred.get("sso_token") or str(sso_cred),
            "apiKey": cred.get("apiKey") or str(sso_cred),
            "sso_token": cred.get("sso_token") or "",
            "sso_rw": cred.get("sso_rw") or "",
            "cookie_header": cred.get("apiKey") or str(sso_cred),
            "cloudflare_cookies": cred.get("cloudflare_cookies") or "",
            "providerSpecificData": cred.get("providerSpecificData") or {},
            **profile,
        }

    slog(
        "CREATED",
        f"email={email}  name={profile.get('given_name','')} {profile.get('family_name','')}  "
        f"pass={profile.get('password','')}",
    )

    # Primary path: SSO → Device OAuth Build → 9router grok-cli
    try:
        slog("FLOW", "⑥ convert SSO → Build OAuth → 9router")
        convert_and_push_grok_cli(result)
        slog("PUSH", "9router import OK")
    except Exception as e:
        slog("PUSH", f"convert/push failed: {e}", level="error")

    # Optional: grok2api Web pool (off by default)
    try:
        push_sso_to_grok2api([result])
    except Exception as e:
        slog("PUSH", f"grok2api failed: {e}", level="warn")

    # Optional: 9router grok-web cookie import (off by default)
    try:
        push_sso_to_9router([result])
    except Exception as e:
        slog("PUSH", f"grok-web failed: {e}", level="warn")

    slog("DONE", f"account complete  email={email}")
    return result


def convert_and_push_grok_cli(result: dict) -> None:
    """
    Path A: pure HTTP convert Web SSO → Build OAuth, then push to 9router grok-cli
    via POST /api/oauth/grok-cli/import-token (NOT raw SQLite — UI won't see DB-only writes).

    Config (config.json → grok_cli):
      enabled: true
      base_url: "http://127.0.0.1:20127"
      data_dir: "~/.9router"
    """
    conf = _load_config()
    gcli = conf.get("grok_cli") if isinstance(conf.get("grok_cli"), dict) else {}
    if gcli.get("enabled") is False:
        print("[*] grok-cli convert disabled (grok_cli.enabled=false)")
        return

    from push_9router_grok_cli import push_build_tokens_to_9router
    from sso_to_build import convert_sso_to_build

    email = str(result.get("email") or "").strip()
    # Human name for displayName (matches manual OAuth cards like "Neo Lin")
    given = str(result.get("given_name") or "").strip()
    family = str(result.get("family_name") or "").strip()
    display = f"{given} {family}".strip()

    print(f"[*] Converting Web SSO → Build OAuth (email={email or '-'}) ...")
    tokens = convert_sso_to_build(result, name_hint=email)
    push_build_tokens_to_9router(
        tokens,
        base_url=str(gcli.get("base_url") or "http://127.0.0.1:20127"),
        data_dir=str(gcli.get("data_dir") or "~/.9router"),
        # Dashboard password login (Gsuiteto9router style) — works for https://ai.khalid.id
        password=str(gcli.get("password") or gcli.get("dashboard_password") or ""),
        cli_token=str(gcli.get("cli_token") or ""),
        name=email or tokens.name,
        email=email or tokens.email,
        display_name=display or tokens.name,
    )
    result["build_access_token"] = tokens.access_token
    result["build_refresh_token"] = tokens.refresh_token
    result["build_email"] = tokens.email
    result["build_user_id"] = tokens.user_id
    print(f"[*] grok-cli ready: email={tokens.email or email} user_id={tokens.user_id or '-'}")


def load_run_count() -> int:
    # Default run count from config.json (fallback 10).
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        import json
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        v = conf.get("run", {}).get("count")
        if isinstance(v, int) and v >= 0:
            return v
    except Exception:
        pass
    return 10


def load_display_mode_from_config() -> str:
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            conf = json.load(f)
        for key in (
            (conf.get("run") or {}).get("display"),
            (conf.get("pool") or {}).get("display"),
            conf.get("display"),
        ):
            if isinstance(key, str) and key.strip():
                return key.strip().lower()
    except Exception:
        pass
    return ""


def main():
    # Loop registrations; restart browser between rounds.
    global run_logger, WORKER_ID, DEFAULT_SSO_FILE, DISPLAY_MODE
    global WORKER_TOTAL, POOL_TOTAL, POOL_OFFSET

    config_count = load_run_count()
    config_display = load_display_mode_from_config()

    parser = argparse.ArgumentParser(description="xAI auto-register and capture SSO / Grok CLI tokens")
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=config_count,
        help=f"rounds (0=unlimited until stop; default config run.count={config_count})",
    )
    parser.add_argument(
        "-u",
        "--unlimited",
        action="store_true",
        help="farm forever until Ctrl+C / stop (same as -n 0)",
    )
    parser.add_argument("--output", default=None, help="SSO output txt path")
    parser.add_argument("--worker-id", default="", help="Worker id (pool); also set via GROK_WORKER_ID")
    parser.add_argument(
        "--display",
        choices=["headed", "offscreen", "headless"],
        default=None,
        help="headed=normal windows | offscreen=hidden off-screen (Mac work-friendly) | headless=no window",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="shortcut for --display headless",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="shortcut for --display offscreen (recommended on Mac while working)",
    )
    parser.add_argument("--extract-numbers", action="store_true", help="Also extract visible numbers after signup")
    args = parser.parse_args()
    if getattr(args, "unlimited", False):
        args.count = 0

    if args.worker_id:
        WORKER_ID = str(args.worker_id).strip()
        os.environ["GROK_WORKER_ID"] = WORKER_ID

    # Priority: CLI flags > env GROK_DISPLAY > config > default headed
    if args.headless:
        DISPLAY_MODE = "headless"
    elif args.offscreen:
        DISPLAY_MODE = "offscreen"
    elif args.display:
        DISPLAY_MODE = args.display
    elif os.environ.get("GROK_DISPLAY"):
        DISPLAY_MODE = os.environ["GROK_DISPLAY"].strip().lower()
    elif config_display:
        DISPLAY_MODE = config_display
    if DISPLAY_MODE in ("bg", "background", "minimized", "minimise", "minimize"):
        DISPLAY_MODE = "offscreen"
    if DISPLAY_MODE not in ("headed", "offscreen", "headless"):
        DISPLAY_MODE = "headed"
    os.environ["GROK_DISPLAY"] = DISPLAY_MODE

    wid = WORKER_ID or f"p{os.getpid()}"
    if not args.output:
        os.makedirs(_sso_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        args.output = os.path.join(_sso_dir, f"sso_{ts}_w{wid}.txt")

    WORKER_ID = wid
    # This worker's share (from --count); pool env set by run_pool.py
    WORKER_TOTAL = int(args.count) if args.count and args.count > 0 else int(os.environ.get("GROK_WORKER_SHARE", "0") or "0")
    if args.count and args.count > 0:
        WORKER_TOTAL = int(args.count)
    POOL_TOTAL = int(os.environ.get("GROK_POOL_TOTAL", "0") or "0")
    POOL_OFFSET = int(os.environ.get("GROK_POOL_OFFSET", "0") or "0")
    if POOL_TOTAL <= 0 and WORKER_TOTAL > 0:
        POOL_TOTAL = WORKER_TOTAL  # single-process: global == local

    run_logger = setup_run_logger()
    slog(
        "BOOT",
        f"share={WORKER_TOTAL or '∞'}  pool={POOL_TOTAL or WORKER_TOTAL or '∞'}  "
        f"offset={POOL_OFFSET}  display={DISPLAY_MODE}  out={os.path.basename(args.output)}",
    )
    slog(
        "BOOT",
        "log legend: W# local/share · #global/total · remW left-on-worker · ✓ok ✗fail",
    )

    # Pool/TUI sends SIGTERM to the whole process group on quit — close Chromium cleanly.
    def _on_term(signum=None, frame=None):
        try:
            slog("STOP", f"signal {signum} — quitting browser", level="warn")
        except Exception:
            pass
        try:
            stop_browser()
        except Exception:
            pass
        # 130 = typical shell code for Ctrl+C; 143 = 128+SIGTERM
        raise SystemExit(143 if signum == getattr(signal, "SIGTERM", 15) else 130)

    try:
        import signal as _signal

        _signal.signal(_signal.SIGTERM, _on_term)
        _signal.signal(_signal.SIGINT, _on_term)
    except Exception:
        pass

    current_round = 0
    collected_sso: list = []
    try:
        slog("BROWSER", "starting Chromium…")
        start_browser()  # single process for all rounds
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            progress_begin_account(current_round)
            hard_fail = False

            try:
                result = run_single_registration(args.output, extract_numbers=args.extract_numbers)
                collected_sso.append(result.get("sso_token") or result.get("sso") or result.get("apiKey") or "")
                progress_end_account(True, f"email={result.get('email','')}")
            except KeyboardInterrupt:
                slog("STOP", "interrupted by user", level="warn")
                break
            except Exception as error:
                progress_end_account(False, str(error))
                err_l = str(error).lower()
                hard_fail = any(
                    k in err_l
                    for k in (
                        "disconnected",
                        "connection",
                        "target closed",
                        "browser has been closed",
                        "no such",
                        "crash",
                    )
                )
            finally:
                if args.count == 0 or current_round < args.count:
                    slog(
                        "BROWSER",
                        "soft-reset → next account"
                        if not hard_fail
                        else "FULL restart (browser unhealthy)",
                    )
                    restart_browser(force_full=hard_fail)
                    _apply_window_policy(quiet=True)

            if args.count == 0 or current_round < args.count:
                time.sleep(2)

    finally:
        slog(
            "SUMMARY",
            f"worker finished  ✓{_progress_ok}  ✗{_progress_fail}  "
            f"share={WORKER_TOTAL or current_round}",
        )
        if collected_sso:
            push_sso_to_api(collected_sso)
        stop_browser()


if __name__ == "__main__":
    main()

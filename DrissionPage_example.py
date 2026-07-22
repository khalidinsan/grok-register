from DrissionPage import Chromium, ChromiumOptions
from DrissionPage.errors import PageDisconnectedError
import argparse
import json
import shutil
import signal
import tempfile
import datetime
import logging
import time
import os
import platform
import secrets
import sys

# Windows: console/pipe often cp1252 — Unicode in print()/slog (→ ✓ …) must not crash farm
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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
    Compact multi-worker tag — MUST stay parseable by farm_tui._SLOG_RE.

    Finite:
      W2 3/33 · #70/100 · remW 30 · ✓2 ✗0
    Unlimited (∞):
      W2 3 · #3 · ✓2 ✗0
        cur without /share is intentional; TUI accepts both forms.
        ✓ = success (pass)   ✗ = failed   done = ✓+✗ (accounts finished)
    """
    wid = WORKER_ID or "?"
    cur = _progress_current
    wtot = WORKER_TOTAL or 0
    if wtot > 0 and cur > 0:
        local = f"W{wid} {cur}/{wtot}"
        rem_w = max(0, wtot - cur + 1)
    elif cur > 0:
        # unlimited: "W2 3" so TUI can read cur=3 (not "W2 #3" which broke parsing)
        local = f"W{wid} {cur}"
        rem_w = None
    else:
        local = f"W{wid}"
        rem_w = wtot if wtot > 0 else None

    parts = [local]
    if cur > 0:
        gidx = (POOL_OFFSET + cur) if POOL_TOTAL > 0 else cur
        if POOL_TOTAL > 0:
            parts.append(f"#{gidx}/{POOL_TOTAL}")
        else:
            # unlimited global index (no total)
            parts.append(f"#{gidx}")
    if rem_w is not None:
        parts.append(f"remW {rem_w}")
    # success / failed (done is derived as sum in TUI)
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
    """
    Close one account attempt and bump counters.

    Semantics (TUI):
      done     = accounts finished (success + failed) — "berapa acc dibuat/dicoba"
      success  = ✓ pass (imported / pipeline OK)
      failed   = ✗ fail (not pass)
    """
    global _progress_ok, _progress_fail
    elapsed = (time.time() - _account_t0) if _account_t0 else 0
    elapsed_s = f"{elapsed:.0f}s" if elapsed else "?"
    if ok:
        _progress_ok += 1
        slog(
            "OK",
            f"SUCCESS in {elapsed_s}  {detail}".strip()
            + "  → success+1",
        )
    else:
        _progress_fail += 1
        slog(
            "FAIL",
            f"FAILED after {elapsed_s}  {detail}".strip()
            + "  → failed+1",
            level="error",
        )
    # done = accounts finished (always grows in unlimited)
    wtot = WORKER_TOTAL or 0
    done = _progress_ok + _progress_fail
    if wtot > 0:
        left = max(0, wtot - done)
        left_s = f"  left≈{left}/{wtot}"
        done_s = f"done={done}/{wtot}"
    else:
        left_s = "  left=∞"
        done_s = f"done={done}"
    slog(
        "SCORE",
        f"worker tally  done={done}  success={_progress_ok}  failed={_progress_fail}"
        f"  ✓{_progress_ok} ✗{_progress_fail}  {done_s}{left_s}",
    )
    # Explicit RESULT line for easy grepping / TUI scan
    slog(
        "RESULT",
        ("PASS ✓" if ok else "FAIL ✗")
        + f"  account#{_progress_current}"
        + f"  done={done} success={_progress_ok} failed={_progress_fail}"
        + f"  ✓{_progress_ok}/✗{_progress_fail}"
        + (f"  {detail}" if detail else ""),
        level="info" if ok else "error",
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

# Browser proxy pool — env (pool/TUI) overrides config.json
#   GROK_PROXIES     = newline-separated full list (per_account rotate)
#   GROK_BROWSER_PROXY = single sticky proxy (per_worker or legacy)
#   GROK_PROXY_MODE  = per_account | per_worker
_browser_proxy = ""
_proxy_pool: list = []
_proxy_mode = (os.environ.get("GROK_PROXY_MODE") or "per_account").strip().lower()
if _proxy_mode in ("rotate", "account"):
    _proxy_mode = "per_account"
elif _proxy_mode in ("worker", "sticky"):
    _proxy_mode = "per_worker"


def _init_proxy_pool() -> None:
    """Load proxy list once (called at import and safe to re-call)."""
    global _browser_proxy, _proxy_pool, _proxy_mode
    try:
        from proxy_util import (
            decode_proxy_env,
            load_proxy_file,
            load_proxy_list,
            mask_proxy,
            normalize_proxy,
        )
    except Exception:
        decode_proxy_env = None  # type: ignore
        load_proxy_file = None  # type: ignore
        load_proxy_list = None  # type: ignore
        mask_proxy = lambda u: u  # type: ignore
        normalize_proxy = lambda s: (s or "").strip()  # type: ignore

    pool: list = []
    env_list = (os.environ.get("GROK_PROXIES") or "").strip()
    if env_list and decode_proxy_env:
        pool = decode_proxy_env(env_list)
    if not pool:
        single = (
            os.environ.get("GROK_BROWSER_PROXY")
            or os.environ.get("BROWSER_PROXY")
            or ""
        ).strip()
        if single:
            n = normalize_proxy(single)
            pool = [n] if n else [single]
    if not pool:
        try:
            import json as _json_mod

            _cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
            if os.path.isfile(_cfg_path):
                with open(_cfg_path, "r") as _f:
                    _cfg = _json_mod.load(_f)
                pool_cfg = _cfg.get("pool") if isinstance(_cfg.get("pool"), dict) else {}
                mode = str(pool_cfg.get("proxy_mode") or _proxy_mode).strip().lower()
                if mode in ("per_account", "rotate", "account"):
                    _proxy_mode = "per_account"
                elif mode in ("per_worker", "worker", "sticky"):
                    _proxy_mode = "per_worker"
                pf = str(pool_cfg.get("proxy_file") or _cfg.get("proxy_file") or "").strip()
                if pf and load_proxy_file:
                    try:
                        pool = load_proxy_file(pf)
                    except Exception as e:
                        print(f"[Warn] proxy_file: {e}")
                if not pool and load_proxy_list:
                    pool = load_proxy_list(
                        pool_cfg.get("proxies") or pool_cfg.get("proxy_list") or []
                    )
                if not pool:
                    single = str(
                        _cfg.get("browser_proxy", "") or _cfg.get("proxy", "") or ""
                    ).strip()
                    if single:
                        n = normalize_proxy(single)
                        pool = [n] if n else [single]
        except Exception:
            pass

    _proxy_pool = pool
    if _proxy_mode == "per_worker" and pool:
        _browser_proxy = pool[0]
    elif len(pool) == 1:
        _browser_proxy = pool[0]
    else:
        _browser_proxy = ""  # set per account

    if pool:
        print(
            f"[*] Proxy pool: {len(pool)}  mode={_proxy_mode}  "
            f"first={mask_proxy(pool[0]) if pool else '-'}",
            flush=True,
        )


_init_proxy_pool()


def current_proxy_url() -> str:
    """Active proxy for browser + HTTP (convert/smoke)."""
    return (_browser_proxy or os.environ.get("GROK_BROWSER_PROXY") or "").strip()


def select_proxy_for_account(account_index: int) -> str:
    """
    Pick proxy for this account (1-based local index).

    Finite pool: global index = POOL_OFFSET + account_index.
    Unlimited / no pool total: interleave by worker id so concurrent workers
    don't both grab proxies[0] on their local account #1.
    """
    if not _proxy_pool:
        return current_proxy_url()
    try:
        wid = int(WORKER_ID) if str(WORKER_ID).isdigit() else 1
    except Exception:
        wid = 1
    wid = max(1, wid)
    if _proxy_mode == "per_worker":
        return _proxy_pool[(wid - 1) % len(_proxy_pool)]
    # per_account
    if POOL_TOTAL > 0 and account_index > 0:
        gidx = POOL_OFFSET + account_index  # 1-based global
    else:
        # ∞ mode: worker-major interleave
        # W1#1→1, W2#1→2, W1#2→3, W2#2→4, …
        try:
            n_workers = int(os.environ.get("GROK_POOL_CONCURRENT") or "1") or 1
        except Exception:
            n_workers = 1
        n_workers = max(1, n_workers)
        local = max(1, account_index)
        gidx = (local - 1) * n_workers + wid
    if gidx <= 0:
        gidx = 1
    return _proxy_pool[(gidx - 1) % len(_proxy_pool)]


def _env_bool_local(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def is_proxy_failure(err: BaseException | str) -> bool:
    """Flash-aligned: detect dead proxy / tunnel so we can retry or fall direct."""
    s = str(err).lower()
    name = type(err).__name__.lower() if not isinstance(err, str) else ""
    keys = (
        "invalidproxy",
        "failed to connect to proxy",
        "proxy connection",
        "err_proxy",
        "tunnel connection failed",
        "402 payment required",
        "proxy_error",
        "ns_error_proxy",
        "proxy refused",
        "proxy timeout",
        "err_tunnel",
        "socks",
        "net::err_proxy",
        "could not connect to proxy",
        "proxy authentication",
        "407 proxy",
    )
    return any(k in s or k in name for k in keys)


def force_browser_proxy(proxy_url: str, *, account_index: int = 0, reason: str = "") -> str:
    """
    Point browser at proxy_url (empty = direct). Full restart when URL changes.
    Used by per-account pick and by proxy-retry → direct fallback.
    """
    global _browser_proxy
    try:
        from proxy_util import mask_proxy
    except Exception:
        mask_proxy = lambda u: (u[:32] + "…") if u and len(u) > 32 else (u or "")  # type: ignore

    nxt = (proxy_url or "").strip()
    prev = _browser_proxy or ""
    if browser is not None and nxt == prev:
        if reason:
            slog("PROXY", f"reuse  #{account_index or '?'}  {mask_proxy(nxt) if nxt else '(direct)'}  ({reason})")
        else:
            slog(
                "PROXY",
                f"reuse  #{account_index or '?'}  {mask_proxy(nxt) if nxt else '(direct)'}",
            )
        return nxt

    _browser_proxy = nxt
    if nxt:
        os.environ["GROK_BROWSER_PROXY"] = nxt
    else:
        os.environ.pop("GROK_BROWSER_PROXY", None)
        os.environ.pop("BROWSER_PROXY", None)

    slot = ""
    if _proxy_pool and nxt in _proxy_pool:
        slot = f"  slot={_proxy_pool.index(nxt) + 1}/{len(_proxy_pool)}"
    need_restart = browser is not None and prev != nxt
    tag = f"  ({reason})" if reason else ""
    slog(
        "PROXY",
        f"account#{account_index or '?'} → {mask_proxy(nxt) if nxt else '(direct)'}{slot}"
        + (
            "  [full browser restart]"
            if need_restart
            else ("  [start]" if browser is None else "  [same]")
        )
        + tag,
    )
    if browser is None:
        start_browser()
    elif prev != nxt:
        stop_browser()
        start_browser()
    return nxt


def ensure_browser_proxy_for_account(account_index: int) -> str:
    """
    Switch browser to the proxy for this account.
    Full restart only when proxy URL changes (can't hot-swap).
    """
    return force_browser_proxy(
        select_proxy_for_account(account_index),
        account_index=account_index,
    )


def pick_proxy_for_try(
    account_index: int,
    tried: set[str],
    try_i: int,
    *,
    max_proxy_tries: int,
    allow_direct: bool,
) -> tuple[str | None, str]:
    """
    Flash strategy: try up to N distinct proxies, then direct if pool dead.

    Returns (proxy_url_or_None_for_direct, label).
    """
    # Past proxy budget → direct fallback
    if try_i >= max(1, max_proxy_tries) or not _proxy_pool:
        if allow_direct or not _proxy_pool:
            return None, "direct"
        # no direct allowed and no unused proxy
        return None, "direct"

    # Prefer account's assigned proxy first
    primary = select_proxy_for_account(account_index)
    candidates: list[str] = []
    if primary:
        candidates.append(primary)
    for p in _proxy_pool:
        if p not in candidates:
            candidates.append(p)

    for p in candidates:
        key = p or "direct"
        if key in tried:
            continue
        return p, key

    # All proxies exhausted
    if allow_direct:
        return None, "direct"
    return primary or None, primary or "direct"

# Global browser handles — options rebuilt per start_browser() for multi-worker isolation.
co = None
_chrome_temp_dir: str = ""
_chrome_debug_port: int = 0
browser = None
page = None
# Playwright owns Chromium when using native proxy auth (grok-farm style).
# DrissionPage attaches via CDP existing_only — no local bridge required.
_pw = None  # Playwright instance
_pw_context = None  # BrowserContext (persistent) or Browser

# Display mode (flash-aligned):
#   headed    — normal windows (debug Turnstile / CF)
#   offscreen — headed + park window (best on Mac while working)
#   headless  — Camoufox/Chromium true headless (Linux/VPS flash default)
#   virtual   — Camoufox headed on Xvfb (no pure-headless fingerprint)
try:
    from browser_engine import resolve_display

    DISPLAY_MODE = resolve_display(
        os.environ.get("GROK_DISPLAY") or os.environ.get("DISPLAY_MODE") or ""
    )
except Exception:
    DISPLAY_MODE = (
        os.environ.get("GROK_DISPLAY")
        or os.environ.get("DISPLAY_MODE")
        or ("offscreen" if sys.platform == "darwin" else "headless")
    ).strip().lower()
    if DISPLAY_MODE in ("bg", "background", "minimized", "minimise", "minimize"):
        DISPLAY_MODE = "offscreen"
    if DISPLAY_MODE in ("xvfb", "vd"):
        DISPLAY_MODE = "virtual"
    if DISPLAY_MODE not in ("headed", "offscreen", "headless", "virtual"):
        DISPLAY_MODE = "offscreen" if sys.platform == "darwin" else "headless"

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


def _proxy_driver() -> str:
    """
    playwright (default) — same as grok-farm: server + username/password native.
    bridge — legacy local CONNECT forwarder (flaky on auth.grokipedia.com).
    """
    v = (os.environ.get("GROK_PROXY_DRIVER") or "playwright").strip().lower()
    if v in ("bridge", "local", "legacy"):
        return "bridge"
    return "playwright"


def _wait_cdp_port(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.1)
    return False


def build_chromium_options(profile_dir: str, debug_port: int) -> ChromiumOptions:
    """
    Options for DrissionPage when it LAUNCHES Chromium itself (no-proxy path
    or bridge fallback). Prefer start_browser_playwright_proxy() when auth proxy.
    """
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

    mode = (DISPLAY_MODE or "headless").lower()
    # virtual → treat Chromium as real headless (Xvfb must be outside if needed)
    if mode in ("headless", "virtual"):
        opts.headless(True)
        opts.set_argument("--window-size", "1280,900")
    elif mode == "offscreen":
        opts.set_argument("--window-size", "1100,800")
        opts.set_argument("--window-position", "-32000,-32000")
        opts.set_argument("--start-maximized", False)
    else:
        opts.set_argument("--window-size", "1100,800")

    browser_path = _resolve_browser_path()
    if browser_path:
        opts.set_browser_path(browser_path)
    else:
        print(
            "[Warn] Playwright Chromium not found. Run: "
            "pip install playwright && playwright install chromium\n"
            "       Or set GROK_BROWSER_PATH=/path/to/chromium."
        )
    if os.path.isdir(EXTENSION_PATH):
        opts.add_extension(EXTENSION_PATH)

    # Bridge fallback only (legacy). Prefer Playwright native auth.
    if _browser_proxy and _proxy_driver() == "bridge":
        try:
            from proxy_bridge import ensure_local_bridge
            from proxy_util import mask_proxy, parse_proxy
        except Exception as e:
            slog("PROXY", f"bridge import failed: {e}", level="error")
            parse_proxy = None  # type: ignore
            ensure_local_bridge = None  # type: ignore
            mask_proxy = lambda u: u  # type: ignore

        info = parse_proxy(_browser_proxy) if parse_proxy else None
        if info and info.get("host") and info.get("port") and ensure_local_bridge:
            if info.get("has_auth"):
                chrome_server = ensure_local_bridge(
                    info["host"],
                    int(info["port"]),
                    info.get("user") or "",
                    info.get("password") or "",
                    scheme=info.get("scheme") or "http",
                )
                opts.set_argument("--proxy-server", chrome_server)
                slog(
                    "PROXY",
                    f"LEGACY bridge chrome→{chrome_server} upstream="
                    f"{info['host']}:{info['port']}  "
                    f"{mask_proxy(info.get('url') or '')}",
                    level="warn",
                )
            else:
                server = info.get("chrome_server") or (
                    f"http://{info['host']}:{info['port']}"
                )
                opts.set_argument("--proxy-server", server)
        elif info and info.get("chrome_server"):
            opts.set_argument("--proxy-server", info["chrome_server"])
    return opts


def start_browser_playwright_proxy(profile_dir: str, debug_port: int) -> bool:
    """
    Launch Chromium via Playwright with native proxy auth (grok-farm style),
    then attach DrissionPage over CDP.

    Returns True if browser+page ready, False to fall back to DrissionPage launch.
    """
    global browser, page, co, _pw, _pw_context

    if not _browser_proxy:
        return False

    try:
        from playwright.sync_api import sync_playwright
        from proxy_util import mask_proxy, playwright_proxy_dict
    except Exception as e:
        slog("PROXY", f"playwright unavailable ({e}) — fallback", level="warn")
        return False

    proxy_cfg = playwright_proxy_dict(_browser_proxy)
    if not proxy_cfg:
        return False

    browser_path = _resolve_browser_path()
    if not browser_path:
        slog("PROXY", "no chromium binary for playwright launch", level="warn")
        return False

    mode = (DISPLAY_MODE or "headless").lower()
    headless = mode in ("headless", "virtual")
    # Extensions need non-headless on most Chromium builds
    if os.path.isdir(EXTENSION_PATH) and headless:
        slog(
            "PROXY",
            "turnstile extension needs headed/offscreen — using headless=False",
            level="warn",
        )
        headless = False

    args = [
        f"--remote-debugging-port={debug_port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-popup-blocking",
    ]
    if mode == "offscreen":
        args.extend(
            [
                "--window-size=1100,800",
                "--window-position=-32000,-32000",
            ]
        )
    elif mode in ("headless", "virtual"):
        args.append("--window-size=1280,900")
    else:
        args.append("--window-size=1100,800")

    if os.path.isdir(EXTENSION_PATH):
        # load turnstile patch (same as DrissionPage add_extension)
        args.append(f"--disable-extensions-except={EXTENSION_PATH}")
        args.append(f"--load-extension={EXTENSION_PATH}")

    slog(
        "PROXY",
        f"playwright native auth  server={proxy_cfg.get('server')}  "
        f"user={proxy_cfg.get('username', '')[:6]}***  "
        f"full={mask_proxy(_browser_proxy)}  "
        f"(same pattern as grok-farm Camoufox/Playwright)",
    )

    try:
        _pw = sync_playwright().start()
        # persistent context so profile + extensions work
        _pw_context = _pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            executable_path=browser_path,
            headless=headless,
            proxy=proxy_cfg,
            args=args,
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1100, "height": 800},
            locale="en-US",
        )
        # flash-style font/media block on first page
        try:
            from browser_engine import install_asset_block

            p0 = _pw_context.pages[0] if _pw_context.pages else None
            if p0 is not None and install_asset_block(p0, is_async=False):
                slog("BROWSER", "asset-block on (font/media third-party)")
        except Exception as _ab_e:
            slog("BROWSER", f"asset-block skip: {_ab_e}", level="warn")
    except Exception as e:
        slog("PROXY", f"playwright launch failed: {e}", level="error")
        try:
            if _pw:
                _pw.stop()
        except Exception:
            pass
        _pw = None
        _pw_context = None
        return False

    if not _wait_cdp_port(debug_port, timeout=20.0):
        slog("PROXY", f"CDP port {debug_port} not open after playwright launch", level="error")
        try:
            if _pw_context:
                _pw_context.close()
            if _pw:
                _pw.stop()
        except Exception:
            pass
        _pw = None
        _pw_context = None
        return False

    # Attach DrissionPage to Playwright-owned Chromium (do not re-launch)
    opts = ChromiumOptions(read_file=False)
    opts.set_address(f"127.0.0.1:{debug_port}")
    opts.existing_only(True)
    opts.set_timeouts(base=1)
    co = opts
    try:
        browser = Chromium(opts)
        # Prefer an open tab
        try:
            tabs = list(browser.get_tabs() or [])
            page = tabs[-1] if tabs else browser.latest_tab
        except Exception:
            page = browser.latest_tab
        if page is None:
            page = browser.new_tab("about:blank")
    except Exception as e:
        slog("PROXY", f"DrissionPage attach failed: {e}", level="error")
        try:
            if _pw_context:
                _pw_context.close()
            if _pw:
                _pw.stop()
        except Exception:
            pass
        _pw = None
        _pw_context = None
        browser = None
        page = None
        return False

    slog("PROXY", f"attached DrissionPage → CDP 127.0.0.1:{debug_port} OK")
    return True


_window_policy_logged = False


def _apply_window_policy(quiet: bool = True):
    """
    Keep the farm window out of the way for the whole process lifetime.
    - offscreen Chromium: minimize via CDP
    - Camoufox/Firefox: no CDP window API — skip quietly
    """
    global _window_policy_logged
    if DISPLAY_MODE != "offscreen":
        return
    if browser is None or page is None:
        return
    # Camoufox (Firefox) has no Chrome CDP Browser.setWindowBounds
    if _is_pw_adapter_browser() and _resolve_browser_engine() == "camoufox":
        if not quiet and not _window_policy_logged:
            print("[*] Camoufox offscreen: window park skipped (no CDP bounds API)")
            _window_policy_logged = True
        return
    try:
        if not hasattr(page, "run_cdp"):
            return
        win = page.run_cdp("Browser.getWindowForTarget")
        window_id = win.get("windowId") if isinstance(win, dict) else None
        if window_id is None:
            return
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


def _resolve_browser_engine() -> str:
    """camoufox (flash default) | chromium — env → config → camoufox."""
    env = (os.environ.get("GROK_BROWSER_ENGINE") or os.environ.get("BROWSER_ENGINE") or "").strip().lower()
    cfg_engine = ""
    try:
        conf = _load_config()
        b = conf.get("browser") if isinstance(conf.get("browser"), dict) else {}
        cfg_engine = str(b.get("engine") or "").strip().lower()
    except Exception:
        pass
    try:
        from browser_engine import resolve_engine

        return resolve_engine(env or cfg_engine)
    except Exception:
        pass
    for v in (env, cfg_engine):
        if v in ("camoufox", "fox", "firefox", "cf"):
            return "camoufox"
        if v in ("chromium", "chrome", "pw", "playwright"):
            return "chromium"
    return "camoufox"  # flash default


def start_browser():
    # Engine: chromium (Playwright native proxy + Drission attach) or camoufox.
    global browser, page, _chrome_temp_dir, _chrome_debug_port, co, _window_policy_logged
    global _pw, _pw_context
    wid = WORKER_ID or os.environ.get("GROK_WORKER_ID") or str(os.getpid())
    engine = _resolve_browser_engine()
    os.environ["GROK_BROWSER_ENGINE"] = engine

    # ── Camoufox path (flash-grok-farm style) ───────────────────────
    if engine == "camoufox":
        try:
            from browser_engine import launch_camoufox_session

            hl = "?"
            try:
                from browser_engine import camoufox_headless_arg

                hl = camoufox_headless_arg(DISPLAY_MODE)
            except Exception:
                pass
            slog(
                "BROWSER",
                f"engine=camoufox  proxy={_browser_proxy and 'yes' or 'direct'}  "
                f"display={DISPLAY_MODE}  headless={hl!r}",
            )
            sess = launch_camoufox_session(
                proxy=_browser_proxy or "",
                display=DISPLAY_MODE,
            )
            # Adapter: DrissionPage-compatible browser/page API over Camoufox
            _pw_context = sess  # type: ignore[assignment]
            adapter = sess.extra.get("browser_adapter")
            browser = adapter
            page = adapter.latest_tab if adapter else sess.page
            _chrome_temp_dir = ""
            _chrome_debug_port = 0
            ab = sess.extra.get("asset_block")
            slog(
                "BROWSER",
                f"camoufox ready  display={DISPLAY_MODE}  "
                f"headless={sess.extra.get('headless')!r}  "
                f"os={sess.extra.get('camoufox_os') or '?'}  "
                f"asset_block={'on' if ab else 'off'}",
            )
            return
        except Exception as e:
            # Surface proxy-class launch errors so account loop can retry/direct
            if is_proxy_failure(e):
                slog(
                    "BROWSER",
                    f"camoufox proxy fail ({e}) — raise for retry/direct",
                    level="warn",
                )
                raise
            slog(
                "BROWSER",
                f"camoufox failed ({e}) — fallback chromium",
                level="warn",
            )
            engine = "chromium"
            os.environ["GROK_BROWSER_ENGINE"] = "chromium"

    _chrome_temp_dir = tempfile.mkdtemp(prefix=f"grok_pw_w{wid}_")
    _chrome_debug_port = _pick_free_port(_worker_port_base())
    browser_path = _resolve_browser_path()
    binary_label = browser_path or "(DrissionPage default — install Playwright Chromium!)"
    if browser_path and "Google Chrome.app" in browser_path and "Testing" not in browser_path:
        print("[Warn] Using Google Chrome.app — may affect your daily browser. Prefer Playwright Chromium.")
    print(
        f"[*] Browser start worker={wid} port={_chrome_debug_port} "
        f"engine={engine} display={DISPLAY_MODE} proxy_driver={_proxy_driver()}\n"
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

    # ── Authenticated proxy → Playwright native (like grok-farm) ──
    if _browser_proxy and _proxy_driver() == "playwright":
        if start_browser_playwright_proxy(_chrome_temp_dir, _chrome_debug_port):
            page = page or browser.latest_tab
            _apply_window_policy(quiet=False)
            return
        slog(
            "PROXY",
            "playwright path failed — falling back to DrissionPage "
            f"({'bridge' if _proxy_driver() == 'bridge' else 'direct launch'})",
            level="warn",
        )

    # ── No proxy / bridge fallback: DrissionPage launches Chromium ──
    co = build_chromium_options(_chrome_temp_dir, _chrome_debug_port)
    browser = Chromium(co)
    tabs = browser.get_tabs()
    page = tabs[-1] if tabs else browser.new_tab()
    # Minimize immediately so the single flash (if any) is only on first launch.
    _apply_window_policy(quiet=False)
    return browser, page


def stop_browser():
    # Full quit + cleanup temp profile for this worker only.
    global browser, page, _chrome_temp_dir, _chrome_debug_port, co, _window_policy_logged
    global _pw, _pw_context
    # Camoufox BrowserSession stored in _pw_context
    if _pw_context is not None and hasattr(_pw_context, "close") and hasattr(_pw_context, "engine"):
        try:
            _pw_context.close()
        except Exception:
            pass
        _pw_context = None
        browser = None
        page = None
        co = None
    else:
        if browser is not None:
            try:
                browser.quit()
            except Exception:
                pass
        browser = None
        page = None
        co = None
        if _pw_context is not None:
            try:
                _pw_context.close()
            except Exception:
                pass
            _pw_context = None
        if _pw is not None:
            try:
                _pw.stop()
            except Exception:
                pass
            _pw = None
    if _chrome_temp_dir and os.path.isdir(_chrome_temp_dir):
        shutil.rmtree(_chrome_temp_dir, ignore_errors=True)
    _chrome_temp_dir = ""
    _chrome_debug_port = 0
    _window_policy_logged = False
    try:
        from proxy_bridge import stop_local_bridge

        stop_local_bridge()
    except Exception:
        pass


def _is_pw_adapter_browser() -> bool:
    """Camoufox / Playwright adapter (not DrissionPage Chromium)."""
    return browser is not None and (
        hasattr(browser, "_async")
        or type(browser).__name__ == "PwBrowserAdapter"
        or (
            _pw_context is not None
            and hasattr(_pw_context, "engine")
            and getattr(_pw_context, "engine", "") in ("camoufox", "chromium")
        )
    )


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
        if _is_pw_adapter_browser():
            # Playwright/Camoufox: clear cookies + blank tab (no Drission get_tabs)
            try:
                page.clear_cache(session_storage=True, cookies=True)
            except Exception:
                pass
            try:
                page.run_js(
                    "try{localStorage.clear();sessionStorage.clear();}catch(e){}"
                )
            except Exception:
                pass
            try:
                page.get("about:blank")
            except Exception:
                page = browser.new_tab("about:blank")
            print("[*] Soft reset camoufox/playwright (same process)")
            return

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
    Camoufox: soft reset preferred; full only when forced / dead.
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
        return page
    try:
        if _is_pw_adapter_browser():
            # Keep current page handle; Camoufox navigations stay on same page
            if page is None:
                page = browser.new_tab("about:blank")
            return page
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
    # Camoufox: flash-native open (get_by_role Sign up with email)
    if _is_pw_adapter_browser():
        try:
            from camoufox_signup import sync_open_signup

            sync_open_signup(page, log=lambda m: slog("EMAIL", m))
            _apply_window_policy(quiet=True)
            return
        except Exception as e:
            slog("EMAIL", f"camoufox open signup warn: {e} — fallback", level="warn")
    try:
        page.get(SIGNUP_URL)
    except Exception as e:
        slog("BROWSER", f"goto signup failed ({e}); new tab", level="warn")
        refresh_active_page()
        try:
            page = browser.new_tab(SIGNUP_URL)
        except Exception as e2:
            raise RuntimeError(f"open signup failed: {e2}") from e2
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
    # Create humanized catch-all email; keep token for OTP step.
    slog("EMAIL", "generating humanized catch-all alias…")
    _pre_given, _pre_family = "", ""
    try:
        from human_email import load_name_pairs, pick_names

        pairs = load_name_pairs()
        if pairs:
            _pre_given, _pre_family = pick_names(pairs)
    except Exception:
        pass
    email, dev_token = get_email_and_token(given=_pre_given, family=_pre_family)
    if not email or not dev_token:
        raise Exception("Failed to create email")
    slog("EMAIL", f"alias={email}")

    # Camoufox: flash-native fill + Sign up click
    if _is_pw_adapter_browser():
        try:
            from camoufox_signup import sync_fill_email

            sync_fill_email(page, email, log=lambda m: slog("EMAIL", m))
            slog("EMAIL", f"submitted sign-up form for {email}")
            return email, dev_token
        except Exception as e:
            slog("EMAIL", f"camoufox fill email failed: {e}", level="error")
            raise

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

    # Camoufox: keyboard/slot fill (flash) — avoid React-breaking JS
    if _is_pw_adapter_browser():
        try:
            from camoufox_signup import sync_fill_otp

            ok = sync_fill_otp(page, code, log=lambda m: slog("OTP", m))
            if ok:
                slog("OTP", "confirmed (camoufox native)")
                return
            # if already on profile form
            if has_profile_form():
                slog("OTP", "already on final signup; skip OTP confirm")
                return
        except Exception as e:
            slog("OTP", f"camoufox OTP warn: {e} — fallback JS", level="warn")

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

    Camoufox: flash-native path (get_by_role Complete, wait /account).
    Chromium/Drission: original mouse/JS strategies.
    """
    given_name, family_name, password = build_profile()
    slog("PROFILE", f"fill form  name={given_name} {family_name}")

    # ── Camoufox / Playwright native (flash-grok-farm) ──
    if _is_pw_adapter_browser():
        try:
            from camoufox_signup import sync_profile_complete

            ok = sync_profile_complete(
                page,
                given_name,
                family_name,
                password,
                log=lambda m: slog("PROFILE", m),
                timeout=float(timeout),
            )
            if ok:
                slog("SUBMIT", f"camoufox complete OK url={getattr(page, 'url', '')[:100]}")
                _wait_post_register_settle(timeout=20)
                return {
                    "given_name": given_name,
                    "family_name": family_name,
                    "password": password,
                }
            raise RuntimeError("camoufox complete_signup did not reach /account")
        except Exception as e:
            slog("PROFILE", f"camoufox profile/complete failed: {e}", level="error")
            raise

    deadline = time.time() + timeout
    fields_filled = False
    turnstile_ready = False
    last_click_status = ""
    click_attempts = 0
    last_heartbeat = 0.0
    cookie_dismissed = False
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
            # Flash-style: detect bounce back to signup chooser (not real success)
            try:
                cur = str(getattr(page, "url", "") or "")
            except Exception:
                cur = ""
            bounced = False
            try:
                bounce = page.run_js(
                    r"""
const t = ((document.body && document.body.innerText) || '').toLowerCase();
return t.includes('sign up with email') || t.includes('create your account')
  || (t.includes('sign up') && t.includes('log in') && !t.includes('complete sign'));
                    """
                )
                bounced = bool(bounce) and ("sign-up" in cur or "signup" in cur)
            except Exception:
                bounced = "sign-up" in cur and "account" not in cur
            if bounced:
                slog(
                    "SUBMIT",
                    f"bounce to signup chooser after Complete (url={cur[:100]}) — retry",
                    level="warn",
                )
                time.sleep(1.0)
                fields_filled = False
                turnstile_ready = False
                continue
            # Prefer navigate to /account to settle session (flash waits for /account URL)
            try:
                if "sign-up" in cur or not cur:
                    slog("SUBMIT", "navigate accounts.x.ai/account to settle…")
                    page.get("https://accounts.x.ai/account")
                    time.sleep(1.5)
            except Exception as e:
                slog("SUBMIT", f"account nav warn: {e}", level="warn")
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
    """Yield (name, value, domain) from all domains (DrissionPage or Playwright/Camoufox)."""
    refresh_active_page()
    if page is None:
        return

    cookies = []
    # 1) DrissionPage / adapter with cookies()
    try:
        if hasattr(page, "cookies"):
            cookies = page.cookies(all_domains=True, all_info=True) or []
    except TypeError:
        try:
            cookies = page.cookies() or []
        except Exception:
            cookies = []
    except Exception:
        cookies = []

    # 2) Playwright raw page / context
    if not cookies:
        try:
            raw = getattr(page, "raw", None) or page
            ctx = getattr(raw, "context", None)
            if ctx is not None:
                if _is_pw_adapter_browser() and hasattr(page, "_async") and page._async:
                    cookies = page._run(ctx.cookies())  # type: ignore[attr-defined]
                else:
                    cookies = ctx.cookies() or []
        except Exception:
            cookies = []

    # 3) Browser-level context (Camoufox session)
    if not cookies and browser is not None:
        try:
            # PwBrowserAdapter → underlying browser.contexts[0]
            if hasattr(browser, "_browser"):
                b = browser._browser
                if getattr(b, "contexts", None):
                    ctx = b.contexts[0]
                    if _is_pw_adapter_browser() and hasattr(browser, "_async") and browser._async:
                        cookies = browser._run(ctx.cookies())  # type: ignore[attr-defined]
                    else:
                        cookies = ctx.cookies() or []
        except Exception:
            cookies = []

    for item in cookies or []:
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


def wait_for_sso_cookie(timeout=90, no_sso_deadline=22):
    """
    Collect SSO after register — JANGAN buru-buru buka grok.com.

    Phases:
      1) wait_sso  — poll cookie di session sekarang (accounts.x.ai).
                     Tunggu sso muncul / redirect natural dulu.
                     Fail-fast kalau stuck di /account tanpa sso
                     (session mati: cuma CF cookies) biar gak nge-hang 90s.
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
    nudged_account = False
    reloaded_account = False
    poll_ticks = 0
    last_poll_log = 0.0
    stuck_on_account_since = None
    wait_started = time.time()

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
                # reset stuck timer on URL change
                if "/account" not in cur or "sign-up" in cur:
                    stuck_on_account_since = None

            try:
                wanted, names_seen = _collect_grok_session_cookies()
            except Exception as e:
                slog("SSO", f"cookie read warn: {e}", level="warn")
                wanted, names_seen = {}, set()
            last_seen_names |= names_seen
            has_sso = bool(wanted.get("sso"))
            now = time.time()

            # ── Phase 1: tunggu SSO tanpa navigasi paksa ──
            if phase == "wait_sso":
                if has_sso:
                    slog("SSO", "cookie muncul — settle session…")
                    phase = "settle"
                    sso_ready_at = now
                    stuck_on_account_since = None
                else:
                    # Kalau natural redirect ke grok.com — pindah settle, tapi
                    # jangan anggap sso ada; settle phase akan fail-fast kalau kosong.
                    if "grok.com" in cur:
                        if now - last_poll_log >= 8:
                            slog("SSO", "on grok.com tanpa sso cookie — wait briefly")
                            last_poll_log = now
                        # short grace on grok without sso then fail
                        if now - wait_started >= no_sso_deadline:
                            raise Exception(
                                "No sso cookie after signup (on grok.com without auth); "
                                f"cookies seen: {sorted(last_seen_names)}"
                            )
                    elif "/account" in cur and "sign-up" not in cur:
                        if stuck_on_account_since is None:
                            stuck_on_account_since = now
                        stuck_for = now - stuck_on_account_since
                        # rate-limit log (every 10s) — dulu spam 1×/detik keliatan hang
                        if now - last_poll_log >= 10:
                            slog(
                                "SSO",
                                f"on /account no sso yet  t={stuck_for:.0f}s  "
                                f"cookies={sorted(names_seen)[:8]}",
                            )
                            last_poll_log = now
                        # one soft reload after ~8s (sometimes cookie late-set)
                        if not reloaded_account and stuck_for >= 8:
                            reloaded_account = True
                            try:
                                slog("SSO", "reload /account once (late sso?)")
                                page.get("https://accounts.x.ai/account")
                                time.sleep(2)
                            except Exception as e:
                                slog("SSO", f"account reload warn: {e}", level="warn")
                        # fail-fast: session dead (CF only, no auth cookie)
                        if stuck_for >= no_sso_deadline:
                            raise Exception(
                                "No sso cookie after signup (stuck on /account); "
                                f"cookies seen: {sorted(last_seen_names)}"
                            )
                    elif "sign-up" in cur:
                        # Still on signup after Complete — nudge to /account once
                        poll_ticks += 1
                        if not nudged_account and poll_ticks >= 5:
                            nudged_account = True
                            try:
                                slog("SSO", "still on sign-up — open /account")
                                page.get("https://accounts.x.ai/account")
                                time.sleep(2)
                            except Exception as e:
                                slog("SSO", f"account nudge warn: {e}", level="warn")
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

                # entered settle without sso (e.g. natural grok redirect) — don't hang
                if not has_sso and sso_ready_at and (time.time() - sso_ready_at) >= no_sso_deadline:
                    raise Exception(
                        f"No sso cookie after signup; cookies seen: {sorted(last_seen_names)}"
                    )

        except PageDisconnectedError:
            refresh_active_page()
        except Exception as e:
            # re-raise our intentional fail-fast
            msg = str(e)
            if msg.startswith("No sso cookie after signup"):
                raise
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
                print(f"[*] 9router imported -> {provider} name={name} id={conn_id}")
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


def _resolve_register_mode() -> str:
    """browser (default) | hybrid — env GROK_REGISTER_MODE or config register_mode."""
    try:
        from hybrid.register import resolve_register_mode

        return resolve_register_mode(_load_config())
    except Exception:
        env = (os.environ.get("GROK_REGISTER_MODE") or "").strip().lower()
        if env in ("hybrid", "browser"):
            return env
        conf = _load_config()
        mode = str(conf.get("register_mode") or "").strip().lower()
        if not mode:
            run = conf.get("run") if isinstance(conf.get("run"), dict) else {}
            mode = str(run.get("register_mode") or "").strip().lower()
        return mode if mode in ("hybrid", "browser") else "browser"


def _try_hybrid_registration() -> dict | None:
    """
    Hybrid path: short browser harvest (castle/cookies/next-action) + protocol HTTP.
    Returns result dict on success, None on failure (caller falls back to browser).
    """
    from hybrid.register import register_one_hybrid

    global page, browser
    # Ensure browser is up (main loop usually already started it)
    if page is None or browser is None:
        try:
            start_browser()
        except Exception as e:
            slog("HYBRID", f"browser start failed: {e}", level="warn")
            return None

    return register_one_hybrid(
        page=page,
        browser=browser,
        log=lambda m: slog("HYBRID", m),
        proxy=current_proxy_url(),
        get_email=get_email_and_token,
        get_otp=lambda tok, em, **kw: get_oai_code(tok, em, timeout=120),
        build_profile=build_profile,
        open_signup_fn=open_signup_page,
        get_turnstile_fn=lambda: getTurnstileToken(),
    )


def run_single_registration(output_path=DEFAULT_SSO_FILE, extract_numbers=False):
    # One round: open signup -> register -> capture SSO -> write file -> push 9router.
    # Optional hybrid: protocol HTTP after short browser harvest; fall back to full browser.
    reg_mode = _resolve_register_mode()
    hybrid_result = None
    if reg_mode == "hybrid":
        slog("FLOW", "① hybrid register (browser harvest + protocol HTTP)")
        try:
            hybrid_result = _try_hybrid_registration()
        except Exception as e:
            slog("HYBRID", f"exception — fall back to browser: {e}", level="warn")
            hybrid_result = None
        if not hybrid_result or not (
            hybrid_result.get("sso_token") or hybrid_result.get("sso") or hybrid_result.get("apiKey")
        ):
            slog(
                "HYBRID",
                "failed or no SSO — fall back to full browser register",
                level="warn",
            )
            hybrid_result = None
            # Mid-signup browser state may be dirty; soft-reset before full UI path
            try:
                soft_reset_browser()
            except Exception:
                try:
                    restart_browser(force_full=False)
                except Exception:
                    pass
        else:
            slog("HYBRID", f"OK email={hybrid_result.get('email','')}")

    if hybrid_result:
        email = str(hybrid_result.get("email") or "")
        profile = {
            "given_name": hybrid_result.get("given_name") or "",
            "family_name": hybrid_result.get("family_name") or "",
            "password": hybrid_result.get("password") or "",
        }
        sso_cred = {
            "apiKey": hybrid_result.get("apiKey") or "",
            "sso_token": hybrid_result.get("sso_token") or hybrid_result.get("sso") or "",
            "sso_rw": hybrid_result.get("sso_rw") or "",
            "cookie_header": hybrid_result.get("cookie_header")
            or hybrid_result.get("apiKey")
            or "",
            "cloudflare_cookies": hybrid_result.get("cloudflare_cookies") or "",
            "providerSpecificData": hybrid_result.get("providerSpecificData") or {},
        }
        # Prefer live browser cookies if materialize already set them
        try:
            live = wait_for_sso_cookie(timeout=25, no_sso_deadline=12)
            if isinstance(live, dict) and (live.get("sso_token") or live.get("apiKey")):
                sso_cred = live
                slog("HYBRID", "using browser-materialized SSO cookies")
        except Exception as e:
            slog("HYBRID", f"SSO wait after hybrid (using protocol sso): {e}", level="warn")
        append_sso_to_txt(sso_cred, output_path)
        result = {
            "email": email,
            "sso": sso_cred.get("sso_token") or sso_cred.get("apiKey") or hybrid_result.get("sso") or "",
            "apiKey": sso_cred.get("apiKey") or hybrid_result.get("apiKey") or "",
            "sso_token": sso_cred.get("sso_token") or hybrid_result.get("sso_token") or "",
            "sso_rw": sso_cred.get("sso_rw") or hybrid_result.get("sso_rw") or "",
            "cookie_header": sso_cred.get("cookie_header")
            or sso_cred.get("apiKey")
            or "",
            "cloudflare_cookies": sso_cred.get("cloudflare_cookies") or "",
            "providerSpecificData": sso_cred.get("providerSpecificData") or {},
            "hybrid": True,
            **profile,
        }
    else:
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
        f"pass={profile.get('password','')}"
        + ("  mode=hybrid" if result.get("hybrid") else ""),
    )

    # Primary: settle → PKCE/device → chat usable → push 9router
    # ANY failure here must raise so main() counts FAIL (not OK).
    # When GROK_CHAT_PROBE_OFF_CRITICAL (default on): OAuth on browser path,
    # soft-reset, then HTTP probe+push so browser is not held during probe.
    try:
        conf = _load_config()
        gcli = conf.get("grok_cli") if isinstance(conf.get("grok_cli"), dict) else {}
        off_crit = _chat_probe_off_critical(gcli)
        if off_crit:
            slog("FLOW", "⑥ SETTLE → ⑦ OAuth PKCE  (PROBE deferred off critical)")
            tokens = convert_grok_cli_tokens(result)
            if tokens is not None:
                slog("PROBE", "deferred (off critical path)")
                result["inject_pending_probe"] = True
                try:
                    soft_reset_browser()
                    slog("BROWSER", "soft-reset after OAuth (probe off critical path)")
                except Exception as e:
                    slog(
                        "BROWSER",
                        f"soft-reset after OAuth failed (non-fatal): {e}",
                        level="warn",
                    )
                slog("FLOW", "⑧ PROBE → ⑨ PUSH")
                probe_and_push_grok_cli(result, tokens)
                result["inject_pending_probe"] = False
        else:
            slog("FLOW", "⑥ SETTLE → ⑦ OAuth PKCE → ⑧ PROBE → ⑨ PUSH")
            convert_and_push_grok_cli(result)
        slog("PUSH", "9router import OK")
    except Exception as e:
        err = str(e)
        el = err.lower()
        if "denied" in el or "probe" in el or "usable" in el or "smoke" in el:
            phase = "PROBE"
        elif "jwt gate" in el or "bot_flag" in el:
            phase = "CONVERT"
        elif (
            "convert" in el
            or "sso" in el
            or "device" in el
            or "token" in el
            or "oauth" in el
            or "pkce" in el
            or "proxy" in el
            or "accounts.x.ai" in el
            or "auth.x.ai" in el
        ):
            phase = "CONVERT"
        else:
            phase = "PUSH"
        slog(phase, f"FAILED: {e}", level="error")
        slog(
            "FAIL",
            f"pipeline stop at {phase}  email={email}  "
            f"(will count as ✗ — not imported)",
            level="error",
        )
        raise RuntimeError(f"{phase}: {e}") from e

    # Optional side paths — non-fatal (don't flip PASS→FAIL)
    try:
        push_sso_to_grok2api([result])
    except Exception as e:
        slog("PUSH", f"grok2api failed (non-fatal): {e}", level="warn")

    try:
        push_sso_to_9router([result])
    except Exception as e:
        slog("PUSH", f"grok-web failed (non-fatal): {e}", level="warn")

    slog("DONE", f"SUCCESS account complete  email={email}  (imported to 9router)")
    return result


# ── grok-cli rate hygiene / probe scheduling ──────────────────────────
# Env (config grok_cli.* also accepted; env wins when set):
#   GROK_OAUTH_GAP_SEC            float, default 8 — min seconds between OAuth mints
#   GROK_CHAT_PROBE_OFF_CRITICAL  bool,  default true — probe+push after browser
#                                 soft-reset (HTTP only; browser not held on probe)
_last_oauth_ts: float = 0.0


def _oauth_gap_sec(gcli: dict | None = None) -> float:
    raw = (os.environ.get("GROK_OAUTH_GAP_SEC") or "").strip()
    if raw:
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            pass
    gcli = gcli or {}
    try:
        if gcli.get("oauth_gap_sec") is not None:
            return max(0.0, float(gcli.get("oauth_gap_sec")))
    except (TypeError, ValueError):
        pass
    return 8.0


def _chat_probe_off_critical(gcli: dict | None = None) -> bool:
    raw = (os.environ.get("GROK_CHAT_PROBE_OFF_CRITICAL") or "").strip()
    if raw:
        return _env_bool_local("GROK_CHAT_PROBE_OFF_CRITICAL", True)
    gcli = gcli or {}
    if gcli.get("chat_probe_off_critical") is not None:
        return bool(gcli.get("chat_probe_off_critical"))
    return True


def _wait_oauth_gap(gap_sec: float) -> None:
    """Sleep remaining gap since last successful OAuth (single-worker safe)."""
    global _last_oauth_ts
    if gap_sec <= 0:
        return
    if _last_oauth_ts <= 0:
        return
    elapsed = time.time() - _last_oauth_ts
    if elapsed < gap_sec:
        rem = gap_sec - elapsed
        slog("OAUTH", f"gap sleep {rem:.1f}s (oauth_gap_sec={gap_sec:g})")
        time.sleep(rem)


def _mark_oauth_done() -> None:
    global _last_oauth_ts
    _last_oauth_ts = time.time()


def _gcli_inject_policy(gcli: dict) -> str:
    inject_policy = str(gcli.get("inject_policy") or "usable").strip().lower()
    if inject_policy in ("jwt", "clean"):
        return "jwt_clean"
    return inject_policy


def convert_grok_cli_tokens(result: dict):
    """
    OAuth-only: settle → PKCE/device → JWT gate. Returns BuildTokens or None.

    Does NOT probe chat or push to 9router. Browser may still be needed for PKCE.

    Env / config (see module comment above convert_and_push_grok_cli):
      GROK_OAUTH_GAP_SEC / grok_cli.oauth_gap_sec
    """
    conf = _load_config()
    gcli = conf.get("grok_cli") if isinstance(conf.get("grok_cli"), dict) else {}
    if gcli.get("enabled") is False:
        print("[*] grok-cli convert disabled (grok_cli.enabled=false)")
        return None

    email = str(result.get("email") or "").strip()
    px = current_proxy_url()

    oauth_mode = str(gcli.get("oauth_mode") or "pkce").strip().lower()
    referrer = str(gcli.get("oauth_referrer") or "grok-build").strip() or "grok-build"
    try:
        settle_s = float(gcli.get("post_signup_settle_sec") or 12)
    except (TypeError, ValueError):
        settle_s = 12.0
    inject_policy = _gcli_inject_policy(gcli)
    reject_bot = bool(gcli.get("jwt_reject_bot_flag") or inject_policy == "jwt_clean")
    enforce_ref = gcli.get("jwt_enforce_referrer")
    if enforce_ref is None:
        enforce_ref = True

    gap_sec = _oauth_gap_sec(gcli)
    _wait_oauth_gap(gap_sec)

    # ── SETTLE (hygiene before OAuth — flash anti-bot) ──────────────
    if settle_s > 0:
        slog("SETTLE", f"post-signup idle {settle_s:.0f}s before OAuth (bot hygiene)")
        time.sleep(settle_s)
        slog("SETTLE", "done")

    # ── PHASE: CONVERT (PKCE browser preferred) ─────────────────────
    tokens = None
    if oauth_mode in ("pkce", "browser", "oidc", "grok-build"):
        slog(
            "CONVERT",
            f"browser PKCE referrer={referrer} email={email or '-'} "
            f"(flash path — same session as signup)",
        )
        try:
            from build_oauth_pkce import obtain_tokens_via_browser_pkce

            # Prefer active page (Chromium Drission / Playwright attach)
            oauth_page = page
            if oauth_page is None:
                raise RuntimeError("no browser page for PKCE")
            tokens = obtain_tokens_via_browser_pkce(
                oauth_page,
                email=email,
                referrer=referrer,
                timeout_sec=float(gcli.get("oauth_timeout_sec") or 90),
                proxy=px,
                log=lambda m: slog("CONVERT", m),
            )
        except Exception as e:
            slog(
                "CONVERT",
                f"PKCE browser failed ({e}) — fallback device SSO convert",
                level="warn",
            )
            tokens = None

    if tokens is None:
        slog("CONVERT", f"Web SSO → Device OAuth (fallback) email={email or '-'}")
        from sso_to_build import convert_sso_to_build

        # Full jar (sso + cf_clearance etc.) helps device verify under CF
        jar: dict = {}
        try:
            wanted, _ = _collect_grok_session_cookies()
            if wanted:
                jar.update(wanted)
        except Exception:
            pass
        if isinstance(result, dict):
            if result.get("sso_token") or result.get("sso"):
                jar.setdefault("sso", result.get("sso_token") or result.get("sso"))
            if result.get("sso_rw"):
                jar.setdefault("sso-rw", result.get("sso_rw"))
            cf = str(result.get("cloudflare_cookies") or "")
            for part in cf.split(";"):
                part = part.strip()
                if "=" in part:
                    k, _, v = part.partition("=")
                    jar.setdefault(k.strip(), v.strip())
        tokens = convert_sso_to_build(
            result,
            name_hint=email,
            proxy=px or current_proxy_url(),
            page=page,
            cookies=jar or None,
        )

    # Normalize token fields onto result (probe/push use tokens object)
    access = getattr(tokens, "access_token", "") or ""
    refresh = getattr(tokens, "refresh_token", "") or ""
    tok_email = getattr(tokens, "email", "") or email
    tok_uid = getattr(tokens, "user_id", "") or ""
    bot_flag = getattr(tokens, "bot_flag_source", None)
    tok_ref = getattr(tokens, "referrer", "") or ""
    slog(
        "CONVERT",
        f"tokens OK  email={tok_email or '-'}  user_id={tok_uid or '-'}  "
        f"bot_flag={bot_flag!r}  referrer={tok_ref!r}  "
        f"mode={getattr(tokens, 'auth_mode', '?')}",
    )

    # JWT gate (soft bot by default; hard referrer=grok-build)
    try:
        from build_oauth_pkce import jwt_gate_decision, BuildTokens as PkceTokens

        if not isinstance(tokens, PkceTokens):
            # wrap device tokens for gate
            gate_tokens = PkceTokens(
                access_token=access,
                refresh_token=refresh,
                email=tok_email,
                user_id=tok_uid,
                bot_flag_source=bot_flag,
                referrer=tok_ref,
            )
        else:
            gate_tokens = tokens
        # Only hard-enforce referrer when we actually minted via browser PKCE
        is_pkce = (
            getattr(tokens, "auth_mode", "") == "oidc_pkce"
            or bool(getattr(tokens, "referrer", "") or tok_ref)
        )
        gate = jwt_gate_decision(
            gate_tokens,
            reject_bot_flag=reject_bot,
            require_referrer=referrer,
            enforce_referrer=bool(enforce_ref) and is_pkce,
        )
        if not gate.get("ok"):
            slog("CONVERT", f"JWT gate FAIL: {gate.get('reason')}", level="error")
            raise RuntimeError(f"JWT gate: {gate.get('reason')}")
        slog(
            "CONVERT",
            f"JWT gate OK bot={gate.get('bot_flag_source')!r} ref={gate.get('referrer')!r}",
        )
    except ImportError:
        pass

    _mark_oauth_done()
    result["build_access_token"] = access
    result["build_refresh_token"] = refresh
    result["build_email"] = tok_email
    result["build_user_id"] = tok_uid
    result["bot_flag_source"] = bot_flag
    result["oauth_referrer"] = tok_ref
    result["_build_tokens"] = tokens
    return tokens


def probe_and_push_grok_cli(result: dict, tokens) -> None:
    """
    Chat usable probe + 9router push. HTTP-only — no browser required.

    inject_policy=usable (default): NEVER inject until probe USABLE.
    DENIED raises RuntimeError so main counts FAIL.
    """
    if tokens is None:
        return

    conf = _load_config()
    gcli = conf.get("grok_cli") if isinstance(conf.get("grok_cli"), dict) else {}
    if gcli.get("enabled") is False:
        return

    from push_9router_grok_cli import push_build_tokens_to_9router

    email = str(result.get("email") or "").strip()
    given = str(result.get("given_name") or "").strip()
    family = str(result.get("family_name") or "").strip()
    display = f"{given} {family}".strip()
    px = current_proxy_url()

    inject_policy = _gcli_inject_policy(gcli)
    probe_model = str(
        gcli.get("chat_probe_model") or gcli.get("smoke_model") or "grok-4.5"
    ).strip()

    access = getattr(tokens, "access_token", "") or result.get("build_access_token") or ""
    tok_email = getattr(tokens, "email", "") or result.get("build_email") or email
    tok_uid = getattr(tokens, "user_id", "") or result.get("build_user_id") or ""

    # ── PHASE: CHAT USABLE (truth for 403 — flash inject_policy=usable) ──
    usable_info = None
    if inject_policy == "usable":
        from chat_usable import probe_chat_usable

        slog("PROBE", f"starting  model={probe_model}  email={tok_email or '-'}")
        usable_info = probe_chat_usable(
            access,
            email=tok_email,
            model=probe_model,
            proxy=px,
            timeout=float(gcli.get("smoke_timeout_sec") or 45),
        )
        if not usable_info.get("usable") and px:
            slog("PROBE", "proxy path failed/denied — retry DIRECT", level="warn")
            usable_info = probe_chat_usable(
                access,
                email=tok_email,
                model=probe_model,
                proxy="",
                timeout=float(gcli.get("smoke_timeout_sec") or 45),
            )
        result["chat_probe"] = usable_info
        if usable_info.get("usable"):
            slog(
                "PROBE",
                f"USABLE  status={usable_info.get('status')}  "
                f"latency_ms={usable_info.get('latency_ms')}  "
                f"reply={str(usable_info.get('reply') or '')[:40]}",
            )
        else:
            slog(
                "PROBE",
                f"DENIED  status={usable_info.get('status')}  "
                f"err={usable_info.get('err')}  "
                f"(will NOT inject — policy=usable)",
                level="error",
            )
            raise RuntimeError(
                f"chat DENIED status={usable_info.get('status')} "
                f"email={tok_email} err={usable_info.get('err')}"
            )
    else:
        # legacy: skip hard usable gate (may still smoke inside push)
        slog("PROBE", f"inject_policy={inject_policy} — skip hard usable gate")

    # ── PHASE: PUSH (only after USABLE when policy=usable) ──────────
    slog("PUSH", f"import grok-cli → 9router  email={tok_email or '-'}")
    push_build_tokens_to_9router(
        tokens,
        base_url=str(gcli.get("base_url") or "http://127.0.0.1:20127"),
        data_dir=str(gcli.get("data_dir") or "~/.9router"),
        password=str(gcli.get("password") or gcli.get("dashboard_password") or ""),
        cli_token=str(gcli.get("cli_token") or ""),
        name=email or getattr(tokens, "name", "") or tok_email,
        email=tok_email,
        display_name=display or getattr(tokens, "name", "") or tok_email,
        # already probed usable — never skip import on JWT bot alone
        smoke_bot_flag=False,
        smoke_model=probe_model,
        smoke_timeout_sec=float(gcli.get("smoke_timeout_sec") or 45),
    )
    result["build_access_token"] = access
    result["build_refresh_token"] = (
        getattr(tokens, "refresh_token", "") or result.get("build_refresh_token") or ""
    )
    result["build_email"] = tok_email
    result["build_user_id"] = tok_uid
    result["bot_flag_source"] = getattr(tokens, "bot_flag_source", None)
    result["oauth_referrer"] = getattr(tokens, "referrer", "") or ""
    slog(
        "PUSH",
        f"grok-cli ready  email={tok_email}  user_id={tok_uid or '-'}",
    )


def convert_and_push_grok_cli(result: dict) -> None:
    """
    Build OAuth → chat usable probe → 9router grok-cli (combined path).

    Prefer split helpers when deferring probe off the browser critical path:
      convert_grok_cli_tokens(result) → tokens
      probe_and_push_grok_cli(result, tokens)

    Default (flash path): browser PKCE with referrer=grok-build on the same
    session as signup. Fallback: HTTP device convert from SSO cookie.

    Env:
      GROK_OAUTH_GAP_SEC            — seconds between OAuth mints (default 8)
      GROK_CHAT_PROBE_OFF_CRITICAL  — if true (default), callers soft-reset
                                      browser before probe (see run_single_registration)

    inject_policy:
      usable  — only push if chat probe USABLE (default; fights 403)
      all     — push if tokens ok (legacy)
      jwt_clean — hard reject bot_flag (optional)
    """
    tokens = convert_grok_cli_tokens(result)
    if tokens is None:
        return
    probe_and_push_grok_cli(result, tokens)


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
        choices=["headed", "offscreen", "headless", "virtual"],
        default=None,
        help=(
            "headed=window | offscreen=Mac park | headless=Linux/VPS flash | "
            "virtual=Camoufox+Xvfb"
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="shortcut for --display headless (flash Linux default)",
    )
    parser.add_argument(
        "--offscreen",
        action="store_true",
        help="shortcut for --display offscreen (recommended on Mac while working)",
    )
    parser.add_argument(
        "--virtual",
        action="store_true",
        help="shortcut for --display virtual (Camoufox headed on Xvfb)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="shortcut for --display headed (visible window; debug)",
    )
    parser.add_argument("--extract-numbers", action="store_true", help="Also extract visible numbers after signup")
    args = parser.parse_args()
    if getattr(args, "unlimited", False):
        args.count = 0

    if args.worker_id:
        WORKER_ID = str(args.worker_id).strip()
        os.environ["GROK_WORKER_ID"] = WORKER_ID

    # Priority: CLI shortcuts > --display > env GROK_DISPLAY/GROK_HEADLESS > config > platform
    if getattr(args, "headed", False):
        DISPLAY_MODE = "headed"
    elif getattr(args, "virtual", False):
        DISPLAY_MODE = "virtual"
    elif args.headless:
        DISPLAY_MODE = "headless"
    elif args.offscreen:
        DISPLAY_MODE = "offscreen"
    elif args.display:
        DISPLAY_MODE = args.display
    elif config_display:
        DISPLAY_MODE = config_display
    else:
        DISPLAY_MODE = ""  # let resolve_display use env + platform default
    try:
        from browser_engine import resolve_display, normalize_display

        forced = (
            getattr(args, "headed", False)
            or getattr(args, "virtual", False)
            or args.headless
            or args.offscreen
            or bool(args.display)
            or bool(config_display)
        )
        if forced and DISPLAY_MODE:
            DISPLAY_MODE = normalize_display(DISPLAY_MODE) or DISPLAY_MODE
            # still allow GROK_HEADLESS only when nothing forced — already forced path
        else:
            DISPLAY_MODE = resolve_display(DISPLAY_MODE)
    except Exception:
        if DISPLAY_MODE in ("bg", "background", "minimized", "minimise", "minimize"):
            DISPLAY_MODE = "offscreen"
        if DISPLAY_MODE in ("xvfb", "vd"):
            DISPLAY_MODE = "virtual"
        if DISPLAY_MODE not in ("headed", "offscreen", "headless", "virtual"):
            DISPLAY_MODE = "offscreen" if sys.platform == "darwin" else "headless"
    os.environ["GROK_DISPLAY"] = DISPLAY_MODE
    if DISPLAY_MODE == "headless":
        os.environ["GROK_HEADLESS"] = "true"
    elif DISPLAY_MODE == "virtual":
        os.environ["GROK_HEADLESS"] = "virtual"
    else:
        os.environ["GROK_HEADLESS"] = "false"

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
        f"offset={POOL_OFFSET}  display={DISPLAY_MODE}  "
        f"register_mode={_resolve_register_mode()}  "
        f"out={os.path.basename(args.output)}",
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
        # Flash-aligned proxy retries (env):
        #   GROK_PROXY_RETRIES=3          max distinct proxies per account
        #   GROK_PROXY_FALLBACK_DIRECT=1  after pool fails → direct (default on)
        max_proxy_tries = int(os.environ.get("GROK_PROXY_RETRIES", "3") or "3")
        allow_direct_fallback = _env_bool_local("GROK_PROXY_FALLBACK_DIRECT", True)
        slog(
            "BROWSER",
            f"proxy_mode={_proxy_mode}  pool={len(_proxy_pool)}  "
            f"retries={max_proxy_tries}  fallback_direct={allow_direct_fallback}  "
            f"(per_account → full restart when proxy changes)",
        )
        while True:
            if args.count > 0 and current_round >= args.count:
                break

            current_round += 1
            progress_begin_account(current_round)
            hard_fail = False

            # Proxy strategy (flash): try up to N distinct proxies, then direct
            tried_pids: set[str] = set()
            last_error: Exception | None = None
            account_ok = False
            fail_recorded = False
            total_attempts = max(1, max_proxy_tries) + (1 if allow_direct_fallback else 0)
            # If no pool, single direct attempt
            if not _proxy_pool:
                total_attempts = 1

            for try_i in range(total_attempts):
                proxy_url, proxy_key = pick_proxy_for_try(
                    current_round,
                    tried_pids,
                    try_i,
                    max_proxy_tries=max_proxy_tries if _proxy_pool else 0,
                    allow_direct=allow_direct_fallback or not _proxy_pool,
                )
                key = proxy_key or (proxy_url or "direct")
                if key in tried_pids and try_i > 0:
                    # nothing new left
                    if allow_direct_fallback and "direct" not in tried_pids:
                        proxy_url, key = None, "direct"
                    else:
                        break
                tried_pids.add(key)

                reason = ""
                if try_i and key == "direct":
                    reason = "proxy pool unusable → DIRECT"
                    slog("PROXY", f"#{current_round} {reason}", level="warn")
                elif try_i:
                    reason = f"proxy retry #{try_i + 1}"
                    slog("PROXY", f"#{current_round} {reason}: {key[:48]}", level="warn")

                try:
                    force_browser_proxy(
                        proxy_url or "",
                        account_index=current_round,
                        reason=reason or "start",
                    )
                    result = run_single_registration(
                        args.output, extract_numbers=args.extract_numbers
                    )
                    collected_sso.append(
                        result.get("sso_token")
                        or result.get("sso")
                        or result.get("apiKey")
                        or ""
                    )
                    progress_end_account(True, f"email={result.get('email','')}")
                    account_ok = True
                    last_error = None
                    break
                except KeyboardInterrupt:
                    slog("STOP", "interrupted by user", level="warn")
                    raise
                except Exception as error:
                    last_error = error
                    more = try_i < total_attempts - 1
                    if is_proxy_failure(error) and more:
                        slog(
                            "PROXY",
                            f"#{current_round} proxy fail, will retry/fallback: {error}",
                            level="warn",
                        )
                        try:
                            stop_browser()
                        except Exception:
                            pass
                        continue
                    # non-proxy fail or out of retries — count as account fail
                    progress_end_account(False, str(error))
                    fail_recorded = True
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
                            "proxy",
                            "err_proxy",
                            "tunnel",
                        )
                    )
                    break

            if not account_ok and last_error is not None and not fail_recorded:
                progress_end_account(False, str(last_error))
                hard_fail = True

            try:
                if args.count == 0 or current_round < args.count:
                    next_idx = current_round + 1
                    next_proxy = select_proxy_for_account(next_idx)
                    same_proxy = next_proxy == current_proxy_url()
                    if hard_fail:
                        slog("BROWSER", "FULL restart (browser unhealthy)")
                        restart_browser(force_full=True)
                    elif same_proxy and _proxy_mode == "per_worker":
                        slog("BROWSER", "soft-reset → next account (same proxy)")
                        restart_browser(force_full=False)
                    elif same_proxy and len(_proxy_pool) <= 1:
                        slog("BROWSER", "soft-reset → next account (single/same proxy)")
                        restart_browser(force_full=False)
                    else:
                        # Next force_browser_proxy will full-restart if proxy changes
                        if same_proxy:
                            slog("BROWSER", "soft-reset → next account (proxy reuse)")
                            restart_browser(force_full=False)
                        else:
                            slog(
                                "BROWSER",
                                "next account uses different proxy → defer restart",
                            )
                    _apply_window_policy(quiet=True)
            except KeyboardInterrupt:
                slog("STOP", "interrupted by user", level="warn")
                break

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

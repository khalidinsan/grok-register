"""Google OIDC signup / sign-in for xAI via Camoufox (Playwright).

Inventory: ``accounts/google_pass.txt`` (email:password), one Google account
per farm round. Config::

    "register_mode": "google",
    "google": {
      "accounts_file": "accounts/google_pass.txt"
    }

Flow (verified live):
  accounts.x.ai/sign-up
    → Continue with Google
    → accounts.google.com email + password
    → consent (Lanjutkan / Continue)
    → accounts.x.ai/exchange-token
    → /account  (Google sign-in method ON)
    → SSO cookies (sso / sso-rw)

Then farm reuses the normal activate → OAuth → probe → 9router pipeline.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent
DEFAULT_ACCOUNTS_FILE = ROOT / "accounts" / "google_pass.txt"
DEFAULT_JSONL = ROOT / "accounts" / "accounts.jsonl"
SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
SIGNIN_URL = "https://accounts.x.ai/sign-in"
ACCOUNT_URL = "https://accounts.x.ai/account"
EXCHANGE_HINT = "exchange-token"

# Final ledger statuses in accounts.jsonl — skip on next claim.
# (jsonl is written AFTER probe for google mode; in-flight claims use CLAIM_LIVE.)
SKIP_STATUSES = frozenset(
    {
        "failed_login",
        "failed_oauth",
        "failed_probe",
        "failed_push",
        "usable",
        "injected",
        "oauth_ok",
        "created",
        "pending",
        "in_progress",
    }
)

# Multi-worker in-flight claims (NOT accounts.jsonl — that is post-probe only)
CLAIM_LIVE = ROOT / "accounts" / ".google_claim_live.jsonl"


class GoogleInventoryEmpty(Exception):
    """No free emails left in google_pass inventory — worker should stop cleanly."""

    pass

LogFn = Callable[[str], None]


def _log(log: Optional[LogFn], msg: str) -> None:
    if log:
        log(msg)


def _loop_of(page: Any):
    return getattr(page, "_loop", None)


def _is_async(page: Any) -> bool:
    return bool(getattr(page, "_async", False))


def _raw(page: Any):
    return getattr(page, "raw", None) or page


def _run(page: Any, coro):
    loop = _loop_of(page)
    if _is_async(page) and loop is not None:
        return loop.run_until_complete(coro)
    if asyncio.iscoroutine(coro):
        return asyncio.get_event_loop().run_until_complete(coro)
    return coro


# ── config / inventory ─────────────────────────────────────────────────────


def load_config() -> dict:
    p = ROOT / "config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_google_accounts_file(config: Optional[dict] = None) -> Path:
    """Priority: env GROK_GOOGLE_ACCOUNTS → config google.accounts_file → default."""
    env = (os.environ.get("GROK_GOOGLE_ACCOUNTS") or "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_absolute() else ROOT / p
    conf = config if config is not None else load_config()
    g = conf.get("google") if isinstance(conf.get("google"), dict) else {}
    raw = str(g.get("accounts_file") or "").strip()
    if not raw:
        raw = "accounts/google_pass.txt"
    p = Path(raw).expanduser()
    return p if p.is_absolute() else ROOT / p


def _read_pass_file(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        em, _, pw = line.partition(":")
        em, pw = em.strip(), pw.strip()
        if not em or not pw or "@" not in em:
            continue
        key = em.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((em, pw))
    return out


def _jsonl_status_by_email(jsonl: Path = DEFAULT_JSONL) -> dict[str, str]:
    by: dict[str, str] = {}
    if not jsonl.is_file():
        return by
    try:
        lines = jsonl.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return by
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        em = str(rec.get("email") or "").strip().lower()
        if not em:
            continue
        by[em] = str(rec.get("status") or "").strip()
    return by


def _with_claim_lock(fn):
    """Cross-process lock for claiming Google inventory rows."""
    lock_path = ROOT / "accounts" / ".google_claim.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = None
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
        try:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except Exception:
            try:
                import msvcrt

                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except Exception:
                pass
        return fn()
    finally:
        if fh is not None:
            try:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _live_claimed_emails() -> set[str]:
    """Emails currently claimed by any worker (in-flight, not yet in jsonl final)."""
    out: set[str] = set()
    if not CLAIM_LIVE.is_file():
        return out
    try:
        for line in CLAIM_LIVE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                # plain email line
                if "@" in line:
                    out.add(line.lower())
                continue
            em = str(rec.get("email") or "").strip().lower()
            st = str(rec.get("status") or "in_progress").strip()
            if em and st in ("in_progress", "pending", "claimed"):
                out.add(em)
    except Exception:
        pass
    return out


def _mark_claim_live(email: str, password: str) -> None:
    """Mark email in-flight for multi-worker (separate from accounts.jsonl)."""
    CLAIM_LIVE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "email": email,
        "password": password,
        "status": "in_progress",
        "provider": "google",
        "pid": os.getpid(),
    }
    with open(CLAIM_LIVE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def release_google_claim(email: str) -> None:
    """Drop in-flight claim so email can be retried (or after final jsonl write)."""
    email = (email or "").strip().lower()
    if not email or not CLAIM_LIVE.is_file():
        return

    def _rel():
        try:
            lines = CLAIM_LIVE.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            return
        kept = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                em = str(rec.get("email") or "").strip().lower()
                if em == email:
                    continue  # drop
                kept.append(json.dumps(rec, ensure_ascii=False))
            except Exception:
                if line.lower() != email:
                    kept.append(line)
        CLAIM_LIVE.write_text(
            ("\n".join(kept) + ("\n" if kept else "")), encoding="utf-8"
        )

    try:
        _with_claim_lock(_rel)
    except Exception:
        pass


def claim_next_google_account(
    accounts_file: Optional[Path] = None,
    *,
    force: bool = False,
    log: Optional[LogFn] = None,
) -> Optional[tuple[str, str]]:
    """Claim next unused Google email:password (multi-worker safe).

    Skips:
      - emails already in accounts.jsonl with terminal status (post-probe ledger)
      - emails currently in-flight (.google_claim_live.jsonl)

    Does NOT write accounts.jsonl (that happens after probe).
    """
    path = accounts_file or resolve_google_accounts_file()
    if (os.environ.get("GROK_GOOGLE_FORCE") or "").strip() in ("1", "true", "yes"):
        force = True

    def _claim() -> Optional[tuple[str, str]]:
        pairs = _read_pass_file(path)
        if not pairs:
            _log(log, f"google inventory empty: {path}")
            return None
        statuses = _jsonl_status_by_email()
        live = _live_claimed_emails()
        for em, pw in pairs:
            key = em.lower()
            st = statuses.get(key, "")
            if not force and st in SKIP_STATUSES:
                continue
            if not force and key in live:
                continue
            _mark_claim_live(em, pw)
            _log(log, f"claimed google account {em}  (was status={st or 'new'})")
            return em, pw
        _log(log, f"no free google accounts left in {path.name}  (n={len(pairs)})")
        return None

    return _with_claim_lock(_claim)


def inventory_stats(accounts_file: Optional[Path] = None) -> dict:
    path = accounts_file or resolve_google_accounts_file()
    pairs = _read_pass_file(path)
    statuses = _jsonl_status_by_email()
    free = 0
    for em, _ in pairs:
        if statuses.get(em.lower(), "") not in SKIP_STATUSES:
            free += 1
    return {
        "file": str(path),
        "total": len(pairs),
        "free": free,
        "skipped": len(pairs) - free,
    }


# ── browser helpers ────────────────────────────────────────────────────────


async def _dismiss_cookie(p) -> None:
    for name in (
        re.compile(r"accept\s*all", re.I),
        re.compile(r"allow\s*all", re.I),
        re.compile(r"reject\s*all", re.I),
    ):
        try:
            btn = p.get_by_role("button", name=name).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=2000)
                await asyncio.sleep(0.3)
                return
        except Exception:
            continue


async def _click_text_button(p, patterns: list[re.Pattern], force: bool = False) -> str:
    for pat in patterns:
        try:
            loc = p.get_by_role("button", name=pat).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=4000, force=force)
                return f"role:{pat.pattern}"
        except Exception:
            pass
        try:
            loc = p.get_by_role("link", name=pat).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=4000, force=force)
                return f"link:{pat.pattern}"
        except Exception:
            pass
    # JS fallback: any clickable with matching text
    for pat in patterns:
        try:
            hit = await p.evaluate(
                """(reSrc) => {
                    const re = new RegExp(reSrc, 'i');
                    const nodes = [...document.querySelectorAll(
                        'button, a, [role="button"], div[role="link"]'
                    )];
                    for (const b of nodes) {
                        const t = (b.innerText || b.textContent || '')
                            .replace(/\\s+/g, ' ').trim();
                        if (!t || t.length > 80) continue;
                        if (re.test(t)) {
                            const r = b.getBoundingClientRect();
                            if (r.width > 0 && r.height > 0) {
                                b.click();
                                return t;
                            }
                        }
                    }
                    return '';
                }""",
                pat.pattern,
            )
            if hit:
                return f"js:{hit}"
        except Exception:
            continue
    return ""


async def open_xai_signup(p, log=None) -> None:
    try:
        await p.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        await p.goto(SIGNUP_URL, wait_until="commit", timeout=45000)
    await asyncio.sleep(1.0)
    await _dismiss_cookie(p)


async def click_continue_with_google(p, log=None) -> bool:
    """Click Continue / Sign up with Google on accounts.x.ai."""
    await _dismiss_cookie(p)
    pats = [
        re.compile(r"continue\s+with\s+google", re.I),
        re.compile(r"sign\s*up\s+with\s+google", re.I),
        re.compile(r"sign\s*in\s+with\s+google", re.I),
        re.compile(r"^google$", re.I),
    ]
    for round_i in range(1, 8):
        url = (p.url or "").lower()
        if "accounts.google.com" in url or "google.com/o/oauth" in url:
            _log(log, "already on Google OAuth")
            return True
        clicked = await _click_text_button(p, pats, force=(round_i >= 3))
        if clicked:
            _log(log, f"google click round {round_i}: {clicked}")
        # also try aria / data attributes
        if not clicked:
            try:
                loc = p.locator(
                    'button:has-text("Google"), a:has-text("Google"), '
                    '[data-provider="google"], [aria-label*="Google" i]'
                ).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=4000, force=(round_i >= 3))
                    clicked = "locator:Google"
                    _log(log, f"google click round {round_i}: {clicked}")
            except Exception:
                pass

        for _ in range(20):
            url = (p.url or "").lower()
            if "accounts.google.com" in url or "google.com/o/oauth" in url:
                return True
            # popup? check pages
            await asyncio.sleep(0.25)
        if round_i in (3, 5):
            try:
                await p.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=20000)
                await asyncio.sleep(0.8)
                await _dismiss_cookie(p)
            except Exception:
                pass
    return "accounts.google.com" in ((p.url or "").lower())


async def _fill_visible(p, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            el = p.locator(sel).first
            if await el.count() == 0:
                continue
            if not await el.is_visible():
                continue
            await el.click(timeout=3000)
            await el.fill("")
            await el.fill(value)
            return True
        except Exception:
            continue
    return False


async def _type_human(p, selectors: list[str], value: str, *, delay_ms: int = 20) -> bool:
    """Click + type with key delay (Gsuiteto9router bot.js style — more reliable on Google)."""
    for sel in selectors:
        try:
            el = p.locator(sel).first
            if await el.count() == 0:
                continue
            if not await el.is_visible():
                continue
            await el.click(timeout=4000)
            await asyncio.sleep(0.2)
            # clear existing
            try:
                await el.fill("")
            except Exception:
                try:
                    await p.keyboard.press("Meta+a")
                    await p.keyboard.press("Backspace")
                except Exception:
                    pass
            # prefer press_sequentially (Playwright) else type
            try:
                await el.press_sequentially(value, delay=delay_ms)
            except Exception:
                try:
                    await el.type(value, delay=delay_ms)
                except Exception:
                    await el.fill(value)
            return True
        except Exception:
            continue
    return False


async def _password_field_visible(p) -> bool:
    for sel in (
        'input[type="password"][name="Passwd"]',
        'input[name="Passwd"]',
        '#password input[type="password"]',
        'input[type="password"]',
    ):
        try:
            el = p.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _identifier_field_visible(p) -> bool:
    for sel in ("#identifierId", 'input[type="email"]', 'input[name="identifier"]'):
        try:
            el = p.locator(sel).first
            if await el.count() > 0 and await el.is_visible():
                return True
        except Exception:
            continue
    return False


async def _click_sel_if_visible(p, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = p.locator(sel).first
            if await el.count() == 0:
                continue
            if not await el.is_visible():
                continue
            await el.click(timeout=3000)
            return sel
        except Exception:
            continue
    return ""


async def _click_identifier_next(p) -> str:
    """Email step only — NEVER #passwordNext (DOM may already have it before pwd filled)."""
    hit = await _click_sel_if_visible(p, ["#identifierNext"])
    if hit:
        return hit
    return await _click_text_button(
        p,
        [
            re.compile(r"^next$", re.I),
            re.compile(r"^berikutnya$", re.I),
            re.compile(r"^lanjut$", re.I),
        ],
    )


async def _click_password_next(p) -> str:
    """Password step only — after password is typed."""
    hit = await _click_sel_if_visible(p, ["#passwordNext", "#idvPreregisteredPhoneNext"])
    if hit:
        return hit
    return await _click_text_button(
        p,
        [
            re.compile(r"^next$", re.I),
            re.compile(r"^berikutnya$", re.I),
            re.compile(r"^lanjut$", re.I),
        ],
    )


async def _press_enter(p) -> None:
    try:
        await p.keyboard.press("Enter")
    except Exception:
        pass


def _xai_path(url: str) -> str:
    """Path on accounts.x.ai only (never match accounts.google.com)."""
    u = (url or "").lower()
    # hostname must be accounts.x.ai (not accounts.google.com — "/account" is a
    # substring of "/accounts.google…" and caused false success → bounce to sign-in)
    if "accounts.x.ai" not in u:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(u)
        host = (parsed.hostname or "").lower()
        if host != "accounts.x.ai" and not host.endswith(".accounts.x.ai"):
            return ""
        return parsed.path or "/"
    except Exception:
        # fallback: path after host
        try:
            after = u.split("accounts.x.ai", 1)[1]
            return after.split("?", 1)[0] or "/"
        except Exception:
            return ""


def _url_of(p) -> str:
    try:
        return p.url or ""
    except Exception:
        return ""


def _is_google_consent_url(url: str) -> bool:
    """Google interstitial that needs a click (ToS / allow / continue) — not sit-still."""
    u = (url or "").lower()
    if "accounts.google.com" not in u and "google.com/o/oauth" not in u:
        return False
    return any(
        x in u
        for x in (
            "speedbump",
            "workspacetermsofservice",
            "gaplustos",
            "consent",
            "oauthchooseaccount",
            "signin/oauth",
            "challenge/iap",
            "challenge/dp",
            "interactiveLoginConsent",
        )
    )


def _is_exchange_midflight(url: str) -> bool:
    """OIDC still exchanging on accounts.x.ai — DO NOT navigate away.

    Never treat Google speedbump / consent pages as mid-flight (those need clicks).
    """
    u = (url or "").lower()
    if _is_google_consent_url(u):
        return False
    if "accounts.google.com" in u:
        return False  # still on Google — login/consent handlers own this
    return EXCHANGE_HINT in u or "exchange-token" in u


def _is_session_ready_url(url: str) -> bool:
    """True when redirect chain finished on a post-auth surface."""
    u = (url or "").lower()
    if _is_exchange_midflight(u):
        return False  # still mid-flight
    path = _xai_path(u)
    if path == "/account" or path.startswith("/account/"):
        return True
    # sign-up with redirect=grok-com often lands on grok.com after exchange
    if "grok.com" in u and "accounts.google" not in u:
        return True
    return False


async def _on_xai_account(p) -> bool:
    """True only when session is ready (not Google, not exchange mid-flight, not sign-in)."""
    url = _url_of(p)
    if _is_session_ready_url(url):
        return True
    path = _xai_path(url)
    if not path:
        return False
    if path.startswith("/sign-in") or path.startswith("/sign-up"):
        return False
    if _is_exchange_midflight(url):
        return False
    # signed-in markers only on accounts.x.ai non-auth pages
    try:
        if await p.locator("text=/Manage your account|Welcome,?\\s*\\w+/i").count() > 0:
            if "sign-in" not in path and "sign-up" not in path:
                return True
    except Exception:
        pass
    return False


async def wait_oidc_redirect_settle(
    p,
    *,
    log: Optional[LogFn] = None,
    timeout: float = 60.0,
) -> str:
    """Wait for Google → exchange-token → /account|grok.com to finish naturally.

    NEVER goto mid-exchange — that cancels cookie binding and bounces to sign-in.
    Only after timeout (and only if exchange already finished) nudge /account once.
    """
    deadline = time.monotonic() + timeout
    last = ""
    saw_exchange = False
    left_google = False
    sign_in_hits = 0

    while time.monotonic() < deadline:
        url = _url_of(p)
        ul = url.lower()
        if url and url != last:
            _log(log, f"OIDC redirect: {url[:130]}")
            last = url

        if "accounts.google.com" in ul or "google.com/o/oauth" in ul:
            await asyncio.sleep(0.4)
            continue

        if _is_exchange_midflight(ul):
            saw_exchange = True
            left_google = True
            # critical: sit still, let complete → next hop
            await asyncio.sleep(0.5)
            continue

        if _is_session_ready_url(ul):
            # brief pause so Set-Cookie from final hop lands
            await asyncio.sleep(1.8)
            url2 = _url_of(p)
            if _is_exchange_midflight(url2):
                # rare: navigated again into exchange
                await asyncio.sleep(0.5)
                continue
            if _is_session_ready_url(url2) or await _on_xai_account(p):
                _log(log, f"OIDC settled: {(url2 or url)[:120]}")
                return url2 or url
            await asyncio.sleep(0.5)
            continue

        path = _xai_path(ul)
        if path.startswith("/sign-in") or path.startswith("/sign-up"):
            left_google = True
            sign_in_hits += 1
            # flash of sign-in right after exchange is sometimes transient
            if saw_exchange and sign_in_hits < 6:
                _log(log, f"OIDC brief sign-in flash — wait ({sign_in_hits})")
                await asyncio.sleep(1.0)
                continue
            if not saw_exchange and sign_in_hits < 3:
                await asyncio.sleep(0.8)
                continue
            raise RuntimeError(
                f"OIDC bounced to login  url={url[:120]}  saw_exchange={saw_exchange}"
            )

        if left_google or saw_exchange:
            await asyncio.sleep(0.4)
            continue
        await asyncio.sleep(0.4)

    # Timeout: only nudge /account if exchange already finished (not mid-flight)
    url = _url_of(p)
    if _is_exchange_midflight(url):
        # still exchanging — wait a bit more, never hard-nav
        _log(log, "OIDC still on exchange-token at timeout — extra wait 8s")
        for _ in range(16):
            await asyncio.sleep(0.5)
            url = _url_of(p)
            if not _is_exchange_midflight(url):
                break
        if _is_session_ready_url(url) or await _on_xai_account(p):
            _log(log, f"OIDC settled (late): {url[:120]}")
            return url

    if saw_exchange and not _is_exchange_midflight(url):
        _log(log, "OIDC settle timeout after exchange — gentle /account once")
        try:
            await p.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(2.0)
            url = _url_of(p)
            if _is_session_ready_url(url) or await _on_xai_account(p):
                if "sign-in" not in (url or "").lower():
                    return url
        except Exception as e:
            _log(log, f"account nudge warn: {e}")

    raise RuntimeError(
        f"OIDC redirect settle timeout  url={(_url_of(p) or '')[:120]}  "
        f"saw_exchange={saw_exchange}"
    )


async def _challenge_blocked(p) -> str:
    """Return reason string if Google shows a hard challenge we cannot auto-solve."""
    try:
        body = await p.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 4000)"
        )
    except Exception:
        return ""
    text = (body or "").lower()
    checks = [
        ("captcha", "captcha / recaptcha"),
        ("unusual activity", "unusual activity"),
        ("verify it’s you", "verify it's you"),
        ("verify it's you", "verify it's you"),
        ("verifikasi bahwa ini anda", "verify it's you (id)"),
        ("2-step", "2-step verification"),
        ("2-step verification", "2-step verification"),
        ("two-step", "2-step verification"),
        ("authenticator", "authenticator challenge"),
        ("this browser or app may not be secure", "browser not secure"),
        ("couldn't sign you in", "couldn't sign you in"),
        ("tidak dapat masuk", "couldn't sign you in (id)"),
        ("account disabled", "account disabled"),
        ("account has been disabled", "account disabled"),
        ("recovery phone", "recovery challenge"),
    ]
    for needle, reason in checks:
        if needle in text:
            return reason
    return ""


async def _click_account_chooser_once(p, email: str, log=None) -> bool:
    """Only on real account-chooser pages — never on password step (email is shown as label)."""
    url = (p.url or "").lower()
    # password step always shows email as text — do NOT click it
    if await _password_field_visible(p):
        return False
    if "challenge/pwd" in url or "challenge/password" in url:
        return False
    chooser_url = any(
        x in url
        for x in (
            "accountchooser",
            "oauthchooseaccount",
            "signin/accountchooser",
            "ListAccounts",
            "AccountChooser",
        )
    )
    # If identifier input is visible, this is type-email page not chooser
    if await _identifier_field_visible(p):
        return False
    if not chooser_url and "accounts.google" not in url:
        return False

    # Prefer data-identifier list entry (real chooser row)
    try:
        chooser = p.locator(
            f'div[data-identifier="{email}"], li[data-identifier="{email}"], '
            f'[data-identifier="{email}"][role="link"], '
            f'[data-email="{email}"]'
        ).first
        if await chooser.count() > 0 and await chooser.is_visible():
            await chooser.click(timeout=3000)
            _log(log, "account chooser: data-identifier click")
            await asyncio.sleep(1.0)
            return True
    except Exception:
        pass
    return False


async def _click_google_consent(p, log=None) -> bool:
    """Gsuiteto9router clickConsent — multi-step (scroll → I understand → Allow/Lanjutkan).

    Workspace ToS often needs:
      1) Scroll down (enable button)
      2) #gaplustosNext / "I understand"
      3) later OAuth Allow / Lanjutkan / Continue
    Call this in a poll loop — one click per call.
    """
    url = (_url_of(p) or "").lower()

    # 1) Scroll down on Workspace ToS (button may stay disabled until scrolled)
    try:
        for sel in (
            '[aria-label="Scroll down"]',
            'button:has-text("Scroll down")',
            'div[jsname][role="button"]:has-text("Scroll")',
        ):
            scroll = p.locator(sel).first
            if await scroll.count() > 0 and await scroll.is_visible():
                await scroll.click(timeout=2000)
                _log(log, f"google consent: scroll ({sel})")
                await asyncio.sleep(0.8)
                return True
    except Exception:
        pass

    # Try scrolling the terms box itself (common on workspacetermsofservice)
    if "speedbump" in url or "workspace" in url or "gaplustos" in url:
        try:
            await p.evaluate(
                """() => {
                    const boxes = [...document.querySelectorAll(
                        'div[role="region"], div[tabindex], .TOSbox, div'
                    )].filter(el => {
                        const s = getComputedStyle(el);
                        return (s.overflowY === 'auto' || s.overflowY === 'scroll')
                            && el.scrollHeight > el.clientHeight + 20;
                    });
                    for (const el of boxes.slice(0, 4)) {
                        el.scrollTop = el.scrollHeight;
                    }
                    window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            await asyncio.sleep(0.4)
        except Exception:
            pass

    # 2) Priority order matches Gsuiteto9router/bot.js clickConsent
    #    "I understand" / gaplustos BEFORE generic Continue (2-step consent)
    selectors = [
        "#gaplustosNext button",
        "#gaplustosNext",
        'button:has-text("I understand")',
        'button:has-text("I Understand")',
        'button:has-text("Saya mengerti")',
        'button:has-text("Saya Memahami")',
        "#submit_approve_access button",
        "#submit_approve_access",
        'button:has-text("Lanjutkan")',
        'button:has-text("Continue")',
        'button:has-text("Allow")',
        'button:has-text("Izinkan")',
        'button:has-text("Accept")',
        'button:has-text("Setuju")',
        'button:has-text("Confirm")',
        'button:has-text("Login")',
        'button:has-text("Log in")',
        'button:has-text("Sign in")',
        'button:has-text("Masuk")',
    ]
    for sel in selectors:
        try:
            el = p.locator(sel).first
            if await el.count() == 0:
                continue
            if not await el.is_visible():
                continue
            # skip disabled (ToS not scrolled yet)
            try:
                disabled = await el.is_disabled()
                if disabled:
                    continue
            except Exception:
                pass
            await el.click(timeout=3000, force=False)
            _log(log, f"google consent: {sel}")
            await asyncio.sleep(1.5)
            return True
        except Exception:
            continue

    # 3) role-based fallback (locale variants)
    for name in (
        re.compile(r"i\s*understand", re.I),
        re.compile(r"saya\s*mengerti", re.I),
        re.compile(r"^lanjutkan$", re.I),
        re.compile(r"^continue$", re.I),
        re.compile(r"^allow$", re.I),
        re.compile(r"^izinkan$", re.I),
        re.compile(r"^accept$", re.I),
    ):
        try:
            loc = p.get_by_role("button", name=name).first
            if await loc.count() > 0 and await loc.is_visible():
                try:
                    if await loc.is_disabled():
                        continue
                except Exception:
                    pass
                await loc.click(timeout=3000)
                _log(log, f"google consent: role:{name.pattern}")
                await asyncio.sleep(1.5)
                return True
        except Exception:
            continue

    return False


async def google_login_flow(
    p,
    email: str,
    password: str,
    *,
    log: Optional[LogFn] = None,
    timeout: float = 120.0,
) -> bool:
    """Google login phased like Gsuiteto9router/bot.js:

    1) optional account chooser (once)
    2) email #identifierId + Enter
    3) wait password field → type + Enter
    4) consent poll until back on accounts.x.ai
    """
    deadline = time.monotonic() + timeout
    email_done = False
    pass_done = False
    chooser_done = False
    t0 = time.time()

    # wait until Google (or already back on xAI)
    for _ in range(40):
        if await _on_xai_account(p):
            _log(log, f"landed xAI url={(p.url or '')[:80]}")
            return True
        url = (p.url or "").lower()
        if "accounts.google.com" in url or "google.com/o/oauth" in url:
            break
        await asyncio.sleep(0.25)

    while time.monotonic() < deadline:
        if await _on_xai_account(p):
            _log(log, f"landed xAI url={(p.url or '')[:80]}")
            return True

        blocked = await _challenge_blocked(p)
        if blocked:
            raise RuntimeError(f"google challenge blocked: {blocked}")

        url = (p.url or "").lower()
        pwd_vis = await _password_field_visible(p)
        id_vis = await _identifier_field_visible(p)

        # --- Account chooser (once, never on password page) ---
        if not chooser_done and not pwd_vis and not email_done:
            if await _click_account_chooser_once(p, email, log=log):
                chooser_done = True
                await asyncio.sleep(0.8)
                continue
            # if no chooser row, don't spin forever
            if any(x in url for x in ("accountchooser", "oauthchooseaccount")):
                chooser_done = True

        # --- Email step (bot.js: #identifierId + Enter) ---
        if not email_done and id_vis and not pwd_vis:
            _log(log, "Google: Email...")
            ok = await _type_human(
                p,
                [
                    "#identifierId",
                    'input[type="email"]',
                    'input[name="identifier"]',
                    'input[autocomplete="username"]',
                ],
                email,
            )
            if not ok:
                ok = await _fill_visible(
                    p,
                    ["#identifierId", 'input[type="email"]', 'input[name="identifier"]'],
                    email,
                )
            if ok:
                email_done = True
                _log(log, f"google email filled: {email}")
                await asyncio.sleep(0.45)
                # bot.js: Enter first; only #identifierNext — never #passwordNext
                await _press_enter(p)
                nxt = await _click_identifier_next(p)
                if nxt:
                    _log(log, f"google email next: {nxt}")
                await asyncio.sleep(1.5)
                continue
            await asyncio.sleep(0.4)
            continue

        # --- Password step (bot.js: wait Passwd, type, Enter) ---
        # CRITICAL: never re-click email label on this page
        if not pass_done and (pwd_vis or (email_done and "challenge" in url)):
            # wait up to ~8s for password field (bot.js wait 2s + selector)
            if not pwd_vis:
                for _ in range(16):
                    if await _password_field_visible(p):
                        pwd_vis = True
                        break
                    if await _on_xai_account(p):
                        return True
                    await asyncio.sleep(0.5)

            if not await _password_field_visible(p):
                _log(log, f"password field not ready url={(p.url or '')[:90]}")
                await asyncio.sleep(0.5)
                # if still on identifier, allow retry email once
                if await _identifier_field_visible(p):
                    email_done = False
                continue

            _log(log, "Google: Password...")
            ok = await _type_human(
                p,
                [
                    'input[type="password"][name="Passwd"]',
                    'input[name="Passwd"]',
                    '#password input[type="password"]',
                    '#password input',
                    'input[type="password"]',
                    'input[autocomplete="current-password"]',
                ],
                password,
            )
            if not ok:
                ok = await _fill_visible(
                    p,
                    [
                        'input[type="password"][name="Passwd"]',
                        'input[name="Passwd"]',
                        'input[type="password"]',
                    ],
                    password,
                )
            if ok:
                pass_done = True
                email_done = True  # lock email step
                chooser_done = True
                _log(log, "google password filled")
                await asyncio.sleep(0.45)
                # password submitted: Enter then #passwordNext only (after filled)
                await _press_enter(p)
                nxt = await _click_password_next(p)
                if nxt:
                    _log(log, f"google password next: {nxt}")
                await asyncio.sleep(1.5)
                continue
            _log(log, f"password type failed url={(p.url or '')[:90]}")
            await asyncio.sleep(0.5)
            continue

        # --- Google consent interstitials (speedbump / I understand / Allow) ---
        # Multi-step: poll every loop like Gsuiteto9router (one click per call).
        # MUST run before exchange mid-flight sit-still.
        if pass_done or _is_google_consent_url(url):
            # never treat speedbump as "sit still"
            if await _click_google_consent(p, log=log):
                await asyncio.sleep(1.0)
                continue
            # still on consent page but no button yet — wait & retry
            if _is_google_consent_url(url):
                if int(time.time() * 2) % 6 == 0:  # rate-limit log
                    _log(log, f"google consent waiting buttons… url={(p.url or '')[:90]}")
                await asyncio.sleep(0.8)
                continue

        # Mid OIDC exchange on accounts.x.ai only — sit still
        if _is_exchange_midflight(url):
            _log(log, f"OIDC exchange mid-flight: {(p.url or '')[:100]}")
            await asyncio.sleep(0.5)
            continue

        # After password, still on Google but not consent — keep polling consent
        if pass_done and "accounts.google.com" in url:
            if await _click_google_consent(p, log=log):
                await asyncio.sleep(1.0)
                continue

        # Mark email done if we somehow have password without logging email
        if pwd_vis and not email_done:
            email_done = True
            continue

        await asyncio.sleep(0.4)

    # final: if mid-exchange or nearly done, let wait_oidc_redirect_settle handle it
    if await _on_xai_account(p) or _is_exchange_midflight(_url_of(p)):
        return True
    # do NOT hard-goto /account here — interrupts OIDC; outer settle waits
    raise RuntimeError(
        f"google login timeout url={(_url_of(p) or '')[:120]}  "
        f"email_done={email_done} pass_done={pass_done}  "
        f"elapsed={time.time() - t0:.0f}s"
    )


async def register_google_async(
    p,
    email: str,
    password: str,
    *,
    log: Optional[LogFn] = None,
    timeout: float = 150.0,
) -> dict:
    """Full Google OIDC path on a raw Playwright page. Returns meta dict."""
    t0 = time.time()
    _log(log, f"google signup start email={email}")
    await open_xai_signup(p, log=log)
    ok = await click_continue_with_google(p, log=log)
    if not ok:
        raise RuntimeError("could not open Google OAuth from xAI sign-up")
    await google_login_flow(p, email, password, log=log, timeout=timeout)

    # Wait for OIDC redirect chain (exchange-token → /account|grok.com).
    # Do NOT force goto /account or grok.com while exchange is running.
    final_url = await wait_oidc_redirect_settle(p, log=log, timeout=min(60.0, timeout))

    if not await _on_xai_account(p) and not _is_session_ready_url(final_url):
        raise RuntimeError(f"not on xAI session after google  url={(p.url or '')[:120]}")

    elapsed = time.time() - t0
    _log(
        log,
        f"google signup OK email={email}  {elapsed:.1f}s  url={(_url_of(p) or final_url)[:80]}",
    )
    return {
        "email": email,
        "password": password,
        "mode": "google",
        "provider": "google",
        "given_name": "",
        "family_name": "",
        "elapsed_sec": elapsed,
        "url": _url_of(p) or final_url,
    }


def register_one_google(
    page_adapter: Any,
    email: str,
    password: str,
    *,
    log: Optional[LogFn] = None,
    timeout: float = 150.0,
) -> dict:
    """Sync entry for farm (PwPageAdapter or raw page with loop)."""
    p = _raw(page_adapter)
    return _run(
        page_adapter,
        register_google_async(p, email, password, log=log, timeout=timeout),
    )


def sync_register_google(page_adapter, email: str, password: str, log=None, timeout=150.0) -> dict:
    return register_one_google(
        page_adapter, email, password, log=log, timeout=timeout
    )


# ── CLI smoke ──────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Google inventory / claim smoke")
    ap.add_argument("--stats", action="store_true", help="print inventory stats")
    ap.add_argument("--claim", action="store_true", help="claim one account (dry mark pending)")
    ap.add_argument("--file", default="", help="google_pass path")
    args = ap.parse_args(argv)
    path = Path(args.file).expanduser() if args.file else resolve_google_accounts_file()
    if args.stats or not args.claim:
        st = inventory_stats(path)
        print(json.dumps(st, indent=2))
        if not args.claim:
            return 0
    pair = claim_next_google_account(path, log=print)
    if not pair:
        print("NO_ACCOUNT")
        return 1
    print(f"CLAIMED {pair[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

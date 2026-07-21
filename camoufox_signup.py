"""
Camoufox / Playwright-native signup steps (ported from flash-grok-farm).

Use these instead of DrissionPage run_js thrash when engine=camoufox.
All functions accept either:
  - PwPageAdapter (with .raw + ._loop + ._async)
  - raw Playwright Page + optional loop for async Camoufox
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Callable, Optional

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
ACCOUNT_URL = "https://accounts.x.ai/account"


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
    # sync playwright: coro might already be result if someone passed wrong
    if asyncio.iscoroutine(coro):
        # create temp loop only if needed (shouldn't for sync)
        return asyncio.get_event_loop().run_until_complete(coro)
    return coro


def _log(log: Optional[Callable], msg: str) -> None:
    if log:
        log(msg)


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


async def _email_visible(p) -> bool:
    try:
        loc = p.locator(
            'input[type="email"], input[name="email"], input[autocomplete="email"]'
        ).first
        return await loc.count() > 0 and await loc.is_visible()
    except Exception:
        return False


async def _fill_input(p, selectors: list[str], value: str) -> bool:
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
            await el.evaluate(
                """(el, v) => {
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value'
                    )?.set;
                    if (setter) setter.call(el, v); else el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }""",
                value,
            )
            return True
        except Exception:
            continue
    return False


async def open_email_signup(p, log=None) -> bool:
    """Click Sign up with email until email field shows (flash _open_email_signup_form)."""
    if await _email_visible(p):
        return True
    await _dismiss_cookie(p)

    for round_i in range(1, 8):
        await _dismiss_cookie(p)
        if await _email_visible(p):
            return True

        clicked = None
        try:
            loc = p.get_by_role("button", name=re.compile(r"sign\s*up\s*with\s*email", re.I))
            if await loc.count() > 0 and await loc.first.is_visible():
                await loc.first.scroll_into_view_if_needed(timeout=3000)
                await loc.first.click(timeout=4000, force=(round_i >= 3))
                clicked = "role"
        except Exception:
            pass

        if not clicked:
            for kw in ("Sign up with email", "Continue with email", "Sign up with Email"):
                try:
                    loc = p.get_by_role("button", name=re.compile(re.escape(kw), re.I))
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000, force=(round_i >= 3))
                        clicked = kw
                        break
                except Exception:
                    continue

        if not clicked or round_i >= 2:
            try:
                js = await p.evaluate(
                    """() => {
                        const nodes = [...document.querySelectorAll('button, a, [role="button"], div')];
                        for (const b of nodes) {
                            const t = (b.innerText || b.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (/sign up with email/i.test(t)) {
                                const r = b.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) { b.click(); return t; }
                            }
                        }
                        return '';
                    }"""
                )
                if js:
                    clicked = f"js:{js}"
            except Exception:
                pass

        if clicked:
            _log(log, f"email-signup click round {round_i}: {clicked}")

        for _ in range(14):
            if await _email_visible(p):
                return True
            await asyncio.sleep(0.25)

        try:
            await p.keyboard.press("Enter")
            await asyncio.sleep(0.4)
            if await _email_visible(p):
                return True
        except Exception:
            pass

        if round_i in (3, 5):
            try:
                _log(log, "reload signup (email form not open)")
                await p.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1.0)
            except Exception as e:
                _log(log, f"signup reload warn: {e}")

    # last resort direct URLs
    for url in (
        f"{SIGNUP_URL}&email=true" if "?" in SIGNUP_URL else f"{SIGNUP_URL}?email=true",
        "https://accounts.x.ai/sign-up/email",
    ):
        try:
            await p.goto(url, wait_until="domcontentloaded", timeout=20000)
            await asyncio.sleep(0.8)
            if await _email_visible(p) or await open_email_signup_once(p, log):
                return True
        except Exception:
            continue
    return await _email_visible(p)


async def open_email_signup_once(p, log=None) -> bool:
    try:
        loc = p.get_by_role("button", name=re.compile(r"sign\s*up\s*with\s*email", re.I))
        if await loc.count() > 0:
            await loc.first.click(timeout=4000)
            await asyncio.sleep(0.5)
        return await _email_visible(p)
    except Exception:
        return False


async def fill_email_and_submit(p, email: str, log=None) -> None:
    if not await open_email_signup(p, log):
        raise RuntimeError("Could not open Sign up with email form")
    ok = await _fill_input(
        p,
        [
            'input[name="email"]',
            'input[type="email"]',
            'input[autocomplete="email"]',
            'input[placeholder*="email" i]',
        ],
        email,
    )
    if not ok:
        raise RuntimeError("Failed to fill email")
    await asyncio.sleep(0.35)
    try:
        await p.locator('button[type="submit"]').filter(
            has_text=re.compile(r"^sign up$", re.I)
        ).click(timeout=5000)
    except Exception:
        try:
            await p.get_by_role("button", name=re.compile(r"^sign up$", re.I)).click(timeout=4000)
        except Exception:
            await p.evaluate(
                """() => {
                    const btns = [...document.querySelectorAll('button')];
                    for (const b of btns) {
                        const t = (b.innerText||'').trim();
                        if (/^sign up$/i.test(t)) { b.click(); return; }
                    }
                }"""
            )
    await asyncio.sleep(1.2)


async def fill_otp(p, code: str, log=None) -> bool:
    """Keyboard/per-slot OTP fill (flash style — no React-breaking JS setter)."""
    code = re.sub(r"[^A-Za-z0-9]", "", (code or "").upper())[:6]
    if len(code) < 3:
        return False

    async def passed() -> bool:
        try:
            if await p.locator("text=Complete your sign up").count() > 0:
                return True
            if await p.locator('input[type="password"]').count() > 0:
                if await p.locator(
                    'input[name="code"], input[autocomplete="one-time-code"]'
                ).count() == 0:
                    return True
        except Exception:
            pass
        return False

    if await passed():
        return True

    # wait inputs
    for _ in range(20):
        n = await p.locator(
            'input[name="code"], input[autocomplete="one-time-code"], input[maxlength="1"]'
        ).count()
        if n > 0:
            break
        await asyncio.sleep(0.3)

    slots = p.locator('input[maxlength="1"]')
    sn = await slots.count()
    if sn >= 4:
        try:
            await slots.first.click(timeout=2000)
            await p.keyboard.type(code[:6], delay=40)
            await asyncio.sleep(0.4)
            if await passed():
                return True
        except Exception as e:
            _log(log, f"OTP slot type warn: {e}")
        for i, ch in enumerate(code[: min(6, sn)]):
            try:
                await slots.nth(i).click(timeout=1000)
                await p.keyboard.press("Backspace")
                await p.keyboard.type(ch, delay=25)
            except Exception:
                continue
        await asyncio.sleep(0.3)
        if await passed():
            return True

    # single field
    for sel in (
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
    ):
        try:
            el = p.locator(sel).first
            if await el.count() == 0:
                continue
            await el.click(timeout=2000)
            await el.fill(code)
            await asyncio.sleep(0.3)
            break
        except Exception:
            continue

    try:
        await p.get_by_role(
            "button", name=re.compile(r"confirm\s*email|verify|confirm", re.I)
        ).click(timeout=3000)
    except Exception:
        pass
    await asyncio.sleep(0.8)
    return await passed()


async def fill_profile_and_complete(
    p,
    first: str,
    last: str,
    password: str,
    log=None,
    timeout: float = 75.0,
) -> bool:
    """Flash complete_signup: fill + turnstile wait + Complete, wait /account."""
    await _dismiss_cookie(p)
    deadline = time.monotonic() + timeout

    async def signup_done() -> bool:
        try:
            cur = (p.url or "").lower()
            if "/account" in cur and "sign-up" not in cur and "sign-in" not in cur:
                return True
            if await p.locator("text=Complete your sign up").count() == 0:
                if await p.get_by_role(
                    "button", name=re.compile(r"complete\s+sign\s*up", re.I)
                ).count() == 0:
                    if await p.locator('input[type="password"]').count() == 0:
                        return True
            if await p.locator("text=/Welcome,?\\s*\\w+|Manage your account/i").count() > 0:
                if await p.locator("text=Complete your sign up").count() == 0:
                    return True
        except Exception:
            return False
        return False

    async def token_len() -> int:
        try:
            return int(
                await p.evaluate(
                    """() => {
                        const i = document.querySelector('input[name="cf-turnstile-response"]');
                        return i ? String(i.value||'').length : 0;
                    }"""
                )
                or 0
            )
        except Exception:
            return 0

    # fill names
    await _fill_input(
        p,
        [
            'input[data-testid="givenName"]',
            'input[name="givenName"]',
            'input[name="firstName"]',
            'input[autocomplete="given-name"]',
            'input[name*="first" i]',
        ],
        first,
    )
    await asyncio.sleep(0.2)
    await _fill_input(
        p,
        [
            'input[data-testid="familyName"]',
            'input[name="familyName"]',
            'input[name="lastName"]',
            'input[autocomplete="family-name"]',
            'input[name*="last" i]',
        ],
        last,
    )
    await asyncio.sleep(0.2)
    await _fill_input(
        p,
        [
            'input[data-testid="password"]',
            'input[name="password"]',
            'input[type="password"]',
        ],
        password,
    )
    await asyncio.sleep(0.5)
    _log(log, f"profile filled {first} {last}")

    round_i = 0
    while time.monotonic() < deadline:
        round_i += 1
        if await signup_done():
            _log(log, f"already on account page (url={(p.url or '')[:80]})")
            return True

        if round_i <= 2:
            await _fill_input(
                p,
                ['input[name="givenName"]', 'input[name="firstName"]', 'input[autocomplete="given-name"]'],
                first,
            )
            await _fill_input(
                p,
                ['input[name="familyName"]', 'input[name="lastName"]', 'input[autocomplete="family-name"]'],
                last,
            )
            await _fill_input(p, ['input[type="password"]', 'input[name="password"]'], password)

        # wait natural turnstile
        tok = await token_len()
        if tok <= 20:
            # click turnstile box if present
            try:
                await p.evaluate(
                    """() => {
                        const ifr = document.querySelector('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]');
                        if (ifr) { ifr.click(); return true; }
                        const w = document.querySelector('[data-sitekey], .cf-turnstile');
                        if (w) { w.click(); return true; }
                        return false;
                    }"""
                )
            except Exception:
                pass
            # wait up to 6s for token
            for _ in range(12):
                tok = await token_len()
                if tok > 20:
                    break
                if await signup_done():
                    return True
                await asyncio.sleep(0.5)

        tok = await token_len()
        _log(log, f"complete round {round_i}: token_len={tok}")
        if tok <= 20 and (deadline - time.monotonic()) > 8:
            await asyncio.sleep(0.8)
            continue

        # click Complete — flash style
        try:
            await p.get_by_role(
                "button", name=re.compile(r"complete\s+sign\s*up", re.I)
            ).click(timeout=3500)
        except Exception:
            try:
                await p.get_by_role(
                    "button", name=re.compile(r"create\s*account", re.I)
                ).click(timeout=2500)
            except Exception:
                try:
                    await p.evaluate(
                        """() => {
                            const btns = [...document.querySelectorAll('button')];
                            for (const b of btns) {
                                const t = (b.innerText||'').toLowerCase();
                                if (t.includes('complete sign') || t.includes('create account')) {
                                    b.click(); return true;
                                }
                            }
                            return false;
                        }"""
                    )
                except Exception:
                    pass

        # wait URL /account (flash)
        try:
            await p.wait_for_url(
                re.compile(r".*/account(?!.*sign-).*$", re.I),
                timeout=7000,
            )
            _log(log, f"complete OK → {(p.url or '')[:100]}")
            return True
        except Exception:
            pass

        # poll leave / bounce
        for _ in range(12):
            if await signup_done():
                _log(log, f"complete OK (poll) → {(p.url or '')[:100]}")
                return True
            try:
                cur = (p.url or "").lower()
                if "sign-up" in cur and await p.locator(
                    "text=/Sign up with email|Create your account/i"
                ).count() > 0:
                    _log(log, "bounced to signup chooser after Complete — retry")
                    break
                # 404 page?
                if await p.locator("text=/404|page not found|not found/i").count() > 0:
                    _log(log, "hit 404 after Complete — go /account")
                    try:
                        await p.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    if await signup_done():
                        return True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.3)

        # form gone but no password = done
        try:
            if await p.locator("text=Complete your sign up").count() == 0:
                if await p.locator('input[type="password"]').count() == 0:
                    _log(log, "complete form gone — treat as done")
                    # settle on /account
                    try:
                        await p.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    return True
        except Exception:
            pass

        await asyncio.sleep(0.5)

    # final: try /account anyway
    try:
        await p.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=15000)
        await asyncio.sleep(1)
        if await signup_done() or "sign-in" not in (p.url or "").lower():
            return True
    except Exception:
        pass
    return await signup_done()


# ── Sync wrappers for farm ──────────────────────────────────────────────────

def sync_open_signup(page_adapter, log=None) -> None:
    p = _raw(page_adapter)
    async def _go():
        try:
            await p.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            await p.goto(SIGNUP_URL, wait_until="commit", timeout=45000)
        await asyncio.sleep(1.0)
        await _dismiss_cookie(p)
        ok = await open_email_signup(p, log)
        if not ok:
            raise RuntimeError("Could not open Sign up with email form")
    _run(page_adapter, _go())


def sync_fill_email(page_adapter, email: str, log=None) -> None:
    _run(page_adapter, fill_email_and_submit(_raw(page_adapter), email, log))


def sync_fill_otp(page_adapter, code: str, log=None) -> bool:
    return bool(_run(page_adapter, fill_otp(_raw(page_adapter), code, log)))


def sync_profile_complete(
    page_adapter, first: str, last: str, password: str, log=None, timeout: float = 75
) -> bool:
    return bool(
        _run(
            page_adapter,
            fill_profile_and_complete(
                _raw(page_adapter), first, last, password, log, timeout
            ),
        )
    )

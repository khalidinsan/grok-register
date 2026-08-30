"""Hybrid v2 — REST signup against accounts.x.ai /api/auth/* endpoints.

Flow (from live audit 2026-08-30, logs/api_flow_bodies.jsonl):
  BROWSER (short): open signup → castle mint + cf_clearance + cookies +
                   turnstile solve on the final profile form.
  HTTP (curl_cffi): send-verification-code → verify-email → ValidatePassword
                   (gRPC-web) → create-account (REST, turnstileToken inside).

Replaces the v1 gRPC-web CreateEmail + next-action server-action path, which
no longer exists on accounts.x.ai.

Everything here mirrors the exact browser payloads:
  - POST /api/auth/send-verification-code  {"email", "castleRequestToken"}
  - POST /api/auth/sign-up/verify-email    {"email", "code"}
  - POST /auth_mgmt.AuthManagement/ValidatePassword  (grpc-web proto)
  - POST /api/auth/sign-up/create-account  {"email","password","givenName",
        "familyName","emailValidationCode","turnstileToken","castleRequestToken"?}
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .protocol_session import ProtocolSession

ROOT = Path(__file__).resolve().parent.parent

LogFn = Callable[[str], None]

SEND_CODE_URL = "https://accounts.x.ai/api/auth/send-verification-code"
VERIFY_URL = "https://accounts.x.ai/api/auth/sign-up/verify-email"
CREATE_URL = "https://accounts.x.ai/api/auth/sign-up/create-account"
VALIDATE_PW_URL = "https://accounts.x.ai/auth_mgmt.AuthManagement/ValidatePassword"

_BASE_HEADERS = {
    "content-type": "application/json",
    "origin": "https://accounts.x.ai",
    "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
    "accept": "*/*",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def _default_log(msg: str) -> None:
    try:
        from DrissionPage_example import slog  # type: ignore

        slog("HYBRID2", msg)
    except Exception:
        print(f"[HYBRID2] {msg}", flush=True)


class RestSignupClient:
    """Pure-HTTP signup client over a ProtocolSession (curl_cffi)."""

    def __init__(self, session: ProtocolSession, log: Optional[LogFn] = None):
        self.s = session
        self.lg = log or (lambda m: None)

    def _post_json(self, url: str, payload: dict, timeout: int = 30) -> dict:
        body = json.dumps(payload)
        r = self.s.post_raw(
            url,
            data=body.encode("utf-8"),
            headers=dict(_BASE_HEADERS),
            timeout=timeout,
        )
        status = int(getattr(r, "status_code", 0) or 0)
        text = ""
        try:
            text = r.text or ""
        except Exception:
            pass
        return {"status": status, "text": text, "raw": r}

    def send_verification_code(self, email: str, castle_token: str) -> dict:
        return self._post_json(
            SEND_CODE_URL,
            {"email": email, "castleRequestToken": castle_token},
        )

    def verify_email(self, email: str, code: str) -> dict:
        return self._post_json(VERIFY_URL, {"email": email, "code": code})

    def create_account(
        self,
        *,
        email: str,
        password: str,
        given_name: str,
        family_name: str,
        email_validation_code: str,
        turnstile_token: str,
        castle_token: str = "",
    ) -> dict:
        payload: dict[str, Any] = {
            "email": email,
            "password": password,
            "givenName": given_name,
            "familyName": family_name,
            "emailValidationCode": email_validation_code,
            "turnstileToken": turnstile_token,
        }
        if castle_token:
            payload["castleRequestToken"] = castle_token
        return self._post_json(CREATE_URL, payload)

    def validate_password_grpc(self, email: str, password: str) -> dict:
        """ValidatePassword is still gRPC-web proto (kept from v1 client)."""
        from .pb_codec import encode_validate_password, wrap_grpc_web

        body = wrap_grpc_web(encode_validate_password(email, password))
        r = self.s.post_raw(
            VALIDATE_PW_URL,
            data=body,
            headers={
                "content-type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "x-user-agent": "connect-es/2.1.1",
                "origin": "https://accounts.x.ai",
                "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
            },
            timeout=30,
        )
        status = int(getattr(r, "status_code", 0) or 0)
        return {"status": status, "raw": r}


def register_one_hybrid_v2(
    *,
    page: Any = None,
    log: Optional[LogFn] = None,
    proxy: str = "",
    get_email: Optional[Callable[..., tuple]] = None,
    get_otp: Optional[Callable[..., str]] = None,
    build_profile: Optional[Callable[[], tuple]] = None,
    get_turnstile_fn: Optional[Callable[..., str]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[dict]:
    """Hybrid v2: short browser session for castle/turnstile, REST for the rest.

    Returns the same result dict shape as register_one_hybrid (v1), or None
    so the caller can fall back to the full browser path.
    """
    lg = log or _default_log
    stop = should_stop or (lambda: False)
    t0 = time.time()

    if get_email is None or get_otp is None or build_profile is None:
        try:
            from email_register import get_email_and_token, get_oai_code
            from DrissionPage_example import build_profile as _bp  # type: ignore

            get_email = get_email or get_email_and_token
            get_otp = get_otp or (lambda tok, em, **kw: get_oai_code(tok, em, timeout=120))
            build_profile = build_profile or _bp
        except Exception as e:
            lg(f"missing email/profile helpers: {e}")
            return None

    if not proxy:
        proxy = (getattr(page, "_proxy_url", "") or "").strip()
    if not proxy:
        try:
            from DrissionPage_example import current_proxy_url  # type: ignore

            proxy = current_proxy_url() or ""
        except Exception:
            pass

    try:
        from .token_harvester import BrowserTokenSession

        tok_sess = BrowserTokenSession(page=page, browser=None, log=lg)
        if page is None:
            try:
                import DrissionPage_example as dpe  # type: ignore

                tok_sess._page = getattr(dpe, "page", None)
                tok_sess._browser = getattr(dpe, "browser", None)
            except Exception:
                pass
        if tok_sess.page() is None:
            lg("no browser page available")
            return None

        lg("open signup (browser: castle + cookies)…")
        tok_sess.open_signup()

        email, mail_token = get_email()
        if not email:
            lg("no email")
            return None
        lg(f"email={email}")
        if stop():
            return None

        # Castle harvest via NETWORK-level request interception (not JS hook —
        # React holds a closure over the original fetch, so window.fetch
        # patching never sees the call). Everything (hook attach, navigation,
        # form fill/submit, event wait) must run INSIDE the session loop —
        # Playwright events only dispatch while the loop is running.
        castle = ""
        browser_cookies: dict = {}
        send_code_ok = False

        def _on_request(req: Any) -> None:
            nonlocal castle, send_code_ok
            try:
                if "send-verification-code" in (req.url or ""):
                    pd = req.post_data or ""
                    if pd:
                        try:
                            j = json.loads(pd)
                            tok = str(j.get("castleRequestToken") or "")
                            if len(tok) > 200:
                                castle = tok
                        except Exception:
                            import re as _re

                            m = _re.search(
                                r'castleRequestToken["\']?\s*:\s*["\']([^"\']{200,})', pd
                            )
                            if m:
                                castle = m.group(1)
                    send_code_ok = True
            except Exception:
                pass

        # Resolve the session event loop (worker global set by start_browser).
        # NOTE: the worker runs as __main__, so `import DrissionPage_example`
        # would create a SECOND module instance without the runtime globals.
        # Read from __main__ (or the already-imported module if it exists).
        loop = None
        raw = None
        try:
            page_raw = tok_sess.page()
            raw = getattr(page_raw, "raw", None) or page_raw
        except Exception:
            pass

        try:
            import __main__ as _main  # type: ignore

            loop = getattr(_main, "_camoufox_loop", None)
        except Exception:
            pass
        if loop is None and "DrissionPage_example" in sys.modules:
            loop = getattr(sys.modules["DrissionPage_example"], "_camoufox_loop", None)
        if loop is None or raw is None:
            lg("no camoufox loop/page — cannot run hybrid v2 harvest")
            return None

        async def _harvest() -> None:
            # hook FIRST (inside loop context)
            if hasattr(raw, "on"):
                raw.on("request", _on_request)
            else:
                raise RuntimeError(f"page {type(raw).__name__} has no .on()")
            # navigate if needed
            cur = ""
            try:
                cur = raw.url or ""
            except Exception:
                pass
            if "sign-up" not in (cur or ""):
                await raw.goto(
                    "https://accounts.x.ai/sign-up?redirect=grok-com",
                    wait_until="load",
                    timeout=60000,
                )
                await asyncio.sleep(2)
                await raw.get_by_role("button", name="Sign up with email").click(
                    timeout=10000
                )
                await asyncio.sleep(1.5)
            # fill + submit
            await raw.locator("input[type=email]").first.fill(email, timeout=15000)
            await asyncio.sleep(0.5)
            try:
                await raw.get_by_role("button", name="Sign up", exact=True).click(
                    timeout=5000
                )
            except Exception:
                try:
                    await raw.get_by_role("button", name="Continue").click(timeout=3000)
                except Exception as e:
                    lg(f"submit click issue: {e}")
            # wait for send-verification-code to fire (events dispatch in-loop)
            for _ in range(30):
                if castle and send_code_ok:
                    break
                await asyncio.sleep(0.5)

        try:
            loop.run_until_complete(_harvest())
        except Exception as e:
            lg(f"harvest fail: {type(e).__name__}: {e}")

        if not castle:
            lg(f"castle harvest failed len={len(castle)} sent={send_code_ok}")
            return None
        lg(f"castle OK len={len(castle)} send_code_ok={send_code_ok}")

        browser_cookies = tok_sess.export_cookies()

        ua = tok_sess.browser_user_agent() or ""
        sess = ProtocolSession(proxy=(proxy or "").strip(), user_agent=ua, impersonate="chrome131")
        jar = dict(browser_cookies or {})
        for stale in ("sso", "sso-rw"):
            jar.pop(stale, None)
        sess.set_cookies(jar)
        client = RestSignupClient(sess, log=lg)

        # send-verification-code already fired natively in the browser during
        # castle harvest (send_code_ok) — the OTP email is on its way.
        if send_code_ok:
            lg("send-code fired natively during castle harvest (skip REST)")
        else:
            r1 = client.send_verification_code(email, castle)
            lg(f"send-code status={r1['status']}")
            if r1["status"] >= 400:
                hint = " (Cloudflare block)" if "cloudflare" in r1["text"][:500].lower() else ""
                lg(f"send-code fail{hint}")
                return None
        if stop():
            return None

        code = get_otp(mail_token, email)
        clean = str(code or "").replace("-", "").strip()
        if not clean:
            lg("no mail code")
            return None
        lg(f"code={clean}")

        r2 = client.verify_email(email, clean)
        lg(f"verify-email status={r2['status']}")
        if r2["status"] >= 400:
            lg(f"verify-email fail body={r2['text'][:200]}")
            return None
        if stop():
            return None

        given, family, password = build_profile()
        try:
            client.validate_password_grpc(email, password)
        except Exception:
            pass  # advisory only

        # Turnstile lives on the PROFILE form — which only appears after the
        # OTP step. verify-email ran via REST, so the browser page is still on
        # the OTP screen: fill the code there natively to advance the UI,
        # then harvest turnstile from the profile form.
        async def _advance_to_profile() -> None:
            # fill OTP input
            await raw.locator("input[name=code]").first.fill(clean, timeout=15000)
            await asyncio.sleep(0.5)
            try:
                await raw.get_by_role("button", name="Confirm email").click(timeout=5000)
            except Exception:
                try:
                    await raw.keyboard.press("Enter")
                except Exception:
                    pass
            await asyncio.sleep(3)

        try:
            loop.run_until_complete(_advance_to_profile())
        except Exception as e:
            lg(f"OTP advance fail: {type(e).__name__}: {e}")

        # now the profile form + turnstile widget should be present
        turnstile = tok_sess.get_turnstile_token(timeout=40, inject=True)
        if len(turnstile) < 80:
            lg(f"turnstile short len={len(turnstile)} — abort hybrid v2")
            return None

        castle2 = tok_sess.read_captured_castle() or castle
        if len(castle2) < 1000:
            castle2 = castle

        r3 = client.create_account(
            email=email,
            password=password,
            given_name=given,
            family_name=family,
            email_validation_code=clean,
            turnstile_token=turnstile,
            castle_token=castle2,
        )
        lg(f"create-account status={r3['status']} elapsed={time.time() - t0:.1f}s")
        if r3["status"] >= 400:
            lg(f"create-account fail body={r3['text'][:240]}")
            return None

        # ── Final step: POST /sign-up server action ────────────────────
        # create-account (REST) creates the account but does NOT start the
        # SSO session. The browser flow then POSTs the Next.js server action
        # /sign-up (next-action hash), which returns the set-cookie redirect
        # chain (auth.grok.com → auth.x.ai → …) that yields the sso cookie.
        # Live hash captured 2026-08-30: 004cc65179980e138cf0ead080c1c772acba244723
        NEXT_ACTION_LIVE = "004cc65179980e138cf0ead080c1c772acba244723"
        DEPLOYMENT_ID = "ece1284a43250a6cb4c806bd6efac8d5d57371b0"
        ROUTER_STATE_TREE = (
            "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22"
            "%3A%5B%22(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B"
            "%22children%22%3A%5B%22__PAGE__%22%2C%7B%7D%2Cnull%2Cnull%2C0%5D"
            "%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull%2C0%5D%7D%2Cnull%2Cnull"
            "%2C0%5D%7D%2Cnull%2Cnull%2C16%5D"
        )
        sso = ""
        try:
            body = json.dumps(
                [
                    {
                        "emailValidationCode": clean,
                        "createUserAndSessionRequest": {
                            "email": email,
                            "givenName": given,
                            "familyName": family,
                            "clearTextPassword": password,
                            "tosAcceptedVersion": 1,
                        },
                        "turnstileToken": turnstile,
                        "conversionId": str(uuid.uuid4()),
                        "castleRequestToken": castle2,
                    }
                ],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            r4 = sess.post_raw(
                "https://accounts.x.ai/sign-up?redirect=grok-com",
                data=body,
                headers={
                    "content-type": "text/plain;charset=UTF-8",
                    "accept": "text/x-component",
                    "next-action": NEXT_ACTION_LIVE,
                    "next-router-state-tree": ROUTER_STATE_TREE,
                    "x-deployment-id": DEPLOYMENT_ID,
                    "origin": "https://accounts.x.ai",
                    "referer": "https://accounts.x.ai/sign-up?redirect=grok-com",
                },
                timeout=45,
            )
            st4 = int(getattr(r4, "status_code", 0) or 0)
            lg(f"sign-up action status={st4}")
            # harvest sso from cookies set by the chain
            try:
                for c in sess.session.cookies:
                    if getattr(c, "name", "") == "sso":
                        sso = getattr(c, "value", "") or ""
            except Exception:
                pass
            if not sso:
                # parse Set-Cookie headers directly
                import re as _re

                try:
                    raw_h = getattr(r4, "headers", None)
                    blob = ""
                    if raw_h is not None:
                        if hasattr(raw_h, "get_list"):
                            blob = "\n".join(
                                (raw_h.get_list("set-cookie") or [])
                                + (raw_h.get_list("Set-Cookie") or [])
                            )
                        else:
                            blob = str(dict(raw_h))
                    m = _re.search(r"\bsso=([^;\s]+)", blob)
                    if m:
                        sso = m.group(1)
                except Exception:
                    pass
        except Exception as e:
            lg(f"sign-up action fail: {e}")

        if not sso:
            # The set-cookie chain (auth.grok.com → auth.x.ai → …) is
            # fingerprint-gated by CF on plain HTTP (curl_cffi gets 403 on
            # /account — cf_clearance is bound to the browser TLS stack).
            # Drive the chain in the LIVE browser instead: it already has the
            # verified-email state, so navigating the action redirect works.
            async def _finish_session() -> None:
                await raw.goto(
                    "https://accounts.x.ai/account",
                    wait_until="domcontentloaded",
                    timeout=45000,
                )
                await asyncio.sleep(2)
                # If we landed on sign-in, try the email login path: the
                # account was created via REST, so log in with the password.
                cur = raw.url or ""
                if "sign-in" in cur:
                    await raw.goto(
                        "https://accounts.x.ai/sign-in?email=true",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    await asyncio.sleep(2)
                    try:
                        await raw.locator("input[type=email]").first.fill(
                            email, timeout=10000
                        )
                        await asyncio.sleep(0.5)
                        # find password field if present on same form
                        pwd = raw.locator("input[type=password]").first
                        try:
                            if await pwd.is_visible(timeout=3000):
                                await pwd.fill(password, timeout=5000)
                        except Exception:
                            pass
                        await raw.keyboard.press("Enter")
                        await asyncio.sleep(4)
                    except Exception as e:
                        lg(f"login attempt fail: {e}")
                # Final settle on grok.com to materialize sso across domains
                try:
                    await raw.goto(
                        "https://grok.com/", wait_until="domcontentloaded", timeout=45000
                    )
                    await asyncio.sleep(2)
                except Exception:
                    pass

            try:
                loop.run_until_complete(_finish_session())
            except Exception as e:
                lg(f"finish session fail: {e}")
            browser_cookies = tok_sess.export_cookies() or {}
            sso = browser_cookies.get("sso") or ""

        if not sso:
            lg("no sso after sign-up action + browser chain")
            return None

        # Materialize wrapper → session SSO when needed (same as v1).
        try:
            from .sso_util import is_session_sso, is_wrapper_sso, materialize_sso_via_browser

            if is_wrapper_sso(sso) or not is_session_sso(sso):
                lg("wrapper sso — materialize via browser…")
                p = tok_sess.page()
                sess_sso = materialize_sso_via_browser(p, sso, log=lg, timeout=40) if p is not None else ""
                if sess_sso and is_session_sso(sess_sso):
                    sso = sess_sso
        except Exception as e:
            lg(f"sso materialize: {e}")

        full_jar = dict(tok_sess.export_cookies() or {})
        full_jar["sso"] = sso
        full_jar.setdefault("sso-rw", sso)
        cf_parts = [f"{k}={v}" for k, v in full_jar.items()
                    if str(k).lower().startswith("cf_") or str(k).lower() in ("__cf_bm", "cf_clearance")]

        result = {
            "email": email,
            "password": password,
            "given_name": given,
            "family_name": family,
            "sso": sso,
            "sso_token": sso,
            "sso_rw": full_jar.get("sso-rw") or sso,
            "apiKey": f"sso={sso}; sso-rw={full_jar.get('sso-rw') or sso}",
            "cookie_header": f"sso={sso}; sso-rw={full_jar.get('sso-rw') or sso}",
            "cloudflare_cookies": "; ".join(cf_parts),
            "cookies": full_jar,
            "providerSpecificData": {"cloudflareCookies": "; ".join(cf_parts)},
            "hybrid": True,
            "hybrid_v2": True,
        }
        lg(f"[+] OK {email}  elapsed={time.time() - t0:.1f}s")
        return result

    except Exception as e:
        lg(f"exception: {e}")
        try:
            lg(traceback.format_exc().splitlines()[-3])
        except Exception:
            pass
        return None

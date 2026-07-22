"""Hybrid registration orchestration for grok-register.

Short Camoufox/browser session harvests castle + cookies + next-action,
then protocol HTTP (curl_cffi) does CreateEmail / OTP verify / profile submit.
"""
from __future__ import annotations

import json
import os
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .auth_client import AuthManagementClient, KNOWN_NEXT_ACTION
from .protocol_session import ProtocolSession
from .token_harvester import BrowserTokenSession

ROOT = Path(__file__).resolve().parent.parent

LogFn = Callable[[str], None]
GetEmailFn = Callable[..., tuple]  # () -> (email, mail_token)
GetOtpFn = Callable[..., Optional[str]]  # (mail_token, email) -> code
BuildProfileFn = Callable[[], tuple]  # () -> (given, family, password)


def _default_log(msg: str) -> None:
    try:
        from DrissionPage_example import slog  # type: ignore

        slog("HYBRID", msg)
    except Exception:
        print(f"[HYBRID] {msg}", flush=True)


def resolve_register_mode(config: Optional[dict] = None) -> str:
    """Return 'browser' | 'hybrid'. Default browser.

    Priority: env GROK_REGISTER_MODE → config register_mode → browser
    """
    env = (os.environ.get("GROK_REGISTER_MODE") or "").strip().lower()
    if env in ("hybrid", "browser"):
        return env
    conf = config
    if conf is None:
        try:
            cfg_path = ROOT / "config.json"
            if cfg_path.is_file():
                conf = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            conf = {}
    conf = conf or {}
    mode = str(conf.get("register_mode") or "").strip().lower()
    if not mode:
        run = conf.get("run") if isinstance(conf.get("run"), dict) else {}
        mode = str(run.get("register_mode") or "").strip().lower()
    if mode in ("hybrid", "browser"):
        return mode
    return "browser"


def load_next_action_from_capture() -> str:
    """Optional offline capture of next-action hash under capture_out/rpc/."""
    rpc = ROOT / "capture_out" / "rpc"
    for name in ("03_SignUpSubmit.req.headers.json",):
        p = rpc / name
        if p.is_file():
            try:
                h = json.loads(p.read_text(encoding="utf-8"))
                return h.get("next-action") or h.get("Next-Action") or ""
            except Exception:
                pass
    if rpc.is_dir():
        for f in rpc.glob("*.req.headers.json"):
            try:
                h = json.loads(f.read_text(encoding="utf-8"))
                if h.get("next-action"):
                    return h["next-action"]
            except Exception:
                pass
    return ""


def register_one_hybrid(
    *,
    page: Any = None,
    browser: Any = None,
    log: Optional[LogFn] = None,
    proxy: str = "",
    user_agent: str = "",
    next_action: str = "",
    get_email: Optional[GetEmailFn] = None,
    get_otp: Optional[GetOtpFn] = None,
    build_profile: Optional[BuildProfileFn] = None,
    open_signup_fn: Optional[Callable[[], None]] = None,
    get_turnstile_fn: Optional[Callable[..., str]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[dict]:
    """Register one account via hybrid path.

    Returns result dict on SSO success::
        {
          email, password, given_name, family_name,
          sso, sso_token, sso_rw, apiKey, cookie_header,
          cloudflare_cookies, cookies, hybrid=True
        }
    Returns None on failure (caller should fall back to full browser path).
    """
    lg = log or _default_log
    stop = should_stop or (lambda: False)
    t0 = time.time()
    action = (next_action or load_next_action_from_capture() or "").strip()

    if get_email is None or get_otp is None or build_profile is None:
        # Lazy import host project helpers (avoid circular import at module load)
        try:
            from email_register import get_email_and_token, get_oai_code
            from DrissionPage_example import build_profile as _bp  # type: ignore

            get_email = get_email or get_email_and_token
            get_otp = get_otp or (
                lambda tok, em, **kw: get_oai_code(tok, em, timeout=120)
            )
            build_profile = build_profile or _bp
        except Exception as e:
            lg(f"[hybrid] missing email/profile helpers: {e}")
            return None

    # Proxy fallback
    if not proxy:
        proxy = (
            os.environ.get("GROK_BROWSER_PROXY")
            or os.environ.get("GROK_PROXY")
            or ""
        ).strip()
        if not proxy:
            try:
                from DrissionPage_example import current_proxy_url  # type: ignore

                proxy = current_proxy_url() or ""
            except Exception:
                pass

    try:
        tok_sess = BrowserTokenSession(
            page=page,
            browser=browser,
            open_signup_fn=open_signup_fn,
            get_turnstile_fn=get_turnstile_fn,
            log=lg,
        )
        if page is None:
            # Try host globals
            try:
                import DrissionPage_example as dpe  # type: ignore

                tok_sess._page = getattr(dpe, "page", None)
                tok_sess._browser = getattr(dpe, "browser", None)
                if open_signup_fn is None:
                    tok_sess._open_signup_fn = getattr(dpe, "open_signup_page", None)
                if get_turnstile_fn is None:
                    gtt = getattr(dpe, "getTurnstileToken", None)
                    if gtt:
                        tok_sess._get_turnstile_fn = lambda: gtt()
            except Exception:
                pass

        if tok_sess.page() is None:
            lg("[hybrid] no browser page available")
            return None

        if stop():
            return None

        lg("[hybrid] open signup…")
        tok_sess.open_signup()
        tok_sess.install_network_hook()
        action = action or tok_sess.scrape_next_action() or action

        email, mail_token = get_email()
        if not email:
            lg("[hybrid] no email")
            return None
        lg(f"[hybrid] email={email}")
        if stop():
            return None

        # Browser UI submit triggers native CreateEmail (passes CF). Capture castle.
        castle = tok_sess.harvest_castle_via_email_submit(email, timeout=45)
        browser_cookies = tok_sess.export_cookies()
        if not castle or len(castle) < 1000 or not str(castle).startswith("IBYIll"):
            # Accept long tokens even without IBYIll prefix (SDK may change)
            if not castle or len(castle) < 2000:
                lg(
                    f"[hybrid] bad castle len={len(castle or '')} "
                    f"head={(castle or '')[:24]}"
                )
                return None
            lg(f"[hybrid] castle non-IBYIll but long len={len(castle)}")

        ua = tok_sess.browser_user_agent() or user_agent or ""
        sess = ProtocolSession(
            proxy=(proxy or "").strip(),
            user_agent=ua,
            impersonate="chrome131",
        )
        jar = dict(browser_cookies or {})
        for stale in ("sso", "sso-rw"):
            jar.pop(stale, None)
        sess.set_cookies(jar)
        client = AuthManagementClient(sess)
        if action:
            client.next_action = action

        browser_sent = tok_sess.create_email_sent_via_browser()
        if browser_sent:
            lg(
                f"[hybrid] CreateEmail via browser OK (skip protocol) "
                f"castle_len={len(castle)}"
            )
        else:
            r1 = client.create_email_validation_code(email, castle)
            lg(
                f"[hybrid] CreateEmail status={r1['status']} "
                f"castle_len={len(castle)}"
            )
            if r1["status"] >= 400:
                body_hint = ""
                try:
                    raw = r1.get("raw") or b""
                    if b"cloudflare" in raw[:500].lower() or b"<!DOCTYPE" in raw[:200]:
                        body_hint = " (Cloudflare block)"
                except Exception:
                    pass
                lg(
                    f"[hybrid] CreateEmail fail{body_hint} "
                    f"strings={r1.get('strings')[:2]}"
                )
                return None
        if stop():
            return None

        code = get_otp(mail_token, email)
        clean = str(code or "").replace("-", "").strip()
        if not clean:
            lg("[hybrid] no mail code")
            return None
        lg(f"[hybrid] code={clean}")

        r2 = client.verify_email_validation_code(email, clean)
        lg(f"[hybrid] VerifyEmail status={r2['status']}")
        if r2["status"] >= 400:
            lg(f"[hybrid] VerifyEmail fail {r2.get('strings')[:5]}")
            return None
        if stop():
            return None

        given, family, password = build_profile()
        try:
            client.validate_password(email, password)
        except Exception:
            pass

        turnstile = tok_sess.get_turnstile_token(timeout=90, inject=True)
        if len(turnstile) < 80:
            lg(f"[hybrid] turnstile short len={len(turnstile)}")
            return None

        castle2 = tok_sess.read_captured_castle() or castle
        if len(castle2) < 1000:
            castle2 = castle
        browser_cookies = tok_sess.export_cookies()
        jar2 = dict(browser_cookies or {})
        for stale in ("sso", "sso-rw"):
            jar2.pop(stale, None)
        sess.set_cookies(jar2)
        action = (
            action
            or tok_sess.scrape_next_action()
            or load_next_action_from_capture()
        )
        if not action:
            client.next_action = ""
            action = client.discover_next_action(timeout=60)
        known = KNOWN_NEXT_ACTION
        if not action:
            action = known
            lg(f"[hybrid] next-action fallback={action[:16]}...")
        elif action != known:
            lg(
                f"[hybrid] next-action discovered={action[:20]}... "
                f"known={known[:16]}..."
            )
        else:
            lg(f"[hybrid] next-action={action[:20]}...")
        client.next_action = action
        if stop():
            return None

        def _do_signup(act: str):
            return client.create_user_via_server_action(
                email=email,
                code=clean,
                given_name=given,
                family_name=family,
                password=password,
                turnstile_token=turnstile,
                castle_token=castle2,
                next_action=act,
                conversion_id=str(uuid.uuid4()),
            )

        r3 = _do_signup(action)
        sso = r3.get("sso") or ""
        if not sso:
            sso = (
                (r3.get("cookies") or {}).get("sso")
                or (r3.get("cookies") or {}).get("sso-rw")
                or ""
            )
        body_txt = str(r3.get("text") or "")
        if (not sso) and action != known and (
            "isLoggedInWithSSO" in body_txt or r3.get("status") == 200
        ):
            lg(f"[hybrid] retry sign-up with known next-action={known[:16]}...")
            jar3 = dict(tok_sess.export_cookies() or {})
            for stale in ("sso", "sso-rw"):
                jar3.pop(stale, None)
            sess.set_cookies(jar3)
            r3 = _do_signup(known)
            sso = r3.get("sso") or ""
            if not sso:
                sso = (
                    (r3.get("cookies") or {}).get("sso")
                    or (r3.get("cookies") or {}).get("sso-rw")
                    or ""
                )
            body_txt = str(r3.get("text") or "")

        lg(
            f"[hybrid] sign-up status={r3['status']} sso_len={len(sso)} "
            f"elapsed={time.time() - t0:.1f}s"
        )
        if not sso:
            lg(
                f"[hybrid] no sso cookies={list((r3.get('cookies') or {}).keys())[:12]} "
                f"body={body_txt[:240]}"
            )
            return None

        # Materialize wrapper JWT → real session sso when needed
        try:
            from .sso_util import (
                is_session_sso,
                is_wrapper_sso,
                materialize_sso_via_browser,
                materialize_sso_via_http,
                inject_sso_into_page,
            )

            if is_wrapper_sso(sso) or not is_session_sso(sso):
                lg(f"[hybrid] sso looks like wrapper len={len(sso)}; materialize…")
                p = tok_sess.page()
                sess_sso = ""
                if p is not None:
                    sess_sso = materialize_sso_via_browser(
                        p, sso, log=lg, timeout=40
                    )
                if not sess_sso or not is_session_sso(sess_sso):
                    jar = dict(tok_sess.export_cookies() or {})
                    sess_sso = (
                        materialize_sso_via_http(
                            sso,
                            proxy=(proxy or "").strip(),
                            extra_cookies=jar,
                            log=lg,
                        )
                        or sess_sso
                    )
                if sess_sso and is_session_sso(sess_sso):
                    lg(f"[hybrid] session sso ready len={len(sess_sso)}")
                    sso = sess_sso
                else:
                    lg(
                        f"[hybrid] WARN still wrapper/non-session sso "
                        f"len={len(sso)}"
                    )
            # Inject into browser for downstream PKCE / convert
            p = tok_sess.page()
            if p is not None and sso:
                inject_sso_into_page(p, sso)
                try:
                    # Also set via Playwright context if available
                    raw = getattr(p, "raw", None) or getattr(p, "_page", None)
                    ctx = None
                    if raw is not None:
                        ctx = getattr(raw, "context", None)
                    if ctx is not None and hasattr(ctx, "add_cookies"):
                        cookies_add = [
                            {
                                "name": "sso",
                                "value": sso,
                                "domain": ".x.ai",
                                "path": "/",
                            },
                            {
                                "name": "sso-rw",
                                "value": sso,
                                "domain": ".x.ai",
                                "path": "/",
                            },
                        ]
                        if hasattr(tok_sess, "_async") is False:
                            try:
                                # sync
                                if getattr(p, "_async", False) and hasattr(p, "_run"):
                                    p._run(ctx.add_cookies(cookies_add))
                                else:
                                    ctx.add_cookies(cookies_add)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception as e:
            lg(f"[hybrid] sso materialize: {e}")

        # Build CF cookie string
        full_jar = dict(tok_sess.export_cookies() or {})
        full_jar["sso"] = sso
        full_jar["sso-rw"] = full_jar.get("sso-rw") or sso
        # Merge protocol response cookies
        for k, v in (r3.get("cookies") or {}).items():
            if k and v and k not in full_jar:
                full_jar[k] = v

        cf_parts = []
        for k, v in full_jar.items():
            kl = str(k).lower()
            if kl.startswith("cf_") or kl in ("__cf_bm", "cf_clearance"):
                cf_parts.append(f"{k}={v}")

        sso_rw = full_jar.get("sso-rw") or sso
        api_key = f"sso={sso}; sso-rw={sso_rw}"
        result = {
            "email": email,
            "password": password,
            "given_name": given,
            "family_name": family,
            "sso": sso,
            "sso_token": sso,
            "sso_rw": sso_rw,
            "apiKey": api_key,
            "cookie_header": api_key,
            "cloudflare_cookies": "; ".join(cf_parts),
            "cookies": full_jar,
            "providerSpecificData": {
                "cloudflareCookies": "; ".join(cf_parts),
            },
            "hybrid": True,
        }
        lg(f"[hybrid][+] OK {email}  elapsed={time.time() - t0:.1f}s")
        return result

    except Exception as e:
        lg(f"[hybrid] exception: {e}")
        try:
            lg(traceback.format_exc().splitlines()[-3])
        except Exception:
            pass
        return None

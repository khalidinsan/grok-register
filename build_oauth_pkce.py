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
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_AUTHORIZE = "https://auth.x.ai/oauth2/authorize"
XAI_TOKEN = "https://auth.x.ai/oauth2/token"
XAI_REDIRECT_URI = "http://127.0.0.1:56121/callback"
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
    try:
        urls.append(str(getattr(raw, "url", "") or ""))
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


def _click_allow_hard(page: Any, log: Optional[Callable[[str], None]] = None) -> bool:
    """
    Aggressively click Allow / Authorize on consent (flash click_text_button + JS).
    Must run on raw Playwright page via async loop for Camoufox.
    """
    raw, loop, is_async = _unwrap_page(page)

    def _log(m: str) -> None:
        if log:
            log(m)

    # 1) exact role Allow / Authorize (async Camoufox)
    if hasattr(raw, "get_by_role") and is_async and loop is not None:
        for kw in ("Allow", "Authorize", "Approve", "Accept"):
            try:
                async def _click_kw(k=kw):
                    loc = raw.get_by_role(
                        "button", name=re.compile(rf"^{re.escape(k)}$", re.I)
                    )
                    n = await loc.count()
                    if n <= 0:
                        loc = raw.get_by_role("button", name=re.compile(k, re.I))
                        n = await loc.count()
                    if n <= 0:
                        return False
                    btn = loc.first
                    try:
                        await btn.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    # try normal then force
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

    # 2) JS exact match first (most reliable for React consent)
    if hasattr(raw, "evaluate"):
        try:
            clicked = _run(
                loop,
                is_async,
                raw.evaluate(
                    """() => {
                        const prefer = ['allow', 'authorize', 'approve', 'accept'];
                        const nodes = [...document.querySelectorAll(
                            'button, [role="button"], input[type="submit"], a'
                        )];
                        // pass 1: exact
                        for (const want of prefer) {
                          for (const b of nodes) {
                            const txt = (b.innerText || b.textContent || b.value || '')
                              .replace(/\\s+/g, ' ').trim().toLowerCase();
                            if (txt !== want) continue;
                            const r = b.getBoundingClientRect();
                            if (r.width <= 0 || r.height <= 0) continue;
                            if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                            b.click();
                            return txt;
                          }
                        }
                        // pass 2: includes (but not deny/cancel)
                        for (const b of nodes) {
                          const txt = (b.innerText || b.textContent || b.value || '')
                            .replace(/\\s+/g, ' ').trim().toLowerCase();
                          if (!txt || /deny|cancel|sign out|go back|google|apple/.test(txt)) continue;
                          if (!prefer.some(w => txt === w || txt.includes(w))) continue;
                          const r = b.getBoundingClientRect();
                          if (r.width <= 0 || r.height <= 0) continue;
                          b.click();
                          return txt;
                        }
                        return '';
                    }"""
                ),
            )
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
                if loc.count() > 0:
                    loc.click(timeout=3000, force=True)
                    _log(f"clicked Allow sync role({kw!r})")
                    return True
            except Exception:
                continue
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

    # Install route BEFORE goto
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
            _log("OAuth callback route installed (127.0.0.1:56121)")
        except Exception as e:
            _log(f"route setup warn: {e}")

    _page_goto(page, auth_url)
    time.sleep(0.8)  # let consent paint

    t0 = time.time()
    last = ""
    allow_attempts = 0
    last_allow_try = 0.0

    while time.time() - t0 < timeout_sec:
        if captured.get("code"):
            break

        code = _capture_code_from_pages(page)
        if code:
            captured["code"] = code
            _log("OAuth code from page URL (localhost callback)")
            break

        cur = _page_url(page)
        if cur != last:
            _log(f"OAuth page (+{time.time() - t0:.0f}s): {cur[:140]}")
            last = cur

        on_consent = "/oauth2/consent" in cur or "auth.x.ai" in cur
        if try_consent_click and on_consent:
            # Retry Allow every ~1.2s until redirect (do NOT stop after one silent fail)
            if time.time() - last_allow_try >= 1.2:
                last_allow_try = time.time()
                allow_attempts += 1
                # Optional identity confirm once early
                if allow_attempts == 1:
                    if _click_allow_hard.__doc__:
                        pass
                    # identity buttons (Confirm) — only if present, via same JS path
                    try:
                        raw2, loop2, asy2 = _unwrap_page(page)
                        conf = _run(
                            loop2,
                            asy2,
                            raw2.evaluate(
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
                                }"""
                            ),
                        )
                        if conf:
                            _log(f"OAuth identity: {conf}")
                            time.sleep(0.5)
                    except Exception:
                        pass

                ok = _click_allow_hard(page, log=_log)
                if ok:
                    _log(f"OAuth Allow attempt #{allow_attempts} OK — wait callback…")
                    # wait for localhost redirect
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
                            c2 = _capture_code_from_pages(page) or captured.get("code")
                            if c2:
                                captured["code"] = c2
                                break
                    except Exception:
                        time.sleep(0.4)
                else:
                    _log(
                        f"OAuth Allow attempt #{allow_attempts} MISS "
                        f"(no button found / click failed)"
                    )

        time.sleep(0.35)

    if not captured.get("code"):
        captured["code"] = _capture_code_from_pages(page)

    # cleanup routes
    try:
        raw, loop, is_async = _unwrap_page(page)
        if hasattr(raw, "unroute"):
            if is_async and loop is not None:
                loop.run_until_complete(raw.unroute("http://127.0.0.1:56121/**"))
                loop.run_until_complete(raw.unroute("http://localhost:56121/**"))
            else:
                raw.unroute("http://127.0.0.1:56121/**")
                raw.unroute("http://localhost:56121/**")
    except Exception:
        pass

    code = captured.get("code")
    if not code:
        hint = ""
        try:
            raw, loop, is_async = _unwrap_page(page)
            if hasattr(raw, "evaluate"):
                hint = str(
                    _run(
                        loop,
                        is_async,
                        raw.evaluate(
                            """() => (document.body && document.body.innerText || '')
                                .replace(/\\s+/g, ' ').trim().slice(0, 160)"""
                        ),
                    )
                    or ""
                )
        except Exception:
            pass
        raise RuntimeError(
            f"OAuth code not captured in {timeout_sec:.0f}s "
            f"(allow_attempts={allow_attempts} last_url={_page_url(page)[:140]!r} "
            f"hint={hint[:100]!r}). Need successful Allow click → redirect 127.0.0.1:56121."
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

"""
Push Grok Build OAuth tokens into 9router provider `grok-cli` via HTTP API.

Auth (same pattern as Gsuiteto9router/bot.js):
  1. POST {base_url}/api/auth/login  { password }
  2. Capture Set-Cookie auth_token
  3. POST {base_url}/api/providers with Cookie: auth_token=...

Works with external URLs e.g. https://ai.khalid.id

Fallback: x-9r-cli-token from local ~/.9router (localhost only usually).

Bot-flag gate:
  JWT bot_flag_source alone is NOT a hard ban (many flagged tokens still chat OK).
  When the access token is bot-flagged, we smoke-test cli-chat-proxy with model
  grok-4.5; import only if the probe succeeds (HTTP 2xx + response stream).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import requests

try:
    from sso_to_build import BuildTokens
except Exception:  # pragma: no cover
    from build_oauth_pkce import BuildTokens  # type: ignore

DEFAULT_BASE = "http://127.0.0.1:20127"

# Official Grok CLI / Grok Build chat proxy (OpenAI Responses API)
GROK_CLI_CHAT_URL = "https://cli-chat-proxy.grok.com/v1/responses"
GROK_CLI_VERSION = "0.2.99"
GROK_CLI_USER_AGENT = f"grok-shell/{GROK_CLI_VERSION} (linux; x86_64)"
GROK_CLI_CLIENT_IDENTIFIER = "grok-shell"
DEFAULT_SMOKE_MODEL = "grok-4.5"
DEFAULT_SMOKE_TIMEOUT = 45

# Cache login cookie per base_url so multi-account farm doesn't re-login each time
_session_cache: Dict[str, str] = {}


def _compute_cli_token(data_dir: Optional[str] = None) -> Optional[str]:
    base = Path(os.path.expanduser(data_dir or "~/.9router"))
    mid = base / "machine-id"
    secret = base / "auth" / "cli-secret"
    if not mid.is_file() or not secret.is_file():
        return None
    raw = mid.read_text(encoding="utf-8").strip()
    sec = secret.read_text(encoding="utf-8").strip()
    if not raw or not sec:
        return None
    return hashlib.sha256(f"{raw}9r-cli-auth{sec}".encode()).hexdigest()[:16]


def _login_dashboard(base_url: str, password: str, timeout: int = 30) -> str:
    """
    Login like Gsuiteto9router/bot.js → return Cookie header value auth_token=...
    """
    base = base_url.rstrip("/")
    if base in _session_cache:
        return _session_cache[base]

    url = f"{base}/api/auth/login"
    print(f"[*] 9router login {url} ...")
    resp = requests.post(
        url,
        json={"password": password},
        headers={"Content-Type": "application/json"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"9router login HTTP {resp.status_code}: {resp.text[:300]}"
        )
    try:
        data = resp.json()
    except Exception:
        data = {}
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(f"9router login failed: {data}")

    # Prefer Set-Cookie auth_token=
    cookie_header = None
    for c in resp.cookies:
        if c.name == "auth_token" and c.value:
            cookie_header = f"auth_token={c.value}"
            break
    if not cookie_header:
        # requests may also expose in headers
        raw = resp.headers.get("Set-Cookie") or ""
        if "auth_token=" in raw:
            part = raw.split("auth_token=", 1)[1].split(";", 1)[0].strip()
            if part:
                cookie_header = f"auth_token={part}"

    if not cookie_header:
        raise RuntimeError(
            "9router login OK but auth_token cookie missing "
            "(check Set-Cookie / SameSite on remote)"
        )

    _session_cache[base] = cookie_header
    print("[*] 9router login OK (session cookie cached)")
    return cookie_header


def probe_status_to_9router_flags(probe_status: int, probe_err: str = "") -> Dict[str, Any]:
    """Map chat probe HTTP status → 9router fields that re-probe candidates expect.

    isGrokCliReprobeCandidate needs testStatus in:
      quota_exhausted | permission_denied | unavailable
    and/or PSD quotaExhausted / permissionDenied — NOT plain "inactive".
    """
    st = int(probe_status or 0)
    err = (probe_err or "").strip()
    err_l = err.lower()
    if st == 402 or "402" in err_l or "quota" in err_l or "payment" in err_l:
        return {
            "test_status": "quota_exhausted",
            "error_code": 402,
            "last_error_type": "quota_exhausted",
            "psd": {
                "quotaExhausted": True,
                "permissionDenied": False,
                "farmInjectOff": True,
                "farmProbeStatus": st or 402,
            },
        }
    if st == 403 or "403" in err_l or "permission" in err_l or "denied" in err_l:
        return {
            "test_status": "permission_denied",
            "error_code": 403,
            "last_error_type": "permission_denied",
            "psd": {
                "quotaExhausted": False,
                "permissionDenied": True,
                "farmInjectOff": True,
                "farmProbeStatus": st or 403,
            },
        }
    # other soft-fail (network 0, 429, 5xx, …) still OFF but re-probe-able
    return {
        "test_status": "unavailable",
        "error_code": st or None,
        "last_error_type": "unavailable",
        "psd": {
            "farmInjectOff": True,
            "farmProbeStatus": st,
        },
    }


def _build_payload(
    tokens: BuildTokens,
    *,
    name: str = "",
    email: str = "",
    display_name: str = "",
    is_active: bool = True,
    test_status: str = "",
    last_error: str = "",
    error_code: Any = None,
    last_error_type: str = "",
    provider_specific_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    email = (email or tokens.email or "").strip()
    name = (name or tokens.name or email or tokens.user_id or "Grok CLI").strip()
    display_name = (display_name or "").strip()
    if not display_name and tokens.id_token:
        try:
            import base64

            parts = tokens.id_token.split(".")
            pad = parts[1] + "=" * (-len(parts[1]) % 4)
            claims = json.loads(base64.urlsafe_b64decode(pad))
            given = (claims.get("given_name") or "").strip()
            family = (claims.get("family_name") or "").strip()
            display_name = f"{given} {family}".strip()
        except Exception:
            pass
    if not display_name:
        display_name = name

    payload: Dict[str, Any] = {
        "provider": "grok-cli",
        "accessToken": tokens.access_token,
        "refreshToken": tokens.refresh_token or None,
        "idToken": tokens.id_token or None,
        "expiresIn": tokens.expires_in,
        "expiresAt": tokens.expires_at,
        "scope": tokens.scope,
        "email": email or tokens.email or None,
        "name": name,
        "displayName": display_name,
        "userId": tokens.user_id or None,
        "isActive": bool(is_active),
    }
    if test_status:
        payload["testStatus"] = str(test_status)
    elif not is_active:
        # Prefer re-probe-friendly status over plain "inactive" (manual-off lookalike)
        payload["testStatus"] = "unavailable"
    if last_error:
        payload["lastError"] = str(last_error)[:500]
    if error_code is not None:
        try:
            payload["errorCode"] = int(error_code)
        except (TypeError, ValueError):
            pass
    if last_error_type:
        payload["lastErrorType"] = str(last_error_type)
    if provider_specific_data:
        payload["providerSpecificData"] = dict(provider_specific_data)
    return payload


def _jwt_payload(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    try:
        import base64

        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


def access_token_bot_flagged(access_token: str) -> bool:
    """True if xAI stamped bot_flag_source on Build/CLI access token.

    Env GROK_IMPORT_ALLOW_BOT_FLAG=1 disables the flag check entirely
    (import without smoke; for debugging only).
    """
    if os.environ.get("GROK_IMPORT_ALLOW_BOT_FLAG", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    claims = _jwt_payload(access_token)
    flag = claims.get("bot_flag_source")
    if flag is None or flag is False or flag == 0 or flag == "0":
        return False
    return True


def bot_flag_source_value(access_token: str) -> Any:
    claims = _jwt_payload(access_token)
    return claims.get("bot_flag_source")


def smoke_test_grok_cli(
    access_token: str,
    *,
    model: str = DEFAULT_SMOKE_MODEL,
    email: str = "",
    user_id: str = "",
    timeout: float = DEFAULT_SMOKE_TIMEOUT,
    proxy: str = "",
) -> Dict[str, Any]:
    """
    Live probe against cli-chat-proxy.grok.com /v1/responses (Grok Build).

    Returns dict:
      ok: bool
      status: int HTTP status (0 on network error)
      model: str
      detail: short reason / body snippet
      latency_ms: int
    """
    import time

    model = (model or DEFAULT_SMOKE_MODEL).strip() or DEFAULT_SMOKE_MODEL
    sid = str(uuid.uuid4())
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": GROK_CLI_USER_AGENT,
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-identifier": GROK_CLI_CLIENT_IDENTIFIER,
        "x-grok-client-version": GROK_CLI_VERSION,
        "x-grok-client-mode": "headless",
        "x-grok-session-id": sid,
        "x-grok-conv-id": sid,
        "x-grok-req-id": str(uuid.uuid4()),
        "x-grok-turn-idx": "1",
        "x-grok-model-override": model,
    }
    email_s = (email or "").strip()
    user_s = (user_id or "").strip()
    if not user_s:
        claims = _jwt_payload(access_token)
        user_s = str(
            claims.get("principal_id") or claims.get("sub") or ""
        ).strip()
    if email_s:
        headers["x-email"] = email_s
    if user_s:
        headers["x-userid"] = user_s

    # Minimal Responses payload (stream required by cli-chat-proxy forceStream)
    body = {
        "model": model,
        "stream": True,
        "store": False,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": "Reply with exactly: ok",
            }
        ],
        "max_output_tokens": 16,
        "reasoning": {"effort": "low", "summary": "concise"},
    }

    req_proxies = None
    proxy_s = (proxy or os.environ.get("GROK_BROWSER_PROXY") or "").strip()
    if proxy_s:
        req_proxies = {"http": proxy_s, "https": proxy_s}

    t0 = time.monotonic()
    try:
        resp = requests.post(
            GROK_CLI_CHAT_URL,
            headers=headers,
            json=body,
            timeout=timeout,
            stream=True,
            proxies=req_proxies,
        )
    except requests.RequestException as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": False,
            "status": 0,
            "model": model,
            "detail": f"network error: {e}",
            "latency_ms": latency_ms,
        }

    status = int(resp.status_code)
    # Read a small prefix of the SSE stream (enough to confirm acceptance)
    head = ""
    try:
        # status already known; for non-2xx read text body; for 2xx read first chunk
        if status >= 400:
            head = (resp.text or "")[:400]
        else:
            chunks: list[str] = []
            total = 0
            for raw in resp.iter_content(chunk_size=512, decode_unicode=True):
                if not raw:
                    continue
                piece = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                chunks.append(piece)
                total += len(piece)
                if total >= 400:
                    break
            head = "".join(chunks)[:400]
    except Exception as e:
        head = f"read error: {e}"
    finally:
        try:
            resp.close()
        except Exception:
            pass

    latency_ms = int((time.monotonic() - t0) * 1000)

    if status == 403:
        return {
            "ok": False,
            "status": status,
            "model": model,
            "detail": f"403 permission-denied: {head[:200]}",
            "latency_ms": latency_ms,
        }
    if status == 401:
        return {
            "ok": False,
            "status": status,
            "model": model,
            "detail": f"401 unauthorized: {head[:200]}",
            "latency_ms": latency_ms,
        }
    if status == 402:
        return {
            "ok": False,
            "status": status,
            "model": model,
            "detail": f"402 quota/payment: {head[:200]}",
            "latency_ms": latency_ms,
        }
    if status < 200 or status >= 300:
        return {
            "ok": False,
            "status": status,
            "model": model,
            "detail": f"HTTP {status}: {head[:200]}",
            "latency_ms": latency_ms,
        }

    # 2xx: expect Responses SSE (response.created / output_text / etc.)
    head_l = head.lower()
    looks_ok = (
        "response.created" in head_l
        or "response.output" in head_l
        or "output_text" in head_l
        or '"type":"response' in head_l
        or "data:" in head_l
    )
    if not looks_ok and not head.strip():
        return {
            "ok": False,
            "status": status,
            "model": model,
            "detail": "empty stream body",
            "latency_ms": latency_ms,
        }
    if not looks_ok:
        # Still 2xx — treat as soft-pass but note odd body
        return {
            "ok": True,
            "status": status,
            "model": model,
            "detail": f"2xx but unexpected body: {head[:120]}",
            "latency_ms": latency_ms,
        }
    return {
        "ok": True,
        "status": status,
        "model": model,
        "detail": f"ok ({head.splitlines()[0][:80] if head else 'stream'})",
        "latency_ms": latency_ms,
    }


def push_build_tokens_to_9router(
    tokens: BuildTokens,
    *,
    base_url: str = DEFAULT_BASE,
    data_dir: Optional[str] = None,
    cli_token: Optional[str] = None,
    password: Optional[str] = None,
    name: str = "",
    email: str = "",
    display_name: str = "",
    smoke_bot_flag: bool = True,
    smoke_model: str = DEFAULT_SMOKE_MODEL,
    smoke_timeout_sec: float = DEFAULT_SMOKE_TIMEOUT,
    is_active: bool = True,
    test_status: str = "",
    last_error: str = "",
    error_code: Any = None,
    last_error_type: str = "",
    provider_specific_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create grok-cli connection via 9router HTTP API.

    Auth priority:
      1. password (dashboard login → Cookie)  — works for external URLs
      2. cli_token / local ~/.9router CLI token — mainly localhost

    is_active=False → import with connection OFF (dashboard inactive). Used when
    chat probe is 402/403 but OAuth tokens are still valid.

    Bot-flag policy (default):
      - no bot_flag_source → import directly
      - bot_flag_source set → smoke-test model (default grok-4.5) on cli-chat-proxy;
        import only if probe succeeds. Failed smoke → skip import (raise).
      - GROK_IMPORT_ALLOW_BOT_FLAG=1 → skip flag check entirely
      - GROK_IMPORT_SKIP_SMOKE=1 → import bot-flagged without probe (debug)
      - smoke_bot_flag=False → same as skip smoke for flagged tokens
    """
    base = (base_url or DEFAULT_BASE).rstrip("/")
    payload = _build_payload(
        tokens,
        name=name,
        email=email,
        display_name=display_name,
        is_active=is_active,
        test_status=test_status,
        last_error=last_error,
        error_code=error_code,
        last_error_type=last_error_type,
        provider_specific_data=provider_specific_data,
    )
    email_s = payload.get("email") or ""
    name_s = payload.get("name") or ""
    access = getattr(tokens, "access_token", "") or ""
    smoke_info: Optional[Dict[str, Any]] = None

    # bot_flag appears only after OAuth convert (not on Web SSO cookie)
    if access_token_bot_flagged(access):
        flag = bot_flag_source_value(access)
        skip_smoke = (
            not smoke_bot_flag
            or os.environ.get("GROK_IMPORT_SKIP_SMOKE", "").strip().lower()
            in ("1", "true", "yes")
        )
        if skip_smoke:
            print(
                f"[*] bot_flag_source={flag} email={email_s or '?'} — "
                f"smoke disabled, importing anyway",
                flush=True,
            )
        else:
            model = (
                (smoke_model or "").strip()
                or os.environ.get("GROK_SMOKE_MODEL", "").strip()
                or DEFAULT_SMOKE_MODEL
            )
            print(
                f"[*] bot_flag_source={flag} email={email_s or '?'} — "
                f"smoke test model={model} ...",
                flush=True,
            )
            smoke_info = smoke_test_grok_cli(
                access,
                model=model,
                email=email_s or getattr(tokens, "email", "") or "",
                user_id=str(getattr(tokens, "user_id", "") or ""),
                timeout=float(smoke_timeout_sec or DEFAULT_SMOKE_TIMEOUT),
            )
            if smoke_info.get("ok"):
                print(
                    f"[*] smoke PASS model={smoke_info.get('model')} "
                    f"status={smoke_info.get('status')} "
                    f"latency_ms={smoke_info.get('latency_ms')} "
                    f"detail={smoke_info.get('detail')}",
                    flush=True,
                )
            else:
                msg = (
                    f"skip import: bot_flag_source={flag} email={email_s or '?'} "
                    f"smoke FAIL model={smoke_info.get('model')} "
                    f"status={smoke_info.get('status')} "
                    f"latency_ms={smoke_info.get('latency_ms')} "
                    f"detail={smoke_info.get('detail')} "
                    f"(set GROK_IMPORT_ALLOW_BOT_FLAG=1 to force, or "
                    f"GROK_IMPORT_SKIP_SMOKE=1 to import without probe)"
                )
                print(f"[*] {msg}", flush=True)
                raise RuntimeError(msg)

    headers: Dict[str, str] = {"Content-Type": "application/json"}
    auth_mode = "none"

    pwd = (password or os.environ.get("NINEROUTER_PASSWORD") or "").strip()
    if pwd:
        cookie = _login_dashboard(base, pwd)
        headers["Cookie"] = cookie
        auth_mode = "password-cookie"
    else:
        token = (cli_token or "").strip() or _compute_cli_token(data_dir)
        if token:
            headers["x-9r-cli-token"] = token
            auth_mode = "cli-token"
        else:
            raise RuntimeError(
                "No 9router auth: set grok_cli.password (dashboard password) "
                "for remote URL, or ensure ~/.9router CLI token for localhost."
            )

    url = f"{base}/api/providers"
    print(
        f"[*] 9router POST {url} auth={auth_mode} "
        f"name={name_s} email={email_s} isActive={bool(is_active)}"
    )
    resp = requests.post(url, headers=headers, json=payload, timeout=45)

    # Session expired → re-login once if password mode
    if resp.status_code == 401 and pwd:
        print("[*] 9router 401 — re-login and retry...")
        _session_cache.pop(base, None)
        cookie = _login_dashboard(base, pwd)
        headers["Cookie"] = cookie
        resp = requests.post(url, headers=headers, json=payload, timeout=45)

    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"9router grok-cli import HTTP {resp.status_code}: {resp.text[:400]}"
        )

    data = resp.json() if resp.content else {}
    conn = data.get("connection") or {}
    conn_id = conn.get("id") or ""

    # Fallback: older 9router may ignore isActive/testStatus on create — PATCH
    need_patch = bool(conn_id) and (
        (not is_active and conn.get("isActive") is not False)
        or (
            not is_active
            and test_status
            and str(conn.get("testStatus") or "") != str(test_status)
        )
    )
    if need_patch:
        try:
            patch_url = f"{base}/api/providers/{conn_id}"
            patch_body: Dict[str, Any] = {
                "isActive": False,
                # re-probe candidates need quota_exhausted | permission_denied | unavailable
                "testStatus": test_status or "unavailable",
            }
            if last_error:
                patch_body["lastError"] = str(last_error)[:500]
            if error_code is not None:
                try:
                    patch_body["errorCode"] = int(error_code)
                except (TypeError, ValueError):
                    pass
            if provider_specific_data:
                patch_body["providerSpecificData"] = dict(provider_specific_data)
            pr = requests.patch(patch_url, headers=headers, json=patch_body, timeout=30)
            if pr.status_code in (200, 201):
                print(f"[*] 9router PATCH isActive=false id={conn_id}", flush=True)
                try:
                    pdata = pr.json() if pr.content else {}
                    if isinstance(pdata, dict) and pdata.get("connection"):
                        conn = pdata["connection"]
                except Exception:
                    conn["isActive"] = False
            else:
                print(
                    f"[*] 9router PATCH isActive warn HTTP {pr.status_code}: "
                    f"{(pr.text or '')[:200]}",
                    flush=True,
                )
        except Exception as e:
            print(f"[*] 9router PATCH isActive warn: {e}", flush=True)

    active_s = "on" if (conn.get("isActive") is not False and is_active) else "off"
    print(
        f"[*] 9router grok-cli imported "
        f"id={conn_id} name={conn.get('name') or name_s} "
        f"email={conn.get('email') or email_s} active={active_s}"
    )
    out: Dict[str, Any] = {
        "id": conn_id,
        "name": conn.get("name") or name_s,
        "email": conn.get("email") or email_s,
        "isActive": bool(is_active) if conn.get("isActive") is None else bool(conn.get("isActive")),
        "raw": data,
        "auth_mode": auth_mode,
    }
    if smoke_info is not None:
        out["smoke"] = smoke_info
    return out


def clear_session_cache() -> None:
    _session_cache.clear()

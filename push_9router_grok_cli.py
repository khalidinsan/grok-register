"""
Push Grok Build OAuth tokens into 9router provider `grok-cli` via HTTP API.

Auth (same pattern as Gsuiteto9router/bot.js):
  1. POST {base_url}/api/auth/login  { password }
  2. Capture Set-Cookie auth_token
  3. POST {base_url}/api/providers with Cookie: auth_token=...

Works with external URLs e.g. https://ai.khalid.id

Fallback: x-9r-cli-token from local ~/.9router (localhost only usually).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from sso_to_build import BuildTokens

DEFAULT_BASE = "http://127.0.0.1:20127"

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


def _build_payload(
    tokens: BuildTokens,
    *,
    name: str = "",
    email: str = "",
    display_name: str = "",
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

    return {
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
) -> Dict[str, Any]:
    """
    Create grok-cli connection via 9router HTTP API.

    Auth priority:
      1. password (dashboard login → Cookie)  — works for external URLs
      2. cli_token / local ~/.9router CLI token — mainly localhost
    """
    base = (base_url or DEFAULT_BASE).rstrip("/")
    payload = _build_payload(
        tokens, name=name, email=email, display_name=display_name
    )
    email_s = payload.get("email") or ""
    name_s = payload.get("name") or ""

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
        f"name={name_s} email={email_s}"
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
    print(
        f"[*] 9router grok-cli imported "
        f"id={conn_id} name={conn.get('name') or name_s} email={conn.get('email') or email_s}"
    )
    return {
        "id": conn_id,
        "name": conn.get("name") or name_s,
        "email": conn.get("email") or email_s,
        "raw": data,
        "auth_mode": auth_mode,
    }


def clear_session_cache() -> None:
    _session_cache.clear()

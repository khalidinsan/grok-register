"""
Convert Grok Web SSO cookie → Grok Build / CLI OAuth tokens via Device Flow.

Port of grok2api backend/internal/infra/provider/web/sso_build.go (HTTP-only, no browser).

Flow:
  1. Validate SSO session on accounts.x.ai
  2. Start device code (client_id = Grok CLI / Build)
  3. Open verification URI + verify user_code + approve (with sso cookie)
  4. Poll token endpoint → access_token + refresh_token
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)
ACCOUNTS_URL = "https://accounts.x.ai/"
DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
TOKEN_URL = "https://auth.x.ai/oauth2/token"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


@dataclass
class BuildTokens:
    access_token: str
    refresh_token: str
    id_token: str
    expires_at: str
    expires_in: int
    email: str
    user_id: str
    team_id: str
    name: str
    scope: str = SCOPE


def normalize_sso_token(value: str) -> str:
    value = (value or "").strip()
    if value.lower().startswith("sso="):
        value = value[4:].strip()
    if ";" in value and "sso=" not in value.lower():
        value = value.split(";", 1)[0].strip()
    return value.replace("\r", "").replace("\n", "").replace("\x00", "").strip()


def extract_sso_from_credential(raw: Any) -> Tuple[str, str]:
    """Return (sso, sso_rw) from string or dict credential."""
    sso, sso_rw = "", ""
    if isinstance(raw, dict):
        header = str(raw.get("apiKey") or raw.get("cookie_header") or "")
        sso = str(raw.get("sso_token") or raw.get("sso") or "").strip()
        sso_rw = str(raw.get("sso_rw") or raw.get("sso-rw") or "").strip()
        if header:
            for part in header.split(";"):
                part = part.strip()
                low = part.lower()
                if low.startswith("sso=") and not low.startswith("sso-rw="):
                    sso = part.split("=", 1)[1].strip()
                elif low.startswith("sso-rw="):
                    sso_rw = part.split("=", 1)[1].strip()
        if not sso and raw.get("token"):
            sso = normalize_sso_token(str(raw.get("token")))
    else:
        text = str(raw or "")
        if "sso=" in text.lower() or "sso-rw=" in text.lower():
            for part in text.split(";"):
                part = part.strip()
                low = part.lower()
                if low.startswith("sso=") and not low.startswith("sso-rw="):
                    sso = part.split("=", 1)[1].strip()
                elif low.startswith("sso-rw="):
                    sso_rw = part.split("=", 1)[1].strip()
        else:
            sso = normalize_sso_token(text)
    sso = normalize_sso_token(sso)
    sso_rw = (sso_rw or sso).strip()
    return sso, sso_rw


def _safe_xai_url(raw: str) -> bool:
    try:
        p = urlparse(raw)
    except Exception:
        return False
    if p.scheme != "https" or p.username or p.password or not p.hostname:
        return False
    host = p.hostname.lower()
    return host == "x.ai" or host.endswith(".x.ai")


def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


class SsoBuildFlow:
    def __init__(self, sso: str, sso_rw: str | None = None, user_agent: str = USER_AGENT):
        self.user_agent = user_agent
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": user_agent,
            }
        )
        self.session.cookies.set("sso", sso, domain=".x.ai", path="/")
        self.session.cookies.set("sso-rw", sso_rw or sso, domain=".x.ai", path="/")

    def do(
        self,
        method: str,
        url: str,
        form: Optional[Dict[str, str]] = None,
        timeout: float = 30,
    ) -> Tuple[int, str, bytes]:
        if not _safe_xai_url(url):
            raise ValueError(f"unsafe xAI URL: {url}")

        current_url = url
        current_method = method.upper()
        current_form = form
        for _ in range(9):
            kwargs = {
                "method": current_method,
                "url": current_url,
                "timeout": timeout,
                "allow_redirects": False,
            }
            if current_form is not None:
                kwargs["data"] = current_form
                kwargs["headers"] = {"Content-Type": "application/x-www-form-urlencoded"}

            resp = self.session.request(**kwargs)
            status = resp.status_code
            body = resp.content[: (2 << 20) + 1]
            if len(body) > (2 << 20):
                raise RuntimeError("xAI OAuth response too large")

            if 300 <= status <= 399:
                location = (resp.headers.get("Location") or "").strip()
                if not location:
                    raise RuntimeError(f"redirect missing Location (HTTP {status})")
                next_url = urljoin(current_url, location)
                if not _safe_xai_url(next_url):
                    raise RuntimeError(f"redirect to untrusted host: {next_url}")
                if status == 303 or (
                    status in (301, 302) and current_method not in ("GET", "HEAD")
                ):
                    current_method = "GET"
                    current_form = None
                current_url = next_url
                continue

            return status, current_url, body

        raise RuntimeError("too many redirects")

    def convert(self, name_hint: str = "") -> BuildTokens:
        print("[Build] Validating Web SSO on accounts.x.ai ...")
        status, final_url, _ = self.do("GET", ACCOUNTS_URL)
        if status == 401 or "sign-in" in final_url or "sign-up" in final_url:
            raise RuntimeError("SSO unauthorized / redirected to sign-in")
        if status < 200 or status >= 400:
            raise RuntimeError(f"SSO check failed HTTP {status} url={final_url}")

        print("[Build] Starting device code ...")
        status, _, body = self.do(
            "POST",
            DEVICE_URL,
            {"client_id": CLIENT_ID, "scope": SCOPE, "referrer": "grok-build"},
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"device code failed HTTP {status}: {body[:200]!r}")
        device = json.loads(body.decode("utf-8", errors="replace"))
        device_code = device.get("device_code") or ""
        user_code = device.get("user_code") or ""
        verify_complete = device.get("verification_uri_complete") or ""
        interval = int(device.get("interval") or 5)
        expires_in = int(device.get("expires_in") or 1800)
        if not device_code or not user_code or not _safe_xai_url(verify_complete):
            raise RuntimeError(f"incomplete device response: {device}")

        print(f"[Build] user_code={user_code}")

        status, final_url, _ = self.do("GET", verify_complete)
        if status < 200 or status >= 400:
            raise RuntimeError(f"open verify page failed HTTP {status}")

        print("[Build] Verifying user_code with SSO ...")
        status, final_url, _ = self.do("POST", VERIFY_URL, {"user_code": user_code})
        if status < 200 or status >= 400:
            raise RuntimeError(f"verify failed HTTP {status} url={final_url}")
        if "consent" not in final_url:
            print(f"[Build] warn: verify final_url={final_url} (expected consent)")

        print("[Build] Approving device flow ...")
        status, final_url, _ = self.do(
            "POST",
            APPROVE_URL,
            {
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
        )
        if status < 200 or status >= 400:
            raise RuntimeError(f"approve failed HTTP {status} url={final_url}")
        if "done" not in final_url:
            print(f"[Build] warn: approve final_url={final_url}")

        print("[Build] Polling OAuth token ...")
        if interval < 1:
            interval = 1
        deadline = time.time() + min(expires_in, 75)
        while time.time() < deadline:
            time.sleep(interval)
            status, _, body = self.do(
                "POST",
                TOKEN_URL,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                },
            )
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except Exception as e:
                raise RuntimeError(f"token parse error: {e} body={body[:200]!r}") from e

            if 200 <= status < 300 and payload.get("access_token"):
                exp_in = int(payload.get("expires_in") or 3600)
                access = payload["access_token"]
                refresh = payload.get("refresh_token") or ""
                id_token = payload.get("id_token") or ""
                claims = _decode_jwt_claims(id_token or access)
                email = str(claims.get("email") or "").strip()
                user_id = str(claims.get("sub") or "").strip()
                team_id = str(claims.get("team_id") or "").strip()
                expires_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + exp_in)
                )
                name = email or name_hint or user_id or "Grok Build account"
                print(f"[Build] OAuth OK email={email or '-'} expires_in={exp_in}s")
                return BuildTokens(
                    access_token=access,
                    refresh_token=refresh,
                    id_token=id_token,
                    expires_at=expires_at,
                    expires_in=exp_in,
                    email=email,
                    user_id=user_id,
                    team_id=team_id,
                    name=name,
                    scope=str(payload.get("scope") or SCOPE),
                )

            err = payload.get("error") or ""
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err in ("access_denied", "expired_token"):
                raise RuntimeError(f"authorization denied: {err}")
            if status >= 400:
                raise RuntimeError(
                    f"token failed HTTP {status}: "
                    f"{payload.get('error_description') or err or body[:200]!r}"
                )
            raise RuntimeError(f"token failed: {payload}")

        raise RuntimeError("device flow poll timeout")


def convert_sso_to_build(sso_credential: Any, name_hint: str = "") -> BuildTokens:
    sso, sso_rw = extract_sso_from_credential(sso_credential)
    if not sso:
        raise ValueError("empty SSO token")
    return SsoBuildFlow(sso=sso, sso_rw=sso_rw).convert(name_hint=name_hint)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python sso_to_build.py <sso-jwt-or-header-file>")
        sys.exit(1)
    raw = open(sys.argv[1], encoding="utf-8").read().strip()
    tok = convert_sso_to_build(raw)
    print(
        json.dumps(
            {
                "email": tok.email,
                "user_id": tok.user_id,
                "expires_at": tok.expires_at,
                "access_token_prefix": tok.access_token[:40] + "...",
                "has_refresh": bool(tok.refresh_token),
            },
            indent=2,
        )
    )

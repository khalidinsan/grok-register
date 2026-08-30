"""Request/response body recorder for the hybrid v2 audit.

Extends api_audit with POST body capture: arm via GROK_API_AUDIT=2 (bodies).
Writes to logs/api_flow_bodies.jsonl. Headers are captured but Authorization
values are truncated. For direct-HTTP endpoint specs only.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_OUT = _ROOT / "logs" / "api_flow_bodies.jsonl"

_REDACT_HEADERS = ("authorization", "cookie", "x-castle-request-token")

_INTEREST_URLS = (
    "/api/auth/send-verification-code",
    "/api/auth/sign-up/verify-email",
    "/api/auth/sign-up/create-account",
    "/auth_mgmt.AuthManagement/ValidatePassword",
    "/oauth2/token",
    "/sign-up",
)


def bodies_enabled() -> bool:
    return (os.environ.get("GROK_API_AUDIT") or "").strip() == "2"


def _redact_headers(h: dict) -> dict:
    out = {}
    for k, v in (h or {}).items():
        lk = str(k).lower()
        if any(x in lk for x in _REDACT_HEADERS):
            out[str(k)] = str(v)[:16] + "…"
        else:
            out[str(k)] = v
    return out


def record_body(url: str, method: str, post_data: str, headers: dict) -> None:
    """Append one request-body line for interesting endpoints. Best-effort."""
    try:
        if not any(x in url for x in _INTEREST_URLS):
            return
        line = {
            "ts": time.strftime("%H:%M:%S"),
            "url": url,
            "method": (method or "").upper(),
            "post_data": (post_data or "")[:2000],
            "headers": _redact_headers(dict(headers or {})),
        }
        with _OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass

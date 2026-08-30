"""Endpoint flow recorder — writes every non-static request URL+method+status
to logs/api_flow.jsonl while the farm runs. For the direct-API audit.

Activate with env GROK_API_AUDIT=1. Overhead: one line append per request.
Only records POST/fetch/xhr/beacon (API calls), skips static assets, and
redacts Authorization header values (keeps first 12 chars).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_OUT = _ROOT / "logs" / "api_flow.jsonl"

_STATIC_EXT = (
    ".js", ".css", ".woff", ".woff2", ".ttf", ".png", ".jpg", ".jpeg",
    ".svg", ".gif", ".webp", ".ico", ".mp4", ".webm", ".map",
)


def audit_enabled() -> bool:
    return (os.environ.get("GROK_API_AUDIT") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _is_static(url: str) -> bool:
    path = url.split("?")[0].lower()
    return path.endswith(_STATIC_EXT)


def record_request(url: str, method: str, resource_type: str, status: int) -> None:
    """Append one API call line. Best-effort; never raises into the browser loop."""
    try:
        if _is_static(url):
            return
        rt = (resource_type or "").lower()
        if rt not in ("fetch", "xhr", "beacon", "document", "other"):
            return
        line = {
            "ts": time.strftime("%H:%M:%S"),
            "url": url,
            "method": (method or "GET").upper(),
            "rt": rt,
            "status": status,
        }
        with _OUT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
    except Exception:
        pass

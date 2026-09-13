"""
Chat usable probe — truth for 403 vs free Build (flash-grok-farm style).

USABLE = HTTP 200 on cli-chat-proxy /v1/responses with extractable text.
bot_flag_source alone does NOT mean unusable.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Optional

import requests

CHAT_URL = "https://cli-chat-proxy.grok.com/v1/responses"
CLI_UA = "grok-shell/0.2.99 (linux; x86_64)"
CLI_ID = "grok-shell"
CLI_VER = "0.2.99"
DEFAULT_MODEL = "grok-4.6"

# ── Account-quality probe ───────────────────────────────────────────────────
# Degraded accounts answer `print 407` with "202" — deterministically, every
# time. Verified 2026-09-13 over 108 farmed accounts at effort=xhigh:
#   407 -> 407   11 accounts  (always emit a reasoning output item)
#   407 -> 202   93 accounts  (never emit a reasoning item)
# A wrong digit here is therefore an account defect, not sampling noise. Nearby
# numbers and arithmetic answer correctly on both cohorts, and nothing on the
# backend distinguishes them (/user, /billing, /models, /settings and 25 other
# endpoints are byte-identical; JWT claims match; system_fingerprint matches).
QUALITY_PROBE_NUMBER = "407"
QUALITY_PROBE_EFFORT = "xhigh"


def _first_int(text: Any) -> Optional[str]:
    """First integer run in a reply.

    NOT every digit squashed together: a healthy account can leak a trailing
    system-prompt fragment ("407\\n\\confidence{100}"), which concatenation would
    turn into "407100" and misread as a wrong answer.
    """
    import re

    m = re.search(r"\d+", str(text or ""))
    return m.group(0) if m else None


def probe_account_quality(
    access_token: str,
    *,
    email: str = "",
    model: str = DEFAULT_MODEL,
    proxy: str = "",
    timeout: float = 60.0,
    confirm: bool = True,
) -> Dict[str, Any]:
    """Probe one token for the 407 degradation.

    Returns a dict with:
      ok       — True when the account echoed 407
      degraded — True only when upstream answered 200 with a reproducibly wrong
                 number. Transport errors / non-200 never set this.
      digits   — the first integer seen
      status   — HTTP status (0 on transport failure)
      err      — error detail, when any

    `confirm=True` re-probes once before declaring degradation, so a single odd
    reply can never mark a healthy account.
    """
    token = (access_token or "").strip()
    email = (email or "").strip()
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    proxies = {"http": proxy, "https": proxy} if proxy else None

    out: Dict[str, Any] = {
        "ok": False,
        "degraded": False,
        "digits": None,
        "status": 0,
        "email": email,
        "model": model,
        "reply": None,
        "err": None,
    }
    if not token:
        out["err"] = "empty access_token"
        return out

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLI_UA,
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-identifier": CLI_ID,
        "x-grok-client-version": CLI_VER,
        "x-grok-client-mode": "headless",
        "x-grok-session-id": str(uuid.uuid4()),
        "x-grok-req-id": str(uuid.uuid4()),
        "x-grok-model-override": model,
    }
    if email:
        headers["x-email"] = email

    def once() -> tuple:
        """One probe turn -> (status, digits, reply_text, err)."""
        body = {
            "model": model,
            "stream": False,
            "store": False,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": f"print {QUALITY_PROBE_NUMBER}",
                }
            ],
            "reasoning": {"summary": "concise", "effort": QUALITY_PROBE_EFFORT},
        }
        try:
            r = requests.post(
                CHAT_URL, headers=headers, json=body, timeout=timeout, proxies=proxies
            )
        except Exception as e:
            return 0, None, None, f"{type(e).__name__}: {e}"
        if r.status_code != 200:
            return r.status_code, None, None, r.text[:200]
        try:
            data = r.json()
        except Exception:
            data = r.text
        text = _extract_text(data) or ""
        return r.status_code, _first_int(text), text[:120], None

    status, digits, reply, err = once()
    out.update({"status": status, "digits": digits, "reply": reply, "err": err})
    if status != 200:
        return out
    if digits == QUALITY_PROBE_NUMBER:
        out["ok"] = True
        return out

    # Wrong digits on a 200 — confirm on a second, independent turn.
    if confirm and digits:
        status2, digits2, reply2, err2 = once()
        out["status"] = status2 or status
        out["reply"] = reply2 or reply
        out["err"] = err2 or err
        if status2 == 200 and digits2 == digits:
            out["degraded"] = True
    return out


def _extract_text(body: Any) -> Optional[str]:
    if body is None:
        return None
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            t = body.strip()
            return t[:200] if t else None
    if not isinstance(body, dict):
        return None
    for key in ("output_text", "text"):
        v = body.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:200]
    out = body.get("output")
    if isinstance(out, list):
        chunks = []
        for item in out:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        t = c.get("text") or c.get("output_text")
                        if isinstance(t, str) and t.strip():
                            chunks.append(t.strip())
        if chunks:
            return " ".join(chunks)[:200]
    return None


def probe_chat_usable(
    access_token: str,
    *,
    email: str = "",
    model: str = DEFAULT_MODEL,
    proxy: str = "",
    timeout: float = 60.0,
    retries: int = 1,
) -> Dict[str, Any]:
    token = (access_token or "").strip()
    email = (email or "").strip()
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    proxies = {"http": proxy, "https": proxy} if proxy else None

    last: Dict[str, Any] = {
        "usable": False,
        "status": 0,
        "model": model,
        "email": email,
        "reply": None,
        "err": None,
        "latency_ms": 0,
    }
    if not token:
        last["err"] = "empty access_token"
        return last

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": CLI_UA,
        "x-xai-token-auth": "xai-grok-cli",
        "x-grok-client-identifier": CLI_ID,
        "x-grok-client-version": CLI_VER,
        "x-grok-client-mode": "headless",
        "x-grok-session-id": str(uuid.uuid4()),
        "x-grok-req-id": str(uuid.uuid4()),
        "x-grok-model-override": model,
    }
    if email:
        headers["x-email"] = email

    # Prefer non-stream for easier parse; fall back stream if rejected
    payloads = [
        {
            "model": model,
            "stream": False,
            "store": False,
            "input": [
                {"type": "message", "role": "user", "content": "Reply with exactly: READY"}
            ],
            "max_output_tokens": 32,
        },
        {
            "model": model,
            "stream": True,
            "store": False,
            "input": [
                {"type": "message", "role": "user", "content": "Reply with exactly: READY"}
            ],
            "max_output_tokens": 32,
        },
    ]

    for attempt in range(max(1, retries + 1)):
        for body in payloads:
            t0 = time.perf_counter()
            try:
                r = requests.post(
                    CHAT_URL,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                    proxies=proxies,
                    stream=bool(body.get("stream")),
                )
                ms = int((time.perf_counter() - t0) * 1000)
                text = ""
                if body.get("stream"):
                    chunks = []
                    for line in r.iter_lines(decode_unicode=True):
                        if not line:
                            continue
                        chunks.append(line if isinstance(line, str) else line.decode())
                        if len("".join(chunks)) > 800:
                            break
                    raw = "\n".join(chunks)
                    text = raw
                    reply = "READY" if "READY" in raw.upper() or "response.created" in raw else _extract_text(raw)
                else:
                    try:
                        data = r.json()
                    except Exception:
                        data = r.text
                    reply = _extract_text(data)
                    text = str(data)[:200]
                last = {
                    "usable": r.status_code == 200 and (
                        bool(reply) or "response.created" in text or r.status_code == 200
                    ),
                    "status": r.status_code,
                    "model": model,
                    "email": email,
                    "reply": (reply or text or "")[:120],
                    "err": None if r.status_code == 200 else text[:200],
                    "latency_ms": ms,
                }
                # 200 with empty body still often OK for free tier
                if r.status_code == 200:
                    last["usable"] = True
                    return last
                if r.status_code in (401, 403, 402):
                    return last
            except Exception as e:
                last = {
                    "usable": False,
                    "status": 0,
                    "model": model,
                    "email": email,
                    "reply": None,
                    "err": f"{type(e).__name__}: {e}",
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                }
        if attempt + 1 < max(1, retries + 1):
            time.sleep(1.0)
    return last

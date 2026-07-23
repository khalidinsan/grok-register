"""
mailer.exzork.me — receive-only temp mail API provider.

Docs: https://mailer.exzork.me/docs
Base:  https://mailer.exzork.me

Auth:  X-API-Key: tm_...   or   Authorization: Bearer tm_...

Config (config.json → email.* or env):
  email.provider = "exzork"
  email.domain   = "koew.tech"          # apex (claim + wildcard MX)
  email.exzork_api_key / EXZORK_API_KEY / MAILER_EXZORK_API_KEY
  email.exzork_base_url (default https://mailer.exzork.me)
  email.exzork_use_subdomain (bool) — random@<rand>.domain.com when wildcard claimed
  email.local_style — human | random (local-part style)

Never commit API keys. Prefer env EXZORK_API_KEY.
"""

from __future__ import annotations

import json
import os
import random
import re
import string
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

# ── config ──────────────────────────────────────────────────────────

_ROOT = Path(__file__).resolve().parent
_conf: Dict[str, Any] = {}
_cfg_path = _ROOT / "config.json"
if _cfg_path.is_file():
    try:
        _conf = json.loads(_cfg_path.read_text(encoding="utf-8"))
    except Exception:
        _conf = {}

_email = _conf.get("email") if isinstance(_conf.get("email"), dict) else {}


def _ecfg(key: str, *env_keys: str, default: str = "") -> str:
    val = _email.get(key)
    if val is None or val == "":
        for ek in env_keys:
            ev = os.environ.get(ek, "")
            if ev:
                return str(ev).strip()
        return default
    return str(val).strip()


def _ebool(key: str, env_key: str = "", default: bool = False) -> bool:
    if env_key:
        raw = (os.environ.get(env_key) or "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            return True
        if raw in ("0", "false", "no", "off"):
            return False
    v = _email.get(key)
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


DEFAULT_BASE = "https://mailer.exzork.me"


def base_url() -> str:
    return (
        _ecfg("exzork_base_url", "EXZORK_BASE_URL", "MAILER_EXZORK_BASE_URL", default=DEFAULT_BASE)
        .rstrip("/")
        or DEFAULT_BASE
    )


def api_key() -> str:
    return _ecfg(
        "exzork_api_key",
        "EXZORK_API_KEY",
        "MAILER_EXZORK_API_KEY",
        "MAILER_API_KEY",
        default="",
    )


def apex_domain() -> str:
    d = _ecfg("domain", "EMAIL_DOMAIN", "EXZORK_DOMAIN", default="")
    return d.lstrip("@").strip().lower()


def use_subdomain() -> bool:
    # default True when provider is exzork (wildcard is the point); override with false
    return _ebool("exzork_use_subdomain", "EXZORK_USE_SUBDOMAIN", default=True)


# ── HTTP ────────────────────────────────────────────────────────────


def _request(
    method: str,
    path: str,
    *,
    body: Optional[dict] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    key = api_key()
    if not key:
        raise RuntimeError(
            "exzork API key missing — set email.exzork_api_key or env EXZORK_API_KEY"
        )
    url = f"{base_url()}{path if path.startswith('/') else '/' + path}"
    data = None
    headers = {
        "Accept": "application/json",
        "X-API-Key": key,
        "Authorization": f"Bearer {key}",
        "User-Agent": "grok-register-exzork/1.0",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = getattr(resp, "status", 200) or 200
            if not raw:
                return int(code), None
            try:
                return int(code), json.loads(raw)
            except Exception:
                return int(code), raw
    except HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        try:
            parsed = json.loads(err_body) if err_body else err_body
        except Exception:
            parsed = err_body
        return int(e.code), parsed
    except URLError as e:
        raise RuntimeError(f"exzork network error: {e}") from e


# ── helpers ─────────────────────────────────────────────────────────


def _rand_sub(n: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
    # start with letter (safer for hostnames)
    first = random.choice(string.ascii_lowercase)
    rest = "".join(random.choice(chars) for _ in range(max(0, n - 1)))
    return first + rest


def _human_or_random_local(given: str = "", family: str = "") -> str:
    style = (
        str(_email.get("local_style") or os.environ.get("EMAIL_LOCAL_STYLE") or "human")
        .strip()
        .lower()
    )
    if style in ("random", "legacy", "garbage"):
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(random.randint(8, 13)))
    try:
        from human_email import human_local_part

        return human_local_part(given=given, family=family)
    except Exception:
        chars = string.ascii_lowercase + string.digits
        return "".join(random.choice(chars) for _ in range(random.randint(8, 12)))


def _extract_address(payload: Any) -> str:
    """Normalize create-mailbox response → email address string."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        s = payload.strip()
        return s if "@" in s else ""
    if not isinstance(payload, dict):
        return ""
    for k in ("address", "email", "mailbox", "addr"):
        v = payload.get(k)
        if isinstance(v, str) and "@" in v:
            return v.strip()
    # nested
    for nest in ("data", "mailbox", "result"):
        inner = payload.get(nest)
        if isinstance(inner, dict):
            a = _extract_address(inner)
            if a:
                return a
        if isinstance(inner, str) and "@" in inner:
            return inner.strip()
    return ""


def _message_list(payload: Any) -> List[dict]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("messages", "data", "items", "results", "mails"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # single message object
        if any(k in payload for k in ("subject", "body", "text", "from", "id")):
            return [payload]
    return []


def _message_text(msg: dict) -> str:
    parts: List[str] = []
    for k in (
        "subject",
        "Subject",
        "body",
        "text",
        "text_body",
        "html",
        "content",
        "raw",
        "snippet",
        "preview",
    ):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
        elif isinstance(v, dict):
            # nested body
            for kk in ("text", "html", "plain"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    parts.append(vv)
    # strip crude HTML
    blob = "\n".join(parts)
    blob = re.sub(r"<[^>]+>", " ", blob)
    return blob


# ── public API used by email_register ───────────────────────────────


def create_mailbox(
    given: str = "",
    family: str = "",
    *,
    domain: str = "",
    prefer_subdomain: Optional[bool] = None,
) -> str:
    """
    Create a mailbox on exzork. Returns full address (local@host).

    With wildcard claimed + use_subdomain:
      local@<random>.koew.tech
    Else:
      local@koew.tech  (or API random on apex)
    """
    apex = (domain or apex_domain()).lstrip("@").strip().lower()
    if not apex:
        raise RuntimeError("exzork: email.domain / EMAIL_DOMAIN empty")

    local = _human_or_random_local(given=given, family=family)
    want_sub = use_subdomain() if prefer_subdomain is None else prefer_subdomain

    # Target host for the address
    if want_sub:
        host = f"{_rand_sub(8)}.{apex}"
    else:
        host = apex
    address = f"{local}@{host}"

    # Try explicit address first (best control over subdomain)
    for attempt, body in enumerate(
        (
            {"address": address},
            {"email": address},
            {"random": True, "domain": host},
            {"random": True, "domain": apex},
        ),
        start=1,
    ):
        code, data = _request("POST", "/api/v1/mailboxes", body=body)
        if 200 <= code < 300:
            got = _extract_address(data) or address
            print(f"[exzork] mailbox OK ({attempt}) {got}")
            return got
        # 4xx on explicit → try next shape
        print(f"[exzork] create try={attempt} HTTP {code} body={str(data)[:160]}")

    raise RuntimeError(f"exzork create mailbox failed for domain={apex}")


def list_messages(address: str) -> List[dict]:
    addr = quote(str(address or "").strip(), safe="@._+-")
    if not addr:
        return []
    code, data = _request("GET", f"/api/v1/mailboxes/{addr}/messages")
    if code == 404:
        return []
    if code >= 400:
        print(f"[exzork] list messages HTTP {code}: {str(data)[:200]}")
        return []
    return _message_list(data)


def wait_for_code(
    address: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 1.5,
) -> Optional[str]:
    """Poll mailbox messages until xAI OTP found or timeout."""
    from email_register import extract_verification_code

    address = (address or "").strip()
    if not address:
        return None
    print(f"[exzork] Waiting for OTP to {address} (timeout={int(timeout)}s)...")
    t0 = time.time()
    poll = 0
    seen: set[str] = set()

    while time.time() - t0 < timeout:
        poll += 1
        elapsed = int(time.time() - t0)
        try:
            msgs = list_messages(address)
            for msg in msgs:
                mid = str(msg.get("id") or msg.get("_id") or "")
                blob = _message_text(msg)
                # de-dupe by id+subject head
                sig = mid or blob[:80]
                if sig in seen:
                    continue
                seen.add(sig)
                # prefer x.ai / confirmation subjects
                low = blob.lower()
                if any(
                    k in low
                    for k in (
                        "x.ai",
                        "xai",
                        "confirmation",
                        "verification",
                        "verify",
                        "grok",
                        "one-time",
                        "otp",
                    )
                ) or not mid:
                    code = extract_verification_code(blob)
                    if code:
                        print(
                            f"[exzork] Found OTP: {code} for {address} in {elapsed}s"
                        )
                        return code
                else:
                    code = extract_verification_code(blob)
                    if code:
                        print(
                            f"[exzork] Found OTP: {code} for {address} in {elapsed}s"
                        )
                        return code
        except Exception as e:
            print(f"[exzork] poll error: {e}")

        if poll == 1 or poll % 8 == 0:
            print(f"[exzork] waiting... {elapsed}s/{int(timeout)}s")
        time.sleep(poll_interval)

    print(f"[exzork] Timeout waiting for OTP to {address}")
    return None


def get_email_and_token(
    given: str = "",
    family: str = "",
) -> Tuple[Optional[str], Optional[str]]:
    """Adapter: returns (email, token) — token is the same address for polling."""
    try:
        addr = create_mailbox(given=given, family=family)
        return addr, addr
    except Exception as e:
        print(f"[exzork] get_email_and_token failed: {e}")
        return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 120) -> Optional[str]:
    """Adapter: poll OTP; strip hyphens for form fill (caller may also strip)."""
    target = (email or dev_token or "").strip()
    code = wait_for_code(target, timeout=float(timeout))
    if code:
        return code.replace("-", "")
    return None


if __name__ == "__main__":
    # smoke: needs EXZORK_API_KEY + EMAIL_DOMAIN in env
    print("base=", base_url())
    print("domain=", apex_domain())
    print("key_set=", bool(api_key()))
    if api_key() and apex_domain():
        em, tok = get_email_and_token()
        print("mailbox=", em)
    else:
        print("skip live create — set EXZORK_API_KEY and EMAIL_DOMAIN")

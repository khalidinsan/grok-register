"""
mailer.khalid.id — receive-only temp mail API (khalidmailer).

Docs: https://mailer.khalid.id/docs
Base:  https://mailer.khalid.id

Auth:  X-API-Key: tm_...   or   Authorization: Bearer tm_...

Config (config.json → email.* or env):
  email.provider = "khalidmailer"   # aliases: khalid, mailer.khalid
  email.domain   = "gumial.web.id"  # apex; wildcard MX * → mailer.khalid.id
  email.khalidmailer_api_key / KHALIDMAILER_API_KEY / MAILER_API_KEY
  email.khalidmailer_base_url (default https://mailer.khalid.id)
  email.khalidmailer_use_subdomain (bool, default true) — local@<rand>.apex
  email.local_style — human | random

Never commit API keys. Prefer env KHALIDMAILER_API_KEY.
"""

from __future__ import annotations

import json
import os
import quopri
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


DEFAULT_BASE = "https://mailer.khalid.id"


def base_url() -> str:
    return (
        _ecfg(
            "khalidmailer_base_url",
            "KHALIDMAILER_BASE_URL",
            "MAILER_KHALID_BASE_URL",
            "MAILER_BASE_URL",
            default=DEFAULT_BASE,
        )
        .rstrip("/")
        or DEFAULT_BASE
    )


def api_key() -> str:
    return _ecfg(
        "khalidmailer_api_key",
        "KHALIDMAILER_API_KEY",
        "MAILER_KHALID_API_KEY",
        "MAILER_API_KEY",
        # legacy env names still accepted during migration
        "EXZORK_API_KEY",
        default="",
    )


def apex_domain() -> str:
    """Primary/first domain (compat). Prefer next_email_domain() for RR create."""
    try:
        from email_register import list_email_domains

        pool = list_email_domains()
        if pool:
            return pool[0]
    except Exception:
        pass
    d = _ecfg("domain", "EMAIL_DOMAIN", "KHALIDMAILER_DOMAIN", default="")
    return d.lstrip("@").strip().lower()


def use_subdomain() -> bool:
    # default True (wildcard is the point for farm)
    return _ebool(
        "khalidmailer_use_subdomain",
        "KHALIDMAILER_USE_SUBDOMAIN",
        default=True,
    ) or _ebool("exzork_use_subdomain", "EXZORK_USE_SUBDOMAIN", default=False)


# ── HTTP ────────────────────────────────────────────────────────────


def _unwrap(payload: Any) -> Any:
    """Unwrap { success, data } envelopes from mailer.khalid.id."""
    if not isinstance(payload, dict):
        return payload
    if "data" in payload and payload.get("success") is not False:
        return payload["data"]
    return payload


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
            "khalidmailer API key missing — set email.khalidmailer_api_key "
            "or env KHALIDMAILER_API_KEY"
        )
    # API lives under /api/v1
    p = path if path.startswith("/") else "/" + path
    if not p.startswith("/api/"):
        p = "/api/v1" + p if p.startswith("/") else "/api/v1/" + p
    url = f"{base_url()}{p}"
    data = None
    headers = {
        "Accept": "application/json",
        "X-API-Key": key,
        "Authorization": f"Bearer {key}",
        "User-Agent": "grok-register-khalidmailer/1.0",
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
        raise RuntimeError(f"khalidmailer network error: {e}") from e


# ── helpers ─────────────────────────────────────────────────────────


def _rand_sub(n: int = 8) -> str:
    chars = string.ascii_lowercase + string.digits
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
    payload = _unwrap(payload)
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
    local = payload.get("local_part") or payload.get("local")
    domain = payload.get("domain")
    if isinstance(local, str) and isinstance(domain, str) and local and domain:
        return f"{local}@{domain}"
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
    """
    List endpoint (docs):
      { "success": true, "data": [ { id, from_addr, subject, ... } ] }
    """
    payload = _unwrap(payload)
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for k in ("messages", "data", "items", "results", "mails"):
            v = payload.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        if isinstance(payload.get("message"), dict):
            return [payload["message"]]
        if any(
            k in payload
            for k in ("subject", "body", "body_text", "from_addr", "id", "snippet")
        ):
            return [payload]
    return []


def _decode_qpish(raw: str) -> str:
    if not raw:
        return ""
    s = raw
    if "=3D" in s or "=\r\n" in s or "=\n" in s or "quoted-printable" in s.lower():
        try:
            s = quopri.decodestring(s.encode("utf-8", errors="replace")).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            try:
                s = quopri.decodestring(s.encode("latin-1", errors="replace")).decode(
                    "latin-1", errors="replace"
                )
            except Exception:
                pass
    return s


def _message_text(msg: dict) -> str:
    parts: List[str] = []
    for k in (
        "subject",
        "Subject",
        "body",
        "body_text",
        "body_html",
        "text",
        "text_body",
        "html",
        "html_body",
        "content",
        "raw",
        "snippet",
        "preview",
        "from_addr",
        "from_address",
        "to_addr",
        "to_address",
    ):
        v = msg.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
        elif isinstance(v, dict):
            for kk in ("text", "html", "plain", "text_body", "body_text"):
                vv = v.get(kk)
                if isinstance(vv, str) and vv.strip():
                    parts.append(vv)
    blob = "\n".join(parts)
    blob = _decode_qpish(blob)
    if "Subject:" in blob or "subject:" in blob:
        m = re.search(r"(?im)^Subject:\s*(.+)$", blob)
        if m:
            parts.insert(0, m.group(1).strip())
            blob = m.group(1).strip() + "\n" + blob
    blob = re.sub(r"<[^>]+>", " ", blob)
    blob = re.sub(r"&nbsp;", " ", blob, flags=re.I)
    blob = re.sub(r"\s+", " ", blob)
    return blob


def get_message(message_id: Any) -> Optional[dict]:
    """GET /api/v1/messages/{id} → full body (body_text / body_html)."""
    mid = str(message_id or "").strip()
    if not mid:
        return None
    code, data = _request("GET", f"/api/v1/messages/{quote(mid, safe='')}")
    if code >= 400 or data is None:
        print(f"[khalidmailer] get message {mid} HTTP {code}: {str(data)[:160]}")
        return None
    un = _unwrap(data)
    if isinstance(un, dict):
        if isinstance(un.get("message"), dict):
            return un["message"]
        return un
    if isinstance(data, dict) and isinstance(data.get("message"), dict):
        return data["message"]
    return None


# ── public API used by email_register ───────────────────────────────


def _host_of(addr: str) -> str:
    a = (addr or "").strip().lower()
    if "@" not in a:
        return ""
    return a.rsplit("@", 1)[-1]


def _is_subdomain_of(host: str, apex: str) -> bool:
    host = (host or "").lower().strip(".")
    apex = (apex or "").lower().strip(".")
    if not host or not apex or host == apex:
        return False
    return host.endswith("." + apex)


def create_mailbox(
    given: str = "",
    family: str = "",
    *,
    domain: str = "",
    prefer_subdomain: Optional[bool] = None,
) -> str:
    """
    Create mailbox. Returns full address.

    Wildcard + use_subdomain:
      local@<random>.gumial.web.id
    Else:
      local@gumial.web.id
    """
    apex = (domain or "").lstrip("@").strip().lower()
    if not apex:
        try:
            from email_register import next_email_domain

            apex = next_email_domain().lstrip("@").strip().lower()
        except Exception:
            apex = apex_domain()
    if not apex:
        raise RuntimeError("khalidmailer: email.domain / EMAIL_DOMAIN empty")

    local = _human_or_random_local(given=given, family=family)
    want_sub = use_subdomain() if prefer_subdomain is None else prefer_subdomain

    if want_sub:
        host = f"{_rand_sub(8)}.{apex}"
    else:
        host = apex
    address = f"{local}@{host}"
    print(
        f"[khalidmailer] create want_sub={want_sub} requested={address} apex={apex}"
    )

    # Docs: { "local_part", "domain" } — domain may be apex or full host for wildcard
    if want_sub:
        attempts: list[dict] = [
            {"local_part": local, "domain": host},
            {"local_part": local, "domain": f"*.{apex}"},
            {"domain": host},  # random local on that host
            {"domain": f"*.{apex}"},
            {"address": address},
            {"email": address},
        ]
    else:
        attempts = [
            {"local_part": local, "domain": apex},
            {"domain": apex},
            {"address": address},
            {"email": address},
        ]

    last_err = ""
    for attempt, body in enumerate(attempts, start=1):
        code, data = _request("POST", "/api/v1/mailboxes", body=body)
        if 200 <= code < 300:
            got = _extract_address(data) or ""
            if not got or "@" not in got:
                got = address
            got_host = _host_of(got)
            if want_sub and not _is_subdomain_of(got_host, apex):
                if _is_subdomain_of(_host_of(address), apex):
                    print(
                        f"[khalidmailer] API returned apex {got!r} — "
                        f"using requested subdomain {address!r}"
                    )
                    got = address
                else:
                    print(
                        f"[khalidmailer] create try={attempt} got apex {got!r}, "
                        f"need subdomain — retry"
                    )
                    last_err = f"apex_not_sub:{got}"
                    continue
            print(f"[khalidmailer] mailbox OK ({attempt}) {got}")
            return got
        last_err = f"HTTP {code} {str(data)[:160]}"
        print(f"[khalidmailer] create try={attempt} {last_err}")

    raise RuntimeError(
        f"khalidmailer create mailbox failed want_sub={want_sub} "
        f"apex={apex} last={last_err}"
    )


def list_messages(address: str) -> List[dict]:
    addr = quote(str(address or "").strip(), safe="@._+-")
    if not addr:
        return []
    code, data = _request("GET", f"/api/v1/mailboxes/{addr}/messages")
    if code == 404:
        return []
    if code >= 400:
        print(f"[khalidmailer] list messages HTTP {code}: {str(data)[:200]}")
        return []
    return _message_list(data)


def wait_for_code(
    address: str,
    *,
    timeout: float = 120.0,
    poll_interval: float = 1.5,
) -> Optional[str]:
    """Poll until xAI OTP found (list meta + full message body)."""
    from email_register import extract_verification_code

    address = (address or "").strip()
    if not address:
        return None
    print(f"[khalidmailer] Waiting for OTP to {address} (timeout={int(timeout)}s)...")
    t0 = time.time()
    poll = 0
    fetched_ids: set[str] = set()

    while time.time() - t0 < timeout:
        poll += 1
        elapsed = int(time.time() - t0)
        try:
            msgs = list_messages(address)
            if poll == 1 or (msgs and poll % 5 == 0):
                print(f"[khalidmailer] list n={len(msgs)} elapsed={elapsed}s")
            for msg in msgs:
                mid = str(msg.get("id") or msg.get("_id") or "").strip()
                blob = _message_text(msg)
                code = extract_verification_code(blob) if blob else None
                if code:
                    print(
                        f"[khalidmailer] Found OTP: {code} for {address} "
                        f"in {elapsed}s (list)"
                    )
                    return code

                if mid and mid not in fetched_ids:
                    full = get_message(mid)
                    fetched_ids.add(mid)
                    if full:
                        blob2 = _message_text(full)
                        raw_tb = str(
                            full.get("body_text")
                            or full.get("text_body")
                            or full.get("body")
                            or full.get("body_html")
                            or ""
                        )
                        raw_tb = _decode_qpish(raw_tb)
                        code = extract_verification_code(blob2) or extract_verification_code(
                            raw_tb
                        )
                        if code:
                            print(
                                f"[khalidmailer] Found OTP: {code} for {address} "
                                f"in {elapsed}s (message id={mid})"
                            )
                            return code
                        snip = (raw_tb or blob2 or "")[:120].replace("\n", " ")
                        print(
                            f"[khalidmailer] message id={mid} no OTP yet "
                            f"from={(full.get('from_addr') or full.get('from_address') or '')[:40]!r} "
                            f"snip={snip!r}"
                        )
        except Exception as e:
            print(f"[khalidmailer] poll error: {e}")

        if poll == 1 or poll % 8 == 0:
            print(f"[khalidmailer] waiting... {elapsed}s/{int(timeout)}s")
        time.sleep(poll_interval)

    print(f"[khalidmailer] Timeout waiting for OTP to {address}")
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
        print(f"[khalidmailer] get_email_and_token failed: {e}")
        return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 120) -> Optional[str]:
    """Adapter: poll OTP; strip hyphens for form fill."""
    target = (email or dev_token or "").strip()
    code = wait_for_code(target, timeout=float(timeout))
    if code:
        return code.replace("-", "")
    return None


if __name__ == "__main__":
    print("base=", base_url())
    print("domain=", apex_domain())
    print("key_set=", bool(api_key()))
    if api_key() and apex_domain():
        em, tok = get_email_and_token()
        print("mailbox=", em)
    else:
        print("skip live create — set KHALIDMAILER_API_KEY and EMAIL_DOMAIN")

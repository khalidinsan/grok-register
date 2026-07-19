
from __future__ import annotations

import email as email_lib
import imaplib
import json
import os
import random
import re
import string
import time
from email.header import decode_header
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ============================================================
# Config: catch-all domain + IMAP (fast, alibaba-farm style)
# ============================================================

_config_path = Path(__file__).parent / "config.json"
_conf: Dict[str, Any] = {}
if _config_path.exists():
    with _config_path.open("r", encoding="utf-8") as _f:
        _conf = json.load(_f)

_email_conf = _conf.get("email") if isinstance(_conf.get("email"), dict) else {}


def _cfg(key: str, env_key: str, default: str = "") -> str:
    val = _email_conf.get(key)
    if val is None or val == "":
        val = _conf.get(key)
    if val is None or val == "":
        val = os.environ.get(env_key, default)
    return str(val or default)


EMAIL_DOMAIN = _cfg("domain", "EMAIL_DOMAIN") or _cfg("email_domain", "EMAIL_DOMAIN")
IMAP_USER = _cfg("imap_user", "IMAP_USER")
IMAP_PASS = _cfg("imap_pass", "IMAP_PASS")
IMAP_HOST = _cfg("imap_host", "IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(_cfg("imap_port", "IMAP_PORT", "993") or "993")

# Server-side filters — tiny result set, no full-inbox scan
_SEARCH_CRITERIA = (
    '(FROM "x.ai")',
    '(FROM "SpaceXAI")',
    '(SUBJECT "confirmation code")',
)

# ============================================================
# Adapter for DrissionPage_example.py
# ============================================================

_temp_email_cache: Dict[str, str] = {}


def get_email_and_token() -> Tuple[Optional[str], Optional[str]]:
    """Generate catch-all alias. Returns (email, token) — token == email for IMAP."""
    email_addr = create_temp_email()
    if email_addr:
        _temp_email_cache[email_addr] = email_addr
        return email_addr, email_addr
    return None, None


def get_oai_code(dev_token: str, email: str, timeout: int = 120) -> Optional[str]:
    """Poll IMAP for Grok/x.ai OTP. Strips hyphens for form fill."""
    target = (email or dev_token or "").strip()
    if not target:
        return None
    code = wait_for_verification_code(target_email=target, timeout=timeout)
    if code:
        code = code.replace("-", "")
    return code


# ============================================================
# Core
# ============================================================

def _require_imap_config() -> None:
    missing = []
    if not EMAIL_DOMAIN:
        missing.append("email.domain / EMAIL_DOMAIN")
    if not IMAP_USER:
        missing.append("email.imap_user / IMAP_USER")
    if not IMAP_PASS:
        missing.append("email.imap_pass / IMAP_PASS")
    if missing:
        raise Exception(
            "IMAP catch-all belum dikonfigurasi. Isi config.json (email.*) atau env: "
            + ", ".join(missing)
        )


def create_temp_email() -> str:
    _require_imap_config()
    chars = string.ascii_lowercase + string.digits
    local = "".join(random.choice(chars) for _ in range(random.randint(8, 13)))
    email_addr = f"{local}@{EMAIL_DOMAIN.lstrip('@')}"
    print(f"[*] Catch-all email: {email_addr}")
    return email_addr


def _decode_header_value(raw: Optional[str]) -> str:
    if not raw:
        return ""
    parts = decode_header(raw)
    out: List[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            out.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(str(chunk))
    return " ".join(out)


def extract_verification_code(content: str) -> Optional[str]:
    """
    Extract OTP from subject/body.
    x.ai subject: "SpaceXAI confirmation code: B10-CZJ"
    """
    if not content:
        return None

    m = re.search(
        r"confirmation code[:\s]+([A-Z0-9]{3}-[A-Z0-9]{3})",
        content,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()

    m = re.search(
        r"(?:verification code|验证码|your code|one-time code|code is)[:\s]*([A-Z0-9]{3}-[A-Z0-9]{3})",
        content,
        re.IGNORECASE,
    )
    if m:
        return m.group(1).upper()

    m = re.search(r"(?<![A-Z0-9-])([A-Z0-9]{3}-[A-Z0-9]{3})(?![A-Z0-9-])", content)
    if m:
        return m.group(1).upper()

    skip = {"177010", "181818", "666666", "808080", "000000"}
    for code in re.findall(r"(?<![&#\d])(\d{6})(?![&#\d])", content):
        if code not in skip:
            return code
    return None


def _open_imap() -> imaplib.IMAP4_SSL:
    _require_imap_config()
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    mail.login(IMAP_USER, IMAP_PASS)
    return mail


def _select(mail: imaplib.IMAP4_SSL, box: str) -> bool:
    for name in (box, f'"{box}"'):
        try:
            st, _ = mail.select(name)
            if st == "OK":
                return True
        except Exception:
            continue
    return False


def _search_xai_ids(mail: imaplib.IMAP4_SSL) -> List[bytes]:
    """Server-side search — only x.ai / confirmation mails."""
    found: Set[bytes] = set()
    for criteria in _SEARCH_CRITERIA:
        try:
            st, data = mail.search(None, criteria)
            if st == "OK" and data and data[0]:
                found.update(data[0].split())
        except Exception:
            continue
    return sorted(found, key=lambda x: int(x))


def _fetch_headers(mail: imaplib.IMAP4_SSL, mid: bytes) -> Optional[email_lib.message.Message]:
    """Header-only — OTP lives in Subject, no body needed."""
    try:
        st, data = mail.fetch(
            mid,
            "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE DELIVERED-TO X-ORIGINAL-TO)])",
        )
        if st != "OK" or not data or not data[0]:
            return None
        raw = data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            return None
        return email_lib.message_from_bytes(bytes(raw))
    except Exception:
        return None


def _recipient_blob(msg: email_lib.message.Message) -> str:
    return " ".join(
        [
            msg.get("To", "") or "",
            msg.get("Delivered-To", "") or "",
            msg.get("X-Original-To", "") or "",
            msg.get("Cc", "") or "",
        ]
    ).lower()


def _find_code_for_alias(
    mail: imaplib.IMAP4_SSL,
    target: str,
    recent: int = 20,
) -> Optional[Tuple[str, str, bytes]]:
    """
    Among recent FROM-x.ai mails, find one addressed to `target`.
    Returns (code, subject, msg_id) or None.

    Unique catch-all alias = no need for heavy "seen" snapshot.
    """
    ids = _search_xai_ids(mail)
    if not ids:
        return None

    for mid in reversed(ids[-recent:]):
        msg = _fetch_headers(mail, mid)
        if not msg:
            continue
        if target not in _recipient_blob(msg):
            continue
        subj = _decode_header_value(msg.get("Subject", ""))
        code = extract_verification_code(subj)
        if code:
            return code, subj, mid
    return None


def wait_for_verification_code(target_email: str, timeout: int = 120) -> Optional[str]:
    """
    Fast poll:
    - keep one IMAP connection
    - SEARCH FROM x.ai only (not full inbox / not All Mail)
    - header-only fetch; parse OTP from Subject
    - match unique alias in To (no slow snapshot that can race-skip new mail)
    """
    _require_imap_config()
    target = target_email.lower().strip()
    print(f"[IMAP] Waiting for OTP to {target_email} (timeout={timeout}s)...")
    print(f"[IMAP] {IMAP_USER} @ {IMAP_HOST} — fast search FROM x.ai")

    start = time.time()
    poll = 0
    mail = _open_imap()

    try:
        if not _select(mail, "INBOX"):
            raise Exception("Cannot select INBOX")
        print("[IMAP] Connected, polling every 2s...")

        spam_checked = False

        while time.time() - start < timeout:
            poll += 1
            elapsed = int(time.time() - start)

            try:
                try:
                    mail.noop()
                except Exception:
                    try:
                        mail.logout()
                    except Exception:
                        pass
                    mail = _open_imap()
                    _select(mail, "INBOX")

                hit = _find_code_for_alias(mail, target)
                if hit:
                    code, subj, mid = hit
                    print(
                        f"[IMAP] Found OTP: {code} for {target_email} "
                        f"(Subj={subj!r}) in {elapsed}s"
                    )
                    try:
                        mail.store(mid, "+FLAGS", "\\Seen")
                    except Exception:
                        pass
                    return code

                # One-time Spam peek if still empty after 25s
                if not spam_checked and elapsed >= 25:
                    spam_checked = True
                    for spam_box in ("[Gmail]/Spam", "[Gmail]/Junk"):
                        if not _select(mail, spam_box):
                            continue
                        hit = _find_code_for_alias(mail, target, recent=10)
                        _select(mail, "INBOX")
                        if hit:
                            code, subj, mid = hit
                            print(f"[IMAP] Found OTP in Spam: {code} (Subj={subj!r})")
                            return code
                        break

                if poll == 1 or poll % 5 == 0:
                    print(f"[IMAP] waiting... {elapsed}s/{timeout}s")

            except Exception as e:
                print(f"[IMAP] Error: {e}")
                try:
                    mail.logout()
                except Exception:
                    pass
                time.sleep(1)
                try:
                    mail = _open_imap()
                    _select(mail, "INBOX")
                except Exception as e2:
                    print(f"[IMAP] Reconnect failed: {e2}")

            time.sleep(2)

    finally:
        try:
            mail.logout()
        except Exception:
            pass

    print(f"[IMAP] Timeout waiting for OTP to {target_email}")
    return None

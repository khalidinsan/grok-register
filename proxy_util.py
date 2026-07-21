"""
Proxy list helpers for the Grok farm.

Supported line formats:
  - http://user:pass@host:port
  - http://user:pass@host:port/          (trailing slash stripped)
  - socks5://user:pass@host:port
  - user:pass@host:port
  - host:port:user:pass                  ← Webshare export
  - host:port
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote, urlunsplit, urlsplit


def _looks_like_host_port(host: str, port: str) -> bool:
    if not host or not port:
        return False
    if not port.isdigit():
        return False
    # basic host: ipv4 / hostname / ipv6 in brackets
    return True


def _canonicalize_proxy_url(url: str) -> str:
    """
    Normalize a scheme:// URL for Chromium/requests:
      - drop path/query/fragment (proxy URLs shouldn't carry /)
      - keep user:pass@host:port
      - leave userinfo percent-encoding as-is if already present
    """
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return url.rstrip("/")

    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks4a"):
        # unknown scheme — return stripped only
        return url.strip().rstrip("/")

    # urlsplit puts user:pass in .username/.password (decoded) and host:port in hostname/port
    host = parts.hostname or ""
    port = parts.port
    if not host:
        return url.strip().rstrip("/")

    netloc_host = host
    if ":" in host and not host.startswith("["):
        # ipv6 without brackets — rare
        netloc_host = f"[{host}]"

    if port:
        netloc_host = f"{netloc_host}:{port}"
    elif parts.netloc and "@" in parts.netloc:
        # port missing from .port but present in netloc? urlsplit handles normally
        after = parts.netloc.rsplit("@", 1)[-1]
        if ":" in after and after.rsplit(":", 1)[-1].isdigit():
            netloc_host = after

    user = parts.username
    password = parts.password
    if user is not None:
        # re-encode so special chars stay safe; unquote first to avoid double-encoding
        u = quote(unquote(user), safe="")
        if password is not None:
            p = quote(unquote(password), safe="")
            netloc = f"{u}:{p}@{netloc_host}"
        else:
            netloc = f"{u}@{netloc_host}"
    else:
        # No username in parse — preserve raw netloc if it had odd auth form
        netloc = parts.netloc
        # strip trailing junk from netloc only
        if netloc.endswith("/"):
            netloc = netloc.rstrip("/")

    # proxy URL = scheme://user:pass@host:port  (no path)
    return urlunsplit((scheme, netloc, "", "", ""))


def _webshare_from_parts(parts: list[str]) -> str:
    """host:port:user:pass… → http://user:pass@host:port"""
    host, port, user = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:]) if len(parts) > 3 else ""
    if not _looks_like_host_port(host, port):
        return ""
    u = quote(user, safe="")
    p = quote(password, safe="")
    if password or user:
        return f"http://{u}:{p}@{host}:{port}"
    return f"http://{host}:{port}"


def normalize_proxy(line: str) -> str:
    """Normalize one proxy line to a clean scheme://[user:pass@]host:port URL."""
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return ""
    s = s.replace("\ufeff", "").strip()

    # ── 1) Already a URL ────────────────────────────────────────────
    if "://" in s:
        return _canonicalize_proxy_url(s)

    # ── 2) Webshare host:port:user:pass  (before "@" shortcut!) ─────
    # Password may contain "@" or ":" — detect by port field being digits.
    parts = s.split(":")
    if len(parts) >= 4 and parts[1].isdigit():
        # host:port:user:pass…
        # Avoid misreading "user:pass@host:port" (no — that has @ and fewer pure splits)
        # If string has "@", it might still be webshare with @ in password:
        #   1.2.3.4:8080:user:p@ss:word  → parts[1]=8080 digit ✓
        built = _webshare_from_parts(parts)
        if built:
            return built

    # ── 3) user:pass@host:port  (no scheme) ─────────────────────────
    if "@" in s:
        after = s.rsplit("@", 1)[-1]
        # after should look like host:port
        if ":" in after:
            host, port = after.rsplit(":", 1)
            if _looks_like_host_port(host, port):
                return _canonicalize_proxy_url(f"http://{s}")
        # host only after @
        return _canonicalize_proxy_url(f"http://{s}")

    # ── 4) host:port ────────────────────────────────────────────────
    if len(parts) == 2 and parts[1].isdigit():
        return f"http://{parts[0]}:{parts[1]}"

    # ── 5) host:port:user (no password) ─────────────────────────────
    if len(parts) == 3 and parts[1].isdigit():
        built = _webshare_from_parts(parts + [""])
        if built:
            return built

    return s


def load_proxy_file(path: str) -> List[str]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"proxy file not found: {p}")
    out: List[str] = []
    seen = set()
    text = p.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        url = normalize_proxy(line)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def load_proxy_list(raw: Optional[list] = None) -> List[str]:
    """Normalize a list of proxy strings (config pool.proxies)."""
    out: List[str] = []
    seen = set()
    for item in raw or []:
        url = normalize_proxy(str(item))
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def mask_proxy(url: str) -> str:
    if not url or "@" not in url:
        return url or ""
    try:
        left, right = url.rsplit("@", 1)
        if "://" in left:
            scheme, creds = left.split("://", 1)
            if ":" in creds:
                user = creds.split(":", 1)[0]
                return f"{scheme}://{user}:***@{right}"
            return f"{scheme}://***@{right}"
        return f"***@{right}"
    except Exception:
        return "***"


def playwright_proxy_dict(url: str) -> Optional[Dict[str, str]]:
    """
    Playwright / Camoufox-style proxy dict (same as grok-farm farm._parse_proxy).

    Chromium cannot embed user:pass in --proxy-server; Playwright passes
    username/password separately and handles Proxy-Authorization natively.

      {"server": "http://host:port", "username": "...", "password": "..."}
    """
    info = parse_proxy(url)
    host = info.get("host") or ""
    port = info.get("port")
    if not host or not port:
        return None
    scheme = (info.get("scheme") or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks4a"):
        scheme = "http"
    out: Dict[str, str] = {"server": f"{scheme}://{host}:{port}"}
    user = info.get("user") or ""
    password = info.get("password") or ""
    if user:
        out["username"] = user
    if password:
        out["password"] = password
    return out


def parse_proxy(url: str) -> Dict[str, Any]:
    """
    Split a normalized proxy URL into parts for Chromium vs requests.

    Chromium --proxy-server cannot embed user:pass (ERR_NO_SUPPORTED_PROXIES).
    Use chrome_server (no auth) + proxy-auth extension for browser;
    keep full url for requests.
    """
    raw = normalize_proxy(url or "")
    if not raw:
        return {
            "url": "",
            "scheme": "http",
            "host": "",
            "port": None,
            "user": "",
            "password": "",
            "chrome_server": "",
            "has_auth": False,
        }
    try:
        parts = urlsplit(raw)
    except Exception:
        return {
            "url": raw,
            "scheme": "http",
            "host": "",
            "port": None,
            "user": "",
            "password": "",
            "chrome_server": raw,
            "has_auth": False,
        }
    scheme = (parts.scheme or "http").lower()
    if scheme not in ("http", "https", "socks5", "socks5h", "socks4", "socks4a"):
        scheme = "http"
    host = parts.hostname or ""
    port = parts.port
    user = unquote(parts.username) if parts.username else ""
    password = unquote(parts.password) if parts.password else ""
    # Chrome --proxy-server: scheme://host:port  (NO credentials)
    if host and port:
        chrome_server = f"{scheme}://{host}:{port}"
    elif host:
        chrome_server = f"{scheme}://{host}"
    else:
        chrome_server = ""
    return {
        "url": raw,  # full URL with auth — for requests
        "scheme": scheme,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "chrome_server": chrome_server,
        "has_auth": bool(user or password),
    }


def requests_proxies(proxy_url: str) -> Optional[dict]:
    """Dict for requests Session (keeps user:pass in URL), or None if empty."""
    info = parse_proxy(proxy_url)
    p = info.get("url") or ""
    if not p:
        return None
    return {"http": p, "https": p}


def write_proxy_auth_extension(
    username: str,
    password: str,
    dest_dir: Optional[str] = None,
) -> str:
    """
    Write a tiny unpacked Chrome extension that answers proxy 407 auth.

    Chromium cannot take user:pass in --proxy-server (ERR_NO_SUPPORTED_PROXIES).
    Pattern: --proxy-server=http://host:port + this extension.

    Returns path to the extension directory.
    """
    if dest_dir:
        root = Path(dest_dir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="grok_proxy_auth_"))

    # Manifest V2: webRequestBlocking still works on Playwright Chromium
    # for local unpacked extensions (MV3 asyncBlocking is flaky for proxy).
    manifest = {
        "version": "1.0.0",
        "manifest_version": 2,
        "name": "Grok Farm Proxy Auth",
        "description": "Supplies proxy username/password (automation only)",
        "permissions": [
            "webRequest",
            "webRequestBlocking",
            "proxy",
            "<all_urls>",
        ],
        "background": {"scripts": ["background.js"], "persistent": True},
        "minimum_chrome_version": "22.0.0",
    }
    # JSON.stringify-safe credentials in JS
    bg = (
        "// auto-generated — do not edit\n"
        f"var _u = {json.dumps(username or '')};\n"
        f"var _p = {json.dumps(password or '')};\n"
        "chrome.webRequest.onAuthRequired.addListener(\n"
        "  function (details) {\n"
        "    return { authCredentials: { username: _u, password: _p } };\n"
        "  },\n"
        "  { urls: ['<all_urls>'] },\n"
        "  ['blocking']\n"
        ");\n"
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (root / "background.js").write_text(bg, encoding="utf-8")
    return str(root)


def encode_proxy_env(proxies: List[str]) -> str:
    """Pack list for GROK_PROXIES env (newline-separated)."""
    return "\n".join(proxies)


def decode_proxy_env(raw: str) -> List[str]:
    if not raw:
        return []
    return load_proxy_list(raw.splitlines())

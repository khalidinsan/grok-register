"""Disk-backed HTTP response cache for static assets (proxy-quota saver).

The Camoufox/Firefox disk-cache prefs (`browser.cache.disk.parent_directory`)
do NOT persist across fresh temp profiles (verified empirically: 0 cache files,
warm runs still issue ~the same request count). So instead of relying on the
browser cache, the farm caches immutable static responses on disk and fulfills
them straight from disk — zero bytes through the proxy.

Bodies are stored DECODED (gzip/br/deflate removed) and replayed without a
content-encoding header, so serving them via route.fulfill is deterministic
(no double-decode corruption).

Keyed by URL (sha256). Only GET, only cacheable static resource types, only
public hosts (accounts.x.ai / cdn.grok.com / grok.com), never XHR/fetch,
never responses with Vary: Cookie / Set-Cookie / no-store / no-cache.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

# Static asset hosts that are safe & worth caching (Next.js hashed chunks).
CACHEABLE_HOSTS = (
    "accounts.x.ai",
    "cdn.grok.com",
    "grok.com",
)

CACHEABLE_TYPES = ("script", "stylesheet", "image", "font", "media")

DEFAULT_MAX_BODY = 15 * 1024 * 1024  # 15 MB — a single JS chunk is ~1-3 MB
DEFAULT_TTL = 30 * 24 * 3600  # hashed `_next/static` files are immutable

# Response headers we replay on a cache hit. content-encoding/length are
# intentionally EXCLUDED (bodies are stored decoded). access-control-allow-origin
# is kept for cross-origin @font-face loads.
_STORE_HEADERS = (
    "content-type",
    "cache-control",
    "etag",
    "last-modified",
    "expires",
    "access-control-allow-origin",
)


def cache_root() -> Path:
    override = (os.environ.get("GROK_ASSET_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "grok-register" / "asset-cache"


def _key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]


def _should_cache(url: str, resource_type: str, method: str) -> bool:
    u = (url or "").lower()
    host_ok = any(h in u for h in CACHEABLE_HOSTS)
    return (
        host_ok
        and (method or "GET").upper() == "GET"
        and (resource_type or "").lower() in CACHEABLE_TYPES
    )


class AssetCache:
    """File cache with atomic writes (tmp + os.replace, never a torn file).

    Multi-worker writers on the same URL race harmlessly: last write wins on
    identical content; a partial file is never observed by readers.
    """

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or cache_root())
        self.root.mkdir(parents=True, exist_ok=True)

    def _paths(self, url: str) -> tuple[Path, Path]:
        k = _key(url)
        return self.root / f"{k}.body", self.root / f"{k}.meta.json"

    def get(self, url: str) -> Optional[dict[str, Any]]:
        body_path, meta_path = self._paths(url)
        if not body_path.is_file() or not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        ttl = float(meta.get("ttl") or DEFAULT_TTL)
        if time.time() - float(meta.get("stored_at") or 0) > ttl:
            return None
        return meta

    def put(self, url: str, body: bytes, headers: dict[str, str], ttl: float = DEFAULT_TTL) -> None:
        if not body or len(body) > DEFAULT_MAX_BODY:
            return
        # headers may be an immutable mapping — normalize to a dict
        try:
            hs = {str(k): str(v) for k, v in (headers or {}).items()}
        except Exception:
            return
        # Playwright's response.body() returns the DECODED body (verified:
        # ce=gzip but bytes start with plain 'var ...'). Store as-is; only
        # defensively gunzip when the bytes still carry gzip magic.
        if body[:2] == b"\x1f\x8b" and "gzip" in (hs.get("content-encoding") or "").lower():
            try:
                body = gzip.decompress(body)
            except Exception:
                return  # unreadable → don't cache
        body_path, meta_path = self._paths(url)
        try:
            tmp = body_path.with_suffix(".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, body_path)
            meta = {
                "url": url,
                "stored_at": time.time(),
                "ttl": ttl,
                "headers": {k: v for k, v in hs.items() if k.lower() in _STORE_HEADERS},
            }
            ptmp = meta_path.with_suffix(".tmp.json")
            ptmp.write_text(json.dumps(meta), encoding="utf-8")
            os.replace(ptmp, meta_path)
        except OSError:
            return  # cache write is best-effort, never block the flow

    def body(self, url: str) -> bytes:
        body_path, _ = self._paths(url)
        try:
            return body_path.read_bytes()
        except OSError:
            return b""

    def clear(self) -> int:
        n = 0
        for p in self.root.glob("*.body"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        for p in self.root.glob("*.meta.json"):
            try:
                p.unlink()
            except OSError:
                pass
        return n

    def stats(self) -> dict[str, Any]:
        entries = 0
        total = 0
        for p in self.root.glob("*.body"):
            entries += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return {"root": str(self.root), "entries": entries, "bytes": total}

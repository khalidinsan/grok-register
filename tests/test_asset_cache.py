"""Asset disk-cache tests (proxy-quota saver).

Defends the observable contract of asset_cache: what gets cached
(static GET assets on public hosts only), TTL expiry, and that replay
headers never include content-encoding (bodies are stored decoded).
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asset_cache import AssetCache, _should_cache  # noqa: E402


class ShouldCacheTests(unittest.TestCase):
    def test_static_get_on_cacheable_host(self):
        self.assertTrue(_should_cache("https://accounts.x.ai/_next/static/chunks/app.js", "script", "GET"))
        self.assertTrue(_should_cache("https://cdn.grok.com/font.woff2", "font", "GET"))
        self.assertTrue(_should_cache("https://grok.com/assets/logo.svg", "image", "GET"))

    def test_dynamic_requests_never_cache(self):
        self.assertFalse(_should_cache("https://accounts.x.ai/api/session", "xhr", "GET"))
        self.assertFalse(_should_cache("https://accounts.x.ai/sign-up", "document", "GET"))
        self.assertFalse(_should_cache("https://accounts.x.ai/api/otp", "fetch", "GET"))
        self.assertFalse(_should_cache("https://accounts.x.ai/_next/static/x.js", "script", "POST"))
        self.assertFalse(_should_cache("https://auth.x.ai/oauth2/authorize", "xhr", "GET"))
        self.assertFalse(_should_cache("https://cli-chat-proxy.grok.com/v1/responses", "fetch", "GET"))
        self.assertFalse(_should_cache("https://challenges.cloudflare.com/turnstile/v0/api.js", "script", "GET"))

    def test_other_hosts_not_cached(self):
        self.assertFalse(_should_cache("https://api.ipify.org/ip", "fetch", "GET"))
        self.assertFalse(_should_cache("https://www.googletagmanager.com/gtm.js", "script", "GET"))


class AssetCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = AssetCache(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_put_get_roundtrip_and_headers_sanitized(self):
        url = "https://accounts.x.ai/_next/static/chunks/app.js"
        headers = {
            "content-type": "application/javascript",
            "content-encoding": "gzip",  # must NOT be replayed
            "content-length": "999",  # stale after decode → must NOT be replayed
            "cache-control": "public, max-age=31536000, immutable",
            "set-cookie": "secret=1",  # must NOT be replayed
        }
        self.cache.put(url, b"console.log('hi');", headers)
        meta = self.cache.get(url)
        self.assertIsNotNone(meta)
        rh = meta["headers"]
        self.assertEqual(rh.get("content-type"), "application/javascript")
        self.assertEqual(rh.get("cache-control"), "public, max-age=31536000, immutable")
        self.assertNotIn("content-encoding", rh)
        self.assertNotIn("content-length", rh)
        self.assertNotIn("set-cookie", rh)
        self.assertEqual(self.cache.body(url), b"console.log('hi');")

    def test_gzip_magic_body_is_gunzipped(self):
        import gzip as gz

        url = "https://cdn.grok.com/chunk.js"
        raw = gz.compress(b"var a=1;")
        self.cache.put(url, raw, {"content-encoding": "gzip", "content-type": "application/javascript"})
        # stored decoded → replayable without content-encoding header
        self.assertEqual(self.cache.body(url), b"var a=1;")

    def test_ttl_expiry(self):
        import json

        url = "https://cdn.grok.com/img.png"
        self.cache.put(url, b"png", {}, ttl=1)
        self.assertIsNotNone(self.cache.get(url))
        # force expiry
        body_path, meta_path = self.cache._paths(url)
        meta = json.loads(meta_path.read_text())
        meta["stored_at"] = time.time() - 100
        meta_path.write_text(json.dumps(meta))
        self.assertIsNone(self.cache.get(url))

    def test_missing_entry_is_none(self):
        self.assertIsNone(self.cache.get("https://grok.com/never-seen.js"))

    def test_oversize_body_skipped(self):
        url = "https://grok.com/huge.js"
        self.cache.put(url, b"x" * (16 * 1024 * 1024), {})
        self.assertIsNone(self.cache.get(url))

    def test_clear_and_stats(self):
        self.cache.put("https://grok.com/a.js", b"aaa", {})
        self.cache.put("https://grok.com/b.js", b"bbb", {})
        st = self.cache.stats()
        self.assertEqual(st["entries"], 2)
        self.assertEqual(st["bytes"], 6)
        n = self.cache.clear()
        self.assertEqual(n, 2)
        self.assertEqual(self.cache.stats()["entries"], 0)


if __name__ == "__main__":
    unittest.main()

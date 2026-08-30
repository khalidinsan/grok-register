"""Asset-block decision tests for browser_engine.

Defends the observable contract of asset_block_rejects: which requests the
farm's browser route aborts (proxy quota) vs passes (signup/Turnstile auth).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from browser_engine import asset_block_rejects  # noqa: E402


class AssetBlockRejectsTests(unittest.TestCase):
    def test_critical_hosts_never_abort(self):
        for url, rtype in (
            ("https://challenges.cloudflare.com/turnstile/v/api.js", "script"),
            ("https://challenges.cloudflare.com/captcha.png", "image"),
            ("https://accounts.x.ai/_next/static/app.js", "script"),
            ("https://accounts.x.ai/avatar.png", "image"),
            ("https://auth.x.ai/oauth2/authorize", "xhr"),
            ("https://status.x.ai/api/status", "fetch"),
        ):
            self.assertFalse(asset_block_rejects(url, rtype), url)

    def test_drop_hosts_always_abort(self):
        for url in (
            "https://cdn.cookielaw.org/consent/s.js",
            "https://geolocation.onetrust.com/cookie",
            "https://js.stripe.com/v3",
            "https://ublockorigin.pages.dev/filter.txt",
            "https://pgl.yoyo.org/adservers/serverlist.php",
            "https://curbengh.github.io/hosts",
            "https://www.googletagmanager.com/gtm.js",
            "https://static.cloudflareinsights.com/beacon.min.js",
        ):
            self.assertTrue(asset_block_rejects(url, "script"), url)
            self.assertTrue(asset_block_rejects(url, "xhr"), url)

    def test_heavy_types_abort_outside_critical(self):
        for url, rtype in (
            ("https://cdn.grok.com/images/hero.webp", "image"),
            ("https://cdn.grok.com/fonts/inter.woff2", "font"),
            ("https://cdn.grok.com/assets/logo.svg", "image"),
            ("https://media.grok.com/video.mp4", "media"),
        ):
            self.assertTrue(asset_block_rejects(url, rtype), url)

    def test_ui_hosts_pass_scripts_and_xhr(self):
        for url in (
            "https://grok.com/_next/static/chunks/app.js",
            "https://cli-chat-proxy.grok.com/v1/responses",
            "https://auth.grok.com/api/session",
        ):
            self.assertFalse(asset_block_rejects(url, "script"), url)
            self.assertFalse(asset_block_rejects(url, "fetch"), url)

    def test_other_hosts_pass_non_heavy(self):
        # generic third-party scripts (not analytics/trackers) intentionally
        # pass — only heavy/media/font/image are quota waste.
        self.assertFalse(asset_block_rejects("https://www.gstatic.com/firebasejs/8.0/app.js", "script"))
        self.assertFalse(asset_block_rejects("https://unpkg.com/react@18/umd/react.js", "script"))
        # analytics beacons are blocked (added to drop hosts)
        self.assertTrue(asset_block_rejects("https://www.google-analytics.com/analytics.js", "script"))

    def test_empty_url_never_aborts(self):
        self.assertFalse(asset_block_rejects("", "script"))
        self.assertFalse(asset_block_rejects(None, "font"))


if __name__ == "__main__":
    unittest.main()

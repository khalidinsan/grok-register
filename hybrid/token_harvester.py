"""Browser-only token harvest for Castle / Turnstile (hybrid mode).

Adapted for grok-register's Camoufox / PwPageAdapter / DrissionPage page API:
  page.get, page.run_js, page.cookies, browser.cookies
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"

LogFn = Callable[[str], None]
PageGetter = Callable[[], Any]
BrowserGetter = Callable[[], Any]


@dataclass
class HarvestedTokens:
    turnstile: str = ""
    castle: str = ""
    page_url: str = ""
    cookies: dict = field(default_factory=dict)
    next_action: str = ""


class BrowserTokenSession:
    """Harvest castle / turnstile / cookies / next-action from a live page."""

    def __init__(
        self,
        *,
        page: Any = None,
        browser: Any = None,
        get_page: Optional[PageGetter] = None,
        get_browser: Optional[BrowserGetter] = None,
        open_signup_fn: Optional[Callable[[], None]] = None,
        get_turnstile_fn: Optional[Callable[..., str]] = None,
        log: Optional[LogFn] = None,
    ):
        self._page = page
        self._browser = browser
        self._get_page = get_page
        self._get_browser = get_browser
        self._open_signup_fn = open_signup_fn
        self._get_turnstile_fn = get_turnstile_fn
        self.log = log or (lambda _m: None)
        self._hooked = False

    def _lg(self, msg: str):
        try:
            self.log(msg)
        except Exception:
            pass

    def page(self) -> Any:
        if self._page is not None:
            return self._page
        if self._get_page:
            return self._get_page()
        return None

    def browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        if self._get_browser:
            return self._get_browser()
        return None

    def open_signup(self):
        if self._open_signup_fn:
            self._open_signup_fn()
            return
        page = self.page()
        if page is None:
            raise RuntimeError("no page for open_signup")
        try:
            page.get(SIGNUP_URL)
        except TypeError:
            page.get(SIGNUP_URL)
        # Best-effort click "Sign up with email"
        try:
            page.run_js(
                r"""
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = candidates.find((node) => {
  const text = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return text.includes('使用邮箱注册') || text.includes('signupwithemail')
    || text.includes('signupemail') || text.includes('signupwithemail')
    || (text.includes('email') && (text.includes('sign') || text.includes('register') || text.includes('注册')));
});
if (target) { target.click(); return true; }
return false;
"""
            )
        except Exception:
            pass
        time.sleep(0.8)

    def install_network_hook(self) -> bool:
        """Capture castleRequestToken from native React fetch/XHR bodies."""
        page = self.page()
        if page is None:
            return False
        try:
            res = page.run_js(
                r"""
(function(){
  if (window.__hybrid_net_hooked) return 'already';
  window.__hybrid_net_hooked = true;
  window.__hybrid_castles = [];
  window.__hybrid_castle = '';
  window.__hybrid_net = [];
  window.__hybrid_create_email_ok = false;
  window.__hybrid_create_email_status = 0;
  function captureBody(body, url) {
    try {
      if (!body) return;
      let s = '';
      if (typeof body === 'string') s = body;
      else if (body instanceof ArrayBuffer) s = new TextDecoder().decode(body);
      else if (body instanceof Uint8Array) s = new TextDecoder().decode(body);
      else return;
      const u = String(url||'');
      window.__hybrid_net.push({url: u, len: s.length});
      if (u.includes('CreateEmailValidationCode')) {
        window.__hybrid_create_email_seen = true;
      }
      if (s.includes('castleRequestToken')) {
        try {
          const j = JSON.parse(s);
          const tok = j && j[0] && j[0].castleRequestToken;
          if (tok && String(tok).length > 200) {
            window.__hybrid_castle = String(tok);
            window.__hybrid_castles.push(String(tok));
          }
        } catch (e) {
          const m = s.match(/castleRequestToken["']?\s*:\s*["']([^"']{200,})/);
          if (m) {
            window.__hybrid_castle = m[1];
            window.__hybrid_castles.push(m[1]);
          }
        }
      }
      const m2 = s.match(/IBYIll\|[A-Za-z0-9+/=|_-]{200,}/);
      if (m2) {
        window.__hybrid_castle = m2[0];
        window.__hybrid_castles.push(m2[0]);
      }
    } catch (e) {}
  }
  const ofetch = window.fetch;
  window.fetch = async function(input, init) {
    let url = '';
    try {
      url = (typeof input === 'string') ? input : (input && input.url) || '';
      captureBody(init && init.body, url);
    } catch (e) {}
    const resp = await ofetch.apply(this, arguments);
    try {
      if (String(url).includes('CreateEmailValidationCode')) {
        window.__hybrid_create_email_status = resp.status || 0;
        window.__hybrid_create_email_ok = !!(resp.ok || (resp.status >= 200 && resp.status < 300));
      }
    } catch (e) {}
    return resp;
  };
  const oopen = XMLHttpRequest.prototype.open;
  const osend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function(m,u){ this.__u=u; return oopen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function(body){
    captureBody(body, this.__u);
    const xhr = this;
    try {
      xhr.addEventListener('load', function(){
        try {
          if (String(xhr.__u||'').includes('CreateEmailValidationCode')) {
            window.__hybrid_create_email_status = xhr.status || 0;
            window.__hybrid_create_email_ok = xhr.status >= 200 && xhr.status < 300;
          }
        } catch (e) {}
      });
    } catch (e) {}
    return osend.apply(this, arguments);
  };
  return 'hooked';
})();
"""
            )
            self._hooked = True
            self._lg(f"[hybrid] net hook={res}")
            return True
        except Exception as e:
            self._lg(f"[hybrid] net hook fail: {e}")
            return False

    def create_email_sent_via_browser(self) -> bool:
        page = self.page()
        if page is None:
            return False
        try:
            data = page.run_js(
                """
return {
  ok: !!window.__hybrid_create_email_ok,
  status: Number(window.__hybrid_create_email_status||0),
  seen: !!window.__hybrid_create_email_seen,
  castle: (window.__hybrid_castle||'').length
};
"""
            )
            if isinstance(data, dict):
                if data.get("ok") or (
                    int(data.get("status") or 0) in (200, 0) and data.get("seen")
                ):
                    if data.get("ok") or int(data.get("status") or 0) == 200:
                        return True
                    if data.get("seen") and int(data.get("castle") or 0) > 1000:
                        return True
        except Exception:
            pass
        return bool(self.read_captured_castle())

    def browser_user_agent(self) -> str:
        page = self.page()
        if page is None:
            return ""
        try:
            ua = page.run_js("return navigator.userAgent || ''")
            return str(ua or "").strip()
        except Exception:
            return ""

    def read_captured_castle(self) -> str:
        page = self.page()
        if page is None:
            return ""
        try:
            data = page.run_js(
                """
const list = window.__hybrid_castles || [];
let best = window.__hybrid_castle || '';
for (const t of list) {
  if (String(t||'').length > String(best||'').length) best = t;
}
return {castle: String(best||''), n: list.length};
"""
            )
            if isinstance(data, dict):
                c = str(data.get("castle") or "")
                if len(c) >= 1000 and c.startswith("IBYIll"):
                    return c
                if len(c) >= 2000:
                    return c
        except Exception:
            pass
        return ""

    def harvest_castle_via_email_submit(self, email: str, timeout: int = 40) -> str:
        """Trigger React useCastle() by submitting email in UI; capture ~14KB token."""
        if not self._hooked:
            self.install_network_hook()
        page = self.page()
        if page is None:
            return ""
        try:
            page.run_js(
                "window.__hybrid_castle=''; window.__hybrid_castles=[]; "
                "window.__hybrid_net=[]; window.__hybrid_create_email_ok=false; "
                "window.__hybrid_create_email_status=0; "
                "window.__hybrid_create_email_seen=false; true;"
            )
        except Exception:
            pass
        try:
            r = page.run_js(
                """
const email = arguments[0];
function isVisible(node) {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  if (style.display === 'none' || style.visibility === 'hidden') return false;
  const rect = node.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
}
function setInputValue(input, v) {
  input.focus(); input.click();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker) tracker.setValue('');
  if (setter) setter.call(input, v); else input.value = v;
  input.dispatchEvent(new InputEvent('input', {bubbles:true, data:v, inputType:'insertText'}));
  input.dispatchEvent(new Event('change', {bubbles:true}));
}
const input = Array.from(document.querySelectorAll('input')).find((n) => {
  if (!isVisible(n)) return false;
  const meta = [n.type, n.name, n.id, n.placeholder, n.getAttribute('data-testid')].join(' ').toLowerCase();
  return meta.includes('email') || n.type === 'email';
});
if (!input) return 'no-input';
setInputValue(input, email);
const btn = Array.from(document.querySelectorAll('button')).find((n) => {
  if (!isVisible(n) || n.disabled) return false;
  const t = (n.innerText||'').replace(/\\s+/g,'');
  return t.includes('继续') || t.includes('注册') || t.includes('Continue')
    || t.includes('Sign up') || t.includes('Signup') || n.type==='submit';
});
if (btn) { btn.click(); return 'submitted'; }
return 'filled-no-button';
                """,
                email,
            )
            self._lg(f"[hybrid] UI email for castle: {r}")
        except Exception as e:
            self._lg(f"[hybrid] UI email castle: {e}")
            return ""

        deadline = time.time() + timeout
        while time.time() < deadline:
            c = self.read_captured_castle()
            if c:
                self._lg(f"[hybrid] native castle len={len(c)} head={c[:20]}")
                return c
            time.sleep(0.4)
        self._lg("[hybrid] native castle timeout; try injected SDK")
        return self.get_castle_token_injected(timeout=15)

    def export_cookies(self) -> dict:
        jar: dict = {}
        # Prefer browser context cookies (covers all domains)
        b = self.browser()
        if b is not None:
            try:
                if hasattr(b, "cookies") and callable(b.cookies):
                    cookies = b.cookies() or []
                elif hasattr(b, "_browser"):
                    cookies = []
                else:
                    cookies = []
                for c in cookies or []:
                    if isinstance(c, dict):
                        n, v = c.get("name", ""), c.get("value", "")
                    else:
                        n, v = getattr(c, "name", ""), getattr(c, "value", "")
                    if n:
                        jar[str(n)] = str(v)
            except Exception:
                pass
            # Playwright adapter: context cookies via page
            try:
                if not jar and hasattr(b, "latest_tab"):
                    p = b.latest_tab
                    cookies = p.cookies(all_domains=True, all_info=True) or []
                    for c in cookies or []:
                        if isinstance(c, dict) and c.get("name"):
                            jar[str(c["name"])] = str(c.get("value") or "")
            except Exception:
                pass

        page = self.page()
        if page is not None and not jar:
            try:
                cookies = page.cookies(all_domains=True, all_info=True) or page.cookies() or []
                for c in cookies or []:
                    if isinstance(c, dict):
                        n, v = c.get("name", ""), c.get("value", "")
                    else:
                        n, v = getattr(c, "name", ""), getattr(c, "value", "")
                    if n:
                        jar[str(n)] = str(v)
            except Exception as e:
                self._lg(f"[hybrid] export_cookies: {e}")
        return jar

    def scrape_next_action(self) -> str:
        page = self.page()
        if page is None:
            return ""
        try:
            action = page.run_js(
                r"""
const html = document.documentElement.innerHTML || '';
let m = html.match(/next-action["'\s:=]+([a-f0-9]{40,})/i);
if (m) return m[1];
for (const s of Array.from(document.scripts || [])) {
  const t = s.textContent || '';
  const idx = t.indexOf('createUserAndSession');
  if (idx >= 0) {
    const slice = t.slice(Math.max(0, idx - 300), idx + 400);
    const m3 = slice.match(/[a-f0-9]{40,}/);
    if (m3) return m3[0];
  }
  const idx2 = t.indexOf('emailValidationCode');
  if (idx2 >= 0) {
    const slice = t.slice(Math.max(0, idx2 - 400), idx2 + 400);
    const m4 = slice.match(/createServerReference\)?\(['"]([a-f0-9]{40,})['"]/);
    if (m4) return m4[1];
  }
}
return '';
"""
            )
            return str(action or "")
        except Exception:
            return ""

    def _extract_castle_pk(self) -> str:
        page = self.page()
        if page is None:
            return "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"
        try:
            pk = page.run_js(
                r"""
const html = document.documentElement.innerHTML || '';
const patterns = [
  /"castlePk":"([^"]+)"/,
  /castlePk\\":\\"([^\\"]+)/,
  /castlePk["']?\s*[:=]\s*["'](pk_[^"']+)/,
];
for (const p of patterns) {
  const m = html.match(p);
  if (m && m[1]) return m[1];
}
return '';
"""
            )
            if pk and str(pk).startswith("pk_"):
                return str(pk)
        except Exception as e:
            self._lg(f"[hybrid] castle pk: {e}")
        return "pk_p8GGWvD3TmFJZRsX3BQcqAv9aFVispNz"

    def _ensure_castle_sdk(self, pk: str) -> bool:
        page = self.page()
        if page is None:
            return False
        try:
            st = page.run_js(
                "return {s: window.__hybrid_castle_status||'', l:(window.__hybrid_castle||'').length};"
            )
            if isinstance(st, dict) and (
                st.get("s") == "done" or int(st.get("l") or 0) > 40
            ):
                return True
        except Exception:
            pass

        cdn = "https://cdn.jsdelivr.net/npm/@castleio/castle-js@2.1.8/dist/castle.min.js"
        try:
            page.run_js(
                f"""
window.__hybrid_castle = window.__hybrid_castle || '';
window.__hybrid_castle_status = 'loading-sdk';
window.__hybrid_castle_err = '';
(function(){{
  function mint(C) {{
    try {{
      var api = C;
      if (api && api.default) api = api.default;
      if (api && typeof api.configure === 'function') {{
        try {{ api.configure({{pk: {pk!r}}}); }} catch (e1) {{}}
      }}
      var fn = null;
      if (api && typeof api.createRequestToken === 'function') fn = api.createRequestToken.bind(api);
      if (!fn && typeof C === 'function') {{
        try {{
          var inst = C({{pk: {pk!r}}});
          if (inst && typeof inst.createRequestToken === 'function') fn = inst.createRequestToken.bind(inst);
        }} catch (e2) {{}}
      }}
      if (!fn) {{
        window.__hybrid_castle_status = 'no-method';
        return;
      }}
      window.__hybrid_castle_status = 'minting';
      Promise.resolve(fn()).then(function(t){{
        window.__hybrid_castle = String(t || '');
        window.__hybrid_castle_status = (window.__hybrid_castle.length > 20) ? 'done' : 'empty';
      }}).catch(function(e){{
        window.__hybrid_castle_err = String(e);
        window.__hybrid_castle_status = 'error';
      }});
    }} catch (e) {{
      window.__hybrid_castle_err = String(e);
      window.__hybrid_castle_status = 'exception';
    }}
  }}
  var existing = window.Castle || window.castle || null;
  if (existing) {{ mint(existing); return; }}
  if (window.__hybrid_castle_script) {{ return; }}
  window.__hybrid_castle_script = true;
  var s = document.createElement('script');
  s.src = {cdn!r};
  s.onload = function(){{
    var C = window.Castle || window.castle || null;
    mint(C);
  }};
  s.onerror = function(){{
    window.__hybrid_castle_err = 'sdk script load failed';
    window.__hybrid_castle_status = 'sdk-fail';
  }};
  document.head.appendChild(s);
}})();
true;
"""
            )
            return True
        except Exception as e:
            self._lg(f"[hybrid] ensure castle sdk: {e}")
            return False

    def get_castle_token_injected(self, timeout: int = 45) -> str:
        page = self.page()
        if page is None:
            return ""
        pk = self._extract_castle_pk()
        self._lg(f"[hybrid] castle pk={pk[:16]}...")
        self._ensure_castle_sdk(pk)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = page.run_js(
                    """
let castle = '';
try {
  if (window.__hybrid_castles && window.__hybrid_castles.length) {
    for (const t of window.__hybrid_castles) {
      if (String(t||'').length > String(castle||'').length) castle = String(t);
    }
  }
  if ((!castle || castle.length < 1000) && window.__hybrid_castle)
    castle = String(window.__hybrid_castle);
} catch (e) {}
return {castle: castle || '', status: String(window.__hybrid_castle_status || '')};
"""
                )
                if isinstance(data, dict):
                    castle = str(data.get("castle") or "")
                    if len(castle) >= 40:
                        self._lg(f"[hybrid] castle token len={len(castle)}")
                        return castle
            except Exception:
                pass
            time.sleep(0.5)
        self._lg("[hybrid] castle token timeout")
        return ""

    def _extract_turnstile_sitekey(self) -> str:
        page = self.page()
        if page is None:
            return "0x4AAAAAAAhr9JGVDZbrZOo0"
        try:
            sk = page.run_js(
                r"""
const html = document.documentElement.innerHTML || '';
const pats = [
  /"sitekey":"(0x4[^"]+)"/,
  /sitekey\\":\\"(0x4[^\\"]+)/,
  /sitekey["']?\s*[:=]\s*["'](0x4[^"']+)/i,
];
for (const p of pats) {
  const m = html.match(p);
  if (m && m[1]) return m[1];
}
const el = document.querySelector('[data-sitekey], .cf-turnstile');
if (el) {
  const v = el.getAttribute('data-sitekey') || '';
  if (v) return v;
}
return '';
"""
            )
            if sk and str(sk).startswith("0x"):
                return str(sk)
        except Exception:
            pass
        return "0x4AAAAAAAhr9JGVDZbrZOo0"

    def inject_turnstile_widget(self, sitekey: str = "") -> bool:
        page = self.page()
        if page is None:
            return False
        sk = (sitekey or self._extract_turnstile_sitekey()).strip()
        self._lg(f"[hybrid] turnstile sitekey={sk[:20]}...")
        try:
            page.run_js(
                f"""
window.__hybrid_turnstile = '';
window.__hybrid_turnstile_status = 'init';
(function(){{
  var sitekey = {sk!r};
  function renderWhenReady() {{
    if (!window.turnstile || typeof turnstile.render !== 'function') {{
      window.__hybrid_turnstile_status = 'waiting-api';
      return false;
    }}
    var host = document.getElementById('hybrid-turnstile-host');
    if (!host) {{
      host = document.createElement('div');
      host.id = 'hybrid-turnstile-host';
      host.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:2147483647;background:#111;padding:8px;';
      document.body.appendChild(host);
    }} else {{
      host.innerHTML = '';
    }}
    try {{
      turnstile.render(host, {{
        sitekey: sitekey,
        theme: 'dark',
        size: 'flexible',
        callback: function(token) {{
          window.__hybrid_turnstile = String(token || '');
          window.__hybrid_turnstile_status = 'done';
        }},
        'error-callback': function() {{ window.__hybrid_turnstile_status = 'error'; }},
        'expired-callback': function() {{ window.__hybrid_turnstile_status = 'expired'; }}
      }});
      window.__hybrid_turnstile_status = 'rendered';
      return true;
    }} catch (e) {{
      window.__hybrid_turnstile_status = 'render-fail';
      return false;
    }}
  }}
  if (renderWhenReady()) return;
  if (!document.getElementById('hybrid-cf-script')) {{
    var s = document.createElement('script');
    s.id = 'hybrid-cf-script';
    s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    s.async = true;
    s.onload = function(){{ renderWhenReady(); }};
    s.onerror = function(){{ window.__hybrid_turnstile_status = 'script-fail'; }};
    document.head.appendChild(s);
  }}
  var n = 0;
  var t = setInterval(function(){{
    n += 1;
    if (renderWhenReady() || n > 40) clearInterval(t);
  }}, 250);
}})();
true;
"""
            )
            return True
        except Exception as e:
            self._lg(f"[hybrid] inject turnstile: {e}")
            return False

    def _read_turnstile_js(self) -> dict:
        """Read token + status from page (native input, API, or inject host)."""
        page = self.page()
        if page is None:
            return {"tok": "", "status": "no-page"}
        try:
            tok = page.run_js(
                """
let tok = '';
try { if (window.__hybrid_turnstile) tok = String(window.__hybrid_turnstile); } catch (e) {}
if (!tok) {
  const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
  if (byInput) tok = byInput;
}
try {
  if (!tok && window.turnstile && typeof turnstile.getResponse === 'function') {
    tok = String(turnstile.getResponse() || '').trim();
  }
} catch (e) {}
return {
  tok: tok || '',
  status: String(window.__hybrid_turnstile_status || '')
};
"""
            )
            if isinstance(tok, dict):
                return {
                    "tok": str(tok.get("tok") or "").strip(),
                    "status": str(tok.get("status") or ""),
                }
            return {"tok": str(tok or "").strip(), "status": ""}
        except Exception:
            return {"tok": "", "status": "err"}

    def _nudge_native_turnstile(self) -> None:
        """Camoufox-safe: click iframe/widget via JS (no Drission shadow_root)."""
        page = self.page()
        if page is None:
            return
        try:
            page.run_js(
                """
(() => {
  try {
    const ifr = document.querySelector(
      'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'
    );
    if (ifr) { ifr.click(); return 'iframe'; }
    const w = document.querySelector('[data-sitekey], .cf-turnstile, div[id*="turnstile"]');
    if (w) { w.click(); return 'widget'; }
  } catch (e) {}
  return '';
})()
"""
            )
        except Exception:
            pass
        # Playwright frame click (Camoufox) — optional, best-effort
        try:
            raw = getattr(page, "raw", None) or page
            loop = getattr(page, "_loop", None)
            is_async = bool(getattr(page, "_async", False) or loop)

            def _click_frames(p):
                for fr in p.frames:
                    u = (fr.url or "").lower()
                    if "turnstile" not in u and "challenges.cloudflare" not in u:
                        continue
                    for sel in (
                        "input[type=checkbox]",
                        "label",
                        "body",
                        "#challenge-stage",
                        ".ctp-checkbox-label",
                    ):
                        try:
                            loc = fr.locator(sel).first
                            if loc.count() > 0:
                                loc.click(timeout=1500)
                                return True
                        except Exception:
                            continue
                return False

            if is_async and loop is not None:

                async def _go():
                    return _click_frames(raw)

                # frames/click may be sync on raw in some wrappers — try both
                try:
                    loop.run_until_complete(raw.evaluate("() => true"))
                except Exception:
                    pass
                try:
                    for fr in list(getattr(raw, "frames", []) or []):
                        u = str(getattr(fr, "url", "") or "").lower()
                        if "turnstile" not in u and "challenges.cloudflare" not in u:
                            continue
                        try:
                            loop.run_until_complete(
                                fr.locator("input[type=checkbox]").first.click(timeout=1500)
                            )
                            return
                        except Exception:
                            try:
                                loop.run_until_complete(fr.locator("body").click(timeout=1000))
                            except Exception:
                                pass
                except Exception:
                    pass
            else:
                _click_frames(raw)
        except Exception:
            pass

    def get_turnstile_token(self, timeout: int = 40, inject: bool = True) -> str:
        """
        Camoufox-first turnstile strategy (regkit-inspired, no Drission shadow):

          1) Poll native widget briefly + JS/iframe nudge (~8s)
          2) Inject standalone widget (regkit inject path) + poll remainder
          3) Skip getTurnstileToken/Drission shadow_root (burns ~45s on Camoufox)

        Opt-in legacy host solver: GROK_HYBRID_USE_DRISSION_TS=1
        """
        page = self.page()
        if page is None:
            return ""

        # Opt-in only: Drission shadow click path (Chromium + extension). Camoufox: skip.
        use_drission = (os.environ.get("GROK_HYBRID_USE_DRISSION_TS") or "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if use_drission and self._get_turnstile_fn:
            try:
                tok = self._get_turnstile_fn()
                if tok and len(str(tok)) >= 80:
                    self._lg(f"[hybrid] turnstile via host solver len={len(str(tok))}")
                    return str(tok)
            except Exception as e:
                self._lg(f"[hybrid] host turnstile skip: {e}")

        t0 = time.time()
        # Inject path succeeds in ~1s; keep native poll short (was ~10s dead wait)
        native_budget = min(4.0, max(2.0, float(timeout) * 0.1))
        # Phase A — native widget already on page (if any)
        self._lg("[hybrid] turnstile: native poll + nudge")
        while time.time() - t0 < native_budget:
            st = self._read_turnstile_js()
            val = st.get("tok") or ""
            if len(val) >= 80:
                self._lg(f"[hybrid] turnstile native len={len(val)}")
                return val
            self._nudge_native_turnstile()
            time.sleep(0.6)

        # Phase B — inject standalone (regkit style; works headless Camoufox)
        if inject:
            self._lg("[hybrid] turnstile: inject widget")
            self.inject_turnstile_widget()

        deadline = time.time() + max(5.0, timeout - (time.time() - t0))
        re_inject_at = time.time() + 12.0
        while time.time() < deadline:
            st = self._read_turnstile_js()
            val = st.get("tok") or ""
            if len(val) >= 80:
                self._lg(f"[hybrid] turnstile inject len={len(val)} status={st.get('status')}")
                return val
            status = st.get("status") or ""
            if status in ("script-fail", "render-fail", "error") or time.time() >= re_inject_at:
                if inject:
                    self.inject_turnstile_widget()
                re_inject_at = time.time() + 15.0
            self._nudge_native_turnstile()
            time.sleep(0.7)

        self._lg("[hybrid] turnstile timeout (native+inject)")
        return ""

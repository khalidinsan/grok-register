"""Normalize xAI SSO cookies (set-cookie chain wrapper → session JWT).

Port of grok-regkit/protocol/sso_util.py for grok-register.
"""
from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Callable, Optional

LogFn = Callable[[str], None]


def _noop_log(_: str) -> None:
    return None


def normalize_sso_value(value: str) -> str:
    """Strip sso= prefix, trailing attributes, and control characters."""
    value = (value or "").strip()
    if value.lower().startswith("sso="):
        value = value[4:].strip()
    if ";" in value and "sso=" not in value.lower():
        value = value.split(";", 1)[0].strip()
    return value.replace("\r", "").replace("\n", "").replace("\x00", "").strip()


def _b64json(segment: str) -> Optional[dict]:
    try:
        pad = "=" * ((4 - len(segment) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(segment + pad))
    except Exception:
        return None


def decode_jwt_payload(token: str) -> Optional[dict]:
    token = normalize_sso_value(token)
    parts = token.split(".")
    if len(parts) < 2:
        return None
    return _b64json(parts[1])


def is_wrapper_sso(token: str) -> bool:
    """True if token is set-cookie hop JWT (config.token + success_url), not session sso."""
    payload = decode_jwt_payload(token)
    if not isinstance(payload, dict):
        return False
    cfg = payload.get("config")
    if not isinstance(cfg, dict):
        return False
    return bool(cfg.get("success_url") and (cfg.get("token") or cfg.get("success_url")))


def is_session_sso(token: str) -> bool:
    """Heuristic: real session cookies are short JWTs with session claims, not set-cookie wrappers."""
    token = normalize_sso_value(token)
    if not token or len(token) < 40:
        return False
    if is_wrapper_sso(token):
        return False
    payload = decode_jwt_payload(token)
    if not isinstance(payload, dict):
        return False
    # historical session tokens often ~150 chars and start with eyJ0eXAi
    if token.startswith("eyJ0eXAi") or "session" in payload or "user" in payload:
        return True
    # any non-wrapper JWT of moderate length
    return 40 <= len(token) <= 800 and "config" not in payload


def unwrap_success_url(token: str) -> str:
    payload = decode_jwt_payload(token)
    if not isinstance(payload, dict):
        return ""
    cfg = payload.get("config") or {}
    return str(cfg.get("success_url") or "").strip()


def _page_set_cookie_js(page: Any, token: str, log: LogFn) -> None:
    """Inject sso/sso-rw via page JS (DrissionPage run_js or Playwright-like evaluate)."""
    js = """
const v = String(arguments[0] || '');
if (!v) return false;
document.cookie = 'sso=' + v + '; path=/; domain=.x.ai; Secure; SameSite=Lax';
document.cookie = 'sso-rw=' + v + '; path=/; domain=.x.ai; Secure; SameSite=Lax';
return true;
"""
    try:
        if hasattr(page, "run_js"):
            page.run_js(js, token)
            return
        if hasattr(page, "evaluate"):
            # Playwright-style: no arguments[] — close over token
            page.evaluate(
                """(v) => {
                  if (!v) return false;
                  document.cookie = 'sso=' + v + '; path=/; domain=.x.ai; Secure; SameSite=Lax';
                  document.cookie = 'sso-rw=' + v + '; path=/; domain=.x.ai; Secure; SameSite=Lax';
                  return true;
                }""",
                token,
            )
            return
    except Exception as e:
        log(f"[sso] inject cookie: {e}")


def _page_get(page: Any, url: str) -> None:
    if not hasattr(page, "get"):
        raise AttributeError("page has no .get()")
    try:
        page.get(url, timeout=30)
    except TypeError:
        page.get(url)


def _page_cookies(page: Any) -> list:
    if not hasattr(page, "cookies"):
        return []
    try:
        cookies = page.cookies(all_domains=True, all_info=True)
        if cookies is not None:
            return list(cookies) if not isinstance(cookies, list) else cookies
    except TypeError:
        pass
    except Exception:
        pass
    try:
        cookies = page.cookies()
        if cookies is None:
            return []
        if isinstance(cookies, dict):
            return [{"name": k, "value": v} for k, v in cookies.items()]
        return list(cookies)
    except Exception:
        return []


def materialize_sso_via_browser(
    page: Any,
    wrapper_or_sso: str,
    log: Optional[LogFn] = None,
    timeout: float = 45.0,
) -> str:
    """Use live browser tab to follow set-cookie chain and return session sso.

    Accepts a DrissionPage-like page or Playwright adapter with .get / .cookies / .run_js
    (or .evaluate).
    """
    log = log or _noop_log
    token = normalize_sso_value(wrapper_or_sso)
    if not token:
        return ""
    if is_session_sso(token) and not is_wrapper_sso(token):
        return token

    success = unwrap_success_url(token) if is_wrapper_sso(token) else ""
    _page_set_cookie_js(page, token, log)

    urls = []
    if success:
        urls.append(success)
    urls.append("https://accounts.x.ai/")
    urls.append("https://grok.com/")

    deadline = time.time() + timeout
    for url in urls:
        if time.time() >= deadline:
            break
        try:
            _page_get(page, url)
            time.sleep(1.2)
        except Exception as e:
            log(f"[sso] navigate {url[:60]}: {e}")
            continue
        for _ in range(12):
            if time.time() >= deadline:
                break
            cookies = _page_cookies(page)
            for item in cookies:
                if isinstance(item, dict):
                    name = str(item.get("name") or "")
                    value = str(item.get("value") or "")
                else:
                    name = str(getattr(item, "name", "") or "")
                    value = str(getattr(item, "value", "") or "")
                if name == "sso" and value and is_session_sso(value):
                    log(f"[sso] materialized session len={len(value)}")
                    return value
            time.sleep(0.5)

    # last try: read any sso even if still wrapper
    try:
        for item in _page_cookies(page):
            if isinstance(item, dict) and item.get("name") == "sso" and item.get("value"):
                return str(item.get("value"))
    except Exception:
        pass
    return token if is_session_sso(token) else ""


def _http_session(proxy: str = ""):
    """Prefer curl_cffi (Chrome TLS); fall back to requests."""
    try:
        from curl_cffi import requests as cf

        s = cf.Session()
        if proxy:
            s.proxies = {"http": proxy, "https": proxy}
        return s, True
    except Exception:
        import requests

        s = requests.Session()
        if proxy:
            s.proxies.update({"http": proxy, "https": proxy})
        return s, False


def _set_cookie_domains(session: Any, name: str, value: str, domains: tuple[str, ...]) -> None:
    for d in domains:
        try:
            session.cookies.set(str(name), str(value), domain=d)
        except Exception:
            try:
                session.cookies.set(str(name), str(value), domain=d, path="/")
            except Exception:
                pass


def materialize_sso_via_http(
    wrapper: str,
    *,
    proxy: str = "",
    cookies_jar: Optional[Any] = None,
    extra_cookies: Optional[dict] = None,
    log: Optional[LogFn] = None,
    timeout: float = 30.0,
) -> str:
    """Best-effort pure HTTP exchange (often needs fresh CF cookies).

    cookies_jar: dict or list of cookie dicts (cf_clearance, __cf_bm, sso-rw, ...).
    extra_cookies: legacy alias for a plain name→value dict (merged into jar).
    Uses curl_cffi if available, else requests.
    """
    log = log or _noop_log
    wrapper = normalize_sso_value(wrapper)
    if not is_wrapper_sso(wrapper):
        return wrapper if is_session_sso(wrapper) else ""

    success = unwrap_success_url(wrapper)
    if not success:
        return ""

    try:
        s, use_impersonate = _http_session(proxy)
    except Exception as e:
        log(f"[sso] http session create failed: {e}")
        return ""

    domains = (".x.ai", "accounts.x.ai", "auth.x.ai", ".grok.com")

    # Merge jar sources
    items: list[dict] = []
    if isinstance(cookies_jar, dict):
        items.extend({"name": k, "value": v} for k, v in cookies_jar.items())
    elif isinstance(cookies_jar, (list, tuple)):
        items.extend(c for c in cookies_jar if isinstance(c, dict))
    if isinstance(extra_cookies, dict):
        items.extend({"name": k, "value": v} for k, v in extra_cookies.items())

    for c in items:
        name = str(c.get("name") or c.get("Name") or "").strip()
        value = c.get("value") if "value" in c else c.get("Value")
        if not name or value is None:
            continue
        _set_cookie_domains(s, name, str(value), domains)

    for d in (".x.ai", "accounts.x.ai", "auth.x.ai"):
        try:
            s.cookies.set("sso", wrapper, domain=d)
            s.cookies.set("sso-rw", wrapper, domain=d)
        except Exception:
            pass

    url = success
    for hop in range(8):
        try:
            if use_impersonate:
                try:
                    r = s.get(url, impersonate="chrome131", timeout=timeout, allow_redirects=True)
                except TypeError:
                    r = s.get(url, timeout=timeout, allow_redirects=True)
            else:
                r = s.get(url, timeout=timeout, allow_redirects=True)
        except Exception as e:
            log(f"[sso] hop {hop} fail: {e}")
            break

        # inspect jar for short session sso
        try:
            jar = getattr(s.cookies, "jar", None)
            if jar is not None:
                for c in jar:
                    if getattr(c, "name", None) == "sso" and c.value and is_session_sso(c.value):
                        log(f"[sso] http materialize len={len(c.value)}")
                        return c.value
            else:
                jar_dict = dict(s.cookies)
                for name in ("sso", "sso-rw"):
                    v = jar_dict.get(name) or ""
                    if is_session_sso(v):
                        log(f"[sso] http materialize len={len(v)}")
                        return v
        except Exception:
            try:
                jar_dict = dict(s.cookies)
                for name in ("sso", "sso-rw"):
                    v = jar_dict.get(name) or ""
                    if is_session_sso(v):
                        return v
            except Exception:
                pass

        final = getattr(r, "url", "") or ""
        if "sign-in" in final or "auth-error" in final:
            log(f"[sso] http landed {final[:100]}")
            break
        # follow nested success_url if still wrapper in location/body
        m = re.search(
            r"https://auth\.[^\s\"']+set-cookie\?q=[^\s\"']+",
            getattr(r, "text", "") or "",
        )
        if m:
            url = m.group(0)
            continue
        break
    return ""


if __name__ == "__main__":
    # Unit-style smoke (no network): fake JWT payloads for wrapper vs session detection.
    def _fake_jwt(payload: dict) -> str:
        header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        return f"{header}.{body}.sig"

    wrapper = _fake_jwt(
        {
            "config": {
                "token": "inner",
                "success_url": "https://auth.x.ai/set-cookie?q=abc",
            }
        }
    )
    session = _fake_jwt({"session": {"id": "abc"}, "user": "u1", "sub": "user-1"})
    bare = "not-a-jwt"
    short = _fake_jwt({"foo": 1})  # may be short

    assert is_wrapper_sso(wrapper), "wrapper JWT should be detected"
    assert not is_session_sso(wrapper), "wrapper must not count as session"
    assert unwrap_success_url(wrapper).startswith("https://auth.x.ai/")
    assert is_session_sso(session), "session JWT should be detected"
    assert not is_wrapper_sso(session), "session must not count as wrapper"
    assert decode_jwt_payload(wrapper) is not None
    assert normalize_sso_value("sso=" + session) == session
    assert not is_wrapper_sso(bare)
    assert not is_session_sso(bare)
    # short non-wrapper without session/user claims still may pass length heuristic
    _ = is_session_sso(short)
    print("sso_util smoke OK")
    print(f"  wrapper len={len(wrapper)} is_wrapper={is_wrapper_sso(wrapper)}")
    print(f"  session len={len(session)} is_session={is_session_sso(session)}")

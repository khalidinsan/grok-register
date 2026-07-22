"""
Convert Grok Web SSO cookie → Grok Build / CLI OAuth tokens via Device Flow.

Port of grok2api backend/internal/infra/provider/web/sso_build.go (HTTP-only, no browser).

Flow:
  1. Materialize wrapper JWT → session SSO (if needed)
  2. Validate SSO session on accounts.x.ai
  3. Start device code (client_id = Grok CLI / Build)
  4. Open verification URI + verify user_code + approve (with full cookie jar)
  5. Poll token endpoint → access_token + refresh_token
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests

from sso_util import (
    is_session_sso,
    is_wrapper_sso,
    materialize_sso_via_browser,
    materialize_sso_via_http,
    normalize_sso_value,
)

CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = (
    "openid profile email offline_access "
    "grok-cli:access api:access conversations:read conversations:write"
)
ACCOUNTS_URL = "https://accounts.x.ai/"
DEVICE_URL = "https://auth.x.ai/oauth2/device/code"
VERIFY_URL = "https://auth.x.ai/oauth2/device/verify"
APPROVE_URL = "https://auth.x.ai/oauth2/device/approve"
TOKEN_URL = "https://auth.x.ai/oauth2/token"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)

# Domains that need SSO / CF cookies for device OAuth
_COOKIE_DOMAINS = (".x.ai", "accounts.x.ai", "auth.x.ai")

# Rate-limit / slow-down retry. Fail-fast default 2 (env GROK_DEVICE_RL_MAX_TRIES).
# Was 4 with sleep up to 120s → ~7min hangs on dead SSO. Prefer short + fail.
def _rl_max_tries_default() -> int:
    try:
        return max(1, min(6, int(os.environ.get("GROK_DEVICE_RL_MAX_TRIES") or "2")))
    except (TypeError, ValueError):
        return 2


def _poll_timeout_default() -> float:
    try:
        return max(15.0, float(os.environ.get("GROK_DEVICE_POLL_TIMEOUT_SEC") or "45"))
    except (TypeError, ValueError):
        return 45.0


_RL_MAX_TRIES = 2  # module default; SsoBuildFlow can override


@dataclass
class BuildTokens:
    access_token: str
    refresh_token: str
    id_token: str
    expires_at: str
    expires_in: int
    email: str
    user_id: str
    team_id: str
    name: str
    scope: str = SCOPE


def normalize_sso_token(value: str) -> str:
    return normalize_sso_value(value)


def extract_sso_from_credential(raw: Any) -> Tuple[str, str]:
    """Return (sso, sso_rw) from string or dict credential."""
    sso, sso_rw = "", ""
    if isinstance(raw, dict):
        header = str(raw.get("apiKey") or raw.get("cookie_header") or "")
        sso = str(raw.get("sso_token") or raw.get("sso") or "").strip()
        sso_rw = str(raw.get("sso_rw") or raw.get("sso-rw") or "").strip()
        if header:
            for part in header.split(";"):
                part = part.strip()
                low = part.lower()
                if low.startswith("sso=") and not low.startswith("sso-rw="):
                    sso = part.split("=", 1)[1].strip()
                elif low.startswith("sso-rw="):
                    sso_rw = part.split("=", 1)[1].strip()
        if not sso and raw.get("token"):
            sso = normalize_sso_token(str(raw.get("token")))
    else:
        text = str(raw or "")
        if "sso=" in text.lower() or "sso-rw=" in text.lower():
            for part in text.split(";"):
                part = part.strip()
                low = part.lower()
                if low.startswith("sso=") and not low.startswith("sso-rw="):
                    sso = part.split("=", 1)[1].strip()
                elif low.startswith("sso-rw="):
                    sso_rw = part.split("=", 1)[1].strip()
        else:
            sso = normalize_sso_token(text)
    sso = normalize_sso_token(sso)
    sso_rw = normalize_sso_token(sso_rw or sso)
    return sso, sso_rw


def _cookies_to_items(cookies: Any) -> List[Dict[str, Any]]:
    """Normalize dict / list / cookie-header string into [{name,value,...}]."""
    if not cookies:
        return []
    if isinstance(cookies, dict):
        # Could be name→value OR a single cookie dict with "name"/"value"
        if "name" in cookies and ("value" in cookies or "Value" in cookies):
            return [cookies]
        return [{"name": k, "value": v} for k, v in cookies.items() if k]
    if isinstance(cookies, (list, tuple)):
        out: List[Dict[str, Any]] = []
        for c in cookies:
            if isinstance(c, dict):
                out.append(c)
            elif isinstance(c, str) and "=" in c:
                n, v = c.split("=", 1)
                out.append({"name": n.strip(), "value": v.strip()})
        return out
    if isinstance(cookies, str):
        items = []
        for part in cookies.split(";"):
            part = part.strip()
            if "=" in part:
                n, v = part.split("=", 1)
                items.append({"name": n.strip(), "value": v.strip()})
        return items
    return []


def _looks_rate_limited(status: int, body: bytes | str = b"", err_text: str = "") -> bool:
    text = err_text
    if isinstance(body, bytes):
        text = (text + " " + body.decode("utf-8", errors="replace")).lower()
    else:
        text = (text + " " + str(body or "")).lower()
    if status == 429:
        return True
    return any(
        k in text
        for k in (
            "rate_limit",
            "rate limit",
            "ratelimit",
            "slow_down",
            "too many requests",
            "try again later",
        )
    )


def _rl_sleep(attempt: int, *, cap: float = 25.0) -> None:
    """attempt is 1-based try index after a failure. Fail-fast: cap ~25s (was 120)."""
    delay = min(8 * attempt, cap)
    print(
        f"[Build] rate-limit / slow_down — backoff {delay:.0f}s (attempt {attempt})",
        flush=True,
    )
    time.sleep(delay)


def _safe_xai_url(raw: str) -> bool:
    try:
        p = urlparse(raw)
    except Exception:
        return False
    if p.scheme != "https" or p.username or p.password or not p.hostname:
        return False
    host = p.hostname.lower()
    return host == "x.ai" or host.endswith(".x.ai")


def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        pad = parts[1] + "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(pad))
    except Exception:
        return {}


class SsoBuildFlow:
    def __init__(
        self,
        sso: str,
        sso_rw: str | None = None,
        user_agent: str = USER_AGENT,
        proxy: str | None = None,
        cookies: Any = None,
        *,
        max_rl_tries: int | None = None,
        poll_timeout_sec: float | None = None,
    ):
        self.user_agent = user_agent
        self.session = requests.Session()
        self._max_rl = (
            max(1, int(max_rl_tries))
            if max_rl_tries is not None
            else _rl_max_tries_default()
        )
        self._poll_timeout = (
            float(poll_timeout_sec)
            if poll_timeout_sec is not None
            else _poll_timeout_default()
        )
        # Optional HTTP(S) proxy. Pass proxy="" to force direct.
        if proxy is None:
            proxy = (os.environ.get("GROK_BROWSER_PROXY") or "").strip()
        self._proxy = (proxy or "").strip()
        if self._proxy:
            self.session.proxies.update({"http": self._proxy, "https": self._proxy})
        self.session.headers.update(
            {
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "User-Agent": user_agent,
            }
        )
        # Full jar first (cf_clearance, __cf_bm, ...), then force SSO on top
        self._set_extra_cookies(cookies)
        self._set_sso_cookies(sso, sso_rw or sso)

    def _set_cookie_all_domains(self, name: str, value: str, path: str = "/") -> None:
        for domain in _COOKIE_DOMAINS:
            try:
                self.session.cookies.set(name, value, domain=domain, path=path)
            except Exception:
                try:
                    self.session.cookies.set(name, value, domain=domain)
                except Exception:
                    pass

    def _set_sso_cookies(self, sso: str, sso_rw: str) -> None:
        sso = normalize_sso_token(sso)
        sso_rw = normalize_sso_token(sso_rw or sso)
        if not sso:
            return
        self._set_cookie_all_domains("sso", sso)
        self._set_cookie_all_domains("sso-rw", sso_rw or sso)

    def _set_extra_cookies(self, cookies: Any) -> int:
        """Inject full jar (cf_clearance etc.) — SSO alone often fails CF on device/verify."""
        n = 0
        for c in _cookies_to_items(cookies):
            name = str(c.get("name") or c.get("Name") or "").strip()
            value = c.get("value") if "value" in c else c.get("Value")
            if not name or value is None:
                continue
            # Skip overwriting sso here; _set_sso_cookies runs after
            path = str(c.get("path") or c.get("Path") or "/").strip() or "/"
            domain_hint = str(c.get("domain") or c.get("Domain") or "").strip()
            domains = list(_COOKIE_DOMAINS)
            if domain_hint and domain_hint not in domains:
                domains.insert(0, domain_hint)
            for d in domains:
                try:
                    self.session.cookies.set(name, str(value), domain=d, path=path)
                    n += 1
                    break
                except Exception:
                    try:
                        self.session.cookies.set(name, str(value), domain=d)
                        n += 1
                        break
                    except Exception:
                        continue
        return n

    def do(
        self,
        method: str,
        url: str,
        form: Optional[Dict[str, str]] = None,
        timeout: float = 30,
    ) -> Tuple[int, str, bytes]:
        if not _safe_xai_url(url):
            raise ValueError(f"unsafe xAI URL: {url}")

        current_url = url
        current_method = method.upper()
        current_form = form
        for _ in range(9):
            kwargs: Dict[str, Any] = {
                "method": current_method,
                "url": current_url,
                "timeout": timeout,
                "allow_redirects": False,
            }
            if current_form is not None:
                kwargs["data"] = current_form
                kwargs["headers"] = {"Content-Type": "application/x-www-form-urlencoded"}

            resp = self.session.request(**kwargs)
            status = resp.status_code
            body = resp.content[: (2 << 20) + 1]
            if len(body) > (2 << 20):
                raise RuntimeError("xAI OAuth response too large")

            if 300 <= status <= 399:
                location = (resp.headers.get("Location") or "").strip()
                if not location:
                    raise RuntimeError(f"redirect missing Location (HTTP {status})")
                next_url = urljoin(current_url, location)
                if not _safe_xai_url(next_url):
                    raise RuntimeError(f"redirect to untrusted host: {next_url}")
                if status == 303 or (
                    status in (301, 302) and current_method not in ("GET", "HEAD")
                ):
                    current_method = "GET"
                    current_form = None
                current_url = next_url
                continue

            return status, current_url, body

        raise RuntimeError("too many redirects")

    def _do_with_rl_retry(
        self,
        method: str,
        url: str,
        form: Optional[Dict[str, str]] = None,
        *,
        label: str = "request",
        timeout: float = 30,
    ) -> Tuple[int, str, bytes]:
        """HTTP call with exponential backoff on 429 / rate_limit / slow_down."""
        last: Tuple[int, str, bytes] = (0, url, b"")
        max_tries = getattr(self, "_max_rl", None) or _rl_max_tries_default()
        for attempt in range(1, max_tries + 1):
            status, final_url, body = self.do(method, url, form=form, timeout=timeout)
            last = (status, final_url, body)
            if not _looks_rate_limited(status, body):
                return status, final_url, body
            if attempt >= max_tries:
                break
            _rl_sleep(attempt)
        return last

    def convert(self, name_hint: str = "") -> BuildTokens:
        print("[Build] Validating Web SSO on accounts.x.ai ...")
        status, final_url, body = self._do_with_rl_retry("GET", ACCOUNTS_URL, label="accounts")
        if status == 401 or "sign-in" in final_url or "sign-up" in final_url:
            raise RuntimeError("SSO unauthorized / redirected to sign-in")
        if status < 200 or status >= 400:
            if _looks_rate_limited(status, body):
                raise RuntimeError(f"SSO check rate-limited HTTP {status} url={final_url}")
            raise RuntimeError(f"SSO check failed HTTP {status} url={final_url}")

        print("[Build] Starting device code ...")
        status, _, body = self._do_with_rl_retry(
            "POST",
            DEVICE_URL,
            {"client_id": CLIENT_ID, "scope": SCOPE, "referrer": "grok-build"},
            label="device_code",
        )
        if status < 200 or status >= 300:
            raise RuntimeError(f"device code failed HTTP {status}: {body[:200]!r}")
        device = json.loads(body.decode("utf-8", errors="replace"))
        device_code = device.get("device_code") or ""
        user_code = device.get("user_code") or ""
        verify_complete = device.get("verification_uri_complete") or ""
        interval = int(device.get("interval") or 5)
        expires_in = int(device.get("expires_in") or 1800)
        if not device_code or not user_code or not _safe_xai_url(verify_complete):
            raise RuntimeError(f"incomplete device response: {device}")

        print(f"[Build] user_code={user_code}")

        status, final_url, body = self._do_with_rl_retry(
            "GET", verify_complete, label="verify_uri"
        )
        if status < 200 or status >= 400:
            raise RuntimeError(f"open verify page failed HTTP {status}")

        print("[Build] Verifying user_code with SSO ...")
        status, final_url, body = self._do_with_rl_retry(
            "POST", VERIFY_URL, {"user_code": user_code}, label="verify"
        )
        if status < 200 or status >= 400:
            raise RuntimeError(f"verify failed HTTP {status} url={final_url}")
        if "consent" not in final_url:
            print(f"[Build] warn: verify final_url={final_url} (expected consent)")

        print("[Build] Approving device flow ...")
        status, final_url, body = self._do_with_rl_retry(
            "POST",
            APPROVE_URL,
            {
                "user_code": user_code,
                "action": "allow",
                "principal_type": "User",
                "principal_id": "",
            },
            label="approve",
        )
        if status < 200 or status >= 400:
            raise RuntimeError(f"approve failed HTTP {status} url={final_url}")
        if "done" not in final_url:
            print(f"[Build] warn: approve final_url={final_url}")

        print("[Build] Polling OAuth token ...")
        if interval < 1:
            interval = 1
        poll_cap = float(getattr(self, "_poll_timeout", None) or _poll_timeout_default())
        deadline = time.time() + min(expires_in, poll_cap)
        rl_poll_tries = 0
        max_rl = getattr(self, "_max_rl", None) or _rl_max_tries_default()
        while time.time() < deadline:
            time.sleep(interval)
            status, _, body = self.do(
                "POST",
                TOKEN_URL,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": CLIENT_ID,
                    "device_code": device_code,
                },
            )
            try:
                payload = json.loads(body.decode("utf-8", errors="replace"))
            except Exception as e:
                raise RuntimeError(f"token parse error: {e} body={body[:200]!r}") from e

            if 200 <= status < 300 and payload.get("access_token"):
                access = str(payload["access_token"])
                refresh = str(payload.get("refresh_token") or "").strip()
                if not refresh:
                    raise RuntimeError(
                        "token response missing refresh_token "
                        "(offline_access / Build scope required)"
                    )
                exp_in = int(payload.get("expires_in") or 3600)
                id_token = payload.get("id_token") or ""
                claims = _decode_jwt_claims(id_token or access)
                email = str(claims.get("email") or "").strip()
                user_id = str(claims.get("sub") or "").strip()
                team_id = str(claims.get("team_id") or "").strip()
                expires_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + exp_in)
                )
                name = email or name_hint or user_id or "Grok Build account"
                print(f"[Build] OAuth OK email={email or '-'} expires_in={exp_in}s")
                return BuildTokens(
                    access_token=access,
                    refresh_token=refresh,
                    id_token=id_token,
                    expires_at=expires_at,
                    expires_in=exp_in,
                    email=email,
                    user_id=user_id,
                    team_id=team_id,
                    name=name,
                    scope=str(payload.get("scope") or SCOPE),
                )

            err = str(payload.get("error") or "")
            if err == "authorization_pending":
                continue
            if err == "slow_down" or _looks_rate_limited(status, body, err):
                rl_poll_tries += 1
                if rl_poll_tries > max_rl:
                    raise RuntimeError(
                        f"token poll rate-limited after {max_rl} tries: "
                        f"{payload.get('error_description') or err or body[:200]!r}"
                    )
                interval += 2
                _rl_sleep(rl_poll_tries)
                continue
            if err in ("access_denied", "expired_token"):
                raise RuntimeError(f"authorization denied: {err}")
            if status >= 400:
                raise RuntimeError(
                    f"token failed HTTP {status}: "
                    f"{payload.get('error_description') or err or body[:200]!r}"
                )
            raise RuntimeError(f"token failed: {payload}")

        raise RuntimeError("device flow poll timeout")


def _maybe_materialize_sso(
    sso: str,
    *,
    proxy: str | None,
    cookies: Any,
    page: Any,
) -> str:
    """If sso is a set-cookie wrapper JWT, exchange it for a session cookie."""
    sso = normalize_sso_token(sso)
    if not sso or not is_wrapper_sso(sso):
        return sso

    print("[Build] SSO looks like set-cookie wrapper — materializing session ...", flush=True)
    log = lambda m: print(m, flush=True)  # noqa: E731

    # Prefer live page if provided
    if page is not None:
        try:
            out = materialize_sso_via_browser(page, sso, log=log, timeout=45.0)
            if out and is_session_sso(out):
                print(f"[Build] browser materialize OK len={len(out)}", flush=True)
                return out
            if out:
                print(
                    f"[Build] browser materialize returned non-session len={len(out)}",
                    flush=True,
                )
        except Exception as e:
            print(f"[Build] browser materialize failed: {e}", flush=True)

    px = ""
    if proxy is not None:
        px = (proxy or "").strip()
    else:
        px = (os.environ.get("GROK_BROWSER_PROXY") or "").strip()

    jar = cookies
    if jar is None:
        jar = {}
    try:
        out = materialize_sso_via_http(
            sso,
            proxy=px,
            cookies_jar=jar,
            log=log,
            timeout=30.0,
        )
        if out and is_session_sso(out):
            print(f"[Build] http materialize OK len={len(out)}", flush=True)
            return out
    except Exception as e:
        print(f"[Build] http materialize failed: {e}", flush=True)

    # Fall through with original; device flow may still fail clearly
    print("[Build] warn: could not materialize wrapper; trying device flow as-is", flush=True)
    return sso


def convert_sso_to_build(
    sso_credential: Any,
    name_hint: str = "",
    *,
    proxy: str | None = None,
    fallback_direct: bool = True,
    cookies: Any = None,
    page: Any = None,
    max_rl_tries: int | None = None,
    poll_timeout_sec: float | None = None,
) -> BuildTokens:
    """
    Convert Web SSO → Build tokens (requires access_token + refresh_token).

    proxy=None → use GROK_BROWSER_PROXY env (browser account proxy).
    proxy=""   → force direct (no proxy).
    fallback_direct: if proxied convert hits 429 / proxy errors, retry once direct.
    cookies: optional full jar (dict or list) with cf_clearance, __cf_bm, sso-rw, etc.
    page: optional DrissionPage / Playwright-like handle for wrapper materialize.
    max_rl_tries / poll_timeout_sec: fail-fast knobs (defaults from env, ~2 tries / 45s).
    """
    sso, sso_rw = extract_sso_from_credential(sso_credential)
    if not sso:
        # Try pulling sso from cookies jar
        for c in _cookies_to_items(cookies):
            name = str(c.get("name") or c.get("Name") or "")
            value = c.get("value") if "value" in c else c.get("Value")
            if name == "sso" and value:
                sso = normalize_sso_token(str(value))
            elif name in ("sso-rw", "sso_rw") and value and not sso_rw:
                sso_rw = normalize_sso_token(str(value))
    if not sso:
        raise ValueError("empty SSO token")

    sso = _maybe_materialize_sso(sso, proxy=proxy, cookies=cookies, page=page)
    sso_rw = normalize_sso_token(sso_rw or sso)

    def _run(px: str | None) -> BuildTokens:
        return SsoBuildFlow(
            sso=sso,
            sso_rw=sso_rw,
            proxy=px,
            cookies=cookies,
            max_rl_tries=max_rl_tries,
            poll_timeout_sec=poll_timeout_sec,
        ).convert(name_hint=name_hint)

    try:
        return _run(proxy)
    except Exception as e:
        err = str(e).lower()
        used_proxy = (
            (proxy if proxy is not None else os.environ.get("GROK_BROWSER_PROXY") or "")
            .strip()
        )
        proxy_fail = any(
            k in err
            for k in (
                "proxyerror",
                "unable to connect to proxy",
                "tunnel connection failed",
                "429",
                "too many requests",
                "proxy connection",
                "rate-limited",
                "rate_limit",
                "slow_down",
            )
        )
        if fallback_direct and used_proxy and proxy_fail:
            # Datacenter proxies often 429 on auth.x.ai — browser OK, HTTP convert not.
            print(
                f"[*] convert via proxy failed ({e!s:.120}) — retry DIRECT (no proxy)",
                flush=True,
            )
            return _run("")
        raise


if __name__ == "__main__":
    import sys

    # Lightweight unit checks (no network) when run with --self-test
    if len(sys.argv) >= 2 and sys.argv[1] == "--self-test":
        from sso_util import decode_jwt_payload, unwrap_success_url

        def _fake_jwt(payload: dict) -> str:
            header = (
                base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}')
                .decode()
                .rstrip("=")
            )
            body = (
                base64.urlsafe_b64encode(
                    json.dumps(payload, separators=(",", ":")).encode()
                )
                .decode()
                .rstrip("=")
            )
            return f"{header}.{body}.sig"

        wrapper = _fake_jwt(
            {
                "config": {
                    "token": "inner",
                    "success_url": "https://auth.x.ai/set-cookie?q=abc",
                }
            }
        )
        session = _fake_jwt({"session": {"id": "abc"}, "user": "u1"})
        assert is_wrapper_sso(wrapper) and not is_session_sso(wrapper)
        assert is_session_sso(session) and not is_wrapper_sso(session)
        assert unwrap_success_url(wrapper).startswith("https://auth.x.ai/")
        assert decode_jwt_payload(wrapper) is not None
        assert normalize_sso_token("sso=" + session) == session
        # cookie jar helper
        items = _cookies_to_items(
            {"cf_clearance": "x", "sso": session, "__cf_bm": "y"}
        )
        assert len(items) == 3
        assert _looks_rate_limited(429)
        assert _looks_rate_limited(200, b'{"error":"slow_down"}')
        assert not _looks_rate_limited(200, b'{"error":"authorization_pending"}')
        print("sso_to_build self-test OK")
        sys.exit(0)

    if len(sys.argv) < 2:
        print("Usage: python sso_to_build.py <sso-jwt-or-header-file>")
        print("       python sso_to_build.py --self-test")
        sys.exit(1)
    raw = open(sys.argv[1], encoding="utf-8").read().strip()
    tok = convert_sso_to_build(raw)
    print(
        json.dumps(
            {
                "email": tok.email,
                "user_id": tok.user_id,
                "expires_at": tok.expires_at,
                "access_token_prefix": tok.access_token[:40] + "...",
                "has_refresh": bool(tok.refresh_token),
            },
            indent=2,
        )
    )

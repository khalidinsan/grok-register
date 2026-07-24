#!/usr/bin/env python3
"""
Login → OAuth (Grok Build) → chat probe → inject 9router (usable only).

Starts from existing accounts (email:password), NOT register.

  email_pass.txt   →  inventory (email:password dump)
  accounts.jsonl   →  ledger (password + status per email; last row wins)

Default queue = email_pass minus emails whose jsonl status is already terminal
(so next run tidak bawa ulang akun yang sudah diproses).

  → Camoufox login accounts.x.ai
  → activate grok.com (optional)
  → device OAuth / PKCE
  → probe cli-chat-proxy
  → push 9router if usable
  → upsert status + password ke accounts.jsonl

Status (dipakai filter & skip):
  pending       belum dicoba (import only)
  created       dari register farm
  login_ok      login berhasil, OAuth belum selesai
  failed_login  password / form login gagal
  failed_oauth  login OK, grant/token ditolak
  failed_probe  token ada tapi chat 402/403
  usable        probe OK
  injected      push 9router OK
  failed_push   usable tapi inject gagal
  oauth_ok      token OK (legacy)

Examples:
  .venv/bin/python login_to_build.py
  .venv/bin/python login_to_build.py --file accounts/email_pass.txt -n 5 -c 1
  .venv/bin/python login_to_build.py --email a@b.com --password 'Secret!'
  .venv/bin/python login_to_build.py --status failed_oauth,login_ok --headed
  .venv/bin/python login_to_build.py --force -n 3   # ignore skip, reprocess
  .venv/bin/python login_to_build.py --no-inject --no-activate
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# email=true skips provider chooser ("Login with email")
SIGNIN_URL = "https://accounts.x.ai/sign-in?email=true"
ACCOUNT_URL = "https://accounts.x.ai/account"
GROK_URL = "https://grok.com/"
XAI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_AUTHORIZE = "https://auth.x.ai/oauth2/authorize"
XAI_TOKEN = "https://auth.x.ai/oauth2/token"
XAI_REDIRECT = "http://127.0.0.1:56121/callback"
XAI_SCOPE = "openid profile email offline_access grok-cli:access api:access"

ACCOUNTS_JSONL = ROOT / "accounts" / "accounts.jsonl"
EMAIL_PASS_DEFAULT = ROOT / "accounts" / "email_pass.txt"

# Status terminal → default queue SKIP (next run tidak bawa lagi).
# Pakai --status <csv> atau --force untuk reprocess.
SKIP_STATUSES_DEFAULT = frozenset(
    {
        "failed_login",
        "failed_oauth",
        "failed_probe",
        "failed_push",
        "usable",
        "injected",
        "oauth_ok",
    }
)

# Infra / browser / Camoufox errors — NEVER treat as account terminal.
# Keep retryable on next run (not in SKIP).
INFRA_ERROR_MARKERS = (
    "is not installed",
    "camoufox fetch",
    "executable doesn't exist",
    "browser has been closed",
    "target closed",
    "Target page, context or browser has been closed",
    "Failed to launch",
    "Connection refused",
    "ECONNREFUSED",
    "net::ERR_",
)


def is_infra_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(x.lower() in m for x in INFRA_ERROR_MARKERS)


# Status yang masih boleh di-queue dari jsonl (belum “selesai”).
QUEUE_STATUSES_DEFAULT = frozenset(
    {
        "",
        "pending",
        "created",
        "login_ok",  # login sudah, OAuth belum final — boleh retry
        "error_infra",  # Camoufox/browser hiccup — retry
    }
)


@dataclass
class Cred:
    email: str
    password: str
    status: str = ""


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{ts} {msg}", flush=True)


def load_config() -> dict:
    path = ROOT / "config.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_jsonl_by_email(jsonl: Path | None = None) -> dict[str, dict]:
    """Last row per email (lower) → full record dict."""
    path = jsonl or ACCOUNTS_JSONL
    by: dict[str, dict] = {}
    if not path.is_file():
        return by
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return by
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        em = str(rec.get("email") or "").strip()
        if not em:
            continue
        by[em.lower()] = rec
    return by


def _read_email_pass(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        em, _, pw = line.partition(":")
        em, pw = em.strip(), pw.strip()
        if not em or not pw:
            continue
        key = em.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((em, pw))
    return out


def load_creds(
    *,
    file: Path | None,
    email: str,
    password: str,
    status_filter: set[str] | None,
    limit: int,
    force: bool = False,
    jsonl_only: bool = False,
) -> list[Cred]:
    """
    Credential sources:

    1. --email + --password → single (always, no skip)
    2. --status CSV → accounts.jsonl rows with that status (password from
       jsonl, fallback email_pass if blank)
    3. default → email_pass inventory, SKIP emails whose jsonl status is
       already terminal (failed_*, usable, injected, …) unless --force
    4. --jsonl-only → queue from jsonl only (status in pending/created/login_ok
       or matching --status)
    """
    if email and password:
        return [Cred(email=email.strip(), password=password)]

    jsonl_map = load_jsonl_by_email()
    ep_path = file or EMAIL_PASS_DEFAULT
    ep_pairs = _read_email_pass(ep_path)
    ep_by = {e.lower(): (e, p) for e, p in ep_pairs}

    def _pw_for(em: str, rec: dict | None) -> str:
        if rec and str(rec.get("password") or "").strip():
            return str(rec.get("password") or "").strip()
        hit = ep_by.get(em.lower())
        return hit[1] if hit else ""

    pairs: list[Cred] = []
    skipped = 0

    # --- status filter: get-by-status from jsonl ---
    if status_filter:
        for em_l, rec in jsonl_map.items():
            st = str(rec.get("status") or "").strip()
            if st not in status_filter:
                continue
            em = str(rec.get("email") or "").strip()
            pw = _pw_for(em, rec)
            if not em or not pw:
                continue
            pairs.append(Cred(email=em, password=pw, status=st))
        if limit > 0:
            pairs = pairs[:limit]
        _log(
            f"queue from jsonl --status={','.join(sorted(status_filter))}  "
            f"n={len(pairs)}"
        )
        return pairs

    # --- jsonl-only: pending work from ledger ---
    if jsonl_only:
        for em_l, rec in jsonl_map.items():
            st = str(rec.get("status") or "").strip()
            em = str(rec.get("email") or "").strip()
            pw = _pw_for(em, rec)
            if not em or not pw:
                continue
            if force:
                pairs.append(Cred(email=em, password=pw, status=st or "pending"))
                continue
            if st in SKIP_STATUSES_DEFAULT or st not in QUEUE_STATUSES_DEFAULT:
                skipped += 1
                continue
            pairs.append(Cred(email=em, password=pw, status=st or "pending"))
        if limit > 0:
            pairs = pairs[:limit]
        _log(f"queue jsonl-only  n={len(pairs)}  skipped={skipped}")
        return pairs

    # --- default: email_pass − already-done (jsonl status terminal) ---
    for em, pw in ep_pairs:
        rec = jsonl_map.get(em.lower())
        st = str((rec or {}).get("status") or "").strip()
        if not force and st in SKIP_STATUSES_DEFAULT:
            skipped += 1
            continue
        pairs.append(Cred(email=em, password=pw, status=st or "pending"))

    if limit > 0:
        pairs = pairs[:limit]
    _log(
        f"queue email_pass  n={len(pairs)}  skipped_done={skipped}  "
        f"force={force}  file={ep_path.name}"
    )
    return pairs


def update_account_status(
    email: str,
    status: str,
    *,
    password: str = "",
    error: str = "",
    probe_status: Any = None,
    has_oauth: bool | None = None,
    injected: bool | None = None,
    extra: dict | None = None,
) -> None:
    """Upsert one email row in accounts.jsonl (create file if missing)."""
    jsonl = ACCOUNTS_JSONL
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    email_l = email.strip().lower()
    lines: list[str] = []
    if jsonl.is_file():
        try:
            lines = jsonl.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
    idx = -1
    rec: dict | None = None
    for i in range(len(lines) - 1, -1, -1):
        try:
            r = json.loads(lines[i])
        except Exception:
            continue
        if str(r.get("email") or "").strip().lower() == email_l:
            idx = i
            rec = r
            break
    now = datetime.now().isoformat(timespec="seconds")
    if rec is None:
        rec = {
            "ts": now,
            "email": email,
            "password": password or "",
            "status": status,
            "mode": "login_to_build",
        }
        lines.append("")
        idx = len(lines) - 1
    rec["email"] = email
    rec["status"] = status
    rec["updated_at"] = now
    rec["mode"] = rec.get("mode") or "login_to_build"
    if password:
        rec["password"] = password
    elif "password" not in rec:
        rec["password"] = ""
    if error:
        rec["error"] = str(error)[:400]
    if probe_status is not None:
        rec["probe_status"] = probe_status
    if has_oauth is not None:
        rec["has_oauth"] = bool(has_oauth)
    if injected is not None:
        rec["injected"] = bool(injected)
    if status == "login_ok" and not rec.get("login_at"):
        rec["login_at"] = now
    if status in ("usable", "injected", "failed_probe", "failed_oauth", "oauth_ok"):
        rec["oauth_at"] = now
    if extra:
        for k, v in extra.items():
            if v is not None:
                rec[k] = v
    lines[idx] = json.dumps(rec, ensure_ascii=False)
    tmp = jsonl.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(jsonl)


def cookie_header_from_list(cookies: list[dict]) -> str:
    want = ("sso", "sso-rw", "cf_clearance", "__cf_bm")
    parts = []
    for name in want:
        for c in cookies:
            if (c.get("name") or "") == name and c.get("value"):
                parts.append(f"{name}={c['value']}")
                break
    # any other auth-ish
    for c in cookies:
        n = c.get("name") or ""
        if n in want:
            continue
        if any(k in n.lower() for k in ("session", "auth", "token")):
            parts.append(f"{n}={c.get('value')}")
    return "; ".join(parts)


async def dismiss_cookies(page) -> None:
    try:
        await page.evaluate(
            """() => {
              const re = /accept all cookies|accept all|allow all|reject all/i;
              for (const b of document.querySelectorAll('button,[role=button]')) {
                const t = (b.innerText||'').trim();
                if (re.test(t)) { b.click(); return t; }
              }
              return '';
            }"""
        )
    except Exception:
        pass


async def click_login_with_email(page) -> bool:
    for label in (
        "Login with email",
        "Log in with email",
        "Sign in with email",
        "Continue with email",
    ):
        try:
            loc = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if await loc.count() > 0:
                await loc.first.click(timeout=3000)
                return True
        except Exception:
            pass
    try:
        hit = await page.evaluate(
            """() => {
              const re = /(log\\s*in|sign\\s*in|continue)\\s+with\\s+email/i;
              for (const b of document.querySelectorAll('button,a,[role=button]')) {
                const t = (b.innerText||'').replace(/\\s+/g,' ').trim();
                if (re.test(t)) { b.click(); return t; }
              }
              return '';
            }"""
        )
        return bool(hit)
    except Exception:
        return False


async def fill_input(page, selectors: list[str], value: str) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() == 0:
                continue
            await loc.fill(value, timeout=4000)
            return True
        except Exception:
            try:
                await loc.click(timeout=2000)
                await page.keyboard.type(value, delay=20)
                return True
            except Exception:
                continue
    return False


async def ensure_password(page, password: str) -> bool:
    for _ in range(4):
        try:
            loc = page.locator('input[type="password"]').first
            if await loc.count() == 0:
                await asyncio.sleep(0.4)
                continue
            await loc.fill(password, timeout=4000)
            val = await loc.input_value()
            if val == password:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return False


async def _turnstile_token_len(page) -> int:
    """Read CF turnstile token length (input + window.turnstile API)."""
    try:
        return int(
            await page.evaluate(
                """() => {
                  try {
                    if (window.turnstile && typeof turnstile.getResponse === 'function') {
                      const r = turnstile.getResponse();
                      if (r && String(r).trim()) return String(r).trim().length;
                    }
                  } catch (e) {}
                  const el = document.querySelector(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
                  );
                  return el && el.value ? String(el.value).length : 0;
                }"""
            )
            or 0
        )
    except Exception:
        return 0


async def _turnstile_visible(page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                  if (document.querySelector(
                    'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"],'
                    + 'iframe[src*="challenges.cloudflare"], iframe[src*="turnstile"],'
                    + '.cf-turnstile, [data-sitekey]'
                  )) return true;
                  const t = ((document.body && document.body.innerText) || '').toLowerCase();
                  return t.includes('verify you are human');
                }"""
            )
        )
    except Exception:
        return False


async def _click_turnstile_checkbox(page) -> str:
    """
    Human path for CF Turnstile checkbox (same idea as register / DrissionPage).
    Returns which strategy hit, or ''.
    """
    # 1) Visible label / text
    for sel in (
        'text=Verify you are human',
        'label:has-text("Verify you are human")',
        '[aria-label*="Verify" i]',
    ):
        try:
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=1500, force=False)
                return f"label:{sel}"
        except Exception:
            pass

    # 2) Widget container / sitekey host
    try:
        hit = await page.evaluate(
            """() => {
              const pick = document.querySelector(
                '.cf-turnstile, [data-sitekey], div[id^="cf-chl"], #cf-turnstile'
              );
              if (pick) {
                const r = pick.getBoundingClientRect();
                // checkbox is left side of widget (~30, mid-y)
                const x = r.left + Math.min(28, Math.max(12, r.width * 0.08));
                const y = r.top + r.height / 2;
                const el = document.elementFromPoint(x, y) || pick;
                el.dispatchEvent(new MouseEvent('click', {
                  bubbles: true, cancelable: true, view: window,
                  clientX: x, clientY: y
                }));
                if (typeof el.click === 'function') el.click();
                return 'widget-point';
              }
              const ifr = document.querySelector(
                'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'
              );
              if (ifr) {
                ifr.click();
                return 'iframe-host';
              }
              return '';
            }"""
        )
        if hit:
            return str(hit)
    except Exception:
        pass

    # 3) Frame locator — click inside CF iframe body (checkbox area)
    for frame_sel in (
        'iframe[src*="challenges.cloudflare"]',
        'iframe[src*="turnstile"]',
        'iframe[title*="Widget" i]',
        'iframe[title*="cloudflare" i]',
    ):
        try:
            fl = page.frame_locator(frame_sel).first
            # checkbox / body left side
            body = fl.locator("body")
            if await body.count() > 0:
                box = await body.bounding_box()
                if box:
                    await body.click(
                        timeout=1500,
                        position={"x": min(28, max(10, box["width"] * 0.12)), "y": box["height"] / 2},
                    )
                    return f"frame-body:{frame_sel}"
            # input checkbox inside iframe
            cb = fl.locator('input[type="checkbox"], input[type="button"], label, .ctp-checkbox-label')
            if await cb.count() > 0:
                await cb.first.click(timeout=1500)
                return f"frame-cb:{frame_sel}"
        except Exception:
            pass

    # 4) Shadow-root pierce (CF often nests iframe in closed/open shadow)
    try:
        hit = await page.evaluate(
            """() => {
              function walk(root, depth) {
                if (!root || depth > 6) return null;
                const ifr = root.querySelector && root.querySelector(
                  'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'
                );
                if (ifr) return ifr;
                const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                for (const el of all) {
                  if (el.shadowRoot) {
                    const f = walk(el.shadowRoot, depth + 1);
                    if (f) return f;
                  }
                }
                return null;
              }
              const host = document.querySelector(
                'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
              );
              let iframe = null;
              if (host) {
                let p = host.parentElement;
                for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
                  if (p.shadowRoot) {
                    iframe = walk(p.shadowRoot, 0);
                    if (iframe) break;
                  }
                  iframe = p.querySelector && p.querySelector(
                    'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"]'
                  );
                  if (iframe) break;
                }
              }
              if (!iframe) iframe = walk(document, 0);
              if (!iframe) return '';
              const r = iframe.getBoundingClientRect();
              const x = r.left + 26;
              const y = r.top + r.height / 2;
              const el = document.elementFromPoint(x, y) || iframe;
              el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, clientX:x, clientY:y}));
              el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, clientX:x, clientY:y}));
              el.dispatchEvent(new MouseEvent('click', {bubbles:true, clientX:x, clientY:y}));
              try { iframe.click(); } catch (e) {}
              return 'shadow-pierce';
            }"""
        )
        if hit:
            return str(hit)
    except Exception:
        pass
    return ""


async def handle_turnstile_light(page, max_wait: float = 35.0) -> bool:
    """
    Apply CF Turnstile like register flow:
      - detect widget
      - click checkbox (human path)
      - wait natural token (Camoufox often auto-solves after click)
    Returns True if token present or no widget / already logged in.
    """
    if _url_looks_logged_in(page.url or ""):
        return True

    # No widget → ok
    if not await _turnstile_visible(page):
        tok0 = await _turnstile_token_len(page)
        if tok0 > 20:
            return True
        # brief settle — widget may mount late after password fill
        for _ in range(6):
            await asyncio.sleep(0.35)
            if await _turnstile_visible(page) or await _turnstile_token_len(page) > 20:
                break
        else:
            return True  # no CF on page

    tok = await _turnstile_token_len(page)
    if tok > 20:
        _log(f"  turnstile already solved  token_len={tok}")
        return True

    _log("  turnstile: waiting / clicking Verify you are human …")
    deadline = time.monotonic() + max(8.0, max_wait)
    clicked = ""
    last_log = 0.0
    while time.monotonic() < deadline:
        if _url_looks_logged_in(page.url or ""):
            return True
        tok = await _turnstile_token_len(page)
        if tok > 20:
            _log(f"  turnstile OK  token_len={tok}  via={clicked or 'auto'}")
            return True

        # re-click periodically (not every 0.3s — looks botty)
        now = time.monotonic()
        if not clicked or (now - last_log) > 2.5:
            hit = await _click_turnstile_checkbox(page)
            if hit:
                clicked = hit
                if (now - last_log) > 2.5:
                    _log(f"  turnstile click {hit}")
                    last_log = now

        await asyncio.sleep(0.45)

    tok = await _turnstile_token_len(page)
    if tok > 20:
        _log(f"  turnstile OK late  token_len={tok}")
        return True
    _log(f"  turnstile NOT ready  token_len={tok}  (will still try Login)")
    return False


def _url_looks_logged_in(url: str) -> bool:
    u = (url or "").lower()
    if not u:
        return False
    # Still on auth/device/consent flows
    if any(
        x in u
        for x in (
            "/sign-in",
            "/sign-up",
            "/login",
            "oauth2/device",
            "oauth2/consent",
            "oauth2/authorize",
        )
    ):
        return False
    if "/account" in u:
        return True
    if "grok.com" in u:
        return True
    # /security, /sessions, /data after login
    if "accounts.x.ai" in u and any(
        p in u for p in ("/security", "/sessions", "/data")
    ):
        return True
    return False


async def _login_success(page) -> bool:
    """Fast success checks — URL first, then SSO cookie, then form gone."""
    if _url_looks_logged_in(page.url or ""):
        return True
    try:
        cookies = await page.context.cookies()
        if any(
            (c.get("name") or "") == "sso" and c.get("value") for c in cookies
        ):
            # SSO alone is weak if still on sign-in (partial session)
            if "sign-in" not in (page.url or "").lower():
                return True
    except Exception:
        pass
    try:
        u = (page.url or "").lower()
        if "sign-in" not in u and "sign-up" not in u and "login" not in u:
            if await page.locator('input[type="password"]').count() == 0:
                return True
    except Exception:
        pass
    return False


def _log_login_landed(page, note: str = "") -> None:
    u = (page.url or "").lower()
    suffix = f" ({note})" if note else ""
    if "/account" in u:
        _log(f"  login landed on /account{suffix}")
    elif "grok.com" in u:
        _log(f"  login landed on grok.com{suffix}")
    else:
        _log(f"  login OK url={(page.url or '')[:80]}{suffix}")


async def _wait_login_landed(page, timeout_sec: float = 18.0) -> bool:
    """Poll URL/cookies aggressively after Login click (was: sleep 2 + CF 12s)."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if await _login_success(page):
            _log_login_landed(page)
            return True
        # password wrong — fail fast
        try:
            if await page.locator(
                "text=/incorrect|invalid password|wrong password/i"
            ).count() > 0:
                return False
        except Exception:
            pass
        await asyncio.sleep(0.2)
    if await _login_success(page):
        _log_login_landed(page)
        return True
    return False


async def drive_login(page, email: str, password: str) -> bool:
    await dismiss_cookies(page)

    # Already in?
    if await _login_success(page):
        _log(f"  already logged in  url={(page.url or '')[:80]}")
        return True

    # Fallback only if email form missing (direct ?email=true should skip this)
    if await page.locator('input[type="email"], input[type="password"]').count() == 0:
        await click_login_with_email(page)
        await asyncio.sleep(0.5)

    if await page.locator('input[type="email"], input[name="email"]').count() > 0:
        await fill_input(
            page,
            [
                'input[type="email"]',
                'input[name="email"]',
                'input[autocomplete="email"]',
                'input[autocomplete="username"]',
            ],
            email,
        )
        await asyncio.sleep(0.2)
        if await page.locator('input[type="password"]').count() == 0:
            try:
                await page.get_by_role("button", name=re.compile(r"^next$", re.I)).click(
                    timeout=3000
                )
            except Exception:
                try:
                    await page.get_by_role(
                        "button", name=re.compile(r"^continue$", re.I)
                    ).click(timeout=2500)
                except Exception:
                    pass
            for _ in range(20):
                if await page.locator('input[type="password"]').count() > 0:
                    break
                if await _login_success(page):
                    _log("  login landed during email step")
                    return True
                await asyncio.sleep(0.25)

    await ensure_password(page, password)
    # Register-style: solve Turnstile BEFORE Login (checkbox + wait token)
    await handle_turnstile_light(page, max_wait=35)
    await ensure_password(page, password)  # re-fill if CF remounted form

    for round_i in range(4):
        if await _login_success(page):
            _log_login_landed(page)
            return True

        await ensure_password(page, password)
        # Ensure CF token each round (widget may reset after failed submit)
        cf_ok = await handle_turnstile_light(page, max_wait=25 if round_i == 0 else 15)
        if await _login_success(page):
            _log_login_landed(page, note="after CF")
            return True
        await ensure_password(page, password)

        tok = await _turnstile_token_len(page)
        if tok <= 20 and await _turnstile_visible(page):
            _log(
                f"  login round {round_i+1}: turnstile still empty "
                f"(token_len={tok}, cf_ok={cf_ok}) — wait more"
            )
            await handle_turnstile_light(page, max_wait=12)
            await ensure_password(page, password)
            tok = await _turnstile_token_len(page)

        _log(f"  click Login  round={round_i+1}  turnstile_len={tok}")
        try:
            await page.get_by_role(
                "button", name=re.compile(r"^(login|log in|sign in)$", re.I)
            ).click(timeout=3000)
        except Exception:
            try:
                await page.evaluate(
                    """() => {
                      const re = /^(login|log in|sign in|continue)$/i;
                      for (const b of document.querySelectorAll('button,[role=button]')) {
                        const t = (b.innerText||'').replace(/\\s+/g,' ').trim();
                        if (re.test(t)) { b.click(); return t; }
                      }
                      return '';
                    }"""
                )
            except Exception:
                pass

        # Fast poll after submit
        if await _wait_login_landed(page, timeout_sec=14.0):
            return True

        if await page.locator(
            "text=/incorrect|invalid password|wrong password/i"
        ).count() > 0:
            _log(f"  login rejected (wrong password?) round={round_i+1}")
            return False

        # Failed submit often clears token — next round re-solves
        if await _turnstile_visible(page) and await _turnstile_token_len(page) <= 20:
            _log("  turnstile cleared after Login — re-solve")

    if await _login_success(page):
        _log("  login OK (final check)")
        return True
    # last resort: sso cookie even on weird URL
    try:
        cookies = await page.context.cookies()
        if any((c.get("name") or "") == "sso" and c.get("value") for c in cookies):
            _log("  login OK via sso cookie")
            return True
    except Exception:
        pass
    return False


async def login_accounts_xai(page, email: str, password: str) -> bool:
    _log(f"  goto sign-in  {email}")
    try:
        await page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        try:
            await page.goto(SIGNIN_URL, wait_until="commit", timeout=45000)
        except Exception as e:
            raise RuntimeError(f"goto sign-in failed: {e}") from e
    await asyncio.sleep(0.4)
    # Redirect to /account while still loading?
    if await _wait_login_landed(page, timeout_sec=2.0):
        return True
    ok = await drive_login(page, email, password)
    if not ok:
        # retry once from account url
        try:
            await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=30000)
            if await _wait_login_landed(page, timeout_sec=3.0):
                return True
            if "sign-in" in (page.url or "") or await page.locator(
                'input[type="password"]'
            ).count() > 0:
                ok = await drive_login(page, email, password)
            else:
                ok = True
        except Exception:
            pass
    return ok


async def activate_grok_com(page, timeout_sec: float = 45.0) -> bool:
    _log("  activate grok.com …")
    try:
        await page.goto(GROK_URL, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        try:
            await page.goto(GROK_URL, wait_until="commit", timeout=45000)
        except Exception as e:
            _log(f"  activate goto warn: {e}")
    deadline = time.monotonic() + max(15.0, timeout_sec)
    ready = False
    while time.monotonic() < deadline:
        try:
            info = await page.evaluate(
                """() => {
                  const title = (document.title||'').toLowerCase();
                  const body = ((document.body&&document.body.innerText)||'').slice(0,900).toLowerCase();
                  const url = (location.href||'').toLowerCase();
                  const just = title.includes('just a moment') || body.includes('just a moment')
                    || body.includes('checking your browser');
                  const hasUi = !!(document.querySelector('textarea,[contenteditable="true"],nav,main,[data-testid]'));
                  return { url, just, hasUi, ready: url.includes('grok.com') && !just && (hasUi || body.includes('grok')) };
                }"""
            )
            if isinstance(info, dict) and info.get("ready"):
                ready = True
                break
            if isinstance(info, dict) and info.get("just"):
                await asyncio.sleep(0.8)
                continue
        except Exception:
            pass
        await asyncio.sleep(0.5)
    await dismiss_cookies(page)
    # ToS
    try:
        await page.evaluate(
            """() => {
              const want = /^(accept|agree|i agree|continue|ok|got it|start)$/i;
              for (const b of document.querySelectorAll('button,[role=button]')) {
                const t = (b.innerText||'').replace(/\\s+/g,' ').trim();
                if (want.test(t)) { b.click(); return t; }
              }
              return '';
            }"""
        )
    except Exception:
        pass
    _log(f"  activate done ready={ready} url={(page.url or '')[:90]}")
    return ready


async def obtain_pkce_tokens(
    page,
    *,
    email: str,
    password: str = "",
    referrer: str = "grok-build",
    timeout_sec: float = 75.0,
) -> dict:
    """
    Browser PKCE. After Allow, xAI often redirects to 127.0.0.1:56121/callback?code=.

    Capture via: page.route + request listener + real HTTP server on :56121.
    Do NOT loop /account → re-authorize forever (Allow can land on /account
    when callback is missed — that is a dead end for this attempt).
    """
    from build_oauth_pkce import (
        generate_pkce_pair,
        extract_code_from_url,
        exchange_code_for_tokens,
        BuildTokens,
        _PkceCallbackServer,
        _looks_like_oauth_auth_code,
    )

    verifier, challenge = generate_pkce_pair()
    state = os.urandom(16).hex()
    params = {
        "response_type": "code",
        "client_id": XAI_CLIENT_ID,
        "redirect_uri": XAI_REDIRECT,
        "scope": XAI_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": os.urandom(8).hex(),
        "plan": "generic",
        "referrer": referrer or "grok-build",
        "login_hint": email,
    }
    auth_url = f"{XAI_AUTHORIZE}?{urlencode(params)}"
    captured: dict[str, Optional[str]] = {"code": None}

    def _maybe_set_code(raw_url: str, src: str) -> bool:
        c = extract_code_from_url(raw_url or "")
        if c and _looks_like_oauth_auth_code(c):
            captured["code"] = c
            _log(f"  OAuth code via {src} len={len(c)}")
            return True
        # also accept longer codes that extract_code allows
        if c and len(c) >= 20:
            captured["code"] = c
            _log(f"  OAuth code via {src} len={len(c)} (len-ok)")
            return True
        return False

    async def on_route(route):
        url = route.request.url
        if _maybe_set_code(url, "route"):
            try:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<html><body>OAuth callback OK</body></html>",
                )
            except Exception:
                try:
                    await route.abort()
                except Exception:
                    pass
            return
        # Still fulfill bare callback host so browser doesn't hang
        if "127.0.0.1:56121" in url or "localhost:56121" in url:
            try:
                await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<html><body>no code</body></html>",
                )
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass
            return
        await route.continue_()

    def on_request(req):
        try:
            _maybe_set_code(req.url, "request")
        except Exception:
            pass

    # Real HTTP listener (Chromium-style) — also helps if route misses
    cb_server = _PkceCallbackServer(captured)
    cb_up = cb_server.start(wait_lock_sec=3.0)
    _log(f"  PKCE callback server :56121={'on' if cb_up else 'off (route-only)'}")

    await page.route("http://127.0.0.1:56121/**", on_route)
    await page.route("http://localhost:56121/**", on_route)
    page.on("request", on_request)
    _log(f"  PKCE authorize referrer={referrer}")

    async def _goto_authorize(reason: str = "") -> None:
        if reason:
            _log(f"  re-goto authorize ({reason})")
        try:
            await page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
        except Exception:
            # Navigation to callback may abort authorize goto — check code
            if captured.get("code"):
                return
            try:
                await page.goto(auth_url, wait_until="commit", timeout=45000)
            except Exception as e:
                if captured.get("code"):
                    return
                raise e
        await asyncio.sleep(0.6)

    async def _click_allow() -> bool:
        try:
            loc = page.get_by_role("button", name=re.compile(r"^Allow$", re.I))
            if await loc.count() > 0:
                await loc.first.click(timeout=3000)
                _log("  clicked Allow")
                return True
        except Exception:
            pass
        try:
            hit = await page.evaluate(
                """() => {
                  const exact = new Set(['allow','authorize','approve']);
                  for (const b of document.querySelectorAll('button,[role=button]')) {
                    const t = (b.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                    if (exact.has(t)) { b.click(); return t; }
                  }
                  return '';
                }"""
            )
            if hit:
                _log(f"  clicked Allow via JS ({hit!r})")
                return True
        except Exception:
            pass
        return False

    async def _wait_for_code(seconds: float, label: str) -> bool:
        """Poll capture after Allow — do not re-authorize during this window."""
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            if captured.get("code"):
                return True
            url = page.url or ""
            if _maybe_set_code(url, "url-poll"):
                return True
            # Connection successful / copy-code UI
            try:
                body = await page.evaluate(
                    "() => ((document.body&&document.body.innerText)||'').slice(0,500)"
                )
                low = (body or "").lower()
                if "failed to generate authentication code" in low:
                    raise RuntimeError(f"OAuth access denied UI: {(body or '')[:120]}")
                if "connection successful" in low or "happy building" in low:
                    _log(f"  UI: connection successful ({label}) — wait capture")
            except RuntimeError:
                raise
            except Exception:
                pass
            await asyncio.sleep(0.35)
        return bool(captured.get("code"))

    re_login_count = 0
    authorize_rounds = 0
    max_authorize_rounds = 2  # not infinite

    try:
        while authorize_rounds < max_authorize_rounds and not captured.get("code"):
            authorize_rounds += 1
            await _goto_authorize(
                "" if authorize_rounds == 1 else f"round {authorize_rounds}"
            )
            deadline = time.monotonic() + min(40.0, timeout_sec / max_authorize_rounds)
            allow_clicks = 0
            last_url_log = ""

            while time.monotonic() < deadline and not captured.get("code"):
                url = page.url or ""
                u_low = url.lower()
                if url != last_url_log:
                    _log(f"  OAuth page: {url[:130]}")
                    last_url_log = url

                if _maybe_set_code(url, "page.url"):
                    break
                await dismiss_cookies(page)

                # Consent
                if "/oauth2/consent" in u_low or (
                    "consent" in u_low and "oauth" in u_low
                ):
                    if await _click_allow():
                        allow_clicks += 1
                        # CRITICAL: wait for callback code — do NOT treat /account as re-auth yet
                        got = await _wait_for_code(10.0, f"after Allow #{allow_clicks}")
                        if got:
                            break
                        # Still no code: check where we are
                        cur = (page.url or "").lower()
                        _log(
                            f"  no code after Allow #{allow_clicks}  "
                            f"url={cur[:100]!r} — stop re-auth loop this round"
                        )
                        # One more Allow only if still on consent
                        if "/oauth2/consent" in cur and allow_clicks < 2:
                            continue
                        break  # leave inner loop → maybe next authorize round or fail
                    await asyncio.sleep(0.5)
                    continue

                # Real sign-in form
                if password and (
                    ("/sign-in" in u_low or "/sign-up" in u_low)
                    and await page.locator('input[type="password"], input[type="email"]').count()
                    > 0
                ):
                    if re_login_count >= 2:
                        raise RuntimeError("OAuth stuck on sign-in")
                    re_login_count += 1
                    _log(f"  OAuth sign-in form — re-login #{re_login_count}")
                    await drive_login(page, email, password)
                    # continue same authorize round after short pause (may redirect to consent)
                    await asyncio.sleep(1.0)
                    # if still not consent, re-goto authorize once
                    if "consent" not in (page.url or "").lower():
                        await _goto_authorize("after re-login")
                    continue

                # Landed on /account WITHOUT code after Allow = callback missed
                if "/account" in u_low and "sign-in" not in u_low:
                    if allow_clicks > 0:
                        _log(
                            "  landed /account after Allow without code "
                            "(callback miss) — end PKCE attempt"
                        )
                        break
                    # Before any Allow: session ready, need authorize (first paint)
                    if authorize_rounds == 1 and allow_clicks == 0:
                        await _goto_authorize("session on /account before consent")
                        continue
                    break

                # access denied
                try:
                    body = await page.evaluate(
                        "() => ((document.body&&document.body.innerText)||'').slice(0,300).toLowerCase()"
                    )
                    if "failed to generate authentication code" in (body or ""):
                        raise RuntimeError(
                            f"OAuth access denied UI: {(body or '')[:120]}"
                        )
                except RuntimeError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(0.45)

            if captured.get("code"):
                break
            if authorize_rounds < max_authorize_rounds:
                _log(f"  PKCE round {authorize_rounds} no code — retry authorize once")
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass
        try:
            await page.unroute("http://127.0.0.1:56121/**")
            await page.unroute("http://localhost:56121/**")
        except Exception:
            pass
        if cb_up:
            try:
                cb_server.stop()
            except Exception:
                pass

    code = captured.get("code")
    if not code:
        raise RuntimeError(
            f"OAuth code not captured url={(page.url or '')[:120]!r} "
            f"(Allow often redirects to /account when :56121 callback is missed)"
        )

    _log(f"  code OK len={len(code)} → exchange (browser context)")
    tokens_obj: Any = None
    try:
        tokens_obj = exchange_code_for_tokens(
            code, verifier, proxy="", page=page, log=lambda m: _log(f"  {m}")
        )
    except Exception as e:
        _log(f"  browser exchange fail: {e} — try host HTTP")
        tokens_obj = exchange_code_for_tokens(code, verifier, proxy="")

    if isinstance(tokens_obj, BuildTokens):
        return {
            "access_token": tokens_obj.access_token,
            "refresh_token": tokens_obj.refresh_token,
            "email": tokens_obj.email or email,
            "user_id": tokens_obj.user_id,
            "expires_at": tokens_obj.expires_at,
            "expires_in": tokens_obj.expires_in,
            "bot_flag_source": tokens_obj.bot_flag_source,
            "referrer": tokens_obj.referrer,
            "auth_mode": "oidc_pkce",
            "_obj": tokens_obj,
        }
    raise RuntimeError("token exchange returned empty")


async def _refresh_sso_on_account(page) -> list[dict]:
    """Land on accounts.x.ai/account so SSO cookies are fresh for device/PKCE."""
    try:
        await page.goto(ACCOUNT_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        try:
            await page.goto(ACCOUNT_URL, wait_until="commit", timeout=30000)
        except Exception as e:
            _log(f"  refresh /account warn: {e}")
    await asyncio.sleep(1.0)
    try:
        return await page.context.cookies()
    except Exception:
        return []


def _sso_debug(cookies: list[dict]) -> str:
    """Short debug: cookie names + session vs wrapper JWT."""
    names = sorted({c.get("name") or "" for c in cookies if c.get("name")})
    sso = ""
    for c in cookies:
        if c.get("name") == "sso" and c.get("value"):
            sso = str(c["value"])
            break
    kind = "?"
    if sso:
        try:
            import base64

            parts = sso.split(".")
            if len(parts) >= 2:
                pad = parts[1] + "=" * (-len(parts[1]) % 4)
                payload = json.loads(base64.urlsafe_b64decode(pad.encode()).decode())
                if "session_id" in payload or "session" in payload:
                    kind = "session"
                elif "config" in payload or "success_url" in str(payload):
                    kind = "wrapper"
                else:
                    kind = "jwt"
        except Exception:
            kind = "opaque"
    return f"cookies={len(names)} sso={kind} names={','.join(names[:12])}"


async def device_via_browser_session(page, email: str) -> dict:
    """
    Device OAuth using the LIVE browser cookie jar (page.request).

    Pure HTTP convert_sso_to_build often gets Access denied after approve when
    the jar is incomplete / TLS differs. Same-browser request keeps CF + SSO.
    """
    from sso_to_build import (
        CLIENT_ID,
        SCOPE,
        DEVICE_URL,
        TOKEN_URL,
        BuildTokens,
    )
    import time as _time

    # Prefer accounts.x.ai origin so cookies attach
    cookies = await _refresh_sso_on_account(page)
    _log(f"  device browser-session  {_sso_debug(cookies)}")
    if not any((c.get("name") == "sso" and c.get("value")) for c in cookies):
        raise RuntimeError("no sso cookie after /account refresh")

    # 1) device code
    r = await page.request.post(
        DEVICE_URL,
        form={
            "client_id": CLIENT_ID,
            "scope": SCOPE,
            "referrer": "grok-build",
        },
        headers={
            "Accept": "application/json",
            "Origin": "https://accounts.x.ai",
            "Referer": "https://accounts.x.ai/",
        },
        timeout=30000,
    )
    if r.status >= 400:
        raise RuntimeError(f"device code HTTP {r.status}: {(await r.text())[:200]}")
    device = await r.json()
    device_code = device.get("device_code") or ""
    user_code = device.get("user_code") or ""
    veri = device.get("verification_uri_complete") or ""
    interval = int(device.get("interval") or 5)
    if not device_code or not user_code or not veri:
        raise RuntimeError(f"incomplete device response: {device}")
    _log(f"  device user_code={user_code}")

    # 2) open verify in same browser (SSO auto)
    try:
        await page.goto(veri, wait_until="domcontentloaded", timeout=45000)
    except Exception:
        await page.goto(veri, wait_until="commit", timeout=45000)
    await asyncio.sleep(1.0)
    await dismiss_cookies(page)

    # 3) UI-only approve (Continue → Allow). Do NOT also HTTP POST approve —
    # double-approve after UI success returns 400 and can poison the grant.
    ui_allowed = False
    for _ in range(8):
        url = (page.url or "").lower()
        body_hint = ""
        try:
            body_hint = await page.evaluate(
                "() => ((document.body&&document.body.innerText)||'').slice(0,400).toLowerCase()"
            )
        except Exception:
            pass
        if (
            "done" in url
            or "success" in url
            or "authorized" in (body_hint or "")
            or "you can close" in (body_hint or "")
            or "device authorized" in (body_hint or "")
        ):
            ui_allowed = True
            _log(f"  device UI authorized  url={url[:90]}")
            break
        clicked = False
        # Prefer Allow/Approve over Continue (Continue = enter code step only)
        for pattern in (r"^Allow$", r"^Approve$", r"^Authorize$", r"^Confirm$", r"^Continue$"):
            try:
                loc = page.get_by_role("button", name=re.compile(pattern, re.I))
                if await loc.count() > 0:
                    await loc.first.click(timeout=2500)
                    _log(f"  device UI click {pattern}")
                    if "Allow" in pattern or "Approve" in pattern or "Authorize" in pattern:
                        ui_allowed = True
                    clicked = True
                    await asyncio.sleep(1.4)
                    break
            except Exception:
                pass
        if not clicked:
            try:
                hit = await page.evaluate(
                    """() => {
                      // Prefer allow over continue
                      const order = ['allow','approve','authorize','confirm','continue'];
                      const nodes = [...document.querySelectorAll('button,[role=button]')];
                      for (const w of order) {
                        for (const b of nodes) {
                          const t = (b.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                          if (t === w) { b.click(); return t; }
                        }
                      }
                      return '';
                    }"""
                )
                if hit:
                    _log(f"  device UI JS click {hit!r}")
                    if hit in ("allow", "approve", "authorize"):
                        ui_allowed = True
                    await asyncio.sleep(1.4)
                    continue
            except Exception:
                pass
            break

    # 4) HTTP approve ONLY if UI never clicked Allow (headless / no button)
    if not ui_allowed:
        try:
            ar = await page.request.post(
                "https://auth.x.ai/oauth2/device/approve",
                form={"user_code": user_code, "action": "allow"},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://accounts.x.ai",
                    "Referer": veri or "https://accounts.x.ai/",
                    "Accept": "text/html,application/json",
                },
                timeout=30000,
                max_redirects=5,
            )
            body_a = (await ar.text())[:120]
            _log(f"  device approve HTTP {ar.status} body={body_a!r}")
            if ar.status < 400 or "done" in (ar.url or "").lower():
                ui_allowed = True
        except Exception as e:
            _log(f"  device approve warn: {e}")
    else:
        _log("  skip HTTP approve (UI already Allow — avoid double-approve 400)")

    # 5) poll token (immediate first poll — grant may already be ready)
    deadline = _time.time() + 45
    last_err = ""
    first = True
    while _time.time() < deadline:
        if not first:
            await asyncio.sleep(max(1, interval))
        first = False
        tr = await page.request.post(
            TOKEN_URL,
            form={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": device_code,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "Origin": "https://auth.x.ai",
                "Referer": "https://accounts.x.ai/",
            },
            timeout=30000,
        )
        text = await tr.text()
        _log(f"  token poll HTTP {tr.status} body={text[:180]!r}")
        try:
            payload = json.loads(text) if text.strip().startswith("{") else {}
        except Exception:
            # Plain "Access denied" body
            last_err = f"token HTTP {tr.status}: {text[:200]}"
            if "access denied" in text.lower():
                break
            continue
        if not payload and text:
            last_err = f"token HTTP {tr.status}: {text[:200]}"
            if "access denied" in text.lower():
                break
            continue
        if tr.status < 300 and payload.get("access_token"):
            access = str(payload["access_token"])
            refresh = str(payload.get("refresh_token") or "")
            if not refresh:
                raise RuntimeError("device token missing refresh_token")
            exp_in = int(payload.get("expires_in") or 21600)
            from datetime import timezone, timedelta

            exp_at = (
                datetime.now(timezone.utc) + timedelta(seconds=exp_in)
            ).isoformat().replace("+00:00", "Z")
            em = email
            try:
                from build_oauth_pkce import _decode_jwt_claims

                claims = _decode_jwt_claims(payload.get("id_token") or access)
                em = str(claims.get("email") or email)
                uid = str(claims.get("sub") or "")
            except Exception:
                uid = ""
            tok = BuildTokens(
                access_token=access,
                refresh_token=refresh,
                id_token=str(payload.get("id_token") or ""),
                expires_at=exp_at,
                expires_in=exp_in,
                email=em,
                user_id=uid,
                team_id="",
                name=em or email,
                scope=str(payload.get("scope") or SCOPE),
            )
            _log(f"  device OAuth OK email={em}")
            return {
                "access_token": access,
                "refresh_token": refresh,
                "email": em,
                "user_id": uid,
                "expires_at": exp_at,
                "expires_in": exp_in,
                "auth_mode": "device_browser",
                "_obj": tok,
            }
        err = str(payload.get("error") or "")
        desc = str(payload.get("error_description") or "")
        if err in ("authorization_pending", "slow_down"):
            if err == "slow_down":
                interval += 2
            continue
        last_err = f"token HTTP {tr.status}: {desc or err or text[:160]}"
        # Hard deny
        if "access_denied" in err or "access denied" in (desc + text).lower():
            break
        if tr.status >= 400 and err not in ("authorization_pending", "slow_down"):
            break
    raise RuntimeError(last_err or "device token poll timeout")


async def device_fallback(page, email: str, password: str) -> dict:
    """
    Device OAuth after login:
      1) browser-session device (preferred)
      2) pure HTTP sso_to_build convert (fallback)
    """
    from sso_to_build import convert_sso_to_build

    # 1) browser-native device
    try:
        return await device_via_browser_session(page, email)
    except Exception as e1:
        _log(f"  device browser-session failed: {e1}")

    # 2) classic HTTP
    cookies = await _refresh_sso_on_account(page)
    jar = {c["name"]: c["value"] for c in cookies if c.get("name") and c.get("value")}
    header = cookie_header_from_list(cookies)
    _log(f"  device HTTP fallback  {_sso_debug(cookies)}")
    if "sso" not in jar and not header:
        raise RuntimeError("no SSO cookies for device fallback")
    raw = header or f"sso={jar.get('sso','')}; sso-rw={jar.get('sso-rw', jar.get('sso',''))}"
    tok = convert_sso_to_build(
        raw,
        name_hint=email,
        cookies=jar,
        proxy="",
        fallback_direct=False,
        max_rl_tries=2,
        poll_timeout_sec=45,
    )
    return {
        "access_token": tok.access_token,
        "refresh_token": tok.refresh_token,
        "email": tok.email or email,
        "user_id": tok.user_id,
        "expires_at": tok.expires_at,
        "expires_in": tok.expires_in,
        "auth_mode": "device_http",
        "_obj": tok,
    }


async def process_one(
    cred: Cred,
    *,
    headless: bool,
    activate: bool,
    inject: bool,
    referrer: str,
    conf: dict,
    gap_sec: float,
    oauth_mode: str = "auto",
) -> dict:
    from camoufox.async_api import AsyncCamoufox
    from chat_usable import probe_chat_usable
    from push_9router_grok_cli import push_build_tokens_to_9router
    from build_oauth_pkce import BuildTokens

    gcli = conf.get("grok_cli") if isinstance(conf.get("grok_cli"), dict) else {}
    result: dict[str, Any] = {
        "email": cred.email,
        "ok": False,
        "usable": False,
        "injected": False,
        "status": "failed_oauth",
    }

    launch: dict[str, Any] = {
        "headless": headless,
        "humanize": True,
        "os": ["windows"] if sys.platform != "darwin" else ["macos"],
        "locale": "en-US",
        "window": (1280, 900),
    }

    async with AsyncCamoufox(**launch) as browser:
        page = await browser.new_page()
        page.set_default_timeout(60000)

        # 1) Login — always stamp password into ledger so --status works later
        ok_login = await login_accounts_xai(page, cred.email, cred.password)
        if not ok_login:
            result["error"] = "login failed"
            result["status"] = "failed_login"
            update_account_status(
                cred.email,
                "failed_login",
                password=cred.password,
                error="login failed",
                has_oauth=False,
            )
            return result
        _log("  login OK")
        sso_hdr = ""
        try:
            cookies = await page.context.cookies()
            sso_hdr = cookie_header_from_list(cookies)
            result["sso_cookie"] = sso_hdr
        except Exception:
            pass
        update_account_status(
            cred.email,
            "login_ok",
            password=cred.password,
            has_oauth=False,
            extra={"sso_cookie": sso_hdr} if sso_hdr else None,
        )

        # 2) Activate
        if activate:
            try:
                await activate_grok_com(
                    page,
                    timeout_sec=float(gcli.get("activate_timeout_sec") or 45),
                )
            except Exception as e:
                _log(f"  activate warn: {e}")

        # 3) OAuth
        # After password login we already have SSO cookies → device flow is the
        # reliable path. PKCE browser redirect to 127.0.0.1:56121 often lands on
        # /account without a code (same symptom as "Allow loop").
        # auto = device first, then PKCE; pkce = PKCE first; device = device only.
        tokens: dict | None = None
        mode = (oauth_mode or "auto").strip().lower()
        errors: list[str] = []

        async def _try_device() -> dict:
            _log("  OAuth mode=device (SSO cookie → device code)")
            return await device_fallback(page, cred.email, cred.password)

        async def _try_pkce() -> dict:
            _log("  OAuth mode=pkce (browser Allow → callback)")
            return await obtain_pkce_tokens(
                page,
                email=cred.email,
                password=cred.password,
                referrer=referrer,
                timeout_sec=75,
            )

        order: list[str]
        if mode in ("device", "sso", "http"):
            order = ["device"]
        elif mode in ("pkce", "browser", "oidc"):
            order = ["pkce", "device"]
        else:
            order = ["device", "pkce"]  # auto

        for step in order:
            try:
                if step == "device":
                    tokens = await _try_device()
                else:
                    tokens = await _try_pkce()
                break
            except Exception as e:
                errors.append(f"{step}: {e}")
                _log(f"  {step} failed: {e}")
                tokens = None

        if tokens is None:
            result["error"] = " | ".join(errors)[:400] or "oauth failed"
            result["status"] = "failed_oauth"
            update_account_status(
                cred.email,
                "failed_oauth",
                password=cred.password,
                error=result["error"],
                has_oauth=False,
            )
            return result

        result["has_oauth"] = True
        result["auth_mode"] = tokens.get("auth_mode")
        result["build_email"] = tokens.get("email")
        access = tokens["access_token"]
        tok_obj = tokens.get("_obj")

        # 4) Probe
        model = str(gcli.get("chat_probe_model") or "grok-4.5")
        _log(f"  probe model={model}")
        probe = probe_chat_usable(
            access, email=tokens.get("email") or cred.email, model=model, timeout=45
        )
        result["probe_status"] = probe.get("status")
        result["usable"] = bool(probe.get("usable"))
        if not result["usable"]:
            result["error"] = str(probe.get("err") or probe.get("status"))
            result["status"] = "failed_probe"
            update_account_status(
                cred.email,
                "failed_probe",
                password=cred.password,
                error=result["error"],
                probe_status=probe.get("status"),
                has_oauth=True,
                injected=False,
            )
            return result

        _log(f"  USABLE reply={str(probe.get('reply') or '')[:40]!r}")

        # 5) Inject
        if not inject or gcli.get("enabled") is False:
            result["ok"] = True
            result["status"] = "usable"
            update_account_status(
                cred.email,
                "usable",
                password=cred.password,
                probe_status=200,
                has_oauth=True,
                injected=False,
            )
            return result

        if tok_obj is None:
            tok_obj = BuildTokens(
                access_token=access,
                refresh_token=tokens.get("refresh_token") or "",
                email=tokens.get("email") or cred.email,
                user_id=str(tokens.get("user_id") or ""),
                expires_at=str(tokens.get("expires_at") or ""),
                expires_in=int(tokens.get("expires_in") or 21600),
            )
        try:
            out = push_build_tokens_to_9router(
                tok_obj,
                base_url=str(gcli.get("base_url") or "http://localhost:20127"),
                data_dir=str(gcli.get("data_dir") or "~/.9router"),
                password=str(gcli.get("password") or ""),
                email=tokens.get("email") or cred.email,
                name=tokens.get("email") or cred.email,
                smoke_bot_flag=False,
            )
            result["injected"] = True
            result["ok"] = True
            result["status"] = "injected"
            result["conn_id"] = out.get("id")
            update_account_status(
                cred.email,
                "injected",
                password=cred.password,
                probe_status=200,
                has_oauth=True,
                injected=True,
                extra={"conn_id": out.get("id")},
            )
            _log(f"  injected id={out.get('id')}")
        except Exception as e:
            result["error"] = f"push: {e}"
            result["status"] = "failed_push"
            result["usable"] = True
            update_account_status(
                cred.email,
                "failed_push",
                password=cred.password,
                error=str(e)[:300],
                probe_status=200,
                has_oauth=True,
                injected=False,
            )
        return result


async def run_batch(args: argparse.Namespace) -> int:
    conf = load_config()
    gcli = conf.get("grok_cli") if isinstance(conf.get("grok_cli"), dict) else {}
    status_filter = None
    if args.status:
        status_filter = {s.strip() for s in args.status.split(",") if s.strip()}

    creds = load_creds(
        file=Path(args.file) if args.file else None,
        email=args.email or "",
        password=args.password or "",
        status_filter=status_filter,
        limit=args.limit or args.n or 0,
        force=bool(getattr(args, "force", False)),
        jsonl_only=bool(getattr(args, "jsonl_only", False)),
    )
    if not creds:
        _log(
            "no credentials found — check accounts/email_pass.txt, "
            "--status / --jsonl-only, or --email/--password "
            "(done accounts are skipped by default)"
        )
        return 2

    headless = not args.headed
    if args.headless:
        headless = True
    activate = not args.no_activate
    if gcli.get("activate_grok_com") is False and not args.activate:
        activate = False
    inject = not args.no_inject
    referrer = (
        args.referrer
        or str(gcli.get("oauth_referrer") or "grok-build")
        or "grok-build"
    )
    try:
        gap = float(args.gap if args.gap is not None else gcli.get("oauth_gap_sec") or 8)
    except (TypeError, ValueError):
        gap = 8.0

    _log(
        f"login_to_build  n={len(creds)}  concurrent={args.concurrent}  "
        f"headless={headless}  activate={activate}  inject={inject}  "
        f"referrer={referrer}"
    )

    sem = asyncio.Semaphore(max(1, int(args.concurrent)))
    stats = {"ok": 0, "usable": 0, "injected": 0, "fail": 0}
    last_oauth = [0.0]
    lock = asyncio.Lock()

    async def one(i: int, cred: Cred):
        async with sem:
            async with lock:
                wait = gap - (time.time() - last_oauth[0])
                if wait > 0 and i > 1:
                    await asyncio.sleep(wait)
                last_oauth[0] = time.time()
            _log(f"[{i}/{len(creds)}] START {cred.email}  prev_status={cred.status or '-'}")
            t0 = time.time()
            try:
                row = await process_one(
                    cred,
                    headless=headless,
                    activate=activate,
                    inject=inject,
                    referrer=referrer,
                    conf=conf,
                    gap_sec=gap,
                    oauth_mode=str(args.oauth or "auto"),
                )
            except Exception as e:
                err = str(e)
                # Infra (Camoufox missing, browser crash) must NOT poison account
                # as failed_oauth — that would skip forever on next run.
                if is_infra_error(err):
                    st_err = "error_infra"
                    update_account_status(
                        cred.email,
                        "error_infra",
                        password=cred.password,
                        error=err[:300],
                    )
                else:
                    st_err = "failed_oauth"
                    update_account_status(
                        cred.email,
                        "failed_oauth",
                        password=cred.password,
                        error=err[:300],
                    )
                row = {
                    "email": cred.email,
                    "ok": False,
                    "error": err,
                    "status": st_err,
                }
            dt = time.time() - t0
            st = row.get("status") or ("ok" if row.get("ok") else "fail")
            if row.get("injected"):
                stats["injected"] += 1
                stats["ok"] += 1
                stats["usable"] += 1
                flag = "✓ inject"
            elif row.get("usable"):
                stats["usable"] += 1
                stats["ok"] += 1
                flag = "○ usable"
            else:
                stats["fail"] += 1
                flag = "✗"
            _log(
                f"[{i}/{len(creds)}] {flag} {cred.email}  status={st}  "
                f"{dt:.0f}s  {(row.get('error') or '')[:100]}"
            )
            return row

    tasks = [one(i, c) for i, c in enumerate(creds, 1)]
    await asyncio.gather(*tasks)
    _log(
        f"DONE  injected={stats['injected']} usable={stats['usable']} "
        f"fail={stats['fail']} total={len(creds)}"
    )
    return 0 if stats["fail"] < len(creds) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--file",
        "-f",
        default="",
        help="email:password file (default accounts/email_pass.txt)",
    )
    p.add_argument("--email", default="", help="single email")
    p.add_argument("--password", default="", help="single password")
    p.add_argument(
        "--status",
        default="",
        help=(
            "get-by-status from accounts.jsonl (CSV), e.g. "
            "failed_oauth,login_ok,failed_probe — password diisi dari jsonl "
            "atau fallback email_pass"
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="reprocess even if jsonl status is terminal (failed_*, usable, …)",
    )
    p.add_argument(
        "--jsonl-only",
        action="store_true",
        help="queue only from accounts.jsonl (pending/created/login_ok), ignore email_pass inventory",
    )
    p.add_argument("-n", "--limit", type=int, default=0, help="max accounts (0=all)")
    p.add_argument("-c", "--concurrent", type=int, default=1, help="parallel browsers")
    p.add_argument("--headed", action="store_true", help="visible browser")
    p.add_argument("--headless", action="store_true", help="force headless")
    p.add_argument("--no-activate", action="store_true", help="skip grok.com activate")
    p.add_argument("--activate", action="store_true", help="force activate on")
    p.add_argument("--no-inject", action="store_true", help="skip 9router push")
    p.add_argument("--referrer", default="", help="oauth referrer (default grok-build)")
    p.add_argument(
        "--oauth",
        default="auto",
        choices=("auto", "device", "pkce"),
        help="auto=device then PKCE (recommended after login); device only; pkce first",
    )
    p.add_argument("--gap", type=float, default=None, help="seconds between OAuth mints")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run_batch(args))
    except KeyboardInterrupt:
        _log("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

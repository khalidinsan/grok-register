# Grok Register Farm

Automated Grok (x.ai) account registration with [DrissionPage](https://github.com/g1879/DrissionPage).

Flow:

1. Create a catch-all alias (`random@yourdomain.com`)
2. Complete signup in Chrome (Turnstile patched via extension)
3. Read OTP from Gmail IMAP
4. Capture Web SSO cookies
5. Convert SSO → **Grok Build / CLI OAuth** (Device Flow)
6. Push the account into **9router** as provider `grok-cli` (optional)

> x.ai rejects common disposable mail domains. Use your own catch-all domain.

---

## Features

- Catch-all domain + Gmail IMAP OTP (same pattern as alibaba farm)
- Cloudflare Turnstile helper (Chrome extension patches CDP `MouseEvent.screenX/Y`)
- **SSO → Build OAuth** conversion (`sso_to_build.py`)
- Auto-import into **9router `grok-cli`** (HTTP API; works with sql.js)
- Optional grok2api Web import (legacy / optional)
- **Multi-worker pool** (`run_pool.py`) for small concurrent farms
- **Unlimited mode** (`-n 0` / `--unlimited`) — farm until Ctrl+C / quit / Force stop
- **Terminal UI dashboard** (`farm_tui.py` / `run_pool.py --tui`) — live progress, per-worker table, filterable logs
- **Display modes**: `headed` · `offscreen` · `headless` (Mac-friendly offscreen default)
- Isolated Chrome per worker (unique CDP port + profile; no shared PortFinder collisions)

---

## Requirements

- Python **3.10–3.13** recommended (3.14 may hit TLS edge cases)
- Chrome / Chromium
- Your domain with **catch-all** → Gmail
- Gmail **App Password** (IMAP)
- Optional: [9router](https://github.com/) on `http://127.0.0.1:20127`
- Optional: [grok2api](https://github.com/chenyme/grok2api)

---

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Required: Playwright Chromium (isolated from your daily Google Chrome)
playwright install chromium
```

The farm **always** prefers Playwright’s Chromium / Chrome for Testing binary. It will **not** use `/Applications/Google Chrome.app` unless you set `GROK_ALLOW_SYSTEM_CHROME=1`.

| Browser | Role |
|---------|------|
| **Google Chrome** (your app) | Daily work — untouched |
| **Playwright Chromium** | Farm only — own profile + CDP port |

Optional: `GROK_BROWSER_PATH=/path/to/chromium` to force a binary.

### Linux server (Xvfb)

```bash
apt install -y xvfb
pip install PyVirtualDisplay
playwright install chromium
python -m playwright install-deps chromium
```

macOS does **not** use Xvfb. Use display modes below. Farm windows are **Chromium**, not your Google Chrome.

---

## Config (`config.json`)

```bash
cp config.example.json config.json
```

Example:

```json
{
  "run": {
    "count": 1
  },
  "pool": {
    "workers": 2,
    "count": 1,
    "stagger_sec": 20,
    "display": "offscreen",
    "proxies": []
  },
  "email": {
    "domain": "yourdomain.com",
    "imap_user": "you@gmail.com",
    "imap_pass": "abcd efgh ijkl mnop",
    "imap_host": "imap.gmail.com",
    "imap_port": 993
  },
  "proxy": "",
  "browser_proxy": "",
  "grok_cli": {
    "enabled": true,
    "base_url": "http://127.0.0.1:20127",
    "data_dir": "~/.9router"
  },
  "grok2api": {
    "enabled": false,
    "base_url": "http://127.0.0.1:8000",
    "username": "admin",
    "password": "your-admin-password",
    "tier": "auto"
  },
  "ninerouter": {
    "enabled": false,
    "base_url": "http://127.0.0.1:20127",
    "provider": "grok-web",
    "data_dir": "~/.9router"
  }
}
```

Env overrides for email: `EMAIL_DOMAIN`, `IMAP_USER`, `IMAP_PASS`, `IMAP_HOST`, `IMAP_PORT`.

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `run.count` | int | Rounds for single-process mode (`0` = unlimited until stop). Overridden by `--count` / `--unlimited`. |
| `pool.workers` | int | Parallel processes for `run_pool.py` (Mac: start with 2–3). Alias of concurrent. |
| `pool.count` | int | **Total** accounts for the pool (`0` = unlimited). Overridden by `-n` / `--unlimited`. |
| `pool.concurrent` | int | How many browsers in parallel. |
| `pool.stagger_sec` | number | Delay (seconds) between starting workers. |
| `pool.display` | string | `headed` · `offscreen` · `headless` (default `offscreen` on Mac). |
| `pool.proxies` | string[] | Proxy URLs (or Webshare `host:port:user:pass`). |
| `pool.proxy_file` | string | Path to proxy list file (Webshare export OK). |
| `pool.proxy_mode` | string | `per_account` (default, 1 proxy / account rotate) or `per_worker` (sticky). |
| `email.domain` | string | Catch-all domain → generates `random@domain`. |
| `email.imap_user` | string | Gmail address that receives catch-all mail. |
| `email.imap_pass` | string | Gmail App Password (not your login password). |
| `email.imap_host` | string | Default `imap.gmail.com`. |
| `email.imap_port` | int | Default `993`. |
| `browser_proxy` | string | Global browser proxy (overridden per worker by `GROK_BROWSER_PROXY`). |
| `grok_cli.enabled` | bool | Convert SSO → Build OAuth and push to 9router `grok-cli`. |
| `grok_cli.base_url` | string | 9router base URL (default `http://127.0.0.1:20127`). |
| `grok_cli.smoke_bot_flag` | bool | If JWT has `bot_flag_source`, smoke-test before import (default `true`). |
| `grok_cli.smoke_model` | string | Model for bot-flag smoke (default `grok-4.5`). |
| `grok_cli.smoke_timeout_sec` | number | Smoke HTTP timeout seconds (default `45`). |
| `grok2api.enabled` | bool | Optional import of Web SSO into grok2api. |
| `ninerouter.enabled` | bool | Legacy grok-web cookie import (usually leave `false`). |

---

## Email setup (catch-all + IMAP)

1. Point your domain to Cloudflare Email Routing (or similar).
2. Enable **Catch-All** → forward to your Gmail.
3. Enable Google 2FA and create an [App Password](https://myaccount.google.com/apppasswords).
4. Fill `email.*` in `config.json`.

---

## Usage

### Single process (1 browser)

```bash
source .venv/bin/activate

# Use run.count from config
python DrissionPage_example.py

# Explicit rounds
python DrissionPage_example.py --count 5
# short flag:
python DrissionPage_example.py -n 5

# Unlimited (until Ctrl+C)
python DrissionPage_example.py --count 0
python DrissionPage_example.py --unlimited
python DrissionPage_example.py -u

# Display modes
python DrissionPage_example.py --offscreen          # Mac-friendly (default intent)
python DrissionPage_example.py --headless
python DrissionPage_example.py --display headed     # visible windows (debug)
```

### Concurrent pool (multi-process)

Simple API: **total accounts** + **how many parallel browsers**.

```bash
# “I want 100 accounts, 3 at a time”
python run_pool.py --count 100 --concurrent 3
# short flags:
python run_pool.py -n 100 -c 3

# Split example: 100 ÷ 3 → workers get 34 + 33 + 33
python run_pool.py -n 100 -c 3 --dry-run

# Unlimited — every worker runs forever until Ctrl+C / quit
python run_pool.py --unlimited -c 2 --offscreen
python run_pool.py -u -c 2 --offscreen
python run_pool.py -n 0 -c 2 --offscreen

# Mac-friendly window (minimized Chromium, not your Google Chrome)
python run_pool.py -n 10 -c 2 --offscreen

# Debug with visible windows
python run_pool.py -n 2 -c 1 --display headed

# Proxies (round-robin to workers)
python run_pool.py -n 20 -c 3 --proxy-file proxies.txt

# Live Terminal UI (recommended for multi-worker)
python run_pool.py --tui -n 20 -c 3 --stagger 5 --offscreen
python run_pool.py --tui --unlimited -c 2 --offscreen
# same thing:
python farm_tui.py -n 20 -c 3 --stagger 5 --offscreen
python farm_tui.py -u -c 2 --offscreen
```

### Terminal UI (`farm_tui.py`)

Dashboard instead of interleaved stdout:

| Panel | What you see |
|-------|----------------|
| **Summary** | global `done/total` (or `done=N · ∞ mode`), ✓/✗, rate/min, progress bar |
| **Workers table** | per-worker status, phase (EMAIL/OTP/PROFILE/…), email, last msg |
| **Live log** | structured lines; filter by worker |

| Key | Action |
|-----|--------|
| `q` | stop all workers & quit (also closes Chromium) |
| `a` | show all workers in log |
| `1`–`9` | filter log to that worker |
| `p` | pause / resume log auto-scroll |

| Flag | Meaning |
|------|---------|
| `-n` / `--count` | **Total** accounts to create (`0` = unlimited until stop) |
| `-u` / `--unlimited` | Same as `-n 0` — farm forever until Ctrl+C / quit |
| `-c` / `--concurrent` | How many browsers run in parallel |
| `--stagger` | Seconds between *starting* each worker |
| `--tui` | Launch live dashboard (`farm_tui`) |
| `--dry-run` | Print split plan only |

### Unlimited mode

`0` and `--unlimited` mean **no target total**: each concurrent worker keeps registering until you stop it.

| Surface | How to enable | How to stop |
|---------|---------------|-------------|
| Single process | `-n 0` / `-u` / `--unlimited` | `Ctrl+C` |
| Pool / TUI | `-n 0` / `-u` / `--unlimited` | `Ctrl+C` or `q` in TUI |
| 9router Add Account | **Total accounts = 0** | **Force stop** |

In unlimited mode, progress shows `done/∞` (no ETA to a finish target; acc/min still updates).

Config `pool` in `config.json`:

```json
"pool": {
  "count": 100,
  "concurrent": 3,
  "stagger_sec": 20,
  "display": "offscreen"
}
```

### Display modes (flash-aligned)

Defaults (same idea as flash-grok-farm):

| Platform | Default | Engine default |
|----------|---------|----------------|
| **Linux / VPS** | **`headless`** | **Camoufox** |
| **Windows** | **`headless`** | **Camoufox** |
| **macOS** | **`offscreen`** | **Camoufox** |

| Mode | Window | Turnstile | When to use |
|------|--------|-----------|-------------|
| **`headless`** | None (Camoufox `headless=True`) | Usually OK with Camoufox + good proxy | **Linux/VPS farm (flash default)** |
| **`virtual`** | Headed on **Xvfb** (Camoufox `headless='virtual'`) | Better if pure headless fails CF | Linux without GUI; needs `xvfb` |
| **`offscreen`** | Parked off-screen + hide | Usually OK | **Mac while working** |
| **`headed`** | Visible | Most reliable for debug | Turnstile / CF debug |

CLI / env (flash-compatible):

```bash
# shortcuts
--headless | --virtual | --offscreen | --headed
--display headless|virtual|offscreen|headed

# env (same flags as flash-grok-farm)
export GROK_HEADLESS=true          # → headless
export GROK_HEADLESS=false         # → headed
export GROK_DISPLAY=virtual        # Camoufox + Xvfb
export GROK_BROWSER_ENGINE=camoufox

# Linux VPS helper (auto xvfb-run when needed)
./run_linux.sh farm_tui.py -u -c 2
./run_linux.sh --virtual farm_tui.py -u -c 1
GROK_HEADLESS=true ./run_linux.sh run_pool.py -n 10 -c 2
```

Mac daily farm (unchanged ergonomics):

```bash
python farm_tui.py -u -c 2 --offscreen   # or omit flags → offscreen on darwin
```

---

## Pipeline details

### Per successful registration

1. Register on `accounts.x.ai`
2. Wait for OTP via IMAP (header-only poll from x.ai when possible)
3. Settle SSO cookies (brief wait before opening grok.com)
4. Save SSO line under `sso/`
5. If `grok_cli.enabled` (flash-aligned pipeline):
   - **SETTLE** — idle after signup (`post_signup_settle_sec`, default 12s)
   - **CONVERT** — browser **PKCE** with `referrer=grok-build` (same session); fallback device SSO
   - **PROBE** — chat usable on `cli-chat-proxy` (`inject_policy=usable`); **403 DENIED → no inject**
   - **PUSH** — only if usable → `POST /api/providers` as **`grok-cli`**

Browser engine (`browser.engine` / `GROK_BROWSER_ENGINE`): **`chromium`** (default) or **`camoufox`**. Both use **native proxy** `{server, username, password}` (not user:pass in `--proxy-server`).

Email: `email.local_style=human` → `first.last` style aliases (not pure random).
6. Optional grok2api Web import if enabled

### 9router notes

- Import must go through the **HTTP API** (sql.js in-memory: raw SQLite writes are invisible to the UI).
- Dashboard → **Add Account → Grok CLI (Register)**: set **Total accounts** + concurrent; use **`0` for unlimited** (stop with Force stop).
- Quota page (`/dashboard/quota`) for free Build accounts shows estimated **Free tokens (est. 24h)** ≈ local usage / 1,000,000 (same idea as grok2api free estimate).

### Concurrent isolation

Each worker process gets:

- Unique CDP port (`GROK_DEBUG_PORT`, e.g. ~9320, ~9340)
- Unique Chrome user-data-dir
- Optional unique proxy (`GROK_BROWSER_PROXY`)
- Separate logs / SSO files

Do **not** share one Chrome profile across workers.

---

## Outputs

```
sso/
  sso_<timestamp>_w<N>.txt    # SSO lines (per worker)
logs/
  run_<timestamp>_w<N>.log    # Per-worker run log
```

Log lines include email/password and status (`CREATED`, etc.).

---

## Project layout

```
├── DrissionPage_example.py   # Main farm (single process)
├── run_pool.py               # Concurrent multi-process launcher
├── farm_tui.py               # Terminal UI dashboard (Textual)
├── email_register.py         # Catch-all + IMAP OTP
├── sso_to_build.py           # SSO → Grok Build Device OAuth
├── push_9router_grok_cli.py  # Push tokens to 9router grok-cli
├── config.json               # Local config (not committed)
├── config.example.json       # Template
├── requirements.txt
├── turnstilePatch/           # Chrome extension for Turnstile mouse coords
├── sso/                      # SSO output (auto-created)
└── logs/                     # Run logs (auto-created)
```

---

## Linux server tips

- Prefer Playwright Chromium over snap Chromium (AppArmor).
- If x.ai is blocked, set `browser_proxy` or `pool.proxies`.
- Script auto-starts Xvfb on Linux when no `DISPLAY` (or `USE_XVFB=1`).
- For larger concurrent farms: Linux + Xvfb + **1 residential proxy per worker** is safer than many workers on one home IP.

---

## Quick start (Mac, small concurrent)

```bash
source .venv/bin/activate
# ensure 9router is up if grok_cli.enabled
python run_pool.py --dry-run
python run_pool.py -n 2 -c 2 --offscreen
# or unlimited until Ctrl+C:
# python run_pool.py --unlimited -c 2 --offscreen
```

Watch logs:

```bash
tail -f logs/run_*_w1.log
```

---

## Credits

- [kevinr229/grok-maintainer](https://github.com/kevinr229/grok-maintainer) — original project
- [grok2api](https://github.com/chenyme/grok2api) — Grok gateway / free-quota model reference
- Catch-all + Gmail IMAP for verification codes

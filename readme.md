# Grok Register Farm

Automated **xAI / Grok** account registration farm: signup → SSO → **Grok Build / CLI OAuth** → chat usable probe → inject **9router** (`grok-cli`).

Default browser engine: **Camoufox** (anti-detect Firefox). Optional **Chromium** (Playwright) fallback.

```text
email (IMAP catch-all **or** mailer.exzork.me API)
  → register (browser full UI  |  hybrid: short browser + protocol HTTP)
  → SSO cookies (wrapper → session materialize if needed)
  → SETTLE (bot hygiene, default 12s)
  → OAuth PKCE referrer=grok-build  (fallback: device OAuth, fail-fast)
  → PROBE cli-chat-proxy (inject only if USABLE)
  → PUSH 9router grok-cli
```

> x.ai rejects common disposable mail domains. Use your **own catch-all domain**.

**Research / personal use only.** Respect site ToS and local law.

---

## Features

| Area | Notes |
|------|--------|
| **Engines** | Camoufox (default) · Playwright Chromium fallback |
| **Register modes** | `browser` full UI · `hybrid` (castle harvest + protocol HTTP) |
| **Mail** | IMAP Gmail catch-all **or** [exzork](https://mailer.exzork.me/) API (wildcard subdomains) · humanized local-parts |
| **OAuth** | Browser **PKCE** `referrer=grok-build` · device SSO fallback (fail-fast) |
| **Inject policy** | `usable` — chat probe **200** only; **402/403 DENIED never inject** |
| **Proxy** | Pool file, `per_account` / `per_worker`, health check, retry → direct |
| **Asset block** | Optional third-party font/media block (bandwidth) |
| **Farm** | Multi-worker pool · unlimited mode · Textual TUI |
| **Display** | `headless` (Linux) · `offscreen` (Mac) · `virtual` (Xvfb) · `headed` |

---

## Requirements

| | |
|--|--|
| **OS** | macOS · Linux (VPS OK) · Windows |
| **Python** | **3.10–3.13** recommended (3.14 may hit TLS edge cases) |
| **Mail** | Domain with **catch-all** → Gmail + [App Password](https://myaccount.google.com/apppasswords) |
| **Optional** | Local [9router](https://github.com/) (`http://127.0.0.1:20127` or remote URL) |
| **Optional** | Residential proxies (`proxy.txt`) |

---

## Install

### 1) Clone & venv

```bash
cd grok-register
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` includes: Camoufox, Playwright, Textual, requests, **curl_cffi** (hybrid + device TLS).

### 2) Browser binaries (required)

```bash
# Camoufox binary (default engine) — run once per machine
python -m camoufox fetch

# Chromium fallback (optional but recommended)
playwright install chromium
```

| Binary | Role |
|--------|------|
| **Camoufox** | Default farm engine |
| **Playwright Chromium** | Fallback / legacy extension path |
| **Google Chrome.app** | **Not used** (daily browser stays clean) |

Optional: `GROK_BROWSER_PATH=/path/to/chromium` to force Chromium binary.

### 3) Config

```bash
cp config.example.json config.json
# edit config.json — never commit secrets
```

### 4) Proxies (optional)

```bash
# one proxy per line — supported formats:
#   host:port:user:pass
#   http://user:pass@host:port
#   user:pass@host:port
cp proxy.txt.example proxy.txt   # if you have an example
# or write your own proxy.txt
```

Residential sticky tip: **~30 min** session duration is a good default (reg + OAuth ~1–2 min).

---

## Linux / VPS setup (complete)

Do **not** run as **root** (Camoufox XPCOM often breaks).

```bash
# Debian / Ubuntu example
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  xvfb \
  libgtk-3-0 libx11-xcb1 libdbus-glib-1-2 \
  libasound2t64 || sudo apt install -y libasound2

# project
git clone <your-fork-or-repo> grok-register
cd grok-register
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch
playwright install chromium
# if Chromium fails to launch:
python -m playwright install-deps chromium || true

cp config.example.json config.json
# fill email.*, grok_cli.*, register_mode, pool.proxy_file, etc.
# add proxy.txt if using residential proxies
```

### Run on Linux

```bash
# Preferred helper (defaults: Camoufox + headless; xvfb-run if needed)
chmod +x run_linux.sh
./run_linux.sh farm_tui.py -u -c 2 --stagger 15 \
  --display headless --proxy-file proxy.txt --proxy-mode per_account

# Pure headless without wrapper
GROK_DISPLAY=headless GROK_BROWSER_ENGINE=camoufox \
  .venv/bin/python farm_tui.py -u -c 2 --display headless \
  --proxy-file proxy.txt --proxy-mode per_account

# If pure headless fails CF often — headed on virtual X:
./run_linux.sh --virtual farm_tui.py -u -c 1 --proxy-file proxy.txt
```

| Flag / env | Meaning |
|------------|---------|
| `./run_linux.sh` | Activates `.venv`, sets Camoufox + headless by default |
| `--headless` | `GROK_DISPLAY=headless` |
| `--virtual` | Camoufox on Xvfb (`needs xvfb`) |
| `--headed` | Real display (VNC / desktop) |
| **Never root** | `run_linux.sh` exits if `id -u == 0` |

---

## macOS setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m camoufox fetch
playwright install chromium
cp config.example.json config.json
# edit config.json + proxy.txt
```

Default display on Mac: **`offscreen`** (parked windows, work-friendly).

```bash
.venv/bin/python farm_tui.py -u -c 2 --offscreen \
  --proxy-file proxy.txt --proxy-mode per_account

# debug visible
.venv/bin/python farm_tui.py -c 1 --display headed
```

---

## Config (`config.json`)

```bash
cp config.example.json config.json
```

### Minimal example

```json
{
  "run": { "count": 1 },
  "register_mode": "hybrid",
  "pool": {
    "count": 0,
    "concurrent": 2,
    "stagger_sec": 15,
    "display": "headless",
    "proxy_mode": "per_account",
    "proxy_file": "proxy.txt",
    "proxy_check": true,
    "proxy_retries": 3,
    "proxy_fallback_direct": true,
    "block_assets": true
  },
  "email": {
    "domain": "yourdomain.com",
    "imap_user": "you@gmail.com",
    "imap_pass": "abcd efgh ijkl mnop",
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "local_style": "human"
  },
  "browser": { "engine": "camoufox" },
  "grok_cli": {
    "enabled": true,
    "base_url": "http://127.0.0.1:20127",
    "password": "your-9router-dashboard-password",
    "data_dir": "~/.9router",
    "oauth_mode": "pkce",
    "oauth_referrer": "grok-build",
    "post_signup_settle_sec": 12,
    "oauth_gap_sec": 8,
    "chat_probe_off_critical": true,
    "inject_policy": "usable",
    "jwt_reject_bot_flag": false,
    "jwt_enforce_referrer": true,
    "chat_probe_model": "grok-4.5"
  }
}
```

Env overrides for email: `EMAIL_DOMAIN`, `EMAIL_PROVIDER`, `IMAP_*`, `EXZORK_API_KEY`, `EXZORK_USE_SUBDOMAIN`.

### Important fields

| Field | Description |
|-------|-------------|
| `register_mode` | `browser` (full UI) · `hybrid` (protocol after short harvest). Default if unset: **browser**. Env: `GROK_REGISTER_MODE`. |
| `pool.concurrent` / `-c` | Parallel workers (browsers). |
| `pool.count` / `-n` | Total accounts (`0` = unlimited). |
| `pool.display` | `headless` · `offscreen` · `virtual` · `headed`. |
| `pool.proxy_file` | Path to proxy list. |
| `pool.proxy_mode` | `per_account` (rotate) · `per_worker` (sticky). |
| `pool.proxy_check` | Probe proxies before spawn (default on). |
| `pool.proxy_retries` | Distinct proxies per account before fail (default 3). |
| `pool.proxy_fallback_direct` | After proxy fails → try **direct** (default true). |
| `pool.block_assets` | Abort third-party font/media (`GROK_BLOCK_ASSETS`). |
| `browser.engine` | `camoufox` (default) · `chromium`. |
| `grok_cli.post_signup_settle_sec` | **Bot hygiene** idle before OAuth (default **12**). |
| `grok_cli.oauth_gap_sec` | Min seconds between OAuth mints (default 8). |
| `grok_cli.chat_probe_off_critical` | Soft-reset browser before HTTP probe (default true). |
| `grok_cli.inject_policy` | `usable` · `jwt_clean` · `all`. |
| `grok_cli.oauth_mode` | `pkce` (preferred) · device fallback automatic. |

---

## Environment variables (quick ref)

| Env | Default | Meaning |
|-----|---------|---------|
| `GROK_BROWSER_ENGINE` | `camoufox` | `camoufox` \| `chromium` |
| `GROK_DISPLAY` / `GROK_HEADLESS` | platform | Display mode |
| `GROK_REGISTER_MODE` | config / `browser` | `hybrid` \| `browser` |
| `GROK_BLOCK_ASSETS` | `1` | Font/media asset block |
| `GROK_PROXY_RETRIES` | `3` | Proxy tries per account |
| `GROK_PROXY_FALLBACK_DIRECT` | `1` | Fall back to direct |
| `GROK_OAUTH_GAP_SEC` | `8` | Gap between converts |
| `GROK_CHAT_PROBE_OFF_CRITICAL` | `1` | Defer probe after OAuth |
| `GROK_DEVICE_RL_MAX_TRIES` | `2` | Device OAuth rate-limit tries |
| `GROK_DEVICE_POLL_TIMEOUT_SEC` | `45` | Device token poll cap |
| `GROK_HYBRID_USE_DRISSION_TS` | off | Opt-in slow Drission Turnstile path |
| `GROK_BROWSER_PROXY` / `GROK_PROXIES` | — | Worker proxy (set by pool/TUI) |

---

## Email setup

### A) IMAP catch-all (default)

1. Cloudflare Email Routing (or similar) catch-all → Gmail.
2. Google 2FA → [App Password](https://myaccount.google.com/apppasswords).
3. Config:

```json
"email": {
  "provider": "imap",
  "domain": "yourdomain.com",
  "imap_user": "you@gmail.com",
  "imap_pass": "app-password",
  "imap_host": "imap.gmail.com",
  "imap_port": 993,
  "local_style": "human"
}
```

### B) mailer.exzork.me (`provider: exzork`)

Receive-only API with **wildcard subdomains** (`user@rand.yourdomain.com`).

1. DNS (Cloudflare DNS only, not Email Routing MX):

```text
@  MX  10  mailer.exzork.me
*  MX  10  mailer.exzork.me
```

2. Claim apex + wildcard at https://mailer.exzork.me/ — **save API key once**.
3. Config / env (prefer env for the key):

```bash
export EXZORK_API_KEY='tm_...'
```

```json
"email": {
  "provider": "exzork",
  "domain": "koew.tech",
  "local_style": "human",
  "exzork_base_url": "https://mailer.exzork.me",
  "exzork_use_subdomain": true,
  "exzork_api_key": ""
}
```

| Field | Meaning |
|-------|---------|
| `provider` | `imap` \| `exzork` |
| `exzork_use_subdomain` | `true` → `local@<random>.domain.com` (needs wildcard MX) |
| `EXZORK_API_KEY` | Preferred over putting key in config |

Never commit API keys.

---

## Usage

### Single process

```bash
source .venv/bin/activate

python DrissionPage_example.py -n 5
python DrissionPage_example.py -u                    # unlimited
python DrissionPage_example.py -n 1 --display headed # debug
```

### Multi-worker / TUI (recommended)

```bash
# Mac
python farm_tui.py -u -c 2 --stagger 15 --offscreen \
  --proxy-file proxy.txt --proxy-mode per_account

# Linux (helper)
./run_linux.sh farm_tui.py -u -c 2 --stagger 15 \
  --display headless --proxy-file proxy.txt --proxy-mode per_account

# Skip proxy health check (if check is wrong but proxies work)
python farm_tui.py -c 1 --no-proxy-check --proxy-file proxy.txt
```

| Flag | Meaning |
|------|---------|
| `-n` / `--count` | Total accounts (`0` = unlimited) |
| `-u` / `--unlimited` | Same as `-n 0` |
| `-c` / `--concurrent` | Parallel workers |
| `--stagger` | Seconds between starting workers |
| `--display` / `--headless` / `--offscreen` / `--virtual` / `--headed` | Display |
| `--proxy-file` | Proxy list path |
| `--proxy-mode` | `per_account` \| `per_worker` |
| `--no-proxy-check` | Skip preflight probe |
| `--dry-run` | Print worker split only (`run_pool.py`) |

### TUI keys

| Key | Action |
|-----|--------|
| `q` | Stop all workers & quit |
| `a` | All workers in log |
| `1`–`9` | Filter log by worker |
| `p` | Pause / resume log scroll |

### Display modes

| Platform | Default |
|----------|---------|
| **Linux / VPS** | **`headless`** |
| **Windows** | **`headless`** |
| **macOS** | **`offscreen`** |

| Mode | When |
|------|------|
| `headless` | VPS farm (default Linux) |
| `virtual` | Linux no GUI, better CF than pure headless — needs `xvfb` |
| `offscreen` | Mac while working |
| `headed` | Debug Turnstile / CF |

---

## Register modes

| Mode | How | Config / env |
|------|-----|----------------|
| **`browser`** | Full Camoufox UI signup | default if unset |
| **`hybrid`** | Short browser (castle + cookies) → protocol HTTP signup → materialize SSO → same OAuth/probe | `"register_mode": "hybrid"` or `GROK_REGISTER_MODE=hybrid` |

Hybrid needs **`curl_cffi`** (in requirements). On failure, farm **falls back** to full browser UI automatically.

Turnstile on Camoufox hybrid: native poll + inject widget (not Drission shadow click). Legacy path: `GROK_HYBRID_USE_DRISSION_TS=1`.

---

## Pipeline (per account)

1. **Register** — `browser` or `hybrid` → SSO  
2. **SETTLE** — `post_signup_settle_sec` (default **12s** bot hygiene)  
3. **CONVERT** — PKCE `grok-build` on live session; prep tab after hybrid; device fallback fail-fast  
4. **PROBE** — `cli-chat-proxy` model `grok-4.5` (default off critical path)  
5. **PUSH** — only if **USABLE** (`inject_policy=usable`)

| Probe | Meaning |
|-------|---------|
| **200** + reply | USABLE → inject 9router |
| **402** spending-limit | Token OK, no free credits — **not** injected |
| **403** | Chat denied — not injected |

Proxy failures during convert may retry **direct** (`proxy_fallback_direct`).

---

## Outputs

```text
sso/
  sso_<timestamp>_w<N>.txt
logs/
  run_<timestamp>_w<N>.log
```

```bash
tail -f logs/run_*_w1.log
```

---

## Project layout

```text
├── DrissionPage_example.py   # Main worker (register + OAuth + probe + push)
├── farm_tui.py               # Textual multi-worker dashboard
├── run_pool.py               # CLI multi-process launcher
├── run_linux.sh              # Linux/VPS launcher (headless / xvfb)
├── browser_engine.py         # Camoufox / Chromium session + asset-block
├── hybrid/                   # Hybrid register (castle + protocol)
├── sso_util.py               # SSO wrapper ↔ session materialize
├── sso_to_build.py           # Device OAuth (HTTP) fail-fast
├── build_oauth_pkce.py       # Browser PKCE referrer=grok-build
├── push_9router_grok_cli.py  # 9router grok-cli import
├── chat_usable.py            # Chat probe
├── proxy_util.py / proxy_health.py
├── config.example.json
├── requirements.txt
├── turnstilePatch/           # Chromium extension (optional path)
├── sso/  logs/               # outputs (auto)
```

---

## Troubleshooting

| Symptom | What to check |
|---------|----------------|
| Camoufox won't start | `python -m camoufox fetch`; non-root user; Linux libs |
| `proxy … @null:10000` | Bad proxy export — need real host:port |
| All proxies DROP | Format / plan / `curl -x … https://accounts.x.ai/` |
| `get_turnstile_fn failed` (old) | Update to latest; hybrid uses inject path now |
| PKCE `NS_BINDING_ABORTED` | Fixed via PKCE prep after hybrid; update main |
| Device approve hang 7min | Fixed fail-fast (~45s); update main |
| **402 spending-limit** | xAI free quota / policy — not a local crash |
| Hybrid always fallback | Check logs for castle/OTP; `curl_cffi` installed? |
| 9router empty after PASS | `grok_cli.base_url` + password; import only on USABLE |

---

## Quick starts

**Mac**

```bash
source .venv/bin/activate
python farm_tui.py -u -c 2 --offscreen --proxy-file proxy.txt --proxy-mode per_account
```

**Linux VPS**

```bash
source .venv/bin/activate
./run_linux.sh farm_tui.py -u -c 2 --stagger 15 \
  --display headless --proxy-file proxy.txt --proxy-mode per_account
```

**One account debug (headed)**

```bash
GROK_REGISTER_MODE=hybrid python farm_tui.py -c 1 --display headed --no-proxy-check
```

---

## Credits

- [kevinr229/grok-maintainer](https://github.com/kevinr229/grok-maintainer) — original project lineage  
- flash-grok-farm / community OAuth + farm patterns  
- [grok2api](https://github.com/chenyme/grok2api) — gateway / free-quota reference  
- Catch-all + Gmail IMAP for verification codes  

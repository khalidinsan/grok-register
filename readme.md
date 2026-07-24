# Grok Register Farm

Automated **xAI / Grok** account registration farm: signup → SSO → **Grok Build / CLI OAuth** → chat usable probe → inject **9router** (`grok-cli`).

Default browser engine: **Camoufox** (anti-detect Firefox). Optional **Chromium** (Playwright) fallback.

```text
email (IMAP catch-all  |  mailer.exzork.me API)
  → register (browser full UI  |  hybrid: short browser + protocol HTTP)
  → SSO cookies (wrapper → session materialize if needed)
  → SAVE accounts/ (email + password + status=created)  +  sso/*.txt (cookie only)
  → ACTIVATE grok.com (SSO session — replaces idle bot-hygiene sleep)
  → OAuth PKCE referrer=grok-build  (Camoufox)
      · token exchange prefers Playwright browser context (less invalid_grant)
      · Chromium default: device SSO (skip :56121 lock)
      · device fallback fail-fast if PKCE dies
  → PROBE cli-chat-proxy (inject only if USABLE)
  → PUSH 9router grok-cli  →  accounts status=injected
  → on fail: accounts status=failed_oauth | failed_probe | failed_push
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
| **Password** | **Fixed** for all accounts via `account.password` (not random) · env `GROK_ACCOUNT_PASSWORD` |
| **Account ledger** | `accounts/accounts.jsonl` (email/pass + pipeline **status**) · `accounts/email_pass.txt` |
| **OAuth** | Browser **PKCE** `referrer=grok-build` · exchange via **browser context** first · device SSO fallback |
| **Device OAuth** | HTTP device flow: approve `user_code` + `action=allow` · no empty `principal_id` · fail-fast RL |
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
| **Mail** | Domain with **catch-all** → Gmail + [App Password](https://myaccount.google.com/apppasswords) **or** exzork API |
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
# fill email.*, account.password, grok_cli.*, register_mode, pool.proxy_file, etc.
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
  "account": {
    "password": "Nfarm!a7#GrokBuild26"
  },
  "grok_cli": {
    "enabled": true,
    "base_url": "http://127.0.0.1:20127",
    "password": "your-9router-dashboard-password",
    "data_dir": "~/.9router",
    "oauth_mode": "auto",
    "oauth_referrer": "grok-build",
    "activate_grok_com": true,
    "activate_timeout_sec": 45,
    "post_signup_settle_sec": 0,
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
| `account.password` | **Fixed password for every farmed account** (not random). Env: `GROK_ACCOUNT_PASSWORD`. |
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
| `grok_cli.activate_grok_com` | Visit **grok.com** with SSO before OAuth (default **true**). Replaces idle bot-hygiene sleep; helps free-tier / chat activation. |
| `grok_cli.activate_timeout_sec` | Max wait for grok.com ready (default 45). |
| `grok_cli.post_signup_settle_sec` | Extra idle **after** activate (default **0**). Legacy 12s sleep only if activate is off. |
| `grok_cli.oauth_gap_sec` | Min seconds between OAuth mints (default 8). |
| `grok_cli.chat_probe_off_critical` | Soft-reset browser before HTTP probe (default true). |
| `grok_cli.inject_policy` | `usable` · `jwt_clean` · `all`. |
| `grok_cli.oauth_mode` | `auto` (PKCE on Camoufox, device on Chromium) · `pkce` · `device`. Force Chromium PKCE: `GROK_FORCE_CHROMIUM_PKCE=1`. |
| `grok_cli.oauth_referrer` | `grok-build` (default, 9router Build) · `cli-proxy-api` (free CLI style). See OAuth referrer section. |

---

## Environment variables (quick ref)

| Env | Default | Meaning |
|-----|---------|---------|
| `GROK_BROWSER_ENGINE` | `camoufox` | `camoufox` \| `chromium` |
| `GROK_DISPLAY` / `GROK_HEADLESS` | platform | Display mode |
| `GROK_REGISTER_MODE` | config / `browser` | `hybrid` \| `browser` |
| `GROK_ACCOUNT_PASSWORD` | config `account.password` | Fixed signup password for all accounts |
| `GROK_BLOCK_ASSETS` | `1` | Font/media asset block |
| `GROK_PROXY_RETRIES` | `3` | Proxy tries per account |
| `GROK_PROXY_FALLBACK_DIRECT` | `1` | Fall back to direct |
| `GROK_OAUTH_GAP_SEC` | `8` | Gap between converts |
| `GROK_ACTIVATE_GROK_COM` | `1` | Visit grok.com before OAuth (`0` = disable) |
| `GROK_ACTIVATE_TIMEOUT_SEC` | `45` | Activate wait cap |
| `GROK_CHAT_PROBE_OFF_CRITICAL` | `1` | Defer probe after OAuth |
| `GROK_DEVICE_RL_MAX_TRIES` | `2` | Device OAuth rate-limit tries |
| `GROK_DEVICE_POLL_TIMEOUT_SEC` | `45` | Device token poll cap |
| `GROK_FORCE_CHROMIUM_PKCE` | off | Force browser PKCE on Chromium (needs :56121) |
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

# Unlimited · 5 workers · headless
.venv/bin/python farm_tui.py -u -c 5 --display headless

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
| `headed` | Debug Turnstile / CF / OAuth consent |

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
2. **SAVE** — `sso/*.txt` (cookie) + `accounts/` (`status=created`, email + password)  
3. **ACTIVATE** — open **grok.com** with SSO (ToS / first session). Replaces idle 12s “bot hygiene”.  
4. **CONVERT** — PKCE `oauth_referrer` (default `grok-build`) on live session; Chromium prefers device SSO; device fallback fail-fast  
5. **PROBE** — `cli-chat-proxy` model `grok-4.5` (default off critical path)  
6. **PUSH** — only if **USABLE** (`inject_policy=usable`) → `status=injected`

### OAuth `referrer` — `grok-build` vs `cli-proxy-api`

Both use the **same** xAI OAuth client id (`b1a00492-…` Grok CLI). The `referrer=` query on `/oauth2/authorize` tells xAI **which product surface** minted the token. That value is often embedded in the JWT (`referrer` claim) and can affect which APIs / free tiers accept the token.

| `oauth_referrer` | Used by | Intended surface | Notes |
|------------------|---------|------------------|--------|
| **`grok-build`** (default here) | This farm → 9router **grok-cli** / Grok Build | Build / shell CLI path | Matches 9router import + `jwt_enforce_referrer` when enabled |
| **`cli-proxy-api`** | grok-sgb / some free CLI proxies | cli-chat-proxy / free CLI style | Different claim; may work on chat proxy but fail referrer gate for Build |

```text
authorize?…&client_id=b1a00492-…&referrer=grok-build     →  JWT often has referrer=grok-build
authorize?…&client_id=b1a00492-…&referrer=cli-proxy-api →  JWT often has referrer=cli-proxy-api
```

**Practical advice**

- Keep **`grok-build`** if the goal is **9router grok-cli / Grok Build**.  
- Only switch to **`cli-proxy-api`** if you intentionally want sgb-style free CLI tokens (and turn off hard referrer enforce if needed).  
- Do **not** mix referrers in one 9router pool if you enforce JWT referrer.

| Probe | Meaning | `accounts.status` |
|-------|---------|-------------------|
| **200** + reply | USABLE → inject 9router | `injected` |
| **402** spending-limit | Token may be OK later; free credits / soft limit — **not** injected | `failed_probe` |
| **403** | Chat denied — not injected (sometimes recovers later) | `failed_probe` |
| OAuth / `invalid_grant` / Access denied | No Build token or xAI denied grant | `failed_oauth` |

Proxy failures during convert may retry **direct** (`proxy_fallback_direct`).

> **402/403 are not always permanent.** Free-tier quota and soft denials can clear after hours. Prefer **reprobe** tokens that already have OAuth over mass-reconvert of dead SSO files.

---

## Outputs

```text
sso/                          # OUTPUT ONLY — farm never reads these back
  sso_<timestamp>_w<N>.txt    # one cookie-header line per created account

accounts/                     # gitignored (secrets)
  accounts.jsonl              # structured ledger: email, password, status, sso_cookie, …
  email_pass.txt              # email:password (all created accounts)

logs/
  run_<timestamp>_w<N>.log
```

### `sso/` vs `accounts/`

| Path | Written when | Read by farm? | Contents |
|------|----------------|---------------|----------|
| `sso/*.txt` | After SSO cookies exist | **No** (output only; offline reconvert tools may read) | Cookie header only |
| `accounts/accounts.jsonl` | After SSO + **updated** after OAuth/probe/push | No | email, password, **status**, SSO snapshot, errors |
| `accounts/email_pass.txt` | After create | No | `email:password` for every created account |

### Account status values (`accounts.jsonl`)

| `status` | Meaning |
|----------|---------|
| `created` | Register + SSO OK (has email/password/cookie) |
| `oauth_ok` | Build OAuth tokens minted (intermediate) |
| `injected` | Chat usable + pushed to 9router (**full success**) |
| `failed_oauth` | SSO OK but PKCE/device OAuth failed |
| `failed_probe` | OAuth OK but chat 402/403/denied (candidate for later reprobe) |
| `failed_push` | Usable but 9router import failed |

```bash
# full success
grep '"status": "injected"' accounts/accounts.jsonl

# OAuth OK but chat denied (maybe reprobe later)
grep '"status": "failed_probe"' accounts/accounts.jsonl

# register only / OAuth fail
grep '"status": "failed_oauth"' accounts/accounts.jsonl

tail -f logs/run_*_w1.log
```

### Offline reconvert (SSO → Build, no register)

`sso/*.txt` is **not** used by the live farm loop. You can convert stored cookies offline with:

```bash
# single cookie header / file
.venv/bin/python sso_to_build.py path/to/sso_line_or_file.txt
```

### Login → OAuth (from email:password stock)

For accounts that already exist (no register). Reads `accounts/email_pass.txt` or filters `accounts.jsonl` by status:

```bash
# batch from email_pass.txt (login → activate → PKCE → probe → inject usable)
.venv/bin/python login_to_build.py -n 5 -c 1 --headed

# only failed_probe / failed_oauth rows from accounts.jsonl
.venv/bin/python login_to_build.py --status failed_probe,failed_oauth -n 10 --headless

# single account
.venv/bin/python login_to_build.py --email you@domain.com --password 'YourPass' --headed

# mint + probe only (no 9router)
.venv/bin/python login_to_build.py -n 3 --no-inject
```

Flow: **login accounts.x.ai** → **activate grok.com** → **PKCE `grok-build`** (device fallback) → **chat probe** → **inject if usable** → update `accounts.jsonl` status.

Device flow notes (current code):

- Approve payload: `user_code` + `action=allow` only (empty `principal_id` → `Invalid action`)
- Rate-limit detector ignores large accounts.x.ai HTML (i18n `"try again later"` false positives)
- Old SSO often returns `token failed HTTP 400: Access denied` (session dead) — not worth bulk reconvert

---

## Project layout

```text
├── DrissionPage_example.py   # Main worker (register + OAuth + probe + push + accounts ledger)
├── farm_tui.py               # Textual multi-worker dashboard
├── run_pool.py               # CLI multi-process launcher
├── run_linux.sh              # Linux/VPS launcher (headless / xvfb)
├── browser_engine.py         # Camoufox / Chromium session + asset-block
├── hybrid/                   # Hybrid register (castle + protocol)
├── sso_util.py               # SSO wrapper ↔ session materialize
├── sso_to_build.py           # Device OAuth (HTTP) fail-fast
├── build_oauth_pkce.py       # Browser PKCE referrer=grok-build + browser-context token exchange
├── push_9router_grok_cli.py  # 9router grok-cli import
├── chat_usable.py            # Chat probe
├── proxy_util.py / proxy_health.py
├── config.example.json
├── requirements.txt
├── turnstilePatch/           # Chromium extension (optional path)
├── sso/                      # cookie outputs (auto, gitignored)
├── accounts/                 # email/pass + status ledger (auto, gitignored)
├── logs/                     # worker logs (auto, gitignored)
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
| PKCE `invalid_grant` / Access denied after code OK | Token exchange prefers **browser context**; update main. xAI may still deny grant for flagged sessions |
| UI: *Failed to generate authentication code* | xAI hard-deny on consent — fail-fast; try device fallback / new account |
| Device approve hang 7min | Fixed fail-fast (~45s); update main |
| Device `Invalid action` on approve | Empty `principal_id` bug — fixed (approve = `user_code` + `action=allow`) |
| **402 spending-limit** | xAI free quota / policy — not a local crash; may recover later → `failed_probe` |
| **403** chat denied | Soft deny / new account; sometimes recovers → `failed_probe` |
| Hybrid always fallback | Check logs for castle/OTP; `curl_cffi` installed? |
| 9router empty after PASS | `grok_cli.base_url` + password; import only on USABLE; check `accounts.status=injected` |
| Forgot password on old accounts | Pre-ledger runs used random passwords (only in logs). New runs use fixed `account.password` |

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

**Unlimited · 5 concurrent · headless**

```bash
.venv/bin/python farm_tui.py -u -c 5 --display headless
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

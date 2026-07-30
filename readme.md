# Grok Register Farm

Automated **xAI / Grok** account registration farm: signup → SSO → **Grok Build / CLI OAuth** → chat usable probe → inject **9router** (`grok-cli`).

Default browser engine: **Camoufox** (anti-detect Firefox). Optional **Chromium** (Playwright) fallback.

```text
identity:
  browser/hybrid → email (IMAP catch-all | khalidmailer API)
  google         → accounts/google_pass.txt (email:password inventory)

  → register (browser | hybrid | google OIDC)
  → SSO cookies (+ sso/*.txt)
  → ACTIVATE grok.com + bot-hygiene settle (post_signup_settle_sec, default 12s)
  → OAuth PKCE referrer=grok-build  (Camoufox; Chromium often device SSO)
  → PROBE cli-chat-proxy grok-4.5
  → PUSH 9router grok-cli
      · USABLE (200)     → isActive=ON
      · 402 / 403 / other → isActive=OFF + testStatus re-probe-ready
      · 400 only          → no inject
  → accounts.jsonl AFTER probe (google) / after create then update (browser|hybrid)
```

> x.ai rejects common disposable mail domains. For **browser/hybrid** use your **own catch-all**. For **google** mode you only need a Google inventory file.

**Research / personal use only.** Respect site ToS and local law.

---

## Features

| Area | Notes |
|------|--------|
| **Engines** | Camoufox (default) · Playwright Chromium fallback |
| **Register modes** | `browser` · `hybrid` · **`google`** (OIDC from inventory file) |
| **Mail** | IMAP catch-all **or** [khalidmailer](https://mailer.khalid.id/) · humanized locals (**browser/hybrid only**) |
| **Google inventory** | `accounts/google_pass.txt` · multi-worker claim · inventory empty → **workers stop cleanly** |
| **Password** | Fixed signup password via `account.password` (browser/hybrid) · Google mode uses inventory password |
| **Account ledger** | `accounts/accounts.jsonl` + `email_pass.txt` · **google: write after probe** (not at signup) |
| **OAuth** | Browser **PKCE** `referrer=grok-build` · device SSO fallback · activate + **12s bot hygiene** |
| **Browser reset** | `browser.reset=hard\|soft` · google defaults **hard** (fresh profile per account) |
| **Inject policy** | `usable`: **200 → ON**; **402/403 → OFF** (`quota_exhausted` / `permission_denied` for 9router re-probe); **400 → skip** |
| **Proxy** | Pool file, `per_account` / `per_worker`, health check, retry → direct |
| **Farm** | Multi-worker pool · unlimited mode · Textual TUI |

---

## Requirements

| | |
|--|--|
| **OS** | macOS · Linux (VPS OK) · Windows |
| **Python** | **3.10–3.13** recommended (3.14 may hit TLS edge cases) |
| **Mail** | Domain with **catch-all** → Gmail + [App Password](https://myaccount.google.com/apppasswords) **or** [khalidmailer](https://mailer.khalid.id/docs) API |
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
  "browser": { "engine": "camoufox", "reset": "hard" },
  "google": { "accounts_file": "accounts/google_pass.txt" },
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

Env overrides for email: `EMAIL_DOMAIN`, `EMAIL_DOMAINS`, `EMAIL_PROVIDER`, `IMAP_*`, `KHALIDMAILER_API_KEY`, `KHALIDMAILER_USE_SUBDOMAIN`.

### Email domain (single **or** multi) — one key only: `email.domain`

```json
// single
"domain": "syzerf.my.id"

// multi round-robin (array)
"domain": ["syzerf.my.id", "khalid.id", "yani43.site", "yani43.com", "sikilat.web.id"]

// multi round-robin (comma string — also OK)
"domain": "syzerf.my.id,khalid.id,yani43.site"
```

- Multi → each new alias advances `accounts/.domain_rr.index` (multi-worker safe); log `rr=2/5`.
- All domains must catch-all to the **same** IMAP inbox.
- Env: `EMAIL_DOMAIN` (same formats).

### Important fields

| Field | Description |
|-------|-------------|
| `register_mode` | `browser` · `hybrid` · `google`. Env: `GROK_REGISTER_MODE`. |
| `email.domain` | Catch-all domain(s) — **one key**: string or array. Multi = round-robin. Env: `EMAIL_DOMAIN`. |
| `google.accounts_file` | Inventory for google mode (`email:password` per line). Env: `GROK_GOOGLE_ACCOUNTS`. |
| `account.password` | Fixed password for **browser/hybrid** signup. Env: `GROK_ACCOUNT_PASSWORD`. |
| `pool.concurrent` / `-c` | Parallel workers (browsers). |
| `pool.count` / `-n` | Total accounts (`0` = unlimited until inventory empty in google mode). |
| `pool.display` | `headless` · `offscreen` · `virtual` · `headed`. |
| `pool.proxy_file` | Path to proxy list. |
| `pool.proxy_mode` | `per_account` (rotate) · `per_worker` (sticky). |
| `pool.proxy_check` | Probe proxies before spawn (default on). |
| `pool.proxy_retries` | Distinct proxies per account before fail (default 3). |
| `pool.proxy_fallback_direct` | After proxy fails → try **direct** (default true). |
| `pool.block_assets` | Abort third-party font/media (`GROK_BLOCK_ASSETS`). |
| `browser.engine` | `camoufox` (default) · `chromium`. |
| `browser.reset` | `hard` (kill + fresh profile per account) · `soft` (reuse process). **Google defaults hard.** Env: `GROK_BROWSER_RESET`. |
| `grok_cli.activate_grok_com` | Visit **grok.com** with SSO before OAuth (default **true**). |
| `grok_cli.activate_timeout_sec` | Max wait for grok.com ready (default 45). |
| `grok_cli.post_signup_settle_sec` | **Bot hygiene idle after activate** (default **12**). Set `0` to skip. |
| `grok_cli.oauth_gap_sec` | Min seconds between OAuth mints (default 8). |
| `grok_cli.chat_probe_off_critical` | Soft-reset browser before HTTP probe (default true). |
| `grok_cli.inject_policy` | `usable` (default) · `jwt_clean` · `all`. See inject table below. |
| `grok_cli.oauth_mode` | `auto` · `pkce` · `device`. Force Chromium PKCE: `GROK_FORCE_CHROMIUM_PKCE=1`. |
| `grok_cli.oauth_referrer` | `grok-build` (default) · `cli-proxy-api`. |

---

## Environment variables (quick ref)

| Env | Default | Meaning |
|-----|---------|---------|
| `GROK_BROWSER_ENGINE` | `camoufox` | `camoufox` \| `chromium` |
| `GROK_DISPLAY` / `GROK_HEADLESS` | platform | Display mode |
| `GROK_REGISTER_MODE` | config / `browser` | `hybrid` \| `browser` \| `google` |
| `EMAIL_DOMAIN` | config `email.domain` | Single domain, comma list, or set array in config only |
| `GROK_GOOGLE_ACCOUNTS` | `accounts/google_pass.txt` | Google inventory path |
| `GROK_GOOGLE_FORCE` | off | Re-claim google emails even if ledger status terminal |
| `GROK_BROWSER_RESET` | soft / hard for google | `hard` = full relaunch per account · `soft` = reuse process |
| `GROK_ACCOUNT_PASSWORD` | config `account.password` | Fixed signup password (browser/hybrid) |
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

### B) mailer.khalid.id (`provider: khalidmailer`)

Receive-only API with **wildcard subdomains** (`user@rand.gumial.web.id`). Docs: https://mailer.khalid.id/docs

1. DNS (Cloudflare: **DNS only** / gray cloud for MX — proxy does not support MX):

```text
@  MX  10  mailer.khalid.id
*  MX  10  mailer.khalid.id
```

2. Claim apex + wildcard (`*.gumial.web.id`) at https://mailer.khalid.id/ — **save API key once**.
3. Config / env (prefer env for the key):

```bash
export KHALIDMAILER_API_KEY='tm_...'
```

```json
"email": {
  "provider": "khalidmailer",
  "domain": "gumial.web.id",
  "local_style": "human",
  "khalidmailer_base_url": "https://mailer.khalid.id",
  "khalidmailer_use_subdomain": true,
  "khalidmailer_api_key": ""
}
```

| Field | Meaning |
|-------|---------|
| `provider` | `imap` \| `khalidmailer` |
| `khalidmailer_use_subdomain` | `true` → `local@<random>.domain.com` (needs wildcard MX) |
| `KHALIDMAILER_API_KEY` | Preferred over putting key in config |
| Module | `khalidmailer.py` (replaces old `exzork_mail.py`) |

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
| **`google`** | Claim `email:password` from inventory → Continue with Google → OIDC consent → SSO → same OAuth/probe | `"register_mode": "google"` + `google.accounts_file` |

Hybrid needs **`curl_cffi`** (in requirements). On failure, farm **falls back** to full browser UI automatically.

Turnstile on Camoufox hybrid: native poll + inject widget (not Drission shadow click). Legacy path: `GROK_HYBRID_USE_DRISSION_TS=1`.

### Google provider (`register_mode=google`)

Pre-seeded **Google** accounts (not catch-all mail). Inventory:

```text
# accounts/google_pass.txt
user1@gmail.com:secret1
user2@clumo.my.id:secret2
```

```json
{
  "register_mode": "google",
  "browser": { "engine": "camoufox", "reset": "hard" },
  "google": { "accounts_file": "accounts/google_pass.txt" }
}
```

```bash
# farm (headed recommended for Google UI)
.venv/bin/python farm_tui.py -n 0 -c 2 --headed   # n=0 unlimited until inventory empty
# inventory stats
.venv/bin/python google_signup.py --stats
```

**Flow**

1. Claim next free line from inventory (multi-worker lock)
2. `accounts.x.ai/sign-up` → **Continue with Google**
3. Email → password → multi-step consent (Workspace **I understand** / **Lanjutkan** / Allow)
4. Wait OIDC **`exchange-token`** finish (do not interrupt redirect)
5. SSO → activate grok.com → bot hygiene settle → OAuth → probe → 9router

**Claim / ledger**

| File | Role |
|------|------|
| `accounts/google_pass.txt` | Inventory (`email:password`) |
| `accounts/.google_claim_live.jsonl` | In-flight claims only (not final ledger) |
| `accounts/accounts.jsonl` | Written **after probe/push** with final status |
| `accounts/email_pass.txt` | Appended with password when ledger row is written |

- Skips emails already in jsonl with terminal status (`injected`, `failed_*`, …) unless `GROK_GOOGLE_FORCE=1`.
- Login fail **before** probe → release live claim (retryable); **no** jsonl row yet.
- **Inventory empty** → worker exits cleanly (`DONE google inventory exhausted`) — **not** counted as ✗ fail. All workers empty ⇒ farm finished.

**Consent / challenges**

- Multi-step: scroll → `#gaplustosNext` / “I understand” → Allow / Lanjutkan.
- 2FA, captcha, “browser not secure” → that account fails (no email/OTP fallback).

---

## Pipeline (per account)

1. **Register** — `browser` / `hybrid` / `google` → SSO (+ `sso/*.txt`)  
2. **Ledger (browser/hybrid)** — `accounts.jsonl` `status=created` early; status updated after OAuth/probe  
   **Ledger (google)** — **no** jsonl until after probe  
3. **ACTIVATE** — open **grok.com** with SSO  
4. **SETTLE** — bot hygiene idle `post_signup_settle_sec` (default **12s**)  
5. **CONVERT** — PKCE `oauth_referrer` (default `grok-build`); device fallback  
6. **PROBE** — `cli-chat-proxy` `grok-4.5`  
7. **PUSH** — 9router (see inject table) · google jsonl written here  

### Inject policy (`inject_policy=usable`)

| Probe HTTP | 9router | `testStatus` (for re-probe) | `accounts.status` |
|------------|---------|-------------------------------|-------------------|
| **200** USABLE | inject **ON** (`isActive=true`) | `active` | `injected` |
| **402** | inject **OFF** | **`quota_exhausted`** + PSD `quotaExhausted` | `injected` (off) |
| **403** | inject **OFF** | **`permission_denied`** + PSD `permissionDenied` | `injected` (off) |
| other soft fail | inject **OFF** | **`unavailable`** | `injected` (off) |
| **400** only | **no inject** | — | `failed_probe` |
| OAuth / Access denied / no token | **no inject** | — | `failed_oauth` |

402/403 rows stay in 9router for **re-probe** (must not use plain `inactive`, which looks like manual off).

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

Proxy failures during convert may retry **direct** (`proxy_fallback_direct`).

> **402/403 are not always permanent.** Free-tier quota and soft denials can clear after hours. Prefer **9router re-probe** of OFF accounts over mass-reconvert.

---

## Outputs

```text
sso/                          # OUTPUT ONLY — farm never reads these back
  sso_<timestamp>_w<N>.txt    # cookie-header lines

accounts/                     # gitignored (secrets)
  google_pass.txt             # google inventory (input)
  .google_claim_live.jsonl    # in-flight claims only (google mode)
  accounts.jsonl              # ledger: email, password, status, probe, …
  email_pass.txt              # email:password dump

logs/
  run_<timestamp>_w<N>.log
```

### `sso/` vs `accounts/`

| Path | Written when | Read by farm? | Contents |
|------|----------------|---------------|----------|
| `sso/*.txt` | After SSO cookies | **No** (output / offline tools) | Cookie header |
| `accounts/google_pass.txt` | You edit | **Yes** (google mode inventory) | `email:password` |
| `accounts/.google_claim_live.jsonl` | On claim → cleared after ledger write | Yes (multi-worker) | In-flight only |
| `accounts/accounts.jsonl` | **browser/hybrid:** after create, then update · **google:** **after probe** | Skip terminal emails for google claim | Full ledger |
| `accounts/email_pass.txt` | With ledger write | No | `email:password` |

### Account status values (`accounts.jsonl`)

| `status` | Meaning |
|----------|---------|
| `created` | browser/hybrid: register + SSO OK |
| `oauth_ok` | Build OAuth minted (intermediate, browser/hybrid) |
| `injected` | Pushed to 9router (ON if usable; **OFF** still `injected` for 402/403 with `inject_active=false`) |
| `failed_oauth` | SSO OK but PKCE/device OAuth failed |
| `failed_probe` | OAuth OK but probe **400** (or hard probe fail without inject) |
| `failed_push` | Usable but 9router import failed |
| `failed_login` | google: signup/SSO failed before OAuth (only if ledger written; live claim released) |

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

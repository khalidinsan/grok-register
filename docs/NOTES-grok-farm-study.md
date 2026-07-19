# Notes — study of `fazulfi/grok-farm` (local clone)

**Date:** 2026-07-19  
**Local path:** `/Users/khalid/Documents/Code/Personal/grok-farm`  
**Upstream:** https://github.com/fazulfi/grok-farm (v2.3.2)

Operator decision: **do not port yet** — keep as reference only.

## What it is

Production **farm → inject** pipeline for Grok CLI OAuth into 9router (not session lifecycle).

- Browser: **Camoufox** (Playwright)
- Token: **OAuth PKCE** direct (not Web SSO → Build convert)
- Inventory: SQLite `akun.db` (`farmed` → `injected`)
- Inject: SSH JSONL bulk into 9router + proxy per account
- Ops: systemd unlimited loop, multi-VPS, multi-domain IMAP, adaptive concurrent, alerts, backup

## Vs our `grok-register`

| | grok-farm | grok-register |
|--|-----------|---------------|
| Engine | Camoufox | DrissionPage + Playwright Chromium |
| Token path | OAuth PKCE in browser | SSO cookie → sso_to_build |
| 9router | SSH inject + proxyPools sync | HTTP / in-process import + Add Account UI |
| Scale | VPS fleet + systemd | Local pool / TUI / dashboard button |

## Worth stealing later (priority)

1. `TURNSTILE_PARALLEL=1` + proxy-per-worker (biggest CF win)
2. Turnstile remount on "Verification failed" + cookie dismiss everywhere
3. Fail-fast per-step timeouts
4. PKCE path instead of/beside SSO→Build
5. SQLite farmed/injected inventory
6. Camoufox engine swap (large effort)
7. systemd 24/7 loop if VPS farming is desired

## Key files in that repo

- `farm.py` — core farmer (~3500 lines)
- `workflow.py` — import + inject
- `brutal_farmer.sh` + `systemd/` — production loop
- `docs/ARCHITECTURE.md`, `INTEGRATION-9ROUTER.md`
- `email_identity.py`, `name_gen.py` — multi-domain identity plane

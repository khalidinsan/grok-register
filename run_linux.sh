#!/usr/bin/env bash
# Flash-aligned Linux / VPS launcher for grok-register.
#
# Default: Camoufox + headless (same as flash-grok-farm GROK_HEADLESS=true).
# Fallbacks:
#   GROK_DISPLAY=virtual  → Camoufox headed on Xvfb (needs xvfb)
#   GROK_HEADLESS=false   → headed (needs real DISPLAY)
#   no DISPLAY + not headless → auto xvfb-run
#
# Usage:
#   ./run_linux.sh farm_tui.py -u -c 2
#   ./run_linux.sh run_pool.py -n 10 -c 2
#   GROK_DISPLAY=virtual ./run_linux.sh farm_tui.py -u -c 1
#   ./run_linux.sh --headed farm_tui.py -n 1 -c 1
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "ERROR: jangan jalankan sebagai root — Camoufox XPCOM sering rusak di root."
  echo "  su - USER -c 'cd $ROOT && ./run_linux.sh …'"
  exit 1
fi

if [[ -d .venv ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Defaults flash-style unless already set
export GROK_BROWSER_ENGINE="${GROK_BROWSER_ENGINE:-camoufox}"
export PYTHONUNBUFFERED=1
export PYTHONUTF8=1

# Parse leading display shortcuts before the python script name
EXTRA=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --headless) export GROK_DISPLAY=headless; export GROK_HEADLESS=true; shift ;;
    --headed)   export GROK_DISPLAY=headed;   export GROK_HEADLESS=false; shift ;;
    --offscreen) export GROK_DISPLAY=offscreen; export GROK_HEADLESS=false; shift ;;
    --virtual)  export GROK_DISPLAY=virtual;  export GROK_HEADLESS=virtual; shift ;;
    *) break ;;
  esac
done

# Platform default if still unset
if [[ -z "${GROK_DISPLAY:-}" ]]; then
  if [[ -n "${GROK_HEADLESS:-}" ]]; then
    case "$(echo "$GROK_HEADLESS" | tr '[:upper:]' '[:lower:]')" in
      true|1|yes|on) export GROK_DISPLAY=headless ;;
      virtual|xvfb)  export GROK_DISPLAY=virtual ;;
      false|0|no|off|headed) export GROK_DISPLAY=headed ;;
      *) export GROK_DISPLAY=headless ;;
    esac
  else
    export GROK_DISPLAY=headless
    export GROK_HEADLESS=true
  fi
fi

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 [--headless|--virtual|--headed|--offscreen] <script.py> [args…]"
  echo "  e.g. $0 farm_tui.py -u -c 2"
  echo "       $0 --virtual run_pool.py -n 5 -c 2"
  exit 2
fi

SCRIPT="$1"
shift

# Auto Xvfb when headed/virtual needs a display and none is available
need_x=0
case "${GROK_DISPLAY}" in
  headed|offscreen|virtual) need_x=1 ;;
esac

if [[ "$need_x" -eq 1 && -z "${DISPLAY:-}" ]]; then
  if command -v xvfb-run >/dev/null 2>&1; then
    echo "[run_linux] No DISPLAY — wrapping with xvfb-run (display=${GROK_DISPLAY})"
    exec xvfb-run -a python "$SCRIPT" "$@"
  fi
  echo "[run_linux] WARN: no DISPLAY and no xvfb-run — set GROK_DISPLAY=headless or install xvfb"
fi

echo "[run_linux] engine=${GROK_BROWSER_ENGINE} display=${GROK_DISPLAY} headless=${GROK_HEADLESS:-?} → python $SCRIPT $*"
exec python "$SCRIPT" "$@"

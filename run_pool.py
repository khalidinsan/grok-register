#!/usr/bin/env python3
"""
Concurrent Grok farm pool.

Clear semantics:
  --count N       total accounts to create (0 = unlimited)
  --unlimited     same as --count 0 (run until Ctrl+C / stop)
  --concurrent K  how many browsers in parallel

Example:
  python run_pool.py --count 100 --concurrent 3
  → 100 accounts split across 3 workers (34 + 33 + 33)

Also:
  python run_pool.py -n 10 -c 2 --offscreen
  python run_pool.py --unlimited -c 2 --offscreen
  python run_pool.py --dry-run -n 100 -c 3
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "DrissionPage_example.py"
CONFIG_PATH = ROOT / "config.json"


def load_pool_config() -> dict:
    from proxy_util import load_proxy_file as _load_pf, load_proxy_list, normalize_proxy

    defaults = {
        "count": 1,          # TOTAL accounts
        "concurrent": 2,     # parallel browsers
        "stagger_sec": 20,
        "proxies": [],
        "proxy_file": "",
        # per_account = rotate 1 proxy per account (default)
        # per_worker  = sticky proxy for whole worker process
        "proxy_mode": "per_account",
        # health check before spawn (latency to accounts.x.ai)
        "proxy_check": True,
        "proxy_check_url": "https://accounts.x.ai/",
        "proxy_max_ms": 4000,
        "proxy_check_timeout": 12,
        "proxy_check_workers": 10,
        # flash-aligned: Mac offscreen · Linux/Win headless
        "display": (
            "offscreen"
            if sys.platform == "darwin"
            else "headless"
        ),
    }
    if not CONFIG_PATH.is_file():
        return defaults
    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as f:
            conf = json.load(f)
        pool = conf.get("pool") or {}
        if not isinstance(pool, dict):
            return defaults
        out = dict(defaults)

        # New keys
        if isinstance(pool.get("count"), int) and pool["count"] >= 0:
            out["count"] = pool["count"]
        if isinstance(pool.get("concurrent"), int) and pool["concurrent"] >= 1:
            out["concurrent"] = pool["concurrent"]
        # Backward compat: pool.workers → concurrent
        elif isinstance(pool.get("workers"), int) and pool["workers"] >= 1:
            out["concurrent"] = pool["workers"]

        if isinstance(pool.get("stagger_sec"), (int, float)) and pool["stagger_sec"] >= 0:
            out["stagger_sec"] = float(pool["stagger_sec"])

        mode = str(pool.get("proxy_mode") or out["proxy_mode"]).strip().lower()
        if mode in ("per_account", "rotate", "account"):
            out["proxy_mode"] = "per_account"
        elif mode in ("per_worker", "worker", "sticky"):
            out["proxy_mode"] = "per_worker"

        proxies: list[str] = []
        pf = str(pool.get("proxy_file") or conf.get("proxy_file") or "").strip()
        if pf:
            out["proxy_file"] = pf
            try:
                proxies = _load_pf(pf)
            except FileNotFoundError as e:
                print(f"[Warn] {e}")
        if not proxies:
            proxies = load_proxy_list(pool.get("proxies") or pool.get("proxy_list") or [])
        if not proxies:
            single = str(conf.get("browser_proxy") or conf.get("proxy") or "").strip()
            if single:
                n = normalize_proxy(single)
                if n:
                    proxies = [n]
        out["proxies"] = proxies

        # proxy health check knobs
        if "proxy_check" in pool:
            out["proxy_check"] = bool(pool.get("proxy_check"))
        for key, cast in (
            ("proxy_check_url", str),
            ("proxy_max_ms", float),
            ("proxy_check_timeout", float),
            ("proxy_check_workers", int),
        ):
            if pool.get(key) is not None:
                try:
                    out[key] = cast(pool[key])
                except (TypeError, ValueError):
                    pass

        display = (
            pool.get("display")
            or (conf.get("run") or {}).get("display")
            or conf.get("display")
        )
        if isinstance(display, str) and display.strip():
            try:
                from browser_engine import normalize_display, DISPLAY_MODES

                d = normalize_display(display) or display.strip().lower()
            except Exception:
                d = display.strip().lower()
                if d in ("bg", "background", "minimized", "minimise", "minimize"):
                    d = "offscreen"
                if d in ("xvfb", "vd"):
                    d = "virtual"
            if d in ("headed", "offscreen", "headless", "virtual"):
                out["display"] = d
        return out
    except Exception as e:
        print(f"[Warn] config pool: {e}")
        return defaults


def load_proxy_file(path: str) -> list[str]:
    from proxy_util import load_proxy_file as _load

    try:
        return _load(path)
    except FileNotFoundError as e:
        raise SystemExit(str(e)) from e


def split_workload(total: int, concurrent: int) -> list[int]:
    """
    Split `total` accounts across `concurrent` workers as evenly as possible.

    Examples:
      100, 3 → [34, 33, 33]
      10, 3  → [4, 3, 3]
      2, 5   → [1, 1]   (only 2 workers needed)
      0, 3   → [0, 0, 0]  (0 = infinite per worker)
    """
    if concurrent < 1:
        raise ValueError("concurrent must be >= 1")
    if total == 0:
        # Infinite mode: every worker runs forever
        return [0] * concurrent
    if total < 0:
        raise ValueError("count must be >= 0")
    # No empty workers
    n = min(concurrent, total)
    base, extra = divmod(total, n)
    return [base + (1 if i < extra else 0) for i in range(n)]


def platform_is_mac() -> bool:
    return sys.platform == "darwin"


def _mask_proxy(url: str) -> str:
    from proxy_util import mask_proxy

    return mask_proxy(url)


def spawn_worker_process(
    cmd: list[str],
    *,
    cwd: str,
    env: dict,
    capture_output: bool = False,
) -> subprocess.Popen:
    """
    Spawn a worker in its own process group so Ctrl+C / quit can kill
    Python + Chromium children together (not leave orphan Chrome windows).
    """
    kwargs: dict = {
        "cwd": cwd,
        "env": env,
    }
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.STDOUT
        kwargs["text"] = True
        kwargs["bufsize"] = 1
        # Windows default pipe encoding is often cp1252; farm logs use Unicode (→ ✓ …)
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"

    # Always force UTF-8 I/O in workers (TUI pipes stdout; Windows charmap breaks prints)
    env = dict(env)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    kwargs["env"] = env

    if sys.platform == "win32":
        # Kill tree via taskkill /T later
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        # New session = process group leader (pgid == pid)
        kwargs["start_new_session"] = True

    return subprocess.Popen(cmd, **kwargs)


def _kill_pids(pids: list[int], sig: int = signal.SIGTERM) -> None:
    for pid in pids:
        if pid <= 0:
            continue
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _pids_listening_on_port(port: int) -> list[int]:
    """PIDs bound to TCP listen port (Chrome CDP)."""
    if port <= 0:
        return []
    pids: list[int] = []
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        for line in out.split():
            try:
                pids.append(int(line.strip()))
            except ValueError:
                pass
    except Exception:
        pass
    return pids


def kill_orphan_farm_chrome(debug_ports: list[int] | None = None) -> None:
    """
    Best-effort sweep of leftover farm Chromium only.
    Never targets daily Google Chrome.app — only:
      - CDP ports used by workers
      - temp profiles grok_pw_w*
      - Playwright Chrome for Testing with those profiles
    """
    ports = list(debug_ports or [])
    for port in ports:
        for pid in _pids_listening_on_port(port):
            _kill_pids([pid], signal.SIGKILL)

    # Profile marker from DrissionPage_example.start_browser()
    patterns = [
        r"user-data-dir=.*/grok_pw_w",
        r"--user-data-dir=.*grok_pw_w",
    ]
    for pat in patterns:
        try:
            subprocess.run(
                ["pkill", "-f", pat],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=4,
            )
        except Exception:
            pass


def terminate_worker_tree(
    proc: subprocess.Popen | None,
    *,
    debug_port: int = 0,
    grace_sec: float = 2.5,
) -> None:
    """
    SIGTERM process group (Python worker + Chromium children), then SIGKILL,
    then port/profile sweep for orphans.
    """
    if proc is None:
        if debug_port:
            kill_orphan_farm_chrome([debug_port])
        return

    pid = proc.pid
    alive = proc.poll() is None

    if alive:
        if sys.platform == "win32":
            try:
                # /T = kill child tree
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=8,
                )
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        else:
            # Process group (start_new_session=True → pgid == pid)
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.terminate()
                except Exception:
                    pass

            deadline = time.time() + grace_sec
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.1)

            if proc.poll() is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                # brief wait after KILL
                try:
                    proc.wait(timeout=1.5)
                except Exception:
                    pass

    # Chrome sometimes detaches; clean by CDP port + profile
    ports = [debug_port] if debug_port else []
    kill_orphan_farm_chrome(ports)


def main() -> int:
    cfg = load_pool_config()

    parser = argparse.ArgumentParser(
        description=(
            "Grok farm pool — total accounts + concurrency.\n"
            "Example: python run_pool.py --count 100 --concurrent 3"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=cfg["count"],
        help=f"TOTAL accounts to create (default {cfg['count']}; 0 = unlimited until stop)",
    )
    parser.add_argument(
        "-u",
        "--unlimited",
        action="store_true",
        help="farm forever until Ctrl+C / stop (same as -n 0)",
    )
    parser.add_argument(
        "-c",
        "--concurrent",
        type=int,
        default=cfg["concurrent"],
        dest="concurrent",
        help=f"how many browsers in parallel (default {cfg['concurrent']})",
    )
    # Alias kept so old scripts don't break
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help=argparse.SUPPRESS,  # hidden alias for --concurrent
    )
    parser.add_argument(
        "--stagger",
        type=float,
        default=cfg["stagger_sec"],
        dest="stagger_sec",
        help=f"seconds between starting each worker (default {cfg['stagger_sec']})",
    )
    parser.add_argument(
        "--proxy-file",
        default=cfg.get("proxy_file") or "",
        help="proxy list file (URL or Webshare host:port:user:pass)",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        help="proxy URL (repeatable)",
    )
    parser.add_argument(
        "--proxy-mode",
        choices=["per_account", "per_worker"],
        default=cfg.get("proxy_mode") or "per_account",
        help="per_account=1 proxy per account (rotate); per_worker=sticky per worker",
    )
    parser.add_argument(
        "--proxy-check",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg.get("proxy_check", True)),
        help="probe proxies → accounts.x.ai and drop slow ones (default: on)",
    )
    parser.add_argument(
        "--proxy-max-ms",
        type=float,
        default=float(cfg.get("proxy_max_ms") or 4000),
        help="max acceptable latency ms to accounts.x.ai (default 4000)",
    )
    parser.add_argument(
        "--proxy-check-url",
        default=str(cfg.get("proxy_check_url") or "https://accounts.x.ai/"),
        help="URL for proxy health probe",
    )
    parser.add_argument(
        "--display",
        choices=["headed", "offscreen", "headless", "virtual"],
        default=None,
        help=(
            "headed | offscreen (Mac) | headless (Linux/VPS flash default) | "
            "virtual (Camoufox Xvfb). Default: config → env → platform "
            "(Mac=offscreen, Linux/Win=headless)"
        ),
    )
    parser.add_argument("--headless", action="store_true", help="shortcut → headless")
    parser.add_argument("--offscreen", action="store_true", help="shortcut → offscreen")
    parser.add_argument(
        "--virtual",
        action="store_true",
        help="shortcut → virtual (Camoufox headed on Xvfb; Linux)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="shortcut → headed (visible window; debug Turnstile)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print split plan only, do not start browsers",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="launch live Terminal UI dashboard (farm_tui.py) instead of raw logs",
    )
    args = parser.parse_args()

    if getattr(args, "unlimited", False):
        args.count = 0

    if args.tui:
        # Hand off to farm_tui with the same CLI flags (minus --tui)
        from farm_tui import run_tui

        # rebuild namespace without dry-run side effects
        return run_tui(args)

    concurrent = args.workers if args.workers is not None else args.concurrent
    total = args.count

    if concurrent < 1:
        raise SystemExit("--concurrent must be >= 1")
    if total < 0:
        raise SystemExit("--count must be >= 0")

    shares = split_workload(total, concurrent)
    n_workers = len(shares)

    if concurrent > 5 and platform_is_mac():
        print(
            f"[Warn] concurrent={concurrent} on macOS is aggressive; "
            "try 2–3 first."
        )

    # Priority: CLI shortcuts > --display > config.pool.display > env > platform
    # (same order as flash GROK_HEADLESS / --headed)
    if getattr(args, "headed", False):
        display = "headed"
    elif getattr(args, "virtual", False):
        display = "virtual"
    elif args.headless:
        display = "headless"
    elif args.offscreen:
        display = "offscreen"
    elif args.display:
        display = args.display
    elif cfg.get("display"):
        display = str(cfg["display"])
    else:
        display = ""
    try:
        from browser_engine import resolve_display, normalize_display

        forced = (
            getattr(args, "headed", False)
            or getattr(args, "virtual", False)
            or args.headless
            or args.offscreen
            or bool(args.display)
        )
        if forced:
            display = normalize_display(display) or display
        else:
            # empty explicit → GROK_DISPLAY / GROK_HEADLESS / platform
            if display:
                os.environ.setdefault("GROK_DISPLAY", normalize_display(display) or display)
            display = resolve_display(display)
    except Exception:
        if not display or display in ("bg", "background"):
            display = "offscreen" if platform_is_mac() else "headless"
    os.environ["GROK_DISPLAY"] = display
    # flash-compatible mirror
    if display == "headless":
        os.environ["GROK_HEADLESS"] = "true"
    elif display == "virtual":
        os.environ["GROK_HEADLESS"] = "virtual"
    else:
        os.environ["GROK_HEADLESS"] = "false"

    from proxy_util import encode_proxy_env, load_proxy_list, normalize_proxy

    if args.proxy:
        proxies = load_proxy_list(args.proxy)
    else:
        proxies = list(cfg["proxies"])
    if args.proxy_file:
        proxies = load_proxy_file(args.proxy_file)
    # normalize any leftover raw lines
    proxies = [normalize_proxy(p) or p for p in proxies if p]
    proxy_mode = getattr(args, "proxy_mode", None) or cfg.get("proxy_mode") or "per_account"

    # ── health check: collect only as many good proxies as accounts need ──
    # e.g. 10 accounts + 100 proxies → stop after 10 KEEP (don't probe all 100)
    do_check = bool(getattr(args, "proxy_check", cfg.get("proxy_check", True)))
    if proxies and do_check:
        from proxy_health import apply_proxy_check, resolve_proxy_need

        need = resolve_proxy_need(
            total_accounts=total,
            concurrent=n_workers,
            proxy_count=len(proxies),
            proxy_mode=proxy_mode,
        )
        print(
            f"[PROXY-CHECK] plan: need {need} good proxy(ies) "
            f"for total={total if total > 0 else '∞'} concurrent={n_workers} "
            f"from {len(proxies)} listed",
            flush=True,
        )
        try:
            proxies = apply_proxy_check(
                proxies,
                enabled=True,
                target=str(
                    getattr(args, "proxy_check_url", None)
                    or cfg.get("proxy_check_url")
                    or "https://accounts.x.ai/"
                ),
                max_ms=float(
                    getattr(args, "proxy_max_ms", None)
                    or cfg.get("proxy_max_ms")
                    or 4000
                ),
                timeout=float(cfg.get("proxy_check_timeout") or 12),
                workers=int(cfg.get("proxy_check_workers") or 10),
                need=need,
                total_accounts=total,
                concurrent=n_workers,
                proxy_mode=proxy_mode,
                require_one=True,
            )
        except RuntimeError as e:
            raise SystemExit(f"[proxy-check] {e}") from e
        try:
            good_path = ROOT / "proxy.good.txt"
            good_path.write_text("\n".join(proxies) + "\n", encoding="utf-8")
            print(f"[PROXY-CHECK] wrote {good_path.name} ({len(proxies)} lines)")
        except Exception:
            pass
    elif proxies and not do_check:
        print("[PROXY-CHECK] skipped (--no-proxy-check)")

    if not SCRIPT.is_file():
        raise SystemExit(f"missing farm script: {SCRIPT}")

    python = sys.executable
    print("=" * 60)
    print("Grok farm pool")
    print(f"  python     : {python}")
    print(f"  total      : {total if total > 0 else '∞ (infinite)'}")
    print(f"  concurrent : {n_workers}")
    print(f"  split      : {shares}  (sum={sum(shares) if total > 0 else '∞'})")
    print(f"  stagger    : {args.stagger_sec}s")
    print(f"  display    : {display}")
    print(
        f"  proxies    : {len(proxies)}  mode={proxy_mode}"
        + (f"  check={'on' if do_check else 'off'}" if proxies else "")
    )
    print("-" * 60)
    print("  log tag   : [W2 3/33 · #70/100 · remW 30 · ✓2 ✗0]")
    print("              W=worker local/share  #=global index")
    print("              remW=sisa di worker ini  ✓/✗ = ok/fail worker")
    print("  phases    : START → FLOW ①..⑤ → CONVERT → SMOKE → PUSH → OK/FAIL")
    if display == "headed" and platform_is_mac():
        print("  [!] headed on Mac steals focus — prefer --offscreen while working")
    if display == "headless":
        print(
            "  [display] headless (flash Linux/VPS mode) — Camoufox preferred; "
            "if Turnstile stuck: --virtual (Xvfb) or --headed / --offscreen"
        )
    if display == "virtual":
        print(
            "  [display] virtual = Camoufox headed on Xvfb "
            "(needs xvfb + pyvirtualdisplay, or: xvfb-run -a …)"
        )
    if not proxies and n_workers > 1:
        print(
            "  [!] no proxies — all workers share your home IP. "
            "OK for a tiny test; add pool.proxies / --proxy-file for safer concurrent."
        )
    if proxies and proxy_mode == "per_account":
        print(
            f"  [proxy] per_account rotate — global account #k uses "
            f"proxy[(k-1) % {len(proxies)}]; browser full-restart on change"
        )
    print("=" * 60)

    # (Popen, debug_port) so shutdown can kill Chromium by CDP port too
    procs: list[tuple[subprocess.Popen, int]] = []

    def _shutdown(signum=None, frame=None):
        print("\n[pool] stopping workers + Chromium…")
        ports = []
        for p, port in procs:
            ports.append(port)
            terminate_worker_tree(p, debug_port=port, grace_sec=2.0)
        kill_orphan_farm_chrome(ports)
        print("[pool] all workers stopped")
        sys.exit(130 if signum else 0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Cumulative offsets so each worker can log global account index
    offsets: list[int] = []
    running_offset = 0
    for share in shares:
        offsets.append(running_offset)
        running_offset += share if total > 0 else 0

    for i, share in enumerate(shares):
        wid = str(i + 1)
        sticky = proxies[i % len(proxies)] if proxies and proxy_mode == "per_worker" else ""
        debug_port = 9300 + int(wid) * 20
        env = os.environ.copy()
        env["GROK_WORKER_ID"] = wid
        env["GROK_DEBUG_PORT"] = str(debug_port)
        env["GROK_DISPLAY"] = display
        env["GROK_WORKER_SHARE"] = str(share)
        env["GROK_POOL_TOTAL"] = str(total if total > 0 else 0)
        env["GROK_POOL_OFFSET"] = str(offsets[i])
        env["GROK_POOL_CONCURRENT"] = str(n_workers)
        env["GROK_PROXY_MODE"] = proxy_mode
        if proxies:
            env["GROK_PROXIES"] = encode_proxy_env(proxies)
        else:
            env.pop("GROK_PROXIES", None)
        if sticky:
            env["GROK_BROWSER_PROXY"] = sticky
        else:
            # per_account: worker picks proxy per account from GROK_PROXIES
            env.pop("GROK_BROWSER_PROXY", None)
            env.pop("BROWSER_PROXY", None)

        # Each worker gets its slice of the total (--count for child = accounts THIS worker runs)
        cmd = [
            python,
            str(SCRIPT),
            "--count",
            str(share),
            "--worker-id",
            wid,
            "--display",
            display,
        ]
        g_from = offsets[i] + 1 if total > 0 else 0
        g_to = offsets[i] + share if total > 0 else 0
        if proxy_mode == "per_worker" and sticky:
            proxy_note = f"  proxy={_mask_proxy(sticky)} (sticky)"
        elif proxies:
            proxy_note = f"  proxies={len(proxies)} (rotate/account)"
        else:
            proxy_note = "  proxy=(none)"
        print(
            f"[pool] worker {wid}/{n_workers}: {share} account(s)"
            + (f"  global#{g_from}–{g_to}" if total > 0 else "")
            + f"  cdp≈{debug_port}"
            + proxy_note
        )
        if args.dry_run:
            print(f"       cmd: {' '.join(cmd)}")
        else:
            env.setdefault("PYTHONUNBUFFERED", "1")
            p = spawn_worker_process(cmd, cwd=str(ROOT), env=env, capture_output=False)
            procs.append((p, debug_port))

        if i + 1 < n_workers and args.stagger_sec > 0:
            print(f"[pool] stagger sleep {args.stagger_sec}s ...")
            if not args.dry_run:
                time.sleep(args.stagger_sec)

    if args.dry_run:
        print("[pool] dry-run done")
        return 0

    print(f"[pool] {len(procs)} workers running — Ctrl+C to stop all (closes Chromium too)")
    t0 = time.time()
    codes = [p.wait() for p, _ in procs]
    elapsed = max(0.1, time.time() - t0)
    # Normal finish: still sweep any orphan chrome
    kill_orphan_farm_chrome([port for _, port in procs])
    ok = sum(1 for c in codes if c == 0)
    # Rough throughput: total accounts / wall minutes (worker exit codes only)
    if total > 0:
        per_min = total / (elapsed / 60.0)
        print(
            f"[pool] done: {ok}/{len(codes)} workers exit 0  codes={codes}  "
            f"wall={elapsed/60:.1f}m  ~{per_min:.1f} acc/min (planned {total})"
        )
    else:
        print(f"[pool] done: {ok}/{len(codes)} workers exit 0  codes={codes}")
    return 0 if all(c == 0 for c in codes) else 1


if __name__ == "__main__":
    raise SystemExit(main())

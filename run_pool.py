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
    defaults = {
        "count": 1,          # TOTAL accounts
        "concurrent": 2,     # parallel browsers
        "stagger_sec": 20,
        "proxies": [],
        "display": "offscreen" if sys.platform == "darwin" else "headed",
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

        proxies = pool.get("proxies") or pool.get("proxy_list") or []
        if isinstance(proxies, list):
            out["proxies"] = [str(p).strip() for p in proxies if str(p).strip()]
        if not out["proxies"]:
            single = str(conf.get("browser_proxy") or conf.get("proxy") or "").strip()
            if single:
                out["proxies"] = [single]

        display = (
            pool.get("display")
            or (conf.get("run") or {}).get("display")
            or conf.get("display")
        )
        if isinstance(display, str) and display.strip():
            d = display.strip().lower()
            if d in ("bg", "background", "minimized", "minimise", "minimize"):
                d = "offscreen"
            if d in ("headed", "offscreen", "headless"):
                out["display"] = d
        return out
    except Exception as e:
        print(f"[Warn] config pool: {e}")
        return defaults


def load_proxy_file(path: str) -> list[str]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"proxy file not found: {p}")
    lines = []
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        lines.append(s)
    return lines


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
    if not url or "@" not in url:
        return url
    try:
        left, right = url.rsplit("@", 1)
        if "://" in left:
            scheme, creds = left.split("://", 1)
            if ":" in creds:
                user = creds.split(":", 1)[0]
                return f"{scheme}://{user}:***@{right}"
        return f"***@{right}"
    except Exception:
        return "***"


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
        default="",
        help="text file: one proxy URL per line",
    )
    parser.add_argument(
        "--proxy",
        action="append",
        default=[],
        help="proxy URL (repeatable); round-robin to workers",
    )
    parser.add_argument(
        "--display",
        choices=["headed", "offscreen", "headless"],
        default=cfg.get("display") or ("offscreen" if platform_is_mac() else "headed"),
        help="headed | offscreen | headless",
    )
    parser.add_argument("--headless", action="store_true", help="shortcut → headless")
    parser.add_argument("--offscreen", action="store_true", help="shortcut → offscreen")
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

    display = "headless" if args.headless else ("offscreen" if args.offscreen else args.display)
    if display in ("bg", "background"):
        display = "offscreen"

    proxies = list(args.proxy) if args.proxy else list(cfg["proxies"])
    if args.proxy_file:
        proxies = load_proxy_file(args.proxy_file)

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
    print(f"  proxies    : {len(proxies)} configured")
    print("-" * 60)
    print("  log tag   : [W2 3/33 · #70/100 · remW 30 · ✓2 ✗0]")
    print("              W=worker local/share  #=global index")
    print("              remW=sisa di worker ini  ✓/✗ = ok/fail worker")
    print("  phases    : START → FLOW ①..⑥ → OK/FAIL → SCORE")
    if display == "headed" and platform_is_mac():
        print("  [!] headed on Mac steals focus — prefer --offscreen while working")
    if display == "headless":
        print("  [!] headless: Turnstile may fail more; try --offscreen if stuck")
    if not proxies and n_workers > 1:
        print(
            "  [!] no proxies — all workers share your home IP. "
            "OK for a tiny test; add pool.proxies or --proxy for safer concurrent."
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
        proxy = proxies[i % len(proxies)] if proxies else ""
        debug_port = 9300 + int(wid) * 20
        env = os.environ.copy()
        env["GROK_WORKER_ID"] = wid
        env["GROK_DEBUG_PORT"] = str(debug_port)
        env["GROK_DISPLAY"] = display
        env["GROK_WORKER_SHARE"] = str(share)
        env["GROK_POOL_TOTAL"] = str(total if total > 0 else 0)
        env["GROK_POOL_OFFSET"] = str(offsets[i])
        if proxy:
            env["GROK_BROWSER_PROXY"] = proxy
        else:
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
        print(
            f"[pool] worker {wid}/{n_workers}: {share} account(s)"
            + (f"  global#{g_from}–{g_to}" if total > 0 else "")
            + f"  cdp≈{debug_port}"
            + (f"  proxy={_mask_proxy(proxy)}" if proxy else "  proxy=(none)")
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

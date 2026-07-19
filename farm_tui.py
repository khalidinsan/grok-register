#!/usr/bin/env python3
"""
Grok Register — multi-worker Terminal UI.

Live dashboard for farm pool: global progress, per-worker state, scrollable logs.

Usage:
  .venv/bin/python farm_tui.py -n 20 -c 3 --stagger 5 --offscreen
  .venv/bin/python run_pool.py --tui -n 20 -c 3 --offscreen

Keys:
  q / Ctrl+C  stop all workers & quit
  a           show all workers in log feed
  1-9         filter log feed to worker N
  p           pause / resume auto-scroll of log
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── pool helpers (reuse run_pool) ──────────────────────────────────────────
from run_pool import (
    ROOT,
    SCRIPT,
    _mask_proxy,
    kill_orphan_farm_chrome,
    load_pool_config,
    load_proxy_file,
    platform_is_mac,
    spawn_worker_process,
    split_workload,
    terminate_worker_tree,
)

# ── log line parser ────────────────────────────────────────────────────────
# 14:24:01 [W1 1/40 · #1/200 · remW 40 · ✓0 ✗0] EMAIL          alias=...
_SLOG_RE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+"
    r"\[W(?P<wid>[^\s·\]]+)"
    r"(?:\s+(?P<cur>\d+)/(?P<share>\d+))?"
    r"(?:\s*·\s*#(?P<gidx>\d+)/(?P<gtotal>\d+))?"
    r"(?:\s*·\s*remW\s+(?P<remw>\d+))?"
    r"(?:\s*·\s*✓(?P<ok>\d+)\s*✗(?P<fail>\d+))?"
    r"\]\s+"
    r"(?P<phase>\S+)\s+"
    r"(?P<msg>.*)$"
)

# IMAP / other untagged lines that still matter
_IMAP_RE = re.compile(r"\[IMAP\]\s*(?P<msg>.*)$", re.I)
_EMAIL_IN_MSG = re.compile(r"(?:alias|email)=([^\s]+)", re.I)

PHASE_STYLE = {
    "START": "bold cyan",
    "BOOT": "dim",
    "BROWSER": "dim cyan",
    "FLOW": "cyan",
    "EMAIL": "blue",
    "OTP": "magenta",
    "PROFILE": "yellow",
    "TURNSTILE": "yellow",
    "SUBMIT": "orange1",
    "SETTLE": "dim yellow",
    "SSO": "green",
    "CONVERT": "green",
    "PUSH": "green",
    "CREATED": "bold green",
    "DONE": "bold green",
    "OK": "bold green",
    "FAIL": "bold red",
    "SCORE": "dim",
    "SUMMARY": "bold",
    "STOP": "red",
    "POOL": "white",
}


@dataclass
class WorkerState:
    wid: str
    share: int
    offset: int
    proxy: str = ""
    debug_port: int = 0
    proc: Optional[subprocess.Popen] = None
    status: str = "pending"  # pending | starting | running | done | dead
    phase: str = "—"
    message: str = ""
    local_cur: int = 0
    ok: int = 0
    fail: int = 0
    email: str = ""
    last_ts: str = ""
    exit_code: Optional[int] = None
    started_at: float = 0.0


@dataclass
class LogLine:
    ts: str
    wid: str
    phase: str
    message: str
    raw: str
    level: str = "info"  # info | warn | error


@dataclass
class PoolState:
    total: int
    concurrent: int
    display: str
    stagger: float
    workers: dict[str, WorkerState] = field(default_factory=dict)
    logs: list[LogLine] = field(default_factory=list)
    max_logs: int = 800
    started_at: float = 0.0
    stopping: bool = False

    @property
    def ok(self) -> int:
        return sum(w.ok for w in self.workers.values())

    @property
    def fail(self) -> int:
        return sum(w.fail for w in self.workers.values())

    @property
    def done(self) -> int:
        return self.ok + self.fail

    @property
    def alive(self) -> int:
        return sum(
            1
            for w in self.workers.values()
            if w.proc is not None and w.proc.poll() is None
        )


def parse_slog_line(line: str, default_wid: str = "?") -> Optional[LogLine]:
    line = line.rstrip("\n\r")
    if not line.strip():
        return None
    m = _SLOG_RE.match(line)
    if m:
        d = m.groupdict()
        phase = (d.get("phase") or "RUN").strip()
        msg = (d.get("msg") or "").strip()
        level = "info"
        if phase in ("FAIL", "STOP") or "error" in msg.lower() or "failed" in msg.lower():
            level = "error"
        elif "warn" in phase.lower() or msg.startswith("…") or "still on form" in msg:
            level = "warn"
        return LogLine(
            ts=d.get("ts") or "",
            wid=str(d.get("wid") or default_wid),
            phase=phase,
            message=msg,
            raw=line,
            level=level,
        )
    # untagged
    im = _IMAP_RE.search(line)
    if im:
        msg = im.group("msg")[:160]
        # skip noisy "waiting... 2s/120s" heartbeats in the UI feed
        if msg.lower().startswith("waiting..."):
            return None
        return LogLine(
            ts=time.strftime("%H:%M:%S"),
            wid=default_wid,
            phase="IMAP",
            message=msg,
            raw=line,
            level="info",
        )
    # Tip / Browser start noise → pool tag
    if line.startswith("[Tip]") or line.startswith("[*] Browser"):
        return LogLine(
            ts=time.strftime("%H:%M:%S"),
            wid=default_wid,
            phase="SYS",
            message=line[:160],
            raw=line,
            level="info",
        )
    if line.startswith("[IMAP]"):
        return LogLine(
            ts=time.strftime("%H:%M:%S"),
            wid=default_wid,
            phase="IMAP",
            message=line.replace("[IMAP]", "").strip()[:160],
            raw=line,
            level="info",
        )
    return LogLine(
        ts=time.strftime("%H:%M:%S"),
        wid=default_wid,
        phase="RAW",
        message=line[:180],
        raw=line,
        level="info",
    )


def apply_log_to_worker(state: PoolState, log: LogLine) -> None:
    w = state.workers.get(log.wid)
    if not w:
        # try numeric only
        return
    w.last_ts = log.ts or w.last_ts
    # Don't let noise overwrite the real pipeline phase
    if log.phase and log.phase not in ("RAW", "SYS", "SCORE", "IMAP", "POOL"):
        w.phase = log.phase
    elif log.phase == "IMAP" and "Found OTP" in (log.message or ""):
        w.phase = "OTP"
    if log.message and log.phase not in ("RAW", "SYS"):
        w.message = log.message[:80]

    # pull structured progress from raw if present
    m = _SLOG_RE.match(log.raw)
    if m:
        d = m.groupdict()
        if d.get("cur"):
            w.local_cur = int(d["cur"])
        if d.get("ok") is not None:
            w.ok = int(d["ok"])
        if d.get("fail") is not None:
            w.fail = int(d["fail"])
        if d.get("share"):
            w.share = int(d["share"])

    em = _EMAIL_IN_MSG.search(log.message or "")
    if em:
        w.email = em.group(1)
    if log.phase == "CREATED" and "email=" in (log.message or ""):
        em2 = re.search(r"email=(\S+)", log.message)
        if em2:
            w.email = em2.group(1)

    if log.phase in ("BOOT", "BROWSER", "START"):
        w.status = "running"
    if log.phase == "SUMMARY":
        w.status = "done"


# ── process manager ────────────────────────────────────────────────────────


class PoolRunner:
    def __init__(self, state: PoolState, event_q: queue.Queue):
        self.state = state
        self.event_q = event_q
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()

    def build_plan(
        self,
        total: int,
        concurrent: int,
        display: str,
        stagger: float,
        proxies: list[str],
    ) -> None:
        shares = split_workload(total, concurrent)
        offsets: list[int] = []
        off = 0
        for s in shares:
            offsets.append(off)
            off += s if total > 0 else 0

        self.state.total = total
        self.state.concurrent = len(shares)
        self.state.display = display
        self.state.stagger = stagger
        self.state.workers.clear()
        for i, share in enumerate(shares):
            wid = str(i + 1)
            proxy = proxies[i % len(proxies)] if proxies else ""
            self.state.workers[wid] = WorkerState(
                wid=wid,
                share=share,
                offset=offsets[i],
                proxy=proxy,
                debug_port=9300 + int(wid) * 20,
            )

    def start_all(self, python: str) -> None:
        self.state.started_at = time.time()
        t = threading.Thread(target=self._spawn_loop, args=(python,), daemon=True)
        t.start()
        self._threads.append(t)

    def _spawn_loop(self, python: str) -> None:
        workers = list(self.state.workers.values())
        for i, w in enumerate(workers):
            if self._stop.is_set() or self.state.stopping:
                break
            self._start_one(python, w)
            if i + 1 < len(workers) and self.state.stagger > 0:
                self.event_q.put(
                    (
                        "log",
                        LogLine(
                            ts=time.strftime("%H:%M:%S"),
                            wid="pool",
                            phase="POOL",
                            message=f"stagger {self.state.stagger:.0f}s before next worker…",
                            raw="",
                            level="info",
                        ),
                    )
                )
                # interruptible sleep
                end = time.time() + self.state.stagger
                while time.time() < end:
                    if self._stop.is_set() or self.state.stopping:
                        return
                    time.sleep(0.2)

    def _start_one(self, python: str, w: WorkerState) -> None:
        env = os.environ.copy()
        env["GROK_WORKER_ID"] = w.wid
        env["GROK_DEBUG_PORT"] = str(w.debug_port)
        env["GROK_DISPLAY"] = self.state.display
        env["GROK_WORKER_SHARE"] = str(w.share)
        env["GROK_POOL_TOTAL"] = str(self.state.total if self.state.total > 0 else 0)
        env["GROK_POOL_OFFSET"] = str(w.offset)
        env["PYTHONUNBUFFERED"] = "1"
        if w.proxy:
            env["GROK_BROWSER_PROXY"] = w.proxy
        else:
            env.pop("GROK_BROWSER_PROXY", None)
            env.pop("BROWSER_PROXY", None)

        cmd = [
            python,
            str(SCRIPT),
            "--count",
            str(w.share),
            "--worker-id",
            w.wid,
            "--display",
            self.state.display,
        ]
        w.status = "starting"
        w.started_at = time.time()
        try:
            # Own process group so quit kills Python + Chromium children
            proc = spawn_worker_process(
                cmd,
                cwd=str(ROOT),
                env=env,
                capture_output=True,
            )
        except Exception as e:
            w.status = "dead"
            self.event_q.put(
                (
                    "log",
                    LogLine(
                        ts=time.strftime("%H:%M:%S"),
                        wid=w.wid,
                        phase="FAIL",
                        message=f"spawn failed: {e}",
                        raw="",
                        level="error",
                    ),
                )
            )
            return

        w.proc = proc
        w.status = "running"
        self.event_q.put(
            (
                "log",
                LogLine(
                    ts=time.strftime("%H:%M:%S"),
                    wid="pool",
                    phase="POOL",
                    message=(
                        f"worker W{w.wid} started  share={w.share}  "
                        f"global#{w.offset + 1}–{w.offset + w.share if w.share else '∞'}  "
                        f"cdp={w.debug_port}  "
                        f"proxy={_mask_proxy(w.proxy) if w.proxy else '(none)'}"
                    ),
                    raw="",
                    level="info",
                ),
            )
        )
        rt = threading.Thread(target=self._read_stdout, args=(w,), daemon=True)
        rt.start()
        self._threads.append(rt)

    def _read_stdout(self, w: WorkerState) -> None:
        assert w.proc and w.proc.stdout
        try:
            for line in w.proc.stdout:
                if self._stop.is_set():
                    break
                log = parse_slog_line(line, default_wid=w.wid)
                if log:
                    # force wid for untagged lines from this process
                    if log.wid in ("?", "pool") and log.phase in ("IMAP", "SYS", "RAW"):
                        log.wid = w.wid
                    self.event_q.put(("log", log))
        except Exception:
            pass
        finally:
            code = w.proc.poll()
            if code is None:
                try:
                    code = w.proc.wait(timeout=1)
                except Exception:
                    code = -1
            w.exit_code = code
            w.status = "done" if code == 0 else "dead"
            self.event_q.put(
                (
                    "log",
                    LogLine(
                        ts=time.strftime("%H:%M:%S"),
                        wid=w.wid,
                        phase="SUMMARY",
                        message=f"process exit={code}  ✓{w.ok} ✗{w.fail}",
                        raw="",
                        level="info" if code == 0 else "error",
                    ),
                )
            )
            self.event_q.put(("worker_exit", w.wid))

    def stop_all(self) -> None:
        """Stop every worker Python process + its Chromium tree."""
        self.state.stopping = True
        self._stop.set()
        ports: list[int] = []
        for w in self.state.workers.values():
            ports.append(w.debug_port)
            terminate_worker_tree(
                w.proc,
                debug_port=w.debug_port,
                grace_sec=2.5,
            )
            w.status = "dead" if (w.proc and w.proc.poll()) else w.status
        # Final sweep (orphans / detached Chrome for Testing)
        kill_orphan_farm_chrome(ports)
        self.event_q.put(
            (
                "log",
                LogLine(
                    ts=time.strftime("%H:%M:%S"),
                    wid="pool",
                    phase="POOL",
                    message="all workers + Chromium stopped",
                    raw="",
                    level="info",
                ),
            )
        )


# ── Textual UI ─────────────────────────────────────────────────────────────


def run_tui(args_ns: argparse.Namespace) -> int:
    try:
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.widgets import DataTable, Footer, Header, RichLog, Static
        from rich.text import Text
        from rich.console import Group
    except ImportError:
        print(
            "textual not installed. Run:\n"
            "  .venv/bin/pip install 'textual>=1.0.0'\n",
            file=sys.stderr,
        )
        return 2

    cfg = load_pool_config()
    if getattr(args_ns, "unlimited", False):
        args_ns.count = 0
    total = args_ns.count
    concurrent = args_ns.workers if args_ns.workers is not None else args_ns.concurrent
    if concurrent < 1:
        print("--concurrent must be >= 1", file=sys.stderr)
        return 2
    if total < 0:
        print("--count must be >= 0", file=sys.stderr)
        return 2

    display = (
        "headless"
        if args_ns.headless
        else ("offscreen" if args_ns.offscreen else args_ns.display)
    )
    if display in ("bg", "background"):
        display = "offscreen"

    proxies = list(args_ns.proxy) if args_ns.proxy else list(cfg["proxies"])
    if args_ns.proxy_file:
        proxies = load_proxy_file(args_ns.proxy_file)

    if not SCRIPT.is_file():
        print(f"missing farm script: {SCRIPT}", file=sys.stderr)
        return 2

    event_q: queue.Queue = queue.Queue()
    state = PoolState(total=total, concurrent=concurrent, display=display, stagger=args_ns.stagger_sec)
    runner = PoolRunner(state, event_q)
    runner.build_plan(total, concurrent, display, args_ns.stagger_sec, proxies)

    def _ascii_bar(done: int, total: int, width: int = 40) -> str:
        if total <= 0:
            filled = min(width, done % (width + 1))
            return "█" * filled + "░" * (width - filled)
        filled = int(width * min(done, total) / total)
        filled = max(0, min(width, filled))
        return "█" * filled + "░" * (width - filled)

    def _fmt_dur(sec: float) -> str:
        if not sec or sec < 0 or sec != sec:  # NaN
            return "—"
        s = int(round(sec))
        h, s = divmod(s, 3600)
        m, s = divmod(s, 60)
        if h:
            return f"{h}h {m:02d}m"
        if m:
            return f"{m}m {s:02d}s"
        return f"{s}s"

    class SummaryPanel(Static):
        def render(self):
            elapsed = time.time() - state.started_at if state.started_at else 0
            tot = state.total if state.total > 0 else 0
            done = state.done
            ok = state.ok
            fail = state.fail
            pct = (done / tot * 100) if tot else 0
            # acc/min: prefer successful accounts; else total done (early stage)
            rate_base = ok if ok > 0 else done
            rate = (rate_base / (elapsed / 60.0)) if elapsed >= 8 and rate_base > 0 else 0.0
            remaining = max(0, tot - (ok if ok > 0 else done)) if tot else 0
            eta_sec = (remaining / rate * 60.0) if rate > 0 and remaining > 0 else (
                0.0 if tot and remaining == 0 and done > 0 else None
            )

            head = Text()
            head.append(" Grok Farm ", style="bold white on dark_blue")
            head.append(f"  {display}  ", style="dim")
            head.append(f"elapsed {_fmt_dur(elapsed)}", style="cyan")
            if state.stopping:
                head.append("  STOPPING…", style="bold red")

            stats = Text()
            if tot:
                stats.append(f"  {done}/{tot}  ", style="bold")
                stats.append(f"({pct:.0f}%)  ", style="dim")
            else:
                stats.append(f"  done={done}  ∞ mode  ", style="bold")
            stats.append(f"✓{ok} ", style="bold green")
            stats.append(f"✗{fail}  ", style="bold red")
            stats.append(f"alive={state.alive}/{len(state.workers)}  ", style="cyan")

            rate_line = Text("  ")
            if rate > 0:
                rate_line.append(f"~{rate:.1f} acc/min  ", style="bold cyan")
            else:
                rate_line.append("acc/min …  ", style="dim")
            if eta_sec is None:
                rate_line.append("ETA …", style="dim")
            elif eta_sec <= 0:
                rate_line.append("ETA done", style="green")
            else:
                rate_line.append(f"ETA ~{_fmt_dur(eta_sec)}", style="yellow")
                try:
                    finish_ts = time.strftime(
                        "%H:%M", time.localtime(time.time() + eta_sec)
                    )
                    rate_line.append(f"  (≈{finish_ts})", style="dim")
                except Exception:
                    pass
            if remaining and tot:
                rate_line.append(f"  left {remaining}", style="dim")

            bar = Text("  ")
            bar.append(_ascii_bar(done, tot, 40), style="green" if done else "dim")
            if tot:
                bar.append(f"  {pct:.0f}%", style="dim")

            return Group(head, stats, rate_line, bar)

    class FarmApp(App):
        CSS = """
        Screen {
            layout: vertical;
        }
        #summary {
            height: 7;
            border: solid $accent;
            padding: 0 1;
            margin: 0 0 1 0;
        }
        #workers {
            height: 10;
            border: solid $primary;
            margin: 0 0 1 0;
        }
        #log {
            height: 1fr;
            border: solid $surface;
            scrollbar-size: 1 1;
        }
        """

        BINDINGS = [
            Binding("q", "quit_stop", "Quit & stop", priority=True),
            Binding("ctrl+c", "quit_stop", "Quit", show=False, priority=True),
            Binding("a", "filter_all", "All logs"),
            Binding("p", "toggle_pause", "Pause scroll"),
            Binding("1", "filter_w('1')", "W1", show=False),
            Binding("2", "filter_w('2')", "W2", show=False),
            Binding("3", "filter_w('3')", "W3", show=False),
            Binding("4", "filter_w('4')", "W4", show=False),
            Binding("5", "filter_w('5')", "W5", show=False),
            Binding("6", "filter_w('6')", "W6", show=False),
            Binding("7", "filter_w('7')", "W7", show=False),
            Binding("8", "filter_w('8')", "W8", show=False),
            Binding("9", "filter_w('9')", "W9", show=False),
        ]

        def __init__(self):
            super().__init__()
            self.filter_wid: Optional[str] = None
            self.pause_scroll = False
            self._exit_code = 0

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield SummaryPanel(id="summary")
            yield DataTable(id="workers", zebra_stripes=True)
            yield RichLog(id="log", highlight=True, markup=True, wrap=True, max_lines=600)
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#workers", DataTable)
            # (label, key) so update_cell keys stay stable
            table.add_columns(
                ("W", "w"),
                ("Status", "status"),
                ("Local", "local"),
                ("Global", "global"),
                ("Phase", "phase"),
                ("✓", "ok"),
                ("✗", "fail"),
                ("Email", "email"),
                ("Last", "last"),
            )
            table.cursor_type = "row"
            for wid in sorted(state.workers.keys(), key=lambda x: int(x) if x.isdigit() else 0):
                w = state.workers[wid]
                g_from = w.offset + 1 if state.total > 0 else 0
                g_to = w.offset + w.share if state.total > 0 else 0
                table.add_row(
                    f"W{wid}",
                    w.status,
                    f"0/{w.share}" if w.share else "0/∞",
                    f"{g_from}-{g_to}" if state.total > 0 else "—",
                    "—",
                    "0",
                    "0",
                    "",
                    "starting…",
                    key=wid,
                )

            log = self.query_one("#log", RichLog)
            log.write(
                f"[bold]Grok Farm TUI[/]  total={state.total or '∞'}  "
                f"workers={len(state.workers)}  display={state.display}  "
                f"stagger={state.stagger}s"
            )
            log.write("[dim]keys: q=stop · a=all logs · 1-9=filter worker · p=pause scroll[/]")

            self.set_interval(0.25, self._drain_events)
            self.set_interval(1.0, self._refresh_summary)
            # start workers after UI is up
            runner.start_all(sys.executable)

        def _refresh_summary(self) -> None:
            self.query_one("#summary", SummaryPanel).refresh()
            self._refresh_table()
            # auto-exit when all workers finished and not stopping mid-way
            if state.started_at and not state.stopping:
                if state.workers and all(
                    w.status in ("done", "dead") and w.proc is not None
                    for w in state.workers.values()
                ):
                    # allow a beat for final logs
                    if all(w.proc and w.proc.poll() is not None for w in state.workers.values()):
                        self._exit_code = 0 if state.fail == 0 else 1
                        self.set_timer(1.5, self.action_quit_stop)

        def _refresh_table(self) -> None:
            table = self.query_one("#workers", DataTable)
            for wid, w in state.workers.items():
                row_key = wid
                try:
                    share_s = f"{w.local_cur}/{w.share}" if w.share else f"{w.local_cur}/∞"
                    gidx = w.offset + w.local_cur if w.local_cur else w.offset + 1
                    g_s = f"#{gidx}/{state.total}" if state.total > 0 else "—"
                    st_style = {
                        "running": "green",
                        "starting": "yellow",
                        "done": "cyan",
                        "dead": "red",
                        "pending": "dim",
                    }.get(w.status, "")
                    table.update_cell(row_key, "status", Text(w.status, style=st_style))
                    table.update_cell(row_key, "local", share_s)
                    table.update_cell(row_key, "global", g_s)
                    table.update_cell(row_key, "phase", w.phase[:14])
                    table.update_cell(row_key, "ok", str(w.ok))
                    table.update_cell(row_key, "fail", str(w.fail))
                    table.update_cell(row_key, "email", (w.email or "")[:28])
                    table.update_cell(row_key, "last", (w.message or "")[:40])
                except Exception:
                    pass

        def _drain_events(self) -> None:
            log_w = self.query_one("#log", RichLog)
            n = 0
            while n < 80:
                try:
                    kind, payload = event_q.get_nowait()
                except queue.Empty:
                    break
                n += 1
                if kind == "log":
                    log: LogLine = payload
                    if log.wid != "pool":
                        apply_log_to_worker(state, log)
                    state.logs.append(log)
                    if len(state.logs) > state.max_logs:
                        state.logs = state.logs[-state.max_logs :]

                    if self.filter_wid and log.wid not in (self.filter_wid, "pool"):
                        continue

                    style = PHASE_STYLE.get(log.phase, "white")
                    if log.level == "error":
                        style = "bold red"
                    elif log.level == "warn":
                        style = "yellow"

                    wid_s = f"W{log.wid}" if log.wid not in ("pool", "?") else log.wid
                    line = Text()
                    line.append(f"{log.ts} ", style="dim")
                    line.append(f"{wid_s:<4} ", style="bold cyan" if log.wid != "pool" else "white")
                    line.append(f"{log.phase:<12} ", style=style)
                    line.append(log.message[:140], style=style if log.level != "info" else "")
                    log_w.write(line)
                    if not self.pause_scroll:
                        log_w.scroll_end(animate=False)

                elif kind == "worker_exit":
                    self._refresh_table()

        def action_filter_all(self) -> None:
            self.filter_wid = None
            self.query_one("#log", RichLog).write("[dim]filter: all workers[/]")

        def action_filter_w(self, wid: str) -> None:
            if wid in state.workers:
                self.filter_wid = wid
                self.query_one("#log", RichLog).write(f"[dim]filter: W{wid} only (press a = all)[/]")

        def action_toggle_pause(self) -> None:
            self.pause_scroll = not self.pause_scroll
            self.query_one("#log", RichLog).write(
                f"[dim]auto-scroll {'paused' if self.pause_scroll else 'resumed'}[/]"
            )

        def action_quit_stop(self) -> None:
            if not state.stopping:
                state.stopping = True
                try:
                    self.query_one("#log", RichLog).write(
                        "[bold red]stopping all workers + Chromium…[/]"
                    )
                except Exception:
                    pass
                # Synchronous: wait until process groups + CDP ports are dead
                runner.stop_all()
                try:
                    self.query_one("#log", RichLog).write(
                        "[bold green]Chrome closed. Bye.[/]"
                    )
                except Exception:
                    pass
            self.exit(self._exit_code)

    app = FarmApp()
    # ensure clean stop on signals outside textual when possible
    def _sig(_s=None, _f=None):
        runner.stop_all()

    try:
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass

    return app.run() or 0


def build_arg_parser() -> argparse.ArgumentParser:
    cfg = load_pool_config()
    p = argparse.ArgumentParser(
        description="Grok farm TUI — live multi-worker dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "-n",
        "--count",
        type=int,
        default=cfg["count"],
        help="total accounts (0 = unlimited until stop)",
    )
    p.add_argument(
        "-u",
        "--unlimited",
        action="store_true",
        help="farm forever until quit (same as -n 0)",
    )
    p.add_argument(
        "-c",
        "--concurrent",
        type=int,
        default=cfg["concurrent"],
        dest="concurrent",
        help="parallel browsers",
    )
    p.add_argument("--workers", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument(
        "--stagger",
        type=float,
        default=cfg["stagger_sec"],
        dest="stagger_sec",
        help="seconds between starting each worker",
    )
    p.add_argument("--proxy-file", default="", help="proxy list file")
    p.add_argument("--proxy", action="append", default=[], help="proxy URL (repeatable)")
    p.add_argument(
        "--display",
        choices=["headed", "offscreen", "headless"],
        default=cfg.get("display") or ("offscreen" if platform_is_mac() else "headed"),
    )
    p.add_argument("--headless", action="store_true")
    p.add_argument("--offscreen", action="store_true")
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_tui(args)


if __name__ == "__main__":
    raise SystemExit(main())

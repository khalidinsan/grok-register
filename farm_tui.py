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
import json
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
# Worker log tag forms (all must match):
#   Finite:     [W2 3/33 · #70/100 · remW 30 · ✓2 ✗0]
#   Unlimited:  [W2 3 · #3 · ✓2 ✗0]
#   Legacy ∞:   [W2 #3 · ✓2 ✗0]  (still accepted)
_SLOG_RE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+"
    r"\[W(?P<wid>[^\s·\]]+)"
    # cur with optional /share  OR  legacy "#cur"
    r"(?:"
    r"\s+(?P<cur>\d+)(?:/(?P<share>\d+))?"
    r"|"
    r"\s+#(?P<cur_legacy>\d+)"
    r")?"
    # global index with optional /total
    r"(?:\s*·\s*#(?P<gidx>\d+)(?:/(?P<gtotal>\d+))?)?"
    r"(?:\s*·\s*remW\s+(?P<remw>\d+))?"
    # success / failed counters
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
    "GOOGLE": "bright_blue",
    "HYBRID": "blue",
    "OTP": "magenta",
    "PROFILE": "yellow",
    "TURNSTILE": "yellow",
    "SUBMIT": "orange1",
    "SETTLE": "dim yellow",
    "SSO": "green",
    "CONVERT": "green",
    # Bot-flag live probe (cli-chat-proxy grok-4.6) — high visibility
    "SMOKE": "bold bright_cyan",
    "PROBE": "bold bright_cyan",
    "SETTLE": "dim yellow",
    "PROXY": "bold magenta",
    "PUSH": "green",
    "CREATED": "bold green",
    "DONE": "bold green",
    "OK": "bold green",
    "FAIL": "bold red",
    "RESULT": "bold white",
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
    phase_started_at: float = 0.0
    phase_durations: dict[str, float] = field(default_factory=dict)
    watchdog_warned_phase: str = ""
    outcomes: dict[str, int] = field(default_factory=lambda: {
        name: 0 for name in ("attempted", "registered", "oauth_ok", "usable", "inactive", "hard_failed")
    })


@dataclass
class LogLine:
    ts: str
    wid: str
    phase: str
    message: str
    raw: str
    level: str = "info"  # info | warn | error
    event: Optional[dict] = None


@dataclass(frozen=True)
class AccountRecord:
    """Immutable snapshot shown in the globally ordered recent-account ledger."""
    ts: str
    wid: str
    email: str
    status: str
    duration: Optional[float] = None
    identity: str = ""
    account_index: Optional[int] = None


@dataclass
class PoolState:
    total: int
    concurrent: int
    display: str
    stagger: float
    workers: dict[str, WorkerState] = field(default_factory=dict)
    logs: list[LogLine] = field(default_factory=list)
    accounts: tuple[AccountRecord, ...] = ()
    seen_event_keys: set[str] = field(default_factory=set)
    event_key_order: list[str] = field(default_factory=list)
    terminal_failure_attempts: set[str] = field(default_factory=set)
    max_event_keys: int = 2048
    max_logs: int = 800
    started_at: float = 0.0
    stopping: bool = False
    proxy_mode: str = "per_account"
    proxy_pool: list[str] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return sum(w.ok for w in self.workers.values())

    @property
    def fail(self) -> int:
        return sum(w.fail for w in self.workers.values())

    @property
    def done(self) -> int:
        """Accounts finished (success + failed) — not only passes."""
        return self.ok + self.fail

    @property
    def success(self) -> int:
        """Alias: accounts that passed pipeline."""
        return self.ok

    @property
    def failed(self) -> int:
        """Alias: accounts that failed pipeline."""
        return self.fail

    @property
    def alive(self) -> int:
        return sum(
            1
            for w in self.workers.values()
            if w.proc is not None and w.proc.poll() is None
        )


def progress_bar(done: int, total: int, width: int = 40, now: Optional[float] = None) -> tuple[str, bool]:
    """Return a monotonic finite bar, or a time-based indeterminate activity pulse."""
    width = max(1, width)
    if total > 0:
        filled = max(0, min(width, int(width * min(max(done, 0), total) / total)))
        return "█" * filled + "░" * (width - filled), True
    tick = int((time.time() if now is None else now) * 4)
    if width == 1:
        return ("◆" if tick % 2 == 0 else "·"), False
    pulse_width = max(1, min(3, width - 1))
    position = tick % (width - pulse_width + 1)
    return "░" * position + "█" * pulse_width + "░" * (width - position - pulse_width), False


def pass_rate(success: int, failed: int) -> Optional[float]:
    terminal = max(0, success) + max(0, failed)
    return (max(0, success) / terminal * 100.0) if terminal else None


def select_log_filter(requested: Optional[str], worker_ids) -> Optional[str]:
    if requested in (None, "", "all"):
        return None
    requested = str(requested)
    return requested if requested in {str(wid) for wid in worker_ids} else None


def log_matches_filter(log: LogLine, wid: Optional[str]) -> bool:
    return wid is None or log.wid in (wid, "pool")


def _event_status(log: LogLine) -> Optional[str]:
    event = log.event or {}
    outcome = str(event.get("outcome") or "").lower()
    ledger = str(event.get("ledger_status") or "").lower()
    if outcome in ("usable", "inactive", "hard_failed"):
        return {"usable": "USABLE", "inactive": "INACTIVE", "hard_failed": "FAIL"}[outcome]
    if ledger in ("usable", "injected"):
        return "USABLE"
    if ledger == "failed_probe":
        return "INACTIVE" if int(event.get("probe_status") or 0) == 403 else "FAIL"
    if str(event.get("category") or "").lower() == "account" and str(event.get("event") or "").lower() == "complete":
        return "PASS" if event.get("ok") is not False else "FAIL"
    if log.phase == "RESULT":
        if re.search(r"\bPASS\b", log.message.upper()):
            return "PASS"
        if re.search(r"\bFAIL(?:ED)?\b", log.message.upper()):
            return "FAIL"
    return None


def capture_account(state: PoolState, log: LogLine) -> Optional[AccountRecord]:
    """Capture/update completion and async outcomes using stable correlation keys."""
    status = _event_status(log)
    if status is None:
        return None
    event = log.event or {}
    worker = str(event.get("worker") or event.get("worker_id") or log.wid or "?")
    value = event.get("account_index", event.get("index"))
    try:
        account_index = int(value) if value is not None else None
    except (TypeError, ValueError):
        account_index = None
    identity = str(event.get("job_id") or event.get("attempt_id") or event.get("account_id") or event.get("event_id") or "")
    email = str(event.get("email") or "").strip()
    if not email:
        match = _EMAIL_IN_MSG.search(log.message or "")
        email = match.group(1) if match else ""
    worker_state = state.workers.get(worker)
    if not email and worker_state:
        email = worker_state.email
    if not email:
        email = "—"
    raw_duration = event.get("elapsed_sec", event.get("duration_sec"))
    duration = float(raw_duration) if isinstance(raw_duration, (int, float)) else None
    match_at = None
    for pos, old in enumerate(state.accounts):
        if (identity and old.identity == identity) or (account_index is not None and old.account_index == account_index and old.wid == worker) or (email != "—" and old.email == email):
            match_at = pos
            break
    if match_at is not None:
        old = state.accounts[match_at]
        record = AccountRecord(log.ts or old.ts, worker if worker != "pool" else old.wid,
            email if email != "—" else old.email, status, duration if duration is not None else old.duration,
            identity or old.identity, account_index if account_index is not None else old.account_index)
        state.accounts = state.accounts[:match_at] + state.accounts[match_at + 1:] + (record,)
    else:
        record = AccountRecord(log.ts, worker, email, status, duration, identity, account_index)
        state.accounts = (state.accounts + (record,))[-10:]
    return record


# Loose fallback when tag form drifts — still recover W# / phase / ✓✗
_SLOG_LOOSE_RE = re.compile(
    r"^(?P<ts>\d{2}:\d{2}:\d{2})\s+"
    r"\[W(?P<wid>[^\s·\]]+)[^\]]*\]\s+"
    r"(?P<phase>\S+)\s+"
    r"(?P<msg>.*)$"
)


def parse_slog_line(line: str, default_wid: str = "?") -> Optional[LogLine]:
    line = line.rstrip("\n\r")
    if not line.strip():
        return None
    # Producer protocol is an exact prefix followed immediately by compact JSON.
    # Keep accepting bare JSON for compatibility with older/offline emitters.
    marker_at = line.find("@@GROK_EVENT@@")
    event_text = line[marker_at + len("@@GROK_EVENT@@"):] if marker_at >= 0 else line
    try:
        event = json.loads(event_text)
    except (TypeError, ValueError):
        event = None
    if isinstance(event, dict) and ("event" in event or "outcome" in event or "phase" in event):
        phase = str(event.get("phase") or event.get("event") or "EVENT").upper()
        message = str(event.get("message") or event.get("detail") or event.get("outcome") or "")
        level = str(event.get("level") or "info").lower()
        if level not in ("info", "warn", "error"):
            level = "info"
        return LogLine(
            ts=str(event.get("ts") or time.strftime("%H:%M:%S")),
            wid=str(event.get("worker") or event.get("worker_id") or event.get("wid") or default_wid),
            phase=phase, message=message, raw=line, level=level, event=event,
        )
    m = _SLOG_RE.match(line)
    if not m:
        # unlimited / future tag variants still carry W# + phase
        m = _SLOG_LOOSE_RE.match(line)
    if m:
        d = m.groupdict()
        phase = (d.get("phase") or "RUN").strip()
        msg = (d.get("msg") or "").strip()
        level = "info"
        msg_u = msg.upper()
        msg_l = msg.lower()
        # Pass outcomes first — SCORE/RESULT embed "failed=0" which must NOT go red
        if phase in ("OK", "DONE", "CREATED") or (
            phase == "RESULT" and "PASS" in msg_u and "FAIL" not in msg_u.split("PASS")[0]
        ):
            level = "info"
        elif phase == "RESULT" and ("FAIL" in msg_u or "✗" in msg):
            level = "error"
        elif phase in ("FAIL", "STOP"):
            level = "error"
        elif re.search(r"\berror\b", msg_l) and "→ success" not in msg_l:
            level = "error"
        # bare "failed" only when it's a real failure phrase, not "failed=N" tally
        elif re.search(r"\bfailed\b(?!\s*[=:]\s*\d)", msg_l) and phase not in (
            "SCORE",
            "RESULT",
            "OK",
            "DONE",
        ):
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


def _extract_ok_fail(text: str) -> Optional[tuple[int, int]]:
    """
    Pull success/failed tallies from a log line or message.

    Accepts (in priority order):
      success=3 failed=1
      ✓3/✗1
      ✓3 ✗1
    """
    if not text:
        return None
    ms = re.search(r"success[=:](\d+)", text, re.I)
    mf = re.search(r"failed[=:](\d+)", text, re.I)
    if ms and mf:
        return int(ms.group(1)), int(mf.group(1))
    m = re.search(r"✓(\d+)\s*/\s*✗(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"✓(\d+)\s*✗(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None


def structured_event_key(log: LogLine) -> str:
    event = log.event or {}
    event_id = str(event.get("event_id") or "")
    if event_id:
        return "event:" + event_id
    job_id = str(event.get("job_id") or "")
    outcome = str(event.get("outcome") or "")
    category = str(event.get("category") or "")
    if job_id and outcome:
        return f"job:{job_id}:{category}:{outcome}"
    return ""


def accept_structured_event(state: PoolState, log: LogLine) -> bool:
    """Bounded replay guard; unkeyed phase/log events remain distinct."""
    key = structured_event_key(log)
    if not key:
        return True
    if key in state.seen_event_keys:
        return False
    state.seen_event_keys.add(key)
    state.event_key_order.append(key)
    overflow = len(state.event_key_order) - max(1, state.max_event_keys)
    if overflow > 0:
        for expired in state.event_key_order[:overflow]:
            state.seen_event_keys.discard(expired)
        del state.event_key_order[:overflow]
    return True


def apply_log_to_worker(state: PoolState, log: LogLine, *, event_accepted: Optional[bool] = None) -> None:
    if event_accepted is None:
        event_accepted = accept_structured_event(state, log)
    event_worker = str((log.event or {}).get("worker") or (log.event or {}).get("worker_id") or "")
    w = state.workers.get(event_worker or log.wid)
    if not w:
        return
    w.last_ts = log.ts or w.last_ts
    now = time.time()
    if log.phase and log.phase not in ("RAW", "SYS", "SCORE", "IMAP", "POOL") and log.phase != w.phase:
        if w.phase_started_at and w.phase not in ("—", ""):
            w.phase_durations[w.phase] = w.phase_durations.get(w.phase, 0.0) + now - w.phase_started_at
        w.phase_started_at = now
        w.watchdog_warned_phase = ""
    if log.event and event_accepted:
        producer_event = str(log.event.get("event") or "").lower()
        category = str(log.event.get("category") or "").lower()
        ledger_status = str(log.event.get("ledger_status") or "").lower()
        outcome = str(log.event.get("outcome") or "").lower()
        if not outcome:
            if category == "account" and producer_event == "start":
                outcome = "attempted"
            elif category == "account" and producer_event == "complete" and log.event.get("ok"):
                outcome = "registered"
            elif producer_event == "success" and category == "oauth":
                outcome = "oauth_ok"
            elif ledger_status in ("usable", "injected"):
                outcome = "usable"
            elif ledger_status == "failed_probe" and int(log.event.get("probe_status") or 0) == 403:
                outcome = "inactive"
        if outcome in w.outcomes:
            if outcome == "hard_failed":
                # OAuth/phase failures may be followed by account complete false.
                # Count only explicit structured terminal outcomes, once per attempt.
                attempt_key = str(log.event.get("attempt_id") or log.event.get("job_id") or "")
                if attempt_key and attempt_key in state.terminal_failure_attempts:
                    outcome = ""
                elif attempt_key:
                    state.terminal_failure_attempts.add(attempt_key)
            if outcome:
                value = log.event.get("count")
                w.outcomes[outcome] = int(value) if isinstance(value, int) else w.outcomes[outcome] + 1
        counts = log.event.get("outcomes")
        if isinstance(counts, dict):
            for name in w.outcomes:
                if isinstance(counts.get(name), int):
                    w.outcomes[name] = counts[name]
        phase_started = log.event.get("phase_started_at")
        if isinstance(phase_started, (int, float)):
            w.phase_started_at = float(phase_started)
        duration = log.event.get("duration_sec")
        if isinstance(duration, (int, float)):
            w.phase_durations[log.phase] = float(duration)
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
        cur = d.get("cur") or d.get("cur_legacy")
        if cur:
            w.local_cur = int(cur)
        if d.get("ok") is not None:
            w.ok = int(d["ok"])
        if d.get("fail") is not None:
            w.fail = int(d["fail"])
        if d.get("share"):
            try:
                sh = int(d["share"])
                if sh > 0:
                    w.share = sh
            except (TypeError, ValueError):
                pass
    else:
        # Loose: recover cur from "W1 #4 · …" or "W1 4 · …" when strict tag drifts
        mcur = re.search(
            r"\[W[^\s·\]]+(?:\s+(?P<cur>\d+)(?:/\d+)?|\s+#(?P<cur_legacy>\d+))",
            log.raw or "",
        )
        if mcur:
            cur = mcur.group("cur") or mcur.group("cur_legacy")
            if cur:
                w.local_cur = int(cur)

    # Fallback / belt-and-suspenders: any line may embed tallies
    # Semantics: success=✓ pass, failed=✗ not-pass, done=success+failed
    for blob in (log.raw or "", log.message or ""):
        pair = _extract_ok_fail(blob)
        if pair:
            w.ok, w.fail = pair
            break

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
    # Keep last outcome visible on the worker row
    if log.phase == "OK" or (log.phase == "RESULT" and "PASS" in (log.message or "").upper()):
        w.message = f"✓ {(log.message or '')[:70]}"
    elif log.phase == "FAIL" or (log.phase == "RESULT" and "FAIL" in (log.message or "").upper()):
        w.message = f"✗ {(log.message or '')[:70]}"


# ── process manager ────────────────────────────────────────────────────────


class PoolRunner:
    def __init__(self, state: PoolState, event_q: queue.Queue):
        self.state = state
        self.event_q = event_q
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._probe_proc: Optional[subprocess.Popen] = None
        self._probe_cmd: list[str] = []
        self._probe_env: dict[str, str] = {}
        self._probe_queue_path = ""
        self._probe_restarts = 0
        self.probe_worker_unhealthy = False
        self.queue_state_unknown = False

    def build_plan(
        self,
        total: int,
        concurrent: int,
        display: str,
        stagger: float,
        proxies: list[str],
        proxy_mode: str = "per_account",
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
        self.state.proxy_mode = proxy_mode
        self.state.proxy_pool = list(proxies or [])
        self.state.workers.clear()
        for i, share in enumerate(shares):
            wid = str(i + 1)
            # sticky preview only; per_account rotates inside worker
            sticky = (
                proxies[i % len(proxies)]
                if proxies and proxy_mode == "per_worker"
                else (f"pool×{len(proxies)}" if proxies else "")
            )
            self.state.workers[wid] = WorkerState(
                wid=wid,
                share=share,
                offset=offsets[i],
                proxy=sticky,
                debug_port=9300 + int(wid) * 20,
            )

    def start_all(self, python: str) -> None:
        self.state.started_at = time.time()
        async_raw = str(os.environ.get("GROK_ASYNC_PROBE_PUSH") or "").lower()
        async_enabled = async_raw in ("1", "true", "yes", "on")
        if not async_raw:
            try:
                full = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                gcli = full.get("grok_cli") if isinstance(full.get("grok_cli"), dict) else {}
                async_enabled = bool(gcli.get("async_probe_push", False))
            except Exception:
                async_enabled = False
        if async_enabled:
            os.environ["GROK_ASYNC_PROBE_PUSH"] = "1"
            queue_path = (os.environ.get("GROK_PROBE_QUEUE_PATH") or "").strip()
            if not queue_path:
                try:
                    full = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                    gcli = full.get("grok_cli") if isinstance(full.get("grok_cli"), dict) else {}
                    queue_path = str(gcli.get("probe_queue_path") or "").strip()
                except Exception:
                    queue_path = ""
            if not queue_path:
                queue_path = str(ROOT / "logs" / "probe-queue" / "jobs.sqlite3")
            env = os.environ.copy()
            env["GROK_PROBE_QUEUE_PATH"] = queue_path
            self._probe_queue_path = queue_path
            self._probe_env = env
            self._probe_cmd = [python, str(ROOT / "probe_queue.py"), "--db", queue_path,
                               "--handler", "probe_job_handler:handle", "--terminal-handler",
                               "probe_job_handler:finalize_stale"]
            self._start_probe_worker()
        t = threading.Thread(target=self._spawn_loop, args=(python,), daemon=True)
        t.start()
        self._threads.append(t)

    def _start_probe_worker(self) -> None:
        self._probe_proc = spawn_worker_process(self._probe_cmd, cwd=str(ROOT),
                                                env=self._probe_env, capture_output=True)
        self.event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "POOL",
                                           "async probe worker started", "", "info")))
        thread = threading.Thread(target=self._read_probe_stdout, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _read_probe_stdout(self) -> None:
        proc = self._probe_proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            log = parse_slog_line(line, default_wid="pool")
            if log:
                log.wid = "pool"
                self.event_q.put(("log", log))
        code = proc.wait()
        if self._stop.is_set() or self.state.stopping:
            return
        self.event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "FAIL",
                                           f"async probe worker exited code={code}", "", "error")))
        if self._probe_restarts < 1:
            self._probe_restarts += 1
            time.sleep(1.0)
            try:
                self._start_probe_worker()
            except Exception as exc:
                self.probe_worker_unhealthy = True
                self.event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "FAIL",
                                                   f"async probe restart failed: {exc}", "", "error")))
        else:
            self.probe_worker_unhealthy = True
            self.event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "FAIL",
                                               "async probe worker persistently unhealthy", "", "error")))

    def queue_counts(self) -> Optional[dict[str, int]]:
        if not self._probe_queue_path:
            return {"pending": 0, "claimed": 0, "terminal_pending": 0, "finalizing": 0}
        try:
            from probe_queue import ProbeQueue
            return ProbeQueue(self._probe_queue_path).counts()
        except Exception as exc:
            self.queue_state_unknown = True
            self.event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "FAIL",
                                               f"queue state unavailable: {type(exc).__name__}", "", "error")))
            return None

    def _spawn_loop(self, python: str) -> None:
        workers = list(self.state.workers.values())
        for i, w in enumerate(workers):
            if self._stop.is_set() or self.state.stopping:
                break
            # Adaptive concurrency is deliberately non-destructive: honor a shared
            # cooldown before starting new work, never terminate live workers.
            try:
                from farm_coordination import cooldown_remaining
                while cooldown_remaining() > 0:
                    if self._stop.is_set() or self.state.stopping:
                        return
                    time.sleep(min(1.0, cooldown_remaining()))
            except Exception:
                pass
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
        from proxy_util import encode_proxy_env

        env = os.environ.copy()
        env["GROK_WORKER_ID"] = w.wid
        env["GROK_DEBUG_PORT"] = str(w.debug_port)
        env["GROK_DISPLAY"] = self.state.display
        env["GROK_WORKER_SHARE"] = str(w.share)
        env["GROK_POOL_TOTAL"] = str(self.state.total if self.state.total > 0 else 0)
        env["GROK_POOL_OFFSET"] = str(w.offset)
        env["GROK_POOL_CONCURRENT"] = str(self.state.concurrent)
        env["GROK_PROXY_MODE"] = getattr(self.state, "proxy_mode", None) or "per_account"
        coord_dir = Path(os.environ.get("GROK_COORD_DIR") or (ROOT / "logs" / "coordination"))
        env.setdefault("GROK_COORD_DIR", str(coord_dir))
        env.setdefault("GROK_COORD_STATE_PATH", str(coord_dir / "state.json"))
        env.setdefault("GROK_COORD_LOCK_PATH", str(coord_dir / "state.lock"))
        env.setdefault("GROK_CONFIG_PATH", str(ROOT / "config.json"))
        env.setdefault("GROK_EVENT_FORMAT", "jsonl")
        env["PYTHONUNBUFFERED"] = "1"
        # flash-aligned proxy retry / asset-block (env wins if already set)
        env.setdefault(
            "GROK_PROXY_RETRIES",
            str(os.environ.get("GROK_PROXY_RETRIES") or "3"),
        )
        env.setdefault(
            "GROK_PROXY_FALLBACK_DIRECT",
            str(os.environ.get("GROK_PROXY_FALLBACK_DIRECT") or "1"),
        )
        env.setdefault(
            "GROK_BLOCK_ASSETS",
            str(os.environ.get("GROK_BLOCK_ASSETS") or "1"),
        )
        # register_mode / oauth gap / deferred probe — prefer shell env, else config.json
        _full = {}
        try:
            _cfgp = ROOT / "config.json"
            if _cfgp.is_file():
                import json as _json

                _full = _json.loads(_cfgp.read_text(encoding="utf-8"))
        except Exception:
            _full = {}
        _gcli = _full.get("grok_cli") if isinstance(_full.get("grok_cli"), dict) else {}
        _rmode = str(
            os.environ.get("GROK_REGISTER_MODE")
            or _full.get("register_mode")
            or (_full.get("run") or {}).get("register_mode")
            or "browser"
        ).strip().lower()
        if _rmode not in ("hybrid", "browser", "google"):
            _rmode = "browser"
        env.setdefault("GROK_REGISTER_MODE", _rmode)
        env.setdefault(
            "GROK_OAUTH_GAP_SEC",
            str(
                os.environ.get("GROK_OAUTH_GAP_SEC")
                or _gcli.get("oauth_gap_sec")
                or "8"
            ),
        )
        _off = os.environ.get("GROK_CHAT_PROBE_OFF_CRITICAL")
        if _off is None or str(_off).strip() == "":
            _off_cfg = _gcli.get("chat_probe_off_critical")
            _off = "1" if (_off_cfg is None or _off_cfg is True) else "0"
        env.setdefault("GROK_CHAT_PROBE_OFF_CRITICAL", str(_off))
        pool = getattr(self.state, "proxy_pool", None) or []
        if pool:
            env["GROK_PROXIES"] = encode_proxy_env(pool)
        else:
            env.pop("GROK_PROXIES", None)
        mode = env["GROK_PROXY_MODE"]
        if mode == "per_worker" and w.proxy and not w.proxy.startswith("pool×"):
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
        if self._probe_proc is not None:
            terminate_worker_tree(self._probe_proc, grace_sec=5.0)
            self._probe_proc = None
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
        from textual.containers import Horizontal, Vertical
        from textual.widgets import DataTable, Footer, Header, RichLog, Static, Tab, Tabs
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

    # flash-aligned: CLI shortcuts > --display > config > env > platform
    if getattr(args_ns, "headed", False):
        display = "headed"
    elif getattr(args_ns, "virtual", False):
        display = "virtual"
    elif args_ns.headless:
        display = "headless"
    elif args_ns.offscreen:
        display = "offscreen"
    elif args_ns.display:
        display = args_ns.display
    elif cfg.get("display"):
        display = str(cfg["display"])
    else:
        display = ""
    try:
        from browser_engine import resolve_display, normalize_display

        forced = (
            getattr(args_ns, "headed", False)
            or getattr(args_ns, "virtual", False)
            or args_ns.headless
            or args_ns.offscreen
            or bool(args_ns.display)
        )
        if forced:
            display = normalize_display(display) or display
        else:
            if display:
                os.environ.setdefault(
                    "GROK_DISPLAY", normalize_display(display) or display
                )
            display = resolve_display(display)
    except Exception:
        if not display or display in ("bg", "background"):
            display = "offscreen" if platform_is_mac() else "headless"
    os.environ["GROK_DISPLAY"] = display
    if display == "headless":
        os.environ["GROK_HEADLESS"] = "true"
    elif display == "virtual":
        os.environ["GROK_HEADLESS"] = "virtual"
    else:
        os.environ["GROK_HEADLESS"] = "false"

    from proxy_util import load_proxy_list, normalize_proxy

    if args_ns.proxy:
        proxies = load_proxy_list(args_ns.proxy)
    else:
        proxies = list(cfg.get("proxies") or [])
    if args_ns.proxy_file:
        proxies = load_proxy_file(args_ns.proxy_file)
    proxies = [normalize_proxy(p) or p for p in proxies if p]
    proxy_mode = (
        getattr(args_ns, "proxy_mode", None)
        or cfg.get("proxy_mode")
        or "per_account"
    )

    do_check = bool(getattr(args_ns, "proxy_check", cfg.get("proxy_check", True)))
    if proxies and do_check:
        from proxy_health import apply_proxy_check, resolve_proxy_need

        need = resolve_proxy_need(
            total_accounts=total,
            concurrent=concurrent,
            proxy_count=len(proxies),
            proxy_mode=proxy_mode,
        )
        print(
            f"[PROXY-CHECK] need {need} good of {len(proxies)} listed  "
            f"(accounts={total if total > 0 else '∞'}, c={concurrent}, "
            f"max {float(getattr(args_ns, 'proxy_max_ms', None) or cfg.get('proxy_max_ms') or 4000):.0f}ms)",
            flush=True,
        )
        try:
            proxies = apply_proxy_check(
                proxies,
                enabled=True,
                target=str(
                    getattr(args_ns, "proxy_check_url", None)
                    or cfg.get("proxy_check_url")
                    or "https://accounts.x.ai/"
                ),
                max_ms=float(
                    getattr(args_ns, "proxy_max_ms", None)
                    or cfg.get("proxy_max_ms")
                    or 4000
                ),
                timeout=float(cfg.get("proxy_check_timeout") or 12),
                workers=int(cfg.get("proxy_check_workers") or 10),
                need=need,
                total_accounts=total,
                concurrent=concurrent,
                proxy_mode=proxy_mode,
                require_one=True,
            )
        except RuntimeError as e:
            print(f"[proxy-check] {e}", file=sys.stderr)
            return 2
        try:
            good_path = ROOT / "proxy.good.txt"
            good_path.write_text("\n".join(proxies) + "\n", encoding="utf-8")
            print(f"[PROXY-CHECK] kept {len(proxies)} → {good_path.name}", flush=True)
        except Exception:
            pass
    elif proxies and not do_check:
        print("[PROXY-CHECK] skipped (--no-proxy-check)", flush=True)

    if not SCRIPT.is_file():
        print(f"missing farm script: {SCRIPT}", file=sys.stderr)
        return 2

    event_q: queue.Queue = queue.Queue()
    state = PoolState(total=total, concurrent=concurrent, display=display, stagger=args_ns.stagger_sec)
    runner = PoolRunner(state, event_q)
    runner.build_plan(
        total, concurrent, display, args_ns.stagger_sec, proxies, proxy_mode=proxy_mode
    )

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
            # done = accounts finished (created/attempted); success/failed = pass outcome
            done = state.done
            success = state.success
            failed = state.failed
            pct = (done / tot * 100) if tot else 0
            # throughput: accounts finished per minute (done), not only passes
            rate_base = done if done > 0 else success
            rate = (rate_base / (elapsed / 60.0)) if elapsed >= 8 and rate_base > 0 else 0.0
            remaining = max(0, tot - done) if tot else 0
            eta_sec = (remaining / rate * 60.0) if rate > 0 and remaining > 0 else (
                0.0 if tot and remaining == 0 and done > 0 else None
            )

            head = Text()
            head.append(" Grok Farm ", style="bold white on dark_blue")
            head.append(f"  {display}  ", style="dim")
            head.append(f"elapsed {_fmt_dur(elapsed)}", style="cyan")
            if state.stopping:
                head.append("  STOPPING…", style="bold red")

            stats = Text("  ")
            pr = pass_rate(success, failed)
            stats.append(
                f"PASS {pr:.0f}%  " if pr is not None else "PASS —  ",
                style="bold cyan" if pr is not None else "dim",
            )
            if tot:
                stats.append(f"done {done}/{tot} ({pct:.0f}%)  ", style="bold")
            else:
                stats.append(f"done {done} ∞  ", style="bold")
            outcomes = {
                name: sum(w.outcomes[name] for w in state.workers.values())
                for name in ("usable", "inactive", "hard_failed")
            }
            stats.append(f"usable {outcomes['usable']}  ", style="bold green")
            stats.append(f"inactive {outcomes['inactive']}  ", style="yellow")
            stats.append(f"fail {failed}", style="bold red")

            rate_line = Text("  ")
            if rate > 0:
                rate_line.append(f"~{rate:.1f} acc/min  ", style="bold cyan")
            else:
                rate_line.append("acc/min …  ", style="dim")
            if not tot:
                rate_line.append("activity pulse · unlimited", style="dim")
            elif eta_sec is None:
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
            bar_text, determinate = progress_bar(done, tot, 40)
            bar.append(bar_text, style="green" if determinate and done else "cyan" if not determinate else "dim")
            if tot:
                bar.append(f"  {pct:.0f}%", style="dim")

            return Group(head, stats, rate_line, bar)

    class FarmApp(App):
        CSS = """
        Screen {
            layout: vertical;
        }
        #summary {
            height: 6;
            border: solid $accent;
            padding: 0 1;
        }
        #dashboard {
            height: 10;
            layout: horizontal;
        }
        #workers-pane {
            width: 3fr;
            min-width: 0;
        }
        #recent-pane {
            width: 2fr;
            min-width: 0;
        }
        #workers, #recent {
            height: 1fr;
        }
        #workers {
            border: solid $primary;
        }
        #recent {
            border: solid $secondary;
        }
        #log-tabs {
            height: 3;
        }
        #log {
            height: 1fr;
            min-height: 4;
            border: solid $surface;
            scrollbar-size: 1 1;
        }
        Screen.narrow #summary {
            height: 6;
        }
        Screen.narrow #dashboard {
            height: 6;
            layout: vertical;
        }
        Screen.narrow #workers-pane,
        Screen.narrow #recent-pane {
            width: 1fr;
            height: 1fr;
            min-height: 4;
        }
        Screen.narrow #recent-pane,
        Screen.narrow.show-recent #workers-pane {
            display: none;
        }
        Screen.narrow.show-recent #recent-pane {
            display: block;
        }
        """

        BINDINGS = [
            Binding("q", "quit_stop", "Quit & stop", priority=True),
            Binding("ctrl+c", "quit_stop", "Quit", show=False, priority=True),
            Binding("a", "filter_all", "All logs"),
            Binding("r", "toggle_recent", "Workers / Last 10"),
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

        def _apply_responsive_classes(self, width: int) -> None:
            """Use stable DOM class APIs rather than version-specific resize helpers."""
            narrow = width < 100
            screen = self.screen
            if narrow:
                screen.add_class("narrow")
            else:
                screen.remove_class("narrow", "show-recent")

        def on_resize(self, event) -> None:
            self._apply_responsive_classes(event.size.width)

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield SummaryPanel(id="summary")
            with Horizontal(id="dashboard"):
                with Vertical(id="workers-pane"):
                    yield DataTable(id="workers", zebra_stripes=True)
                with Vertical(id="recent-pane"):
                    yield DataTable(id="recent", zebra_stripes=True)
            yield Tabs(Tab("All", id="tab-all"), *(
                Tab(f"W{wid}", id=f"tab-w-{wid}")
                for wid in sorted(state.workers, key=lambda value: int(value) if value.isdigit() else value)
            ), id="log-tabs", active="tab-all")
            # auto_scroll=False — we own scroll via pause_scroll + write(scroll_end=…)
            yield RichLog(
                id="log",
                highlight=True,
                markup=True,
                wrap=True,
                max_lines=600,
                auto_scroll=False,
            )
            yield Footer()

        _queue_drain_deadline: float = 0.0

        def on_mount(self) -> None:
            self._apply_responsive_classes(self.size.width)
            table = self.query_one("#workers", DataTable)
            # (label, key) so update_cell keys stay stable
            table.add_columns(
                ("W", "w"),
                ("Status", "status"),
                ("Local", "local"),
                ("Global", "global"),
                ("Phase", "phase"),
                ("Success", "ok"),
                ("Failed", "fail"),
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

            recent = self.query_one("#recent", DataTable)
            recent.border_title = "Last 10 Accounts"
            recent.add_columns("Time", "Worker", "Email", "Status", "Duration")
            recent.cursor_type = "row"

            log = self.query_one("#log", RichLog)
            log.write(
                f"[bold]Grok Farm TUI[/]  total={state.total or '∞'}  "
                f"workers={len(state.workers)}  display={state.display}  "
                f"stagger={state.stagger}s"
            )
            log.write("[dim]keys: q=stop · r=workers/Last 10 · a=all logs · 1-9=worker · p=pause[/]")

            self.set_interval(0.25, self._drain_events)
            self.set_interval(1.0, self._refresh_summary)
            # start workers after UI is up
            runner.start_all(sys.executable)

        def _refresh_summary(self) -> None:
            self.query_one("#summary", SummaryPanel).refresh()
            self._refresh_table()
            now = time.time()
            default_limit = float(os.environ.get("GROK_PHASE_WATCHDOG_SEC", "") or 0)
            if default_limit <= 0:
                try:
                    full = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                    farm = full.get("farm") if isinstance(full.get("farm"), dict) else {}
                    default_limit = float(farm.get("phase_watchdog_sec") or 180)
                except Exception:
                    default_limit = 180.0
            for worker in state.workers.values():
                if worker.status != "running" or not worker.phase_started_at:
                    continue
                key = "GROK_PHASE_WATCHDOG_" + re.sub(r"[^A-Z0-9]", "_", worker.phase.upper()) + "_SEC"
                limit = float(os.environ.get(key, default_limit) or default_limit)
                elapsed = now - worker.phase_started_at
                if limit > 0 and elapsed >= limit and worker.watchdog_warned_phase != worker.phase:
                    worker.watchdog_warned_phase = worker.phase
                    event_q.put(("log", LogLine(
                        ts=time.strftime("%H:%M:%S"), wid=worker.wid, phase="WATCHDOG",
                        message=f"{worker.phase} running {elapsed:.0f}s (limit {limit:.0f}s); worker left alive",
                        raw="", level="warn",
                    )))
            # auto-exit when all workers finished and not stopping mid-way
            if state.started_at and not state.stopping:
                if state.workers and all(
                    w.status in ("done", "dead") and w.proc is not None
                    for w in state.workers.values()
                ):
                    # allow a beat for final logs
                    if all(w.proc and w.proc.poll() is not None for w in state.workers.values()):
                        self._exit_code = 0 if state.fail == 0 else 1
                        counts = runner.queue_counts()
                        active = -1 if counts is None else sum(counts.get(name, 0) for name in
                            ("pending", "claimed", "terminal_pending", "finalizing"))
                        if runner.probe_worker_unhealthy:
                            self._exit_code = 1
                        if active != 0:
                            if not self._queue_drain_deadline:
                                drain_raw = (os.environ.get("GROK_ASYNC_DRAIN_TIMEOUT_SEC") or "").strip()
                                if drain_raw:
                                    timeout = float(drain_raw)
                                else:
                                    try:
                                        full = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
                                        gcli = full.get("grok_cli") if isinstance(full.get("grok_cli"), dict) else {}
                                        timeout = float(gcli.get("async_drain_timeout_sec") or 120)
                                    except Exception:
                                        timeout = 120.0
                                self._queue_drain_deadline = now + max(0.0, timeout)
                                event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "POOL",
                                    ("queue state unknown" if active < 0 else f"waiting for {active} durable probe job(s)") +
                                    f", timeout={timeout:g}s", "", "warn" if active < 0 else "info")))
                            elif now >= self._queue_drain_deadline:
                                event_q.put(("log", LogLine(time.strftime("%H:%M:%S"), "pool", "FAIL",
                                    f"probe drain timeout; {active} durable job(s) remain", "", "error")))
                                self._exit_code = 1
                                self.action_quit_stop()
                        else:
                            self.set_timer(1.0, self.action_quit_stop)

        def _refresh_table(self) -> None:
            table = self.query_one("#workers", DataTable)
            for wid, w in state.workers.items():
                row_key = wid
                try:
                    share_s = f"{w.local_cur}/{w.share}" if w.share else f"{w.local_cur}/∞"
                    gidx = w.offset + w.local_cur if w.local_cur else (
                        w.offset + 1 if state.total > 0 else 0
                    )
                    if state.total > 0:
                        g_s = f"#{gidx}/{state.total}"
                    elif w.local_cur:
                        g_s = f"#{w.local_cur}"
                    else:
                        g_s = "∞"
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
                    table.update_cell(
                        row_key,
                        "ok",
                        Text(str(w.ok), style="bold green" if w.ok else "dim"),
                    )
                    table.update_cell(
                        row_key,
                        "fail",
                        Text(str(w.fail), style="bold red" if w.fail else "dim"),
                    )
                    table.update_cell(row_key, "email", (w.email or "")[:28])
                    last_msg = w.message or ""
                    last_style = ""
                    if last_msg.startswith("✓") or last_msg.upper().startswith("PASS"):
                        last_style = "green"
                    elif last_msg.startswith("✗") or "FAIL" in last_msg.upper()[:8]:
                        last_style = "red"
                    table.update_cell(
                        row_key,
                        "last",
                        Text(last_msg[:40], style=last_style) if last_style else last_msg[:40],
                    )
                except Exception:
                    pass

        def _render_log_line(self, log: LogLine) -> Text:
            style = PHASE_STYLE.get(log.phase, "white")
            msg_u = (log.message or "").upper()
            if log.phase in ("OK", "DONE", "CREATED") or (log.phase == "RESULT" and "PASS" in msg_u):
                style = "bold green"
            elif log.level == "error" or log.phase in ("FAIL", "STOP") or (log.phase == "RESULT" and "FAIL" in msg_u):
                style = "bold red"
            elif log.level == "warn":
                style = "yellow"
            wid_s = f"W{log.wid}" if log.wid not in ("pool", "?") else log.wid
            line = Text()
            line.append(f"{log.ts} ", style="dim")
            line.append(f"{wid_s:<4} ", style="bold cyan" if log.wid != "pool" else "white")
            line.append(f"{log.phase:<12} ", style=style)
            line.append(log.message[:140], style=style if style in ("bold green", "bold red", "yellow") or log.level != "info" else "")
            return line

        def _rerender_logs(self) -> None:
            widget = self.query_one("#log", RichLog)
            widget.clear()
            for item in state.logs:
                if log_matches_filter(item, self.filter_wid):
                    widget.write(self._render_log_line(item), scroll_end=False)
            if not self.pause_scroll:
                widget.scroll_end(animate=False)

        def _refresh_recent(self) -> None:
            table = self.query_one("#recent", DataTable)
            table.clear(columns=False)
            for number, account in enumerate(reversed(state.accounts)):
                status_style = {
                    "PASS": "bold green",
                    "USABLE": "bold green",
                    "INACTIVE": "bold yellow",
                    "FAIL": "bold red",
                }.get(account.status, "bold red")
                table.add_row(account.ts, f"W{account.wid}", account.email[:34],
                    Text(account.status, style=status_style), _fmt_dur(account.duration or 0),
                    key=f"recent-{number}")

        def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
            tab_id = event.tab.id or "tab-all"
            requested = tab_id[6:] if tab_id.startswith("tab-w-") else None
            self.filter_wid = select_log_filter(requested, state.workers)
            self._rerender_logs()

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
                    event_accepted = accept_structured_event(state, log)
                    if log.wid != "pool":
                        apply_log_to_worker(state, log, event_accepted=event_accepted)
                    state.logs.append(log)
                    if len(state.logs) > state.max_logs:
                        state.logs = state.logs[-state.max_logs :]

                    if event_accepted and capture_account(state, log):
                        self._refresh_recent()
                    if log_matches_filter(log, self.filter_wid):
                        log_w.write(self._render_log_line(log), scroll_end=not self.pause_scroll)

                elif kind == "worker_exit":
                    self._refresh_table()

        def action_filter_all(self) -> None:
            self.filter_wid = None
            self.query_one("#log-tabs", Tabs).active = "tab-all"
            self._rerender_logs()

        def action_filter_w(self, wid: str) -> None:
            selected = select_log_filter(wid, state.workers)
            if selected is not None:
                self.filter_wid = selected
                self.query_one("#log-tabs", Tabs).active = f"tab-w-{selected}"
                self._rerender_logs()

        def action_toggle_recent(self) -> None:
            if "narrow" not in self.screen.classes:
                return
            self.screen.toggle_class("show-recent")
            showing_recent = "show-recent" in self.screen.classes
            self.notify(
                "Dashboard: Last 10 Accounts" if showing_recent else "Dashboard: Workers",
                timeout=1.5,
            )

        def action_toggle_pause(self) -> None:
            self.pause_scroll = not self.pause_scroll
            log_w = self.query_one("#log", RichLog)
            # belt-and-suspenders vs RichLog default auto_scroll
            log_w.auto_scroll = not self.pause_scroll
            if not self.pause_scroll:
                log_w.scroll_end(animate=False)
            log_w.write(
                f"[dim]auto-scroll {'paused' if self.pause_scroll else 'resumed'}[/]",
                scroll_end=not self.pause_scroll,
            )

        def action_quit_stop(self) -> None:
            if not state.stopping:
                state.stopping = True
                try:
                    self.query_one("#log", RichLog).write(
                        "[bold red]stopping all workers + Chromium…[/]",
                        scroll_end=True,
                    )
                except Exception:
                    pass
                # Synchronous: wait until process groups + CDP ports are dead
                runner.stop_all()
                try:
                    self.query_one("#log", RichLog).write(
                        "[bold green]Chrome closed. Bye.[/]",
                        scroll_end=True,
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
    p.add_argument(
        "--proxy-file",
        default=cfg.get("proxy_file") or "",
        help="proxy list (URL or Webshare host:port:user:pass)",
    )
    p.add_argument("--proxy", action="append", default=[], help="proxy URL (repeatable)")
    p.add_argument(
        "--proxy-mode",
        choices=["per_account", "per_worker"],
        default=cfg.get("proxy_mode") or "per_account",
        help="per_account=rotate each account; per_worker=sticky",
    )
    p.add_argument(
        "--proxy-check",
        action=argparse.BooleanOptionalAction,
        default=bool(cfg.get("proxy_check", True)),
        help="probe proxies → accounts.x.ai; drop slow ones (default on)",
    )
    p.add_argument(
        "--proxy-max-ms",
        type=float,
        default=float(cfg.get("proxy_max_ms") or 4000),
        help="max latency ms to keep a proxy (default 4000)",
    )
    p.add_argument(
        "--proxy-check-url",
        default=str(cfg.get("proxy_check_url") or "https://accounts.x.ai/"),
        help="health-check URL",
    )
    p.add_argument(
        "--display",
        choices=["headed", "offscreen", "headless", "virtual"],
        default=None,
        help=(
            "headed | offscreen (Mac) | headless (Linux flash) | virtual (Xvfb). "
            "Default: config → env → platform"
        ),
    )
    p.add_argument("--headless", action="store_true", help="shortcut → headless")
    p.add_argument("--offscreen", action="store_true", help="shortcut → offscreen")
    p.add_argument("--virtual", action="store_true", help="shortcut → virtual (Xvfb)")
    p.add_argument("--headed", action="store_true", help="shortcut → headed (debug)")
    return p


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_tui(args)


if __name__ == "__main__":
    raise SystemExit(main())

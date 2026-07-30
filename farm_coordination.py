"""Cross-process coordination for farm workers (stdlib only).

State is guarded by an OS file lock and updated atomically.  Workers may import
this module directly or discover the shared paths through the GROK_* env vars
set by farm_tui.py.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

DEFAULT_STATE_PATH = Path(os.environ.get("GROK_COORD_STATE_PATH", tempfile.gettempdir() + "/grok-farm-coordination.json"))
DEFAULT_LOCK_PATH = Path(os.environ.get("GROK_COORD_LOCK_PATH", str(DEFAULT_STATE_PATH) + ".lock"))


@contextmanager
def _locked(lock_path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def snapshot(state_path: Path = DEFAULT_STATE_PATH, lock_path: Path = DEFAULT_LOCK_PATH) -> dict[str, Any]:
    with _locked(lock_path):
        return _read(state_path)


def acquire_rate_gate(category: str, min_interval: float, *, now: float | None = None,
                      state_path: Path = DEFAULT_STATE_PATH, lock_path: Path = DEFAULT_LOCK_PATH) -> float:
    """Reserve the next signup/OAuth slot and return seconds the caller should wait."""
    current = time.time() if now is None else now
    with _locked(lock_path):
        state = _read(state_path)
        gates = state.setdefault("rate_gates", {})
        next_at = float(gates.get(category, 0.0) or 0.0)
        slot = max(current, next_at)
        gates[category] = slot + max(0.0, float(min_interval))
        _write(state_path, state)
    return max(0.0, slot - current)


def set_global_cooldown(seconds: float, *, reason: str = "", now: float | None = None,
                        state_path: Path = DEFAULT_STATE_PATH, lock_path: Path = DEFAULT_LOCK_PATH) -> float:
    """Extend (never shorten) the global cooldown and return its expiry epoch."""
    current = time.time() if now is None else now
    with _locked(lock_path):
        state = _read(state_path)
        old = float(state.get("cooldown_until", 0.0) or 0.0)
        state["cooldown_until"] = max(old, current + max(0.0, float(seconds)))
        if reason:
            state["cooldown_reason"] = reason
        _write(state_path, state)
        return float(state["cooldown_until"])


def cooldown_remaining(*, now: float | None = None, state_path: Path = DEFAULT_STATE_PATH,
                       lock_path: Path = DEFAULT_LOCK_PATH) -> float:
    current = time.time() if now is None else now
    return max(0.0, float(snapshot(state_path, lock_path).get("cooldown_until", 0.0) or 0.0) - current)


def circuit_allow(category: str, *, now: float | None = None, state_path: Path = DEFAULT_STATE_PATH,
                  lock_path: Path = DEFAULT_LOCK_PATH) -> bool:
    current = time.time() if now is None else now
    data = snapshot(state_path, lock_path).get("circuits", {}).get(category, {})
    return current >= float(data.get("open_until", 0.0) or 0.0)


def circuit_record(category: str, success: bool, *, threshold: int = 3, cooldown: float = 60.0,
                   now: float | None = None, state_path: Path = DEFAULT_STATE_PATH,
                   lock_path: Path = DEFAULT_LOCK_PATH, **_ignored: Any) -> dict[str, Any]:
    """Record a categorized result; consecutive failures open only that category."""
    current = time.time() if now is None else now
    with _locked(lock_path):
        state = _read(state_path)
        circuits = state.setdefault("circuits", {})
        item = circuits.setdefault(category, {})
        failures = 0 if success else int(item.get("failures", 0) or 0) + 1
        item.update({"failures": failures, "updated_at": current})
        if success:
            item["open_until"] = 0.0
        elif failures >= max(1, int(threshold)):
            item["open_until"] = max(float(item.get("open_until", 0.0) or 0.0), current + max(0.0, cooldown))
        _write(state_path, state)
        return dict(item)


def wait_rate_gate(category: str, min_interval: float, **kwargs: Any) -> float:
    """Reserve and synchronously wait for a cross-process rate slot."""
    delay = acquire_rate_gate(category, min_interval, **kwargs)
    if delay > 0:
        time.sleep(delay)
    return delay


def adaptive_cooldown(category: str, reason: str = "", *, severity: str = "failure",
                      worker_id: str = "", **kwargs: Any) -> dict[str, Any]:
    """P1-compatible failure recorder; defaults only open a categorized circuit."""
    del reason, severity, worker_id
    return circuit_record(category, False, **kwargs)


request_cooldown = adaptive_cooldown


def record_failure(category: str, reason: str = "", **kwargs: Any) -> dict[str, Any]:
    return adaptive_cooldown(category, reason, **kwargs)

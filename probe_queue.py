"""Durable, multi-process-safe queue for probe and push work.

Payloads are UTF-8 JSON objects with this explicit envelope::

    {"version": 1, "operation": "probe" | "push", "data": {...}}

``data`` is deliberately application-defined. The queue never pickles objects,
infers token formats, or writes payloads to logs. Callers that put credentials in
``data`` should treat the SQLite database as a secret-bearing file.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

LOG = logging.getLogger("probe_queue")
_SCHEMA_VERSION = 1
_OPERATIONS = frozenset(("probe", "push"))


@dataclass(frozen=True)
class Job:
    id: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    claimed_by: str
    claimed_at: float


class ProbeQueue:
    """SQLite-backed at-least-once queue; safe for concurrent processes."""

    def __init__(self, path: os.PathLike[str] | str, *, timeout: float = 30.0):
        self.path = Path(path).expanduser()
        self.timeout = float(timeout)
        self._prepare_storage()
        self._initialize()

    def _prepare_storage(self) -> None:
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not parent_existed:
            os.chmod(self.path.parent, 0o700)

    @contextmanager
    def _connect(self):
        old_umask = os.umask(0o077)
        try:
            conn = sqlite3.connect(
                str(self.path), timeout=self.timeout, isolation_level=None
            )
        finally:
            os.umask(old_umask)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = %d" % int(self.timeout * 1000))
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            if self.path.exists():
                os.chmod(self.path, 0o600)
            yield conn
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connect() as conn:
            existing = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
            if existing and "terminal_pending" not in str(existing["sql"]):
                conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN
                        ('pending','claimed','done','failed','terminal_pending','finalizing','dead')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    available_at REAL NOT NULL,
                    claimed_at REAL,
                    claimed_by TEXT,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            if existing and "terminal_pending" not in str(existing["sql"]):
                conn.execute("INSERT INTO jobs SELECT * FROM jobs_legacy")
                conn.execute("DROP TABLE jobs_legacy")
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            if "delivery_meta" not in columns:
                conn.execute("ALTER TABLE jobs ADD COLUMN delivery_meta TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS jobs_ready "
                "ON jobs(status, available_at, created_at)"
            )

    @staticmethod
    def validate_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        clean = dict(payload)
        if set(clean) != {"version", "operation", "data"}:
            raise ValueError("payload keys must be exactly: version, operation, data")
        if clean["version"] != _SCHEMA_VERSION:
            raise ValueError("payload version must be 1")
        if clean["operation"] not in _OPERATIONS:
            raise ValueError("operation must be 'probe' or 'push'")
        if not isinstance(clean["data"], dict):
            raise ValueError("payload data must be a JSON object")
        try:
            encoded = json.dumps(clean, separators=(",", ":"), ensure_ascii=False)
            decoded = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must contain only JSON-serializable values") from exc
        return decoded

    def enqueue(
        self,
        payload: Mapping[str, Any],
        *,
        max_attempts: int = 3,
        available_at: Optional[float] = None,
        job_id: Optional[str] = None,
    ) -> str:
        clean = self.validate_payload(payload)
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        now = time.time()
        identifier = job_id or uuid.uuid4().hex
        encoded = json.dumps(clean, separators=(",", ":"), ensure_ascii=False)
        with self._connect() as conn:
            changed = conn.execute(
                "INSERT OR IGNORE INTO jobs(id,payload,status,attempts,max_attempts,"
                "available_at,created_at,updated_at) VALUES(?,?,'pending',0,?,?,?,?)",
                (identifier, encoded, int(max_attempts),
                 now if available_at is None else float(available_at), now, now),
            ).rowcount
            if changed == 0:
                existing = conn.execute("SELECT payload FROM jobs WHERE id=?", (identifier,)).fetchone()
                if existing is None or existing["payload"] != encoded:
                    raise ValueError("job_id already exists with a different payload")
        LOG.info("%s job id=%s operation=%s", "enqueued" if changed else "existing", identifier, clean["operation"])
        return identifier

    def claim(self, worker_id: str, *, now: Optional[float] = None) -> Optional[Job]:
        if not worker_id.strip():
            raise ValueError("worker_id is required")
        timestamp = time.time() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT id FROM jobs WHERE status='pending' AND available_at<=? "
                "ORDER BY available_at,created_at LIMIT 1", (timestamp,)
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            changed = conn.execute(
                "UPDATE jobs SET status='claimed',claimed_by=?,claimed_at=?,"
                "attempts=attempts+1,updated_at=? WHERE id=? AND status='pending'",
                (worker_id, timestamp, timestamp, row["id"]),
            ).rowcount
            if changed != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            conn.execute("COMMIT")
        return Job(claimed["id"], json.loads(claimed["payload"]), claimed["attempts"],
                   claimed["max_attempts"], claimed["claimed_by"], claimed["claimed_at"])

    def record_delivery_meta(self, job: Job, metadata: Mapping[str, Any]) -> bool:
        """Persist sanitized post-side-effect facts before application bookkeeping."""
        encoded = json.dumps(dict(metadata), separators=(",", ":"), ensure_ascii=False)
        with self._connect() as conn:
            return conn.execute("UPDATE jobs SET delivery_meta=?,updated_at=? WHERE id=? AND status='claimed' "
                                "AND claimed_by=? AND claimed_at=?",
                                (encoded, time.time(), job.id, job.claimed_by, job.claimed_at)).rowcount == 1

    def delivery_meta(self, job_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT delivery_meta FROM jobs WHERE id=?", (job_id,)).fetchone()
        try:
            return json.loads(row["delivery_meta"]) if row and row["delivery_meta"] else {}
        except ValueError:
            return {}

    def ack(self, job: Job) -> bool:
        return self._finish(job, "done", None, None)

    def fail(self, job: Job, error: str, *, retry_delay: float = 0.0) -> bool:
        retry = job.attempts < job.max_attempts
        status = "pending" if retry else "failed"
        available = time.time() + max(0.0, float(retry_delay)) if retry else None
        return self._finish(job, status, self._safe_error(error), available)

    def _finish(self, job: Job, status: str, error: Optional[str], available: Optional[float]) -> bool:
        now = time.time()
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE jobs SET status=?,available_at=COALESCE(?,available_at),"
                "claimed_at=NULL,claimed_by=NULL,last_error=?,updated_at=? "
                "WHERE id=? AND status='claimed' AND claimed_by=? AND claimed_at=?",
                (status, available, error, now, job.id, job.claimed_by, job.claimed_at),
            ).rowcount
        return changed == 1

    def recover_stale(self, stale_after: float, *, now: Optional[float] = None,
                      terminal_handler: Optional[Callable[[dict[str, Any], str], Any]] = None,
                      terminal_retry_delay: float = 5.0) -> int:
        if stale_after < 0:
            raise ValueError("stale_after cannot be negative")
        timestamp = time.time() if now is None else float(now)
        cutoff = timestamp - stale_after
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            terminal_rows = conn.execute(
                "SELECT id,payload FROM jobs WHERE status='claimed' AND claimed_at<=? "
                "AND attempts>=max_attempts", (cutoff,)
            ).fetchall()
            # A crashed terminal callback leaves a finalizing lease. Recover it
            # only after its claimed_at lease expires, and keep it strictly in
            # the bookkeeping lane so claim() can never invoke external work.
            recovered_finalizing = conn.execute(
                "UPDATE jobs SET status='terminal_pending',available_at=?,claimed_at=NULL,"
                "claimed_by=NULL,last_error='stale terminal bookkeeping lease recovered',updated_at=? "
                "WHERE status='finalizing' AND claimed_at<=?",
                (timestamp + max(0.0, float(terminal_retry_delay)), timestamp, cutoff),
            ).rowcount
            # Final attempts enter a distinct bookkeeping-only lane. Normal
            # claim() can never redeliver them to the external handler.
            terminal = conn.execute(
                "UPDATE jobs SET status='terminal_pending',available_at=?,claimed_at=NULL,claimed_by=NULL,"
                "last_error='finalize stale terminal delivery',updated_at=? "
                "WHERE status='claimed' AND claimed_at<=? AND attempts>=max_attempts",
                (timestamp, timestamp, cutoff),
            ).rowcount
            pending = conn.execute(
                "UPDATE jobs SET status='pending',available_at=?,claimed_at=NULL,"
                "claimed_by=NULL,last_error='stale claim recovered',updated_at=? "
                "WHERE status='claimed' AND claimed_at<=? AND attempts<max_attempts",
                (timestamp, timestamp, cutoff),
            ).rowcount
            conn.execute("COMMIT")
        if terminal_handler:
            for row in terminal_rows:
                self.finalize_terminal(str(row["id"]), terminal_handler,
                                       reason="claim expired after final attempt")
        return terminal + pending + recovered_finalizing

    def finalize_terminal(self, job_id: str, handler: Callable[[dict[str, Any], str], Any],
                          *, reason: str, retry_delay: float = 5.0) -> bool:
        """Lease one dead-letter bookkeeping task; never invoke the normal handler."""
        owner = f"terminal-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        now = time.time()
        with self._connect() as conn:
            changed = conn.execute(
                "UPDATE jobs SET status='finalizing',claimed_by=?,claimed_at=?,updated_at=? "
                "WHERE id=? AND status='terminal_pending' AND available_at<=?",
                (owner, now, now, job_id, now),
            ).rowcount
            if changed != 1:
                return False
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        try:
            handler(json.loads(row["payload"]), reason)
        except Exception as exc:
            with self._connect() as conn:
                conn.execute("UPDATE jobs SET status='terminal_pending',available_at=?,claimed_by=NULL,"
                             "claimed_at=NULL,last_error=?,updated_at=? WHERE id=? AND status='finalizing' "
                             "AND claimed_by=?", (time.time() + max(0, retry_delay), self._safe_error(exc),
                                                  time.time(), job_id, owner))
            return False
        with self._connect() as conn:
            conn.execute("UPDATE jobs SET status='dead',claimed_by=NULL,claimed_at=NULL,last_error=?,"
                         "updated_at=? WHERE id=? AND status='finalizing' AND claimed_by=?",
                         (self._safe_error(reason), time.time(), job_id, owner))
        return True

    def finalize_ready_terminals(self, handler: Callable[[dict[str, Any], str], Any]) -> int:
        with self._connect() as conn:
            rows = conn.execute("SELECT id,last_error FROM jobs WHERE status='terminal_pending' "
                                "AND available_at<=?", (time.time(),)).fetchall()
        return sum(self.finalize_terminal(str(row["id"]), handler,
                                          reason=str(row["last_error"] or "terminal delivery"))
                   for row in rows)

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status,COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        result = {name: 0 for name in ("pending", "claimed", "done", "failed", "terminal_pending", "finalizing", "dead")}
        result.update({str(row["status"]): int(row["n"]) for row in rows})
        return result

    @staticmethod
    def _safe_error(error: str) -> str:
        # Error text may still be persisted for diagnosis, but never logged. Keep it bounded.
        return str(error).replace("\x00", "")[:1000]

    def status(self, job_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        return None if row is None else str(row["status"])


def load_handler(spec: str) -> Callable[[dict[str, Any]], Any]:
    """Load a ``module:callable`` handler. No payload-driven imports are allowed."""
    if ":" not in spec:
        raise ValueError("handler must use module:callable syntax")
    module_name, attribute = spec.split(":", 1)
    handler = getattr(importlib.import_module(module_name), attribute)
    if not callable(handler):
        raise TypeError("imported handler is not callable")
    return handler


def run_worker(
    queue: ProbeQueue,
    handler: Callable[[dict[str, Any]], Any],
    *,
    worker_id: Optional[str] = None,
    poll_interval: float = 1.0,
    stale_after: float = 300.0,
    retry_delay: float = 5.0,
    once: bool = False,
    terminal_handler: Optional[Callable[[dict[str, Any], str], Any]] = None,
) -> int:
    """Process jobs until interrupted, or at most one job when ``once`` is true."""
    identity = worker_id or f"pid-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    processed = 0
    while True:
        queue.recover_stale(stale_after, terminal_handler=terminal_handler)
        if terminal_handler:
            queue.finalize_ready_terminals(terminal_handler)
        job = queue.claim(identity)
        if job is None:
            if once:
                return processed
            time.sleep(max(0.05, poll_interval))
            continue
        LOG.info("processing job id=%s operation=%s attempt=%d/%d",
                 job.id, job.payload["operation"], job.attempts, job.max_attempts)
        try:
            # Trusted handlers may use these non-secret delivery facts to defer
            # terminal ledger updates until retries are exhausted.
            old_attempt = os.environ.get("GROK_PROBE_JOB_ATTEMPT")
            old_max = os.environ.get("GROK_PROBE_JOB_MAX_ATTEMPTS")
            old_id = os.environ.get("GROK_PROBE_JOB_ID")
            old_db = os.environ.get("GROK_PROBE_QUEUE_PATH")
            os.environ["GROK_PROBE_JOB_ATTEMPT"] = str(job.attempts)
            os.environ["GROK_PROBE_JOB_MAX_ATTEMPTS"] = str(job.max_attempts)
            os.environ["GROK_PROBE_JOB_ID"] = job.id
            os.environ["GROK_PROBE_QUEUE_PATH"] = str(queue.path)
            try:
                handler(job.payload)
            finally:
                if old_attempt is None:
                    os.environ.pop("GROK_PROBE_JOB_ATTEMPT", None)
                else:
                    os.environ["GROK_PROBE_JOB_ATTEMPT"] = old_attempt
                if old_max is None:
                    os.environ.pop("GROK_PROBE_JOB_MAX_ATTEMPTS", None)
                else:
                    os.environ["GROK_PROBE_JOB_MAX_ATTEMPTS"] = old_max
                for key, old in (("GROK_PROBE_JOB_ID", old_id), ("GROK_PROBE_QUEUE_PATH", old_db)):
                    if old is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = old
        except Exception as exc:  # handler boundary: retry any failure
            queue.fail(job, f"{type(exc).__name__}: {exc}", retry_delay=retry_delay)
            LOG.warning("job failed id=%s attempt=%d/%d error_type=%s",
                        job.id, job.attempts, job.max_attempts, type(exc).__name__)
        else:
            queue.ack(job)
            LOG.info("job completed id=%s", job.id)
        processed += 1
        if once:
            return processed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a durable probe/push queue worker")
    parser.add_argument("--db", required=True, help="SQLite queue path (may contain secrets)")
    parser.add_argument("--handler", required=True, help="trusted module:callable accepting payload")
    parser.add_argument("--worker-id")
    parser.add_argument("--terminal-handler", help="trusted module:callable(payload, reason)")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--stale-after", type=float, default=300.0)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    handler = load_handler(args.handler)
    terminal_handler = load_handler(args.terminal_handler) if args.terminal_handler else None
    run_worker(ProbeQueue(args.db), handler, worker_id=args.worker_id,
               poll_interval=args.poll_interval, stale_after=args.stale_after,
               retry_delay=args.retry_delay, once=args.once,
               terminal_handler=terminal_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

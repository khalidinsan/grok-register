import logging
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path

from probe_queue import ProbeQueue, run_worker
from build_oauth_pkce import BuildTokens
from probe_job_handler import make_payload, tokens_from_payload


def _claim_one(path, output):
    job = ProbeQueue(path).claim(f"worker-{os.getpid()}")
    output.put(None if job is None else job.id)


class ProbeQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "queue.sqlite3"
        self.queue = ProbeQueue(self.path)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def payload(operation="probe", **data):
        return {"version": 1, "operation": operation, "data": data}

    def test_enqueue_claim_ack_and_permissions(self):
        job_id = self.queue.enqueue(self.payload(account_id="opaque"))
        job = self.queue.claim("worker-a")
        self.assertEqual(job_id, job.id)
        self.assertEqual(job.payload["data"], {"account_id": "opaque"})
        self.assertTrue(self.queue.ack(job))
        self.assertEqual("done", self.queue.status(job_id))
        self.assertEqual(0o600, stat.S_IMODE(self.path.stat().st_mode))
        self.assertEqual(0o700, stat.S_IMODE(self.path.parent.stat().st_mode))

    def test_retry_then_terminal_failure(self):
        job_id = self.queue.enqueue(self.payload(), max_attempts=2)
        first = self.queue.claim("worker")
        self.assertTrue(self.queue.fail(first, "secret-value", retry_delay=0))
        self.assertEqual("pending", self.queue.status(job_id))
        second = self.queue.claim("worker")
        self.assertEqual(2, second.attempts)
        self.assertTrue(self.queue.fail(second, "again"))
        self.assertEqual("failed", self.queue.status(job_id))

    def test_stale_claim_recovery_and_old_owner_cannot_ack(self):
        job_id = self.queue.enqueue(self.payload(), max_attempts=2, available_at=0)
        old = self.queue.claim("crashed", now=10)
        self.assertEqual(1, self.queue.recover_stale(20, now=31))
        new = self.queue.claim("replacement", now=31)
        self.assertEqual(job_id, new.id)
        self.assertFalse(self.queue.ack(old))
        self.assertTrue(self.queue.ack(new))

    def test_stale_final_attempt_gets_terminal_callback_and_redelivery(self):
        job_id = self.queue.enqueue(self.payload(), max_attempts=1, available_at=0)
        self.queue.claim("crashed", now=10)
        seen = []
        self.assertEqual(1, self.queue.recover_stale(20, now=31,
            terminal_handler=lambda payload, reason: seen.append((payload, reason))))
        self.assertEqual("dead", self.queue.status(job_id))
        self.assertEqual("claim expired after final attempt", seen[0][1])
        self.assertIsNone(self.queue.claim("normal-worker", now=32))

    def test_terminal_callback_failure_stays_bookkeeping_only(self):
        job_id = self.queue.enqueue(self.payload(), max_attempts=1, available_at=0)
        self.queue.claim("crashed", now=10)
        calls = []
        def fail_terminal(payload, reason):
            calls.append(reason)
            raise RuntimeError("bookkeeping unavailable")
        self.queue.recover_stale(20, now=31, terminal_handler=fail_terminal)
        self.assertEqual("terminal_pending", self.queue.status(job_id))
        self.assertIsNone(self.queue.claim("normal-worker", now=100))
        self.assertEqual(1, len(calls))

    def test_stale_finalizing_lease_recovers_to_bookkeeping_only(self):
        job_id = self.queue.enqueue(self.payload(), max_attempts=1, available_at=0)
        self.queue.claim("external-crashed", now=10)
        self.queue.recover_stale(20, now=31)
        # Simulate a terminal worker crash after acquiring its lease.
        with self.queue._connect() as conn:
            conn.execute("UPDATE jobs SET status='finalizing',claimed_by='terminal-crashed',"
                         "claimed_at=40,available_at=40 WHERE id=?", (job_id,))
        self.assertEqual(1, self.queue.recover_stale(20, now=61, terminal_retry_delay=5))
        self.assertEqual("terminal_pending", self.queue.status(job_id))
        self.assertIsNone(self.queue.claim("normal-worker", now=100))
        seen = []
        with self.queue._connect() as conn:
            row = conn.execute("SELECT available_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(66, row["available_at"])
            conn.execute("UPDATE jobs SET available_at=0 WHERE id=?", (job_id,))
        self.assertTrue(self.queue.finalize_terminal(job_id,
            lambda payload, reason: seen.append(reason), reason="recovered"))
        self.assertEqual(["recovered"], seen)
        self.assertEqual("dead", self.queue.status(job_id))
        self.assertIsNone(self.queue.claim("normal-worker", now=200))

    def test_multiprocess_claim_is_exclusive(self):
        job_id = self.queue.enqueue(self.payload())
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        workers = [context.Process(target=_claim_one, args=(self.path, output)) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(10)
            self.assertEqual(0, worker.exitcode)
        claims = [output.get(timeout=2) for _ in workers]
        self.assertEqual([job_id], [claim for claim in claims if claim is not None])

    def test_worker_api_and_redacted_logs(self):
        secret = "do-not-log-this-token"
        job_id = self.queue.enqueue(self.payload("push", token=secret))
        seen = []
        with self.assertLogs("probe_queue", logging.INFO) as logs:
            run_worker(self.queue, lambda payload: seen.append(payload), once=True)
        self.assertEqual("done", self.queue.status(job_id))
        self.assertEqual(secret, seen[0]["data"]["token"])
        self.assertNotIn(secret, "\n".join(logs.output))

    def test_idempotent_enqueue_same_job_id(self):
        payload = self.payload(account_id="opaque")
        first = self.queue.enqueue(payload, job_id="stable")
        second = self.queue.enqueue(payload, job_id="stable")
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            self.queue.enqueue(self.payload(account_id="different"), job_id="stable")

    def test_build_tokens_full_round_trip(self):
        original = BuildTokens(
            access_token="access", refresh_token="refresh", id_token="id",
            expires_at="2030-01-01", expires_in=123, email="a@example.com",
            user_id="u", team_id="t", name="A", scope="scope", referrer="ref",
            bot_flag_source={"source": "test"}, jwt_claims={"sub": "u"}, auth_mode="oidc_pkce",
        )
        payload = make_payload({"email": original.email}, original, job_id="job")
        rebuilt = tokens_from_payload(payload)
        self.assertEqual(original, rebuilt)

    def test_counts_lifecycle(self):
        job_id = self.queue.enqueue(self.payload())
        self.assertEqual(1, self.queue.counts()["pending"])
        job = self.queue.claim("worker")
        self.assertEqual(1, self.queue.counts()["claimed"])
        self.queue.ack(job)
        self.assertEqual("done", self.queue.status(job_id))
        self.assertEqual(0, self.queue.counts()["pending"] + self.queue.counts()["claimed"])

    def test_payload_schema_rejects_implicit_or_non_json_tokens(self):
        invalid = [
            {"operation": "probe", "data": {}},
            {"version": 1, "operation": "delete", "data": {}},
            {"version": 1, "operation": "push", "data": b"token"},
            {"version": 1, "operation": "push", "data": {"token": object()}},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    self.queue.enqueue(payload)


if __name__ == "__main__":
    unittest.main()

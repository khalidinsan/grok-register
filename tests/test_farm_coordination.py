import ast
import asyncio
import io
import json
import logging
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from farm_coordination import (
    acquire_rate_gate, circuit_allow, circuit_record, cooldown_remaining,
    set_global_cooldown,
)
import farm_tui
from farm_tui import (
    PoolState, WorkerState, apply_log_to_worker, capture_account, pass_rate,
    parse_slog_line, progress_bar, select_log_filter,
)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state.json"
        self.lock = root / "state.lock"

    def tearDown(self):
        self.temp.cleanup()

    def test_rate_gate_reserves_serial_slots(self):
        self.assertEqual(acquire_rate_gate("oauth", 8, now=100, state_path=self.state, lock_path=self.lock), 0)
        self.assertEqual(acquire_rate_gate("oauth", 8, now=101, state_path=self.state, lock_path=self.lock), 7)

    def test_categorized_circuit_and_cooldown(self):
        circuit_record("oauth", False, threshold=2, cooldown=30, now=10, state_path=self.state, lock_path=self.lock)
        circuit_record("oauth", False, threshold=2, cooldown=30, now=11, state_path=self.state, lock_path=self.lock)
        self.assertFalse(circuit_allow("oauth", now=12, state_path=self.state, lock_path=self.lock))
        self.assertTrue(circuit_allow("signup", now=12, state_path=self.state, lock_path=self.lock))
        set_global_cooldown(20, now=50, state_path=self.state, lock_path=self.lock)
        self.assertEqual(cooldown_remaining(now=55, state_path=self.state, lock_path=self.lock), 15)


class EventTests(unittest.TestCase):
    def test_json_event_and_text_compatibility(self):
        # Actual setup_run_logger output prefixes machine records with HH:MM:SS.
        event = parse_slog_line("12:34:56 @@GROK_EVENT@@" + json.dumps({"event": "phase", "worker": "1", "phase": "OAUTH", "outcome": "oauth_ok", "duration_sec": 2.5}))
        self.assertIsNotNone(event)
        state = PoolState(1, 1, "headless", 0, workers={"1": WorkerState("1", 1, 0)})
        apply_log_to_worker(state, event)
        self.assertEqual(state.workers["1"].outcomes["oauth_ok"], 1)
        self.assertEqual(state.workers["1"].phase_durations["OAUTH"], 2.5)
        text = parse_slog_line("12:00:00 [W1 1/1 · #1/1 · ✓1 ✗0] DONE email=a@example.com")
        self.assertEqual(text.phase, "DONE")

    def test_actual_logging_formatter_output(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        logger = logging.Logger("event-test")
        logger.addHandler(handler)
        logger.info("@@GROK_EVENT@@%s", json.dumps({"event": "complete", "category": "probe",
                                                     "worker": "1", "outcome": "inactive"}))
        parsed = parse_slog_line(stream.getvalue())
        self.assertEqual("inactive", parsed.event["outcome"])
        self.assertEqual("1", parsed.wid)


class TuiHelperTests(unittest.TestCase):
    def state(self):
        return PoolState(10, 1, "headless", 0, workers={"1": WorkerState("1", 10, 0)})

    def test_finite_and_unlimited_progress(self):
        half, determinate = progress_bar(5, 10, 10, now=0)
        self.assertTrue(determinate)
        self.assertEqual("█████░░░░░", half)
        complete, _ = progress_bar(12, 10, 10, now=0)
        self.assertEqual("██████████", complete)
        pulse_a, determinate = progress_bar(50, 0, 10, now=0)
        pulse_b, _ = progress_bar(50, 0, 10, now=0.5)
        self.assertFalse(determinate)
        self.assertNotEqual(pulse_a, pulse_b)
        self.assertEqual(3, pulse_a.count("█"))

    def test_pass_rate_and_filter_selection(self):
        self.assertIsNone(pass_rate(0, 0))
        self.assertEqual(75.0, pass_rate(3, 1))
        self.assertIsNone(select_log_filter("all", {"1", "2"}))
        self.assertEqual("2", select_log_filter("2", {"1", "2"}))
        self.assertIsNone(select_log_filter("9", {"1", "2"}))

    def test_real_account_complete_then_pool_async_outcome_updates_row(self):
        state = self.state()
        # Producer contract: completion is emitted after enqueue and therefore owns
        # the stable job id, attempt identity, final email, and elapsed_sec.
        complete = parse_slog_line("@@GROK_EVENT@@" + json.dumps({
            "category": "account", "event": "complete", "worker": "1",
            "account_index": 3, "attempt_id": "w1-account-3",
            "event_id": "w1-account-3:complete", "job_id": "probe-3",
            "email": "a@example.com", "ok": True, "elapsed_sec": 9.25,
        }))
        capture_account(state, complete)
        # Durable handler output arrives on pool stdout but preserves origin fields.
        outcome = parse_slog_line("@@GROK_EVENT@@" + json.dumps({
            "category": "probe", "event": "complete", "worker": "1",
            "account_index": 3, "attempt_id": "w1-account-3",
            "event_id": "probe-3:usable", "job_id": "probe-3",
            "email": "a@example.com", "outcome": "usable", "probe_status": 200,
        }), default_wid="pool")
        capture_account(state, outcome)
        self.assertEqual(1, len(state.accounts))
        self.assertEqual(("USABLE", "a@example.com", 9.25),
                         (state.accounts[0].status, state.accounts[0].email,
                          state.accounts[0].duration))

    def test_replayed_durable_event_does_not_inflate_outcomes(self):
        state = self.state()
        line = "@@GROK_EVENT@@" + json.dumps({
            "category": "probe", "event": "complete", "worker": "1",
            "event_id": "probe-7:inactive", "job_id": "probe-7",
            "email": "seven@example.com", "outcome": "inactive",
        })
        first = parse_slog_line(line, default_wid="pool")
        replay = parse_slog_line(line, default_wid="pool")
        first.wid = replay.wid = "pool"  # probe worker transport owns stdout
        apply_log_to_worker(state, first)
        apply_log_to_worker(state, replay)
        self.assertEqual(1, state.workers["1"].outcomes["inactive"])
        # Unkeyed legitimate phase events are deliberately not collapsed.
        phase = parse_slog_line("@@GROK_EVENT@@" + json.dumps({
            "category": "oauth", "event": "phase", "worker": "1",
            "phase": "OAUTH", "outcome": "oauth_ok",
        }))
        apply_log_to_worker(state, phase)
        apply_log_to_worker(state, phase)
        self.assertEqual(2, state.workers["1"].outcomes["oauth_ok"])

    def test_async_inactive_update_and_legacy_result_capture(self):
        state = self.state()
        initial = parse_slog_line("@@GROK_EVENT@@" + json.dumps({
            "category": "account", "event": "complete", "worker": "1",
            "account_index": 7, "attempt_id": "w1-account-7",
            "event_id": "w1-account-7:complete", "job_id": "probe-7",
            "email": "known@example.com", "ok": True, "elapsed_sec": 4.5,
        }))
        capture_account(state, initial)
        update = parse_slog_line("@@GROK_EVENT@@" + json.dumps({
            "category": "probe", "event": "complete", "worker": "1",
            "account_index": 7, "attempt_id": "w1-account-7",
            "event_id": "probe-7:inactive", "job_id": "probe-7",
            "email": "known@example.com", "outcome": "inactive", "probe_status": 403,
        }))
        capture_account(state, update)
        self.assertEqual(("known@example.com", "INACTIVE", 4.5),
                         (state.accounts[0].email, state.accounts[0].status,
                          state.accounts[0].duration))
        legacy = parse_slog_line("12:00:00 [W1 8/10 · #8/10 · ✓2 ✗0] RESULT PASS email=legacy@example.com")
        capture_account(state, legacy)
        self.assertEqual(("PASS", "legacy@example.com"),
                         (state.accounts[-1].status, state.accounts[-1].email))

    def test_narrow_unlimited_pulses(self):
        for width in range(1, 6):
            first, determinate = progress_bar(99, 0, width, now=0)
            later, _ = progress_bar(99, 0, width, now=0.25)
            self.assertFalse(determinate)
            self.assertEqual(width, len(first))
            self.assertEqual(width, len(later))
            self.assertNotEqual(first, later)

    def test_multiple_failure_events_do_not_inflate_terminal_fail(self):
        state = self.state()
        events = [
            {"category": "oauth", "event": "failure", "worker": "1", "attempt_id": "attempt-2", "event_id": "oauth-failure"},
            {"category": "account", "event": "complete", "worker": "1", "attempt_id": "attempt-2", "event_id": "account-complete", "ok": False},
            {"category": "probe", "event": "complete", "worker": "1", "attempt_id": "attempt-2", "event_id": "probe-hard-fail", "outcome": "hard_failed"},
        ]
        for event in events:
            apply_log_to_worker(state, parse_slog_line("@@GROK_EVENT@@" + json.dumps(event)))
        self.assertEqual(1, state.workers["1"].outcomes["hard_failed"])
        self.assertEqual(0, state.fail)


class ProbeOutcomeTests(unittest.TestCase):
    def _run(self, probe, *, push_raises=False, async_context=False, policy="usable"):
        import DrissionPage_example as browser
        result = {"email": "person@example.com"}
        if async_context:
            result.update(_event_worker="1", _event_account_index=4, _event_attempt_id="attempt-4")
        tokens = types.SimpleNamespace(access_token="access", refresh_token="refresh", email="person@example.com", user_id="user-1", name="Person", bot_flag_source=None, referrer="")
        push = (mock.Mock(side_effect=RuntimeError("push failed")) if push_raises
                else mock.Mock(return_value={"id": "router-1"}))
        emitted = []
        chat_module = types.SimpleNamespace(probe_chat_usable=mock.Mock(return_value=probe))
        push_module = types.SimpleNamespace(push_build_tokens_to_9router=push, probe_status_to_9router_flags=lambda status, error: {"test_status": "unavailable", "psd": {}})
        config = {"grok_cli": {"enabled": True, "inject_policy": policy}}
        with mock.patch.object(browser, "_load_config", return_value=config), mock.patch.object(browser, "current_proxy_url", return_value=""), mock.patch.object(browser, "_emit_event", side_effect=lambda *a, **kw: emitted.append((a, kw))), mock.patch.dict("sys.modules", {"chat_usable": chat_module, "push_9router_grok_cli": push_module}):
            if push_raises:
                with self.assertRaisesRegex(RuntimeError, "push failed"):
                    browser.probe_and_push_grok_cli(result, tokens)
            else:
                browser.probe_and_push_grok_cli(result, tokens)
        return result, emitted

    def test_sync_usable_and_inactive_emit_once_after_push(self):
        for probe, expected in [({"usable": True, "status": 200}, "usable"), ({"usable": False, "status": 403, "err": "denied"}, "inactive")]:
            with self.subTest(expected=expected):
                result, emitted = self._run(probe)
                complete = [item for item in emitted if item[0] == ("probe", "complete")]
                self.assertEqual(1, len(complete))
                self.assertEqual(expected, complete[0][1]["outcome"])
                self.assertEqual(expected == "usable", complete[0][1]["inject_active"])
                self.assertIn(f":probe:{expected}", complete[0][1]["event_id"])
                self.assertEqual(expected == "usable", result["inject_active"])

    def test_push_failure_emits_no_probe_outcome(self):
        _, emitted = self._run({"usable": True, "status": 200}, push_raises=True)
        self.assertFalse([item for item in emitted if item[0] == ("probe", "complete")])

    def test_async_context_suppresses_sync_outcome(self):
        result, emitted = self._run({"usable": True, "status": 200}, async_context=True)
        self.assertTrue(result["_suppress_probe_complete_event"])
        self.assertFalse([item for item in emitted if item[0] == ("probe", "complete")])

    def test_inject_all_emits_truthful_usable(self):
        result, emitted = self._run({}, policy="all")
        self.assertTrue(result["inject_active"])
        self.assertEqual("usable", emitted[-1][1]["outcome"])


class SourceRegressionTests(unittest.TestCase):
    def test_re_is_imported_before_progress_end_account_uses_it(self):
        source_path = Path(farm_tui.__file__).with_name("DrissionPage_example.py")
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        re_import_line = min(
            node.lineno for node in tree.body
            if isinstance(node, ast.Import)
            and any(alias.name == "re" for alias in node.names)
        )
        progress_fn = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "progress_end_account"
        )
        uses_re = any(
            isinstance(node, ast.Name) and node.id == "re"
            for node in ast.walk(progress_fn)
        )
        self.assertTrue(uses_re)
        self.assertLess(re_import_line, progress_fn.lineno)


@unittest.skipUnless(__import__("importlib").util.find_spec("textual"), "Textual not installed")
class ResponsiveTuiTests(unittest.TestCase):
    def _run_geometry_case(self, width, height):
        from textual.app import App
        from textual.widgets import RichLog, Tabs

        args = farm_tui.build_arg_parser().parse_args([
            "--count", "4", "--concurrent", "2", "--headless",
            "--no-proxy-check",
        ])
        observed = {}

        async def inspect(app):
            async with app.run_test(size=(width, height)) as pilot:
                await pilot.pause()
                workers = app.query_one("#workers-pane")
                recent = app.query_one("#recent-pane")
                tabs = app.query_one("#log-tabs", Tabs)
                log = app.query_one("#log", RichLog)
                summary = app.query_one("#summary")
                dashboard = app.query_one("#dashboard")
                observed.update(
                    workers=workers.region,
                    recent=recent.region,
                    tabs=tabs.region,
                    log=log.region,
                    summary=summary.region,
                    dashboard=dashboard.region,
                )
                if width < 100:
                    display_value = lambda widget: getattr(
                        widget.styles.display, "value", widget.styles.display
                    )
                    self.assertEqual("none", display_value(recent))
                    await pilot.press("r")
                    await pilot.pause()
                    self.assertNotEqual("none", display_value(recent))
                    self.assertEqual("none", display_value(workers))

        def run_for_test(app_self, *args, **kwargs):
            asyncio.run(inspect(app_self))
            return 0

        with mock.patch.object(farm_tui.PoolRunner, "start_all", autospec=True), \
             mock.patch.object(App, "run", new=run_for_test):
            farm_tui.run_tui(args)

        self.assertGreater(observed["summary"].height, 0)
        self.assertLessEqual(observed["summary"].bottom, height)
        self.assertGreater(observed["tabs"].height, 0)
        self.assertGreaterEqual(observed["log"].height, 4)
        self.assertGreater(observed["dashboard"].height, 0)
        if width >= 100:
            self.assertEqual(observed["workers"].y, observed["recent"].y)
            self.assertGreater(observed["recent"].x, observed["workers"].x)
            ratio = observed["workers"].width / observed["dashboard"].width
            self.assertGreater(ratio, 0.5)
            self.assertLess(ratio, 0.7)
        else:
            self.assertIn("narrow", observed.get("classes", {"narrow"}))

    def test_responsive_geometry_120x40(self):
        self._run_geometry_case(120, 40)

    def test_responsive_geometry_100x32(self):
        self._run_geometry_case(100, 32)

    def test_responsive_geometry_80x24(self):
        self._run_geometry_case(80, 24)


if __name__ == "__main__":
    unittest.main()

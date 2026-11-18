import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import cli, run, timing

START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def ev(minute, name, **fields):
    event = {"event": name, "at": (START + timedelta(minutes=minute)).isoformat()}
    event.update(fields)
    return event


class TestTimingReport(unittest.TestCase):
    def test_stage_segments_sum_between_stage_enters(self):
        events = [ev(0, "init"), ev(1, "stage_enter", to="baseline"),
                  ev(7, "stage_enter", to="briefing"), ev(37, "stage_enter", to="implementation")]
        report = timing.report(events)
        self.assertEqual(report["stages"]["admission"], {"entries": 1, "seconds": 60.0, "idle_seconds": 0.0})
        self.assertEqual(report["stages"]["baseline"]["seconds"], 360.0)
        self.assertEqual(report["stages"]["briefing"]["seconds"], 1800.0)
        self.assertEqual(report["wall_clock_seconds"], 2220.0)

    def test_a_gap_over_the_threshold_is_idle_not_work(self):
        events = [ev(0, "init"), ev(1, "stage_enter", to="gates"),
                  ev(2, "session", kind="stop"), ev(402, "control", command="resume")]
        report = timing.report(events)
        self.assertEqual(report["stages"]["gates"]["idle_seconds"], 24000.0)
        self.assertEqual(report["stages"]["gates"]["seconds"], 60.0)
        self.assertEqual(report["idle_gaps"], [{
            "from": events[2]["at"], "to": events[3]["at"], "seconds": 24000.0,
            "stage": "gates", "after": "session:stop", "ended_by": "control"}])

    def test_a_stall_event_does_not_split_an_idle_gap(self):
        events = [ev(0, "init"), ev(1, "stage_enter", to="gates"),
                  ev(2, "session", kind="stop"), ev(17, "stall"), ev(402, "control")]
        report = timing.report(events)
        self.assertEqual(len(report["idle_gaps"]), 1)
        self.assertEqual(report["idle_gaps"][0]["seconds"], 24000.0)
        self.assertEqual(report["idle_gaps"][0]["after"], "session:stop")

    def test_a_dispatch_is_timed_from_its_start_to_its_usage_event(self):
        events = [ev(0, "init"), ev(1, "dispatch_start", role="ppl-briefer", agent_id="a1"),
                  ev(12, "usage", role="ppl-briefer", agent_id="a1"),
                  ev(13, "dispatch_start", role="ppl-briefer", agent_id="a2")]
        self.assertEqual(timing.report(events)["dispatches"]["ppl-briefer"],
                         {"count": 2, "seconds": 660.0, "longest_seconds": 660.0})

    def test_a_role_with_no_paired_start_has_null_times(self):
        events = [ev(0, "init"), ev(1, "dispatch_start", role="ppl-verifier", agent_id="a1")]
        self.assertEqual(timing.report(events)["dispatches"]["ppl-verifier"],
                         {"count": 1, "seconds": None, "longest_seconds": None})

    def test_a_log_from_before_the_start_event_reports_no_dispatch_times(self):
        events = [ev(0, "init"), ev(1, "budget", kind="dispatch", used=1, cap=34)]
        report = timing.report(events)
        self.assertEqual(report["dispatches"], {})
        self.assertIn("dispatches", report["reasons"])

    def test_a_gate_gets_the_wait_before_its_receipt(self):
        events = [ev(0, "init"), ev(1, "stage_enter", to="gates"),
                  ev(2, "evidence", check="focused-tests"), ev(16, "evidence", check="regression"),
                  ev(30, "evidence", check="regression")]
        gates = timing.report(events)["gates"]
        self.assertEqual(gates["focused-tests"], {"runs": 1, "wait_seconds": 60.0})
        self.assertEqual(gates["regression"], {"runs": 2, "wait_seconds": 1680.0})

    def test_a_gate_after_an_idle_gap_waits_zero(self):
        events = [ev(0, "init"), ev(500, "evidence", check="regression")]
        self.assertEqual(timing.report(events)["gates"]["regression"], {"runs": 1, "wait_seconds": 0.0})

    def test_init_to_mr_and_its_missing_reason(self):
        with_mr = [ev(0, "init"), ev(102, "mr", iid=1)]
        self.assertEqual(timing.report(with_mr)["init_to_mr_seconds"], 6120.0)
        without = timing.report([ev(0, "init")])
        self.assertIsNone(without["init_to_mr_seconds"])
        self.assertIn("init_to_mr_seconds", without["reasons"])

    def test_broken_lines_are_skipped(self):
        events = ["not a dict", {"event": "init"}, {"event": "init", "at": "nonsense"},
                  ev(0, "init"), ev(1, "stage_enter", to="baseline")]
        self.assertEqual(timing.report(events)["wall_clock_seconds"], 60.0)

    def test_no_init_event_is_null_with_a_reason(self):
        report = timing.report([ev(1, "stage_enter", to="baseline")])
        self.assertIsNone(report["wall_clock_seconds"])
        self.assertIn("wall_clock_seconds", report["reasons"])

    def test_an_at_without_a_timezone_is_skipped(self):
        events = [ev(0, "init"), ev(1, "stage_enter", to="baseline"), ev(2, "mr", iid=1)]
        naive = {"event": "control", "at": "2026-01-01T00:00:30"}
        self.assertEqual(timing.report(events[:1] + [naive] + events[1:]), timing.report(events))

    def test_the_text_prints_null_as_a_dash(self):
        out = timing.text(timing.report([ev(0, "init")]))
        self.assertIn("init to merge request: -", out)
        self.assertIn("wait before receipt (includes the orchestrator's own time)", out)


def call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class TestTimingCli(unittest.TestCase):
    def test_json_has_the_seven_keys(self):
        run.init("DEMO-7701", "https://j/DEMO-7701", "/tmp/repo")
        code, out, _ = call("timing", "--task", "DEMO-7701", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(out)), sorted([
            "wall_clock_seconds", "init_to_mr_seconds", "stages", "dispatches", "gates",
            "idle_gaps", "reasons"]))

    def test_unknown_task_exits_2(self):
        code, _, _ = call("timing", "--task", "DEMO-7702")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()

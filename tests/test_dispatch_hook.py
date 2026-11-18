import contextlib
import io
import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))

import fixtures
from unittest import mock

from ppl import run, store
import ppl_dispatch


def start(task_id, repo="/tmp/repo"):
    """A run with its lane registered. Returns the dispatch hook's command line in it."""
    run.init(task_id, "u", repo)
    fixtures.bind_lane(task_id, repo)
    return ["--task", task_id]


class TestDispatchHook(unittest.TestCase):
    def test_start_increments_the_dispatch_counter(self):
        lane = start("DEMO-800")
        ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}, lane)
        self.assertEqual(run.load("DEMO-800")["budgets"]["dispatch_used"], 1)

    def test_the_brief_challenger_is_counted(self):
        # rule: R-BUDGET-DISPATCH-CAP
        lane = start("DEMO-807")
        ppl_dispatch.on_start({"agent_type": "ppl-brief-challenger", "cwd": "/tmp/repo"}, lane)
        self.assertEqual(run.load("DEMO-807")["budgets"]["dispatch_used"], 1)

    def test_a_non_pipeline_agent_is_not_counted(self):
        lane = start("DEMO-802")
        ppl_dispatch.on_start({"agent_type": "Explore", "cwd": "/tmp/repo"}, lane)
        self.assertEqual(run.load("DEMO-802")["budgets"]["dispatch_used"], 0)

    def test_a_role_dispatched_from_beside_the_worktree_is_counted(self):
        # rule: R-BUDGET-DISPATCH-CAP
        # the documented layout (DESIGN 5.4, README): the orchestrator's session,
        # whose cwd every subagent inherits, sits BESIDE the run's repo, not
        # inside it. Scoping the counter by cwd left it at zero in exactly that
        # arrangement, so the hard cap at `ppl stage enter` never fired.
        lane = start("DEMO-803")
        ppl_dispatch.on_start({"agent_type": "ppl-code-worker",
                               "cwd": "/home/dev/ppl-workspace"}, lane)
        self.assertEqual(run.load("DEMO-803")["budgets"]["dispatch_used"], 1)

    def test_an_unknown_ppl_prefixed_agent_is_not_counted(self):
        # scoping is by role identity (boundary.ROLE_RULES), not by the prefix
        lane = start("DEMO-806")
        ppl_dispatch.on_start({"agent_type": "ppl-imposter", "cwd": "/tmp/repo"}, lane)
        self.assertEqual(run.load("DEMO-806")["budgets"]["dispatch_used"], 0)

    def test_start_denies_past_the_hard_cap(self):
        lane = start("DEMO-801")
        state = run.load("DEMO-801")
        state["budgets"]["dispatch_used"] = 34
        run.save(state)
        result = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}, lane)
        self.assertIsNotNone(result)
        self.assertIn("BUDGET_EXHAUSTED",
                      result["hookSpecificOutput"]["permissionDecisionReason"])
        # Verify that denied dispatch does not consume budget
        self.assertEqual(run.load("DEMO-801")["budgets"]["dispatch_used"], 34)
        # Verify it stays pinned on repeated denial
        result2 = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}, lane)
        self.assertIsNotNone(result2)
        self.assertIn("BUDGET_EXHAUSTED",
                      result2["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(run.load("DEMO-801")["budgets"]["dispatch_used"], 34)

    def test_a_role_start_logs_a_dispatch_start_event(self):
        lane = start("DEMO-810")
        ppl_dispatch.on_start({"agent_type": "ppl-briefer", "agent_id": "a1"}, lane)
        starts = [e for e in store.read_log("DEMO-810") if e.get("event") == "dispatch_start"]
        self.assertEqual([(e["role"], e["agent_id"]) for e in starts], [("ppl-briefer", "a1")])

    def test_a_start_without_an_agent_id_logs_null(self):
        lane = start("DEMO-811")
        ppl_dispatch.on_start({"agent_type": "ppl-briefer"}, lane)
        starts = [e for e in store.read_log("DEMO-811") if e.get("event") == "dispatch_start"]
        self.assertIsNone(starts[0]["agent_id"])

    def test_a_non_role_start_logs_nothing(self):
        lane = start("DEMO-812")
        ppl_dispatch.on_start({"agent_type": "general-purpose", "agent_id": "a1"}, lane)
        self.assertEqual([e for e in store.read_log("DEMO-812")
                          if e.get("event") == "dispatch_start"], [])

    def test_a_log_that_cannot_be_appended_does_not_change_the_result(self):
        lane = start("DEMO-813")
        real = store.append_log

        def full_for_starts(task_id, event):
            # budget.spend logs through the same function and is not under test here
            if event.get("event") == "dispatch_start":
                raise OSError("disk full")
            return real(task_id, event)

        stderr = io.StringIO()
        with mock.patch.object(store, "append_log", side_effect=full_for_starts), \
                contextlib.redirect_stderr(stderr):
            result = ppl_dispatch.on_start({"agent_type": "ppl-briefer", "agent_id": "a1"}, lane)
        self.assertIn("disk full", stderr.getvalue())
        self.assertIsNone(result)

    def test_a_refused_spend_still_logs_the_start(self):
        lane = start("DEMO-814")
        state = run.load("DEMO-814")
        state["budgets"]["dispatch_used"] = 34
        run.save(state)
        result = ppl_dispatch.on_start({"agent_type": "ppl-briefer", "agent_id": "a1"}, lane)
        self.assertIsNotNone(result)
        self.assertEqual(len([e for e in store.read_log("DEMO-814")
                              if e.get("event") == "dispatch_start"]), 1)

    def test_no_active_run_is_a_no_op(self):
        # a session in no lane, and one whose lane the registry does not hold
        for argv in ((), ["--task", "DEMO-899"]):
            self.assertIsNone(ppl_dispatch.on_start(
                {"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}, argv))

    def test_corrupt_state_file_is_a_no_op(self):
        lane = start("DEMO-804")
        state_path = Path(os.environ["PPL_HOME"], "runs", "DEMO-804", "state.json")
        state_path.write_text("{ invalid json", encoding="utf-8")
        result = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}, lane)
        self.assertIsNone(result)

    def test_missing_state_file_is_a_no_op(self):
        lane = start("DEMO-805")
        state_path = Path(os.environ["PPL_HOME"], "runs", "DEMO-805", "state.json")
        state_path.unlink()
        result = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}, lane)
        self.assertIsNone(result)

    def test_invalid_payload_none(self):
        result = ppl_dispatch.on_start(None)
        self.assertIsNone(result)

    def test_invalid_payload_not_dict(self):
        result = ppl_dispatch.on_start(42)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

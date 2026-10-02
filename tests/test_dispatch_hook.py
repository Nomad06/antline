import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from ppl import run
import ppl_dispatch


class TestDispatchHook(unittest.TestCase):
    def test_start_increments_the_dispatch_counter(self):
        run.init("DEMO-800", "u", "/tmp/repo")
        ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"})
        self.assertEqual(run.load("DEMO-800")["budgets"]["dispatch_used"], 1)

    def test_the_brief_challenger_is_counted(self):
        # rule: R-BUDGET-DISPATCH-CAP
        run.init("DEMO-807", "u", "/tmp/repo")
        ppl_dispatch.on_start({"agent_type": "ppl-brief-challenger", "cwd": "/tmp/repo"})
        self.assertEqual(run.load("DEMO-807")["budgets"]["dispatch_used"], 1)

    def test_a_non_pipeline_agent_is_not_counted(self):
        run.init("DEMO-802", "u", "/tmp/repo")
        ppl_dispatch.on_start({"agent_type": "Explore", "cwd": "/tmp/repo"})
        self.assertEqual(run.load("DEMO-802")["budgets"]["dispatch_used"], 0)

    def test_a_role_dispatched_from_beside_the_worktree_is_counted(self):
        # rule: R-BUDGET-DISPATCH-CAP
        # the documented layout (DESIGN 5.4, README): the orchestrator's session,
        # whose cwd every subagent inherits, sits BESIDE the run's repo, not
        # inside it. Scoping the counter by cwd left it at zero in exactly that
        # arrangement, so the hard cap at `ppl stage enter` never fired.
        run.init("DEMO-803", "u", "/tmp/repo")
        ppl_dispatch.on_start({"agent_type": "ppl-code-worker",
                               "cwd": "/home/dev/ppl-workspace"})
        self.assertEqual(run.load("DEMO-803")["budgets"]["dispatch_used"], 1)

    def test_an_unknown_ppl_prefixed_agent_is_not_counted(self):
        # scoping is by role identity (boundary.ROLE_RULES), not by the prefix
        run.init("DEMO-806", "u", "/tmp/repo")
        ppl_dispatch.on_start({"agent_type": "ppl-imposter", "cwd": "/tmp/repo"})
        self.assertEqual(run.load("DEMO-806")["budgets"]["dispatch_used"], 0)

    def test_start_denies_past_the_hard_cap(self):
        run.init("DEMO-801", "u", "/tmp/repo")
        state = run.load("DEMO-801")
        state["budgets"]["dispatch_used"] = 34
        run.save(state)
        result = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"})
        self.assertIsNotNone(result)
        self.assertIn("BUDGET_EXHAUSTED",
                      result["hookSpecificOutput"]["permissionDecisionReason"])
        # Verify that denied dispatch does not consume budget
        self.assertEqual(run.load("DEMO-801")["budgets"]["dispatch_used"], 34)
        # Verify it stays pinned on repeated denial
        result2 = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"})
        self.assertIsNotNone(result2)
        self.assertIn("BUDGET_EXHAUSTED",
                      result2["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(run.load("DEMO-801")["budgets"]["dispatch_used"], 34)

    def test_no_active_run_is_a_no_op(self):
        Path(os.environ["PPL_HOME"], "runs").mkdir(parents=True, exist_ok=True)
        Path(os.environ["PPL_HOME"], "runs", "ACTIVE").write_text("\n", encoding="utf-8")
        self.assertIsNone(ppl_dispatch.on_start(
            {"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"}))

    def test_corrupt_state_file_is_a_no_op(self):
        run.init("DEMO-804", "u", "/tmp/repo")
        state_path = Path(os.environ["PPL_HOME"], "runs", "DEMO-804", "state.json")
        state_path.write_text("{ invalid json", encoding="utf-8")
        result = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"})
        self.assertIsNone(result)

    def test_missing_state_file_is_a_no_op(self):
        run.init("DEMO-805", "u", "/tmp/repo")
        state_path = Path(os.environ["PPL_HOME"], "runs", "DEMO-805", "state.json")
        state_path.unlink()
        result = ppl_dispatch.on_start({"agent_type": "ppl-code-worker", "cwd": "/tmp/repo"})
        self.assertIsNone(result)

    def test_invalid_payload_none(self):
        result = ppl_dispatch.on_start(None)
        self.assertIsNone(result)

    def test_invalid_payload_not_dict(self):
        result = ppl_dispatch.on_start(42)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()

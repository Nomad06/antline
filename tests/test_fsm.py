import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ.setdefault("PPL_HOME", tmphome.mkdtemp(prefix="pplhome-"))

from ppl import fsm
from ppl.errors import PplError


class TestStageGraph(unittest.TestCase):
    def test_happy_path_is_legal(self):
        chain = ["admission", "baseline", "briefing", "implementation", "tests",
                 "gates", "verification", "mr", "review", "closure"]
        for current, target in zip(chain, chain[1:]):
            fsm.check_stage(current, target)

    def test_cannot_skip_verification(self):
        # rule: R-FSM-STAGE-GRAPH
        with self.assertRaises(PplError) as ctx:
            fsm.check_stage("gates", "mr")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_rework_returns_from_verification_to_implementation(self):
        fsm.check_stage("verification", "implementation")

    def test_parking_is_reachable_from_every_stage(self):
        for stage in fsm.STAGES:
            fsm.check_stage(stage, "parking")

    def test_the_stage_graph_alone_does_not_narrow_a_resume_from_parking(self):
        # fsm.py stays a pure stage graph: it does not know a run's actual
        # resume_stage, so it still accepts any real stage as a target from
        # "parking" - narrowing that down to the one recorded stage is
        # `ppl/run.py:_resume_target`'s job, layered on top of this
        # function the same way `control.enforce` layers on top of it for
        # the pause gate. See tests/test_run.py::TestResumeFromParking for
        # the actual restriction (D1, cycle 2 wave D) [R-PARK-RESUME-EXACT].
        fsm.check_stage("parking", "gates")

    def test_merge_watch_only_follows_closure(self):
        # rule: R-MERGE-WATCH-TERMINAL
        fsm.check_stage("closure", "merge_watch")
        with self.assertRaises(PplError):
            fsm.check_stage("review", "merge_watch")

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(PplError) as ctx:
            fsm.check_stage("admission", "teleport")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestLifecycle(unittest.TestCase):
    def test_normal_progression(self):
        fsm.check_lifecycle("planned", "ready")
        fsm.check_lifecycle("ready", "in_progress")
        fsm.check_lifecycle("in_progress", "done")

    def test_done_is_terminal(self):
        # rule: R-FSM-LIFECYCLE
        # rule: R-MERGE-WATCH-TERMINAL
        with self.assertRaises(PplError):
            fsm.check_lifecycle("done", "in_progress")

    def test_blocked_can_return_to_in_progress(self):
        fsm.check_lifecycle("blocked", "in_progress")

    def test_blocked_cannot_reach_done_directly(self):
        # rule: R-PARK-RESUME-RESTORES-LIFECYCLE (W3, cycle 2 wave B). This is
        # exactly why a resumed run's lifecycle status has to be restored to
        # in_progress somewhere before `ppl transition done` - blocked has no
        # edge to done at all, so a run left at blocked forever (which is
        # what used to happen on every resume from parking) could never
        # close, whatever the rest of the done gate said.
        with self.assertRaises(PplError) as ctx:
            fsm.check_lifecycle("blocked", "done")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")


if __name__ == "__main__":
    unittest.main()

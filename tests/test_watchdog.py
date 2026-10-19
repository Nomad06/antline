import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import recover, run, store, verdict, watchdog
from ppl.errors import PplError


def make_past_deadline():
    # Any timestamp string smaller than store.now() at call time works: the
    # comparison in watchdog.check is a plain string compare on matching
    # fixed-width isoformat strings (store.deadline_in's own docstring), so a
    # date far in the past is as good as one second ago.
    return "2000-01-01T00:00:00+00:00"


class TestWatchdogCheck(unittest.TestCase):
    """R-WATCHDOG-DEADLINE: a stage that overran its deadline is parked with
    TIMED_OUT and the lease generation is bumped.
    """

    def test_does_nothing_before_the_deadline(self):
        run.init("DEMO-960", "https://j/DEMO-960", "/tmp/repo")
        result = watchdog.check("DEMO-960")
        self.assertFalse(result["timed_out"])
        self.assertEqual(run.load("DEMO-960")["stage"], "admission")
        self.assertEqual(run.load("DEMO-960")["lease_generation"], 1)

    def test_parks_with_timed_out_once_the_deadline_has_passed(self):
        # rule: R-WATCHDOG-DEADLINE
        run.init("DEMO-961", "https://j/DEMO-961", "/tmp/repo")
        run.stage_enter("DEMO-961", "baseline")
        state = run.load("DEMO-961")
        state["deadline"] = make_past_deadline()
        run.save(state)
        result = watchdog.check("DEMO-961")
        self.assertTrue(result["timed_out"])
        parked = run.load("DEMO-961")
        self.assertEqual(parked["stage"], "parking")
        self.assertEqual(parked["status"], "blocked")
        # The generation the parked stage's attempt was holding is fenced,
        # the same refusal a returning stale worker trips (R-LEASE-STALE-WRITE).
        with self.assertRaises(PplError) as ctx:
            run.check_generation("DEMO-961", 1)
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_bumps_the_lease_generation_on_timeout(self):
        # rule: R-WATCHDOG-DEADLINE
        run.init("DEMO-962", "https://j/DEMO-962", "/tmp/repo")
        state = run.load("DEMO-962")
        state["deadline"] = make_past_deadline()
        run.save(state)
        result = watchdog.check("DEMO-962")
        self.assertEqual(result["lease_generation"], 2)
        self.assertEqual(run.load("DEMO-962")["lease_generation"], 2)
        with self.assertRaises(PplError) as ctx:
            run.check_generation("DEMO-962", 1)
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_records_timed_out_as_the_parking_blocker(self):
        run.init("DEMO-963", "https://j/DEMO-963", "/tmp/repo")
        state = run.load("DEMO-963")
        state["deadline"] = make_past_deadline()
        run.save(state)
        watchdog.check("DEMO-963")
        from ppl import paths
        parking = store.read_json(paths.run_dir("DEMO-963") / "parking.json")
        self.assertEqual(parking["blocker"], "TIMED_OUT")

    def test_is_a_no_op_once_the_run_is_already_parked(self):
        # A second call must not double-bump the generation or overwrite an
        # already-recorded parking record with a fresh timestamp.
        run.init("DEMO-964", "https://j/DEMO-964", "/tmp/repo")
        state = run.load("DEMO-964")
        state["deadline"] = make_past_deadline()
        run.save(state)
        first = watchdog.check("DEMO-964")
        self.assertTrue(first["timed_out"])
        second = watchdog.check("DEMO-964")
        self.assertFalse(second["timed_out"])
        self.assertEqual(run.load("DEMO-964")["lease_generation"], 2)

    def test_merge_watch_is_exempt_even_past_its_deadline(self):
        # merge_watch is the optional post-done observational tail
        # (R-MERGE-WATCH-TERMINAL). Parking it would serve nothing.
        run.init("DEMO-970", "https://j/DEMO-970", "/tmp/repo")
        for stage in ("baseline", "briefing", "implementation", "tests", "gates",
                      "verification", "mr", "review", "closure", "merge_watch"):
            if stage == "merge_watch":
                # merge_watch needs status done [R-MERGE-WATCH-AFTER-DONE], and the done
                # gate is not what this test is about, so the status is stamped.
                state = run.load("DEMO-970")
                state["status"] = "done"
                run.save(state)
            if stage == "closure":
                # closure needs the done gate to pass [R-CLOSURE-ONLY-WHEN-DONE-PASSES], which
                # needs a real repo and evidence, so it is stubbed for this one step.
                with mock.patch.object(verdict, "current_blockers", return_value=[]):
                    run.stage_enter("DEMO-970", stage)
                continue
            run.stage_enter("DEMO-970", stage)
            if stage == "baseline":
                # E1, cycle 2 wave E: stage_enter now refuses to leave
                # "baseline" without a recorded base_sha
                # [R-BASELINE-RECORDED-BEFORE-LEAVING] - stamped directly
                # since this test has no real git repo to record one against.
                state = run.load("DEMO-970")
                state["base_sha"] = "a" * 40
                run.save(state)
            if stage == "briefing":
                # stage_enter also refuses to leave "briefing" without an
                # ACCEPTED brief challenge
                # [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION] - stamped the
                # same way, this test has no briefer to run.
                state = run.load("DEMO-970")
                state["spec_revision"] = "sha256:" + "0" * 64
                state["brief_challenge"] = {"spec_revision": state["spec_revision"],
                                            "verdict": "ACCEPTED",
                                            "at": "2026-01-01T00:00:00+00:00"}
                run.save(state)
        state = run.load("DEMO-970")
        self.assertEqual(state["stage"], "merge_watch")
        state["deadline"] = make_past_deadline()
        run.save(state)
        result = watchdog.check("DEMO-970")
        self.assertFalse(result["timed_out"])
        self.assertEqual(run.load("DEMO-970")["stage"], "merge_watch")

    def test_escalation_is_exempt_even_past_its_deadline(self):
        # rule: R-WATCHDOG-DEADLINE
        # escalation is a legal stage (fsm.STAGES) reached directly with
        # `ppl stage enter escalation` - never through `ppl escalate`, whose
        # run.park always lands on "parking" instead (round-2 review: the
        # comment here used to call this "the terminal-ish state a timeout
        # itself produces", which is only true of parking's half of the
        # tuple). It already reads as "waiting on the owner" to
        # ppl/panel/server.py's needs-you check, the same as parking, so
        # parking it a second time would not surface anything new. This test
        # drives a run into escalation directly, rather than assuming the
        # branch, so the exemption is exercised and not just asserted.
        run.init("DEMO-985", "https://j/DEMO-985", "/tmp/repo")
        state = run.stage_enter("DEMO-985", "escalation")
        self.assertEqual(state["stage"], "escalation")
        state["deadline"] = make_past_deadline()
        run.save(state)
        result = watchdog.check("DEMO-985")
        self.assertFalse(result["timed_out"])
        self.assertEqual(run.load("DEMO-985")["stage"], "escalation")

    def test_a_run_with_no_deadline_recorded_is_left_alone(self):
        # Defensive: a state predating this field (or one built directly by a
        # test without going through stage_enter) must not crash the check.
        run.init("DEMO-965", "https://j/DEMO-965", "/tmp/repo")
        state = run.load("DEMO-965")
        del state["deadline"]
        run.save(state)
        result = watchdog.check("DEMO-965")
        self.assertFalse(result["timed_out"])


class TestStageEnterDeadline(unittest.TestCase):
    def test_init_stamps_a_deadline_for_admission(self):
        state = run.init("DEMO-966", "https://j/DEMO-966", "/tmp/repo")
        self.assertIsNotNone(state["deadline"])

    def test_stage_enter_refreshes_the_deadline(self):
        run.init("DEMO-967", "https://j/DEMO-967", "/tmp/repo")
        state = run.load("DEMO-967")
        old_deadline = state["deadline"]
        state["deadline"] = make_past_deadline()
        run.save(state)
        entered = run.stage_enter("DEMO-967", "baseline")
        self.assertNotEqual(entered["deadline"], make_past_deadline())
        self.assertGreaterEqual(entered["deadline"], old_deadline)


class TestHeartbeatSemantics(unittest.TestCase):
    """The watchdog measures inactivity, not stage duration - review-round-1
    ruling on cycle 2 task 4. Three cases, each independently provable by
    mutation (see the task report for the mutation results):

    (a) a quiet control-plane call that finds nothing wrong MUST refresh the
        deadline - including a quiet `ppl recover` tick, since that is the
        review loop's only per-tick call once nothing needs a reply.
    (b) a hang with zero `ppl` calls MUST still expire, unchanged from
        before this ruling - nothing "refreshes" a run nobody touches.
    (c) a stale attempt's refused write MUST NOT refresh the deadline the
        live attempt is racing.
    """

    def test_a_quiet_recover_tick_refreshes_the_deadline(self):
        # (a) rule: R-WATCHDOG-DEADLINE
        run.init("DEMO-980", "https://j/DEMO-980", "/tmp/repo")
        state = run.load("DEMO-980")
        # Not expired, but far short of a fresh 4-hour deadline - anything
        # this small still standing after the call would mean no refresh.
        marker = store.deadline_in(60)
        state["deadline"] = marker
        run.save(state)
        # repo is not a real git worktree here, so reconcile takes its
        # blocked-repo path - but that path is reached only after the
        # refresh below, so it proves nothing about whether refresh ran.
        recover.reconcile("DEMO-980")
        refreshed = run.load("DEMO-980")["deadline"]
        self.assertGreater(refreshed, store.deadline_in(3600))

    def test_a_hang_with_zero_ppl_calls_still_times_out(self):
        # (b) rule: R-WATCHDOG-DEADLINE
        run.init("DEMO-981", "https://j/DEMO-981", "/tmp/repo")
        run.stage_enter("DEMO-981", "baseline")
        state = run.load("DEMO-981")
        state["deadline"] = make_past_deadline()
        run.save(state)
        # The first ppl call after the hang, whatever it is, discovers it.
        result = recover.reconcile("DEMO-981")
        self.assertTrue(any("TIMED_OUT" in line for line in result["actions"]))
        self.assertEqual(run.load("DEMO-981")["stage"], "parking")
        with self.assertRaises(PplError) as ctx:
            run.check_generation("DEMO-981", 1)
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_refresh_deadline_is_a_direct_restamp(self):
        run.init("DEMO-982", "https://j/DEMO-982", "/tmp/repo")
        state = run.load("DEMO-982")
        state["deadline"] = make_past_deadline()
        run.save(state)
        refreshed = run.refresh_deadline("DEMO-982")
        self.assertGreater(refreshed, make_past_deadline())
        self.assertEqual(run.load("DEMO-982")["deadline"], refreshed)


class TestRecoverWatchdogIntegration(unittest.TestCase):
    """ppl recover is one of the two callers the brief names by name."""

    def test_reconcile_reports_the_timeout_and_bumps_generation_once(self):
        run.init("DEMO-968", "https://j/DEMO-968", "/tmp/repo")
        state = run.load("DEMO-968")
        state["deadline"] = make_past_deadline()
        run.save(state)
        result = recover.reconcile("DEMO-968")
        self.assertTrue(any("TIMED_OUT" in line for line in result["actions"]))
        self.assertEqual(run.load("DEMO-968")["lease_generation"], 2)

    def test_reconcile_still_reports_the_new_generation_line(self):
        run.init("DEMO-969", "https://j/DEMO-969", "/tmp/repo")
        state = run.load("DEMO-969")
        state["deadline"] = make_past_deadline()
        run.save(state)
        result = recover.reconcile("DEMO-969")
        self.assertTrue(any("pass --generation 2" in line for line in result["actions"]))


if __name__ == "__main__":
    unittest.main()

import itertools
import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import metrics, paths, run, store
from ppl.errors import PplError

PRODUCER_A = {"provider": "claude-code-subagent", "model": "opus", "params_hash": "h1"}
PRODUCER_B = {"provider": "claude-code-subagent", "model": "sonnet", "params_hash": "h2"}
COUNTER = itertools.count(700)


def new_task():
    task = "DEMO-%d" % next(COUNTER)
    run.init(task, "u", "/tmp/repo")
    return task


def write_verdict(task, sha, outcome, producer):
    store.write_json(paths.run_dir(task) / "verdicts" / ("%s.json" % sha), {
        "task_id": task, "candidate_sha": sha, "spec_revision": "sha256:x",
        "verdict": outcome, "acceptance_evidence": {}, "findings": [],
        "producer": producer,
    })


def mark_done(task, sha):
    state = run.load(task)
    state["candidate_sha"] = sha
    run.save(state)
    store.append_log(task, {"event": "lifecycle", "to": "done"})


class IsolatedFleet(unittest.TestCase):
    """metrics.compute() deliberately has no per-task scope - it reads the
    whole fleet under PPL_HOME, which is the point of a fleet-wide command.
    That means, unlike every other test module here, tests in THIS module
    cannot share one PPL_HOME across the file: an earlier test's verdicts
    would silently count in a later test's "no data yet" assertion. Each
    test gets its own empty runs directory instead.
    """

    def setUp(self):
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")


class TestRecordLabel(IsolatedFleet):
    def test_rejects_an_unknown_actual(self):
        # rule: R-LABEL-VALUE-VOCAB
        task = new_task()
        with self.assertRaises(PplError) as ctx:
            metrics.record_label(task, "a" * 40, "maybe")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_rejects_a_non_hex_sha(self):
        # rule: R-LABEL-VALUE-VOCAB
        task = new_task()
        with self.assertRaises(PplError) as ctx:
            metrics.record_label(task, "not-hex!", "defective")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_rejects_a_sha_with_traversal(self):
        # rule: R-LABEL-VALUE-VOCAB
        task = new_task()
        with self.assertRaises(PplError):
            metrics.record_label(task, "../abc", "defective")

    def test_rejects_labelling_a_task_that_does_not_exist(self):
        # rule: R-LABEL-VALUE-VOCAB
        with self.assertRaises(PplError) as ctx:
            metrics.record_label("DEMO-999999", "a" * 40, "defective")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_records_a_readable_label(self):
        task = new_task()
        metrics.record_label(task, "abc123", "correct", by="reviewer")
        stored = store.read_json(paths.home() / "labels" / task / "abc123.json")
        self.assertEqual(stored["actual"], "correct")
        self.assertEqual(stored["by"], "reviewer")


class TestIncompleteCheckRate(IsolatedFleet):
    def test_undefined_with_no_verdicts_anywhere(self):
        # rule: R-METRIC-SAMPLE-DEPENDENT-UNDEFINED
        report = metrics.compute()
        self.assertFalse(report["overall"]["incomplete_check_rate"]["defined"])
        self.assertIsNone(report["overall"]["incomplete_check_rate"]["value"])

    def test_a_mix_of_outcomes_is_a_real_rate_not_undefined(self):
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        write_verdict(task, "a2", "TIMEOUT", PRODUCER_A)
        write_verdict(task, "a3", "REWORK", PRODUCER_A)
        write_verdict(task, "a4", "ERROR", PRODUCER_A)
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["incomplete_check_rate"]
        self.assertTrue(entry["defined"])
        self.assertAlmostEqual(entry["value"], 0.5)


class TestLabelDependentRates(IsolatedFleet):
    def test_outcompeted_rate_counts_outcompeted_labels_on_pass_verdicts(self):
        # rule: R-METRIC-OUTCOMPETED
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        write_verdict(task, "a2", "PASS", PRODUCER_A)
        metrics.record_label(task, "a1", "outcompeted")
        metrics.record_label(task, "a2", "correct")
        entry = metrics.compute()["overall"]["outcompeted_rate"]
        self.assertTrue(entry["defined"])
        self.assertEqual(entry["value"], 0.5)
        escape = metrics.compute()["overall"]["defect_escape_rate"]
        self.assertEqual(escape["value"], 0.0)

    def test_outcompeted_rate_undefined_without_a_label(self):
        # rule: R-METRIC-OUTCOMPETED
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        entry = metrics.compute()["overall"]["outcompeted_rate"]
        self.assertFalse(entry["defined"])
        self.assertIn("ppl label", entry["reason"])

    def test_defect_escape_rate_undefined_without_a_label(self):
        # rule: R-METRIC-LABEL-DEPENDENT-UNDEFINED
        # A PASS verdict exists here, so this is the "recorded but unlabelled"
        # cause - the reason must name a real, copyable sha, not a <sha>
        # placeholder, and must not be confused with "nothing to label".
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["defect_escape_rate"]
        self.assertFalse(entry["defined"])
        self.assertIsNone(entry["value"])
        self.assertIn("a1", entry["reason"])
        self.assertIn(task, entry["reason"])
        self.assertIn("ppl label", entry["reason"])

    def test_defect_escape_rate_undefined_with_no_pass_verdict_at_all(self):
        # rule: R-METRIC-LABEL-DEPENDENT-UNDEFINED
        # The defect review round 2 found: two labels ARE recorded here, but
        # for verdicts of the wrong kind (REWORK), so there is no accepted
        # (PASS) verdict for either label to attach to. The reason must say
        # that plainly and must NOT suggest running `ppl label` - there is
        # nothing a label could attach to.
        task = new_task()
        write_verdict(task, "a1", "REWORK", PRODUCER_A)
        write_verdict(task, "a2", "BLOCKED", PRODUCER_A)
        metrics.record_label(task, "a1", "correct")
        metrics.record_label(task, "a2", "defective")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["defect_escape_rate"]
        self.assertFalse(entry["defined"])
        self.assertIsNone(entry["value"])
        self.assertIn("no", entry["reason"].lower())
        self.assertIn("recorded", entry["reason"])
        self.assertNotIn("ppl label", entry["reason"])

    def test_defect_escape_rate_after_a_label(self):
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        write_verdict(task, "a2", "PASS", PRODUCER_A)
        metrics.record_label(task, "a1", "defective")
        metrics.record_label(task, "a2", "correct")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["defect_escape_rate"]
        self.assertTrue(entry["defined"])
        self.assertAlmostEqual(entry["value"], 0.5)

    def test_false_fail_rate_undefined_without_a_label(self):
        # rule: R-METRIC-LABEL-DEPENDENT-UNDEFINED
        task = new_task()
        write_verdict(task, "a1", "REWORK", PRODUCER_A)
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["false_fail_rate"]
        self.assertFalse(entry["defined"])
        self.assertIn("a1", entry["reason"])
        self.assertIn("ppl label", entry["reason"])

    def test_false_fail_rate_undefined_with_no_rejected_verdict_at_all(self):
        # rule: R-METRIC-LABEL-DEPENDENT-UNDEFINED
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        metrics.record_label(task, "a1", "defective")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["false_fail_rate"]
        self.assertFalse(entry["defined"])
        self.assertNotIn("ppl label", entry["reason"])

    def test_false_fail_rate_after_a_label(self):
        task = new_task()
        write_verdict(task, "a1", "REWORK", PRODUCER_A)
        write_verdict(task, "a2", "BLOCKED", PRODUCER_A)
        metrics.record_label(task, "a1", "correct")
        metrics.record_label(task, "a2", "defective")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["false_fail_rate"]
        self.assertTrue(entry["defined"])
        self.assertAlmostEqual(entry["value"], 0.5)

    def test_an_incomplete_outcome_never_feeds_either_labelled_rate(self):
        # rule: R-METRIC-LABEL-DEPENDENT-UNDEFINED
        # INCONCLUSIVE/ERROR/CANCELLED/TIMEOUT are neither a PASS a human can
        # call a hidden defect, nor a REWORK/BLOCKED a human can call a false
        # rejection - a check that did not happen is evidence of neither.
        task = new_task()
        write_verdict(task, "a1", "TIMEOUT", PRODUCER_A)
        metrics.record_label(task, "a1", "defective")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]
        self.assertFalse(entry["defect_escape_rate"]["defined"])
        self.assertFalse(entry["false_fail_rate"]["defined"])
        # The label WAS recorded, so neither reason may suggest recording one -
        # there is a TIMEOUT verdict here, never a PASS or a REWORK/BLOCKED.
        self.assertNotIn("ppl label", entry["defect_escape_rate"]["reason"])
        self.assertNotIn("ppl label", entry["false_fail_rate"]["reason"])


class TestStructurallyUndefinedMetrics(IsolatedFleet):
    def test_cost_per_accepted_task_is_always_undefined(self):
        # rule: R-METRIC-STRUCTURALLY-UNDEFINED
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        report = metrics.compute()
        self.assertFalse(report["overall"]["cost_per_accepted_task"]["defined"])
        self.assertIsNone(report["overall"]["cost_per_accepted_task"]["value"])

    def test_human_minutes_is_always_undefined(self):
        # rule: R-METRIC-HUMAN-MINUTES-UNDEFINED
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        report = metrics.compute()
        self.assertFalse(report["overall"]["human_minutes"]["defined"])
        self.assertIsNone(report["overall"]["human_minutes"]["value"])


class TestTimeToAcceptance(IsolatedFleet):
    def test_undefined_when_nothing_has_reached_done(self):
        # rule: R-METRIC-SAMPLE-DEPENDENT-UNDEFINED
        new_task()
        report = metrics.compute()
        self.assertFalse(report["overall"]["time_to_acceptance_seconds"]["defined"])

    def test_defined_once_a_task_reaches_done_and_attributed_to_its_producer(self):
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_B)
        mark_done(task, "a1")
        report = metrics.compute()
        overall = report["overall"]["time_to_acceptance_seconds"]
        self.assertTrue(overall["defined"])
        self.assertGreaterEqual(overall["value"], 0)
        sliced = report["producers"]["claude-code-subagent:sonnet"]["time_to_acceptance_seconds"]
        self.assertTrue(sliced["defined"])

    def test_undefined_reason_distinguishes_nothing_done_from_not_this_producer(self):
        # rule: R-METRIC-SAMPLE-DEPENDENT-UNDEFINED
        # A task accepted by PRODUCER_B has reached done. PRODUCER_A has an
        # open (not-done) task. PRODUCER_A's slice must not say "no task in
        # scope has reached done yet" - that would be false, since one has.
        done_task = new_task()
        write_verdict(done_task, "a1", "PASS", PRODUCER_B)
        mark_done(done_task, "a1")
        open_task = new_task()
        write_verdict(open_task, "a2", "TIMEOUT", PRODUCER_A)
        report = metrics.compute()
        a_entry = report["producers"]["claude-code-subagent:opus"]["time_to_acceptance_seconds"]
        self.assertFalse(a_entry["defined"])
        self.assertNotIn("no task in scope has reached done yet", a_entry["reason"])
        self.assertIn("1", a_entry["reason"])
        b_entry = report["producers"]["claude-code-subagent:sonnet"]["time_to_acceptance_seconds"]
        self.assertTrue(b_entry["defined"])


class TestSlicedByProducer(IsolatedFleet):
    def test_two_producers_are_reported_separately(self):
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        write_verdict(task, "a2", "TIMEOUT", PRODUCER_B)
        report = metrics.compute()
        self.assertIn("claude-code-subagent:opus", report["producers"])
        self.assertIn("claude-code-subagent:sonnet", report["producers"])
        self.assertTrue(
            report["producers"]["claude-code-subagent:sonnet"]["incomplete_check_rate"]["defined"])
        self.assertAlmostEqual(
            report["producers"]["claude-code-subagent:sonnet"]["incomplete_check_rate"]["value"],
            1.0)
        self.assertAlmostEqual(
            report["producers"]["claude-code-subagent:opus"]["incomplete_check_rate"]["value"],
            0.0)


class TestArchivedRunsAreVisible(IsolatedFleet):
    """The defect found in review round 1: `ppl destroy --scope local` moves
    a run's whole directory to `runs/_archive/...`. Defect escape and false
    FAIL are only knowable after a task has actually merged, which in the
    normal flow (done -> merge_watch -> destroy) is after that move has
    already happened - so every reader here has to see archived history
    too, and a label has to be recordable against an archived task
    [R-METRIC-SCANS-ARCHIVE].
    """

    def test_verdicts_from_a_destroyed_run_still_count(self):
        task = new_task()
        write_verdict(task, "a1", "TIMEOUT", PRODUCER_A)
        run.destroy(task, "local")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["incomplete_check_rate"]
        self.assertTrue(entry["defined"])
        self.assertAlmostEqual(entry["value"], 1.0)

    def test_time_to_acceptance_counts_a_destroyed_done_task(self):
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_B)
        mark_done(task, "a1")
        run.destroy(task, "local")
        report = metrics.compute()
        self.assertTrue(report["overall"]["time_to_acceptance_seconds"]["defined"])

    def test_a_label_recorded_before_destroy_is_still_joined_after(self):
        # rule: R-METRIC-SCANS-ARCHIVE
        task = new_task()
        write_verdict(task, "a1", "PASS", PRODUCER_A)
        metrics.record_label(task, "a1", "defective")
        run.destroy(task, "local")
        report = metrics.compute()
        entry = report["producers"]["claude-code-subagent:opus"]["defect_escape_rate"]
        self.assertTrue(entry["defined"])
        self.assertAlmostEqual(entry["value"], 1.0)

    def test_a_task_can_be_labelled_only_after_it_is_destroyed(self):
        # rule: R-METRIC-SCANS-ARCHIVE
        # This is the exact reproduction that found the defect: label ->
        # destroy -> label again used to raise "no run for ... Run ppl init
        # first" on the second call, because record_label only ever checked
        # the live run directory.
        task = new_task()
        run.destroy(task, "local")
        record = metrics.record_label(task, "c0ffee", "correct")
        self.assertEqual(record["actual"], "correct")

    def test_labelling_a_task_that_truly_never_existed_is_still_refused(self):
        with self.assertRaises(PplError) as ctx:
            metrics.record_label("DEMO-000000", "a1", "defective")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

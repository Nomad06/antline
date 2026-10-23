import itertools
import json
import os
import re
import sys
import unittest
from datetime import datetime, timezone
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


def write_verdict_history(task, sha, outcomes, producer):
    for n, outcome in enumerate(outcomes, 1):
        doc = {
            "task_id": task, "candidate_sha": sha, "spec_revision": "sha256:x",
            "verdict": outcome, "acceptance_evidence": {}, "findings": [],
            "producer": producer,
        }
        store.write_json(paths.run_dir(task) / "verdicts" / sha / ("%d.json" % n), doc)
        store.write_json(paths.run_dir(task) / "verdicts" / ("%s.json" % sha), doc)


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


class TestVerdictHistoryFeedsTheIncompleteRate(IsolatedFleet):
    def test_an_earlier_inconclusive_on_the_same_sha_still_counts(self):
        task = new_task()
        write_verdict_history(task, "a1", ["INCONCLUSIVE", "PASS"], PRODUCER_A)
        entry = metrics.compute()["overall"]["incomplete_check_rate"]
        self.assertAlmostEqual(entry["value"], 0.5)

    def test_the_other_rates_use_only_the_latest_verdict_per_sha(self):
        task = new_task()
        write_verdict_history(task, "a1", ["REWORK", "PASS"], PRODUCER_A)
        metrics.record_label(task, "a1", "correct")
        report = metrics.compute()["overall"]
        self.assertFalse(report["false_fail_rate"]["defined"])
        self.assertEqual(report["defect_escape_rate"]["value"], 0.0)

    def test_the_history_is_sliced_by_each_records_own_producer(self):
        task = new_task()
        write_verdict_history(task, "a1", ["TIMEOUT", "PASS"], PRODUCER_A)
        entry = metrics.compute()["producers"]["claude-code-subagent:opus"]
        self.assertAlmostEqual(entry["incomplete_check_rate"]["value"], 0.5)


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


PRICED = '[prices."m"]\ninput = 1000000\ncache_write = 0\ncache_read = 0\noutput = 0\n'


def write_prices(text):
    paths.config_file().write_text(text, encoding="utf-8")


def log_usage(task, role, agent_id, model, **counts):
    full = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "thinking": 0}
    full.update(counts)
    store.append_log(task, {"event": "usage", "role": role, "agent_id": agent_id,
                            "effort": "high", "models": {model: full}})


def done_run(sha, producer=PRODUCER_A):
    task = new_task()
    write_verdict(task, sha, "PASS", producer)
    mark_done(task, sha)
    return task


class TestCostPerAcceptedTask(IsolatedFleet):
    def test_cost_is_the_mean_dollars_of_accepted_runs_with_usage(self):
        # rule: R-METRIC-COST-NEEDS-PRICES
        write_prices(PRICED)
        log_usage(done_run("a1"), "ppl-code-worker", "w", "m", input=1)
        log_usage(done_run("b1"), "ppl-code-worker", "w", "m", input=3)
        cost = metrics.compute()["overall"]["cost_per_accepted_task"]
        self.assertTrue(cost["defined"])
        self.assertAlmostEqual(cost["value"], 2.0)

    def test_cost_is_undefined_naming_an_unpriced_model(self):
        # rule: R-METRIC-COST-NEEDS-PRICES
        log_usage(done_run("a1"), "ppl-code-worker", "w", "claude-x", input=1)
        cost = metrics.compute()["overall"]["cost_per_accepted_task"]
        self.assertFalse(cost["defined"])
        self.assertIsNone(cost["value"])
        self.assertIn("claude-x", cost["reason"])
        self.assertIn("[prices", cost["reason"])

    def test_cost_is_undefined_without_usage_naming_ppl_setup_claude(self):
        # rule: R-METRIC-SAMPLE-DEPENDENT-UNDEFINED
        write_prices(PRICED)
        done_run("a1")
        report = metrics.compute()["overall"]
        for key in ("cost_per_accepted_task", "tokens_per_accepted_task", "tokens_by_role"):
            self.assertFalse(report[key]["defined"], key)
            self.assertIn("ppl setup claude", report[key]["reason"])

    def test_cost_is_undefined_when_nothing_reached_done(self):
        # rule: R-METRIC-SAMPLE-DEPENDENT-UNDEFINED
        write_prices(PRICED)
        task = new_task()
        log_usage(task, "ppl-code-worker", "w", "m", input=5)
        report = metrics.compute()["overall"]
        for key in ("cost_per_accepted_task", "tokens_per_accepted_task", "tokens_by_role"):
            self.assertFalse(report[key]["defined"], key)
            self.assertIn("reached done", report[key]["reason"])

    def test_tokens_are_defined_without_prices(self):
        log_usage(done_run("a1"), "ppl-code-worker", "w", "m", input=4, output=2)
        log_usage(done_run("b1"), "ppl-code-worker", "w", "m", input=2, cache_read=8)
        report = metrics.compute()["overall"]
        self.assertFalse(report["cost_per_accepted_task"]["defined"])
        tokens = report["tokens_per_accepted_task"]
        self.assertTrue(tokens["defined"])
        self.assertEqual(tokens["value"], {"input": 3.0, "cache_write": 0.0,
                                           "cache_read": 4.0, "output": 1.0})

    def test_tokens_by_role_sums_each_role_over_accepted_runs(self):
        first = done_run("a1")
        log_usage(first, "ppl-code-worker", "w", "m", input=4)
        log_usage(first, "ppl-verifier", "v", "m", output=7)
        log_usage(first, "orchestrator", "orchestrator:s", "m", cache_write=1)
        log_usage(done_run("b1"), "ppl-code-worker", "w", "n", input=6, thinking=5)
        by_role = metrics.compute()["overall"]["tokens_by_role"]
        self.assertTrue(by_role["defined"])
        zero = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}
        self.assertEqual(by_role["value"], {
            "ppl-code-worker": dict(zero, input=10),
            "ppl-verifier": dict(zero, output=7),
            "orchestrator": dict(zero, cache_write=1)})

    def test_a_run_not_done_is_not_counted(self):
        write_prices(PRICED)
        log_usage(done_run("a1"), "ppl-code-worker", "w", "m", input=1)
        open_task = new_task()
        write_verdict(open_task, "z1", "PASS", PRODUCER_A)
        log_usage(open_task, "ppl-code-worker", "w", "m", input=100)
        report = metrics.compute()["overall"]
        self.assertAlmostEqual(report["cost_per_accepted_task"]["value"], 1.0)
        self.assertEqual(report["tokens_per_accepted_task"]["value"]["input"], 1.0)

    def test_a_producer_slice_counts_only_runs_it_accepted(self):
        write_prices(PRICED)
        log_usage(done_run("a1", PRODUCER_A), "ppl-code-worker", "w", "m", input=1)
        log_usage(done_run("b1", PRODUCER_B), "ppl-code-worker", "w", "m", input=5)
        report = metrics.compute()
        self.assertAlmostEqual(report["overall"]["cost_per_accepted_task"]["value"], 3.0)
        sliced = report["producers"]["claude-code-subagent:sonnet"]
        self.assertAlmostEqual(sliced["cost_per_accepted_task"]["value"], 5.0)
        self.assertEqual(sliced["tokens_by_role"]["value"]["ppl-code-worker"]["input"], 5)

    def test_cost_is_never_zero_for_no_data(self):
        # rule: R-METRIC-COST-NEEDS-PRICES
        report = metrics.compute()["overall"]
        for key in ("cost_per_accepted_task", "tokens_per_accepted_task", "tokens_by_role"):
            self.assertIsNone(report[key]["value"], key)
            self.assertFalse(report[key]["defined"], key)


class TestStructurallyUndefinedMetrics(IsolatedFleet):
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


class TestMetricsDocumentation(unittest.TestCase):
    def test_the_docstring_lists_exactly_the_metrics_entry_returns(self):
        listed = set()
        for line in metrics.__doc__.splitlines():
            if line.startswith("- **"):
                listed.update(name.replace(" ", "_").lower()
                              for name in re.findall(r"\*\*([A-Za-z ]+)\*\*", line))
        returned = set(key.removesuffix("_seconds") for key in metrics._entry([], [], None))
        self.assertEqual(listed, returned)



PRESET_COUNTER = itertools.count(5000)


def done_preset_run(preset, minute, **counts):
    task = "DEMO-%d" % next(PRESET_COUNTER)
    run.init(task, "u", "/tmp/repo")
    write_verdict(task, "c%d" % next(PRESET_COUNTER), "PASS", PRODUCER_A)
    mark_done(task, "c%d" % next(PRESET_COUNTER))
    log_usage(task, "ppl-code-worker", "w", "m", **counts)
    state = run.load(task)
    state["routing_preset"] = preset
    run.save(state)
    directory = paths.run_dir(task)
    events = store.read_log_at(directory)
    for event in events:
        if event["event"] == "init":
            event["at"] = "2026-10-01T10:%02d:00Z" % minute
    (directory / "log.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")
    return task


class TestPresetCosts(IsolatedFleet):
    def setUp(self):
        self._home = os.environ["PPL_HOME"]
        super().setUp()

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def test_preset_costs_average_the_newest_ten_done_runs(self):
        write_prices(PRICED)
        for minute in range(12):
            done_preset_run("balanced", minute, input=minute + 1)
        costs = metrics.preset_costs()
        self.assertEqual(costs["balanced"]["runs"], 10)
        self.assertAlmostEqual(costs["balanced"]["tokens_mean"], 7.5)
        self.assertAlmostEqual(costs["balanced"]["usd_mean"], 7.5)
        self.assertEqual(costs["economy"]["runs"], 0)

    def test_preset_costs_leave_usd_undefined_when_a_model_is_unpriced(self):
        done_preset_run("economy", 1, input=10)
        costs = metrics.preset_costs()
        self.assertEqual(costs["economy"]["tokens_mean"], 10)
        self.assertIsNone(costs["economy"]["usd_mean"])

    def test_preset_costs_skip_runs_without_a_preset_or_usage_or_done(self):
        write_prices(PRICED)
        done_preset_run(None, 1, input=1)
        done_preset_run("custom", 2, input=1)
        task = "DEMO-%d" % next(PRESET_COUNTER)
        run.init(task, "u", "/tmp/repo")
        state = run.load(task)
        state["routing_preset"] = "max"
        run.save(state)
        log_usage(task, "ppl-code-worker", "w", "m", input=5)
        quiet = "DEMO-%d" % next(PRESET_COUNTER)
        run.init(quiet, "u", "/tmp/repo")
        mark_done(quiet, "q1")
        state = run.load(quiet)
        state["routing_preset"] = "max"
        run.save(state)
        costs = metrics.preset_costs()
        self.assertEqual(costs["custom"]["runs"], 1)
        self.assertEqual(costs["max"]["runs"], 0)
        self.assertIsNone(costs["max"]["tokens_mean"])

    def test_preset_costs_name_the_unpriced_models(self):
        done_preset_run("economy", 1, input=10)
        entry = metrics.preset_costs()["economy"]
        self.assertEqual(entry["unpriced"], ["m"])
        self.assertIn("no price for m", entry["usd_reason"])

    def test_preset_costs_skip_a_run_that_cannot_be_read(self):
        write_prices(PRICED)
        done_preset_run("balanced", 1, input=4)
        bad_state = done_preset_run("balanced", 2, input=100)
        bad_log = done_preset_run("balanced", 3, input=100)
        (paths.run_dir(bad_state) / "state.json").write_text("{not json", encoding="utf-8")
        (paths.run_dir(bad_log) / "log.jsonl").write_text("{not json\n", encoding="utf-8")
        entry = metrics.preset_costs()["balanced"]
        self.assertEqual(entry["runs"], 1)
        self.assertEqual(entry["tokens_mean"], 4)

    def test_preset_costs_keep_tokens_when_the_prices_table_is_invalid(self):
        done_preset_run("balanced", 1, input=8)
        write_prices('[prices."m"]\ninput = "x"\n')
        entry = metrics.preset_costs()["balanced"]
        self.assertEqual(entry["tokens_mean"], 8)
        self.assertIsNone(entry["usd_mean"])
        self.assertIn("prices.m", entry["usd_reason"])

WARN_COUNTER = itertools.count(9000)
OLD = "2026-09-01T00:00:00+00:00"
YOUNG = "2026-10-20T00:00:00+00:00"
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)


def warn_task():
    task = "DEMO-%d" % next(WARN_COUNTER)
    run.init(task, "u", "/tmp/repo")
    return task


def raw_log(task, event):
    with open(paths.run_dir(task) / "log.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def verdicts(outcomes, producer=PRODUCER_A, at=None):
    """One run holding one verdict per outcome, each on its own SHA. With `at`
    the matching `verdict` log events are dated."""
    task = warn_task()
    for outcome in outcomes:
        sha = "%x" % next(WARN_COUNTER)
        write_verdict(task, sha, outcome, producer)
        if at:
            raw_log(task, {"event": "verdict", "sha": sha, "verdict": outcome, "at": at})
    return task


def park(task, blocker):
    run.park(task, blocker, "next", [])


def deny(task, role):
    store.append_log(task, {"event": "deny", "role": role, "tool": "Edit",
                            "code": "GUARD", "target": "a.py"})


def refuse(task, role, counted=True):
    store.append_log(task, {"event": "refusal", "command": "ppl x", "code": "SCHEMA_INVALID",
                            "message": "m", "role": role, "lease_generation": 1,
                            "counted": counted})


def spend_retries(task):
    state = run.load(task)
    state["budgets"]["retry_used"] = state["budgets"]["retry_max"]
    run.save(state)


def found(report, code, producer=None):
    return [w for w in report["warnings"] if w["code"] == code and w["producer"] == producer]


class TestWarnings(IsolatedFleet):
    def test_verifier_never_reworks_fires_at_its_minimum_sample(self):
        verdicts(["PASS"] * 10)
        got = found(metrics.compute(), "VERIFIER_NEVER_REWORKS")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"], {"n": 10, "value": 1.0, "threshold": 1.0})
        self.assertTrue(got[0]["message"])

    def test_verifier_never_reworks_is_silent_one_sample_below(self):
        verdicts(["PASS"] * 9)
        self.assertEqual(found(metrics.compute(), "VERIFIER_NEVER_REWORKS"), [])

    def test_high_incomplete_checks_fires_at_its_minimum_sample(self):
        verdicts(["PASS"] * 6 + ["TIMEOUT"] * 2)
        got = found(metrics.compute(), "HIGH_INCOMPLETE_CHECKS")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"], {"n": 8, "value": 0.25, "threshold": 0.25})

    def test_high_incomplete_checks_is_silent_one_sample_below(self):
        verdicts(["PASS"] * 5 + ["TIMEOUT"] * 2)
        self.assertEqual(found(metrics.compute(), "HIGH_INCOMPLETE_CHECKS"), [])

    def test_retry_budget_often_spent_fires_at_its_minimum_sample(self):
        tasks = [warn_task() for _ in range(6)]
        for task in tasks[:2]:
            spend_retries(task)
        got = found(metrics.compute(), "RETRY_BUDGET_OFTEN_SPENT")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"]["n"], 6)
        self.assertAlmostEqual(got[0]["evidence"]["value"], 2 / 6)
        self.assertEqual(got[0]["evidence"]["threshold"], 0.30)

    def test_retry_budget_often_spent_is_silent_one_sample_below(self):
        for _ in range(5):
            spend_retries(warn_task())
        self.assertEqual(found(metrics.compute(), "RETRY_BUDGET_OFTEN_SPENT"), [])

    def test_parks_one_cause_fires_at_its_minimum_sample(self):
        for blocker in ["FORMAT_RETRY_EXHAUSTED"] * 3 + ["SPEC_GAP", "TEST_FAILURE", "TIMED_OUT"]:
            park(warn_task(), blocker)
        got = found(metrics.compute(), "PARKS_ONE_CAUSE")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"], {"n": 6, "value": 0.5, "threshold": 0.5})

    def test_parks_one_cause_is_silent_one_sample_below(self):
        for _ in range(5):
            park(warn_task(), "SPEC_GAP")
        self.assertEqual(found(metrics.compute(), "PARKS_ONE_CAUSE"), [])

    def test_parks_one_cause_names_the_blocker_code(self):
        for blocker in ["FORMAT_RETRY_EXHAUSTED"] * 3 + ["SPEC_GAP", "TEST_FAILURE", "TIMED_OUT"]:
            park(warn_task(), blocker)
        message = found(metrics.compute(), "PARKS_ONE_CAUSE")[0]["message"]
        self.assertIn("FORMAT_RETRY_EXHAUSTED", message)

    def test_labels_missing_fires_at_its_minimum_sample(self):
        task = verdicts(["PASS"] * 6, at=OLD)
        for sha in sorted(d["candidate_sha"] for d in metrics._verdicts(paths.run_dir(task)))[:3]:
            metrics.record_label(task, sha, "correct")
        got = found(metrics.compute(now=NOW), "LABELS_MISSING")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"], {"n": 6, "value": 0.5, "threshold": 0.5})

    def test_labels_missing_is_silent_one_sample_below(self):
        verdicts(["PASS"] * 5, at=OLD)
        self.assertEqual(found(metrics.compute(now=NOW), "LABELS_MISSING"), [])

    def test_labels_missing_ignores_passes_younger_than_fourteen_days(self):
        verdicts(["PASS"] * 6, at=OLD)
        verdicts(["PASS"] * 4, at=YOUNG)
        got = found(metrics.compute(now=NOW), "LABELS_MISSING")
        self.assertEqual(got[0]["evidence"]["n"], 6)

    def test_labels_missing_cannot_date_a_pass_without_a_verdict_event(self):
        verdicts(["PASS"] * 8)
        self.assertEqual(found(metrics.compute(now=NOW), "LABELS_MISSING"), [])

    def test_format_refusals_fires_at_its_minimum_sample(self):
        tasks = [warn_task() for _ in range(5)]
        for task in tasks:
            refuse(task, "ppl-code-worker")
            refuse(task, "ppl-code-worker")
        for _ in range(3):
            refuse(tasks[0], "ppl-code-worker", counted=False)
        got = found(metrics.compute(), "FORMAT_REFUSALS")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"], {"n": 5, "value": 2.0, "threshold": 2.0})

    def test_format_refusals_is_silent_one_sample_below(self):
        for _ in range(4):
            task = warn_task()
            refuse(task, "ppl-code-worker")
            refuse(task, "ppl-code-worker")
        self.assertEqual(found(metrics.compute(), "FORMAT_REFUSALS"), [])

    def test_format_refusals_names_the_role(self):
        for _ in range(5):
            task = warn_task()
            refuse(task, "ppl-test-worker")
            refuse(task, "ppl-test-worker")
            refuse(task, "ppl-code-worker")
        message = found(metrics.compute(), "FORMAT_REFUSALS")[0]["message"]
        self.assertIn("ppl-test-worker", message)

    def test_cage_hits_high_fires_at_its_minimum_sample(self):
        tasks = [warn_task() for _ in range(5)]
        for task in tasks:
            for _ in range(5):
                deny(task, "ppl-code-worker")
        for _ in range(10):
            deny(tasks[0], None)
            deny(tasks[0], "not-a-role")
        got = found(metrics.compute(), "CAGE_HITS_HIGH")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["evidence"], {"n": 5, "value": 5.0, "threshold": 5.0})

    def test_cage_hits_high_is_silent_one_sample_below(self):
        for _ in range(4):
            task = warn_task()
            for _ in range(5):
                deny(task, "ppl-code-worker")
        self.assertEqual(found(metrics.compute(), "CAGE_HITS_HIGH"), [])

    def test_cage_hits_high_names_the_role(self):
        for _ in range(5):
            task = warn_task()
            for _ in range(5):
                deny(task, "ppl-verifier")
        message = found(metrics.compute(), "CAGE_HITS_HIGH")[0]["message"]
        self.assertIn("ppl-verifier", message)

    def test_a_verdict_warning_fires_per_producer(self):
        verdicts(["PASS"] * 10, PRODUCER_A)
        verdicts(["REWORK"] * 5, PRODUCER_B)
        report = metrics.compute()
        self.assertEqual(found(report, "VERIFIER_NEVER_REWORKS"), [])
        got = found(report, "VERIFIER_NEVER_REWORKS", "claude-code-subagent:opus")
        self.assertEqual(len(got), 1)
        self.assertEqual([w["producer"] for w in report["warnings"]],
                         ["claude-code-subagent:opus"])

    def test_run_based_warnings_carry_no_producer(self):
        for _ in range(6):
            park(warn_task(), "SPEC_GAP")
        self.assertEqual([w["producer"] for w in metrics.compute()["warnings"]], [None])

    def test_warnings_are_sorted_by_code_then_producer(self):
        verdicts(["PASS"] * 10, PRODUCER_A)
        verdicts(["PASS"] * 10, PRODUCER_B)
        for _ in range(6):
            park(warn_task(), "SPEC_GAP")
        keys = [(w["code"], w["producer"] or "") for w in metrics.compute()["warnings"]]
        self.assertEqual(keys, sorted(keys))
        self.assertGreater(len(keys), 3)

    def test_no_data_means_no_warnings(self):
        self.assertEqual(metrics.compute()["warnings"], [])

    def test_a_config_override_moves_a_threshold(self):
        verdicts(["PASS"] * 3)
        self.assertEqual(found(metrics.compute(), "VERIFIER_NEVER_REWORKS"), [])
        paths.config_file().write_text(
            "[metrics]\nverifier_never_reworks_min_sample = 3\n", encoding="utf-8")
        self.assertEqual(metrics.thresholds()["VERIFIER_NEVER_REWORKS"], (1.0, 3))
        got = found(metrics.compute(), "VERIFIER_NEVER_REWORKS")
        self.assertEqual(got[0]["evidence"], {"n": 3, "value": 1.0, "threshold": 1.0})

    def test_an_unknown_metrics_key_is_refused_naming_the_known_keys(self):
        paths.config_file().write_text("[metrics]\nbogus = 1\n", encoding="utf-8")
        with self.assertRaises(PplError) as ctx:
            metrics.compute()
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("bogus", ctx.exception.message)
        self.assertIn("verifier_never_reworks_threshold", ctx.exception.message)

    def test_a_negative_or_non_number_override_is_refused(self):
        for line in ("cage_hits_high_threshold = -1", 'cage_hits_high_min_sample = "5"',
                     "cage_hits_high_threshold = true"):
            with self.subTest(line=line):
                paths.config_file().write_text("[metrics]\n%s\n" % line, encoding="utf-8")
                with self.assertRaises(PplError) as ctx:
                    metrics.thresholds()
                self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_warnings_skip_unreadable_runs(self):
        for _ in range(4):
            spend_retries(warn_task())
        broken = warn_task()
        state = store.read_json(paths.run_dir(broken) / "state.json")
        del state["budgets"]
        store.write_json(paths.run_dir(broken) / "state.json", state)
        with open(paths.run_dir(broken) / "log.jsonl", "a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        verdicts(["PASS"] * 10)
        report = metrics.compute()
        self.assertEqual(found(report, "RETRY_BUDGET_OFTEN_SPENT"), [])
        self.assertEqual(len(found(report, "VERIFIER_NEVER_REWORKS")), 1)

    def test_a_bad_prices_table_leaves_cost_undefined_and_the_rest_printed(self):
        task = done_run("a1")
        log_usage(task, "ppl-code-worker", "w", "m", input=4)
        write_prices('[prices."m"]\ninput = "free"\n')
        report = metrics.compute()
        cost = report["overall"]["cost_per_accepted_task"]
        self.assertFalse(cost["defined"])
        self.assertIsNone(cost["value"])
        self.assertIn("prices.m", cost["reason"])
        self.assertTrue(report["overall"]["tokens_per_accepted_task"]["defined"])
        self.assertIn("warnings", report)


class TestMetricsText(IsolatedFleet):
    def test_text_prints_a_line_per_metric_then_the_warnings(self):
        verdicts(["PASS"] * 10)
        report = metrics.compute()
        lines = metrics.text(report).splitlines()
        self.assertEqual(len(lines), len(report["overall"]) + len(report["warnings"]))
        self.assertTrue(any(line.startswith("cost_per_accepted_task: undefined (")
                            for line in lines))
        self.assertIn("incomplete_check_rate: 0.0", lines)
        self.assertTrue(lines[-1].startswith("WARNING VERIFIER_NEVER_REWORKS "))

    def test_text_names_the_producer_of_a_producer_warning(self):
        verdicts(["PASS"] * 10, PRODUCER_A)
        verdicts(["REWORK"] * 5, PRODUCER_B)
        line = metrics.text(metrics.compute()).splitlines()[-1]
        self.assertTrue(line.startswith(
            "WARNING VERIFIER_NEVER_REWORKS claude-code-subagent:opus: "), line)


if __name__ == "__main__":
    unittest.main()

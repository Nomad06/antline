import json
import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import fsm, run, schema
from ppl.errors import PplError


def record_challenge(task_id):
    """Stamps an ACCEPTED brief challenge bound to the run's current
    spec_revision (binding a placeholder revision first when the fixture
    never ran `ppl brief set`), the minimal way `ppl brief challenge` would
    after validating the report [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION].
    Keeps fixtures unrelated to that rule out of its way.
    """
    state = run.load(task_id)
    if state.get("spec_revision") is None:
        state["spec_revision"] = "sha256:" + "0" * 64
    state["brief_challenge"] = {"spec_revision": state["spec_revision"],
                                "verdict": "ACCEPTED", "at": "2026-01-01T00:00:00+00:00"}
    run.save(state)


def record_baseline(task_id, sha="a" * 40):
    """E1, cycle 2 wave E: stage_enter now refuses to leave "baseline"
    without a recorded base_sha [R-BASELINE-RECORDED-BEFORE-LEAVING]. These
    are unit tests of run.py directly (no real git repo, no `ppl baseline
    record` call), so the base_sha is stamped onto state the same minimal
    way `ppl baseline record` itself would after validating it - this
    helper exists purely to keep fixtures unrelated to that rule out of its
    way, not to re-test it.
    """
    state = run.load(task_id)
    state["base_sha"] = sha
    run.save(state)


class TestSchema(unittest.TestCase):
    def test_brief_requires_acceptance_and_boundary(self):
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief({"purpose": "x"})
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_brief_accepts_a_complete_document(self):
        schema.validate_brief({
            "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
            "governing_requirements": ["REQ-1"], "acceptance": ["AC-1"],
            "allowed_write_paths": ["src/**"], "forbidden_paths": ["tests/**"],
            "verification": ["focused-tests"], "open_decisions": [],
            "name_contract": {"Foo.bar(int)": "returns long, throws on negative"},
        })

    def test_brief_rejects_empty_write_boundary(self):
        # rule: R-BRIEF-SCHEMA
        with self.assertRaises(PplError):
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": ["AC-1"],
                "allowed_write_paths": [], "forbidden_paths": [],
                "verification": ["focused-tests"], "open_decisions": [],
                "name_contract": {"Foo.bar": "int"},
            })

    def test_brief_rejects_overlap_between_write_and_forbid(self):
        # rule: R-BRIEF-SCHEMA
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": ["AC-1"],
                "allowed_write_paths": ["src/**"], "forbidden_paths": ["src/**"],
                "verification": ["focused-tests"], "open_decisions": [],
                "name_contract": {"Foo.bar": "int"},
            })
        self.assertIn("overlap", ctx.exception.message)

    def test_brief_rejects_missing_name_contract(self):
        # rule: R-BRIEF-SCHEMA
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": ["AC-1"],
                "allowed_write_paths": ["src/**"], "forbidden_paths": [],
                "verification": ["focused-tests"], "open_decisions": [],
            })
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("name_contract", ctx.exception.message)

    def test_brief_rejects_an_empty_name_contract(self):
        # rule: R-BRIEF-SCHEMA
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": ["AC-1"],
                "allowed_write_paths": ["src/**"], "forbidden_paths": [],
                "verification": ["focused-tests"], "open_decisions": [],
                "name_contract": {},
            })
        self.assertIn("name_contract", ctx.exception.message)

    def test_brief_rejects_a_dict_shaped_acceptance_entry(self):
        # rule: R-BRIEF-SCHEMA (E1 CRITICAL, cycle 2 wave B). Only presence
        # (a non-empty list) was ever checked - the elements' shape was not.
        # `[{"id": "AC1", "text": "..."}]` is the obvious reading of
        # "acceptance criteria" when nothing documents the shape, and it used
        # to be accepted here only to blow up as a raw TypeError five stages
        # later, inside done_blockers's `acceptance_id not in proven` set
        # membership test, at exit 1 rather than a routable exit 2.
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": [{"id": "AC1", "text": "x"}],
                "allowed_write_paths": ["src/**"], "forbidden_paths": [],
                "verification": ["focused-tests"], "open_decisions": [],
                "name_contract": {"Foo.bar": "int"},
            })
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("acceptance", ctx.exception.message)

    def test_brief_rejects_a_list_shaped_acceptance_entry(self):
        # rule: R-BRIEF-SCHEMA
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": [["AC1"]],
                "allowed_write_paths": ["src/**"], "forbidden_paths": [],
                "verification": ["focused-tests"], "open_decisions": [],
                "name_contract": {"Foo.bar": "int"},
            })
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_brief_rejects_an_empty_string_acceptance_entry(self):
        # rule: R-BRIEF-SCHEMA
        with self.assertRaises(PplError):
            schema.validate_brief({
                "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
                "governing_requirements": [], "acceptance": ["AC-1", "  "],
                "allowed_write_paths": ["src/**"], "forbidden_paths": [],
                "verification": ["focused-tests"], "open_decisions": [],
                "name_contract": {"Foo.bar": "int"},
            })

    def test_spec_revision_is_deterministic_and_order_independent(self):
        first = schema.spec_revision({"a": 1, "b": 2}, ["AC-1"], ["src/**"], [])
        second = schema.spec_revision({"b": 2, "a": 1}, ["AC-1"], ["src/**"], [])
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("sha256:"))

    def test_spec_revision_changes_when_the_contract_changes(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE
        self.assertNotEqual(
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], []),
            schema.spec_revision({"Foo.bar": "long"}, ["AC-1"], ["src/**"], []))

    def test_spec_revision_changes_when_acceptance_shrinks(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE, widened cycle 2 wave F. THE
        # DEFECT: this used to hash name_contract alone, so a re-brief that
        # dropped an unproven acceptance id left spec_revision untouched and
        # every outstanding verdict looking current - the done gate proves
        # the acceptance ids that are currently listed, and the list was
        # editable by the same session being gated.
        self.assertNotEqual(
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1", "AC-2"], ["src/**"], []),
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], []))

    def test_spec_revision_changes_when_the_write_boundary_widens(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE, widened cycle 2 wave F (reopens
        # N5, cycle 2 wave B). Widening the write boundary after a PASS is
        # the same shape as the acceptance shrink above: a criterion the done
        # gate re-reads fresh, rewritten by the same session being gated to
        # fit a diff that already exists.
        self.assertNotEqual(
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], []),
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**", "other/**"], []))

    def test_spec_revision_changes_when_the_forbid_list_changes(self):
        self.assertNotEqual(
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], []),
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], ["src/secret/**"]))

    def test_spec_revision_unchanged_when_only_non_gate_fields_would_differ(self):
        # purpose/success_outcome/governing_requirements/verification/
        # open_decisions are not part of the signature at all - they are
        # never read by verdict.done_blockers, so a re-brief that only
        # corrects one of them must not stale an outstanding verdict. This is
        # a same-inputs-same-output check standing in for that: the function
        # has no parameter for any of those fields, so there is nothing that
        # could make two calls with identical gate-relevant fields diverge.
        self.assertEqual(
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], []),
            schema.spec_revision({"Foo.bar": "int"}, ["AC-1"], ["src/**"], []))

    def test_verdict_accepts_a_complete_document(self):
        schema.validate_verdict({
            "task_id": "DEMO-1", "candidate_sha": "abc123", "spec_revision": "sha256:x",
            "verdict": "PASS", "acceptance_evidence": {"AC-1": "evidence:focused-tests"},
            "findings": [], "producer": {"role": "verifier"},
        })

    def test_verdict_rejects_an_out_of_enum_value(self):
        # rule: R-VERDICT-OUTCOME-VOCAB
        with self.assertRaises(PplError) as ctx:
            schema.validate_verdict({
                "task_id": "DEMO-1", "candidate_sha": "abc123", "spec_revision": "sha256:x",
                "verdict": "LOOKS_GOOD", "acceptance_evidence": {}, "findings": [],
                "producer": {"role": "verifier"},
            })
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_verdict_rejects_a_non_empty_inline_findings_list(self):
        # rule: R-FINDINGS-VERDICT-FIELD-RETIRED
        with self.assertRaises(PplError) as ctx:
            schema.validate_verdict({
                "task_id": "DEMO-1", "candidate_sha": "abc123", "spec_revision": "sha256:x",
                "verdict": "REWORK", "acceptance_evidence": {},
                "findings": [{"severity": "blocking", "note": "inline, the old shape"}],
                "producer": {"role": "verifier"},
            })
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("ppl findings add", ctx.exception.message)

    def test_verdict_accepts_each_check_did_not_happen_outcome(self):
        # rule: R-VERDICT-OUTCOME-VOCAB
        for outcome in ("INCONCLUSIVE", "ERROR", "CANCELLED", "TIMEOUT"):
            with self.subTest(outcome=outcome):
                schema.validate_verdict({
                    "task_id": "DEMO-1", "candidate_sha": "abc123",
                    "spec_revision": "sha256:x", "verdict": outcome,
                    "acceptance_evidence": {}, "findings": [],
                    "producer": {"role": "verifier"},
                })


class TestRun(unittest.TestCase):
    def test_init_creates_state_at_admission(self):
        state = run.init("DEMO-100", "https://j/DEMO-100", "/tmp/repo")
        self.assertEqual(state["stage"], "admission")
        self.assertEqual(state["status"], "planned")
        self.assertEqual(run.active(), "DEMO-100")

    def test_init_records_orchestrator_identity(self):
        state = run.init("DEMO-101", "https://j/DEMO-101", "/tmp/repo")
        self.assertEqual(state["orchestrator"]["pid"], os.getpid())
        self.assertTrue(state["orchestrator"]["started_at"])

    def test_init_twice_is_refused(self):
        # rule: R-INIT-ONCE
        run.init("DEMO-102", "https://j/DEMO-102", "/tmp/repo")
        with self.assertRaises(PplError) as ctx:
            run.init("DEMO-102", "https://j/DEMO-102", "/tmp/repo")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_stage_enter_follows_the_graph(self):
        run.init("DEMO-103", "https://j/DEMO-103", "/tmp/repo")
        state = run.stage_enter("DEMO-103", "baseline")
        self.assertEqual(state["stage"], "baseline")

    def test_entering_baseline_starts_the_task(self):
        run.init("DEMO-106", "https://j/DEMO-106", "/tmp/repo")
        state = run.stage_enter("DEMO-106", "baseline")
        self.assertEqual(state["status"], "in_progress")

    def test_stage_enter_rejects_an_illegal_jump(self):
        # rule: R-FSM-STAGE-GRAPH
        run.init("DEMO-104", "https://j/DEMO-104", "/tmp/repo")
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-104", "mr")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_stage_enter_logs_the_transition(self):
        run.init("DEMO-105", "https://j/DEMO-105", "/tmp/repo")
        run.stage_enter("DEMO-105", "baseline")
        from ppl import paths
        text = (paths.run_dir("DEMO-105") / "log.jsonl").read_text()
        self.assertIn("stage_enter", text)

    def test_load_missing_run_raises(self):
        with self.assertRaises(PplError) as ctx:
            run.load("DEMO-999")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_init_defaults_isolated_true(self):
        state = run.init("DEMO-107", "https://j/DEMO-107", "/tmp/repo")
        self.assertIs(state["isolated"], True)

    def test_init_can_be_told_not_isolated(self):
        state = run.init("DEMO-108", "https://j/DEMO-108", "/tmp/repo", isolated=False)
        self.assertIs(state["isolated"], False)

    def test_init_records_the_tracker_url_and_no_harness_by_default(self):
        state = run.init("DEMO-109", "https://j/DEMO-109", "/tmp/repo")
        self.assertEqual(state["tracker_url"], "https://j/DEMO-109")
        self.assertIsNone(state["harness"])
        self.assertEqual(run.load("DEMO-109")["harness"], None)

    def test_init_records_the_harness_it_was_given_in_state_and_log(self):
        from ppl import paths
        record = {"name": "example-maven", "path": "/h/example-maven", "format": 1}
        state = run.init("DEMO-110", "https://j/DEMO-110", "/tmp/repo", harness=record)
        self.assertEqual(state["harness"], record)
        self.assertEqual(run.load("DEMO-110")["harness"], record)
        text = (paths.run_dir("DEMO-110") / "log.jsonl").read_text(encoding="utf-8")
        event = json.loads(text.splitlines()[0])
        self.assertEqual((event["event"], event["harness"]), ("init", "example-maven"))
        self.assertEqual(event["tracker_url"], "https://j/DEMO-110")


class TestMergeWatchAfterDone(unittest.TestCase):
    """closure -> merge_watch is a legal edge of the stage graph, but a run that took it
    before `ppl transition done` sat in merge_watch with status in_progress, with no way
    back to closure and the tracker gate one `ppl merge observed` away."""

    def at_closure(self, task_id, status):
        run.init(task_id, "https://j/%s" % task_id, "/tmp/repo")
        state = run.load(task_id)
        state["stage"] = "closure"
        state["status"] = status
        run.save(state)

    def test_entering_merge_watch_before_the_run_is_done_is_refused(self):
        # rule: R-MERGE-WATCH-AFTER-DONE
        self.at_closure("DEMO-5820", "in_progress")
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-5820", "merge_watch")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        self.assertIn("ppl transition done", ctx.exception.message)
        state = run.load("DEMO-5820")
        self.assertEqual((state["stage"], state["status"]), ("closure", "in_progress"))

    def test_entering_merge_watch_once_the_run_is_done_is_allowed(self):
        self.at_closure("DEMO-5821", "done")
        state = run.stage_enter("DEMO-5821", "merge_watch")
        self.assertEqual((state["stage"], state["status"]), ("merge_watch", "done"))


class TestLease(unittest.TestCase):
    """The lease and its fencing generation - R-LEASE-STALE-WRITE."""

    def test_init_starts_at_generation_one(self):
        state = run.init("DEMO-750", "https://j/DEMO-750", "/tmp/repo")
        self.assertEqual(state["lease_generation"], 1)
        self.assertIsNone(state["attempt_id"])

    def test_stage_enter_mints_a_fresh_attempt_id_without_moving_the_generation(self):
        run.init("DEMO-751", "https://j/DEMO-751", "/tmp/repo")
        first = run.stage_enter("DEMO-751", "baseline")
        self.assertIsNotNone(first["attempt_id"])
        record_baseline("DEMO-751")
        second = run.stage_enter("DEMO-751", "briefing")
        self.assertNotEqual(first["attempt_id"], second["attempt_id"])
        self.assertEqual(first["lease_generation"], 1)
        self.assertEqual(second["lease_generation"], 1)

    def test_check_generation_accepts_the_current_generation(self):
        run.init("DEMO-752", "https://j/DEMO-752", "/tmp/repo")
        # does not raise
        run.check_generation("DEMO-752", 1)

    def test_check_generation_rejects_a_stale_generation(self):
        # rule: R-LEASE-STALE-WRITE
        run.init("DEMO-753", "https://j/DEMO-753", "/tmp/repo")
        run.bump_generation("DEMO-753")
        with self.assertRaises(PplError) as ctx:
            run.check_generation("DEMO-753", 1)
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        self.assertIn("stale attempt", ctx.exception.message)

    def test_bump_generation_increments_and_persists(self):
        run.init("DEMO-754", "https://j/DEMO-754", "/tmp/repo")
        self.assertEqual(run.bump_generation("DEMO-754"), 2)
        self.assertEqual(run.load("DEMO-754")["lease_generation"], 2)
        self.assertEqual(run.bump_generation("DEMO-754"), 3)


class TestDeadline(unittest.TestCase):
    """stage_enter records a deadline - R-WATCHDOG-DEADLINE."""

    def test_timed_out_joins_the_blocker_codes(self):
        self.assertIn("TIMED_OUT", run.BLOCKER_CODES)

    def test_verification_unavailable_joins_the_blocker_codes(self):
        # rule: R-VERIFICATION-UNAVAILABLE-PARK
        self.assertIn("VERIFICATION_UNAVAILABLE", run.BLOCKER_CODES)

    def test_init_stamps_a_deadline_on_admission(self):
        state = run.init("DEMO-760", "https://j/DEMO-760", "/tmp/repo")
        self.assertIsNotNone(state["deadline"])

    def test_stage_enter_stamps_a_fresh_deadline(self):
        run.init("DEMO-761", "https://j/DEMO-761", "/tmp/repo")
        before = run.load("DEMO-761")["deadline"]
        state = run.stage_enter("DEMO-761", "baseline")
        self.assertIsNotNone(state["deadline"])
        self.assertGreaterEqual(state["deadline"], before)

    def test_park_accepts_timed_out_as_a_blocker(self):
        run.init("DEMO-762", "https://j/DEMO-762", "/tmp/repo")
        state = run.park("DEMO-762", "TIMED_OUT", "inspect the stuck attempt", [])
        self.assertEqual(state["stage"], "parking")

    def test_format_retry_exhausted_joins_the_blocker_codes(self):
        self.assertIn("FORMAT_RETRY_EXHAUSTED", run.BLOCKER_CODES)

    def test_park_keeps_the_refusals_it_is_given_in_the_parking_record(self):
        from ppl import paths, store
        run.init("DEMO-763", "https://j/DEMO-763", "/tmp/repo")
        refusals = [{"command": "brief set", "role": "ppl-briefer", "message": "one"},
                    {"command": "brief set", "role": "ppl-briefer", "message": "two"}]
        run.park("DEMO-763", "FORMAT_RETRY_EXHAUSTED", "inspect both refusals", [],
                 refusals=refusals)
        record = store.read_json(paths.run_dir("DEMO-763") / "parking.json")
        self.assertEqual(record["blocker"], "FORMAT_RETRY_EXHAUSTED")
        self.assertEqual(record["refusals"], refusals)
        run.park("DEMO-763", "OWNER_DECISION", "operator stop", [])
        self.assertNotIn("refusals", store.read_json(paths.run_dir("DEMO-763") / "parking.json"))


class TestResumeFromParking(unittest.TestCase):
    """W3, cycle 2 wave B: a parked run's lifecycle status is `blocked`
    (fsm._LIFECYCLE_EDGES["blocked"] = {"in_progress", "failed"}), and
    `blocked -> done` is illegal. The documented resume path
    (`ppl stage enter <resume_stage>`, which the stage graph already allows
    from "parking" - `_FORWARD["parking"] = set(STAGES) - {"merge_watch"}`)
    used to leave status stuck at `blocked` forever: nothing ever moved it
    back to `in_progress`, and the string `in_progress` appears nowhere in
    skill/, so an orchestrator following only the skill had no
    documented way out either. Every BLOCKED verdict, every `ppl escalate`,
    every watchdog TIMED_OUT and every operator stop hit this. Fixed in code
    (`stage_enter` itself), not in the skill: a recovery path that depends on
    the operator remembering an undocumented `ppl transition in_progress`
    call is not a recovery path.
    """

    def test_resuming_from_parking_restores_in_progress(self):
        run.init("DEMO-770", "https://j/DEMO-770", "/tmp/repo")
        run.stage_enter("DEMO-770", "baseline")
        record_baseline("DEMO-770")
        run.stage_enter("DEMO-770", "briefing")
        record_challenge("DEMO-770")
        run.stage_enter("DEMO-770", "implementation")
        run.park("DEMO-770", "IMPLEMENTATION_FAILURE", "inspect and redispatch", [])
        self.assertEqual(run.load("DEMO-770")["status"], "blocked")
        state = run.stage_enter("DEMO-770", "implementation")
        self.assertEqual(state["stage"], "implementation")
        self.assertEqual(state["status"], "in_progress")
        # The lifecycle FSM itself now accepts the terminal transition -
        # blocked -> done is illegal, in_progress -> done is not.
        fsm.check_lifecycle(state["status"], "done")

    def test_resuming_from_parking_before_baseline_does_not_fabricate_in_progress(self):
        # rule: R-PARK-RESUME-RESTORES-LIFECYCLE
        # A run parked at (or before) admission - e.g. escalated with
        # SPEC_GAP before baseline ever ran - was never in_progress and must
        # not be lied into it by the same resume path. status stays
        # `planned`, exactly as entering baseline directly already handles.
        # "baseline" is legal here because resume_stage is "admission" - the
        # one exception _resume_target documents: admission's own ordinary
        # forward edge, not a wider allowance.
        run.init("DEMO-771", "https://j/DEMO-771", "/tmp/repo")
        run.park("DEMO-771", "SPEC_GAP", "escalate to the owner", [])
        self.assertEqual(run.load("DEMO-771")["status"], "planned")
        state = run.stage_enter("DEMO-771", "baseline")
        self.assertEqual(state["status"], "in_progress")


class TestParkingCannotTeleport(unittest.TestCase):
    """D1, cycle 2 wave D. `_FORWARD["parking"] = set(STAGES) - {"merge_watch"}`
    in fsm.py made every stage legal from "parking" - a run parked while
    briefing could resume straight into gates, silently skipping
    implementation, tests and verification, and still exit 0. This is the A/B
    repro from the final walk: the identical transition, refused unparked and
    refused parked [R-PARK-RESUME-EXACT].
    """

    def test_briefing_to_gates_is_illegal_whether_or_not_the_run_is_parked(self):
        # rule: R-PARK-RESUME-EXACT
        run.init("DEMO-990", "https://j/DEMO-990", "/tmp/repo")
        run.stage_enter("DEMO-990", "baseline")
        record_baseline("DEMO-990")
        run.stage_enter("DEMO-990", "briefing")
        with self.assertRaises(PplError) as unparked:
            run.stage_enter("DEMO-990", "gates")
        self.assertEqual(unparked.exception.code, "ILLEGAL_TRANSITION")

        run.init("DEMO-991", "https://j/DEMO-991", "/tmp/repo")
        run.stage_enter("DEMO-991", "baseline")
        record_baseline("DEMO-991")
        run.stage_enter("DEMO-991", "briefing")
        run.park("DEMO-991", "CONTEXT_INSUFFICIENT", "re-brief with more context", [])
        with self.assertRaises(PplError) as parked:
            run.stage_enter("DEMO-991", "gates")
        self.assertEqual(parked.exception.code, "ILLEGAL_TRANSITION")
        # Still parked, not silently advanced.
        self.assertEqual(run.load("DEMO-991")["stage"], "parking")

    def test_resuming_from_parking_only_the_recorded_stage_is_legal(self):
        # rule: R-PARK-RESUME-EXACT
        run.init("DEMO-992", "https://j/DEMO-992", "/tmp/repo")
        run.stage_enter("DEMO-992", "baseline")
        record_baseline("DEMO-992")
        run.stage_enter("DEMO-992", "briefing")
        run.park("DEMO-992", "CONTEXT_INSUFFICIENT", "re-brief with more context", [])
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-992", "implementation")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        self.assertIn("may only enter briefing", ctx.exception.message)
        state = run.stage_enter("DEMO-992", "briefing")
        self.assertEqual(state["stage"], "briefing")

    def test_a_second_park_still_resumes_to_the_first_recorded_stage(self):
        # N1 (cycle 2 wave B) preserves resume_stage across a second park -
        # this confirms the exact-match restriction still reads that
        # preserved value, not "parking" itself.
        run.init("DEMO-993", "https://j/DEMO-993", "/tmp/repo")
        run.stage_enter("DEMO-993", "baseline")
        record_baseline("DEMO-993")
        run.stage_enter("DEMO-993", "briefing")
        record_challenge("DEMO-993")
        run.stage_enter("DEMO-993", "implementation")
        run.park("DEMO-993", "IMPLEMENTATION_FAILURE", "inspect", [])
        run.park("DEMO-993", "OWNER_DECISION", "operator stop", [])
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-993", "briefing")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        state = run.stage_enter("DEMO-993", "implementation")
        self.assertEqual(state["stage"], "implementation")

    def test_missing_parking_record_fails_closed_rather_than_wide_open(self):
        # rule: R-PARK-RESUME-EXACT
        run.init("DEMO-994", "https://j/DEMO-994", "/tmp/repo")
        run.stage_enter("DEMO-994", "baseline")
        run.park("DEMO-994", "OWNER_DECISION", "operator stop", [])
        from ppl import paths
        (paths.run_dir("DEMO-994") / "parking.json").unlink()
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-994", "baseline")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_direct_stage_enter_into_parking_is_refused(self):
        # rule: R-PARK-RESUME-EXACT
        # `ppl park` is the only writer of parking.json - entering the stage
        # directly would leave the eventual resume with nothing to check
        # against, and fail confusingly later instead of clearly now.
        run.init("DEMO-995", "https://j/DEMO-995", "/tmp/repo")
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-995", "parking")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestNextSafeActionStaysCurrent(unittest.TestCase):
    """D6, cycle 2 wave D: next_safe_action used to be set once, at
    admission, and never restamped by an ordinary stage transition - `ppl
    show --json` at "done" still read "complete admission checklist". Now
    restamped from run.STAGE_NEXT_ACTION on every real stage_enter call.
    """

    def test_next_safe_action_changes_with_the_stage(self):
        run.init("DEMO-998", "https://j/DEMO-998", "/tmp/repo")
        self.assertEqual(run.load("DEMO-998")["next_safe_action"],
                         "complete admission checklist")
        state = run.stage_enter("DEMO-998", "baseline")
        self.assertNotEqual(state["next_safe_action"], "complete admission checklist")
        self.assertIn("baseline", state["next_safe_action"])
        record_baseline("DEMO-998")
        state = run.stage_enter("DEMO-998", "briefing")
        self.assertIn("briefer", state["next_safe_action"])

    def test_parking_keeps_the_callers_own_safe_next_action(self):
        # run.park's safe_next is a specific answer to a specific blocker -
        # stage_enter must not clobber it with a generic stage-table entry.
        run.init("DEMO-901", "https://j/DEMO-901", "/tmp/repo")
        state = run.park("DEMO-901", "SPEC_GAP", "ask the owner about X", [])
        self.assertEqual(state["next_safe_action"], "ask the owner about X")


class TestEscalationCannotTeleport(unittest.TestCase):
    """The same hole, same fix, for the other side of fsm.py's wide edge:
    `_FORWARD["escalation"] = set(STAGES) - {"merge_watch"}`. Escalation is
    reached directly with `ppl stage enter escalation` (never through `ppl
    escalate`, whose run.park always lands on "parking" instead -
    tests/test_watchdog.py's comment on this is the authority) but it is
    still a real stage in the closed vocabulary, still reachable by a plain
    `ppl stage enter escalation` call, and carried the identical "any stage
    next" bug for as long as parking did [R-PARK-RESUME-EXACT].
    """

    def test_resuming_from_escalation_only_the_recorded_stage_is_legal(self):
        # rule: R-PARK-RESUME-EXACT
        run.init("DEMO-996", "https://j/DEMO-996", "/tmp/repo")
        run.stage_enter("DEMO-996", "baseline")
        record_baseline("DEMO-996")
        run.stage_enter("DEMO-996", "briefing")
        state = run.stage_enter("DEMO-996", "escalation")
        self.assertEqual(state["escalation_resume_stage"], "briefing")
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-996", "implementation")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        resumed = run.stage_enter("DEMO-996", "briefing")
        self.assertEqual(resumed["stage"], "briefing")

    def test_escalation_from_admission_resumes_through_admissions_own_edges(self):
        run.init("DEMO-997", "https://j/DEMO-997", "/tmp/repo")
        state = run.stage_enter("DEMO-997", "escalation")
        self.assertEqual(state["escalation_resume_stage"], "admission")
        resumed = run.stage_enter("DEMO-997", "baseline")
        self.assertEqual(resumed["stage"], "baseline")


if __name__ == "__main__":
    unittest.main()


CHALLENGE_OK = {
    "task_id": "DEMO-1", "spec_revision": None, "verdict": "ACCEPTED",
    "new_classes": [{"name": "FooController", "existing_owner": None, "justified": True}],
    "wire_shapes": [{"shape": "data: [...]", "matches_spec": True}],
    "open_decisions": [{"question": "q", "cost_to_close_files": 12, "decision": "parked"}],
    "producer": {"role": "brief-challenger"},
}


def challenge_for(task_id, **overrides):
    doc = dict(CHALLENGE_OK)
    doc["task_id"] = task_id
    doc["spec_revision"] = run.load(task_id)["spec_revision"]
    doc.update(overrides)
    return doc


class TestBriefChallengeGate(unittest.TestCase):
    """The brief is challenged before implementation starts, and the challenge
    is bound to the spec_revision it read [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION]."""

    def briefed(self, task_id):
        run.init(task_id, "https://j/" + task_id, "/tmp/repo")
        run.stage_enter(task_id, "baseline")
        record_baseline(task_id)
        run.stage_enter(task_id, "briefing")
        state = run.load(task_id)
        state["spec_revision"] = "sha256:" + "1" * 64
        run.save(state)

    def test_leaving_briefing_without_a_challenge_is_refused(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        self.briefed("DEMO-8101")
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-8101", "implementation")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        self.assertIn("brief challenge", ctx.exception.message)

    def test_an_accepted_challenge_opens_implementation(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        self.briefed("DEMO-8102")
        run.record_challenge("DEMO-8102", challenge_for("DEMO-8102"))
        state = run.stage_enter("DEMO-8102", "implementation")
        self.assertEqual(state["stage"], "implementation")

    def test_a_revise_challenge_does_not_open_implementation(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        self.briefed("DEMO-8103")
        run.record_challenge("DEMO-8103", challenge_for(
            "DEMO-8103", verdict="REVISE",
            new_classes=[{"name": "FooManager", "existing_owner": "BarManager", "justified": False}]))
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-8103", "implementation")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")
        self.assertIn("re-brief", run.load("DEMO-8103")["next_safe_action"])

    def test_a_rebrief_stales_the_challenge(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        self.briefed("DEMO-8104")
        run.record_challenge("DEMO-8104", challenge_for("DEMO-8104"))
        state = run.load("DEMO-8104")
        state["spec_revision"] = "sha256:" + "2" * 64
        run.save(state)
        self.assertFalse(run.challenge_current(run.load("DEMO-8104")))
        with self.assertRaises(PplError) as ctx:
            run.stage_enter("DEMO-8104", "implementation")
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_a_challenge_against_another_spec_revision_is_refused(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        self.briefed("DEMO-8105")
        with self.assertRaises(PplError) as ctx:
            run.record_challenge("DEMO-8105", challenge_for(
                "DEMO-8105", spec_revision="sha256:" + "9" * 64))
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("spec_revision", ctx.exception.message)

    def test_a_challenge_before_any_brief_is_refused(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        run.init("DEMO-8106", "https://j/DEMO-8106", "/tmp/repo")
        doc = dict(CHALLENGE_OK, task_id="DEMO-8106", spec_revision="sha256:" + "1" * 64)
        with self.assertRaises(PplError) as ctx:
            run.record_challenge("DEMO-8106", doc)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("brief set", ctx.exception.message)


class TestChallengeSchema(unittest.TestCase):
    """An ACCEPTED report cannot carry the very problems the challenge exists
    to surface [R-BRIEF-CHALLENGE-SELF-CONSISTENT]."""

    def test_accepted_with_an_unjustified_new_class_is_refused(self):
        # rule: R-BRIEF-CHALLENGE-SELF-CONSISTENT
        doc = dict(CHALLENGE_OK, spec_revision="x",
                   new_classes=[{"name": "EventCategoryFormatManager",
                                 "existing_owner": "EventCategoryManager", "justified": False}])
        with self.assertRaises(PplError) as ctx:
            schema.validate_challenge(doc)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("EventCategoryFormatManager", ctx.exception.message)

    def test_accepted_with_a_wire_shape_off_spec_is_refused(self):
        # rule: R-BRIEF-CHALLENGE-SELF-CONSISTENT
        doc = dict(CHALLENGE_OK, spec_revision="x",
                   wire_shapes=[{"shape": "data.formats", "matches_spec": False}])
        with self.assertRaises(PplError) as ctx:
            schema.validate_challenge(doc)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_accepted_with_a_cheap_parked_decision_is_refused(self):
        # rule: R-BRIEF-CHALLENGE-SELF-CONSISTENT
        doc = dict(CHALLENGE_OK, spec_revision="x",
                   open_decisions=[{"question": "virtual project relevance",
                                    "cost_to_close_files": 3, "decision": "parked"}])
        with self.assertRaises(PplError) as ctx:
            schema.validate_challenge(doc)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("parked", ctx.exception.message)

    def test_revise_may_carry_the_problems_it_reports(self):
        doc = dict(CHALLENGE_OK, spec_revision="x", verdict="REVISE",
                   new_classes=[{"name": "X", "existing_owner": "Y", "justified": False}])
        schema.validate_challenge(doc)

    def test_an_unknown_verdict_is_refused(self):
        doc = dict(CHALLENGE_OK, spec_revision="x", verdict="MAYBE")
        with self.assertRaises(PplError) as ctx:
            schema.validate_challenge(doc)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestOpenDecisionsSchema(unittest.TestCase):
    """A parked open decision that is cheap to close is refused at ppl brief
    set [R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP]."""

    def brief(self, open_decisions):
        return {
            "task_id": "DEMO-1", "purpose": "p", "success_outcome": "s",
            "governing_requirements": ["REQ-1"], "acceptance": ["AC-1"],
            "allowed_write_paths": ["src/**"], "forbidden_paths": ["tests/**"],
            "verification": ["focused-tests"], "open_decisions": open_decisions,
            "name_contract": {"Foo.bar(int)": "returns long"},
        }

    def test_a_cheap_parked_decision_is_refused(self):
        # rule: R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief(self.brief([
                {"question": "virtual project relevance", "cost_to_close_files": 3,
                 "decision": "parked"}]))
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("never parked", ctx.exception.message)

    def test_an_expensive_parked_decision_is_accepted(self):
        # rule: R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP
        schema.validate_brief(self.brief([
            {"question": "papi node", "cost_to_close_files": 20, "decision": "parked"}]))

    def test_a_cheap_decision_taken_in_scope_or_to_the_owner_is_accepted(self):
        schema.validate_brief(self.brief([
            {"question": "a", "cost_to_close_files": 1, "decision": "in_scope"},
            {"question": "b", "cost_to_close_files": 0, "decision": "owner"},
            "legacy free text stays accepted"]))

    def test_an_object_without_a_decision_is_refused(self):
        with self.assertRaises(PplError) as ctx:
            schema.validate_brief(self.brief([{"question": "a", "cost_to_close_files": 1}]))
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")



class TestAttemptFieldRetired(unittest.TestCase):
    def test_a_new_run_state_has_no_attempt_key(self):
        run.init("DEMO-1301", "u", "/tmp/repo")
        self.assertNotIn("attempt", run.load("DEMO-1301"))

    def test_an_old_state_carrying_attempt_still_loads_and_shows(self):
        import contextlib
        import io
        from ppl import cli
        run.init("DEMO-1302", "u", "/tmp/repo")
        state = run.load("DEMO-1302")
        state["attempt"] = 1
        run.save(state)
        self.assertEqual(run.load("DEMO-1302")["attempt"], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["show", "--task", "DEMO-1302"]), 0)
            self.assertEqual(cli.main(["show", "--task", "DEMO-1302", "--json"]), 0)
        self.assertIn("DEMO-1302", out.getvalue())

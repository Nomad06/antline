import itertools
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import budget, evidence, findings, paths, run, status, store, verdict
from ppl.errors import PplError

PRODUCER = {"provider": "claude-code-subagent", "model": "opus", "params_hash": "h"}
COUNTER = itertools.count(310)


def rc_zero():
    path = Path(tempfile.mkstemp(suffix=".rc")[1])
    path.write_text("0\n", encoding="utf-8")
    return str(path)


def make_repo():
    repo = tempfile.mkdtemp(prefix="pplrepo-")
    subprocess.run(["git", "init", "-q", "-b", "master", repo], check=True)
    Path(repo, "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "base"], check=True)
    return repo


def head_sha(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name, text):
    Path(repo, name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", name], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", name], check=True)
    return head_sha(repo)


def good_verdict(task, sha):
    return {
        "task_id": task, "candidate_sha": sha, "spec_revision": "sha256:spec",
        "verdict": "PASS",
        "acceptance_evidence": {"AC-1": "evidence:focused-tests"},
        "findings": [],
        "producer": PRODUCER,
    }


class TestBudget(unittest.TestCase):
    def test_dispatch_spend_accumulates(self):
        run.init("DEMO-300", "u", "/tmp/repo")
        state = budget.spend("DEMO-300", "dispatch", 3)
        self.assertEqual(state["budgets"]["dispatch_used"], 3)

    def test_dispatch_hard_cap_raises(self):
        # rule: R-BUDGET-DISPATCH-CAP
        run.init("DEMO-301", "u", "/tmp/repo")
        with self.assertRaises(PplError) as ctx:
            budget.spend("DEMO-301", "dispatch", 35)
        self.assertEqual(ctx.exception.code, "BUDGET_EXHAUSTED")

    def test_retry_cap_raises(self):
        # rule: R-BUDGET-RETRY
        run.init("DEMO-302", "u", "/tmp/repo")
        budget.spend("DEMO-302", "retry", 3)
        with self.assertRaises(PplError):
            budget.spend("DEMO-302", "retry", 1)

    def test_usd_unlimited_when_max_is_zero(self):
        run.init("DEMO-303", "u", "/tmp/repo")
        state = budget.spend("DEMO-303", "usd", 5.0)
        self.assertEqual(state["budgets"]["usd_used"], 5.0)

    def test_usd_cap_enforced_when_set(self):
        # rule: R-BUDGET-USD
        run.init("DEMO-304", "u", "/tmp/repo")
        state = run.load("DEMO-304")
        state["budgets"]["usd_max"] = 1.0
        run.save(state)
        with self.assertRaises(PplError) as ctx:
            budget.spend("DEMO-304", "usd", 1.5)
        self.assertEqual(ctx.exception.code, "BUDGET_EXHAUSTED")

    def test_remaining_reflects_spend(self):
        state = run.init("DEMO-305", "u", "/tmp/repo")
        cap = state["budgets"]["dispatch_hard"]
        state = budget.spend("DEMO-305", "dispatch", 4)
        self.assertEqual(budget.remaining(state, "dispatch"), cap - 4)


class TestVerdict(unittest.TestCase):
    def setUp(self):
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "u", "/tmp/repo")
        state = run.load(self.task)
        state["acceptance"] = ["AC-1"]
        # A real run reaches these tests only after `ppl brief set`, which
        # binds spec_revision (R-DONE-REQUIRES-BRIEF) - bound here to
        # "sha256:spec" to match good_verdict()'s own spec_revision, so these
        # tests exercise a genuinely briefed run rather than working around
        # the new done-gate check with an unbriefed one.
        state["spec_revision"] = "sha256:spec"
        # the done gate also requires an ACCEPTED challenge bound to the
        # current spec_revision (R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION)
        state["brief_challenge"] = {"spec_revision": "sha256:spec", "verdict": "ACCEPTED",
                                    "at": "2026-01-01T00:00:00+00:00"}
        run.save(state)

    def test_record_refuses_a_verdict_for_another_sha(self):
        # rule: R-DONE-SHA-BINDING
        with self.assertRaises(PplError) as ctx:
            verdict.record(self.task, good_verdict(self.task, "old111"), "new222")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("stale", ctx.exception.message)

    def test_record_stores_a_matching_verdict(self):
        stored = verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        self.assertEqual(stored["verdict"], "PASS")
        self.assertIsNotNone(verdict.latest(self.task, "abc"))

    def test_done_blocked_without_verdict(self):
        # rule: R-DONE-GATE
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("no verdict" in r for r in reasons))

    def test_done_blockers_raises_a_routable_error_on_a_corrupt_verdict_file(self):
        # rule: R-STORE-CORRUPT-JSON (W5(b), cycle 2 wave B). A verdicts/<sha>.json
        # left corrupt (a half-written file from a crashed process, or a
        # mangled edit) used to surface as a raw json.JSONDecodeError out of
        # verdict.latest, exit 1. It now routes through store.read_json as a
        # SCHEMA_INVALID PplError instead of crashing.
        path = Path(os.environ["PPL_HOME"], "runs", self.task, "verdicts", "abc.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PplError) as ctx:
            verdict.done_blockers(self.task, "abc", [])
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_done_blocked_when_acceptance_lacks_evidence(self):
        # rule: R-DONE-ACCEPTANCE-EVIDENCE
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("AC-1" in r for r in reasons))

    def test_done_blocked_when_diff_escapes_boundary(self):
        # rule: R-DONE-DIFF-BOUNDARY
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})
        reasons = verdict.done_blockers(self.task, "abc", ["src/other/X.java"])
        self.assertTrue(any("src/other/X.java" in r for r in reasons))

    def test_done_blocked_when_changed_path_matches_forbid(self):
        # rule: R-DONE-DIFF-BOUNDARY (W1, cycle 2 wave B). done_blockers only
        # ever tested any_match(path, bound["write"]) or is_test_path(path) -
        # a path the brief explicitly listed as forbidden, but which also
        # happens to match boundary.write (or is a test source), sailed
        # through the terminal gate untouched. The guard hook denies those
        # writes at the tool-call boundary now, but the done gate should still
        # prove what R-DONE-DIFF-BOUNDARY claims rather than rely on defence
        # in depth alone.
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/**"], "forbid": ["src/secrets/**"]})
        reasons = verdict.done_blockers(self.task, "abc", ["src/secrets/Key.java"])
        self.assertTrue(any("src/secrets/Key.java" in r for r in reasons))

    def test_done_allowed_when_everything_holds(self):
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})
        self.assertEqual(verdict.done_blockers(self.task, "abc", ["src/ok/X.java"]), [])

    def test_done_blocked_without_secret_scan(self):
        # rule: R-DONE-SECRET-SCAN
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("secret-scan" in r for r in reasons))

    def test_done_blocked_when_verdict_is_not_pass(self):
        # rule: R-DONE-VERDICT-PASS
        doc = good_verdict(self.task, "abc")
        doc["verdict"] = "REWORK"
        verdict.record(self.task, doc, "abc")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("REWORK" in r for r in reasons))

    def test_done_blocked_when_verdict_is_inconclusive(self):
        # rule: R-DONE-VERDICT-PASS
        doc = good_verdict(self.task, "abc")
        doc["verdict"] = "INCONCLUSIVE"
        verdict.record(self.task, doc, "abc")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("INCONCLUSIVE" in r for r in reasons))

    def test_done_blocked_when_verdict_is_error(self):
        # rule: R-DONE-VERDICT-PASS
        doc = good_verdict(self.task, "abc")
        doc["verdict"] = "ERROR"
        verdict.record(self.task, doc, "abc")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("ERROR" in r for r in reasons))

    def test_done_blocked_when_verdict_is_cancelled(self):
        # rule: R-DONE-VERDICT-PASS
        doc = good_verdict(self.task, "abc")
        doc["verdict"] = "CANCELLED"
        verdict.record(self.task, doc, "abc")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("CANCELLED" in r for r in reasons))

    def test_done_blocked_when_verdict_is_timeout(self):
        # rule: R-DONE-VERDICT-PASS
        doc = good_verdict(self.task, "abc")
        doc["verdict"] = "TIMEOUT"
        verdict.record(self.task, doc, "abc")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("TIMEOUT" in r for r in reasons))

    def _own_diff_finding(self, severity="blocking"):
        """A real repo and baseline, because ppl/findings.py:add derives its
        track from a real git diff - it cannot be faked with the module's
        default "/tmp/repo" (see R-FINDINGS-TRACK-DERIVED)."""
        self.repo = make_repo()
        state = run.load(self.task)
        state["repo"] = self.repo
        state["base_sha"] = head_sha(self.repo)
        run.save(state)
        head = commit(self.repo, "Foo.java", "one")
        record = findings.add(self.task, severity, "the loop drops the last item",
                              head, "Foo.java")
        return head, record

    def _pass_the_rest_of_the_gate(self, sha):
        verdict.record(self.task, good_verdict(self.task, sha), sha)
        evidence.add(self.task, "focused-tests", "pass", sha, "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", sha, "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", sha, "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["**"], "forbid": []})

    def test_done_blocked_on_unresolved_blocking_finding(self):
        # rule: R-DONE-OPEN-FINDING
        head, _ = self._own_diff_finding("blocking")
        self._pass_the_rest_of_the_gate(head)
        reasons = verdict.done_blockers(self.task, head, ["Foo.java"])
        self.assertTrue(any("blocking finding" in r for r in reasons))

    def test_done_allowed_when_the_only_finding_is_foreign(self):
        # rule: R-DONE-OPEN-FINDING - a foreign finding is batch-escalated to
        # its own owner and must not stall THIS task's done gate.
        repo = make_repo()
        state = run.load(self.task)
        state["repo"] = repo
        state["base_sha"] = head_sha(repo)
        run.save(state)
        head = commit(repo, "Foo.java", "one")
        findings.add(self.task, "blocking", "pre-existing bug", head, "Untouched.java",
                     owner="team-legacy")
        self._pass_the_rest_of_the_gate(head)
        reasons = verdict.done_blockers(self.task, head, ["Foo.java"])
        self.assertEqual(reasons, [])

    def test_done_allowed_once_the_finding_is_marked_fixed(self):
        # rule: R-DONE-OPEN-FINDING, R-FINDINGS-FIXED-REQUIRES-PROOF
        head, record = self._own_diff_finding("blocking")
        fix_head = commit(self.repo, "Foo.java", "two")  # a real fix, touches the file
        findings.set_status(self.task, record["finding_id"], "fixed",
                            fix_link="https://example/commit/abc")
        self._pass_the_rest_of_the_gate(fix_head)
        reasons = verdict.done_blockers(self.task, fix_head, ["Foo.java"])
        self.assertEqual(reasons, [])

    def test_done_still_blocked_after_an_unrelated_commit(self):
        # rule: R-DONE-OPEN-FINDING - round 1 review C1: dropping the
        # affected_sha filter must not have been undone. An unrelated commit
        # (never touching Foo.java, the file the defect lives in) must not
        # clear a real, unfixed, blocking finding off the done gate.
        head, _ = self._own_diff_finding("blocking")
        later_head = commit(self.repo, "README.md", "unrelated")
        self._pass_the_rest_of_the_gate(later_head)
        reasons = verdict.done_blockers(self.task, later_head, ["Foo.java", "README.md"])
        self.assertTrue(any("blocking finding" in r for r in reasons))

    def test_done_blocked_when_acceptance_evidence_has_no_prefix(self):
        # rule: R-ACCEPTANCE-EVIDENCE-FORMAT
        doc = good_verdict(self.task, "abc")
        doc["acceptance_evidence"] = {"AC-1": "focused-tests"}
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "verdicts", "abc.json"), doc)
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("AC-1" in r for r in reasons))

    def test_done_blocked_when_acceptance_evidence_is_not_a_string(self):
        # rule: R-ACCEPTANCE-EVIDENCE-FORMAT
        doc = good_verdict(self.task, "abc")
        doc["acceptance_evidence"] = {"AC-1": 42}
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "verdicts", "abc.json"), doc)
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("AC-1" in r for r in reasons))

    def test_done_blocked_when_retry_budget_exhausted(self):
        # rule: R-DONE-RETRY-BUDGET (W6, cycle 2 wave B). This used to write
        # retry_used = retry_max + 1 directly into state.json - a state
        # budget.spend can never actually produce, since it refuses any
        # proposed spend that would exceed the cap
        # (test_retry_cap_raises), so retry_used maxes out AT retry_max, not
        # past it. done_blockers's `retry_used > retry_max` was therefore
        # unreachable through any real path. Its own deny test proved that by
        # driving an impossible state. Reached here through budget.spend
        # itself, exactly at the cap, matching what a real exhausted run
        # looks like.
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})
        state = run.load(self.task)
        budget.spend(self.task, "retry", state["budgets"]["retry_max"])
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("retry budget" in r for r in reasons))


class TestSpecRevisionStaleness(unittest.TestCase):
    """R-BRIEF-SPEC-REVISION-STALE: the same-SHA discipline (6.5), applied to
    the name contract instead of the git head. `record` catches a verdict that
    arrives after a rebrief already moved state["spec_revision"]; done_blockers
    catches the narrower case record cannot - a verdict recorded PASS BEFORE
    the rebrief, on the same candidate SHA, which nothing else in the done
    gate reads spec_revision to notice.
    """

    def setUp(self):
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "u", "/tmp/repo")
        state = run.load(self.task)
        state["acceptance"] = ["AC-1"]
        state["spec_revision"] = "sha256:bound"
        state["brief_challenge"] = {"spec_revision": "sha256:bound", "verdict": "ACCEPTED",
                                    "at": "2026-01-01T00:00:00+00:00"}
        run.save(state)

    def test_record_refuses_a_verdict_bound_to_a_stale_spec_revision(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE
        doc = good_verdict(self.task, "abc")
        doc["spec_revision"] = "sha256:old"
        with self.assertRaises(PplError) as ctx:
            verdict.record(self.task, doc, "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("stale", ctx.exception.message)
        self.assertIsNone(verdict.latest(self.task, "abc"),
                          "a stale verdict must not be written at all")

    def test_record_accepts_a_verdict_matching_the_bound_spec_revision(self):
        doc = good_verdict(self.task, "abc")
        doc["spec_revision"] = "sha256:bound"
        stored = verdict.record(self.task, doc, "abc")
        self.assertEqual(stored["spec_revision"], "sha256:bound")

    def test_record_allows_any_spec_revision_when_none_has_been_bound_yet(self):
        # No `ppl brief set` ever ran for this task, so state["spec_revision"]
        # is None and there is nothing here to compare the verdict against.
        # This is not a floor - R-DONE-REQUIRES-BRIEF (TestDoneGateRequiresABrief)
        # is what actually stops an unbriefed run, at done, not here.
        task = "DEMO-%d" % next(COUNTER)
        run.init(task, "u", "/tmp/repo")
        state = run.load(task)
        state["acceptance"] = ["AC-1"]
        run.save(state)
        self.assertIsNone(run.load(task)["spec_revision"])
        stored = verdict.record(task, good_verdict(task, "abc"), "abc")
        self.assertEqual(stored["verdict"], "PASS")

    def test_done_blockers_flags_a_pass_verdict_recorded_before_a_rebrief(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE
        # The verdict was PASS and matched spec_revision at the moment it was
        # recorded. Only afterwards does a rebrief move the run's binding -
        # record() cannot have caught this, because it had not happened yet.
        doc = good_verdict(self.task, "abc")
        doc["spec_revision"] = "sha256:bound"
        verdict.record(self.task, doc, "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["**"], "forbid": []})
        state = run.load(self.task)
        state["spec_revision"] = "sha256:new-contract"
        run.save(state)
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("spec_revision" in r for r in reasons), reasons)

    def test_done_blockers_allow_when_spec_revision_still_matches(self):
        doc = good_verdict(self.task, "abc")
        doc["spec_revision"] = "sha256:bound"
        verdict.record(self.task, doc, "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["**"], "forbid": []})
        self.assertEqual(verdict.done_blockers(self.task, "abc", []), [])


class TestDoneGateRequiresABrief(unittest.TestCase):
    """R-DONE-REQUIRES-BRIEF: round 1 review of this task's own work found that
    a run which never calls `ppl brief set` at all (`ppl boundary set` is its
    own command and needs no brief) reached every other check in the done gate
    with nothing recorded - acceptance=[] passes the per-criterion loop
    vacuously, and spec_revision=None used to make R-BRIEF-SPEC-REVISION-STALE
    simply never fire, accepting a verdict carrying any invented string.
    `done_blockers(...) -> []` for a task nobody ever specified. Both are now
    blockers in their own right rather than preconditions gating something
    else.
    """

    def setUp(self):
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "u", "/tmp/repo")
        # Deliberately no brief set at all: state["acceptance"] stays [] and
        # state["spec_revision"] stays None, exactly run.init's defaults.

    def _verified_pass_with_invented_spec_revision(self, sha, spec_revision):
        doc = {
            "task_id": self.task, "candidate_sha": sha, "spec_revision": spec_revision,
            "verdict": "PASS", "acceptance_evidence": {}, "findings": [], "producer": PRODUCER,
        }
        verdict.record(self.task, doc, sha)
        evidence.add(self.task, "focused-tests", "pass", sha, "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", sha, "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", sha, "/tmp/l", rc_zero(), PRODUCER)

    def test_the_reviewers_probe_no_longer_reaches_zero_blockers(self):
        # rule: R-DONE-REQUIRES-BRIEF
        # The exact scenario from round 1 review: no brief, an invented
        # spec_revision, both required receipts passing, no changed paths.
        self._verified_pass_with_invented_spec_revision("abc", "sha256:totally-invented")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertNotEqual(reasons, [], "a run that was never briefed must not reach done")

    def test_refuses_specifically_on_an_unbound_spec_revision(self):
        # rule: R-DONE-REQUIRES-BRIEF
        self._verified_pass_with_invented_spec_revision("abc", "sha256:totally-invented")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("spec_revision" in r for r in reasons), reasons)

    def test_refuses_specifically_on_an_empty_acceptance_list(self):
        # rule: R-DONE-REQUIRES-BRIEF
        # spec_revision bound here so the spec_revision blocker above does not
        # fire, isolating this test to the acceptance-emptiness check alone.
        state = run.load(self.task)
        state["spec_revision"] = "sha256:bound"
        run.save(state)
        self._verified_pass_with_invented_spec_revision("abc", "sha256:bound")
        reasons = verdict.done_blockers(self.task, "abc", [])
        self.assertTrue(any("acceptance" in r for r in reasons), reasons)

    def test_allowed_once_the_run_was_actually_briefed(self):
        # The positive control: bind both the way `ppl brief set` really does,
        # and the two new checks contribute nothing to the gate.
        state = run.load(self.task)
        state["spec_revision"] = "sha256:bound"
        state["acceptance"] = ["AC-1"]
        state["brief_challenge"] = {"spec_revision": "sha256:bound", "verdict": "ACCEPTED",
                                    "at": "2026-01-01T00:00:00+00:00"}
        run.save(state)
        doc = good_verdict(self.task, "abc")
        doc["spec_revision"] = "sha256:bound"
        doc["acceptance_evidence"] = {"AC-1": "evidence:focused-tests"}
        verdict.record(self.task, doc, "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["**"], "forbid": []})
        self.assertEqual(verdict.done_blockers(self.task, "abc", []), [])


class TestVerificationUnavailablePark(unittest.TestCase):
    """A single incomplete outcome (INCONCLUSIVE/ERROR/CANCELLED/TIMEOUT) is
    not the code's fault, so it is routed like REWORK - redispatch the same
    check on the same sha. But redispatching a check that keeps failing to
    reach a result is not what retrying is for: the SECOND consecutive
    incomplete outcome on the same candidate_sha parks the run, in code,
    rather than trusting orchestrator prose to notice the pattern
    [R-VERIFICATION-UNAVAILABLE-PARK].
    """

    def setUp(self):
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "u", "/tmp/repo")
        state = run.load(self.task)
        state["acceptance"] = ["AC-1"]
        run.save(state)

    def _incomplete(self, outcome):
        doc = good_verdict(self.task, "abc")
        doc["verdict"] = outcome
        return doc

    def test_a_single_incomplete_verdict_does_not_park(self):
        verdict.record(self.task, self._incomplete("TIMEOUT"), "abc")
        self.assertEqual(run.load(self.task)["stage"], "admission")

    def test_two_consecutive_incomplete_verdicts_on_the_same_sha_park(self):
        # rule: R-VERIFICATION-UNAVAILABLE-PARK
        verdict.record(self.task, self._incomplete("INCONCLUSIVE"), "abc")
        with self.assertRaises(PplError) as ctx:
            verdict.record(self.task, self._incomplete("TIMEOUT"), "abc")
        self.assertEqual(ctx.exception.code, "VERIFICATION_UNAVAILABLE")
        state = run.load(self.task)
        self.assertEqual(state["stage"], "parking")
        parking = store.read_json(paths.run_dir(self.task) / "parking.json")
        self.assertEqual(parking["blocker"], "VERIFICATION_UNAVAILABLE")

    def test_two_different_incomplete_outcomes_in_a_row_still_count(self):
        # rule: R-VERIFICATION-UNAVAILABLE-PARK
        # The park is on the CLASS (a check that did not happen), not on
        # repeating the exact same outcome string twice.
        verdict.record(self.task, self._incomplete("ERROR"), "abc")
        with self.assertRaises(PplError) as ctx:
            verdict.record(self.task, self._incomplete("CANCELLED"), "abc")
        self.assertEqual(ctx.exception.code, "VERIFICATION_UNAVAILABLE")

    def test_the_verdict_is_still_durably_recorded_even_though_it_parks(self):
        verdict.record(self.task, self._incomplete("INCONCLUSIVE"), "abc")
        with self.assertRaises(PplError):
            verdict.record(self.task, self._incomplete("ERROR"), "abc")
        self.assertEqual(verdict.latest(self.task, "abc")["verdict"], "ERROR")
        self.assertEqual(run.load(self.task)["candidate_sha"], "abc")

    def test_an_incomplete_verdict_right_after_a_real_outcome_does_not_park(self):
        # REWORK on "abc" is a real outcome, not an incomplete one - the very
        # next verdict on the same sha being incomplete is the FIRST of its
        # kind in a row, not the second.
        rework = good_verdict(self.task, "abc")
        rework["verdict"] = "REWORK"
        verdict.record(self.task, rework, "abc")
        verdict.record(self.task, self._incomplete("TIMEOUT"), "abc")
        self.assertEqual(run.load(self.task)["stage"], "admission")


class TestStatusProgressCountsCriteriaNotReceipts(unittest.TestCase):
    """A passing receipt is not an acceptance criterion: the card used to count
    receipts and print totals like "3/1 acceptance criteria proven".
    """

    def setUp(self):
        self.task = "DEMO-38%d" % next(COUNTER)
        run.init(self.task, "u", "/tmp/repo")
        self.sha = "abc123"
        state = run.load(self.task)
        state["acceptance"] = ["AC-1"]
        state["candidate_sha"] = self.sha
        state["spec_revision"] = "sha256:spec"
        run.save(state)

    def add(self, check):
        evidence.add(self.task, check, "pass", self.sha, "/tmp/log.txt", rc_zero(), PRODUCER)

    def skip(self, check):
        evidence.add(self.task, check, "skip", self.sha, "/tmp/log.txt", rc_zero(), PRODUCER)

    def test_extra_receipts_do_not_inflate_the_count(self):
        for check in ("focused-tests", "secret-scan", "lint"):
            self.add(check)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "verdicts", "%s.json" % self.sha),
            good_verdict(self.task, self.sha))
        card = status.card(self.task)
        self.assertIn("Progress: 1/1 acceptance criteria proven", card)

    def test_a_criterion_with_no_passing_receipt_is_not_counted(self):
        self.add("secret-scan")
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "verdicts", "%s.json" % self.sha),
            good_verdict(self.task, self.sha))
        self.assertIn("Progress: 0/1 acceptance criteria proven", status.card(self.task))

    def test_the_card_agrees_with_the_done_gate(self):
        self.add("focused-tests")
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "verdicts", "%s.json" % self.sha),
            good_verdict(self.task, self.sha))
        blockers = verdict.done_blockers(self.task, self.sha, [])
        self.assertFalse([r for r in blockers if "acceptance AC-1" in r])
        self.assertIn("Progress: 1/1", status.card(self.task))


if __name__ == "__main__":
    unittest.main()


class TestDoneGateSimplificationReceipt(unittest.TestCase):
    """The done gate wants a simplification receipt on the head, pass or
    skip, so the pass either happened or its absence is on the record
    [R-DONE-SIMPLIFICATION-RECEIPT]."""

    _ids = itertools.count(8201)

    def setUp(self):
        self.task = "DEMO-%d" % next(self._ids)
        run.init(self.task, "https://j/" + self.task, "/tmp/repo")
        state = run.load(self.task)
        state["spec_revision"] = "sha256:spec"
        state["acceptance"] = ["AC-1"]
        state["brief_challenge"] = {"spec_revision": "sha256:spec", "verdict": "ACCEPTED",
                                    "at": "2026-01-01T00:00:00+00:00"}
        run.save(state)
        verdict.record(self.task, good_verdict(self.task, "abc"), "abc")
        evidence.add(self.task, "focused-tests", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        evidence.add(self.task, "secret-scan", "pass", "abc", "/tmp/l", rc_zero(), PRODUCER)
        store.write_json(
            Path(os.environ["PPL_HOME"], "runs", self.task, "boundary.json"),
            {"write": ["src/ok/**"], "forbid": []})

    def test_done_refuses_without_a_simplification_receipt(self):
        # rule: R-DONE-SIMPLIFICATION-RECEIPT
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("simplification" in r for r in reasons), reasons)

    def test_done_accepts_a_skipped_simplification(self):
        # rule: R-DONE-SIMPLIFICATION-RECEIPT
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        self.assertEqual(verdict.done_blockers(self.task, "abc", ["src/ok/X.java"]), [])

    def test_done_refuses_a_failed_simplification(self):
        # rule: R-DONE-SIMPLIFICATION-RECEIPT
        rc_one = Path(os.environ["PPL_HOME"], "rc-one.txt")
        rc_one.write_text("1\n")
        evidence.add(self.task, "simplification", "fail", "abc", "/tmp/l", str(rc_one), PRODUCER)
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("simplification" in r for r in reasons), reasons)

    def test_done_refuses_when_the_challenge_is_stale(self):
        # rule: R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION
        evidence.add(self.task, "simplification", "skip", "abc", "/tmp/l", rc_zero(), PRODUCER)
        state = run.load(self.task)
        state["brief_challenge"]["spec_revision"] = "sha256:older"
        run.save(state)
        reasons = verdict.done_blockers(self.task, "abc", ["src/ok/X.java"])
        self.assertTrue(any("brief challenge" in r for r in reasons), reasons)


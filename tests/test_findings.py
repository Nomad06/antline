"""The findings registry (cycle 2, Task 6): a durable, independent home for a
finding, replacing the old self-attested `findings[]` list inside the
verdict document. See DESIGN.md 6.6a.
"""
import itertools
import os
import subprocess
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import findings, run, store
from ppl.errors import PplError

COUNTER = itertools.count(1200)


def make_repo():
    repo = tmphome.mkdtemp(prefix="pplrepo-")
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


def init_task_with_baseline(repo):
    task = "DEMO-%d" % next(COUNTER)
    run.init(task, "u", repo)
    state = run.load(task)
    state["base_sha"] = head_sha(repo)
    run.save(state)
    return task


class TestFindingsAdd(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.task = init_task_with_baseline(self.repo)

    def test_a_path_touched_by_the_diff_is_own_diff(self):
        # rule: R-FINDINGS-VOCAB
        head = commit(self.repo, "Foo.java", "one")
        record = findings.add(self.task, "blocking", "the loop drops the last item",
                              head, "Foo.java")
        self.assertEqual(record["track"], "own_diff")
        self.assertEqual(record["status"], "open")
        self.assertEqual(record["owner"], self.task)
        self.assertEqual(record["severity"], "blocking")

    def test_a_foreign_finding_with_an_owner_is_escalated(self):
        # rule: R-FINDINGS-FOREIGN-OWNER
        head = commit(self.repo, "Foo.java", "one")
        record = findings.add(self.task, "important", "pre-existing bug in Bar.java",
                              head, "Bar.java", owner="team-legacy")
        self.assertEqual(record["track"], "foreign")
        self.assertEqual(record["status"], "escalated")
        self.assertEqual(record["owner"], "team-legacy")

    def test_a_foreign_finding_with_no_owner_is_refused(self):
        # rule: R-FINDINGS-FOREIGN-OWNER
        head = commit(self.repo, "Foo.java", "one")
        with self.assertRaises(PplError) as ctx:
            findings.add(self.task, "blocking", "pre-existing bug", head, "Bar.java")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_an_unknown_severity_is_refused(self):
        # rule: R-FINDINGS-VOCAB
        head = commit(self.repo, "Foo.java", "one")
        with self.assertRaises(PplError) as ctx:
            findings.add(self.task, "urgent", "e", head, "Foo.java")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_empty_evidence_is_refused(self):
        head = commit(self.repo, "Foo.java", "one")
        with self.assertRaises(PplError):
            findings.add(self.task, "blocking", "", head, "Foo.java")

    def test_traversal_in_affected_path_is_refused(self):
        head = commit(self.repo, "Foo.java", "one")
        with self.assertRaises(PplError) as ctx:
            findings.add(self.task, "blocking", "e", head, "../Foo.java")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_a_non_hex_affected_sha_is_refused(self):
        with self.assertRaises(PplError) as ctx:
            findings.add(self.task, "blocking", "e", "not-a-sha", "Foo.java")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_refused_before_a_baseline_is_recorded(self):
        task = "DEMO-%d" % next(COUNTER)
        run.init(task, "u", self.repo)
        head = head_sha(self.repo)
        with self.assertRaises(PplError) as ctx:
            findings.add(task, "blocking", "e", head, "a.txt")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestTrackIsDerivedNotAccepted(unittest.TestCase):
    """The whole point of two-track routing is that there is no third box.
    If `add()` accepted a `track` argument, a caller (or a future edit) could
    hand in a value that disagrees with the real diff - this is what proves
    that door does not exist [R-FINDINGS-TRACK-DERIVED].
    """

    def setUp(self):
        self.repo = make_repo()
        self.task = init_task_with_baseline(self.repo)

    def test_add_has_no_track_parameter_to_pass(self):
        # rule: R-FINDINGS-TRACK-DERIVED
        head = commit(self.repo, "Foo.java", "one")
        with self.assertRaises(TypeError):
            findings.add(self.task, "blocking", "e", head, "Foo.java",
                         track="foreign")

    def test_a_path_touched_by_the_diff_is_own_diff(self):
        head = commit(self.repo, "Foo.java", "one")
        record = findings.add(self.task, "blocking", "e", head, "Foo.java")
        self.assertEqual(record["track"], findings.TRACK_OWN_DIFF)

    def test_a_path_the_diff_never_touched_is_foreign(self):
        head = commit(self.repo, "Foo.java", "one")
        record = findings.add(self.task, "blocking", "e", head, "Untouched.java",
                              owner="team-x")
        self.assertEqual(record["track"], findings.TRACK_FOREIGN)


class TestFindingsSetStatus(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.task = init_task_with_baseline(self.repo)
        self.head = commit(self.repo, "Foo.java", "one")
        self.record = findings.add(self.task, "blocking", "e", self.head, "Foo.java")

    def test_an_unknown_status_is_refused(self):
        # rule: R-FINDINGS-VOCAB
        with self.assertRaises(PplError) as ctx:
            findings.set_status(self.task, self.record["finding_id"], "wontfix")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_set_status_to_fixed_records_a_fix_link_and_the_fixing_sha(self):
        # rule: R-FINDINGS-FIXED-REQUIRES-PROOF
        fix_head = commit(self.repo, "Foo.java", "two")  # actually touches the file
        updated = findings.set_status(self.task, self.record["finding_id"], "fixed",
                                      fix_link="https://example/commit/abc")
        self.assertEqual(updated["status"], "fixed")
        self.assertEqual(updated["fix_link"], "https://example/commit/abc")
        self.assertEqual(updated["fixed_at_sha"], fix_head)

    def test_fixed_without_a_fix_link_is_refused(self):
        # rule: R-FINDINGS-FIXED-REQUIRES-PROOF
        commit(self.repo, "Foo.java", "two")
        with self.assertRaises(PplError) as ctx:
            findings.set_status(self.task, self.record["finding_id"], "fixed")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_fixed_with_no_commit_touching_affected_path_is_refused(self):
        # rule: R-FINDINGS-FIXED-REQUIRES-PROOF
        # the head has not moved at all - "the head moved" is too weak a
        # proof (any unrelated commit would satisfy it), the actual floor is
        # a commit that touches the finding's own affected_path.
        with self.assertRaises(PplError) as ctx:
            findings.set_status(self.task, self.record["finding_id"], "fixed",
                                fix_link="https://example/commit/abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_fixed_after_only_an_unrelated_commit_is_still_refused(self):
        # rule: R-FINDINGS-FIXED-REQUIRES-PROOF
        commit(self.repo, "README.md", "unrelated change")
        with self.assertRaises(PplError) as ctx:
            findings.set_status(self.task, self.record["finding_id"], "fixed",
                                fix_link="https://example/commit/abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_status_on_a_missing_finding_is_refused(self):
        with self.assertRaises(PplError) as ctx:
            findings.set_status(self.task, "nosuchid", "fixed", fix_link="x")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_status_change_to_open_needs_no_proof(self):
        # only "fixed" is earned - re-opening (e.g. a human disputing a
        # closure) is a plain state change.
        updated = findings.set_status(self.task, self.record["finding_id"], "open")
        self.assertEqual(updated["status"], "open")


class TestFindingsDelete(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.task = init_task_with_baseline(self.repo)
        self.head = commit(self.repo, "Foo.java", "one")
        self.record = findings.add(self.task, "minor", "e", self.head, "Foo.java")

    def test_delete_without_a_reason_is_refused(self):
        # rule: R-FINDINGS-DELETE-REASON
        with self.assertRaises(PplError) as ctx:
            findings.delete(self.task, self.record["finding_id"], "")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        # the finding must still be there - a refused delete is not a partial one
        self.assertEqual(findings.get(self.task, self.record["finding_id"])["finding_id"],
                         self.record["finding_id"])

    def test_delete_with_a_blank_reason_is_refused(self):
        # rule: R-FINDINGS-DELETE-REASON
        with self.assertRaises(PplError):
            findings.delete(self.task, self.record["finding_id"], "   ")

    def test_delete_with_a_reason_removes_the_finding_and_logs_it(self):
        findings.delete(self.task, self.record["finding_id"], "duplicate of FIND-1")
        with self.assertRaises(PplError):
            findings.get(self.task, self.record["finding_id"])
        events = [e for e in store.read_log(self.task) if e.get("event") == "finding_delete"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "duplicate of FIND-1")
        self.assertEqual(events[0]["finding_id"], self.record["finding_id"])

    def test_delete_of_a_missing_finding_is_refused(self):
        with self.assertRaises(PplError):
            findings.delete(self.task, "nosuchid", "cleanup")


class TestFindingsListing(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()
        self.task = init_task_with_baseline(self.repo)
        self.head = commit(self.repo, "Foo.java", "one")

    def test_list_open_excludes_fixed(self):
        first = findings.add(self.task, "blocking", "e1", self.head, "Foo.java")
        second = findings.add(self.task, "minor", "e2", self.head, "Foo.java")
        commit(self.repo, "Foo.java", "two")
        findings.set_status(self.task, first["finding_id"], "fixed", fix_link="x")
        open_ids = [f["finding_id"] for f in findings.list_open(self.task)]
        self.assertNotIn(first["finding_id"], open_ids)
        self.assertIn(second["finding_id"], open_ids)

    def test_list_all_includes_fixed(self):
        record = findings.add(self.task, "blocking", "e1", self.head, "Foo.java")
        commit(self.repo, "Foo.java", "two")
        findings.set_status(self.task, record["finding_id"], "fixed", fix_link="x")
        all_ids = [f["finding_id"] for f in findings.list_all(self.task)]
        self.assertIn(record["finding_id"], all_ids)


class TestOpenBlockingOnOwnDiff(unittest.TestCase):
    """What `ppl/verdict.py:done_blockers` actually gates on - see
    tests/test_verdict.py for the done-gate integration tests.

    Round 1 review (C1): this used to also require `affected_sha ==
    head_sha`, so ANY later commit - even one that never touched the
    defective file - cleared a real, unfixed, blocking finding off the done
    gate. That filter is gone; a blocking own_diff finding blocks
    regardless of how far the head has moved, until it is actually closed
    as fixed (which itself now requires proof - see TestFindingsSetStatus).
    """

    def setUp(self):
        self.repo = make_repo()
        self.task = init_task_with_baseline(self.repo)
        self.head = commit(self.repo, "Foo.java", "one")

    def test_a_foreign_finding_is_not_in_the_blocking_set(self):
        findings.add(self.task, "blocking", "e", self.head, "Untouched.java",
                    owner="team-x")
        self.assertEqual(findings.open_blocking_on_own_diff(self.task), [])

    def test_a_non_blocking_own_diff_finding_is_not_in_the_blocking_set(self):
        findings.add(self.task, "minor", "e", self.head, "Foo.java")
        self.assertEqual(findings.open_blocking_on_own_diff(self.task), [])

    def test_an_own_diff_blocking_finding_is_in_the_set(self):
        record = findings.add(self.task, "blocking", "e", self.head, "Foo.java")
        found = findings.open_blocking_on_own_diff(self.task)
        self.assertEqual([f["finding_id"] for f in found], [record["finding_id"]])

    def test_a_properly_fixed_own_diff_blocking_finding_is_not_in_the_set(self):
        record = findings.add(self.task, "blocking", "e", self.head, "Foo.java")
        commit(self.repo, "Foo.java", "two")  # a real fix, touches the file
        findings.set_status(self.task, record["finding_id"], "fixed", fix_link="x")
        self.assertEqual(findings.open_blocking_on_own_diff(self.task), [])

    def test_an_unrelated_commit_does_not_clear_a_blocking_finding(self):
        # rule: R-DONE-OPEN-FINDING
        # this is the C1 regression test: an
        # unrelated commit must not be able to walk the same hole back in
        # through the done gate.
        record = findings.add(self.task, "blocking", "e", self.head, "Foo.java")
        commit(self.repo, "README.md", "unrelated change")
        found = findings.open_blocking_on_own_diff(self.task)
        self.assertEqual([f["finding_id"] for f in found], [record["finding_id"]])

    def test_a_wrong_shaped_finding_file_fails_closed_rather_than_opening_the_gate(self):
        # rule: R-FINDINGS-FAILS-CLOSED-ON-MALFORMED (W4, cycle 2 wave B).
        # _load_all used to silently DROP any document that was not a dict,
        # so rewriting a blocking finding's file to `[]` (still valid JSON,
        # just the wrong shape) made it vanish from open_blocking_on_own_diff
        # and opened the done gate - rc=0 - exactly the "an unpopulated
        # registry gated nothing" shape this cycle keeps finding. The guard
        # hook's active_state() was hardened to fail CLOSED on this class in
        # cycle 1. The registry did not get the same treatment until now.
        record = findings.add(self.task, "blocking", "e", self.head, "Foo.java")
        path = (Path(os.environ["PPL_HOME"]) / "runs" / self.task / "findings"
                / ("%s.json" % record["finding_id"]))
        store.write_json(path, [])
        with self.assertRaises(PplError) as ctx:
            findings.open_blocking_on_own_diff(self.task)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_a_malformed_finding_file_fails_closed_not_with_a_raw_crash(self):
        # rule: R-FINDINGS-FAILS-CLOSED-ON-MALFORMED (W4). Invalid JSON used
        # to crash with an uncaught JSONDecodeError, exit 1. It must route as
        # a SCHEMA_INVALID PplError instead - still refusing the done gate,
        # never opening it, but through the documented 0/2/1 contract.
        record = findings.add(self.task, "blocking", "e", self.head, "Foo.java")
        path = (Path(os.environ["PPL_HOME"]) / "runs" / self.task / "findings"
                / ("%s.json" % record["finding_id"]))
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(PplError) as ctx:
            findings.open_blocking_on_own_diff(self.task)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

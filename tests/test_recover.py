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

from ppl import gitfacts, recover, run
from ppl import paths, store
from ppl.errors import PplError


def make_repo():
    repo = tmphome.mkdtemp(prefix="pplrepo-")
    subprocess.run(["git", "init", "-q", "-b", "master", repo], check=True)
    Path(repo, "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "base"], check=True)
    return repo


class TestGitFacts(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_head_sha_is_forty_hex_chars(self):
        sha = gitfacts.head_sha(self.repo)
        self.assertEqual(len(sha), 40)

    def test_branch_is_master(self):
        self.assertEqual(gitfacts.branch(self.repo), "master")

    def test_changed_paths_lists_the_modified_file(self):
        base = gitfacts.head_sha(self.repo)
        Path(self.repo, "b.txt").write_text("two\n", encoding="utf-8")
        subprocess.run(["git", "-C", self.repo, "add", "b.txt"], check=True)
        subprocess.run(["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "second"], check=True)
        head = gitfacts.head_sha(self.repo)
        self.assertEqual(gitfacts.changed_paths(self.repo, base, head), ["b.txt"])

    def test_is_repo_false_for_plain_directory(self):
        self.assertFalse(gitfacts.is_repo(tmphome.mkdtemp()))

    def test_changed_paths_rejects_a_falsy_base(self):
        # E1, cycle 2 wave E: a missing base/head is a bad input document
        # (e.g. an unrecorded base_sha), not a broken external system - the
        # same reasoning is_ancestor already gives for its own two-revisions
        # check. SCHEMA_INVALID, not EXTERNAL_BLOCKED.
        head = gitfacts.head_sha(self.repo)
        with self.assertRaises(PplError) as ctx:
            gitfacts.changed_paths(self.repo, "", head)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_changed_paths_rejects_a_falsy_head(self):
        base = gitfacts.head_sha(self.repo)
        with self.assertRaises(PplError) as ctx:
            gitfacts.changed_paths(self.repo, base, None)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestRecover(unittest.TestCase):
    def setUp(self):
        self.repo = make_repo()

    def test_reconcile_detects_head_moved_since_the_checkpoint(self):
        run.init("DEMO-500", "u", self.repo)
        state = run.load("DEMO-500")
        state["candidate_sha"] = "0" * 40
        state["status"] = "in_progress"
        run.save(state)
        result = recover.reconcile("DEMO-500")
        self.assertTrue(any("head moved" in a for a in result["actions"]))

    def test_reconcile_blocks_when_repo_is_missing(self):
        run.init("DEMO-501", "u", "/tmp/definitely-not-a-repo-xyz")
        result = recover.reconcile("DEMO-501")
        self.assertTrue(result["blocked"])

    def test_reconcile_bumps_the_generation_even_when_blocked(self):
        # rule: R-LEASE-STALE-WRITE
        # ppl review answered never touches the repo, so a stale attempt could
        # still write through a blocked recovery if the bump waited on facts
        # this path cannot gather. Pinned separately from the "safe to
        # continue" path so a regression that moves the bump below the
        # early return is caught here, not just observed by hand.
        run.init("DEMO-506", "u", "/tmp/definitely-not-a-repo-xyz")
        result = recover.reconcile("DEMO-506")
        self.assertTrue(result["blocked"])
        self.assertEqual(result["facts"]["lease_generation"], 2)
        self.assertEqual(run.load("DEMO-506")["lease_generation"], 2)

    def test_reconcile_reports_a_parked_run_and_its_resume_stage(self):
        run.init("DEMO-502", "u", self.repo)
        run.stage_enter("DEMO-502", "baseline")
        run.park("DEMO-502", "EXTERNAL_BLOCKED", "retry when gitlab responds", [])
        result = recover.reconcile("DEMO-502")
        self.assertTrue(any("parked" in a for a in result["actions"]))
        self.assertTrue(any("baseline" in a for a in result["actions"]))

    def test_reconcile_records_facts_it_read(self):
        run.init("DEMO-503", "u", self.repo)
        result = recover.reconcile("DEMO-503")
        self.assertEqual(result["facts"]["branch"], "master")

    def test_reconcile_bumps_the_lease_generation(self):
        # rule: R-LEASE-STALE-WRITE
        run.init("DEMO-505", "u", self.repo)
        result = recover.reconcile("DEMO-505")
        self.assertEqual(result["facts"]["lease_generation"], 2)
        self.assertEqual(run.load("DEMO-505")["lease_generation"], 2)
        self.assertTrue(any("generation is now 2" in a for a in result["actions"]))
        with self.assertRaises(PplError) as ctx:
            run.check_generation("DEMO-505", 1)
        self.assertEqual(ctx.exception.code, "ILLEGAL_TRANSITION")

    def test_reconcile_reports_an_unusable_control_file_and_does_not_silently_pass(self):
        # A malformed control.json (missing requested_at) makes control.read
        # return an invalid marker with command=None, not a HALTING command.
        # Naive `command["command"] in control.HALTING` would be False here
        # and silently report nothing wrong. reconcile must surface it instead.
        run.init("DEMO-504", "u", self.repo)
        store.write_json(paths.run_dir("DEMO-504") / "control.json", {"command": "pause"})
        result = recover.reconcile("DEMO-504")
        self.assertTrue(any("control file is unusable" in a for a in result["actions"]))
        self.assertFalse(result["blocked"])


class TestIsAncestor(unittest.TestCase):
    def commit(self, repo, name):
        Path(repo, name).write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", repo, "add", name], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", name], check=True)
        return gitfacts.head_sha(repo)

    def test_an_earlier_commit_is_an_ancestor_of_head(self):
        repo = make_repo()
        base = gitfacts.head_sha(repo)
        head = self.commit(repo, "b.txt")
        self.assertTrue(gitfacts.is_ancestor(repo, base, head))

    def test_a_commit_is_its_own_ancestor(self):
        repo = make_repo()
        head = gitfacts.head_sha(repo)
        self.assertTrue(gitfacts.is_ancestor(repo, head, head))

    def test_a_commit_head_does_not_descend_from_is_not_an_ancestor(self):
        repo = make_repo()
        ahead = self.commit(repo, "b.txt")
        subprocess.run(["git", "-C", repo, "reset", "--hard", "-q", "HEAD~1"], check=True)
        self.assertFalse(gitfacts.is_ancestor(repo, ahead, gitfacts.head_sha(repo)))

    def test_an_unknown_revision_is_a_schema_error_not_an_external_block(self):
        repo = make_repo()
        with self.assertRaises(PplError) as ctx:
            gitfacts.is_ancestor(repo, "abc123", gitfacts.head_sha(repo))
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_a_missing_revision_is_refused(self):
        repo = make_repo()
        with self.assertRaises(PplError) as ctx:
            gitfacts.is_ancestor(repo, None, gitfacts.head_sha(repo))
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

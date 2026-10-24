import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import cli, harness, os_sandbox, paths, run, store
from ppl.errors import PplError
from ppl.forges import github_fetch

COMMENTS = [
    {"id": 101, "user": {"login": "alice"}, "body": "first", "in_reply_to_id": None},
    {"id": 102, "user": {"login": "hubot"}, "body": "reply", "in_reply_to_id": 101},
    {"id": 103, "user": {"login": "alice"}, "body": "again", "in_reply_to_id": 101},
    {"id": 201, "user": {"login": "bob"}, "body": "other", "in_reply_to_id": None},
]
REVIEWS = [{"id": 7, "user": {"login": "alice"}, "state": "APPROVED", "commit_id": "abc",
            "submitted_at": "2026-10-24T10:00:00Z", "body": "x"}]


def graphql(*resolved):
    nodes = [{"isResolved": flag, "comments": {"nodes": [{"databaseId": ident}]}}
             for ident, flag in resolved]
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}}


class FakeGh:
    def __init__(self, comments=COMMENTS, reviews=REVIEWS, threads=(), fail=None,
                 comments_text=None):
        self.argvs = []
        self.kwargs = []
        self.comments, self.reviews, self.threads = comments, reviews, threads
        self.fail, self.comments_text = fail, comments_text

    def __call__(self, argv, **kwargs):
        self.argvs.append(list(argv))
        self.kwargs.append(kwargs)
        route = argv[2]
        if self.fail:
            return subprocess.CompletedProcess(argv, 1, "", self.fail)
        if route.endswith("/comments"):
            text = self.comments_text if self.comments_text is not None \
                else json.dumps(self.comments)
        elif route.endswith("/reviews"):
            text = json.dumps(self.reviews)
        else:
            text = json.dumps(graphql(*self.threads))
        return subprocess.CompletedProcess(argv, 0, text, "")


class TestGithubFetch(unittest.TestCase):
    def test_threads_are_grouped_by_reply_id_with_string_ids(self):
        doc = github_fetch.fetch("o/r", 5, FakeGh())
        self.assertEqual(doc["pr"], 5)
        self.assertEqual(doc["threads"], [
            {"id": "101", "author": "alice", "resolved": False, "last_note_id": "103",
             "last_author": "alice", "note_count": 3},
            {"id": "201", "author": "bob", "resolved": False, "last_note_id": "201",
             "last_author": "bob", "note_count": 1}])
        self.assertIn("fetched_at", doc)
        self.assertEqual(doc["reviews"][0]["state"], "APPROVED")
        self.assertEqual(doc["reviews"][0]["author"], "alice")

    def test_resolution_comes_from_graphql_by_database_id(self):
        doc = github_fetch.fetch("o/r", 5, FakeGh(threads=[(101, True), (201, False)]))
        self.assertEqual([t["resolved"] for t in doc["threads"]], [True, False])

    def test_a_thread_missing_from_graphql_is_unresolved(self):
        doc = github_fetch.fetch("o/r", 5, FakeGh(threads=[(999, True)]))
        self.assertEqual([t["resolved"] for t in doc["threads"]], [False, False])

    def test_pagination_concatenates_pages(self):
        text = json.dumps(COMMENTS[:2]) + json.dumps(COMMENTS[2:])
        doc = github_fetch.fetch("o/r", 5, FakeGh(comments_text=text))
        self.assertEqual([t["note_count"] for t in doc["threads"]], [3, 1])

    def test_a_gh_failure_writes_nothing(self):
        with self.assertRaises(PplError) as ctx:
            github_fetch.fetch("o/r", 5, FakeGh(fail="HTTP 502"))
        self.assertEqual(ctx.exception.code, "EXTERNAL_BLOCKED")
        self.assertIn("HTTP 502", str(ctx.exception))

    def test_gh_runs_by_the_pinned_path_with_the_timeout(self):
        gh = FakeGh()
        with mock.patch.object(os_sandbox, "gh_path", return_value="/pin/gh"):
            github_fetch.fetch("o/r", 5, gh)
        self.assertEqual({argv[0] for argv in gh.argvs}, {"/pin/gh"})
        self.assertEqual(gh.argvs[0][1:],
                         ["api", "repos/o/r/pulls/5/comments", "--paginate"])
        self.assertEqual(gh.argvs[1][1:],
                         ["api", "repos/o/r/pulls/5/reviews", "--paginate"])
        self.assertEqual(gh.argvs[2][1:3], ["api", "graphql"])
        self.assertEqual({k["timeout"] for k in gh.kwargs}, {github_fetch.GH_TIMEOUT})

    def test_inside_the_sandbox_the_block_names_the_flag(self):
        with mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": "1"}):
            with self.assertRaises(PplError) as ctx:
                github_fetch.fetch("o/r", 5, FakeGh(fail="no route"))
        self.assertIn("this command reaches GitHub, run it with dangerouslyDisableSandbox",
                      str(ctx.exception))
        with mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": ""}):
            with self.assertRaises(PplError) as ctx:
                github_fetch.fetch("o/r", 5, FakeGh(fail="no route"))
        self.assertNotIn("dangerouslyDisableSandbox", str(ctx.exception))


def call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class TestReviewFetchCli(unittest.TestCase):
    def setUp(self):
        self.task = "GH-9"
        shutil.rmtree(paths.run_dir(self.task), ignore_errors=True)
        run.init(self.task, "https://github.com/o/r/issues/9", "/tmp/repo")
        run.set_active(self.task)
        fixtures.install_variant("example-generic", "gh-forge", forge={"kind": "github"},
                                 **fixtures.GITHUB_ISSUES)
        store.write_json(paths.run_dir(self.task) / "harness.json",
                         harness.snapshot(harness.load("gh-forge")))
        self.facts = paths.home() / os_sandbox.FACTS_FILE
        self.addCleanup(lambda: self.facts.unlink() if self.facts.exists() else None)
        self.gh = FakeGh(threads=[(101, True)])
        patcher = mock.patch("subprocess.run", self.gh)
        patcher.start()
        self.addCleanup(patcher.stop)

    def target(self):
        return paths.run_dir(self.task) / "review-threads.json"

    def write_facts(self, forge_repo="o/r"):
        store.write_json(self.facts, {"version": 1, "forge_repo": forge_repo,
                                      "tracker_repo": "o/r"})

    def test_review_fetch_writes_the_threads_file(self):
        code, out, err = call("review", "fetch", "--pr", "5")
        self.assertEqual(code, 0, err)
        doc = json.loads(self.target().read_text(encoding="utf-8"))
        self.assertEqual(doc["pr"], 5)
        self.assertEqual(len(doc["threads"]), 2)
        self.assertTrue(doc["threads"][0]["resolved"])
        self.assertIn(str(self.target()), out)
        self.assertIn("2 threads", out)

    def test_review_fetch_needs_no_sha(self):
        code, _, err = call("review", "fetch", "--pr", "5")
        self.assertEqual(code, 0, err)
        code, _, err = call("review", "pending", "--threads", str(self.target()))
        self.assertEqual(code, 2)
        self.assertIn("--sha", json.loads(err)["message"])

    def test_pending_reads_the_fetched_document(self):
        call("review", "fetch", "--pr", "5")
        code, out, err = call("review", "pending", "--threads", str(self.target()),
                              "--sha", "abc", "--author", "")
        self.assertEqual(code, 0, err)
        pending = json.loads(out.strip().splitlines()[-1])
        self.assertEqual([t["id"] for t in pending], ["201"])
        self.assertIn("1 of 2 threads pending", out)

    def test_a_disagreeing_snapshot_forge_repo_is_refused(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        self.write_facts(forge_repo="o/other")
        code, _, err = call("review", "fetch", "--pr", "5")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("o/other", json.loads(err)["message"])
        self.assertEqual(self.gh.argvs, [])
        self.assertFalse(self.target().exists())

    def test_the_facts_repo_is_the_one_reached(self):
        self.write_facts()
        code, _, err = call("review", "fetch", "--pr", "5")
        self.assertEqual(code, 0, err)
        self.assertIn("repos/o/r/pulls/5/comments", self.gh.argvs[0])

    def test_a_non_github_forge_is_refused(self):
        fixtures.install_variant("example-generic", "gl-forge", forge={"kind": "gitlab",
                                 "mcp_server": "gitlab"})
        store.write_json(paths.run_dir(self.task) / "harness.json",
                         harness.snapshot(harness.load("gl-forge")))
        code, _, err = call("review", "fetch", "--pr", "5")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertEqual(self.gh.argvs, [])

    def test_a_symlinked_run_dir_is_refused_and_nothing_is_written_outside(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        outside = Path(tmphome.mkdtemp(prefix="ghoutside-"))
        self.addCleanup(shutil.rmtree, str(outside), True)
        run_dir = paths.run_dir(self.task)
        moved = run_dir.with_name(self.task + "-real")
        self.addCleanup(shutil.rmtree, str(moved), True)
        run_dir.rename(moved)
        os.symlink(str(outside), str(run_dir))
        self.addCleanup(lambda: run_dir.unlink() if run_dir.is_symlink() else None)
        code, _, err = call("review", "fetch", "--pr", "5")
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(err)["error_code"], "EXTERNAL_BLOCKED")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.gh.argvs, [])

    def test_a_dir_swapped_for_a_link_during_the_fetch_is_refused(self):
        outside = Path(tmphome.mkdtemp(prefix="ghoutside-"))
        self.addCleanup(shutil.rmtree, str(outside), True)
        run_dir = paths.run_dir(self.task)
        moved = run_dir.with_name(self.task + "-real")
        self.addCleanup(shutil.rmtree, str(moved), True)
        real_call = self.gh.__call__

        def swapping(argv, **kwargs):
            if argv[2] == "graphql" and not run_dir.is_symlink():
                run_dir.rename(moved)
                os.symlink(str(outside), str(run_dir))
            return real_call(argv, **kwargs)

        self.addCleanup(lambda: run_dir.unlink() if run_dir.is_symlink() else None)
        with mock.patch("subprocess.run", swapping):
            code, _, err = call("review", "fetch", "--pr", "5")
        self.assertEqual(json.loads(err)["error_code"], "EXTERNAL_BLOCKED")
        self.assertEqual(list(outside.iterdir()), [])

    def test_sha_is_still_required_for_the_other_actions(self):
        for action in ("answered", "approved", "pending"):
            code, _, err = call("review", action, "--thread", "t", "--threads", "x")
            self.assertEqual(code, 2, action)
            self.assertIn("--sha", json.loads(err)["message"])


if __name__ == "__main__":
    unittest.main()

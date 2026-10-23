"""`ppl trail export`: a run's documents copied from an allowlist, redacted, checked for
leaks and listed in a manifest with hashes and measures."""
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import ppl
from ppl import evidence, findings, paths, review, run, store, trail, usage, verdict
from ppl.errors import PplError
from ppl.trackers import github_issues

COUNTER = itertools.count(1)
PRODUCER = {"provider": "claude-code-subagent", "model": "claude-test", "params_hash": ""}
BUILD_LOG = "collected 7 items, the build log body that must stay out of a trail\n"
MODELS = {"claude-test": {"input": 100, "cache_write": 10, "cache_read": 1000, "output": 50,
                          "thinking": 5}}
PRICES = '[prices."claude-test"]\ninput = %s\ncache_write = %s\ncache_read = %s\noutput = %s\n'
TOKEN = "ghp_" + "Ab1" * 12
# the guard keeps this many characters of a denied call's target
TARGET_CAP = 200


def cut_target(head, tail_kept):
    """A denied command's target as the guard logs it: cut at TARGET_CAP characters,
    ending with `tail_kept`."""
    pad = TARGET_CAP - len(head) - 1 - len(tail_kept)
    target = (head + "a" * pad + " " + tail_kept + "rest-of-the-command")[:TARGET_CAP]
    assert target.endswith(tail_kept)
    return target


def priced(prices):
    return sum(MODELS["claude-test"][name] * prices[name] for name in usage.COUNTS) / 1000000


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t"]
                   + list(args), check=True, capture_output=True)


def commit(repo, name, text):
    target = Path(repo, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def record_verdict(task, sha, outcome):
    verdict.record(task, {"task_id": task, "candidate_sha": sha, "spec_revision": "sha256:x",
                          "verdict": outcome,
                          "acceptance_evidence": {"AC1": "evidence:focused-tests"},
                          "findings": [], "producer": PRODUCER}, sha)


def write_issue(task, home):
    number = int(task.split("-")[1])
    issue = {
        "number": number, "title": "Cart total drops a cent", "state": "OPEN",
        "body": "Seen with the notes in %s/notes/cart.txt open." % home,
        "labels": [], "assignees": [{"login": "octocat"}], "author": {"login": "octocat"},
        "createdAt": "2026-10-22T00:00:00Z", "updatedAt": "2026-10-22T00:00:00Z",
        "url": "https://github.com/acme/shop/issues/%d" % number,
        "comments": [{"author": {"login": "monalisa"}, "body": "Same here.",
                      "createdAt": "2026-10-22T01:00:00Z"}],
    }
    doc = {"issue": issue, "links": {"parent": "unavailable", "sub_issues": "unavailable"}}
    folder = paths.run_dir(task) / "tracker"
    store.write_json(folder / "issue.json", doc)
    store.write_text(folder / "issue.md", github_issues.render_issue(doc))


def rewrite_issue(task, change):
    """issue.json changed by `change(doc)`, and issue.md rendered again from it."""
    folder = paths.run_dir(task) / "tracker"
    doc = store.read_json(folder / "issue.json")
    change(doc)
    store.write_json(folder / "issue.json", doc)
    store.write_text(folder / "issue.md", github_issues.render_issue(doc))


def build_run(home, logs):
    """A finished run written by the real writers: two candidates (REWORK, then PASS),
    receipts with a build log under /tmp, two review threads, a finding, a denial and
    a usage event."""
    number = next(COUNTER)
    task = "GH-%d" % number
    repo = os.path.join(home, "work", "shop")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    base = commit(repo, "shop/cart.py", "one\n")
    run.init(task, "https://github.com/acme/shop/issues/%d" % number, repo)
    state = run.load(task)
    state["base_sha"] = base
    run.save(state)
    write_issue(task, home)
    rework = commit(repo, "shop/cart.py", "two\n")
    passed = commit(repo, "shop/cart.py", "three\n")
    rc = Path(logs, "rc")
    rc.write_text("0\n", encoding="utf-8")
    for sha in (rework, passed):
        log = Path(logs, "focused-tests-%s.log" % sha[:7])
        log.write_text(BUILD_LOG, encoding="utf-8")
        evidence.add(task, "focused-tests", "pass", sha, str(log), str(rc), PRODUCER)
    record_verdict(task, rework, "REWORK")
    findings.add(task, "important", "the total drops a cent, gate log in /tmp/ppl-gates/cart.log",
                 rework, "shop/cart.py")
    store.append_log(task, {"event": "deny", "role": "ppl-test-worker", "tool": "Read",
                            "code": "BOUNDARY_VIOLATION",
                            "target": os.path.realpath(repo) + "/shop/cart.py"})
    store.append_log(task, {"event": "usage", "role": "ppl-code-worker", "agent_id": "agent-1",
                            "effort": "medium", "models": MODELS})
    record_verdict(task, passed, "PASS")
    review.set_mr(task, 7, "https://github.com/acme/shop/pull/7", author="octocat")
    review.record_reply(task, "thread-1", passed, "NEEDS_CODE_CHANGE")
    review.record_reply(task, "thread-2", passed, "NEEDS_REPLY_ONLY")
    store.append_log(task, {"event": "lifecycle", "to": "done"})
    return task, repo, rework, passed


def files_under(out):
    return sorted(p for p in Path(out).rglob("*") if p.is_file())


def texts(out):
    return {p.relative_to(out).as_posix(): p.read_text(encoding="utf-8") for p in files_under(out)}


class TestTrailExport(unittest.TestCase):
    def setUp(self):
        saved = {name: os.environ.get(name) for name in ("HOME", "PPL_HOME")}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        self.addCleanup(restore)
        # find_run scans the whole fleet, so each test gets its own
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        self.home = tmphome.mkdtemp(prefix="pplfakehome-")
        os.environ["HOME"] = self.home
        self.logs = tempfile.mkdtemp(prefix="ppltrail-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.logs, True)
        self.task, self.repo, self.rework, self.passed = build_run(self.home, self.logs)
        self.parent = Path(tmphome.mkdtemp(prefix="ppltrailout-"))
        self.out = self.parent / "trail"

    def export(self, out=None, **kwargs):
        return trail.export(self.task, str(out or self.out), **kwargs)

    def test_the_manifest_hashes_every_file(self):
        manifest = self.export()
        on_disk = json.loads((self.out / "MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, manifest)
        listed = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
        written = [p.relative_to(self.out).as_posix() for p in files_under(self.out)]
        self.assertEqual(sorted(listed), sorted(p for p in written if p != "MANIFEST.json"))
        for rel, digest in listed.items():
            actual = hashlib.sha256((self.out / rel).read_bytes()).hexdigest()
            self.assertEqual(actual, digest, rel)
        for rel in ("index.html", "docs/brief.md", "docs/verdicts.md", "docs/threads.md",
                    "docs/state.json", "docs/log.jsonl", "docs/threads.json",
                    "docs/tracker/issue.json", "docs/tracker/issue.md",
                    "docs/verdicts/%s.json" % self.rework, "docs/verdicts/%s/1.json" % self.passed,
                    "docs/evidence/%s/focused-tests.json" % self.passed):
            self.assertIn(rel, listed)
        self.assertEqual(manifest["task_id"], self.task)
        self.assertEqual(manifest["base_sha"], run.load(self.task)["base_sha"])
        self.assertEqual(manifest["head_sha"], self.passed)
        self.assertEqual(manifest["antline_version"], ppl.__version__)

    def test_the_repo_home_and_tmp_paths_are_redacted(self):
        self.export()
        found = texts(self.out)
        forbidden = {self.repo, os.path.realpath(self.repo), self.home,
                     os.path.realpath(self.home), os.environ["PPL_HOME"],
                     os.path.realpath(os.environ["PPL_HOME"]), "/tmp/"}
        for rel, text in found.items():
            for needle in forbidden:
                self.assertNotIn(needle, text, rel)
        state = json.loads(found["docs/state.json"])
        self.assertEqual(state["repo"], "<repo>")
        self.assertNotIn("orchestrator", state)
        self.assertIn('"target": "<repo>/shop/cart.py"', found["docs/log.jsonl"])
        self.assertIn("<tmp>/ppl-gates/cart.log", "".join(found.values()))
        manifest = json.loads(found["MANIFEST.json"])
        self.assertIn("<repo>", manifest["redactions"]["paths"])

    def test_a_home_path_inside_the_issue_markdown_is_redacted(self):
        self.export()
        text = (self.out / "docs" / "tracker" / "issue.md").read_text(encoding="utf-8")
        self.assertNotIn(self.home, text)
        self.assertIn("<home>/notes/cart.txt", text)

    def test_people_become_stable_aliases(self):
        manifest = self.export()
        found = texts(self.out)
        for rel, text in found.items():
            self.assertNotIn("octocat", text.lower(), rel)
            self.assertNotIn("monalisa", text.lower(), rel)
        self.assertIn("- Author: @person-1", found["docs/tracker/issue.md"])
        self.assertIn("### @person-2,", found["docs/tracker/issue.md"])
        self.assertEqual(json.loads(found["docs/state.json"])["mr"]["author"], "@person-1")
        self.assertEqual(manifest["redactions"]["people"], 2)
        again = self.parent / "again"
        self.export(again)
        self.assertEqual(texts(again), found)

    def test_urls_are_kept_unless_asked(self):
        manifest = self.export()
        state = json.loads((self.out / "docs" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["tracker_url"], "https://github.com/acme/shop/issues/%s"
                         % self.task.split("-")[1])
        self.assertEqual(state["mr"]["url"], "https://github.com/acme/shop/pull/7")
        self.assertIs(manifest["redactions"]["urls"], False)

    def test_urls_become_placeholders_when_asked(self):
        manifest = self.export(redact=("paths", "people", "urls"))
        for rel, text in texts(self.out).items():
            self.assertNotIn("https://", text, rel)
        state = json.loads((self.out / "docs" / "state.json").read_text(encoding="utf-8"))
        self.assertRegex(state["tracker_url"], r"^<url-[0-9]+>$")
        self.assertRegex(state["mr"]["url"], r"^<url-[0-9]+>$")
        self.assertNotEqual(state["tracker_url"], state["mr"]["url"])
        self.assertEqual(manifest["redactions"]["urls"], 2)

    def test_a_leak_refuses_and_leaves_no_output(self):
        # rule: R-TRAIL-REDACTED
        token = "ghp_" + "Ab1" * 12
        record = findings.add(self.task, "minor", "the gate printed %s" % token, self.rework,
                              "shop/cart.py")
        with self.assertRaises(PplError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("docs/findings/%s.json" % record["finding_id"], caught.exception.message)
        self.assertNotIn(token, caught.exception.message)
        self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_build_logs_are_never_copied(self):
        self.export()
        for rel, text in texts(self.out).items():
            self.assertNotIn(BUILD_LOG.strip(), text, rel)
        receipts = list((self.out / "docs" / "evidence").rglob("*.json"))
        self.assertEqual(len(receipts), 2)
        for path in receipts:
            receipt = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("log", receipt)
            self.assertEqual(receipt["rc"], 0)

    def test_usage_json_and_unknown_files_are_skipped_and_listed(self):
        transcript = Path(self.home, ".claude", "projects", "session.jsonl")
        transcript.parent.mkdir(parents=True)
        transcript.write_text("", encoding="utf-8")
        usage.note_orchestrator(self.task, str(transcript), "session-1")
        self.assertTrue((paths.run_dir(self.task) / "usage.json").is_file())
        Path(paths.run_dir(self.task), "notes.txt").write_text("scratch\n", encoding="utf-8")
        manifest = self.export()
        self.assertIn("usage.json", manifest["skipped"])
        self.assertIn("notes.txt", manifest["skipped"])
        self.assertFalse((self.out / "docs" / "usage.json").exists())
        self.assertFalse((self.out / "docs" / "notes.txt").exists())

    def test_an_existing_out_is_refused(self):
        self.out.mkdir()
        with self.assertRaises(PplError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual([p.name for p in self.parent.iterdir()], ["trail"])

    def test_redact_without_paths_is_refused(self):
        for redact in (("people",), ("people", "urls"), ("paths", "secrets")):
            with self.assertRaises(PplError) as caught:
                self.export(redact=redact)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", redact)
            self.assertFalse(os.path.lexists(self.out), redact)

    def test_an_archived_run_exports(self):
        run.destroy(self.task, "local")
        self.assertFalse(paths.run_dir(self.task).exists())
        manifest = self.export()
        self.assertEqual(manifest["task_id"], self.task)
        self.assertEqual(manifest["head_sha"], self.passed)
        log = (self.out / "docs" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"event": "destroy"', log)
        self.assertNotIn(self.repo, log)

    def test_an_unknown_task_is_refused(self):
        with self.assertRaises(PplError) as caught:
            trail.export("GH-9999", str(self.out))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertFalse(os.path.lexists(self.out))

    def test_measures_count_rework_rounds_denials_and_tokens(self):
        measures = self.export()["measures"]
        self.assertEqual(measures["rework_rounds"], 1)
        self.assertEqual(measures["denials"], 1)
        self.assertEqual(measures["verdicts"], {"PASS": 1, "REWORK": 1})
        self.assertEqual(measures["tokens_by_role"], {"ppl-code-worker": {
            "input": 100, "cache_write": 10, "cache_read": 1000, "output": 50}})
        self.assertGreaterEqual(measures["wall_clock_seconds"], 0)
        self.assertIsNone(measures["usd"])
        self.assertIn("claude-test", measures["reasons"]["usd"])

    def test_usd_is_priced_once_every_model_has_a_price(self):
        paths.config_file().write_text(
            '[prices."claude-test"]\ninput = 3\ncache_write = 3.75\ncache_read = 0.3\n'
            'output = 15\n', encoding="utf-8")
        measures = trail.measures(trail.find_run(self.task))
        expected = (100 * 3 + 10 * 3.75 + 1000 * 0.3 + 50 * 15) / 1000000
        self.assertAlmostEqual(measures["usd"], expected)
        self.assertNotIn("usd", measures["reasons"])

    def test_nothing_recorded_is_null_not_zero(self):
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        run.init("GH-500", "https://github.com/acme/shop/issues/500", self.repo)
        measures = trail.measures(trail.find_run("GH-500"))
        self.assertIsNone(measures["usd"])
        self.assertIsNone(measures["tokens_by_role"])
        self.assertIn("usd", measures["reasons"])
        self.assertIn("tokens_by_role", measures["reasons"])

    def test_a_deny_target_cut_inside_the_home_path_is_redacted(self):
        # rule: R-TRAIL-REDACTED
        target = cut_target("cat ", self.home[:-3])
        store.append_log(self.task, {"event": "deny", "role": "ppl-code-worker", "tool": "Bash",
                                     "code": "BOUNDARY_VIOLATION", "target": target})
        self.export()
        log = (self.out / "docs" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"target": "%s<home>..."' % target[:-len(self.home[:-3])], log)
        fragment = os.path.basename(self.home)[:-3]
        for rel, text in texts(self.out).items():
            self.assertNotIn(self.home[:-3], text, rel)
            self.assertNotIn(fragment, text, rel)

    def test_a_deny_target_cut_inside_a_token_refuses(self):
        # rule: R-TRAIL-REDACTED
        target = cut_target("echo ", TOKEN[:29])
        store.append_log(self.task, {"event": "deny", "role": "ppl-code-worker", "tool": "Bash",
                                     "code": "BOUNDARY_VIOLATION", "target": target})
        with self.assertRaises(PplError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("docs/log.jsonl", caught.exception.message)
        self.assertNotIn(TOKEN[:29], caught.exception.message)
        self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_a_path_redaction_misses_refuses(self):
        # rule: R-TRAIL-REDACTED
        record = findings.add(self.task, "minor", "copied to x%s/notes/cart.txt" % self.home,
                              self.rework, "shop/cart.py")
        with self.assertRaises(PplError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("docs/findings/%s.json still holds the home directory"
                      % record["finding_id"], caught.exception.message)
        self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(list(self.parent.iterdir()), [])

    def own_repository(self):
        """The run's tracker and MR live in octocat's own repository."""
        state = run.load(self.task)
        state["tracker_url"] = "https://github.com/octocat/shop/issues/1"
        run.save(state)
        review.set_mr(self.task, 3, "https://github.com/octocat/shop/pull/3", author="octocat")

    def test_a_kept_url_keeps_the_owner_login(self):
        self.own_repository()
        owner = {"login": "octocat", "id": 583231,
                 "avatar_url": "https://avatars.githubusercontent.com/u/583231?v=4"}

        def full_author(doc):
            doc["issue"]["author"] = dict(owner)

        rewrite_issue(self.task, full_author)
        manifest = self.export()
        issue_doc = json.loads((self.out / "docs" / "tracker" / "issue.json")
                               .read_text(encoding="utf-8"))
        self.assertEqual(issue_doc["issue"]["author"], owner)
        state = json.loads((self.out / "docs" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["tracker_url"], "https://github.com/octocat/shop/issues/1")
        self.assertEqual(state["mr"]["url"], "https://github.com/octocat/shop/pull/3")
        self.assertEqual(state["mr"]["author"], "octocat")
        issue = (self.out / "docs" / "tracker" / "issue.md").read_text(encoding="utf-8")
        self.assertIn("- Author: octocat", issue)
        self.assertIn("### @person-1,", issue)
        log = (self.out / "docs" / "log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"url": "https://github.com/octocat/shop/pull/3"', log)
        self.assertEqual(manifest["redactions"]["people"], 1)
        for rel, text in texts(self.out).items():
            self.assertNotIn("monalisa", text, rel)

    def test_the_owner_login_is_a_person_when_urls_are_redacted(self):
        # rule: R-TRAIL-REDACTED
        self.own_repository()
        self.export(redact=("paths", "people", "urls"))
        for rel, text in texts(self.out).items():
            self.assertNotIn("octocat", text, rel)
        state = json.loads((self.out / "docs" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["mr"]["author"], "@person-1")

    def test_a_login_glued_to_a_kept_url_is_aliased(self):
        # rule: R-TRAIL-REDACTED
        record = findings.add(self.task, "minor",
                              "see https://github.com/a/b/pull/3,monalisa and @monalisa",
                              self.rework, "shop/cart.py")
        self.export()
        for rel, text in texts(self.out).items():
            self.assertNotIn("monalisa", text, rel)
        finding = json.loads((self.out / "docs" / "findings" / ("%s.json" % record["finding_id"]))
                             .read_text(encoding="utf-8"))
        self.assertEqual(finding["evidence"],
                         "see https://github.com/a/b/pull/3,@person-2 and @person-2")

    def test_another_persons_profile_url_is_aliased(self):
        # rule: R-TRAIL-REDACTED
        def parent(doc):
            doc["links"]["parent"] = {
                "number": 1, "title": "Checkout epic, ask https://github.com/hubot",
                "url": "https://github.com/acme/shop/issues/1",
                "user": {"login": "hubot", "html_url": "https://github.com/hubot"}}

        rewrite_issue(self.task, parent)
        self.export()
        for rel, text in texts(self.out).items():
            self.assertNotIn("hubot", text, rel)
        issue = json.loads((self.out / "docs" / "tracker" / "issue.json")
                           .read_text(encoding="utf-8"))
        parent = issue["links"]["parent"]
        self.assertRegex(parent["user"]["login"], r"^@person-[0-9]+$")
        self.assertEqual(parent["title"],
                         "Checkout epic, ask https://github.com/" + parent["user"]["login"])

    def test_a_login_after_a_percent_escape_is_aliased(self):
        # rule: R-TRAIL-REDACTED
        record = findings.add(self.task, "minor",
                              "see https://github.com/acme/shop/issues?q=is%3Apr+author%3Amonalisa"
                              " and cc %40monalisa", self.rework, "shop/cart.py")
        self.export()
        for rel, text in texts(self.out).items():
            self.assertNotIn("monalisa", text, rel)
        finding = json.loads((self.out / "docs" / "findings" / ("%s.json" % record["finding_id"]))
                             .read_text(encoding="utf-8"))
        self.assertEqual(finding["evidence"],
                         "see https://github.com/acme/shop/issues?q=is%3Apr+author%3A@person-2"
                         " and cc %40@person-2")

    def test_a_person_object_is_reduced_to_its_alias(self):
        # rule: R-TRAIL-REDACTED
        user = {"login": "hubot", "id": 48151623, "node_id": "MDQ6VXNlcjQ4MTUxNjIz",
                "avatar_url": "https://avatars.githubusercontent.com/u/48151623?v=4",
                "gravatar_id": "", "html_url": "https://github.com/hubot",
                "followers_url": "https://api.github.com/users/hubot/followers",
                "type": "User", "site_admin": False}

        def rest_shaped(doc):
            doc["issue"]["author"] = {"id": "MDQ6VXNlcjU4MzIzMQ==", "is_bot": False,
                                      "login": "octocat", "name": "The Octocat"}
            doc["links"]["parent"] = {"number": 1, "title": "Checkout epic",
                                      "url": "https://github.com/acme/shop/issues/1",
                                      "user": user}

        rewrite_issue(self.task, rest_shaped)
        self.export()
        issue = json.loads((self.out / "docs" / "tracker" / "issue.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(issue["issue"]["author"], {"login": "@person-1"})
        self.assertRegex(issue["links"]["parent"]["user"]["login"], r"^@person-[0-9]+$")
        self.assertEqual(list(issue["links"]["parent"]["user"]), ["login"])
        for rel, text in texts(self.out).items():
            for needle in ("48151623", "MDQ6VXNlcjQ4MTUxNjIz", "MDQ6VXNlcjU4MzIzMQ==",
                           "avatars.githubusercontent.com", "hubot", "Octocat"):
                self.assertNotIn(needle, text, rel)

    def test_a_value_ending_in_a_bare_root_component_is_kept(self):
        def body(doc):
            doc["issue"]["body"] = 'Navigate to the "/var" page, or open https://example.com/home'

        rewrite_issue(self.task, body)
        for target in ("cd /var", "ls /Users", "open https://example.com/home"):
            store.append_log(self.task, {"event": "deny", "role": "ppl-code-worker",
                                         "tool": "Bash", "code": "BOUNDARY_VIOLATION",
                                         "target": target})
        self.export()
        log = (self.out / "docs" / "log.jsonl").read_text(encoding="utf-8")
        for target in ("cd /var", "ls /Users", "open https://example.com/home"):
            self.assertIn('"target": "%s"' % target, log)
        issue = (self.out / "docs" / "tracker" / "issue.md").read_text(encoding="utf-8")
        self.assertIn('Navigate to the "/var" page, or open https://example.com/home', issue)

    def test_a_placeholder_used_only_in_the_measures_is_listed(self):
        paths.config_file().write_text("[prices\n", encoding="utf-8")
        manifest = self.export()
        self.assertIn("<ppl-home>/config.toml", manifest["measures"]["reasons"]["usd"])
        self.assertIn("<ppl-home>", manifest["redactions"]["paths"])

    def test_an_out_created_during_the_export_is_refused(self):
        real = trail.measures

        def racing(directory):
            self.out.mkdir()
            return real(directory)

        with mock.patch.object(trail, "measures", racing):
            with self.assertRaises(PplError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(list(self.out.iterdir()), [])
        self.assertEqual([p.name for p in self.parent.iterdir()], ["trail"])

    def test_a_failed_rename_is_refused_and_leaves_nothing(self):
        with mock.patch("ppl.trail.os.rename", side_effect=OSError("cross-device link")):
            with self.assertRaises(PplError) as caught:
                self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("cross-device link", caught.exception.message)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_a_config_error_in_the_measures_is_redacted(self):
        paths.config_file().write_text("[prices\n", encoding="utf-8")
        manifest = self.export()
        self.assertIsNone(manifest["measures"]["usd"])
        self.assertIn("<ppl-home>/config.toml", manifest["measures"]["reasons"]["usd"])

    def test_usd_uses_the_prices_frozen_at_init(self):
        frozen = {"input": 3, "cache_write": 3.75, "cache_read": 0.3, "output": 15}
        paths.config_file().write_text(PRICES % tuple(frozen[n] for n in usage.COUNTS),
                                       encoding="utf-8")
        run.init("GH-600", "https://github.com/acme/shop/issues/600", self.repo)
        store.append_log("GH-600", {"event": "usage", "role": "ppl-code-worker",
                                    "agent_id": "agent-1", "models": MODELS})
        paths.config_file().write_text(PRICES % (30, 37.5, 3, 150), encoding="utf-8")
        measures = trail.measures(trail.find_run("GH-600"))
        self.assertAlmostEqual(measures["usd"], priced(frozen))

    def test_a_relative_repo_is_refused(self):
        state = run.load(self.task)
        state["repo"] = "work/shop"
        run.save(state)
        with self.assertRaises(PplError) as caught:
            self.export()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("work/shop", caught.exception.message)
        self.assertFalse(os.path.lexists(self.out))
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_the_page_links_only_relative_docs(self):
        self.export(redact=("paths", "people"))
        page = (self.out / "index.html").read_text(encoding="utf-8")
        for needle in ("http://", "https://", "//cdn", "<script", " src="):
            self.assertNotIn(needle, page)
        links = re.findall(r'href="([^"]*)"', page)
        self.assertTrue(links)
        for link in links:
            self.assertTrue(link.startswith("docs/"), link)
            self.assertTrue((self.out / link).is_file(), link)


class TestRedactor(unittest.TestCase):
    """The redactor alone, on a home that is not under a temp root."""

    def setUp(self):
        self.redactor = trail.Redactor({}, home="/Users/alexander.smith",
                                       ppl_home="/Users/alexander.smith/.ppl", temp_roots=(),
                                       people=False, urls=False)

    def test_a_value_cut_inside_the_home_becomes_its_placeholder(self):
        self.assertEqual(self.redactor.text("cat /Users/alexander.sm"), "cat <home>...")
        self.assertEqual(self.redactor.text("cat /Users/alexander.smith/.pp"), "cat <ppl-home>...")
        self.assertEqual(self.redactor.text("ls /Users/a"), "ls <home>...")
        self.assertEqual(self.redactor.text("ls /Use"), "ls /Use")
        self.assertEqual(self.redactor.text("ls /Us"), "ls /Us")

    def test_a_cut_path_or_token_left_in_a_file_is_a_leak(self):
        self.assertTrue(trail.leaks('{"target": "cat /Users/alexander.sm"}\n', self.redactor))
        self.assertTrue(trail.leaks('{"target": "echo %s"}\n' % TOKEN[:29], self.redactor))
        self.assertTrue(trail.leaks("echo %s" % TOKEN[:8], self.redactor))
        self.assertEqual(trail.leaks('{"target": "cat <home>..."}\n', self.redactor), [])
        self.assertEqual(trail.leaks('{"target": "echo ghp_Ab1"}\n', self.redactor), [])

    def test_a_bare_root_component_is_not_cut(self):
        linux = trail.Redactor({}, home="/home/alexander", ppl_home="/home/alexander/.ppl",
                               temp_roots=("/tmp", "/var/folders"), people=False, urls=False)
        for value in ("https://example.com/home", "cd /var", "ls /home", "ls /home/"):
            self.assertEqual(linux.text(value), value)
        self.assertEqual(linux.text("ls /home/al"), "ls <home>...")
        self.assertEqual(trail.leaks('Navigate to the "/home" page\n', linux), [])
        self.assertEqual(trail.leaks('{"target": "cd /var"}\n', linux), [])
        self.assertTrue(trail.leaks('{"target": "ls /home/al"}\n', linux))

    def test_a_cut_inside_a_user_specific_first_component_is_redacted(self):
        # rule: R-TRAIL-REDACTED
        own = trail.Redactor({"repo": "/alexsmith-work/shop"}, home="/home/alexander",
                             ppl_home="/home/alexander/.ppl", temp_roots=("/tmp",),
                             people=False, urls=False)
        self.assertEqual(own.text("cd /alexsm"), "cd <repo>...")
        self.assertNotIn("/alexsm", own.text("cd /alexsm"))
        self.assertTrue(trail.leaks('{"target": "cd /alexsm"}\n', own))
        self.assertEqual(own.text("cd /data/x"), "cd /data/x")

    def test_a_single_component_repo_is_cut_checked(self):
        # rule: R-TRAIL-REDACTED
        own = trail.Redactor({"repo": "/alexsmith"}, home="/home/alexander",
                             ppl_home="/home/alexander/.ppl", temp_roots=("/tmp",),
                             people=False, urls=False)
        self.assertEqual(own.text("cd /alexs"), "cd <repo>...")
        self.assertNotIn("/alexs", own.text("cd /alexs"))
        self.assertTrue(trail.leaks('{"target": "cd /alexs"}\n', own))
        self.assertEqual(own.text("cd /al"), "cd /al")

    def test_a_login_after_a_percent_escape_is_a_leak(self):
        people = trail.Redactor({}, home="/home/alexander", ppl_home="/home/alexander/.ppl",
                                temp_roots=(), people=True, urls=False)
        people.find_people({"issue.json": {"author": {"login": "monalisa"}}})
        self.assertTrue(trail.leaks('{"q": "author%3Amonalisa"}\n', people))
        self.assertTrue(trail.leaks('{"q": "cc %40monalisa"}\n', people))
        self.assertEqual(trail.leaks('{"q": "author%3A@person-1"}\n', people), [])

    def test_a_very_long_home_path_does_not_crash(self):
        home = "/home/" + "a" * 1000
        long = trail.Redactor({}, home=home, ppl_home="/srv/ppl", temp_roots=(), people=False,
                              urls=False)
        self.assertEqual(long.text("cat " + home[:600]), "cat <home>...")
        self.assertTrue(trail.leaks('{"target": "cat %s"}\n' % home[:600], long))
        self.assertEqual(trail.leaks('{"target": "cat <home>..."}\n', long), [])


if __name__ == "__main__":
    unittest.main()

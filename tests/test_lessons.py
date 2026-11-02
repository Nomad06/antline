"""`ppl lessons bundle`: a merged run's evidence, written as files for the reflector."""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import cli, evidence, findings, harness, lessons, memory, paths, review, run, store, verdict
from ppl.errors import PplError

PRODUCER = {"provider": "claude-code-subagent", "model": "claude-test", "params_hash": ""}
MODELS = {"claude-test": {"input": 100, "cache_write": 10, "cache_read": 1000, "output": 50,
                          "thinking": 5}}
NOTE_BODY = "Keep the rounding in one place, the cart and the invoice disagree."
OFF_LINE = "reflection is off ([lessons] reflect = false)"


def git(repo, *args):
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t"]
                   + list(args), check=True, capture_output=True)


def commit(repo, name, text):
    Path(repo, name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", name)
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                          text=True, check=True).stdout.strip()


def call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def record_verdict(task, sha, outcome):
    verdict.record(task, {"task_id": task, "candidate_sha": sha, "spec_revision": "sha256:x",
                          "verdict": outcome,
                          "acceptance_evidence": {"AC1": "evidence:focused-tests"},
                          "findings": [], "producer": PRODUCER}, sha)


def threads_doc(thread_id="1001"):
    """review-threads.json as `ppl review fetch` writes it on GitHub."""
    return {"fetched_at": "2026-10-28T00:00:00Z", "pr": 7, "reviews": [],
            "threads": [{"id": thread_id, "author": "monalisa", "resolved": False,
                         "last_note_id": "1002", "last_author": "monalisa", "note_count": 2,
                         "notes": [{"id": thread_id, "author": "monalisa", "path": "cart.py",
                                    "body": NOTE_BODY},
                                   {"id": "1002", "author": "monalisa", "path": "cart.py",
                                    "body": "Still open."}]}]}


def read_bundle(target, name):
    return json.loads((Path(target) / name).read_text(encoding="utf-8"))


class LessonsTestCase(unittest.TestCase):
    """A fresh data root per test with example-maven installed, one lesson in its memory,
    and the reflector's agent file in place."""

    def setUp(self):
        self._env = {key: os.environ.get(key) for key in ("PPL_HOME", "PPL_AGENTS_DIR")}
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        os.environ["PPL_AGENTS_DIR"] = tmphome.mkdtemp(prefix="pplagents-")
        self.reflector = Path(os.environ["PPL_AGENTS_DIR"]) / "ppl-reflector.md"
        self.reflector.write_text("---\nname: ppl-reflector\n---\n", encoding="utf-8")
        fixtures.install_harness("example-maven")
        row = {"id": "M-1", "role": "ppl-code-worker", "cue": "a change touches the cart",
               "lesson": "run the cart tests before handing back",
               "sources": ["DEMO-1:finding:F-1"], "accepted_at": "2026-10-28T00:00:00Z",
               "seen": 1, "status": "active"}
        (paths.harness_dir("example-maven") / "memory.toml").write_text(
            memory.dumps([row]), encoding="utf-8")

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def build_run(self, task):
        """A run walked by the real writers up to merge_watch, the merge not yet recorded: a
        REWORK then a PASS verdict, a finding, a receipt, a park and its resume, a denial, a
        usage event, a reviewer thread with its notes and its ledger record."""
        repo = tmphome.mkdtemp(prefix="pplrepo-")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
        base = commit(repo, "cart.py", "one\n")
        rework = commit(repo, "cart.py", "two\n")
        passed = commit(repo, "cart.py", "three\n")
        doc = harness.load("example-maven")
        run.init(task, "https://tracker/%s" % task, repo,
                 harness={"name": "example-maven", "path": doc["_dir"], "format": harness.FORMAT})
        directory = paths.run_dir(task)
        store.write_json(directory / "harness.json", harness.snapshot(doc))
        store.write_json(directory / "brief.json", {"task_id": task, "acceptance": ["AC1"]})
        state = run.load(task)
        state["base_sha"] = base
        state["stage"] = "verification"
        state["status"] = "in_progress"
        run.save(state)
        rc = Path(repo, "rc")
        rc.write_text("0\n", encoding="utf-8")
        log = Path(repo, "focused-tests.log")
        log.write_text("the build log body\n", encoding="utf-8")
        evidence.add(task, "focused-tests", "pass", passed, str(log), str(rc), PRODUCER)
        record_verdict(task, rework, "REWORK")
        finding = findings.add(task, "important", "the total drops a cent", rework, "cart.py")
        run.park(task, "EXTERNAL_BLOCKED", "wait for the forge", [])
        run.stage_enter(task, "verification")
        store.append_log(task, {"event": "deny", "role": "ppl-test-worker", "tool": "Read",
                                "code": "BOUNDARY_VIOLATION", "target": "cart.py"})
        store.append_log(task, {"event": "usage", "role": "ppl-code-worker",
                                "agent_id": "agent-1", "effort": "medium", "models": MODELS})
        record_verdict(task, passed, "PASS")
        state = run.load(task)
        state["stage"] = "merge_watch"
        state["status"] = "done"
        run.save(state)
        store.write_json(directory / "review-threads.json", threads_doc())
        review.record_reply(task, "1001", passed, "NEEDS_CODE_CHANGE", "1002")
        return {"task": task, "rework": rework, "passed": passed,
                "finding": finding["finding_id"], "directory": directory}

    def merge(self, built):
        code, _, err = call("merge", "observed", "--sha", built["passed"], "--task",
                            built["task"])
        self.assertEqual(code, 0, err)


class TestBundle(LessonsTestCase):
    def test_bundle_holds_every_file_with_the_runs_evidence(self):
        built = self.build_run("DEMO-9101")
        self.merge(built)
        code, out, err = call("lessons", "bundle", "--task", "DEMO-9101")
        self.assertEqual(code, 0, err)
        target = lessons.bundle_dir(built["directory"])
        self.assertEqual(target, built["directory"] / "lessons" / "bundle")
        self.assertEqual(out.strip(), "bundle: %s" % target)
        for name in lessons.BUNDLE_FILES:
            self.assertTrue((target / name).is_file(), name)
        self.assertEqual(sorted(p.name for p in (built["directory"] / "lessons").iterdir()),
                         ["bundle"])
        self.assertEqual(read_bundle(target, "outcome.json"),
                         {"ended": "merged", "merged_sha": built["passed"]})
        self.assertEqual(read_bundle(target, "brief.json"),
                         {"task_id": "DEMO-9101", "acceptance": ["AC1"]})
        self.assertIsNone(read_bundle(target, "challenge.json"))
        found = read_bundle(target, "findings.json")
        self.assertEqual([f["finding_id"] for f in found], [built["finding"]])
        self.assertEqual(found[0]["evidence"], "the total drops a cent")
        verdicts = read_bundle(target, "verdicts.json")
        self.assertEqual([(v["sha"], v["verdict"]) for v in verdicts],
                         [(built["rework"], "REWORK"), (built["passed"], "PASS")])
        self.assertEqual([v["document"]["verdict"] for v in verdicts], ["REWORK", "PASS"])
        events = store.read_log("DEMO-9101")
        timeline = read_bundle(target, "timeline.json")
        for name in ("park", "deny", "merge_observed"):
            matching = [e for e in timeline if e["event"] == name]
            self.assertEqual(len(matching), 1, name)
            self.assertEqual(events[matching[0]["n"]]["event"], name)
        self.assertEqual([v["n"] for v in verdicts],
                         [e["n"] for e in timeline if e["event"] == "verdict"])
        self.assertTrue({e["event"] for e in timeline} <= set(lessons.TIMELINE_EVENTS))
        gates = read_bundle(target, "gates.json")
        self.assertEqual([sorted(g) for g in gates], [["at", "check", "producer", "result", "sha"]])
        self.assertEqual((gates[0]["check"], gates[0]["result"], gates[0]["sha"],
                          gates[0]["producer"]),
                         ("focused-tests", "pass", built["passed"], PRODUCER))
        ledger = read_bundle(target, "threads.json")
        self.assertEqual(ledger["1001"][built["passed"]]["classification"], "NEEDS_CODE_CHANGE")
        remark = read_bundle(target / "remarks", "1001.json")
        self.assertEqual(remark["id"], "1001")
        self.assertEqual([n["body"] for n in remark["notes"]], [NOTE_BODY, "Still open."])
        in_effect = read_bundle(target, "memory-in-effect.json")
        self.assertEqual(in_effect["harness"], "example-maven")
        self.assertEqual([(e["id"], e["role"]) for e in in_effect["entries"]],
                         [("M-1", "ppl-code-worker")])

    def test_bundle_before_the_merge_is_recorded_is_refused(self):
        # rule: R-LESSONS-BUNDLE-AFTER-MERGE
        built = self.build_run("DEMO-9102")
        code, out, err = call("lessons", "bundle", "--task", "DEMO-9102")
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "ILLEGAL_TRANSITION")
        self.assertIn("ppl merge observed", payload["message"])
        self.assertEqual(out, "")
        self.assertFalse((built["directory"] / "lessons").exists())

    def test_bundle_outside_merge_watch_is_refused(self):
        # rule: R-LESSONS-BUNDLE-AFTER-MERGE
        # a merged sha recorded by hand does not stand in for the stage
        for number, stage in enumerate(("review", "closure", "parking")):
            task = "DEMO-%d" % (9110 + number)
            run.init(task, "u", "/tmp/repo")
            state = run.load(task)
            state["stage"] = stage
            state["mr"]["merged_sha"] = "a" * 40
            run.save(state)
            with self.assertRaises(PplError) as caught:
                lessons.bundle(task)
            self.assertEqual(caught.exception.code, "ILLEGAL_TRANSITION", stage)
            self.assertIn("merge_watch", caught.exception.message, stage)
            self.assertFalse((paths.run_dir(task) / "lessons").exists(), stage)

    def test_a_second_bundle_replaces_the_first(self):
        built = self.build_run("DEMO-9103")
        self.merge(built)
        first = lessons.bundle("DEMO-9103")
        (first / "stale.json").write_text("{}\n", encoding="utf-8")
        store.append_log("DEMO-9103", {"event": "deny", "role": "ppl-code-worker",
                                       "tool": "Bash", "code": "BOUNDARY_VIOLATION",
                                       "target": "rm -rf build"})
        second = lessons.bundle("DEMO-9103")
        self.assertEqual(second, first)
        self.assertFalse((second / "stale.json").exists())
        denials = [e for e in read_bundle(second, "timeline.json") if e["event"] == "deny"]
        self.assertEqual([e["tool"] for e in denials], ["Read", "Bash"])
        self.assertEqual(sorted(p.name for p in (built["directory"] / "lessons").iterdir()),
                         ["bundle"])

    def test_bundle_is_skipped_when_reflection_is_off(self):
        built = self.build_run("DEMO-9104")
        self.merge(built)
        paths.config_file().write_text("[lessons]\nreflect = false\n", encoding="utf-8")
        code, out, err = call("lessons", "bundle", "--task", "DEMO-9104")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), OFF_LINE)
        self.assertIsNone(lessons.bundle("DEMO-9104"))
        self.assertFalse((built["directory"] / "lessons").exists())

    def test_bundle_names_a_missing_reflector_agent_file(self):
        built = self.build_run("DEMO-9105")
        self.merge(built)
        self.reflector.unlink()
        code, _, err = call("lessons", "bundle", "--task", "DEMO-9105")
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "SETUP_INCOMPLETE")
        self.assertIn(str(self.reflector), payload["message"])
        self.assertIn("ppl harness apply example-maven", payload["message"])
        self.assertFalse((built["directory"] / "lessons").exists())

    def test_a_paused_run_still_gets_its_bundle(self):
        # no control.enforce: the bundle changes no run state
        built = self.build_run("DEMO-9107")
        self.merge(built)
        code, _, err = call("control", "set", "pause", "--task", "DEMO-9107")
        self.assertEqual(code, 0, err)
        code, out, err = call("lessons", "bundle", "--task", "DEMO-9107")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "bundle: %s" % lessons.bundle_dir(built["directory"]))

    def test_usage_events_lose_their_models_in_the_timeline(self):
        built = self.build_run("DEMO-9106")
        self.merge(built)
        target = lessons.bundle("DEMO-9106")
        usage = [e for e in read_bundle(target, "timeline.json") if e["event"] == "usage"]
        self.assertEqual(len(usage), 1)
        self.assertNotIn("models", usage[0])
        self.assertEqual((usage[0]["role"], usage[0]["agent_id"]), ("ppl-code-worker", "agent-1"))
        self.assertIn("models", store.read_log("DEMO-9106")[usage[0]["n"]])


class TestBuildBundle(LessonsTestCase):
    def test_build_bundle_writes_the_outcome_it_is_given_in_any_stage(self):
        built = self.build_run("DEMO-9121")
        state = run.load("DEMO-9121")
        state["stage"] = "implementation"
        run.save(state)
        outcome = {"ended": "destroyed", "last_stage": "implementation",
                   "blocker": "SPEC_GAP", "why": "the spec named no rounding rule"}
        target = lessons.build_bundle("DEMO-9121", outcome)
        self.assertEqual(target, lessons.bundle_dir(built["directory"]))
        self.assertEqual(read_bundle(target, "outcome.json"), outcome)

    def test_a_verdict_event_whose_document_is_gone_is_still_listed(self):
        built = self.build_run("DEMO-9122")
        (built["directory"] / "verdicts" / built["rework"] / "1.json").unlink()
        target = lessons.build_bundle("DEMO-9122", {"ended": "merged", "merged_sha": "a" * 40})
        verdicts = read_bundle(target, "verdicts.json")
        self.assertEqual([(v["verdict"], v["document"] is None) for v in verdicts],
                         [("REWORK", True), ("PASS", False)])

    def test_a_thread_id_that_is_not_a_plain_name_is_refused(self):
        built = self.build_run("DEMO-9123")
        store.write_json(built["directory"] / "review-threads.json",
                         threads_doc("../../escape"))
        with self.assertRaises(PplError) as caught:
            lessons.build_bundle("DEMO-9123", {"ended": "merged", "merged_sha": "a" * 40})
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("../../escape", caught.exception.message)
        self.assertFalse((built["directory"] / "lessons").exists())
        self.assertFalse((built["directory"] / "escape.json").exists())

    def test_a_linked_lessons_directory_is_refused(self):
        built = self.build_run("DEMO-9124")
        elsewhere = Path(tmphome.mkdtemp(prefix="pplelsewhere-"))
        (built["directory"] / "lessons").symlink_to(elsewhere)
        with self.assertRaises(PplError) as caught:
            lessons.build_bundle("DEMO-9124", {"ended": "merged", "merged_sha": "a" * 40})
        self.assertEqual(caught.exception.code, "EXTERNAL_BLOCKED")
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

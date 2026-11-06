"""`ppl lessons bundle`: a merged run's evidence, written as files for the reflector."""
import io
import json
import os
import subprocess
import sys
import tempfile
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
from ppl import (cli, evidence, findings, harness, lanes, lessons, memory, paths, review, run,
                 store, verdict)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))
import ppl_guard
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
    """A fresh data root per test with example-maven installed and one lesson in its memory.
    Each run built here gets the lane agents file ppl lane renders, ppl-reflector in it."""

    def setUp(self):
        self._env = {key: os.environ.get(key) for key in ("PPL_HOME",)}
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
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
        store.write_json(lanes.start_dir(task) / "agents.json",
                         {"ppl-reflector": {"description": "d", "prompt": "p"}})
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

    def test_bundle_names_a_lane_agents_file_without_the_reflector(self):
        built = self.build_run("DEMO-9105")
        self.merge(built)
        agents = lanes.start_dir("DEMO-9105") / "agents.json"
        for content in ('{"ppl-verifier": {}}', None):
            with self.subTest(content=content):
                if content is None:
                    agents.unlink()
                else:
                    agents.write_text(content, encoding="utf-8")
                code, _, err = call("lessons", "bundle", "--task", "DEMO-9105")
                self.assertEqual(code, 2)
                payload = json.loads(err)
                self.assertEqual(payload["error_code"], "SETUP_INCOMPLETE")
                self.assertIn(str(agents), payload["message"])
                self.assertIn("ppl lane resume DEMO-9105", payload["message"])
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

    def test_a_remarks_link_planted_in_the_build_directory_is_refused(self):
        built = self.build_run("DEMO-9125")
        elsewhere = Path(tmphome.mkdtemp(prefix="pplelsewhere-"))
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp_with_a_link(*args, **kwargs):
            folder = real_mkdtemp(*args, **kwargs)
            Path(folder, "remarks").symlink_to(elsewhere)
            return folder

        with mock.patch.object(lessons.tempfile, "mkdtemp", mkdtemp_with_a_link):
            with self.assertRaises(PplError) as caught:
                lessons.build_bundle("DEMO-9125", {"ended": "merged", "merged_sha": "a" * 40})
        self.assertEqual(caught.exception.code, "EXTERNAL_BLOCKED")
        self.assertIn("remarks", caught.exception.message)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(os.listdir(built["directory"] / "lessons"), [])

    def test_a_remarks_link_planted_while_the_files_are_written_is_refused(self):
        built = self.build_run("DEMO-9126")
        elsewhere = Path(tmphome.mkdtemp(prefix="pplelsewhere-"))
        real_write = store.write_json
        planted = []

        def write_after_planting(path, obj):
            # right before the first bundle file, remarks/ is swapped for a link
            if not planted:
                remarks = Path(path).parent / "remarks"
                remarks.rmdir()
                remarks.symlink_to(elsewhere)
                planted.append(remarks)
            return real_write(path, obj)

        with mock.patch.object(store, "write_json", write_after_planting):
            with self.assertRaises(PplError) as caught:
                lessons.build_bundle("DEMO-9126", {"ended": "merged", "merged_sha": "a" * 40})
        self.assertEqual(caught.exception.code, "EXTERNAL_BLOCKED")
        self.assertEqual(len(planted), 1)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertEqual(os.listdir(built["directory"] / "lessons"), [])

    def test_an_earlier_bundle_survives_a_failed_rebuild(self):
        built = self.build_run("DEMO-9127")
        self.merge(built)
        target = lessons.bundle("DEMO-9127")
        before = {p.relative_to(target).as_posix(): p.read_bytes()
                  for p in sorted(target.rglob("*")) if p.is_file()}
        store.append_log("DEMO-9127", {"event": "deny", "role": "ppl-code-worker",
                                       "tool": "Bash", "code": "BOUNDARY_VIOLATION",
                                       "target": "rm -rf build"})
        real_rename = os.rename
        aside = lessons.TEMP_PREFIX + "old-"

        def refuse_the_new_bundle(source, destination, *args, **kwargs):
            if Path(destination) == target and not Path(source).name.startswith(aside):
                raise OSError(28, "No space left on device")
            return real_rename(source, destination, *args, **kwargs)

        with mock.patch.object(lessons.os, "rename", refuse_the_new_bundle):
            with self.assertRaises(PplError) as caught:
                lessons.bundle("DEMO-9127")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("the earlier bundle is kept", caught.exception.message)
        after = {p.relative_to(target).as_posix(): p.read_bytes()
                 for p in sorted(target.rglob("*")) if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(os.listdir(built["directory"] / "lessons"), ["bundle"])

    def test_leftover_build_directories_are_removed_and_links_left_alone(self):
        built = self.build_run("DEMO-9128")
        folder = built["directory"] / "lessons"
        (folder / ".bundle-abc123" / "remarks").mkdir(parents=True)
        (folder / ".bundle-old-def456").mkdir()
        (folder / ".bundle-old-def456" / "outcome.json").write_text("{}\n", encoding="utf-8")
        elsewhere = Path(tmphome.mkdtemp(prefix="pplelsewhere-"))
        (elsewhere / "keep.json").write_text("{}\n", encoding="utf-8")
        (folder / ".bundle-linked").symlink_to(elsewhere)
        lessons.build_bundle("DEMO-9128", {"ended": "merged", "merged_sha": "a" * 40})
        self.assertEqual(sorted(os.listdir(folder)), [".bundle-linked", "bundle"])
        self.assertEqual(os.listdir(elsewhere), ["keep.json"])


class TestRecord(LessonsTestCase):
    """`ppl lessons record`: the reflector's JSON, checked against the bundle."""

    def bundled(self, task):
        built = self.build_run(task)
        self.merge(built)
        code, _, err = call("lessons", "bundle", "--task", task)
        self.assertEqual(code, 0, err)
        built["bundle"] = lessons.bundle_dir(built["directory"])
        timeline = read_bundle(built["bundle"], "timeline.json")
        built["park_n"] = [e["n"] for e in timeline if e["event"] == "park"][0]
        built["deny_n"] = [e["n"] for e in timeline if e["event"] == "deny"][0]
        built["usage_n"] = [e["n"] for e in timeline if e["event"] == "usage"][0]
        return built

    def doc(self, built, **over):
        task = built["task"]
        doc = {"task": task, "no_entry": False,
               "proposals": [{"role": "ppl-code-worker",
                              "cue": "when a change touches the cart",
                              "lesson": "round once, in the cart total",
                              "evidence": [{"kind": "finding", "ref": built["finding"]},
                                           {"kind": "remark", "ref": "1001"},
                                           {"kind": "verdict", "ref": built["passed"][:7]},
                                           {"kind": "gate",
                                            "ref": built["passed"][:7] + "/focused-tests"},
                                           {"kind": "park", "ref": str(built["park_n"])},
                                           {"kind": "deny", "ref": str(built["deny_n"])},
                                           {"kind": "timeline",
                                            "ref": str(built["usage_n"])}]}],
               "assessments": [{"memory_id": "M-1", "verdict": "helped",
                                "evidence": [{"kind": "verdict",
                                              "ref": built["rework"][:7]}]}]}
        doc.update(over)
        return doc

    def record(self, built, doc, tag="doc"):
        path = Path(tmphome.mkdtemp(prefix="pplreflect-")) / (tag + ".json")
        path.write_text(json.dumps(doc), encoding="utf-8")
        return call("lessons", "record", "--task", built["task"], "--file", str(path))

    def refused(self, code, built, doc):
        """The record is refused with `code`, nothing is stored and the marker stays."""
        exit_code, out, err = self.record(built, doc)
        self.assertEqual(exit_code, 2, out)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], code)
        self.assertEqual(out, "")
        self.assertFalse(lessons.proposals_path("example-maven", built["task"]).exists())
        return payload["message"]

    def test_record_stores_cleaned_proposals_as_pending(self):
        built = self.bundled("DEMO-9201")
        doc = self.doc(built)
        doc["proposals"][0]["cue"] = "When  a change\ntouches the cart "
        doc["proposals"][0]["lesson"] = "## round once,\x07 in the cart total <!-- x -->"
        code, out, err = self.record(built, doc)
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "recorded 1 proposals for DEMO-9201")
        target = lessons.proposals_path("example-maven", "DEMO-9201")
        self.assertEqual(target, paths.home() / "lessons" / "example-maven" / "proposals"
                         / "DEMO-9201.json")
        stored = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(sorted(stored), ["assessments", "harness", "no_entry", "proposals",
                                          "recorded_at", "task"])
        self.assertEqual((stored["task"], stored["harness"], stored["no_entry"]),
                         ("DEMO-9201", "example-maven", False))
        proposal = stored["proposals"][0]
        self.assertEqual(sorted(proposal), ["cue", "evidence", "id", "lesson", "role", "status"])
        self.assertEqual((proposal["id"], proposal["status"], proposal["role"]),
                         ("DEMO-9201.1", "pending", "ppl-code-worker"))
        self.assertEqual(proposal["cue"], memory.clean_cue(doc["proposals"][0]["cue"]))
        self.assertEqual(proposal["cue"], "a change touches the cart")
        self.assertEqual(proposal["lesson"], "round once, in the cart total")
        self.assertEqual(len(proposal["evidence"]), 7)
        self.assertEqual(proposal["evidence"][0], {"kind": "finding", "ref": built["finding"]})
        self.assertEqual(stored["assessments"][0]["memory_id"], "M-1")

    def test_an_unresolved_reference_refuses_the_whole_record(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9202")
        doc = self.doc(built)
        doc["proposals"][0]["evidence"].append({"kind": "finding", "ref": "F-99"})
        message = self.refused("SCHEMA_INVALID", built, doc)
        self.assertIn("evidence finding:F-99 does not resolve in the bundle", message)
        for kind, ref in (("remark", "9999"), ("verdict", "deadbee"), ("gate", "abc1234/x"),
                          ("park", "9999"), ("timeline", "9999")):
            doc = self.doc(built)
            doc["proposals"][0]["evidence"] = [{"kind": kind, "ref": ref}]
            self.assertIn("%s:%s does not resolve" % (kind, ref), self.refused("SCHEMA_INVALID", built, doc))
        doc = self.doc(built)
        doc["assessments"][0]["evidence"] = [{"kind": "finding", "ref": "F-99"}]
        self.assertIn("F-99", self.refused("SCHEMA_INVALID", built, doc))
        self.assertFalse((paths.home() / "lessons").exists())

    def test_each_evidence_kind_resolves_only_against_its_own_file(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9203")
        for kind, ref in (("park", built["deny_n"]), ("deny", built["park_n"]),
                          ("finding", "1001"), ("remark", built["finding"]),
                          ("verdict", built["finding"]), ("park", built["usage_n"])):
            doc = self.doc(built)
            doc["proposals"][0]["evidence"] = [{"kind": kind, "ref": str(ref)}]
            self.assertIn("%s:%s does not resolve" % (kind, ref), self.refused("SCHEMA_INVALID", built, doc))
        # the same event under the kind that names it, and under timeline, resolves
        self.assertTrue(lessons.resolve(built["bundle"], "park", str(built["park_n"])))
        self.assertTrue(lessons.resolve(built["bundle"], "timeline", str(built["deny_n"])))
        self.assertFalse(lessons.resolve(built["bundle"], "park", "%d" % (built["park_n"] + 900)))
        self.assertFalse(lessons.resolve(built["bundle"], "deny", "0%d" % built["deny_n"]))
        self.assertFalse(lessons.resolve(built["bundle"], "nonsense", "1"))

    def test_an_ambiguous_verdict_prefix_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9204")
        file = built["bundle"] / "verdicts.json"
        entries = json.loads(file.read_text(encoding="utf-8"))
        first = dict(entries[0], sha="abcdef1" + "0" * 33)
        second = dict(entries[0], sha="abcdef1" + "1" * 33)
        store.write_json(file, [first, second])
        for ref in ("abcdef1", "abcdef", "abc"):
            doc = self.doc(built)
            doc["proposals"][0]["evidence"] = [{"kind": "verdict", "ref": ref}]
            self.assertIn("does not resolve", self.refused("SCHEMA_INVALID", built, doc))
        doc = self.doc(built)
        doc["proposals"][0]["evidence"] = [{"kind": "verdict", "ref": "abcdef10"}]
        doc["assessments"] = []
        code, _, err = self.record(built, doc)
        self.assertEqual(code, 0, err)

    def test_an_unknown_role_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9205")
        doc = self.doc(built)
        doc["proposals"][0]["role"] = "ppl-nobody"
        self.assertIn("ppl-nobody", self.refused("SCHEMA_INVALID", built, doc))
        doc["proposals"][0]["role"] = "orchestrator"
        code, _, err = self.record(built, doc)
        self.assertEqual(code, 0, err)

    def test_a_stale_memory_id_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9206")
        doc = self.doc(built)
        doc["assessments"][0]["memory_id"] = "M-99"
        self.assertIn("M-99", self.refused("SCHEMA_INVALID", built, doc))
        doc = self.doc(built)
        doc["assessments"][0]["verdict"] = "helped a lot"
        self.assertIn("helped a lot", self.refused("SCHEMA_INVALID", built, doc))

    def test_no_entry_with_proposals_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9207")
        self.assertIn("no_entry", self.refused("SCHEMA_INVALID", built, self.doc(built, no_entry=True)))

    def test_no_entry_records_nothing_to_review(self):
        built = self.bundled("DEMO-9208")
        code, out, err = self.record(built, {"task": "DEMO-9208", "no_entry": True,
                                             "proposals": []})
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "recorded no entry for DEMO-9208")
        stored = json.loads(lessons.proposals_path("example-maven", "DEMO-9208")
                            .read_text(encoding="utf-8"))
        self.assertEqual((stored["no_entry"], stored["proposals"], stored["assessments"]),
                         (True, [], []))

    def test_more_than_five_proposals_are_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9209")
        one = self.doc(built)["proposals"][0]
        self.assertIn("5", self.refused("SCHEMA_INVALID", built, self.doc(built, proposals=[one] * 6)))
        code, _, err = self.record(built, self.doc(built, proposals=[one] * 5))
        self.assertEqual(code, 0, err)
        stored = json.loads(lessons.proposals_path("example-maven", "DEMO-9209")
                            .read_text(encoding="utf-8"))
        self.assertEqual([p["id"] for p in stored["proposals"]],
                         ["DEMO-9209.%d" % n for n in range(1, 6)])

    def test_the_documents_shape_is_checked(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9210")
        good = self.doc(built)
        for bad in (dict(good, task="DEMO-1"), dict(good, extra=1),
                    dict(good, no_entry="yes"), dict(good, proposals={}),
                    dict(good, assessments="x")):
            self.refused("SCHEMA_INVALID", built, bad)
        for key, value in (("cue", ""), ("cue", "<!-- x -->"), ("cue", "x" * 301), ("lesson", " "),
                           ("lesson", "y" * 301), ("evidence", []),
                           ("evidence", [{"kind": "mood", "ref": "1"}]),
                           ("evidence", [{"kind": "finding"}]), ("extra", 1)):
            doc = self.doc(built)
            doc["proposals"][0][key] = value
            self.refused("SCHEMA_INVALID", built, doc)
        self.refused("SCHEMA_INVALID", built, [1, 2])
        # invalid JSON is refused too
        path = Path(tmphome.mkdtemp(prefix="pplreflect-")) / "bad.json"
        path.write_text("{nope", encoding="utf-8")
        code, _, err = call("lessons", "record", "--task", "DEMO-9210", "--file", str(path))
        self.assertEqual((code, json.loads(err)["error_code"]), (2, "SCHEMA_INVALID"))

    def test_a_second_record_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9211")
        code, _, err = self.record(built, self.doc(built))
        self.assertEqual(code, 0, err)
        target = lessons.proposals_path("example-maven", "DEMO-9211")
        before = target.read_bytes()
        code, out, err = self.record(built, self.doc(built, no_entry=False, proposals=[]))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(target.read_bytes(), before)

    def test_a_second_record_clears_an_orphan_marker(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9228")
        code, _, err = self.record(built, self.doc(built))
        self.assertEqual(code, 0, err)
        target = lessons.proposals_path("example-maven", "DEMO-9228")
        before = target.read_bytes()
        marker = self.marker("DEMO-9228", built["bundle"])
        code, out, err = self.record(built, self.doc(built), tag="again")
        self.assertEqual(code, 2, out)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertFalse(marker.exists())
        self.assertEqual(lessons.pending(), [])
        self.assertEqual(target.read_bytes(), before)

    def test_a_destroy_after_the_record_leaves_no_marker_and_the_merged_bundle(self):
        built = self.bundled("DEMO-9229")
        code, _, err = self.record(built, self.doc(built))
        self.assertEqual(code, 0, err)
        code, out, err = call("destroy", "--scope", "local", "--task", "DEMO-9229",
                              "--reflect", "--why", "merged, cleaning up")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["reflection"]), ("archived", "recorded"))
        self.assertFalse(lessons.pending_path("DEMO-9229").exists())
        self.assertEqual(lessons.pending(), [])
        self.assertEqual(call("lessons", "pending")[1].strip(), "no pending reflections")
        archived = lessons.bundle_dir(result["archive"])
        self.assertEqual(read_bundle(archived, "outcome.json"),
                         {"ended": "merged", "merged_sha": built["passed"]})
        self.assertFalse(built["directory"].exists())

    def test_record_needs_a_task_and_a_file(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9212")
        for argv in (["lessons", "record", "--file", "x.json"],
                     ["lessons", "record", "--task", "DEMO-9212"],
                     ["lessons", "record", "--task", "DEMO-9212", "--file", "/no/such.json"]):
            code, _, err = call(*argv)
            self.assertEqual((code, json.loads(err)["error_code"]), (2, "SCHEMA_INVALID"),
                             argv)

    def test_record_leaves_memory_unchanged(self):
        # rule: R-LESSONS-OWNER-ACCEPTS
        built = self.bundled("DEMO-9213")
        memory_file = paths.harness_dir("example-maven") / "memory.toml"
        before = memory_file.read_bytes()
        code, _, err = self.record(built, self.doc(built))
        self.assertEqual(code, 0, err)
        self.assertEqual(memory_file.read_bytes(), before)

    def test_a_bundle_file_that_is_a_link_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9214")
        elsewhere = Path(tmphome.mkdtemp(prefix="pplelsewhere-")) / "findings.json"
        elsewhere.write_text(json.dumps([{"finding_id": "F-99"}]), encoding="utf-8")
        (built["bundle"] / "findings.json").unlink()
        (built["bundle"] / "findings.json").symlink_to(elsewhere)
        doc = self.doc(built)
        doc["proposals"][0]["evidence"] = [{"kind": "finding", "ref": "F-99"}]
        self.assertIn("link", self.refused("SCHEMA_INVALID", built, doc))

    def test_a_missing_bundle_says_to_build_it(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.build_run("DEMO-9215")
        code, out, err = self.record(built, {"task": "DEMO-9215", "no_entry": True})
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        self.assertIn("ppl lessons bundle", payload["message"])

    def test_a_lane_records_only_under_its_own_harness(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # R59: record runs outside the sandbox, and the lane's own bundle lies in its run, which
        # its sandbox writes, so a lane names the harness from its registry entry, not the bundle
        built = self.bundled("DEMO-9230")
        fixtures.bind_lane("DEMO-9230", "/tmp/repo-9230", harness="example-maven")
        store.write_json(built["bundle"] / "memory-in-effect.json",
                         {"harness": "other-harness", "entries": []})
        doc = self.doc(built, assessments=[])
        with mock.patch.dict(os.environ, {"PPL_TASK": "DEMO-9230"}):
            message = self.refused("SCHEMA_INVALID", built, doc)
        self.assertIn("other-harness", message)
        self.assertIn("example-maven", message)
        self.assertFalse((lessons.lessons_home() / "other-harness").exists())
        # from a terminal outside every lane the owner records as before, under the bundle's
        lanes.release("DEMO-9230")
        code, out, err = self.record(built, doc)
        self.assertEqual(code, 0, err)
        self.assertTrue(lessons.proposals_path("other-harness", "DEMO-9230").is_file())

    def test_a_harness_name_that_is_not_a_plain_name_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9216")
        file = built["bundle"] / "memory-in-effect.json"
        store.write_json(file, {"harness": "../../escape", "entries": []})
        doc = self.doc(built, assessments=[])
        self.assertIn("harness", self.refused("SCHEMA_INVALID", built, doc))
        self.assertFalse((paths.home() / "escape").exists())
        self.assertFalse((paths.home().parent / "escape").exists())

    def marker(self, task, bundle):
        target = lessons.pending_path(task)
        self.assertEqual(target, paths.home() / "lessons" / "pending" / (task + ".json"))
        store.write_json(target, {"task": task, "bundle": str(bundle)})
        return target

    def test_a_pending_marker_names_the_bundle_of_a_destroyed_run(self):
        built = self.bundled("DEMO-9217")
        archive = paths.runs_dir() / "_archive" / "DEMO-9217-1"
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.rename(built["directory"], archive)
        moved = dict(built, directory=archive, bundle=lessons.bundle_dir(archive))
        marker = self.marker("DEMO-9217", moved["bundle"])
        code, out, err = self.record(moved, self.doc(moved))
        self.assertEqual(code, 0, err)
        self.assertFalse(marker.exists())
        self.assertTrue(lessons.proposals_path("example-maven", "DEMO-9217").is_file())

    def test_a_refused_record_leaves_the_pending_marker(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9218")
        marker = self.marker("DEMO-9218", built["bundle"])
        doc = self.doc(built)
        doc["proposals"][0]["evidence"] = [{"kind": "finding", "ref": "F-99"}]
        self.refused("SCHEMA_INVALID", built, doc)
        self.assertTrue(marker.is_file())
        code, _, err = self.record(built, {"task": "DEMO-9218", "no_entry": True})
        self.assertEqual(code, 0, err)
        self.assertFalse(marker.exists())

    def test_a_marker_naming_a_path_outside_the_runs_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9219")
        outside = Path(tmphome.mkdtemp(prefix="pplelsewhere-")) / "bundle"
        outside.mkdir()
        for name in lessons.BUNDLE_FILES:
            (outside / name).write_text("null\n", encoding="utf-8")
        marker = self.marker("DEMO-9219", outside)
        self.assertIn("pending", self.refused("SCHEMA_INVALID", built, self.doc(built)))
        linked = paths.runs_dir() / "_archive" / "linked-bundle"
        linked.parent.mkdir(parents=True, exist_ok=True)
        linked.symlink_to(built["bundle"])
        store.write_json(marker, {"task": "DEMO-9219", "bundle": str(linked)})
        self.assertIn("pending", self.refused("SCHEMA_INVALID", built, self.doc(built)))
        store.write_json(marker, {"task": "DEMO-9219"})
        self.assertIn("pending", self.refused("SCHEMA_INVALID", built, self.doc(built)))
        self.assertTrue(marker.is_file())

    def test_a_reference_that_is_not_ascii_or_not_a_string_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9220")
        self.assertFalse(lessons.resolve(built["bundle"], "park", "\u00b2"))
        self.assertFalse(lessons.resolve(built["bundle"], "timeline", "\u0663"))
        for kind, ref in (("park", "\u00b2"), ("timeline", "\u0663"),
                          ("verdict", built["passed"][:6] + "\uff11"),
                          ("gate", "\uff11" * 7 + "/focused-tests"),
                          ("finding", ["F-1"]), ("remark", {"a": 1}), ("park", True)):
            doc = self.doc(built)
            doc["proposals"][0]["evidence"] = [{"kind": kind, "ref": ref}]
            self.refused("SCHEMA_INVALID", built, doc)
        doc = self.doc(built)
        doc["proposals"][0]["evidence"] = [{"kind": ["finding"], "ref": "1"}]
        self.refused("SCHEMA_INVALID", built, doc)

    def test_a_memory_id_that_is_not_a_string_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9221")
        for bad in (["M-1"], {"a": 1}, 1, None):
            doc = self.doc(built)
            doc["assessments"][0]["memory_id"] = bad
            self.refused("SCHEMA_INVALID", built, doc)
        file = built["bundle"] / "memory-in-effect.json"
        for planted in ([{"id": ["M-1"]}], [{"id": 1}], ["M-1"], {"M-1": 1}):
            store.write_json(file, {"harness": "example-maven", "entries": planted})
            self.refused("SCHEMA_INVALID", built, self.doc(built))

    def test_a_bundle_file_that_is_not_utf8_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9222")
        (built["bundle"] / "findings.json").write_bytes(b"\xff\xfe[]")
        self.refused("SCHEMA_INVALID", built, self.doc(built))
        path = Path(tmphome.mkdtemp(prefix="pplreflect-")) / "bad.json"
        path.write_bytes(b"\xff\xfe{}")
        code, _, err = call("lessons", "record", "--task", "DEMO-9222", "--file", str(path))
        self.assertEqual((code, json.loads(err)["error_code"]), (2, "SCHEMA_INVALID"))

    def test_a_gate_receipt_rerun_for_the_same_sha_and_check_still_resolves(self):
        built = self.bundled("DEMO-9223")
        file = built["bundle"] / "gates.json"
        gates = json.loads(file.read_text(encoding="utf-8"))
        store.write_json(file, gates + [dict(gates[0], at="later")])
        self.assertTrue(lessons.resolve(built["bundle"], "gate",
                                        built["passed"][:7] + "/focused-tests"))
        # a second sha with the same prefix and check is ambiguous
        store.write_json(file, gates + [dict(gates[0], sha=built["passed"][:7] + "f" * 33)])
        doc = self.doc(built)
        doc["proposals"][0]["evidence"] = [{"kind": "gate",
                                            "ref": built["passed"][:7] + "/focused-tests"}]
        self.refused("SCHEMA_INVALID", built, doc)

    def test_a_marker_whose_bundle_is_not_lessons_bundle_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9224")
        other = paths.runs_dir() / "_archive" / "DEMO-9224-1" / "elsewhere"
        other.mkdir(parents=True)
        marker = self.marker("DEMO-9224", other)
        self.assertIn("lessons/bundle", self.refused("SCHEMA_INVALID", built, self.doc(built)))
        self.assertTrue(marker.is_file())
        store.write_json(marker, {"task": "DEMO-9224", "bundle": str(built["directory"])})
        self.assertIn("lessons/bundle", self.refused("SCHEMA_INVALID", built, self.doc(built)))

    def test_an_empty_answer_without_no_entry_is_refused(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9225")
        marker = self.marker("DEMO-9225", built["bundle"])
        for empty in ({"task": "DEMO-9225"}, {"task": "DEMO-9225", "no_entry": False,
                                              "proposals": [], "assessments": []}):
            self.assertIn("no_entry", self.refused("SCHEMA_INVALID", built, empty))
        self.assertTrue(marker.is_file())

    def test_an_assessment_of_a_lesson_in_effect_leaves_memory_unchanged(self):
        built = self.bundled("DEMO-9226")
        path = paths.harness_dir("example-maven") / "memory.toml"
        before = path.read_bytes()
        code, _, err = self.record(built, self.doc(built))
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), before)

    def test_a_huge_n_reference_is_refused_without_crashing(self):
        # rule: R-LESSONS-EVIDENCE-RESOLVES
        built = self.bundled("DEMO-9227")
        for kind in ("park", "deny", "timeline"):
            self.assertFalse(lessons.resolve(built["bundle"], kind, "9" * 4400))
            doc = self.doc(built)
            doc["proposals"][0]["evidence"] = [{"kind": kind, "ref": "9" * 4400}]
            self.refused("SCHEMA_INVALID", built, doc)


class TestOwnerReview(LessonsTestCase):
    """`ppl lessons list|accept|reject|retire|edit`: the owner's review, the only writer of
    memory.toml."""

    KEY = "harness:example-maven:memory.toml"
    APPLY = ("commit memory.toml in harness example-maven's repository, and push it when it "
             "has an origin: the next lane ppl lane starts renders it")

    def setUp(self):
        super().setUp()
        paths.config_file().write_text('[harness]\nactive = "example-maven"\n', encoding="utf-8")

    def propose(self, task="DEMO-9301", items=None):
        items = items if items is not None else [
            {"id": task + ".1", "role": "ppl-code-worker", "cue": "a change touches the invoice",
             "lesson": "round once, in the invoice total",
             "evidence": [{"kind": "finding", "ref": "F-9"}, {"kind": "remark", "ref": "1001"}],
             "status": "pending"}]
        store.write_json(lessons.proposals_path("example-maven", task),
                         {"task": task, "harness": "example-maven",
                          "recorded_at": "2026-10-28T00:00:00Z", "no_entry": False,
                          "proposals": items, "assessments": []})

    def memory_path(self):
        return paths.harness_dir("example-maven") / "memory.toml"

    def memory_bytes(self):
        return self.memory_path().read_bytes()

    def entries(self):
        return memory.parse(self.memory_bytes(), "memory.toml", lessons.routing.LESSON_ROLES)

    def stored(self, task="DEMO-9301"):
        return json.loads(lessons.proposals_path("example-maven", task)
                          .read_text(encoding="utf-8"))["proposals"]

    def fill_role(self, role, count):
        rows = [{"id": "M-%d" % n, "role": role, "cue": "cue number %d" % n,
                 "lesson": "lesson %d" % n, "sources": ["DEMO-1:finding:F-1"],
                 "accepted_at": "2026-10-28T00:00:00Z", "seen": 1, "status": "active"}
                for n in range(1, count + 1)]
        self.memory_path().write_text(memory.dumps(rows), encoding="utf-8")

    def refused(self, code, *args):
        before = self.memory_bytes()
        exit_code, out, err = call(*args)
        self.assertEqual(exit_code, 2, out)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], code)
        self.assertEqual(self.memory_bytes(), before)
        return payload["message"]

    def test_accept_appends_a_new_entry_with_its_sources(self):
        self.propose()
        code, out, err = call("lessons", "accept", "DEMO-9301.1")
        self.assertEqual(code, 0, err)
        self.assertIn("M-2", out)
        self.assertIn(self.APPLY, out)
        new = self.entries()[1]
        self.assertEqual((new["id"], new["role"], new["seen"], new["status"]),
                         ("M-2", "ppl-code-worker", 1, "active"))
        self.assertEqual(new["cue"], "a change touches the invoice")
        self.assertEqual(new["sources"], ["DEMO-9301:finding:F-9", "DEMO-9301:remark:1001"])
        self.assertRegex(new["accepted_at"], r"^\d{4}-\d\d-\d\dT")
        self.assertEqual(self.stored()[0]["status"], "accepted")
        self.assertEqual(self.stored()[0]["memory_id"], "M-2")

    def test_accept_with_the_same_cue_merges_into_the_entry(self):
        self.propose(items=[{"id": "DEMO-9301.1", "role": "ppl-code-worker",
                             "cue": "A change touches the cart.", "lesson": "other words",
                             "evidence": [{"kind": "finding", "ref": "F-9"}],
                             "status": "pending"}])
        code, out, err = call("lessons", "accept", "DEMO-9301.1")
        self.assertEqual(code, 0, err)
        entries = self.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["seen"], 2)
        self.assertEqual(entries[0]["sources"], ["DEMO-1:finding:F-1", "DEMO-9301:finding:F-9"])
        self.assertEqual(entries[0]["lesson"], "run the cart tests before handing back")
        self.assertEqual(self.stored()[0]["memory_id"], "M-1")

    def test_accept_takes_an_owner_edit_of_cue_and_lesson(self):
        self.propose()
        code, out, err = call("lessons", "accept", "DEMO-9301.1", "--cue",
                              "When  the invoice\n## changes", "--lesson",
                              "keep <!-- x --> one rounding")
        self.assertEqual(code, 0, err)
        new = self.entries()[1]
        self.assertEqual(new["cue"], "the invoice changes")
        self.assertEqual(new["lesson"], "keep one rounding")

    def test_accept_refuses_an_owner_edit_that_is_empty_or_too_long(self):
        self.propose()
        for flag, value in (("--cue", "  "), ("--lesson", "x" * 301), ("--cue", "y" * 301)):
            message = self.refused("SCHEMA_INVALID", "lessons", "accept", "DEMO-9301.1", flag,
                                   value)
            self.assertIn(flag[2:], message)
        self.assertEqual(self.stored()[0]["status"], "pending")

    def test_accept_refuses_a_role_at_its_cap(self):
        self.fill_role("ppl-code-worker", memory.MAX_ACTIVE)
        self.propose()
        message = self.refused("SCHEMA_INVALID", "lessons", "accept", "DEMO-9301.1")
        self.assertIn("ppl lessons retire", message)
        self.assertEqual(self.stored()[0]["status"], "pending")

    def test_a_merge_at_the_cap_is_accepted(self):
        self.fill_role("ppl-code-worker", memory.MAX_ACTIVE)
        self.propose(items=[{"id": "DEMO-9301.1", "role": "ppl-code-worker",
                             "cue": "cue number 3", "lesson": "x",
                             "evidence": [{"kind": "finding", "ref": "F-9"}],
                             "status": "pending"}])
        code, _, err = call("lessons", "accept", "DEMO-9301.1")
        self.assertEqual(code, 0, err)
        entries = self.entries()
        self.assertEqual(len(entries), memory.MAX_ACTIVE)
        self.assertEqual(entries[2]["seen"], 2)

    def test_reject_needs_a_reason_and_keeps_memory_unchanged(self):
        # rule: R-LESSONS-OWNER-ACCEPTS
        self.propose()
        before = self.memory_bytes()
        for args in (("lessons", "reject", "DEMO-9301.1"),
                     ("lessons", "reject", "DEMO-9301.1", "--reason", "  ")):
            self.refused("SCHEMA_INVALID", *args)
        self.assertEqual(self.stored()[0]["status"], "pending")
        code, out, err = call("lessons", "reject", "DEMO-9301.1", "--reason", "too vague")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()[0]["status"], "rejected")
        self.assertEqual(self.stored()[0]["reason"], "too vague")
        self.assertEqual(self.memory_bytes(), before)

    def test_a_decided_proposal_cannot_be_decided_again(self):
        # rule: R-LESSONS-OWNER-ACCEPTS
        self.propose(items=[
            {"id": "DEMO-9301.1", "role": "ppl-code-worker", "cue": "one", "lesson": "a",
             "evidence": [{"kind": "finding", "ref": "F-9"}], "status": "pending"},
            {"id": "DEMO-9301.2", "role": "ppl-code-worker", "cue": "two", "lesson": "b",
             "evidence": [{"kind": "finding", "ref": "F-9"}], "status": "pending"}])
        self.assertEqual(call("lessons", "accept", "DEMO-9301.1")[0], 0)
        self.assertEqual(call("lessons", "reject", "DEMO-9301.2", "--reason", "no")[0], 0)
        for args in (("accept", "DEMO-9301.1"), ("reject", "DEMO-9301.1", "--reason", "x"),
                     ("accept", "DEMO-9301.2"), ("reject", "DEMO-9301.2", "--reason", "x")):
            self.refused("ILLEGAL_TRANSITION", "lessons", *args)
        self.assertEqual(len(self.entries()), 2)

    def test_an_unknown_proposal_is_refused(self):
        self.propose()
        for target in ("DEMO-9301.7", "DEMO-9999.1", "nonsense", "../x.1"):
            self.refused("SCHEMA_INVALID", "lessons", "accept", target)

    def test_accept_refuses_when_memory_changed_since_it_was_read(self):
        # rule: R-LESSONS-OWNER-ACCEPTS
        from ppl.studio import files
        self.propose()
        before = self.memory_bytes()
        real = files.read(self.KEY)
        stale = dict(real, hash="0" * 64)
        with mock.patch.object(files, "read", return_value=stale):
            exit_code, out, err = call("lessons", "accept", "DEMO-9301.1")
        self.assertEqual(exit_code, 2, out)
        self.assertEqual(json.loads(err)["error_code"], "CHANGED_ON_DISK")
        self.assertEqual(self.memory_bytes(), before)
        self.assertEqual(self.stored()[0]["status"], "pending")

    def test_accept_keeps_the_old_file_in_studio_history(self):
        from ppl.studio import files
        self.propose()
        before = self.memory_bytes()
        self.assertEqual(call("lessons", "accept", "DEMO-9301.1")[0], 0)
        versions = files.versions(self.KEY)
        self.assertEqual(len(versions), 1)
        self.assertEqual(files.version_text(self.KEY, versions[0]["version"]).encode(), before)

    def test_accept_refuses_a_harness_that_is_not_installed(self):
        # rule: R-LESSONS-OWNER-ACCEPTS
        self.propose()
        for name in ("gone", "../x", "a/b"):
            message = self.refused("SCHEMA_INVALID", "lessons", "accept", "DEMO-9301.1",
                                   "--harness", name)
            self.assertIn(name, message)

    def test_a_failed_proposal_write_keeps_the_lesson_and_a_second_accept_repairs_it(self):
        self.propose()
        real = store.write_json

        def failing(path, obj):
            if "proposals" in str(path):
                raise OSError("disk full")
            return real(path, obj)

        with mock.patch.object(store, "write_json", failing):
            code, out, err = call("lessons", "accept", "DEMO-9301.1")
        self.assertEqual(code, 2, out)
        payload = json.loads(err)
        self.assertIn("M-2", payload["message"])
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(self.stored()[0]["status"], "pending")
        code, out, err = call("lessons", "accept", "DEMO-9301.1")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.entries()), 2)
        self.assertEqual(self.entries()[1]["seen"], 1)
        self.assertEqual(self.stored()[0]["memory_id"], "M-2")

    def test_retire_hides_the_entry_from_the_render(self):
        entries = self.entries()
        self.assertIn("run the cart tests", memory.section(entries, "ppl-code-worker"))
        code, out, err = call("lessons", "retire", "M-1")
        self.assertEqual(code, 0, err)
        self.assertIn(self.APPLY, out)
        entries = self.entries()
        self.assertEqual(entries[0]["status"], "retired")
        self.assertEqual(memory.section(entries, "ppl-code-worker"), "")
        self.refused("SCHEMA_INVALID", "lessons", "retire", "M-1")
        self.assertIn("M-9", self.refused("SCHEMA_INVALID", "lessons", "retire", "M-9"))

    def test_edit_cleans_and_checks_the_new_text(self):
        code, out, err = call("lessons", "edit", "M-1", "--cue", "When the cart\n# moves",
                              "--lesson", "test it <!-- c -->")
        self.assertEqual(code, 0, err)
        self.assertIn(self.APPLY, out)
        entry = self.entries()[0]
        self.assertEqual((entry["cue"], entry["lesson"]), ("the cart moves", "test it"))
        self.refused("SCHEMA_INVALID", "lessons", "edit", "M-1")
        self.refused("SCHEMA_INVALID", "lessons", "edit", "M-1", "--lesson", "x" * 301)
        self.refused("SCHEMA_INVALID", "lessons", "edit", "M-1", "--cue", "  ")
        self.assertIn("M-9", self.refused("SCHEMA_INVALID", "lessons", "edit", "M-9",
                                          "--lesson", "x"))
        self.assertEqual(call("lessons", "retire", "M-1")[0], 0)
        self.assertIn("M-1", self.refused("SCHEMA_INVALID", "lessons", "edit", "M-1",
                                          "--lesson", "x"))

    def test_only_accept_and_edit_write_memory(self):
        self.propose()
        before = self.memory_bytes()
        self.assertEqual(call("lessons", "list")[0], 0)
        self.assertEqual(call("lessons", "list", "--json")[0], 0)
        self.assertEqual(call("lessons", "reject", "DEMO-9301.1", "--reason", "no")[0], 0)
        self.assertEqual(self.memory_bytes(), before)

    def test_list_shows_pending_proposals_and_active_entries(self):
        self.propose()
        code, out, err = call("lessons", "list")
        self.assertEqual(code, 0, err)
        for text in ("DEMO-9301.1", "ppl-code-worker", "round once, in the invoice total", "M-1"):
            self.assertIn(text, out)
        code, out, err = call("lessons", "list", "--json")
        data = json.loads(out)
        self.assertEqual(data["harness"], "example-maven")
        self.assertEqual([(p["id"], p["task"]) for p in data["proposals"]],
                         [("DEMO-9301.1", "DEMO-9301")])
        self.assertEqual([e["id"] for e in data["memory"]], ["M-1"])
        self.assertEqual(call("lessons", "reject", "DEMO-9301.1", "--reason", "no")[0], 0)
        self.assertEqual(json.loads(call("lessons", "list", "--json")[1])["proposals"], [])

    def test_list_skips_a_corrupt_proposals_file_and_names_it(self):
        self.propose()
        folder = lessons.proposals_path("example-maven", "DEMO-9302").parent
        (folder / "DEMO-9302.json").write_text("{not json", encoding="utf-8")
        (folder / "DEMO-9303.json").write_text("[1]", encoding="utf-8")
        data = lessons.listing("example-maven")
        self.assertEqual([p["id"] for p in data["proposals"]], ["DEMO-9301.1"])
        self.assertEqual([item["path"] for item in data["skipped"]],
                         ["DEMO-9302.json", "DEMO-9303.json"])
        self.assertTrue(all(item["reason"] for item in data["skipped"]))
        for argv in (("lessons", "list"), ("lessons", "list", "--json")):
            code, out, err = call(*argv)
            self.assertEqual(code, 0, err)
            self.assertIn("DEMO-9301.1", out)
            warnings = err.strip().splitlines()
            self.assertEqual(len(warnings), 2, err)
            self.assertTrue(warnings[0].startswith("warning: "), err)
            self.assertIn("DEMO-9302.json", warnings[0])
            self.assertIn("DEMO-9303.json", warnings[1])
        self.assertEqual(json.loads(call("lessons", "list", "--json")[1])["skipped"],
                         data["skipped"])


class TestAbandonedRun(LessonsTestCase):
    """`ppl destroy --scope local --reflect --why`: a run that ends without a merge."""

    def destroy(self, task, *extra):
        return call("destroy", "--scope", "local", "--task", task, *extra)

    def test_destroy_with_reflect_archives_the_bundle_and_leaves_a_marker(self):
        built = self.build_run("DEMO-9301")
        code, out, err = self.destroy("DEMO-9301", "--reflect", "--why", "  spec was wrong  ")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["reflection"], "pending")
        marker = json.loads(lessons.pending_path("DEMO-9301").read_text(encoding="utf-8"))
        self.assertEqual(marker, {"task": "DEMO-9301",
                                  "bundle": result["archive"] + "/lessons/bundle"})
        self.assertTrue(Path(marker["bundle"]).is_dir())
        self.assertEqual(read_bundle(marker["bundle"], "outcome.json"),
                         {"ended": "destroyed", "last_stage": "merge_watch",
                          "blocker": None, "why": "spec was wrong"})
        self.assertFalse(built["directory"].exists())

    def test_a_resumed_run_has_a_null_blocker(self):
        # build_run parks the run once and resumes it, and parking.json stays on disk
        built = self.build_run("DEMO-9313")
        parking = json.loads((built["directory"] / "parking.json").read_text(encoding="utf-8"))
        self.assertEqual(parking["blocker"], "EXTERNAL_BLOCKED")
        self.assertEqual(run.load("DEMO-9313")["stage"], "merge_watch")
        result = lessons.destroy_with_reflection("DEMO-9313", "gave up")
        outcome = read_bundle(lessons.bundle_dir(result["archive"]), "outcome.json")
        self.assertEqual((outcome["last_stage"], outcome["blocker"]), ("merge_watch", None))

    def test_an_escalated_run_reports_the_stage_it_escalated_from(self):
        built = self.build_run("DEMO-9314")
        state = run.load("DEMO-9314")
        state["stage"] = "briefing"
        state["status"] = "in_progress"
        run.save(state)
        run.stage_enter("DEMO-9314", "escalation")
        self.assertEqual(run.load("DEMO-9314")["escalation_resume_stage"], "briefing")
        result = lessons.destroy_with_reflection("DEMO-9314", "the owner never answered")
        outcome = read_bundle(lessons.bundle_dir(result["archive"]), "outcome.json")
        self.assertEqual(outcome["last_stage"], "briefing")
        self.assertEqual(outcome["blocker"], "EXTERNAL_BLOCKED")
        self.assertFalse(built["directory"].exists())

    def test_a_merged_run_destroyed_before_its_reflection_keeps_the_merged_outcome(self):
        built = self.build_run("DEMO-9315")
        self.merge(built)
        code, out, err = self.destroy("DEMO-9315", "--reflect", "--why", " merged, cleaning up ")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["reflection"], result["why"]), ("pending", "merged, cleaning up"))
        marker = json.loads(lessons.pending_path("DEMO-9315").read_text(encoding="utf-8"))
        self.assertEqual(marker, {"task": "DEMO-9315",
                                  "bundle": result["archive"] + "/lessons/bundle"})
        self.assertEqual(read_bundle(marker["bundle"], "outcome.json"),
                         {"ended": "merged", "merged_sha": built["passed"]})

    def test_a_parked_run_reports_its_resume_stage(self):
        self.build_run("DEMO-9302")
        run.park("DEMO-9302", "SPEC_GAP", "ask", [])
        result = lessons.destroy_with_reflection("DEMO-9302", "gave up")
        outcome = read_bundle(Path(result["archive"]) / "lessons" / "bundle", "outcome.json")
        self.assertEqual(outcome["last_stage"], "merge_watch")
        self.assertEqual(outcome["blocker"], "SPEC_GAP")

    def test_a_run_without_parking_has_a_null_blocker(self):
        built = self.build_run("DEMO-9303")
        (built["directory"] / "parking.json").unlink()
        result = lessons.destroy_with_reflection("DEMO-9303", "gave up")
        outcome = read_bundle(Path(result["archive"]) / "lessons" / "bundle", "outcome.json")
        self.assertIsNone(outcome["blocker"])

    def test_the_marker_is_a_bundle_find_bundle_accepts(self):
        self.build_run("DEMO-9304")
        result = lessons.destroy_with_reflection("DEMO-9304", "gave up")
        self.assertEqual(lessons.find_bundle("DEMO-9304"),
                         Path(result["archive"]) / "lessons" / "bundle")

    def test_reflect_needs_a_why(self):
        built = self.build_run("DEMO-9305")
        for extra in (("--reflect",), ("--reflect", "--why", "   ")):
            code, _, err = self.destroy("DEMO-9305", *extra)
            self.assertEqual(code, 2)
            self.assertIn("SCHEMA_INVALID", err)
        self.assertTrue(built["directory"].is_dir())
        self.assertFalse((built["directory"] / "lessons").exists())

    def test_why_without_reflect_is_refused(self):
        built = self.build_run("DEMO-9306")
        code, _, err = self.destroy("DEMO-9306", "--why", "because")
        self.assertEqual(code, 2)
        self.assertIn("SCHEMA_INVALID", err)
        self.assertTrue(built["directory"].is_dir())

    def test_reflect_is_refused_with_scope_full(self):
        built = self.build_run("DEMO-9307")
        code, _, err = call("destroy", "--scope", "full", "--task", "DEMO-9307", "--reflect",
                            "--why", "because")
        self.assertEqual(code, 2)
        self.assertIn("SCHEMA_INVALID", err)
        self.assertIn("reflect at the local destroy that archives the run", err)
        self.assertFalse((built["directory"] / "remote_cleanup.json").exists())

    def test_destroy_without_reflect_writes_no_bundle(self):
        built = self.build_run("DEMO-9308")
        code, out, err = self.destroy("DEMO-9308")
        self.assertEqual(code, 0, err)
        self.assertNotIn("reflection", json.loads(out))
        self.assertFalse(lessons.pending_path("DEMO-9308").exists())
        self.assertFalse((Path(json.loads(out)["archive"]) / "lessons").exists())
        self.assertFalse(built["directory"].exists())

    def test_reflection_off_destroys_without_a_bundle(self):
        self.build_run("DEMO-9309")
        paths.config_file().write_text("[lessons]\nreflect = false\n", encoding="utf-8")
        code, out, err = self.destroy("DEMO-9309", "--reflect", "--why", "gave up")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["reflection"], "off")
        self.assertFalse((Path(result["archive"]) / "lessons").exists())
        self.assertFalse(lessons.pending_path("DEMO-9309").exists())

    def test_a_bundle_failure_refuses_before_anything_is_archived(self):
        built = self.build_run("DEMO-9310")
        with mock.patch.object(lessons, "build_bundle",
                               side_effect=PplError("SCHEMA_INVALID", "no bundle")):
            with self.assertRaises(PplError):
                lessons.destroy_with_reflection("DEMO-9310", "gave up")
        self.assertTrue(built["directory"].is_dir())
        self.assertFalse(lessons.pending_path("DEMO-9310").exists())

    def test_a_marker_that_cannot_be_written_names_the_archived_bundle(self):
        self.build_run("DEMO-9311")
        real = store.write_json

        def write_json(path, doc, *rest, **more):
            if Path(path) == lessons.pending_path("DEMO-9311"):
                raise OSError("disk full")
            return real(path, doc, *rest, **more)

        with mock.patch.object(store, "write_json", side_effect=write_json):
            with self.assertRaises(PplError) as caught:
                lessons.destroy_with_reflection("DEMO-9311", "gave up")
        self.assertIn("archived", caught.exception.message)
        self.assertIn("/lessons/bundle", caught.exception.message)
        self.assertIn("disk full", caught.exception.message)

    def test_a_pending_reflection_records_against_the_archived_bundle(self):
        built = self.build_run("DEMO-9312")
        lessons.destroy_with_reflection("DEMO-9312", "gave up")
        doc = {"task": "DEMO-9312", "no_entry": False, "assessments": [],
               "proposals": [{"role": "ppl-code-worker", "cue": "when a change touches the cart",
                              "lesson": "round once, in the cart total",
                              "evidence": [{"kind": "finding", "ref": built["finding"]}]}]}
        lessons.record("DEMO-9312", doc)
        self.assertTrue(lessons.proposals_path("example-maven", "DEMO-9312").is_file())
        self.assertFalse(lessons.pending_path("DEMO-9312").exists())

    def test_pending_lists_the_markers_oldest_first(self):
        for task in ("DEMO-9322", "DEMO-9321"):
            self.build_run(task)
            lessons.destroy_with_reflection(task, "gave up")
        os.utime(str(lessons.pending_path("DEMO-9322")), (1000, 1000))
        os.utime(str(lessons.pending_path("DEMO-9321")), (2000, 2000))
        code, out, err = call("lessons", "pending")
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual([line.split()[0] for line in lines], ["DEMO-9322", "DEMO-9321"])
        self.assertEqual(lines[0].split()[1], lessons.find_bundle("DEMO-9322").as_posix())

    def test_pending_with_no_markers(self):
        code, out, _ = call("lessons", "pending")
        self.assertEqual((code, out.strip()), (0, "no pending reflections"))

    def test_pending_skips_a_malformed_marker_with_one_warning(self):
        self.build_run("DEMO-9330")
        lessons.destroy_with_reflection("DEMO-9330", "gave up")
        bad = lessons.pending_path("DEMO-9331")
        bad.write_text("{not json", encoding="utf-8")
        wrong = lessons.pending_path("DEMO-9332")
        wrong.write_text("[1]", encoding="utf-8")
        code, out, err = call("lessons", "pending")
        self.assertEqual(code, 0)
        self.assertEqual([line.split()[0] for line in out.splitlines()], ["DEMO-9330"])
        self.assertEqual(len(err.strip().splitlines()), 2)
        self.assertIn("DEMO-9331", err)

    def abandon(self, task, harness_name="example-maven", files_name=None):
        """A run in a lane of `harness_name` that the owner destroyed with reflection: its
        pending marker's bundle. `files_name` is the harness its own harness.json, and so its
        bundle's memory-in-effect.json, are rewritten to name, as its lane's sandbox can."""
        built = self.build_run(task)
        fixtures.bind_lane(task, "/nonexistent/repo-" + task, lane=9, harness=harness_name)
        if files_name is not None:
            snapshot = json.loads((built["directory"] / "harness.json").read_text(
                encoding="utf-8"))
            store.write_json(built["directory"] / "harness.json", dict(snapshot,
                                                                      name=files_name))
        return lessons.destroy_with_reflection(task, "gave up")["archive"] + "/lessons/bundle"

    def in_lane(self, lane, *args):
        """`ppl <args>` as the session of `lane` runs it."""
        with mock.patch.dict(os.environ, {"PPL_TASK": lane}):
            return call(*args)

    def test_pending_lists_only_this_harness(self):
        # D20: a lane reflects only on the runs of its own harness, and claims what it lists
        mine = self.abandon("DEMO-9340")
        self.abandon("DEMO-9341", harness_name="other-harness")
        fixtures.bind_lane("DEMO-9350", "/tmp/repo-9350", harness="example-maven")
        self.assertEqual([m["task"] for m in lessons.pending("example-maven")], ["DEMO-9340"])
        self.assertEqual([m["task"] for m in lessons.pending("other-harness")], ["DEMO-9341"])
        self.assertEqual(sorted(m["task"] for m in lessons.pending()), ["DEMO-9340", "DEMO-9341"])
        # R59: a lane claims no marker of a run of another harness, even named directly
        self.assertFalse(lessons.claim_pending("DEMO-9341", "DEMO-9350"))
        self.assertTrue(lessons.pending_path("DEMO-9341").is_file())
        code, out, err = self.in_lane("DEMO-9350", "lessons", "pending")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines(), ["DEMO-9340 %s" % mine])
        self.assertFalse(lessons.pending_path("DEMO-9340").exists())
        self.assertTrue(lessons.claimed_path("DEMO-9340", "DEMO-9350").is_file())
        # the other harness's marker stays for a lane of its own harness
        self.assertEqual([m["task"] for m in lessons.pending()], ["DEMO-9341"])

    def test_a_claimed_marker_is_not_listed_twice(self):
        # D20: the first lane's rename wins, and two lanes never reflect on the same run
        bundle = self.abandon("DEMO-9342")
        fixtures.bind_lane("DEMO-9351", "/tmp/repo-9351", lane=1)
        fixtures.bind_lane("DEMO-9352", "/tmp/repo-9352", lane=2)
        self.assertTrue(lessons.claim_pending("DEMO-9342", "DEMO-9351"))
        self.assertFalse(lessons.claim_pending("DEMO-9342", "DEMO-9352"))
        self.assertFalse(lessons.claim_pending("DEMO-9342", "DEMO-9351"))
        self.assertEqual(lessons.pending(), [])
        self.assertEqual(lessons.pending("example-maven"), [])
        code, out, err = self.in_lane("DEMO-9352", "lessons", "pending")
        self.assertEqual((code, out.strip()), (0, "no pending reflections"), err)
        # the claiming lane lists its claim again, so a resumed session finishes it
        code, out, err = self.in_lane("DEMO-9351", "lessons", "pending")
        self.assertEqual((code, out.splitlines()), (0, ["DEMO-9342 %s" % bundle]), err)
        self.assertEqual(lessons.find_bundle("DEMO-9342", "DEMO-9351"), Path(bundle))
        # outside a lane the owner still sees nothing pending to claim
        self.assertEqual(call("lessons", "pending")[1].strip(), "no pending reflections")

    def test_a_marker_carries_the_harness_its_lane_ran_not_the_runs_own_files(self):
        # R61: the run's harness.json and its bundle lie in its own lane's write scope, so the
        # marker carries the harness the registry named, and only that decides who reflects
        bundle = self.abandon("DEMO-9345", harness_name="example-maven",
                              files_name="other-harness")
        self.assertEqual(read_bundle(bundle, "memory-in-effect.json")["harness"], "other-harness")
        marker = json.loads(lessons.pending_path("DEMO-9345").read_text(encoding="utf-8"))
        self.assertEqual(marker, {"task": "DEMO-9345", "bundle": bundle,
                                  "harness": "example-maven"})
        fixtures.bind_lane("DEMO-9355", "/tmp/repo-9355", lane=1, harness="other-harness")
        fixtures.bind_lane("DEMO-9356", "/tmp/repo-9356", lane=2, harness="example-maven")
        self.assertEqual(lessons.pending("other-harness"), [])
        self.assertFalse(lessons.claim_pending("DEMO-9345", "DEMO-9355"))
        code, out, err = self.in_lane("DEMO-9355", "lessons", "pending")
        self.assertEqual((code, out.strip()), (0, "no pending reflections"), err)
        self.assertTrue(lessons.pending_path("DEMO-9345").is_file())
        # the stamped harness's lane claims it and records under that harness
        code, out, err = self.in_lane("DEMO-9356", "lessons", "pending")
        self.assertEqual((code, out.split()), (0, ["DEMO-9345", bundle]), err)
        answer = Path(tmphome.mkdtemp(prefix="pplreflect-")) / "answer.json"
        answer.write_text(json.dumps({"task": "DEMO-9345", "no_entry": True}), encoding="utf-8")
        code, out, err = self.in_lane("DEMO-9356", "lessons", "record", "--task", "DEMO-9345",
                                      "--file", str(answer))
        self.assertEqual(code, 0, err)
        self.assertTrue(lessons.proposals_path("example-maven", "DEMO-9345").is_file())
        self.assertFalse((lessons.lessons_home() / "other-harness").exists())

    def test_a_marker_stamped_after_the_lane_closed_takes_its_record(self):
        # R61: after ppl lane close the registry entry is gone, and lane.json still names the
        # harness ppl lane started the run with
        self.build_run("DEMO-9347")
        fixtures.bind_lane("DEMO-9347", "/nonexistent/repo-9347", harness="example-maven")
        lanes.release("DEMO-9347")
        self.assertEqual(lanes.record("DEMO-9347")["harness"], "example-maven")
        lessons.destroy_with_reflection("DEMO-9347", "gave up")
        marker = json.loads(lessons.pending_path("DEMO-9347").read_text(encoding="utf-8"))
        self.assertEqual(marker["harness"], "example-maven")
        # a record whose harness is not a name is no record, and stamps nothing
        self.build_run("DEMO-9372")
        fixtures.bind_lane("DEMO-9372", "/nonexistent/repo-9372", harness="example-maven")
        lanes.release("DEMO-9372")
        record = json.loads(lanes.record_path("DEMO-9372").read_text(encoding="utf-8"))
        store.write_json(lanes.record_path("DEMO-9372"), dict(record, harness=7))
        self.assertIsNone(lanes.record("DEMO-9372"))
        lessons.destroy_with_reflection("DEMO-9372", "gave up")
        marker = json.loads(lessons.pending_path("DEMO-9372").read_text(encoding="utf-8"))
        self.assertNotIn("harness", marker)

    def test_an_unstamped_marker_is_reflected_only_from_outside_every_lane(self):
        # R61: a marker from before lanes names no harness, so no lane claims it
        bundle = self.abandon("DEMO-9348")
        store.write_json(lessons.pending_path("DEMO-9348"), {"task": "DEMO-9348",
                                                             "bundle": bundle})
        fixtures.bind_lane("DEMO-9357", "/tmp/repo-9357", lane=1)
        self.assertEqual(lessons.pending("example-maven"), [])
        self.assertFalse(lessons.claim_pending("DEMO-9348", "DEMO-9357"))
        self.assertEqual(self.in_lane("DEMO-9357", "lessons", "pending")[1].strip(),
                         "no pending reflections")
        self.assertEqual([m["task"] for m in lessons.pending()], ["DEMO-9348"])

    def test_an_owner_terminal_in_a_lanes_directory_claims_nothing(self):
        # R63: the claim and the in-lane record key on PPL_TASK alone, never on the cwd
        bundle = self.abandon("DEMO-9349")
        fixtures.bind_lane("DEMO-9358", "/tmp/repo-9358", lane=1)
        start = lanes.start_dir("DEMO-9358")
        before = os.getcwd()
        os.chdir(str(start))
        try:
            with mock.patch.dict(os.environ, {"PPL_TASK": ""}):
                code, out, err = call("lessons", "pending")
        finally:
            os.chdir(before)
        self.assertEqual((code, out.split()), (0, ["DEMO-9349", bundle]), err)
        self.assertTrue(lessons.pending_path("DEMO-9349").is_file())
        self.assertEqual(lessons.claimed("DEMO-9358"), [])

    def close_lane(self, lane):
        """A lane whose own run is merged, so ppl lane close frees it."""
        built = self.build_run(lane)
        self.merge(built)
        fixtures.bind_lane(lane, "/nonexistent/repo-" + lane, lane=1)

    def test_closing_a_lane_hands_its_unrecorded_claims_back(self):
        # R60: a claim the lane did not record goes back to pending/<T>.json, so the next lane
        # of its harness lists it, and one whose task has a marker again stays and is reported
        self.close_lane("DEMO-9360")
        handed = self.abandon("DEMO-9361")
        self.abandon("DEMO-9362")
        for task in ("DEMO-9361", "DEMO-9362"):
            self.assertTrue(lessons.claim_pending(task, "DEMO-9360"))
        store.write_json(lessons.pending_path("DEMO-9362"), {"task": "DEMO-9362",
                                                             "bundle": "/elsewhere",
                                                             "harness": "example-maven"})
        code, out, err = self.in_lane("DEMO-9360", "lane", "close")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["claims_handed_back"], ["DEMO-9361"])
        self.assertEqual([kept["task"] for kept in result["claims_kept"]], ["DEMO-9362"])
        self.assertIn("DEMO-9362.json", result["claims_kept"][0]["reason"])
        self.assertIsNone(lanes.entry("DEMO-9360"))
        self.assertFalse(lessons.claimed_path("DEMO-9361", "DEMO-9360").exists())
        self.assertTrue(lessons.claimed_path("DEMO-9362", "DEMO-9360").is_file())
        fixtures.bind_lane("DEMO-9363", "/tmp/repo-9363", lane=2)
        code, out, err = self.in_lane("DEMO-9363", "lessons", "pending")
        self.assertEqual(code, 0, err)
        self.assertIn("DEMO-9361 %s" % handed, out.splitlines())

    def test_a_hand_back_that_fails_holds_the_claim_and_still_frees_the_lane(self):
        # N2: the slot is freed before the hand-back, so an error there is reported with the
        # claim it held, never a crash that leaves no lane to close again
        self.close_lane("DEMO-9374")
        self.abandon("DEMO-9375")
        self.abandon("DEMO-9376")
        for task in ("DEMO-9375", "DEMO-9376"):
            self.assertTrue(lessons.claim_pending(task, "DEMO-9374"))
        real = os.link

        def link(source, target, *rest, **more):
            if "DEMO-9375" in str(source):
                raise OSError(1, "Operation not permitted")
            return real(source, target, *rest, **more)

        with mock.patch.object(os, "link", side_effect=link):
            code, out, err = self.in_lane("DEMO-9374", "lane", "close")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertIsNone(lanes.entry("DEMO-9374"))
        self.assertEqual(result["claims_handed_back"], ["DEMO-9376"])
        self.assertEqual([kept["task"] for kept in result["claims_kept"]], ["DEMO-9375"])
        self.assertIn("Operation not permitted", result["claims_kept"][0]["reason"])
        self.assertTrue(lessons.claimed_path("DEMO-9375", "DEMO-9374").is_file())
        # a destroy goes the same way
        self.build_run("DEMO-9377")
        fixtures.bind_lane("DEMO-9377", "/nonexistent/repo-9377", lane=1)
        self.abandon("DEMO-9378")
        self.assertTrue(lessons.claim_pending("DEMO-9378", "DEMO-9377"))
        with mock.patch.object(os, "link", side_effect=OSError(31, "Too many links")):
            code, out, err = call("destroy", "--scope", "local", "--task", "DEMO-9377")
        self.assertEqual(code, 0, err)
        kept = json.loads(out)["claims_kept"]
        self.assertEqual([item["task"] for item in kept], ["DEMO-9378"])
        self.assertIn("Too many links", kept[0]["reason"])

    def test_a_local_destroy_hands_the_lanes_claims_back(self):
        # R60: a lane freed by ppl destroy --scope local hands its claims back too
        self.build_run("DEMO-9364")
        fixtures.bind_lane("DEMO-9364", "/nonexistent/repo-9364", lane=1)
        self.abandon("DEMO-9365")
        self.assertTrue(lessons.claim_pending("DEMO-9365", "DEMO-9364"))
        code, out, err = call("destroy", "--scope", "local", "--task", "DEMO-9364")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["claims_handed_back"], ["DEMO-9365"])
        self.assertTrue(lessons.pending_path("DEMO-9365").is_file())
        self.assertEqual(lessons.claimed("DEMO-9364"), [])

    def test_a_ppl_task_that_is_not_a_task_id_is_refused_by_record(self):
        # M5: a PPL_TASK that names no task is a routed refusal, never a traceback
        answer = Path(tmphome.mkdtemp(prefix="pplreflect-")) / "answer.json"
        answer.write_text(json.dumps({"task": "DEMO-9371", "no_entry": True}), encoding="utf-8")
        for value in ("../x", "a/b", "demo-1"):
            with self.subTest(ppl_task=value):
                code, _, err = self.in_lane(value, "lessons", "record", "--task", "DEMO-9371",
                                            "--file", str(answer))
                self.assertEqual((code, json.loads(err)["error_code"]), (2, "SCHEMA_INVALID"))

    def test_the_reflector_reads_an_archived_bundle_only_of_a_run_its_lane_claimed(self):
        # rule: R-LANE-REFLECTOR-READS
        # R63: ppl-reflector in a lane reads the archive only inside the bundle of a marker
        # that lane claimed, through the guard's role checks
        self.build_run("DEMO-9366")
        fixtures.bind_lane("DEMO-9366", "/nonexistent/repo-9366", lane=1)
        fixtures.bind_lane("DEMO-9367", "/nonexistent/repo-9367", lane=2)
        mine = self.abandon("DEMO-9368")
        theirs = self.abandon("DEMO-9369")
        unclaimed = self.abandon("DEMO-9370")
        self.assertTrue(lessons.claim_pending("DEMO-9368", "DEMO-9366"))
        self.assertTrue(lessons.claim_pending("DEMO-9369", "DEMO-9367"))
        # a claim that is no marker names no bundle, and breaks nothing
        lessons.claimed_path("DEMO-9373", "DEMO-9366").write_text("[1]", encoding="utf-8")
        start = str(lanes.start_dir("DEMO-9366"))

        def decide(tool, **tool_input):
            return ppl_guard.decide({"hook_event_name": "PreToolUse",
                                     "agent_type": "ppl-reflector", "tool_name": tool,
                                     "tool_input": tool_input, "cwd": start},
                                    ["--task", "DEMO-9366"])

        for tool, tool_input in (("Read", {"file_path": mine + "/outcome.json"}),
                                 ("Grep", {"pattern": "x", "path": mine}),
                                 ("Glob", {"pattern": "**/*.json", "path": mine}),
                                 # no path: the lane's start directory, a sibling of runs/
                                 ("Grep", {"pattern": "x"}),
                                 ("Grep", {"pattern": "x",
                                           "path": str(paths.run_dir("DEMO-9366"))})):
            with self.subTest(tool=tool, allowed=True, tool_input=tool_input):
                self.assertIsNone(decide(tool, **tool_input))
        for tool, tool_input in (
                ("Read", {"file_path": theirs + "/outcome.json"}),
                ("Read", {"file_path": unclaimed + "/outcome.json"}),
                ("Read", {"file_path": mine + "/../../../" + Path(theirs).parts[-3]
                          + "/lessons/bundle/outcome.json"}),
                ("Grep", {"pattern": "x", "path": str(Path(mine).parents[2])}),
                ("Glob", {"pattern": "../../../*/lessons/bundle/*.json", "path": mine}),
                ("Glob", {"pattern": str(Path(theirs).parents[2]) + "/*/lessons/**"}),
                # N1: a search rooted above the archive reads every bundle in it
                ("Grep", {"pattern": ".", "path": str(paths.runs_dir())}),
                ("Grep", {"pattern": ".", "path": str(paths.home())}),
                ("Grep", {"pattern": ".", "path": str(paths.runs_dir()),
                          "glob": "_archive/**"}),
                ("Grep", {"pattern": ".", "path": str(paths.runs_dir()).upper()}),
                ("Glob", {"pattern": "_archive/*/lessons/bundle/*",
                          "path": str(paths.runs_dir())}),
                ("Glob", {"pattern": str(paths.runs_dir()) + "/*/*/lessons/bundle/*"}),
                ("Glob", {"pattern": str(paths.home()) + "/**/outcome.json"}),
                # N3: a `..` segment from a root outside the archive, an escaped absolute
                # pattern and an extglob pattern are refused whatever the root
                ("Glob", {"pattern": "../runs/_archive/**",
                          "path": str(paths.home() / "lanes")}),
                ("Glob", {"pattern": "../../runs/_archive/**"}),
                ("Glob", {"pattern": str(paths.runs_dir()) + "/\\_archive/*/lessons/bundle/*"}),
                ("Glob", {"pattern": "*.@(json|md)", "path": start}),
                ("Glob", {"pattern": "!(x)/**", "path": start}),
                ("Glob", {"pattern": "+(_archive)/**", "path": str(paths.runs_dir())})):
            with self.subTest(tool=tool, tool_input=tool_input):
                result = decide(tool, **tool_input)
                self.assertIsNotNone(result, tool_input)
                self.assertIn("BOUNDARY_VIOLATION",
                              result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_lane_records_only_its_own_run_or_a_marker_it_claimed(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # R57: a lane's record names its own run or a marker it claimed with ppl lessons pending
        self.build_run("DEMO-9343")
        self.abandon("DEMO-9344")
        fixtures.bind_lane("DEMO-9353", "/tmp/repo-9353", lane=1)
        fixtures.bind_lane("DEMO-9354", "/tmp/repo-9354", lane=2)
        fixtures.bind_lane("DEMO-9343", "/tmp/repo-9343", lane=3)
        answer = Path(tmphome.mkdtemp(prefix="pplreflect-")) / "answer.json"

        def record(lane, task):
            answer.write_text(json.dumps({"task": task, "no_entry": True}), encoding="utf-8")
            return self.in_lane(lane, "lessons", "record", "--task", task, "--file",
                                str(answer))

        # a pending marker no lane claimed, and another lane's run
        for task in ("DEMO-9344", "DEMO-9343"):
            code, out, err = record("DEMO-9353", task)
            self.assertEqual(code, 2, out)
            payload = json.loads(err)
            self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
            self.assertIn("ppl lessons pending", payload["message"])
        self.assertTrue(lessons.pending_path("DEMO-9344").is_file())
        self.assertFalse(lessons.proposals_path("example-maven", "DEMO-9344").exists())
        # claimed by the other lane: still not this one's
        self.assertTrue(lessons.claim_pending("DEMO-9344", "DEMO-9354"))
        code, _, err = record("DEMO-9353", "DEMO-9344")
        self.assertEqual((code, json.loads(err)["error_code"]), (2, "SCHEMA_INVALID"))
        code, out, err = record("DEMO-9354", "DEMO-9344")
        self.assertEqual((code, out.strip()), (0, "recorded no entry for DEMO-9344"), err)
        self.assertFalse(lessons.claimed_path("DEMO-9344", "DEMO-9354").exists())
        self.assertTrue(lessons.proposals_path("example-maven", "DEMO-9344").is_file())
        # a lane's own run is its own to record: this one only lacks its bundle
        code, out, err = record("DEMO-9343", "DEMO-9343")
        self.assertEqual(code, 2, out)
        self.assertIn("run ppl lessons bundle first", json.loads(err)["message"])


if __name__ == "__main__":
    unittest.main()

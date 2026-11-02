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

    def test_record_leaves_memory_unchanged(self):
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
    APPLY = "run ppl harness apply example-maven to render it"

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


if __name__ == "__main__":
    unittest.main()

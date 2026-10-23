import io
import json
import os
import runpy
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
HOOKS = Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"
sys.path.insert(0, str(HOOKS))

from ppl import paths, run, store, usage
import ppl_python_floor
import ppl_usage

TASK = "DEMO-900"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
TS = "2099-01-01T00:00:00.000Z"


def line(msg_id, model, output, ts=TS, sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": ts,
            "message": {"id": msg_id, "model": model, "usage": {
                "input_tokens": 1, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": output}}}


def write_lines(path, lines, mode="w"):
    with open(path, mode, encoding="utf-8") as handle:
        for item in lines:
            handle.write(json.dumps(item) + "\n")


def shifted(at, seconds):
    return (datetime.fromisoformat(at) + timedelta(seconds=seconds)).isoformat()


class TestUsageHook(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        self.dir = Path(tmphome.mkdtemp(prefix="pplusagehook-"))
        self.role_path = self.dir / "agent-a1.jsonl"
        self.main_path = self.dir / "session.jsonl"

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def start(self):
        """Creates the run and returns the `at` of its init event."""
        run.init(TASK, "u", "/tmp/repo")
        return next(e["at"] for e in store.read_log(TASK) if e.get("event") == "init")

    def payload(self, **changes):
        doc = {"hook_event_name": "SubagentStop", "session_id": "s1",
               "transcript_path": str(self.main_path), "cwd": "/tmp", "agent_id": "a1",
               "agent_type": "ppl-code-worker", "effort": {"level": "high"},
               "agent_transcript_path": str(self.role_path),
               "last_assistant_message": "done"}
        doc.update(changes)
        return doc

    def usage_events(self):
        return [e for e in store.read_log(TASK) if e.get("event") == "usage"]

    def orchestrator_events(self):
        return [e for e in self.usage_events() if e.get("role") == "orchestrator"]

    def main(self, payload):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = ppl_usage.main()
        return code, out.getvalue(), err.getvalue()

    def test_a_role_stop_logs_its_cumulative_usage(self):
        # rule: R-USAGE-HOOK-CANNOT-BLOCK
        self.start()
        write_lines(self.role_path, [line("m1", OPUS, 10), line("m1", OPUS, 10),
                                     line("m2", HAIKU, 3)])
        result = ppl_usage.on_stop(self.payload(transcript_path=str(self.dir / "missing.jsonl")))
        self.assertIsNone(result)
        events = self.usage_events()
        self.assertEqual(len(events), 1, events)
        event = events[0]
        self.assertEqual((event["role"], event["agent_id"], event["effort"]),
                         ("ppl-code-worker", "a1", "high"))
        self.assertNotIn("error", event)
        self.assertEqual(event["models"], usage.sum_transcript(self.role_path))
        self.assertEqual(event["models"][OPUS]["output"], 10)
        self.assertEqual(event["models"][HAIKU]["output"], 3)

    def test_effort_is_kept_as_given_or_read_from_its_level(self):
        self.start()
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        for given, kept in (("medium", "medium"), ({"level": "low"}, "low"), (5, None),
                            ({"other": "x"}, None)):
            ppl_usage.on_stop(self.payload(effort=given, agent_id="a-%s" % kept))
            self.assertEqual(self.usage_events()[-1]["effort"], kept, given)
        ppl_usage.on_stop({k: v for k, v in self.payload(agent_id="a-none").items()
                           if k != "effort"})
        self.assertIsNone(self.usage_events()[-1]["effort"])

    def test_a_non_role_agent_is_ignored(self):
        self.start()
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        write_lines(self.main_path, [line("o1", OPUS, 10)])
        for agent in ("general-purpose", "Explore", "ppl-imposter", None):
            self.assertIsNone(ppl_usage.on_stop(self.payload(agent_type=agent)))
        self.assertEqual(self.usage_events(), [])
        self.assertFalse((paths.run_dir(TASK) / "usage.json").exists())

    def test_no_active_run_is_ignored(self):
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        paths.runs_dir().mkdir(parents=True, exist_ok=True)
        for active in ("\n", TASK + "\n"):
            # the second is a stale pointer to a run whose state is gone
            paths.active_file().write_text(active, encoding="utf-8")
            self.assertIsNone(ppl_usage.on_stop(self.payload()))
            self.assertEqual(sorted(p.name for p in paths.runs_dir().iterdir()), ["ACTIVE"])

    def test_a_missing_transcript_logs_an_error_event_and_prints_nothing(self):
        # rule: R-USAGE-HOOK-CANNOT-BLOCK
        self.start()
        code, out, err = self.main(self.payload(
            agent_transcript_path=str(self.dir / "gone.jsonl")))
        self.assertEqual((code, out), (0, ""))
        events = self.usage_events()
        self.assertEqual(len(events), 1, events)
        self.assertEqual((events[0]["role"], events[0]["agent_id"], events[0]["error"]),
                         ("ppl-code-worker", "a1", "transcript_missing"))
        self.assertNotIn("models", events[0])

    def test_an_oversized_transcript_logs_transcript_too_large(self):
        self.start()
        write_lines(self.role_path, [line("m%d" % i, OPUS, 1) for i in range(5)])
        self.assertIs(ppl_usage.usage, usage)
        with mock.patch.object(usage, "MAX_TRANSCRIPT_BYTES", 100):
            ppl_usage.on_stop(self.payload())
        events = self.usage_events()
        self.assertEqual([e.get("error") for e in events], ["transcript_too_large"])

    def test_the_orchestrator_counts_only_lines_since_the_run_began(self):
        # rule: R-USAGE-HOOK-CANNOT-BLOCK
        # one orchestrator session running two tasks back to back: the lines
        # written before this run's ppl init belong to the earlier task
        at = self.start()
        write_lines(self.main_path, [
            line("o1", OPUS, 100, ts=shifted(at, -60)),
            line("o2", OPUS, 7, ts=shifted(at, 60)),
            line("o3", OPUS, 50, ts=shifted(at, 60), sidechain=True)])
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        ppl_usage.on_stop(self.payload())
        events = self.orchestrator_events()
        self.assertEqual(len(events), 1, self.usage_events())
        self.assertEqual(events[0]["agent_id"], "orchestrator:s1")
        self.assertEqual(events[0]["models"][OPUS]["output"], 7)
        stored = store.read_json(paths.run_dir(TASK) / "usage.json")
        entry = stored["orchestrator"][str(self.main_path)]
        self.assertEqual((entry["session"], entry["offset"]),
                         ("s1", self.main_path.stat().st_size))

    def test_a_second_stop_adds_only_new_orchestrator_lines(self):
        at = self.start()
        write_lines(self.main_path, [line("o1", OPUS, 7, ts=shifted(at, 60))])
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        ppl_usage.on_stop(self.payload())
        first = self.orchestrator_events()[-1]["models"]
        write_lines(self.main_path, [line("o2", OPUS, 5, ts=shifted(at, 120)),
                                     line("o3", HAIKU, 2, ts=shifted(at, 120))], mode="a")
        ppl_usage.on_stop(self.payload(agent_id="a2"))
        events = self.orchestrator_events()
        self.assertEqual(len(events), 2, events)
        second = events[-1]["models"]
        self.assertEqual(second, usage.add(first, {
            OPUS: {"input": 1, "cache_write": 0, "cache_read": 0, "output": 5, "thinking": 0},
            HAIKU: {"input": 1, "cache_write": 0, "cache_read": 0, "output": 2, "thinking": 0}}))
        self.assertEqual(second[OPUS]["output"], 12)
        stored = store.read_json(paths.run_dir(TASK) / "usage.json")
        self.assertEqual(stored["orchestrator"][str(self.main_path)]["offset"],
                         self.main_path.stat().st_size)
        # nothing new in the main transcript: no orchestrator event
        ppl_usage.on_stop(self.payload(agent_id="a3"))
        self.assertEqual(len(self.orchestrator_events()), 2)

    def test_the_same_agent_stopping_twice_is_counted_at_its_newest_totals(self):
        # a subagent continued through SendMessage stops again with the same id
        self.start()
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        ppl_usage.on_stop(self.payload())
        write_lines(self.role_path, [line("m2", OPUS, 5)], mode="a")
        ppl_usage.on_stop(self.payload())
        got = usage.run_usage(paths.run_dir(TASK))
        self.assertEqual(got["ppl-code-worker"][OPUS]["output"], 15)

    def test_the_hook_never_prints_a_decision(self):
        # rule: R-USAGE-HOOK-CANNOT-BLOCK
        at = self.start()
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        write_lines(self.main_path, [line("o1", OPUS, 7, ts=shifted(at, 60))])
        code, out, err = self.main(self.payload())
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(len(self.usage_events()), 2, self.usage_events())

    def test_an_unexpected_failure_is_an_error_event_not_a_crash(self):
        self.start()
        write_lines(self.role_path, [line("m1", OPUS, 10)])
        with mock.patch.object(usage, "sum_transcript", side_effect=RuntimeError("boom")):
            code, out, err = self.main(self.payload())
        self.assertEqual((code, out), (0, ""))
        self.assertIn("boom", err)
        self.assertEqual([e.get("error") for e in self.usage_events()], ["internal"])

    def test_the_floor_names_subagentstop(self):
        # The hook's own call, captured, is replayed under Python 3.9.0.
        calls = []

        def spy(version_info, hook, event, stdin, stdout, stderr):
            calls.append((hook, event))
            return True
        with mock.patch.object(ppl_python_floor, "refuse_old_python", spy):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(HOOKS / "ppl_usage.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(len(calls), 1, calls)
        hook, event = calls[0]
        self.assertEqual(event, "SubagentStop")
        out, err = io.StringIO(), io.StringIO()
        handled = ppl_python_floor.refuse_old_python(
            (3, 9, 0), hook, event, io.StringIO(json.dumps(self.payload())), out, err)
        self.assertTrue(handled)
        if out.getvalue():
            self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["hookEventName"],
                             "SubagentStop")
        self.assertIn("ppl_usage", err.getvalue())

    def test_invalid_payloads_are_ignored(self):
        self.start()
        for payload in (None, 42, [], "x"):
            self.assertIsNone(ppl_usage.on_stop(payload))
        self.assertEqual(self.usage_events(), [])


if __name__ == "__main__":
    unittest.main()

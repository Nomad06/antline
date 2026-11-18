import ast
import io
import json
import os
import runpy
import stat
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
HOOKS = Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"
sys.path.insert(0, str(HOOKS))

import fixtures
from ppl import paths, run, store
import ppl_python_floor
import ppl_session

TASK = "DEMO-910"
# The hook's own command line in TASK's lane session, as the lane's settings render it.
LANE = ["--task", TASK]


class TestSessionHook(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def start(self):
        run.init(TASK, "u", "/tmp/repo")
        fixtures.bind_lane(TASK, "/tmp/repo")

    def session_events(self):
        return [{k: v for k, v in e.items() if k not in ("event", "at")}
                for e in store.read_log(TASK) if e.get("event") == "session"]

    def raw_log(self):
        return (paths.run_dir(TASK) / "log.jsonl").read_text(encoding="utf-8")

    def main(self, payload, argv=LANE):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "argv", ["ppl_session.py"] + list(argv)), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = ppl_session.main()
        return code, out.getvalue(), err.getvalue()

    def test_a_stop_logs_the_running_background_count(self):
        # rule: R-SESSION-HOOK-CANNOT-BLOCK
        self.start()
        ppl_session.on_event({"hook_event_name": "Stop", "background_tasks": [
            {"id": "a", "type": "subagent", "status": "running"},
            {"id": "b", "type": "shell", "status": "completed"}], "session_crons": []}, LANE)
        self.assertEqual(self.session_events(), [{"kind": "stop", "background": 1, "crons": 0}])

    def test_a_stop_counts_only_running_entries_that_are_objects(self):
        self.start()
        ppl_session.on_event({"hook_event_name": "Stop", "background_tasks": [
            {"status": "running"}, "running", None, {"status": "Running"},
            {"id": "c", "type": "shell", "status": "running"}],
            "session_crons": [{"id": "x"}, {"id": "y"}]}, LANE)
        self.assertEqual(self.session_events(), [{"kind": "stop", "background": 2, "crons": 2}])

    def test_a_stop_without_the_fields_logs_null_counts(self):
        self.start()
        ppl_session.on_event({"hook_event_name": "Stop", "background_tasks": "many"}, LANE)
        self.assertEqual(self.session_events(),
                         [{"kind": "stop", "background": None, "crons": None}])

    def test_the_last_message_is_never_logged(self):
        # rule: R-SESSION-EVENT-NO-TEXT
        self.start()
        ppl_session.on_event({"hook_event_name": "Stop", "last_assistant_message": "SECRET TEXT",
                              "background_tasks": [{"id": "a", "type": "subagent",
                                                    "status": "running",
                                                    "description": "SECRET TASK"}],
                              "session_crons": [{"prompt": "SECRET CRON"}],
                              "transcript_path": "/tmp/SECRET-PATH.jsonl", "cwd": "/tmp/SECRET-CWD",
                              "session_id": "SECRET-SESSION"}, LANE)
        raw = self.raw_log()
        for text in ("SECRET TEXT", "SECRET TASK", "SECRET CRON", "SECRET-PATH", "SECRET-CWD",
                     "SECRET-SESSION"):
            self.assertNotIn(text, raw)
        event = [e for e in store.read_log(TASK) if e.get("event") == "session"][0]
        self.assertEqual(sorted(event), ["at", "background", "crons", "event", "kind"])

    def test_a_session_end_logs_a_plain_reason_only(self):
        # rule: R-SESSION-EVENT-NO-TEXT
        self.start()
        ppl_session.on_event({"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"}, LANE)
        ppl_session.on_event({"hook_event_name": "SessionEnd", "reason": "Not A Plain Word!"}, LANE)
        for reason in ("other\n", "x" * 41, "", 7, None):
            ppl_session.on_event({"hook_event_name": "SessionEnd", "reason": reason}, LANE)
        self.assertEqual(self.session_events(), [{"kind": "end", "reason": "prompt_input_exit"}]
                         + [{"kind": "end", "reason": None}] * 6)

    def test_a_notification_logs_its_type_and_not_its_message(self):
        # rule: R-SESSION-EVENT-NO-TEXT
        self.start()
        ppl_session.on_event({"hook_event_name": "Notification",
                              "notification_type": "permission_prompt",
                              "message": "Claude needs permission to run rm"}, LANE)
        self.assertEqual(self.session_events(),
                         [{"kind": "notification", "type": "permission_prompt"}])
        self.assertNotIn("needs permission", self.raw_log())

    def test_an_unknown_event_and_a_hook_outside_a_lane_log_nothing(self):
        # rule: R-SESSION-HOOK-CANNOT-BLOCK
        self.start()
        ppl_session.on_event({"hook_event_name": "PreCompact"}, LANE)
        ppl_session.on_event({"hook_event_name": "Stop"}, ())
        for payload in (None, 42, [], "x", {}):
            ppl_session.on_event(payload, LANE)
        self.assertEqual(self.session_events(), [])

    def test_a_lane_the_registry_does_not_hold_or_a_run_without_state_logs_nothing(self):
        paths.runs_dir().mkdir(parents=True, exist_ok=True)
        # a lane the registry does not hold, then a lane whose run's state is gone
        for registered in (False, True):
            if registered:
                fixtures.bind_lane(TASK, "/tmp/repo")
            ppl_session.on_event({"hook_event_name": "Stop", "background_tasks": []}, LANE)
            self.assertEqual(list(paths.runs_dir().iterdir()), [])

    def test_the_hook_never_prints_a_decision(self):
        # rule: R-SESSION-HOOK-CANNOT-BLOCK
        self.start()
        code, out, err = self.main({"hook_event_name": "Stop", "background_tasks": [],
                                    "session_crons": []})
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertEqual(self.session_events(), [{"kind": "stop", "background": 0, "crons": 0}])
        # an unreadable payload and an internal failure exit 0 and print nothing on stdout
        with mock.patch.object(sys, "stdin", io.StringIO("not json")), \
                mock.patch.object(sys, "stdout", io.StringIO()) as out:
            self.assertEqual(ppl_session.main(), 0)
        self.assertEqual(out.getvalue(), "")
        with mock.patch.object(ppl_session.store, "append_log",
                               side_effect=RuntimeError("boom")):
            code, out, err = self.main({"hook_event_name": "Stop"})
        self.assertEqual((code, out), (0, ""))
        self.assertIn("boom", err)

    def test_an_old_interpreter_prints_nothing(self):
        # rule: R-SESSION-HOOK-CANNOT-BLOCK
        # refuse_old_python writes a permissionDecision for any call in a lane, and a Stop
        # hook must print nothing: the session hook takes its own, silent floor.
        out, err = io.StringIO(), io.StringIO()
        payload = json.dumps({"hook_event_name": "Stop", "background_tasks": []})
        with mock.patch.object(ppl_python_floor, "MINIMUM", (99, 0)), \
                mock.patch.object(ppl_python_floor, "refuse_old_python",
                                  side_effect=AssertionError("the floor that prints was called")), \
                mock.patch.object(sys, "argv", [str(HOOKS / "ppl_session.py")] + LANE), \
                mock.patch.object(sys, "stdin", io.StringIO(payload)), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(HOOKS / "ppl_session.py"), run_name="__main__")
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(err.getvalue().splitlines()), 1, err.getvalue())
        self.assertIn("ppl_session", err.getvalue())
        self.assertIn("99.0", err.getvalue())
        # and the real file under the real old interpreter, where one is installed
        found = fixtures.older_system_python()
        if found is None:
            return
        interpreter, version = found
        result = subprocess.run([interpreter, str(HOOKS / "ppl_session.py")] + LANE,
                                input=payload, capture_output=True, text=True,
                                env=fixtures.isolated_env(), timeout=30)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn(version, result.stderr)

    def test_the_hook_parses_on_python_3_9(self):
        # rule: R-GUARD-PYTHON-FLOOR
        # The silent floor runs only if the file parses on the old interpreter, and the
        # subprocess branch above skips where /usr/bin/python3 is 3.11 or newer. This holds
        # the syntax everywhere, and CI's python-floor job runs the file under 3.9.
        source = (HOOKS / "ppl_session.py").read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))

    def test_a_fifo_log_is_skipped_without_blocking(self):
        # rule: R-SESSION-HOOK-CANNOT-BLOCK
        # opening a FIFO blocks until the other end opens: the hook would hang
        # until Claude Code's timeout
        self.start()
        fifo = paths.run_dir(TASK) / "log.jsonl"
        fifo.unlink()
        os.mkfifo(str(fifo))
        result = {}
        out, err = io.StringIO(), io.StringIO()
        payload = {"hook_event_name": "Stop", "background_tasks": []}
        # a daemon thread, so a hook stuck on the FIFO cannot hold the
        # suite, and the patches are undone here whatever it does
        worker = threading.Thread(
            target=lambda: result.setdefault("code", ppl_session.main()), daemon=True)
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(sys, "argv", ["ppl_session.py"] + LANE), \
                mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            started = time.monotonic()
            worker.start()
            worker.join(1.0)
            elapsed = time.monotonic() - started
        self.assertFalse(worker.is_alive(), "the hook blocked on the FIFO")
        self.assertLess(elapsed, 1.0)
        self.assertEqual((result["code"], out.getvalue()), (0, ""))
        self.assertTrue(stat.S_ISFIFO(os.lstat(str(fifo)).st_mode))


if __name__ == "__main__":
    unittest.main()

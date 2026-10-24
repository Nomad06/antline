import io
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))

from ppl import control, paths, run, store
import ppl_guard


def edit(path, agent="ppl-code-worker"):
    return {"tool_name": "Edit", "agent_type": agent,
            "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"}}


def denies(task_id):
    return [e for e in store.read_log_at(paths.run_dir(task_id))
            if e.get("event") == "deny"]


class TestGuardDenialLog(unittest.TestCase):
    def setUp(self):
        active = paths.active_file()
        if active.exists():
            active.unlink()

    def test_a_denied_code_worker_edit_logs_one_deny_event(self):
        # rule: R-GUARD-DENIAL-LOGGED
        repo = tmphome.mkdtemp(prefix="repo-")
        run.init("DEMO-8801", "u", repo)
        run.set_active("DEMO-8801")
        result = ppl_guard.decide(edit(repo + "/src/App.java"))
        self.assertIn("BOUNDARY_VIOLATION",
                      result["hookSpecificOutput"]["permissionDecisionReason"])
        events = denies("DEMO-8801")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["role"], "ppl-code-worker")
        self.assertEqual(events[0]["tool"], "Edit")
        self.assertEqual(events[0]["code"], "BOUNDARY_VIOLATION")
        self.assertEqual(events[0]["target"], repo + "/src/App.java")

    def test_the_logged_target_is_cut_to_200_characters(self):
        repo = tmphome.mkdtemp(prefix="repo-")
        run.init("DEMO-8802", "u", repo)
        run.set_active("DEMO-8802")
        ppl_guard.decide(edit(repo + "/" + "x" * 400))
        self.assertEqual(len(denies("DEMO-8802")[0]["target"]), 200)

    def test_a_deny_still_returns_when_the_log_directory_is_not_writable(self):
        # rule: R-GUARD-DENIAL-LOGGED
        repo = tmphome.mkdtemp(prefix="repo-")
        run.init("DEMO-8803", "u", repo)
        run.set_active("DEMO-8803")
        directory = paths.run_dir("DEMO-8803")
        log = directory / "log.jsonl"
        log.touch()
        os.chmod(log, stat.S_IRUSR)
        self.addCleanup(os.chmod, log, stat.S_IRUSR | stat.S_IWUSR)
        result = ppl_guard.decide(edit(repo + "/src/App.java"))
        self.assertIn("BOUNDARY_VIOLATION",
                      result["hookSpecificOutput"]["permissionDecisionReason"])

    def test_an_allowed_call_writes_nothing(self):
        repo = tmphome.mkdtemp(prefix="repo-")
        run.init("DEMO-8804", "u", repo)
        run.set_active("DEMO-8804")
        before = store.read_log_at(paths.run_dir("DEMO-8804"))
        self.assertIsNone(ppl_guard.decide(
            {"tool_name": "Read", "agent_type": None,
             "tool_input": {"file_path": repo + "/a.txt"}}))
        self.assertEqual(store.read_log_at(paths.run_dir("DEMO-8804")), before)

    def test_a_deny_with_no_active_run_logs_nothing(self):
        def total():
            return sum(1 for p in paths.runs_dir().glob("*/log.jsonl")
                       for e in store.read_log_at(p.parent) if e.get("event") == "deny")
        before = total()
        ppl_guard.decide(edit("/nowhere/src/App.java"))
        ppl_guard.decide({"tool_name": "Task", "agent_type": "ppl-code-worker",
                          "tool_input": {}})
        self.assertEqual(total(), before)


class OrderedStdout(io.StringIO):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def write(self, text):
        self.order.append("write")
        return super().write(text)

    def flush(self):
        self.order.append("flush")
        return super().flush()


def run_main(hook_payload, stdout):
    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(hook_payload))), \
            mock.patch.object(sys, "stdout", stdout), \
            mock.patch.object(sys, "stderr", io.StringIO()):
        return ppl_guard.main()


class TestGuardDenialLogFromMain(unittest.TestCase):
    """main() is what Claude Code runs: the decision goes out before the log is touched,
    so a log that is slow or blocks cannot hold the decision back."""

    def setUp(self):
        active = paths.active_file()
        if active.exists():
            active.unlink()

    def active_run(self, task_id, isolated=True):
        repo = tmphome.mkdtemp(prefix="repo-")
        run.init(task_id, "u", repo, isolated=isolated)
        run.set_active(task_id)
        return repo

    def test_main_writes_and_flushes_the_decision_before_it_logs_the_deny(self):
        repo = self.active_run("DEMO-8810")
        order = []
        stdout = OrderedStdout(order)
        with mock.patch.object(ppl_guard, "log_denial",
                               side_effect=lambda _payload, _result: order.append("log")):
            self.assertEqual(run_main(edit(repo + "/src/App.java"), stdout), 0)
        self.assertIn("BOUNDARY_VIOLATION", stdout.getvalue())
        self.assertEqual(order[-1], "log")
        self.assertIn("flush", order[:-1])
        self.assertEqual(order[0], "write")

    def test_main_still_logs_one_deny_event(self):
        repo = self.active_run("DEMO-8811")
        stdout = io.StringIO()
        self.assertEqual(run_main(edit(repo + "/src/App.java"), stdout), 0)
        self.assertIn("BOUNDARY_VIOLATION", stdout.getvalue())
        events = denies("DEMO-8811")
        self.assertEqual([(e["role"], e["code"]) for e in events],
                         [("ppl-code-worker", "BOUNDARY_VIOLATION")])

    def test_the_internal_error_branch_logs_the_role_deny_it_returns(self):
        repo = self.active_run("DEMO-8812")
        stdout = io.StringIO()
        with mock.patch.object(ppl_guard, "_decide", side_effect=RuntimeError("boom")):
            self.assertEqual(run_main(edit(repo + "/src/App.java"), stdout), 0)
        self.assertIn("BOUNDARY_VIOLATION", stdout.getvalue())
        events = denies("DEMO-8812")
        self.assertEqual([(e["role"], e["tool"], e["code"], e["target"]) for e in events],
                         [("ppl-code-worker", "Edit", "BOUNDARY_VIOLATION",
                           repo + "/src/App.java")])

    def test_a_log_that_is_not_a_regular_file_is_skipped_not_opened(self):
        # a FIFO opened for append blocks until a reader appears, and the hook would hang
        repo = self.active_run("DEMO-8813")
        log = paths.run_dir("DEMO-8813") / "log.jsonl"
        log.unlink()
        os.mkfifo(str(log))
        self.addCleanup(log.unlink)
        with mock.patch.object(store, "append_log") as append:
            result = ppl_guard.decide(edit(repo + "/src/App.java"))
        self.assertIn("BOUNDARY_VIOLATION",
                      result["hookSpecificOutput"]["permissionDecisionReason"])
        append.assert_not_called()

    def test_a_run_dir_linked_elsewhere_gets_no_deny_event(self):
        # rule: R-GUARD-DENIAL-LOGGED
        # runs/ is sandbox-writable: a run directory a role swapped for a link would steer the
        # hook's append anywhere
        repo = tmphome.mkdtemp(prefix="repo-")
        run.init("DEMO-8897", "u", repo)
        run.set_active("DEMO-8897")
        elsewhere = Path(tmphome.mkdtemp(prefix="elsewhere-"))
        real_dir = paths.run_dir("DEMO-8897")
        for item in real_dir.iterdir():
            item.rename(elsewhere / item.name)
        real_dir.rmdir()
        real_dir.symlink_to(elsewhere)
        before = (elsewhere / "log.jsonl").read_text(encoding="utf-8")
        result = ppl_guard.decide(edit(repo + "/src/App.java"))
        self.assertIn("BOUNDARY_VIOLATION",
                      result["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual((elsewhere / "log.jsonl").read_text(encoding="utf-8"), before)
        self.assertNotIn("deny", before)

    def test_a_log_that_does_not_exist_yet_is_still_created(self):
        repo = self.active_run("DEMO-8814")
        (paths.run_dir("DEMO-8814") / "log.jsonl").unlink()
        ppl_guard.decide(edit(repo + "/src/App.java"))
        self.assertEqual(len(denies("DEMO-8814")), 1)

    def doctor_probe(self, workspace, **extra):
        probe = {"tool_name": "Read", "tool_input": {"file_path": "/dev/null"}, "cwd": workspace,
                 "session_id": ppl_guard.DOCTOR_PROBE_SESSION_ID}
        probe.update(extra)
        return probe

    def test_the_doctor_probe_is_denied_under_a_paused_run_but_not_logged_against_it(self):
        workspace = self.active_run("DEMO-8815")
        control.set_command("DEMO-8815", "pause")
        result = ppl_guard.decide(self.doctor_probe(workspace))
        self.assertIn("OWNER_DECISION", result["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(denies("DEMO-8815"), [])

    def test_only_the_whole_probe_shape_skips_the_log(self):
        workspace = self.active_run("DEMO-8816")
        control.set_command("DEMO-8816", "pause")
        for changed in ({"session_id": "a-real-session"},
                        {"tool_input": {"file_path": workspace + "/a.txt"}},
                        {"tool_name": "Write"}):
            ppl_guard.decide(self.doctor_probe(workspace, **changed))
        self.assertEqual(len(denies("DEMO-8816")), 3)


if __name__ == "__main__":
    unittest.main()

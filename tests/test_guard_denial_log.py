import os
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from ppl import paths, run, store
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


if __name__ == "__main__":
    unittest.main()

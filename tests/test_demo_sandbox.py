import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import paths
from ppl.errors import PplError
from ppl.demo import sandbox


class TestSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = sandbox.Sandbox.create()
        cls.addClassCleanup(cls.box.remove)
        cls.box.build()

    def test_the_built_sandbox_has_an_initialised_run_on_the_sample_repo(self):
        shown = json.loads(self.box.ppl("show", "--json").stdout)
        self.assertEqual(shown["task_id"], "GH-1")
        self.assertEqual(shown["stage"], "admission")
        self.assertEqual(shown["repo"], str(self.box.repo))
        self.assertEqual(shown["model_check"], "skipped")
        self.assertEqual(len(self.box.git("log", "--oneline").splitlines()), 1)

    def test_the_guard_command_is_the_one_setup_registered(self):
        settings = json.loads((self.box.workspace / ".claude" / "settings.local.json").read_text())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(str(paths.package_parent() / "ppl" / "_assets" / "hooks" / "ppl_guard.py"),
                      command)
        decision = self.box.guard({"hook_event_name": "PreToolUse", "agent_type": None,
                                   "tool_name": "Bash",
                                   "tool_input": {"command": "git push --force origin main"},
                                   "cwd": str(self.box.workspace)})
        self.assertEqual((decision["decision"], decision["code"]), ("deny", "BOUNDARY_VIOLATION"))

    def test_the_gh_stand_in_answers_the_issue_and_404s_the_links(self):
        self.assertEqual(self.box.ppl("stage", "enter", "baseline").returncode, 0)
        base = self.box.git("rev-parse", "HEAD").strip()
        record = self.box.root / "baseline.json"
        record.write_text(json.dumps({"base_sha": base}))
        self.assertEqual(self.box.ppl("baseline", "record", str(record)).returncode, 0)
        self.assertEqual(self.box.ppl("stage", "enter", "briefing").returncode, 0)
        fetched = self.box.ppl("tracker", "fetch")
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        issue = (self.box.ppl_home / "runs" / "GH-1" / "tracker" / "issue.md").read_text()
        self.assertIn("Cart total ignores the discount", issue)
        self.assertIn("Parent: unavailable", issue)

    def test_the_sandbox_env_carries_no_caller_identity_or_data_root(self):
        caller = {"PPL_HOME": "/x", "GH_TOKEN": "t", "GIT_DIR": "/y", "CLAUDECODE": "1"}
        with mock.patch.dict(os.environ, caller):
            other = sandbox.Sandbox.create()
        self.addCleanup(other.remove)
        for name in caller:
            if name == "PPL_HOME":
                self.assertEqual(other.env[name], str(other.root / "ppl"))
            else:
                self.assertNotIn(name, other.env)
        self.assertEqual(Path(other.env["HOME"]).parent, other.root)

    def test_a_hostile_xdg_git_config_of_the_caller_is_never_read(self):
        xdg = Path(tmphome.mkdtemp(prefix="xdg-"))
        (xdg / "git").mkdir()
        (xdg / "git" / "config").write_text(
            "[init]\n\tdefaultBranch = trunk\n[core]\n\thooksPath = /nonexistent-hooks\n")
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            other = sandbox.Sandbox.create()
        self.addCleanup(other.remove)
        self.assertEqual(Path(other.env["XDG_CONFIG_HOME"]), other.home / ".config")
        self.assertEqual(other.env["GIT_CONFIG_GLOBAL"], "/dev/null")
        other.build()
        other.repo.joinpath("extra.txt").write_text("x")
        self.assertEqual(len(other.commit(["extra.txt"], "extra")), 40)
        read = subprocess.run(["git", "-C", str(other.repo), "config", "--get", "init.defaultBranch"],
                              env=other.env, capture_output=True, text=True)
        self.assertNotEqual(read.stdout.strip(), "trunk")

    def test_a_failing_git_step_raises_a_ppl_error_naming_its_stderr(self):
        with self.assertRaises(PplError) as caught:
            self.box.commit(["no-such-file.txt"], "never")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("no-such-file.txt", caught.exception.message)

    def test_remove_deletes_the_whole_sandbox(self):
        other = sandbox.Sandbox.create()
        self.assertTrue(other.root.is_dir())
        other.remove()
        self.assertFalse(other.root.exists())


if __name__ == "__main__":
    unittest.main()

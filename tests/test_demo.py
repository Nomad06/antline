import json
import os
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

from ppl import demo, rules
from ppl.demo import script
from ppl.errors import PplError

CLONE = Path(__file__).resolve().parents[1]
HOSTILE_GITCONFIG = (
    "[commit]\n\tgpgsign = true\n[init]\n\tdefaultBranch = trunk\n"
    "[core]\n\thooksPath = %s\n")
REFUSED_OR_DENIED = [
    (4, "refused ILLEGAL_TRANSITION", "R-TRACKER-FETCH-PROVENANCE"),
    (7, "refused ILLEGAL_TRANSITION", "R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION"),
    (10, "denied BOUNDARY_VIOLATION", "R-ROLE-CODE-WORKER-TESTS"),
    (11, "denied BOUNDARY_VIOLATION", "R-ROLE-TEST-WORKER-MAIN"),
    (13, "denied BOUNDARY_VIOLATION", "R-TEST-WORKER-DISPATCH-LEAK"),
    (20, "refused ILLEGAL_TRANSITION", "R-CLOSURE-ONLY-WHEN-DONE-PASSES"),
    (22, "denied OWNER_DECISION", "R-TRACKER-CLI-GATE"),
]


def temp_dir(case, prefix):
    path = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    case.addCleanup(shutil.rmtree, path, ignore_errors=True)
    return path


class TestDemo(unittest.TestCase):
    def test_the_demo_passes_and_stays_in_its_sandbox(self):
        base = temp_dir(self, "demotest-")
        home, tmp, pplhome, bindir = (base / "home", base / "with space", base / "pplhome",
                                      base / "bin")
        for directory in (home, tmp, pplhome, bindir):
            directory.mkdir()
        hooks = home / "hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n")
        (hooks / "pre-commit").chmod(0o755)
        gitconfig = home / ".gitconfig"
        gitconfig.write_text(HOSTILE_GITCONFIG % hooks)
        gh = bindir / "gh"
        gh.write_text("#!/bin/sh\ntouch '%s'\nexit 1\n" % (bindir / "gh-was-run"))
        gh.chmod(0o755)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("PPL_", "GIT_", "GH_"))}
        env.update(HOME=str(home), TMPDIR=str(tmp), PPL_HOME=str(pplhome), PYTHONPATH=str(CLONE),
                   PATH=os.pathsep.join([str(bindir), env["PATH"]]))
        done = subprocess.run([sys.executable, "-m", "ppl", "demo", "--keep"], cwd=str(base),
                              env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertFalse((bindir / "gh-was-run").exists())
        lines = done.stdout.splitlines()
        marked = [line for line in lines if " denied " in line or " refused " in line]
        self.assertEqual(len(marked), len(REFUSED_OR_DENIED), "\n".join(marked))
        for line, (number, answer, rule) in zip(marked, REFUSED_OR_DENIED):
            self.assertTrue(line.startswith("%2d " % number), line)
            self.assertIn(" -> " + answer, line)
            self.assertTrue(line.endswith("[%s]" % rule), line)
        self.assertIn("ppl merge observed", marked[-1])
        self.assertEqual(sorted(path.name for path in home.iterdir()), [".gitconfig", "hooks"])
        self.assertEqual(gitconfig.read_text(), HOSTILE_GITCONFIG % hooks)
        self.assertEqual(list(pplhome.iterdir()), [])
        (sandbox,) = [path for path in tmp.iterdir() if path.name.startswith("antline-demo-")]
        self.assertEqual(len(list(tmp.iterdir())), 1)
        state = json.loads((sandbox / "ppl" / "runs" / "GH-1" / "state.json").read_text())
        self.assertEqual((state["stage"], state["status"], state["budgets"]["retry_used"]),
                         ("merge_watch", "done", 1))
        self.assertEqual(lines[-1], "kept: %s. Look at the run: PPL_HOME=%s ppl serve --open"
                         % (sandbox, sandbox / "ppl"))
        self.assertRegex(lines[-2], r"^done in [0-9.]+ s$")
        self.assertEqual(len(lines), len(script.STEPS) + 3)

    def test_every_demo_rule_id_is_a_row(self):
        known = {row["id"] for row in rules.load()}
        for actor, tried, expect, rule in script.STEPS:
            if rule:
                self.assertIn(rule, known, tried)

    def test_the_steps_and_their_actions_pair_up(self):
        self.assertEqual(len(script.STEPS), 24)
        self.assertEqual(len(script.ACTIONS), len(script.STEPS))


class TestDemoFailure(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir(self, "demotmp-")
        patcher = mock.patch.object(tempfile, "tempdir", str(self.tmp))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_step_that_raises_removes_the_sandbox(self):
        actions = list(script.ACTIONS)
        actions[1] = mock.Mock(side_effect=RuntimeError("boom"))
        lines = []
        with mock.patch.object(script, "ACTIONS", actions):
            with self.assertRaises(PplError) as caught:
                demo.run(emit=lines.append)
        self.assertEqual(caught.exception.code, "IMPLEMENTATION_FAILURE")
        self.assertIn(script.STEPS[1][1], caught.exception.message)
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_an_unexpected_answer_stops_the_walk(self):
        steps = list(script.STEPS)
        actor, tried, _, rule = steps[9]
        steps[9] = (actor, tried, "allow", rule)
        lines = []
        with mock.patch.object(script, "STEPS", steps):
            with self.assertRaises(PplError) as caught:
                demo.run(emit=lines.append)
        self.assertEqual(caught.exception.code, "IMPLEMENTATION_FAILURE")
        self.assertTrue(lines[-1].endswith("UNEXPECTED, expected allow"), lines[-1])
        self.assertTrue(lines[-1].startswith("10 "), lines[-1])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_no_git_on_path_exits_two_and_creates_nothing(self):
        empty = Path(tempfile.mkdtemp(prefix="nopath-", dir=str(self.tmp.parent))).resolve()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        env = {key: value for key, value in os.environ.items() if not key.startswith("PPL_")}
        env.update(PATH=str(empty), TMPDIR=str(self.tmp), PYTHONPATH=str(CLONE),
                   PPL_HOME=str(empty / "pplhome"))
        done = subprocess.run([sys.executable, "-m", "ppl", "demo"], cwd=str(empty), env=env,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        error = json.loads(done.stderr.strip().splitlines()[-1])
        self.assertEqual(error["error_code"], "EXTERNAL_BLOCKED")
        self.assertIn("git", error["message"])
        self.assertEqual(list(self.tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

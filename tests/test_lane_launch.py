import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tomllib
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import (cli, config, harness, lane_launch, lanes, lessons, os_sandbox, paths, run,
                 setup, store, tomlout)
from ppl.errors import PplError

ROOT = Path(__file__).resolve().parents[1]
TASK = "KEY-7"
LINK = "https://example.atlassian.net/browse/KEY-7"
HARNESS = "lane-maven"
BRANCH = TASK + "-work"
# The servers example-maven names, as the operator's ~/.claude.json holds them.
SERVERS = {"atlassian": {"type": "http", "url": "https://a.example/mcp"},
           "GitLab-MCP": {"command": "npx", "args": ["gitlab"]}}


def git(directory, *args):
    done = subprocess.run(["git", "-C", str(directory), *args], check=True,
                          capture_output=True, text=True)
    return done.stdout.strip()


def call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def refusal(test, code, function, *args, **kwargs):
    with test.assertRaises(PplError) as caught:
        function(*args, **kwargs)
    test.assertEqual(caught.exception.code, code, caught.exception.message)
    return caught.exception.message


class LaneCase(unittest.TestCase):
    """A project repository with a bare origin, an installed example-maven harness committed in
    its own git repository, and `[claude] binary` pointed at a stub that records its argv, its
    environment, its cwd and its pid in a file."""

    def setUp(self):
        self.enterContext(fixtures.git_identity())
        self.base = Path(tmphome.mkdtemp(prefix="ppllane-"))
        fake_home = self.base / "home"
        fake_home.mkdir()
        (fake_home / ".claude.json").write_text(json.dumps({"mcpServers": SERVERS}),
                                                encoding="utf-8")
        self.enterContext(mock.patch.dict(os.environ, {
            "PPL_HOME": str(self.base / "pplhome"), "HOME": str(fake_home),
            "PPL_CLAUDE_HOME": str(fake_home / ".claude")}))
        os.environ.pop("PPL_TASK", None)
        fixtures.install_routing()
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        self.project = self.make_repo(self.base / "shop")
        git(self.project, "remote", "add", "origin", str(self.origin))
        git(self.project, "push", "-q", "origin", "main")
        self.worktrees = self.base / "worktrees"
        self.worktree = self.worktrees / ("shop-" + TASK)
        self.install(HARNESS)
        self.stub_out = self.base / "claude-call.json"
        self.stub = self.base / "bin" / "claude"
        self.stub.parent.mkdir()
        self.stub.write_text(
            "#!%s\nimport json, os, sys\nwith open(%r, 'w') as out:\n"
            "    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd(),"
            " 'pid': os.getpid()}, out)\n" % (sys.executable, str(self.stub_out)),
            encoding="utf-8")
        self.stub.chmod(0o755)
        self.configure(str(self.stub))

    def make_repo(self, directory):
        subprocess.run(["git", "init", "-q", "-b", "main", str(directory)], check=True)
        (directory / "README.md").write_text("shop\n", encoding="utf-8")
        git(directory, "add", "-A")
        git(directory, "commit", "-q", "-m", "first")
        return directory

    def install(self, name, project=None, **tables):
        repo = {"path": str(project or self.project), "default_branch": "main",
                "worktrees": str(self.worktrees)}
        target = fixtures.install_variant("example-maven", name, repo=repo, **tables)
        git(target, "init", "-q", "-b", "main")
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", "Import %s" % name)
        return target

    def edit_harness(self, **env):
        path = paths.harness_dir(HARNESS) / "harness.toml"
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
        doc["env"].update(env)
        path.write_text(tomlout.dumps(doc), encoding="utf-8")

    def configure(self, binary):
        paths.home().mkdir(parents=True, exist_ok=True)
        paths.config_file().write_text(tomlout.dumps({"claude": {"binary": binary}}),
                                       encoding="utf-8")

    def ppl(self, *args, **env):
        """`ppl` in a child process, so its exec of the stub replaces the child, not the suite."""
        child = dict(os.environ, PYTHONPATH=str(ROOT), **env)
        return subprocess.run([sys.executable, "-m", "ppl", *args], cwd=str(self.base),
                              env=child, capture_output=True, text=True, timeout=120)

    def stub_call(self):
        return json.loads(self.stub_out.read_text(encoding="utf-8"))

    def staging_dirs(self):
        root = paths.home() / "lanes"
        return sorted(name for name in os.listdir(str(root))
                      if name.startswith(".staging-")) if root.is_dir() else []

    def branch_exists(self, branch=BRANCH):
        return bool(git(self.project, "branch", "--list", branch))

    def planted(self, name):
        """(an executable script that appends to a marker file, that marker file)."""
        marker = self.base / ("%s.ran" % name)
        script = self.base / "planted" / name
        script.parent.mkdir(exist_ok=True)
        script.write_text("#!/bin/sh\necho ran >> %s\n" % marker, encoding="utf-8")
        script.chmod(0o755)
        return script, marker

    def assert_nothing_made(self):
        self.assertFalse(lanes.start_dir(TASK).exists())
        self.assertFalse(os.path.lexists(paths.run_dir(TASK)))
        self.assertFalse(os.path.lexists(os_sandbox.scratch_dir(TASK)))
        self.assertFalse(self.worktree.exists())
        self.assertNotIn(str(self.worktree), git(self.project, "worktree", "list"))
        self.assertEqual(self.staging_dirs(), [])

    def launch_line(self, entry, link=LINK, resume=False):
        start = entry["start_dir"]
        agents = Path(start, "agents.json").read_text(encoding="utf-8")
        line = [str(self.stub), "--resume" if resume else "--session-id", entry["session_id"],
                "--setting-sources", "local", "--settings", start + "/settings.json",
                "--agents", agents, "--plugin-dir", start + "/plugin", "--strict-mcp-config",
                "--mcp-config", start + "/mcp.json", "--add-dir", entry["worktree"],
                "--permission-mode", "acceptEdits", "-n", TASK]
        return line if resume else line + ["/antline:pipeline " + link]


class TestTaskFromLink(unittest.TestCase):
    JIRA = {"task": {"key_pattern": "^[A-Z][A-Z0-9]+-[0-9]+$",
                     "url_template": "https://example.atlassian.net/browse/<TASK-ID>"},
            "tracker": {"kind": "jira"}}
    GITHUB = {"task": {"key_pattern": "^GH-[0-9]+$",
                       "url_template": "https://github.com/o/r/issues/<TASK-ID-number>"},
              "tracker": {"kind": "github-issues", "repo": "o/r", "task_prefix": "GH"}}

    def test_task_from_a_github_issue_link_and_a_jira_link_and_a_bare_id(self):
        read = lane_launch.task_from_link
        self.assertEqual(read(self.JIRA, "https://example.atlassian.net/browse/KEY-7"), "KEY-7")
        self.assertEqual(read(self.JIRA, "https://example.atlassian.net/browse/KEY-7?focusedId=1"),
                         "KEY-7")
        self.assertEqual(read(self.GITHUB, "https://github.com/o/r/issues/12"), "GH-12")
        self.assertEqual(read(self.GITHUB, "https://github.com/o/r/issues/12#issuecomment-3"),
                         "GH-12")
        self.assertEqual(read(self.JIRA, "KEY-7"), "KEY-7")
        self.assertEqual(read(self.GITHUB, "GH-12"), "GH-12")
        unprefixed = dict(self.GITHUB, tracker={"kind": "github-issues", "repo": "o/r"})
        self.assertEqual(read(unprefixed, "https://github.com/o/r/issues/12"), "GH-12")
        for doc, link in ((self.GITHUB, "https://github.com/o/other/issues/12"),
                          (self.GITHUB, "KEY-7"),
                          (self.JIRA, "https://evil.example/browse/KEY-7"),
                          (self.JIRA, "https://exampleXatlassian.net/browse/KEY-7"),
                          (self.JIRA, "https://example.atlassian.net/browse/KEY-7/../x"),
                          (self.JIRA, "key-7")):
            with self.subTest(link=link):
                message = refusal(self, "SCHEMA_INVALID", read, doc, link)
                self.assertIn(doc["task"]["url_template"], message)


class TestStart(LaneCase):
    def test_no_exec_prints_the_exact_launch_line(self):
        code, out, err = call("lane", LINK, "--harness", HARNESS, "--no-exec")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        printed = json.loads(out)
        entry = lanes.entry(TASK)
        start = str(lanes.start_dir(TASK))
        agents = Path(start, "agents.json").read_text(encoding="utf-8")
        uuid.UUID(entry["session_id"])
        self.assertEqual(printed["argv"], [
            str(self.stub), "--session-id", entry["session_id"], "--setting-sources", "local",
            "--settings", start + "/settings.json", "--agents", agents,
            "--plugin-dir", start + "/plugin", "--strict-mcp-config",
            "--mcp-config", start + "/mcp.json", "--add-dir", str(self.worktree),
            "--permission-mode", "acceptEdits", "-n", TASK, "/antline:pipeline " + LINK])
        self.assertEqual(printed["cwd"], start)
        self.assertEqual(printed["env"], {"PPL_LANE": "1", "PPL_TASK": TASK,
                                          "PPL_HOME": os.environ["PPL_HOME"]})
        self.assertEqual((entry["lane"], entry["harness"], entry["start_dir"]), (1, HARNESS, start))
        self.assertIsNone(entry["pid"])
        self.assertFalse(self.stub_out.exists())

    def test_start_creates_the_worktree_on_a_fresh_default_branch(self):
        other = self.base / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)], check=True)
        (other / "new.txt").write_text("new\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-q", "-m", "second")
        git(other, "push", "-q", "origin", "main")
        pushed = git(other, "rev-parse", "HEAD")
        self.assertNotEqual(git(self.project, "rev-parse", "main"), pushed)
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertEqual(lanes.entry(TASK)["worktree"], str(self.worktree))
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), pushed)
        self.assertEqual(git(self.worktree, "rev-parse", "--abbrev-ref", "HEAD"), BRANCH)

    def test_start_makes_the_run_directories_its_sandbox_cannot_make(self):
        # R44: the lane's profile lets its Bash write runs/<task> and scratch/<task>, not their
        # parents, so ppl lane makes both before the session starts
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        for directory in (paths.run_dir(TASK), os_sandbox.scratch_dir(TASK)):
            self.assertTrue(directory.is_dir(), directory)
            self.assertEqual(os.listdir(directory), [])
        self.assertFalse(run.state_path(TASK).exists())

    def test_a_run_directory_that_is_a_link_is_refused(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        paths.runs_dir().mkdir(parents=True)
        paths.run_dir(TASK).symlink_to(elsewhere)
        message = refusal(self, "SETUP_INCOMPLETE", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn(str(paths.run_dir(TASK)), message)
        self.assertEqual(lanes.read(), [])
        self.assertTrue(os.path.islink(paths.run_dir(TASK)))
        self.assertFalse(lanes.start_dir(TASK).exists())

    def test_without_an_origin_the_worktree_starts_from_the_local_default_branch(self):
        local = self.make_repo(self.base / "local")
        self.install(HARNESS, project=local)
        lane_launch.start(LINK, harness_name=HARNESS, slug="fix-cart", exec_=False)
        worktree = self.worktrees / ("local-" + TASK)
        self.assertEqual(git(worktree, "rev-parse", "HEAD"), git(local, "rev-parse", "main"))
        self.assertEqual(git(worktree, "rev-parse", "--abbrev-ref", "HEAD"), TASK + "-fix-cart")

    def test_a_bad_slug_is_refused(self):
        for slug in ("Fix", "a b", "x" * 41, ""):
            with self.subTest(slug=slug):
                refusal(self, "SCHEMA_INVALID", lane_launch.start, LINK, harness_name=HARNESS,
                        slug=slug, exec_=False)
        self.assertEqual(lanes.read(), [])

    def test_the_harness_is_the_one_whose_repo_holds_the_cwd(self):
        self.install("elsewhere", project=self.make_repo(self.base / "other-project"))
        inside = self.project / "src"
        inside.mkdir()
        lane_launch.start(LINK, cwd=str(inside), exec_=False)
        self.assertEqual(lanes.entry(TASK)["harness"], HARNESS)
        lanes.release(TASK)
        message = refusal(self, "SETUP_INCOMPLETE", lane_launch.start, "KEY-8",
                          cwd=str(self.base), exec_=False)
        self.assertIn("--harness", message)
        self.assertIn("elsewhere", message)
        self.assertIn(HARNESS, message)

    def test_start_renders_from_the_harness_commit(self):
        # rule: R-MEMORY-APPLIED
        installed = paths.harness_dir(HARNESS)
        self.edit_harness(JAVA_HOME="/opt/jdk-committed")
        git(installed, "commit", "-q", "-am", "jdk")
        sha = git(installed, "rev-parse", "HEAD")
        self.edit_harness(JAVA_HOME="/opt/jdk-uncommitted")
        message = refusal(self, "OWNER_DECISION", lane_launch.start, LINK, harness_name=HARNESS,
                          exec_=False)
        self.assertIn("harness.toml", message)
        self.assertEqual(lanes.read(), [])
        self.assert_nothing_made()
        git(installed, "checkout", "--", "harness.toml")
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        start = lanes.start_dir(TASK)
        self.assertEqual(lanes.entry(TASK)["harness_sha"], sha)
        exported = start / "harness" / HARNESS
        self.assertEqual((exported / "harness.toml").read_text(encoding="utf-8"),
                         git(installed, "show", sha + ":harness.toml") + "\n")
        self.assertFalse((exported / ".git").exists())
        gates = (start / "plugin" / "skills" / "pipeline" / "references" / "gates.md")
        self.assertIn("/opt/jdk-committed", gates.read_text(encoding="utf-8"))
        self.assertEqual(self.staging_dirs(), [])

    def test_a_third_lane_on_a_harness_with_max_one_is_busy(self):
        fixtures.bind_lane("KEY-1", self.project, lane=1, harness=HARNESS)
        fixtures.bind_lane("OPS-2", self.project, lane=2, harness="other")
        before = lanes.read()
        message = refusal(self, "BUSY", lane_launch.start, LINK, harness_name=HARNESS,
                          exec_=False)
        self.assertIn("harness %s runs 1 of 1 lanes" % HARNESS, message)
        self.assertIn("KEY-1", message)
        self.assertEqual(lanes.read(), before)
        self.assertFalse(self.branch_exists())
        self.assert_nothing_made()

    def test_a_failed_exec_releases_the_slot_and_removes_what_it_made(self):
        missing = str(self.base / "no-such-dir" / "claude")
        self.configure(missing)
        cwd = os.getcwd()
        message = refusal(self, "SETUP_INCOMPLETE", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=True)
        self.assertIn(missing, message)
        self.assertEqual(os.getcwd(), cwd)
        self.assertEqual(lanes.read(), [])
        self.assertFalse(self.branch_exists())
        self.assert_nothing_made()
        self.assertFalse(self.stub_out.exists())

    def test_a_render_refusal_after_the_claim_undoes_the_lane_too(self):
        os.unlink(str(self.base / "home" / ".claude.json"))
        message = refusal(self, "SETUP_INCOMPLETE", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn("claude mcp add --scope user", message)
        self.assertEqual(lanes.read(), [])
        self.assertFalse(self.branch_exists())
        self.assert_nothing_made()

    def test_a_legacy_active_run_blocks_start_until_finished(self):
        state = {"task_id": "OLD-1", "stage": "implementation", "mr": {"merged_sha": None}}
        store.write_json(run.state_path("OLD-1"), state)
        paths.legacy_active_file().write_text("OLD-1\n", encoding="utf-8")
        message = refusal(self, "OWNER_DECISION", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn("OLD-1", message)
        self.assertIn("ppl destroy --scope local --task OLD-1", message)
        self.assertEqual(lanes.read(), [])
        self.assert_nothing_made()
        state["mr"]["merged_sha"] = "a" * 40
        store.write_json(run.state_path("OLD-1"), state)
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertIsNotNone(lanes.entry(TASK))

    def test_a_legacy_active_run_blocks_start_even_when_its_task_holds_a_lane(self):
        # nothing writes runs/ACTIVE now, so whatever it names is a run from before lanes
        fixtures.bind_lane("OLD-2", self.project, self.base / "old-tree", harness="other")
        store.write_json(run.state_path("OLD-2"), {"task_id": "OLD-2", "stage": "review",
                                                   "mr": {"merged_sha": None}})
        paths.legacy_active_file().write_text("OLD-2\n", encoding="utf-8")
        message = refusal(self, "OWNER_DECISION", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn("OLD-2", message)
        self.assertIsNone(lanes.entry(TASK))
        run.destroy("OLD-2", "local")
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertIsNotNone(lanes.entry(TASK))

    def test_the_claude_process_gets_ppl_lane_and_ppl_task(self):
        fixtures.bind_lane("OPS-1", self.project, lane=1, harness="other")
        done = self.ppl("lane", LINK, "--harness", HARNESS, PPL_INHERITED="kept")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((done.stdout, done.stderr), ("", ""))
        claude = self.stub_call()
        entry = lanes.entry(TASK)
        self.assertEqual(claude["env"]["PPL_LANE"], "2")
        self.assertEqual(claude["env"]["PPL_TASK"], TASK)
        self.assertEqual(claude["env"]["PPL_HOME"], os.environ["PPL_HOME"])
        self.assertEqual(claude["env"]["PPL_INHERITED"], "kept")
        self.assertEqual(os.path.realpath(claude["cwd"]), os.path.realpath(entry["start_dir"]))
        self.assertEqual(claude["argv"], self.launch_line(entry)[1:])
        self.assertEqual(entry["pid"], claude["pid"])

    def test_an_existing_branch_is_refused_naming_branch_delete(self):
        git(self.project, "branch", BRANCH)
        message = refusal(self, "ILLEGAL_TRANSITION", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn("git -C %s branch -D %s" % (self.project, BRANCH), message)
        self.assertTrue(self.branch_exists())
        self.assertEqual(lanes.read(), [])
        self.assert_nothing_made()

    def test_a_start_directory_left_by_an_earlier_lane_is_refused_and_kept(self):
        left = lanes.start_dir(TASK)
        left.mkdir(parents=True)
        (left / "settings.json").write_text("{}\n", encoding="utf-8")
        message = refusal(self, "ILLEGAL_TRANSITION", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn(str(left), message)
        self.assertTrue((left / "settings.json").is_file())
        self.assertEqual(lanes.read(), [])
        self.assertFalse(self.branch_exists())
        self.assertFalse(self.worktree.exists())
        self.assertEqual(self.staging_dirs(), [])

    def test_a_task_whose_lane_runs_is_refused_and_its_lane_kept(self):
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        before = lanes.read()
        message = refusal(self, "ILLEGAL_TRANSITION", lane_launch.start, LINK,
                          harness_name=HARNESS, slug="again", exec_=False)
        self.assertIn("already has a lane", message)
        self.assertEqual(lanes.read(), before)
        self.assertTrue((lanes.start_dir(TASK) / "settings.json").is_file())
        self.assertTrue(self.worktree.is_dir())
        self.assertTrue(self.branch_exists())
        self.assertEqual(self.staging_dirs(), [])


class TestTheLaneWriteScope(LaneCase):
    """R44: under the lane's OS sandbox profile, sandboxed Bash writes only its own run under
    PPL_HOME's runs/ and scratch/. Every path the in-lane ppl CLI writes on a normal flow lies
    inside the rendered allowWrite and outside its denyWrite, and another lane's run does not."""

    def setUp(self):
        super().setUp()
        self.install(HARNESS, os_sandbox={"enabled": True})
        self.enterContext(mock.patch.object(os_sandbox, "pins",
                                            lambda which=None: fixtures.FAKE_PINS))
        self.enterContext(mock.patch.dict(os.environ, {
            "PPL_LOCAL_SETTINGS_DIR": tmphome.mkdtemp(prefix="ppllocalsettings-")}))
        for step in setup.STEPS:
            config.mark_step(step, "done")
        self.written = set()

    def snapshot(self):
        """{path: (type, size, mtime)} of everything under PPL_HOME, links not followed."""
        found = {}
        for root, dirs, files in os.walk(str(paths.home())):
            for name in [root] + [os.path.join(root, n) for n in dirs + files]:
                info = os.lstat(name)
                found[name] = (stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns)
        return found

    def ppl(self, *args):
        """`ppl <args>` as the lane's session runs it, every path it writes under PPL_HOME kept."""
        before = self.snapshot()
        with mock.patch.dict(os.environ, {"PPL_TASK": TASK}):
            code, out, err = call(*args)
        self.assertEqual(code, 0, "ppl %s: %s" % (" ".join(args), err))
        after = self.snapshot()
        self.written |= {path for path in set(before) | set(after)
                         if before.get(path) != after.get(path)}
        return out

    def document(self, name, doc):
        """A file the orchestrator writes in the run's scratch directory."""
        path = os_sandbox.scratch_dir(TASK) / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def generation(self):
        return str(json.loads(self.ppl("show", "--json"))["lease_generation"])

    def test_the_in_lane_cli_writes_only_inside_the_rendered_allow_write(self):
        # rule: R-LANE-CROSS-WRITE
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        fixtures.bind_lane("OPS-2", self.project, self.base / "other-tree", lane=2,
                           harness="other")
        run.init("OPS-2", "u", str(self.base / "other-tree"))
        self.ppl("init", TASK, "--tracker-url", LINK, "--skip-model-check", "--no-os-sandbox")
        self.ppl("recover")
        self.ppl("stage", "enter", "baseline")
        head = git(self.worktree, "rev-parse", "HEAD")
        self.ppl("baseline", "record", self.document("baseline.json", {"base_sha": head}))
        self.ppl("stage", "enter", "briefing")
        brief = {"task_id": TASK, "purpose": "p", "success_outcome": "s",
                 "governing_requirements": [], "acceptance": ["AC-1"],
                 "allowed_write_paths": ["src/**"], "forbidden_paths": [],
                 "verification": ["focused-tests"], "open_decisions": [],
                 "name_contract": {"Foo.bar": "int"}}
        self.ppl("brief", "set", self.document("brief.json", brief), "--from", "ppl-briefer")
        revision = json.loads(self.ppl("show", "--json"))["spec_revision"]
        challenge = {"task_id": TASK, "spec_revision": revision, "verdict": "ACCEPTED",
                     "new_classes": [], "wire_shapes": [], "open_decisions": [],
                     "producer": {"role": "brief-challenger"}}
        self.ppl("brief", "challenge", self.document("challenge.json", challenge),
                 "--generation", self.generation(), "--from", "ppl-brief-challenger")
        for stage in ("implementation", "tests", "gates"):
            self.ppl("stage", "enter", stage)
        log, rc = self.document("tests.log", "ok"), self.document("tests.rc", 0)
        self.ppl("evidence", "add", "--check", "focused-tests", "--result", "pass", "--log", log,
                 "--rc", rc, "--generation", self.generation(), "--from", "orchestrator")
        self.ppl("stage", "enter", "verification")
        verdict = {"task_id": TASK, "candidate_sha": head, "spec_revision": revision,
                   "verdict": "PASS", "acceptance_evidence": {"AC-1": "evidence:focused-tests"},
                   "findings": [], "producer": {"role": "verifier"}}
        self.ppl("verdict", "record", self.document("verdict.json", verdict),
                 "--generation", self.generation(), "--from", "ppl-verifier")
        self.ppl("stage", "enter", "mr")
        self.ppl("mr", "set", "--iid", "1", "--url", "https://forge.example/mr/1")
        self.ppl("stage", "enter", "review")
        self.ppl("review", "answered", "--thread", "t1", "--sha", head, "--classification",
                 "NEEDS_REPLY_ONLY", "--generation", self.generation())
        self.ppl("review", "approved", "--sha", head)
        self.ppl("park", "--blocker", "OWNER_DECISION", "--safe-next", "ask the owner")

        settings = json.loads((lanes.start_dir(TASK) / "settings.json").read_text(
            encoding="utf-8"))
        filesystem = settings["sandbox"]["filesystem"]

        def under(path, entries):
            real = os.path.realpath(path)
            return any(real == os.path.realpath(entry)
                       or real.startswith(os.path.realpath(entry).rstrip(os.sep) + os.sep)
                       for entry in entries)

        self.assertTrue(any(under(path, [paths.run_dir(TASK)]) for path in self.written))
        self.assertEqual(sorted(path for path in self.written
                                if not under(path, filesystem["allowWrite"])), [])
        self.assertEqual(sorted(path for path in self.written
                                if under(path, filesystem["denyWrite"])), [])
        for other in (paths.run_dir("OPS-2"), paths.runs_dir() / "_archive",
                      paths.runs_dir(), os_sandbox.scratch_dir("OPS-2"),
                      lanes.registry_path()):
            self.assertFalse(under(other, filesystem["allowWrite"]), other)


class TestCheckoutGit(LaneCase):
    """ppl lane runs git in the project checkout outside any sandbox, where a sandboxed role
    may have written the config or the hooks."""

    def test_a_checkout_config_key_that_could_run_a_command_is_refused(self):
        # rule: R-LANE-LAUNCH-GIT-CONFIG
        # both would run at ppl lane's fetch from the local origin and its worktree checkout
        script, marker = self.planted("upload-pack")
        git(self.project, "config", "remote.origin.uploadpack", str(script))
        git(self.project, "config", "filter.x.smudge", str(script))
        (self.project / ".git" / "info").mkdir(exist_ok=True)
        (self.project / ".git" / "info" / "attributes").write_text("* filter=x\n",
                                                                   encoding="utf-8")
        message = refusal(self, "OWNER_DECISION", lane_launch.start, LINK,
                          harness_name=HARNESS, exec_=False)
        self.assertIn("remote.origin.uploadpack (local)", message)
        self.assertIn("filter.x.smudge (local)", message)
        self.assertIn("git -C %s config --list --show-scope" % self.project, message)
        self.assertFalse(marker.exists())
        self.assertEqual(lanes.read(), [])
        self.assertFalse(self.branch_exists())
        self.assert_nothing_made()

    def test_a_url_rewrite_an_ssh_command_or_an_include_is_refused(self):
        script, marker = self.planted("ssh")
        # git lowers the section and the name of a key, never its subsection
        for key, named, value in (
                ("url.%s.insteadOf" % self.origin, "url.%s.insteadof" % self.origin,
                 str(self.base / "elsewhere.git")),
                ("core.sshCommand", "core.sshcommand", str(script)),
                ("include.path", "include.path", "/dev/null"),
                ("filter.x.smudge", "filter.x.smudge", str(script))):
            with self.subTest(key=key):
                git(self.project, "config", key, value)
                message = refusal(self, "OWNER_DECISION", lane_launch.start, LINK,
                                  harness_name=HARNESS, exec_=False)
                self.assertIn(named + " (local)", message)
                git(self.project, "config", "--unset", key)
                self.assertEqual(lanes.read(), [])
                self.assert_nothing_made()
        self.assertFalse(marker.exists())

    def test_a_hooks_path_in_the_checkout_config_is_pinned_off(self):
        script, marker = self.planted("post-checkout")
        git(self.project, "config", "core.hooksPath", str(script.parent))
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertTrue(self.worktree.is_dir())
        self.assertFalse(marker.exists())

    def test_a_hook_of_the_checkout_does_not_run(self):
        hooks = self.project / ".git" / "hooks"
        for name in ("post-checkout", "reference-transaction"):
            script, marker = self.planted(name)
            (hooks / name).write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
            (hooks / name).chmod(0o755)
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertTrue(self.worktree.is_dir())
        self.assertFalse((self.base / "post-checkout.ran").exists())
        self.assertFalse((self.base / "reference-transaction.ran").exists())

    def test_a_submodule_of_the_checkout_runs_nothing_of_its_own_config(self):
        # rule: R-LANE-LAUNCH-GIT-CONFIG
        # the checkout's own config passes the check, but a fetch that recursed into the
        # submodule would run the upload-pack its config names, which no check reads
        script, marker = self.planted("submodule-upload-pack")
        # the operator's own global config asks for recursion, which the lane's flag and pin
        # must override
        with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as config:
            config.write("[submodule]\n\trecurse = true\n[fetch]\n\trecurseSubmodules = on-demand\n")
        upstream = self.make_repo(self.base / "upstream")
        first = git(upstream, "rev-parse", "HEAD")
        modules = '[submodule "sub"]\n\tpath = sub\n\turl = %s\n' % upstream
        # what a sandboxed role can write in the checkout: .gitmodules, a gitlink in the
        # index and a submodule repository whose own config names the program
        (self.project / ".gitmodules").write_text(modules, encoding="utf-8")
        git(self.project, "update-index", "--add", "--cacheinfo", "160000,%s,sub" % first)
        subprocess.run(["git", "clone", "-q", str(upstream), str(self.project / "sub")],
                       check=True)
        git(self.project / "sub", "config", "remote.origin.uploadpack", str(script))
        # then origin's main moves the gitlink to a commit the submodule does not have yet,
        # which an on-demand fetch would go and get
        (upstream / "two.txt").write_text("two\n", encoding="utf-8")
        git(upstream, "add", "-A")
        git(upstream, "commit", "-q", "-m", "second")
        other = self.base / "other"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(other)], check=True)
        (other / ".gitmodules").write_text(modules, encoding="utf-8")
        git(other, "add", ".gitmodules")
        git(other, "update-index", "--add", "--cacheinfo",
            "160000,%s,sub" % git(upstream, "rev-parse", "HEAD"))
        git(other, "commit", "-q", "-m", "bump sub")
        git(other, "push", "-q", "origin", "main")
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), git(other, "rev-parse", "HEAD"))
        self.assertFalse(marker.exists())

    def test_a_remapped_fetch_refspec_cannot_choose_the_base(self):
        # a role sends origin's main elsewhere and points origin/main at its own commit
        planted = self.base / "planted.txt"
        (self.project / "planted.txt").write_text("planted\n", encoding="utf-8")
        git(self.project, "add", "planted.txt")
        git(self.project, "commit", "-q", "-m", "planted")
        git(self.project, "update-ref", "refs/remotes/origin/main", "HEAD")
        git(self.project, "config", "remote.origin.fetch",
            "+refs/heads/main:refs/remotes/origin/elsewhere")
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"),
                         git(self.origin, "rev-parse", "main"))
        self.assertFalse((self.worktree / planted.name).exists())

    def plant_ambiguous_refs(self, repo, *refs):
        """plant_side_commit, with the operator's global config turning git's ambiguous-ref
        warning off."""
        with open(os.environ["GIT_CONFIG_GLOBAL"], "a", encoding="utf-8") as config:
            config.write("[core]\n\twarnAmbiguousRefs = false\n")
        self.plant_side_commit(repo, *refs)

    def plant_side_commit(self, repo, *refs):
        """A commit holding planted.txt, on a side branch of `repo`, with `refs` pointed at it."""
        git(repo, "checkout", "-q", "-b", "side")
        (repo / "planted.txt").write_text("planted\n", encoding="utf-8")
        git(repo, "add", "planted.txt")
        git(repo, "commit", "-q", "-m", "planted")
        git(repo, "checkout", "-q", "main")
        for ref in refs:
            git(repo, "update-ref", ref, "side")

    def test_a_ref_planted_under_the_base_name_cannot_choose_the_base(self):
        # rule: R-LANE-LAUNCH-GIT-CONFIG
        # the short name origin/main reads refs/tags/origin/main and refs/heads/origin/main
        # before refs/remotes/origin/main
        self.plant_ambiguous_refs(self.project, "refs/heads/origin/main",
                                  "refs/tags/origin/main")
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"),
                         git(self.origin, "rev-parse", "main"))
        self.assertFalse((self.worktree / "planted.txt").exists())

    def test_without_an_origin_a_tag_named_like_the_default_branch_cannot_choose_the_base(self):
        # the short name main reads refs/tags/main before refs/heads/main
        local = self.make_repo(self.base / "local")
        self.install(HARNESS, project=local)
        self.plant_ambiguous_refs(local, "refs/tags/main")
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        worktree = self.worktrees / ("local-" + TASK)
        self.assertEqual(git(worktree, "rev-parse", "HEAD"),
                         git(local, "rev-parse", "refs/heads/main"))
        self.assertFalse((worktree / "planted.txt").exists())

    def test_a_replace_ref_for_the_base_cannot_change_what_is_checked_out(self):
        # rule: R-LANE-LAUNCH-GIT-CONFIG
        # git reads an object through refs/replace/<its sha> unless told not to: HEAD would
        # still name the fetched tip while the worktree held the replacement's tree
        tip = git(self.origin, "rev-parse", "main")
        self.plant_side_commit(self.project, "refs/replace/" + tip)
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.assertEqual(git(self.worktree, "rev-parse", "HEAD"), tip)
        self.assertFalse((self.worktree / "planted.txt").exists())

    def test_on_a_github_forge_origin_must_be_the_forge_repo(self):
        # rule: R-LANE-LAUNCH-GIT-CONFIG
        github = {"forge": {"kind": "github"}, "tracker": fixtures.GITHUB_ISSUES["tracker"],
                  "task": fixtures.GITHUB_ISSUES["task"]}
        self.install("gh-lane", **github)
        message = refusal(self, "OWNER_DECISION", lane_launch.start,
                          "https://github.com/o/r/issues/7", harness_name="gh-lane", exec_=False)
        self.assertIn("not the forge repo o/r on github.com", message)
        self.assertIn("remote set-url origin", message)
        self.assertEqual(lanes.read(), [])
        self.assertEqual(self.staging_dirs(), [])
        self.assertFalse((self.worktrees / "shop-GH-7").exists())
        doc = harness.load(paths.harness_dir("gh-lane"))
        for urls in (["https://github.com/o/r.git"], ["git@github.com:o/r.git"]):
            git(self.project, "remote", "set-url", "origin", urls[0])
            self.assertIsNone(lane_launch._check_config(doc, str(self.project)))
        git(self.project, "config", "--add", "remote.origin.url", "https://github.com/o/r")
        with self.assertRaises(PplError) as caught:
            lane_launch._check_config(doc, str(self.project))
        self.assertIn("2 urls", caught.exception.message)
        # with no tracker repo the forge repo is read from origin, which must be on github.com
        self.install("gh-jira", forge={"kind": "github"})
        git(self.project, "config", "--unset-all", "remote.origin.url")
        git(self.project, "remote", "set-url", "--add", "origin", str(self.origin))
        message = refusal(self, "OWNER_DECISION", lane_launch.start, LINK,
                          harness_name="gh-jira", exec_=False)
        self.assertIn("names no github.com repository", message)


class TestResume(LaneCase):
    def test_resume_uses_the_same_session_id_and_flags(self):
        started = lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        before = lanes.entry(TASK)
        done = self.ppl("lane", "resume", TASK)
        self.assertEqual(done.returncode, 0, done.stderr)
        claude = self.stub_call()
        expected = list(started["argv"][:-1])
        expected[expected.index("--session-id")] = "--resume"
        self.assertEqual(claude["argv"], expected[1:])
        self.assertEqual(claude["argv"], self.launch_line(before, resume=True)[1:])
        self.assertEqual(claude["argv"][claude["argv"].index("--resume") + 1],
                         before["session_id"])
        self.assertEqual(claude["argv"][-2:], ["-n", TASK])
        self.assertEqual(claude["env"]["PPL_TASK"], TASK)
        after = lanes.read()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["pid"], claude["pid"])
        self.assertEqual(dict(after[0], pid=None), dict(before, pid=None))

    def test_resume_without_a_lane_is_refused(self):
        message = refusal(self, "SCHEMA_INVALID", lane_launch.resume, TASK, exec_=False)
        self.assertIn("ppl lane list", message)


class TestList(LaneCase):
    def test_list_shows_resume_lines_and_whether_the_window_is_alive(self):
        for lane, task in enumerate(("KEY-1", "KEY-2", "KEY-3"), 1):
            fixtures.bind_lane(task, self.project, lane=lane, harness=HARNESS)
        lanes.set_pid("KEY-1", os.getpid())
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        lanes.set_pid("KEY-2", gone.pid)
        store.write_json(run.state_path("KEY-1"), {"task_id": "KEY-1", "stage": "implementation",
                                                   "last_durable_fact": "brief accepted"})
        code, out, err = call("lane", "list", "--json")
        self.assertEqual(code, 0, err)
        rows = {row["task"]: row for row in json.loads(out)}
        self.assertEqual({task: row["alive"] for task, row in rows.items()},
                         {"KEY-1": True, "KEY-2": False, "KEY-3": False})
        self.assertEqual((rows["KEY-1"]["stage"], rows["KEY-1"]["outcome"]),
                         ("implementation", "brief accepted"))
        self.assertEqual((rows["KEY-2"]["stage"], rows["KEY-2"]["outcome"]), (None, None))
        for task, row in rows.items():
            self.assertEqual(row["resume"], "ppl lane resume %s" % task)
        code, out, err = call("lane", "list")
        self.assertEqual(code, 0, err)
        lines = {task: next(line for line in out.splitlines() if " %s " % task in line)
                 for task in rows}
        for task, line in lines.items():
            self.assertIn("ppl lane resume %s" % task, line)
        self.assertIn("open", lines["KEY-1"])
        self.assertIn("closed", lines["KEY-2"])
        with mock.patch.object(os, "kill", side_effect=PermissionError):
            alive = {row["task"]: row["alive"] for row in lane_launch.listing()}
        self.assertEqual(alive, {"KEY-1": True, "KEY-2": True, "KEY-3": False})

    def test_list_with_no_lanes_says_so(self):
        code, out, err = call("lane", "list")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("no lanes", out)


class TestClose(LaneCase):
    def merged(self, task=TASK, sha="b" * 40):
        store.write_json(run.state_path(task), {"task_id": task, "stage": "merge_watch",
                                                "mr": {"merged_sha": sha}})

    def test_close_before_the_merge_is_refused(self):
        fixtures.bind_lane(TASK, self.project)
        message = refusal(self, "ILLEGAL_TRANSITION", lane_launch.close, TASK)
        self.assertIn("merge", message)
        self.merged(sha=None)
        refusal(self, "ILLEGAL_TRANSITION", lane_launch.close, TASK)
        self.assertIsNotNone(lanes.entry(TASK))

    def test_close_with_a_pending_reflection_is_refused(self):
        fixtures.bind_lane(TASK, self.project)
        self.merged()
        store.write_json(lessons.pending_path(TASK), {"task": TASK, "bundle": "/x/lessons/bundle"})
        message = refusal(self, "ILLEGAL_TRANSITION", lane_launch.close, TASK)
        self.assertIn("reflection", message)
        self.assertIsNotNone(lanes.entry(TASK))

    def test_close_after_the_merge_frees_the_slot_and_keeps_the_files(self):
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.merged()
        with mock.patch.dict(os.environ, {"PPL_TASK": TASK}):
            code, out, err = call("lane", "close")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["task"], TASK)
        self.assertEqual(lanes.read(), [])
        self.assertTrue((lanes.start_dir(TASK) / "settings.json").is_file())
        self.assertTrue(self.worktree.is_dir())
        self.assertTrue(self.branch_exists())
        refusal(self, "SCHEMA_INVALID", lane_launch.close, TASK)

    def test_close_takes_its_task_as_every_bare_command_does(self):
        lane_launch.start(LINK, harness_name=HARNESS, exec_=False)
        self.merged()
        with mock.patch.dict(os.environ, {"PPL_TASK": TASK}):
            code, _, err = call("lane", "close", "--task", "OPS-9")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("PPL_TASK", json.loads(err)["message"])
        self.assertIsNotNone(lanes.entry(TASK))
        # no PPL_TASK: the lane whose start directory holds the cwd
        with contextlib.chdir(lanes.start_dir(TASK)):
            code, out, err = call("lane", "close")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["task"], TASK)
        self.assertEqual(lanes.read(), [])


if __name__ == "__main__":
    unittest.main()

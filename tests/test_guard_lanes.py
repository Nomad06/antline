"""The hooks know their run from the `--task <id>` their own command line carries, rendered
into the lane's write-protected settings.json, never from a shared pointer."""
import json
import os
import subprocess
import sys
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
from ppl import control, escape_paths, harness, lanes, os_sandbox, paths, run, store
import ppl_dispatch
import ppl_guard
import ppl_usage

GUARD = HOOKS / "ppl_guard.py"
HOOK_BOUND_SECONDS = 10
# what a role can leave where the registry belongs: past the hooks' 1 MiB cap, and too big
# to read within a hook's timeout
HUGE_BYTES = 2 * 1024 ** 3

LANE_A = "LANE-5101"
LANE_B = "LANE-5102"
UNREGISTERED = "LANE-5199"
ELSEWHERE = "/home/dev/some/other/project"
# the CI python-floor job's payload (.github/workflows/tests.yml)
FLOOR_PAYLOAD = {"hook_event_name": "PreToolUse", "agent_type": "ppl-code-worker",
                 "tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/tmp"}


def payload(agent, tool, tool_input, cwd="/tmp"):
    return {"hook_event_name": "PreToolUse", "agent_type": agent,
            "tool_name": tool, "tool_input": tool_input, "cwd": cwd}


def argv(task):
    return ["--task", task]


def reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


class LaneCase(unittest.TestCase):
    """Each test in its own data root: a lane registry is machine-wide state."""

    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        self.addCleanup(self.restore_home)

    def restore_home(self):
        os.environ["PPL_HOME"] = self._home

    def deny_reason(self, result):
        self.assertIsNotNone(result, "the call was allowed")
        return reason(result)

    def lane(self, task, lane=1):
        """A run with a registered lane, its repo a fresh directory. Returns the repo."""
        repo = tmphome.mkdtemp(prefix="pplrepo-")
        run.init(task, "u", repo)
        fixtures.bind_lane(task, repo, lane=lane)
        return repo


class TestTheHookBinding(LaneCase):
    def test_a_role_without_a_lane_is_denied(self):
        # rule: R-LANE-ROLE-OUTSIDE
        # the old shared pointer names a live run, and still arms nothing
        self.lane(LANE_A)
        self.assertEqual(run.active(), LANE_A)
        for agent in ("ppl-code-worker", "ppl-reflector", "ppl-not-generated-yet"):
            with self.subTest(agent=agent):
                result = ppl_guard.decide(payload(agent, "Bash", {"command": "ls"}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                self.assertIn("ppl lane", self.deny_reason(result))
        result = ppl_guard.decide(payload("ppl-verifier", "Read", {"file_path": "/tmp/x"}))
        self.assertIn("ppl roles run only inside a lane session", self.deny_reason(result))

    def test_an_unroled_session_without_a_lane_gets_only_the_global_denials(self):
        # rule: R-LANE-ROLE-OUTSIDE
        repo = self.lane(LANE_A)
        control.set_command(LANE_A, "pause")
        # nothing run-scoped: not the run's pause, not its repo write boundary
        self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": "ls"}, repo)))
        self.assertIsNone(ppl_guard.decide(payload(None, "Write",
                                                   {"file_path": repo + "/src/App.java"})))
        self.assertIsNone(ppl_guard.decide(payload("general-purpose", "Bash",
                                                   {"command": "ls"})))
        # the global denials stay
        for call in (payload(None, "Bash", {"command": "git add -A"}),
                     payload(None, "Bash", {"command": "git push --force origin x"}),
                     payload(None, "mcp__GitLab-MCP__merge_merge_request", {})):
            with self.subTest(tool=call["tool_name"]):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(ppl_guard.decide(call)))

    def test_an_unregistered_lane_denies_roles_and_unroled_writes_but_allows_reads(self):
        # rule: R-LANE-HOOK-BINDING
        repo = self.lane(LANE_A)
        run.init(UNREGISTERED, "u", repo)
        hook = argv(UNREGISTERED)
        for agent in ("ppl-code-worker", "ppl-verifier"):
            with self.subTest(agent=agent):
                result = ppl_guard.decide(payload(agent, "Read", {"file_path": repo + "/a"}),
                                          hook)
                self.assertIn("OWNER_DECISION", self.deny_reason(result))
                self.assertIn("lane %s is not registered: run ppl lane resume %s"
                              % (UNREGISTERED, UNREGISTERED), self.deny_reason(result))
        for agent in (None, "general-purpose"):
            for call in (payload(agent, "Bash", {"command": "ls"}),
                         payload(agent, "Write", {"file_path": "/tmp/notes.txt"}),
                         payload(agent, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"}),
                         payload(agent, "Bash", {"command": "ls",
                                                 "dangerouslyDisableSandbox": True})):
                with self.subTest(agent=agent, tool=call["tool_name"]):
                    self.assertIn("OWNER_DECISION",
                                  self.deny_reason(ppl_guard.decide(call, hook)))
            for call in (payload(agent, "Read", {"file_path": repo + "/a"}),
                         payload(agent, "Glob", {"pattern": "**/*.java"}),
                         payload(agent, "Grep", {"pattern": "x"})):
                with self.subTest(agent=agent, tool=call["tool_name"]):
                    self.assertIsNone(ppl_guard.decide(call, hook))
        # a malformed command line binds no lane and fails closed the same way
        for bad in (["--task"], ["--task", "lane-1"], ["--task", LANE_A, "--task", LANE_A],
                    ["--task", LANE_A + "\n"], ["-q"], ["--task=" + LANE_A]):
            with self.subTest(argv=bad):
                self.assertIn("OWNER_DECISION", self.deny_reason(ppl_guard.decide(
                    payload("ppl-code-worker", "Read", {"file_path": repo + "/a"}), bad)))
                self.assertIn("OWNER_DECISION", self.deny_reason(ppl_guard.decide(
                    payload(None, "Write", {"file_path": "/tmp/notes.txt"}), bad)))
                self.assertIsNone(ppl_guard.decide(
                    payload(None, "Read", {"file_path": repo + "/a"}), bad))

    def test_a_bound_session_scopes_every_call_to_its_run(self):
        # rule: R-LANE-HOOK-BINDING
        # rule: R-UNROLED-REPO-WRITE
        repo = self.lane(LANE_A)
        # the old shared pointer names another run, and changes nothing for lane A
        self.lane(LANE_B, lane=2)
        self.assertEqual(run.active(), LANE_B)
        start = str(lanes.start_dir(LANE_A))
        for cwd in (repo, start, ELSEWHERE):
            with self.subTest(cwd=cwd):
                result = ppl_guard.decide(payload(None, "Write",
                                                  {"file_path": repo + "/src/App.java"}, cwd),
                                          argv(LANE_A))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                result = ppl_guard.decide(payload(None, "Bash",
                                                  {"command": "echo x > %s/App.java" % repo},
                                                  cwd), argv(LANE_A))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        # the operator's pause reaches the bound session from any cwd
        control.set_command(LANE_A, "pause")
        for cwd in (start, ELSEWHERE):
            with self.subTest(paused=cwd):
                result = ppl_guard.decide(payload(None, "Bash", {"command": "ls"}, cwd),
                                          argv(LANE_A))
                self.assertIn("OWNER_DECISION", self.deny_reason(result))
        # an unroled write outside the run's repo is still the session's own business
        control.clear(LANE_A)
        self.assertIsNone(ppl_guard.decide(
            payload(None, "Write", {"file_path": "/tmp/somewhere-else/notes.txt"}, start),
            argv(LANE_A)))

    def test_before_ppl_init_an_unroled_write_into_the_lane_is_denied(self):
        # rule: R-UNROLED-REPO-WRITE
        # R24: ppl lane registered the lane and made its worktree, ppl init has not run, so
        # there is no state.json to name the run's repo. The registry entry's worktree and
        # repo stand in for it, and an unreadable state.json changes nothing.
        repo = tmphome.mkdtemp(prefix="pplrepo-")
        worktree = tmphome.mkdtemp(prefix="pplworktree-")
        fixtures.bind_lane(LANE_A, repo, worktree=worktree)
        hook = argv(LANE_A)
        start = str(lanes.start_dir(LANE_A))
        for state in (None, "{not json"):
            if state is not None:
                run.state_path(LANE_A).parent.mkdir(parents=True, exist_ok=True)
                run.state_path(LANE_A).write_text(state, encoding="utf-8")
            for root in (worktree, repo):
                for call in (payload(None, "Write", {"file_path": root + "/src/App.java"}, start),
                             payload(None, "Edit", {"file_path": root + "/src/App.java",
                                                    "old_string": "a", "new_string": "b"}),
                             payload(None, "Bash", {"command": "echo x > %s/App.java" % root},
                                     ELSEWHERE),
                             payload("general-purpose", "Write", {"file_path": root + "/x"})):
                    with self.subTest(state=state, root=root, tool=call["tool_name"],
                                      agent=call["agent_type"]):
                        result = ppl_guard.decide(call, hook)
                        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                        self.assertIn(LANE_A, self.deny_reason(result))
            # what lands outside the lane stays the session's business
            self.assertIsNone(ppl_guard.decide(
                payload(None, "Write", {"file_path": "/tmp/somewhere-else/notes.txt"}), hook))
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": "ls"}), hook))
            # a target the hook cannot resolve is denied, as everywhere in a lane
            result = ppl_guard.decide(payload(None, "Bash", {"command": "echo x > $W/App.java"}),
                                      hook)
            self.assertIn("could not be resolved", self.deny_reason(result))
            # and a role is denied every call before ppl init, write or not (R22)
            result = ppl_guard.decide(payload("ppl-code-worker", "Read",
                                              {"file_path": worktree + "/src/App.java"}), hook)
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        # an entry that names no worktree or repo places no write, so every write is denied
        entry = {key: item for key, item in lanes.entry(LANE_A).items()
                 if key not in ("worktree", "repo")}
        store.write_json(lanes.registry_path(), {"version": 1, "lanes": [entry]})
        for call in (payload(None, "Write", {"file_path": "/tmp/somewhere-else/notes.txt"}),
                     payload(None, "Bash", {"command": "echo x > /tmp/notes.txt"})):
            with self.subTest(entry="no roots", tool=call["tool_name"]):
                self.assertIn("OWNER_DECISION",
                              self.deny_reason(ppl_guard.decide(call, hook)))
        self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": "ls"}), hook))

    def test_two_lanes_never_see_each_others_state(self):
        # rule: R-LANE-HOOK-BINDING
        repo_a = self.lane(LANE_A, lane=1)
        repo_b = self.lane(LANE_B, lane=2)
        control.set_command(LANE_A, "pause")
        for agent in (None, "ppl-code-worker"):
            with self.subTest(agent=agent):
                result = ppl_guard.decide(payload(agent, "Bash", {"command": "ls"}),
                                          argv(LANE_A))
                self.assertIn("OWNER_DECISION", self.deny_reason(result))
                self.assertIn(LANE_A, self.deny_reason(result))
                self.assertIsNone(ppl_guard.decide(payload(agent, "Bash", {"command": "ls"}),
                                                   argv(LANE_B)))
        # each lane's write boundary is its own run's repo
        control.clear(LANE_A)
        for task, repo in ((LANE_A, repo_a), (LANE_B, repo_b)):
            with self.subTest(write=task):
                result = ppl_guard.decide(payload(None, "Write",
                                                  {"file_path": repo + "/App.java"}),
                                          argv(task))
                self.assertIn("run %s's repo" % task, self.deny_reason(result))
        # a deny is logged to the bound run only
        def denied_tools(task):
            return [e["tool"] for e in store.read_log(task) if e.get("event") == "deny"]

        self.assertEqual(denied_tools(LANE_B), ["Write"])
        self.assertEqual(denied_tools(LANE_A), ["Bash", "Bash", "Write"])

    def test_an_unreadable_registry_fails_closed_without_blocking(self):
        # rule: R-LANE-HOOK-BINDING
        # Review Focus 2: the registry a hook reads with no lock is a FIFO, garbage, not
        # UTF-8 or over the cap. The hook answers within its bound, the run-scoped calls
        # fail closed and the global denials still fire. Its own data root, run as Claude
        # Code runs the hook.
        env = fixtures.isolated_env()
        registry = Path(env["PPL_HOME"]) / lanes.REGISTRY_FILE

        def fifo():
            os.mkfifo(str(registry))

        def huge():
            with open(str(registry), "wb") as handle:
                handle.truncate(HUGE_BYTES)

        def text(data):
            return lambda: registry.write_bytes(data)

        cases = (("fifo", fifo), ("truncated", text(b'{"version": 1, "lan')),
                 ("not json", text(b"garbage")), ("not utf-8", text(b"\xff")),
                 ("wrong shape", text(b'{"version": 1, "lanes": {}}')),
                 ("over the cap", huge))
        calls = ((payload("ppl-code-worker", "Read", {"file_path": "/tmp/x"}), "OWNER_DECISION"),
                 (payload(None, "Write", {"file_path": "/tmp/notes.txt"}), "OWNER_DECISION"),
                 (payload(None, "Bash", {"command": "ls", "dangerouslyDisableSandbox": True}),
                  "OWNER_DECISION"),
                 (payload(None, "Bash", {"command": "git push --force origin x"}),
                  "BOUNDARY_VIOLATION"),
                 (payload(None, "Read", {"file_path": "/tmp/x"}), None))
        for name, make in cases:
            make()
            try:
                for call, expected in calls:
                    with self.subTest(registry=name, agent=call["agent_type"],
                                      tool=call["tool_name"]):
                        result = self.run_hook(call, env, argv(LANE_A))
                        if expected is None:
                            self.assertIsNone(result)
                        else:
                            self.assertIn(expected, self.deny_reason(result))
                            if expected == "OWNER_DECISION":
                                self.assertIn("lanes.json", self.deny_reason(result))
            finally:
                registry.unlink()

    def run_hook(self, call, env, hook_argv):
        """main() in its own process, as Claude Code runs it, held to a bound."""
        started = time.monotonic()
        try:
            done = subprocess.run([sys.executable, str(GUARD)] + list(hook_argv),
                                  input=json.dumps(call), capture_output=True, text=True,
                                  env=env, timeout=HOOK_BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail("the hook was still running after %d s" % HOOK_BOUND_SECONDS)
        self.assertLess(time.monotonic() - started, HOOK_BOUND_SECONDS)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        return json.loads(done.stdout) if done.stdout.strip() else None

    def test_the_hooks_run_without_the_posix_only_modules(self):
        # rule: R-LANE-HOOK-BINDING
        # Windows has no fcntl, pwd, grp, termios or resource. A hook that dies importing one
        # exits 1 with no output, which Claude Code reads as allow. Only the registry's
        # writers lock (D1), so no hook imports fcntl, and the rest are imported guarded or
        # not at all. The shim blocks them the way a platform without them fails the import.
        env = fixtures.isolated_env()
        shim = ("import runpy, sys\n"
                "for name in ('fcntl', 'pwd', 'grp', 'termios', 'resource'):\n"
                "    sys.modules[name] = None\n"
                "sys.argv = sys.argv[1:]\n"
                "runpy.run_path(sys.argv[0], run_name='__main__')\n")
        push = payload("ppl-code-worker", "Bash", {"command": "git push --force origin x"})
        for hook_argv in ([], argv(LANE_A)):
            with self.subTest(argv=hook_argv):
                done = subprocess.run([sys.executable, "-c", shim, str(GUARD)] + hook_argv,
                                      input=json.dumps(push), capture_output=True, text=True,
                                      env=env, timeout=HOOK_BOUND_SECONDS)
                self.assertEqual((done.returncode, done.stderr), (0, ""))
                decision = json.loads(done.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")
                self.assertIn("BOUNDARY_VIOLATION", decision["permissionDecisionReason"])
        start = {"hook_event_name": "SubagentStart", "agent_type": "ppl-code-worker",
                 "cwd": "/tmp"}
        for hook in ("ppl_dispatch.py", "ppl_usage.py"):
            with self.subTest(hook=hook):
                done = subprocess.run([sys.executable, "-c", shim, str(HOOKS / hook)]
                                      + argv(LANE_A), input=json.dumps(start),
                                      capture_output=True, text=True, env=env,
                                      timeout=HOOK_BOUND_SECONDS)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))

    def test_the_guard_with_no_argv_still_runs_on_the_floor_job_payload(self):
        # D10: the python floor runs before argv is read, so the CI job's payload with no
        # argv gets a decision and exit 0 under a supported python too
        env = fixtures.isolated_env()
        result = self.run_hook(FLOOR_PAYLOAD, env, [])
        self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("ppl lane", self.deny_reason(result))
        for hook in ("ppl_dispatch.py", "ppl_usage.py"):
            with self.subTest(hook=hook):
                done = subprocess.run([sys.executable, str(HOOKS / hook)],
                                      input=json.dumps(FLOOR_PAYLOAD), capture_output=True,
                                      text=True, env=env, timeout=HOOK_BOUND_SECONDS)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        # and under an older python the floor still answers with a lane on the command line
        found = fixtures.older_system_python()
        if found is None:
            return
        for hook in ("ppl_guard.py", "ppl_dispatch.py", "ppl_usage.py"):
            with self.subTest(hook=hook, python=found[1]):
                done = subprocess.run([found[0], str(HOOKS / hook)] + argv(LANE_A),
                                      input=json.dumps(FLOOR_PAYLOAD), capture_output=True,
                                      text=True, env=env, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertIn("needs 3.11 or newer", done.stdout)
                self.assertIn("BOUNDARY_VIOLATION", done.stdout)


class TestTheOtherHooksBind(LaneCase):
    def transcript(self):
        path = Path(tmphome.mkdtemp(prefix="ppltranscript-")) / "agent-a1.jsonl"
        line = {"type": "assistant", "isSidechain": False, "timestamp": "2099-01-01T00:00:00Z",
                "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {
                    "input_tokens": 1, "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0, "output_tokens": 7}}}
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        return path

    def usage_events(self, task):
        return [e for e in store.read_log(task)
                if e.get("event") == "usage" and e.get("role") != "orchestrator"]

    def test_usage_is_recorded_in_the_bound_run(self):
        self.lane(LANE_A, lane=1)
        self.lane(LANE_B, lane=2)
        run.init(UNREGISTERED, "u", "/tmp/repo")
        self.assertEqual(run.active(), UNREGISTERED)
        stop = {"hook_event_name": "SubagentStop", "session_id": "s1",
                "transcript_path": str(self.transcript().parent / "missing.jsonl"),
                "agent_id": "a1", "agent_type": "ppl-code-worker", "cwd": "/tmp",
                "agent_transcript_path": str(self.transcript())}
        self.assertIsNone(ppl_usage.on_stop(stop, argv(LANE_A)))
        self.assertEqual([e["models"]["claude-opus-5-5"]["output"]
                          for e in self.usage_events(LANE_A)], [7])
        # no lane, a lane the registry does not hold, a malformed command line: nothing
        for hook in ([], argv(UNREGISTERED), ["--task"]):
            with self.subTest(argv=hook):
                self.assertIsNone(ppl_usage.on_stop(stop, hook))
        for task in (LANE_B, UNREGISTERED):
            self.assertEqual(self.usage_events(task), [])
        self.assertEqual(len(self.usage_events(LANE_A)), 1)

    def test_dispatch_counts_against_the_bound_run(self):
        self.lane(LANE_A, lane=1)
        self.lane(LANE_B, lane=2)
        run.init(UNREGISTERED, "u", "/tmp/repo")
        start = {"hook_event_name": "SubagentStart", "agent_type": "ppl-code-worker",
                 "cwd": "/tmp"}
        self.assertIsNone(ppl_dispatch.on_start(start, argv(LANE_B)))
        for hook in ([], argv(UNREGISTERED), ["--task", "x"]):
            with self.subTest(argv=hook):
                self.assertIsNone(ppl_dispatch.on_start(start, hook))
        used = {task: run.load(task)["budgets"]["dispatch_used"]
                for task in (LANE_A, LANE_B, UNREGISTERED)}
        self.assertEqual(used, {LANE_A: 0, LANE_B: 1, UNREGISTERED: 0})


def real(path):
    return os.path.realpath(str(path))


def edit_input(tool, target):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {key: target, "old_string": "a", "new_string": "b", "content": "x\n"}


class TestLaneProtection(LaneCase):
    """A lane's own session files, and the other lanes' work, are out of a lane's reach. Two
    lanes on one project checkout, each with its worktree under the checkout's .worktrees, as
    ppl lane lays them out by default, and each past ppl init."""

    def setUp(self):
        super().setUp()
        self.checkout = real(tmphome.mkdtemp(prefix="pplcheckout-"))
        os.makedirs(os.path.join(self.checkout, ".git"))
        self.worktree = {}
        for number, task in ((1, LANE_A), (2, LANE_B)):
            worktree = os.path.join(self.checkout, ".worktrees", "shop-" + task)
            os.makedirs(os.path.join(worktree, "src"))
            # as git worktree add leaves it: a .git file naming its git dir in the checkout
            git_dir = os.path.join(self.checkout, ".git", "worktrees", "shop-" + task)
            os.makedirs(git_dir)
            for name, text in (("gitdir", worktree + "/.git"), ("commondir", "../..")):
                Path(git_dir, name).write_text(text + "\n", encoding="utf-8")
            Path(worktree, ".git").write_text("gitdir: %s\n" % git_dir, encoding="utf-8")
            run.init(task, "u", worktree)
            fixtures.bind_lane(task, self.checkout, worktree=worktree, lane=number)
            self.worktree[task] = worktree

    def decide(self, agent, tool, tool_input, cwd=None, task=LANE_A):
        cwd = str(lanes.start_dir(task)) if cwd is None else cwd
        return ppl_guard.decide(payload(agent, tool, tool_input, cwd), argv(task))

    def bash(self, command, agent=None, cwd=None, task=LANE_A):
        return self.decide(agent, "Bash", {"command": command}, cwd, task)

    def assert_lane_file(self, result):
        """Denied as a lane's file: by the lane files check, or, for an edit tool in a lane, by
        the protected paths that hold with or without facts (R28)."""
        reason = self.deny_reason(result)
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertTrue("ppl lane keeps" in reason or escape_paths.PROTECTED_WHY in reason,
                        reason)

    def test_edit_and_write_into_the_start_directory_are_denied_for_everyone(self):
        # rule: R-LANE-START-DIR-PROTECTED
        # R27: the harness needs no OS sandbox, so no facts file names the start directory,
        # and it is protected all the same: the lane's own, another lane's, and the registry
        own, other = lanes.start_dir(LANE_A), lanes.start_dir(LANE_B)
        self.assertFalse((own / os_sandbox.FACTS_FILE).exists())
        targets = [str(start / rel) for start in (own, other)
                   for rel in ("settings.json", "plugin/skills/pipeline/SKILL.md",
                               os_sandbox.FACTS_FILE)]
        targets.append(str(lanes.registry_path()))
        for agent in (None, "ppl-code-worker", "general-purpose"):
            for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                for target in targets:
                    with self.subTest(agent=agent, tool=tool, target=target):
                        self.assert_lane_file(self.decide(agent, tool, edit_input(tool, target)))
        # a write-shaped Bash too, relative to the start directory the session stands in, and
        # a removal that takes the start directories with it
        for command in ("echo x > settings.json", "echo x > %s/settings.json" % own,
                        "cp /tmp/x %s/mcp.json" % other, "sed -n p x && tee %s/agents.json"
                        % own, "rm -rf %s" % own.parent, "mv %s /tmp/gone" % other):
            for agent in (None, "ppl-code-worker"):
                with self.subTest(command=command, agent=agent):
                    result = self.bash(command, agent=agent)
                    self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                    self.assertIn("ppl lane keeps", self.deny_reason(result))
        # a session in no lane meets it as well, and one that changes nothing there is free
        result = ppl_guard.decide(payload(None, "Write", {"file_path": str(own / "settings.json")},
                                          str(own)))
        self.assertIn("ppl lane keeps", self.deny_reason(result))
        self.assertIsNone(self.bash("cat settings.json"))
        self.assertIsNone(self.decide(None, "Read", {"file_path": str(own / "settings.json")}))

    def test_a_write_into_another_lanes_worktree_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # a lane on another project too, whose worktree lies in no checkout of lane A's
        elsewhere = real(tmphome.mkdtemp(prefix="pplcheckout-"))
        theirs_c = os.path.join(elsewhere, ".worktrees", "web-LANE-5103")
        os.makedirs(theirs_c)
        run.init("LANE-5103", "u", theirs_c)
        fixtures.bind_lane("LANE-5103", elsewhere, worktree=theirs_c, lane=3)
        for owner, worktree in ((LANE_B, self.worktree[LANE_B]), ("LANE-5103", theirs_c)):
            target = worktree + "/src/App.java"
            calls = [(tool, edit_input(tool, target))
                     for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit")]
            calls += [("Bash", {"command": command}) for command in (
                "echo x > %s" % target, "cd %s && echo x > src/App.java" % worktree,
                "cp /tmp/App.java %s" % target, "git -C %s checkout HEAD -- src/App.java"
                % worktree, "rm -rf %s" % worktree)]
            for agent in (None, "ppl-code-worker", "general-purpose"):
                for tool, tool_input in calls:
                    with self.subTest(owner=owner, agent=agent, tool=tool,
                                      what=str(tool_input)[:60]):
                        reason = self.deny_reason(self.decide(agent, tool, tool_input))
                        self.assertIn("BOUNDARY_VIOLATION", reason)
                        self.assertIn("lane %s's worktree" % owner, reason)
        # lane B is held to the same, the other way round
        result = self.decide(None, "Write", edit_input("Write", self.worktree[LANE_A] + "/x"),
                             task=LANE_B)
        self.assertIn("lane %s's worktree" % LANE_A, self.deny_reason(result))

    def test_a_write_into_the_project_checkout_outside_the_lane_worktree_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # R26: the checkout the worktrees branch from is no lane's to write, its .git
        # included, before ppl init and after, for every session
        fixtures.bind_lane("LANE-5104", self.checkout,
                           worktree=os.path.join(self.checkout, ".worktrees", "shop-LANE-5104"),
                           lane=3)
        for task in (LANE_A, "LANE-5104"):
            for agent in (None, "general-purpose"):
                for tool, tool_input in (
                        ("Write", edit_input("Write", self.checkout + "/README.md")),
                        ("Edit", edit_input("Edit", self.checkout + "/src/App.java")),
                        ("Bash", {"command": "echo x >> %s/.git/config" % self.checkout}),
                        ("Bash", {"command": "cd %s && touch notes.md" % self.checkout})):
                    with self.subTest(task=task, agent=agent, tool=tool):
                        reason = self.deny_reason(self.decide(agent, tool, tool_input,
                                                              task=task))
                        self.assertIn("BOUNDARY_VIOLATION", reason)
                        self.assertIn("project checkout %s" % self.checkout, reason)
        # a role, whose Bash is otherwise free outside its run's repo
        result = self.bash("echo x > %s/build.log" % self.checkout, agent="ppl-code-worker")
        self.assertIn("project checkout", self.deny_reason(result))
        # the lane's own worktree lies in the checkout, and this check leaves it to the run's
        # own write boundary (R-UNROLED-REPO-WRITE)
        reason = self.deny_reason(self.decide(None, "Write", edit_input(
            "Write", self.worktree[LANE_A] + "/src/App.java")))
        self.assertIn("inside run %s's repo" % LANE_A, reason)
        self.assertNotIn("project checkout", reason)
        self.assertIsNone(self.decide(None, "Write", edit_input("Write", "/tmp/elsewhere/n.md")))

    def test_a_removal_that_takes_a_project_checkout_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # R28: removing the checkout, or a directory that holds it, takes the lanes' base and
        # their worktrees with it, whether the checkout is the lane's own or another's
        parent = real(tmphome.mkdtemp(prefix="pplprojects-"))
        checkout = os.path.join(parent, "web")
        worktree = os.path.join(checkout, ".worktrees", "web-LANE-5105")
        os.makedirs(worktree)
        run.init("LANE-5105", "u", worktree)
        fixtures.bind_lane("LANE-5105", checkout, worktree=worktree, lane=3)
        for command in ("rm -rf %s" % checkout, "mv %s /tmp/gone" % checkout,
                        "rm -rf %s" % parent, "cd %s && rm -rf web" % parent,
                        "mv %s /tmp/gone" % parent):
            for task in ("LANE-5105", LANE_A):
                with self.subTest(command=command, task=task):
                    reason = self.deny_reason(self.bash(command, task=task))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    if task == "LANE-5105":
                        self.assertIn("project checkout %s" % checkout, reason)
        # lane A's own checkout too, which holds its own worktree
        for command in ("rm -rf %s" % self.checkout, "mv %s /tmp/gone" % self.checkout):
            with self.subTest(own=command):
                reason = self.deny_reason(self.bash(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_without_facts_a_lane_keeps_the_protected_paths(self):
        # rule: R-LANE-START-DIR-PROTECTED
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # R28: the harness needs no OS sandbox, so the lane has no guard facts, and an edit
        # tool still never writes the registry the binding trusts, Claude Code's settings,
        # ppl's own code or its config, for a role or the orchestrator
        fake_home = real(tmphome.mkdtemp(prefix="pplfakehome-"))
        self.assertFalse((lanes.start_dir(LANE_A) / os_sandbox.FACTS_FILE).exists())
        package = os.path.dirname(real(escape_paths.__file__))
        targets = (str(lanes.registry_path()), str(paths.home() / lanes.LOCK_FILE),
                   os.path.join(fake_home, ".claude", "settings.json"),
                   os.path.join(fake_home, ".claude.json"), os.path.join(package, "escape.py"),
                   str(GUARD), str(paths.home() / "config.toml"),
                   str(paths.home() / "routing.toml"))
        with mock.patch.dict(os.environ, {"HOME": fake_home}):
            for agent in (None, "ppl-code-worker", "general-purpose"):
                for tool in ("Edit", "Write"):
                    for target in targets:
                        with self.subTest(agent=agent, tool=tool, target=target):
                            reason = self.deny_reason(self.decide(agent, tool,
                                                                  edit_input(tool, target)))
                            self.assertIn("BOUNDARY_VIOLATION", reason)
                            self.assertIn(escape_paths.PROTECTED_WHY, reason)
            # what is not protected stays the orchestrator's to edit
            self.assertIsNone(self.decide(None, "Write", edit_input(
                "Write", os.path.join(fake_home, "notes", "todo.md"))))

    def test_an_unreadable_registry_holds_a_bound_write_and_a_ppl_command(self):
        # rule: R-LANE-CROSS-WRITE
        # rule: R-LANE-CROSS-TASK
        # the binding read the lane's entry, and the registry cannot be read again for the
        # other lanes' work: a write and a ppl command fail closed, what neither does passes
        bound = lanes.binding(argv(LANE_A))
        start = str(lanes.start_dir(LANE_A))
        write = payload(None, "Write", {"file_path": self.worktree[LANE_A] + "/x"}, start)
        refusal = ppl_guard.cross_lane_denial(write, bound, None)
        self.assertEqual(refusal[0], "OWNER_DECISION")
        self.assertIn("registry cannot be read", refusal[1])
        refusal = ppl_guard.ppl_task_mismatch("ppl show", bound, None, start)
        self.assertEqual(refusal[0], "OWNER_DECISION")
        self.assertIsNone(ppl_guard.ppl_task_mismatch("ls -la", bound, None, start))
        self.assertIsNone(ppl_guard.cross_lane_denial(
            payload(None, "Read", {"file_path": self.worktree[LANE_A] + "/x"}, start), bound,
            None))
        # a session in no lane has no other lanes' work to place
        self.assertIsNone(ppl_guard.cross_lane_denial(
            payload(None, "Write", {"file_path": "/tmp/elsewhere/n.md"}, start),
            lanes.binding([]), None))

    def test_a_write_into_another_lanes_worktree_through_a_symlink_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # Review Focus 4: lane B, on another project, is recorded through a symlinked parent
        # and the write names the real path, then the other way round
        project = real(tmphome.mkdtemp(prefix="pplproject-"))
        theirs = os.path.join(project, ".worktrees", "web-" + LANE_B)
        os.makedirs(theirs)
        alias = os.path.join(real(tmphome.mkdtemp(prefix="pplalias-")), "project")
        os.symlink(project, alias)
        through = os.path.join(alias, ".worktrees", "web-" + LANE_B)
        for recorded, written in ((through, theirs), (theirs, through)):
            fixtures.bind_lane(LANE_B, os.path.dirname(os.path.dirname(recorded)),
                               worktree=recorded, lane=2)
            for tool, tool_input in (
                    ("Write", edit_input("Write", written + "/src/App.java")),
                    ("Bash", {"command": "echo x > %s/src/App.java" % written}),
                    ("Bash", {"command": "cd %s && rm -rf src" % written})):
                with self.subTest(recorded=recorded, tool=tool):
                    reason = self.deny_reason(self.decide(None, tool, tool_input))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("lane %s's worktree" % LANE_B, reason)

    def test_ppl_with_another_lanes_task_is_denied(self):
        # rule: R-LANE-CROSS-TASK
        theirs, worktree = LANE_B, self.worktree[LANE_B]
        start = str(lanes.start_dir(LANE_B))
        commands = ("ppl show --task %s" % theirs, "ppl show --task=%s" % theirs,
                    "PPL_TASK=%s ppl show" % theirs, "env PPL_TASK=%s ppl show" % theirs,
                    "export PPL_TASK=%s; ppl show" % theirs,
                    "ppl findings list --task '%s'" % theirs,
                    "python3 -m ppl show --task %s" % theirs,
                    "/opt/pin/bin/ppl show --task %s" % theirs,
                    "ppl lane close %s" % theirs, "ppl lane resume %s" % theirs,
                    # any module of the package runs the CLI, ppl.__main__ among them
                    "python3 -m ppl.__main__ show --task %s" % theirs,
                    "python3 -mppl show --task %s" % theirs,
                    "python3.11 -I -m ppl.cli show --task %s" % theirs,
                    "python3 -Im ppl.__main__ show --task %s" % theirs,
                    "python3 -bBm ppl show --task %s" % theirs,
                    "ppl show --task $(echo %s)" % theirs,
                    # bare ppl takes its run from a cwd inside a lane once PPL_TASK is gone
                    "cd %s && PPL_TASK= ppl show" % worktree,
                    "cd %s/src; env -u PPL_TASK ppl show" % worktree,
                    "env -C %s ppl show" % start, "pushd %s && ppl show" % start)
        for agent in (None, "ppl-verifier"):
            for command in commands:
                with self.subTest(agent=agent, command=command):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    if agent is None:
                        self.assertIn(theirs, reason)
        # a shell that already stands in the other lane
        for cwd in (worktree, start):
            with self.subTest(cwd=cwd):
                reason = self.deny_reason(self.bash("ppl show", cwd=cwd))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(theirs, reason)

    def test_ppl_where_the_guard_cannot_place_the_cwd_is_denied(self):
        # rule: R-LANE-CROSS-TASK
        # a bare ppl with no PPL_TASK takes its run from the lane holding its cwd, so a cwd the
        # walk cannot place fails closed: blinded by a substitution, a cd to a variable, cd -,
        # a glob or a ~user, whatever directory it would have reached
        alias = os.path.join(real(tmphome.mkdtemp(prefix="pplalias-")), "b")
        os.symlink(self.worktree[LANE_B], alias)
        for command in ("cd %s && env -u PPL_TASK ppl show $(true)" % alias,
                        "cd %s && env -u PPL_TASK ppl show `true`" % alias,
                        "cd %s/.. && cd *5102 && env -u PPL_TASK ppl show" % self.worktree[LANE_A],
                        'cd "$X" && env -u PPL_TASK ppl show', "cd - && ppl show",
                        'env -C "$D" ppl show', "pushd +1 && ppl show",
                        "cd ~bob && ppl show", "ppl show --task %s $(true)" % LANE_A):
            for agent in (None, "ppl-verifier"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("cannot tell the directory", reason)
        # a directory it can place is judged by where it is
        for command in ("cd %s && env -u PPL_TASK ppl show" % self.worktree[LANE_A],
                        "cd /tmp && ls *.log; cd %s && ppl show" % lanes.start_dir(LANE_A)):
            with self.subTest(placed=command):
                self.assertIsNone(self.bash(command))

    def test_ppl_behind_a_shell_construct_the_cd_follower_cannot_see_through_is_denied(self):
        # rule: R-LANE-CROSS-TASK
        # R38: a cd behind a control keyword, in a function, or in a string the shell runs
        # again is one the walk does not follow, so a command that spells ppl with one fails
        # closed, and so does a call that names no cwd at all
        for command in ("if true; then cd $X; fi; env -u PPL_TASK ppl show",
                        'while read d; do cd "$d"; done </tmp/f; env -u PPL_TASK ppl show',
                        "for d in ../*; do (cd $d && env -u PPL_TASK ppl show); done",
                        "f() { cd $X; }; f; env -u PPL_TASK ppl show",
                        "function f { cd ..; }; f; env -u PPL_TASK ppl show",
                        "eval 'cd $X'; env -u PPL_TASK ppl show",
                        "source /tmp/x.sh; env -u PPL_TASK ppl show",
                        ". /tmp/x.sh; env -u PPL_TASK ppl show",
                        "case x in *) cd ..;; esac; env -u PPL_TASK ppl show",
                        "exec env -u PPL_TASK ppl show"):
            for agent in (None, "ppl-verifier"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("cannot tell the directory", reason)
        reason = self.deny_reason(ppl_guard.decide(
            payload(None, "Bash", {"command": "ppl show"}, None), argv(LANE_A)))
        self.assertIn("names no cwd", reason)
        for command in ("ppl show", "cd %s && ppl show" % self.worktree[LANE_A],
                        "if true; then echo x; fi"):
            with self.subTest(allowed=command):
                self.assertIsNone(self.bash(command))

    def test_a_git_subcommand_or_command_word_the_shell_builds_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # R39: a subcommand, a config value or a command word the shell builds may be
        # worktree, an alias or git itself, so it fails closed for every session
        for command in ("W=worktree; git $W prune", 'git "$W" remove shop-%s' % LANE_B,
                        "git -C %s $W remove --force shop-%s" % (self.checkout, LANE_B),
                        "git ${W} prune", "git $1 prune", "git work*ee prune",
                        "git $(echo worktree) prune", "git `printf work`tree prune",
                        'git -c "$K" wt prune', "G=git; $G worktree remove x",
                        "$(command -v git) worktree prune",
                        "C=config; git $C core.hooksPath /tmp/h"):
            for agent in (None, "ppl-code-worker", "general-purpose"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
        for command in ("git status", "git log -1 $(git rev-parse HEAD)", "echo $HOME"):
            with self.subTest(allowed=command):
                self.assertIsNone(self.bash(command, agent="ppl-code-worker",
                                            cwd=self.worktree[LANE_A]))

    def test_a_git_is_judged_by_its_subcommand_whatever_its_arguments_hold(self):
        # rule: R-LANE-SHARED-GIT-DIR
        # rule: R-LANE-CROSS-WRITE
        # R40: a role's commit with its message in a here-document, and a diff with a
        # substituted base, are judged as the commit and the diff they are, and only a git whose
        # subcommand the guard cannot read is judged by its spelling
        own = self.worktree[LANE_A]
        commit = ("git commit -m \"$(cat <<'EOF'\nfix: worktree config loading (lane), don't\n"
                  "git worktree prune is not run here\nEOF\n)\"")
        for command in (commit, "git diff --stat $(git merge-base HEAD main) -- src/config",
                        "git log --oneline `git merge-base HEAD main`..HEAD -- config"):
            with self.subTest(allowed=command):
                self.assertIsNone(self.bash(command, agent="ppl-code-worker", cwd=own))
        for command, why in (("git $(echo worktree) remove x", "builds or expands"),
                             ("echo $(git worktree prune)", "git worktree"),
                             ('git config core.hooksPath "$(cat /tmp/h)"',
                              "may not write git config"),
                             ("git commit -m \"$(cat <<'EOF'\nleft open\n)\"", "git worktree")):
            with self.subTest(denied=command):
                reason = self.deny_reason(self.bash(command + (" && git worktree prune"
                                                               if "left open" in command else ""),
                                                    agent="ppl-code-worker", cwd=own))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(why, reason)

    def test_a_here_document_is_judged_by_what_it_feeds(self):
        # rule: R-LANE-CROSS-WRITE
        # rule: R-LANE-CROSS-TASK
        # R41: a here-document fed to cat is data, one fed to a shell or another command runs
        # as commands, and one fed to an interpreter may not spell ppl, git worktree or git
        # config, which the guard cannot read inside code
        for command in ("bash <<'EOF'\ngit worktree remove x\nEOF",
                        "sh <<EOF\ngit worktree prune\nEOF",
                        "env sh <<'EOF'\ncd ..\ngit worktree prune\nEOF",
                        "bash -s <<'EOF'\ngit $(echo worktree) prune\nEOF",
                        "xargs -0 sh -c 'true' <<'EOF'\ngit worktree prune\nEOF",
                        "python3 - <<'EOF'\nimport subprocess\n"
                        "subprocess.run(['git', 'worktree', 'prune'])\nEOF",
                        "perl <<'EOF'\nsystem('env -u PPL_TASK ppl show')\nEOF",
                        "node <<'EOF'\nrequire('child_process').execSync('git config core.x y')\n"
                        "EOF"):
            for agent in (None, "ppl-code-worker"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent,
                                                        cwd=self.worktree[LANE_A]))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
        reason = self.deny_reason(self.bash("bash <<'EOF'\ngit config core.hooksPath /tmp/h\nEOF",
                                            agent="ppl-code-worker", cwd=self.worktree[LANE_A]))
        self.assertIn("may not write git config", reason)
        # cat's here-document is data: R40's commit and a note naming git worktree pass
        commit = ("git commit -m \"$(cat <<'EOF'\nfix: worktree config loading (lane), don't\n"
                  "git worktree prune is not run here\nEOF\n)\"")
        for command in (commit, "cat > /tmp/note.md <<'EOF'\ngit worktree prune\nEOF"):
            with self.subTest(allowed=command):
                self.assertIsNone(self.bash(command, agent="ppl-code-worker",
                                            cwd=self.worktree[LANE_A]))

    def test_a_hyphenated_git_writer_is_a_write_shape(self):
        # rule: R-LANE-CROSS-WRITE
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # R42: git's hyphenated plumbing writes the index, refs, objects or the tree with no
        # path the walk could name, so it fails closed in a lane, while the read-only
        # hyphenated words (merge-base, rev-parse, cat-file, ...) are no write shape
        theirs = self.worktree[LANE_B]
        git_dir = self.checkout + "/.git"
        for command in ("git -C %s update-index --add src/App.java" % theirs,
                        "git --git-dir=%s update-ref refs/heads/main HEAD" % git_dir,
                        "git -C %s checkout-index -a -f" % theirs,
                        "git -C %s symbolic-ref HEAD refs/heads/other" % theirs,
                        "git -C %s hash-object -w src/App.java" % theirs,
                        "git -C $(echo %s) update-ref refs/heads/x HEAD" % theirs):
            for agent in (None, "ppl-code-worker"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent,
                                                        cwd=self.worktree[LANE_A]))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
        for command in ("git diff --stat $(git merge-base HEAD main) -- src/config",
                        "git rev-parse HEAD", "git cat-file -p HEAD", "git symbolic-ref HEAD",
                        "git ls-files $(git rev-parse --show-toplevel)",
                        "git hash-object src/App.java"):
            with self.subTest(allowed=command):
                self.assertIsNone(self.bash(command, agent="ppl-code-worker",
                                            cwd=self.worktree[LANE_A]))

    def test_git_worktree_in_any_form_but_list_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # R34: git takes a worktree by a unique tail of its path from anywhere in the
        # repository, so no path the guard reads places remove, move, prune, lock or repair
        theirs = self.worktree[LANE_B]
        planted = real(tmphome.mkdtemp(prefix="pplplanted-"))
        os.makedirs(os.path.join(planted, "shop-" + LANE_B))
        for command in ("git worktree remove --force %s" % theirs,
                        "git -C %s worktree remove %s" % (self.checkout, theirs),
                        "git worktree remove shop-%s" % LANE_B,
                        "cd %s && git worktree remove --force shop-%s" % (planted, LANE_B),
                        "cd %s/.worktrees && git worktree remove -f shop-%s"
                        % (self.checkout, LANE_B),
                        "git worktree move %s /tmp/moved" % theirs, "git worktree prune",
                        "git worktree add /tmp/extra", "git worktree lock shop-%s" % LANE_B,
                        "git worktree repair", "timeout 5 git worktree prune",
                        "git worktree remove $(echo shop-%s)" % LANE_B,
                        "git worktree remove %s" % self.worktree[LANE_A]):
            for agent in (None, "ppl-code-worker", "general-purpose"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("git worktree", reason)
        for command in ("git worktree list", "git worktree list --porcelain"):
            with self.subTest(read=command):
                self.assertIsNone(self.bash(command))
                self.assertIsNone(self.bash(command, agent="ppl-verifier"))

    def test_a_git_whose_subcommand_cannot_be_trusted_is_denied(self):
        # rule: R-LANE-CROSS-WRITE
        # R37: the guard reads git's subcommand past its global options, each as git.c reads
        # it, so an option it does not know fails closed, and so does an alias or include set
        # on the command line or config handed in from elsewhere, which can turn a word into
        # git worktree
        theirs = self.worktree[LANE_B]
        for command in ("git --attr-source HEAD worktree remove %s" % theirs,
                        "git --shallow-file /dev/null worktree prune",
                        "git --frobnicate x worktree remove %s" % theirs,
                        "git -C%s status" % self.checkout,
                        "timeout 5 git --frobnicate x worktree prune",
                        "git -c alias.wt=worktree wt remove %s" % theirs,
                        "git -c Alias.Wt=worktree wt remove %s" % theirs,
                        "git --config-env=alias.wt=WT wt remove %s" % theirs,
                        "git --config-env alias.wt=WT wt prune",
                        "git -c include.path=/tmp/aliases wt prune",
                        "git -c includeIf.onbranch:x.path=/tmp/aliases wt prune",
                        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.wt GIT_CONFIG_VALUE_0=worktree "
                        "git wt prune",
                        "env GIT_CONFIG_GLOBAL=/tmp/cfg git wt prune",
                        "HOME=/tmp/fakehome git wt prune",
                        "git -c alias.st=status st $(true)"):
            for agent in (None, "ppl-code-worker", "general-purpose"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("git", reason)
        # a role's git config past an option the table now reads
        reason = self.deny_reason(self.bash("git --attr-source HEAD config core.hooksPath /x",
                                            agent="ppl-code-worker"))
        self.assertIn("may not write git config", reason)
        for command in ("git --no-pager log -1", "git -C %s status" % self.worktree[LANE_A],
                        "git -c core.quotepath=off status", "git --attr-source HEAD log -1",
                        "grep -rn git --include=*.py .", "git --version"):
            with self.subTest(allowed=command):
                self.assertIsNone(self.bash(command))

    def test_writes_under_the_checkouts_git_dir_are_denied(self):
        # rule: R-LANE-SHARED-GIT-DIR
        # R33, R35: the shared config, hooks and info, which every lane on the checkout and
        # the operator's own git read and run, and every git dir in it, another lane's
        # commondir and gitdir that tell its git where to look among them, are no session's
        # to write, and git's own writes there are no write shape
        git_dir = self.checkout + "/.git"
        targets = (git_dir + "/config", git_dir + "/hooks/pre-commit",
                   git_dir + "/info/attributes", git_dir + "/packed-refs",
                   git_dir + "/worktrees/shop-%s/commondir" % LANE_B,
                   git_dir + "/worktrees/shop-%s/gitdir" % LANE_B,
                   git_dir + "/worktrees/shop-%s/gitdir" % LANE_A,
                   git_dir + "/objects/info/alternates")
        for agent in (None, "ppl-code-worker", "general-purpose"):
            for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                for target in targets:
                    with self.subTest(agent=agent, tool=tool, target=target):
                        reason = self.deny_reason(self.decide(agent, tool,
                                                              edit_input(tool, target)))
                        self.assertIn("BOUNDARY_VIOLATION", reason)
                        self.assertIn("project checkout", reason)
            for command in ("echo x >> %s/config" % git_dir,
                            "cp /tmp/h %s/hooks/post-checkout" % git_dir,
                            "tee %s/info/exclude" % git_dir,
                            "echo /tmp/evil > %s/worktrees/shop-%s/commondir" % (git_dir, LANE_B),
                            "cd %s && echo x > ../../.git/hooks/pre-push" % self.worktree[LANE_A]):
                with self.subTest(agent=agent, command=command):
                    reason = self.deny_reason(self.bash(command, agent=agent,
                                                        cwd=self.worktree[LANE_A]))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("project checkout", reason)

    def test_a_role_may_not_write_git_config_in_a_lane(self):
        # rule: R-LANE-SHARED-GIT-DIR
        # git config in a worktree writes the checkout's shared .git/config
        own = self.worktree[LANE_A]
        for command in ("git -C %s config core.hooksPath /tmp/h" % own,
                        "git config --local core.hooksPath /tmp/h",
                        "git config set core.hooksPath /tmp/h",
                        "git config --add include.path /tmp/x", "git config --unset user.name",
                        "timeout 5 git config core.fsmonitor /tmp/x",
                        "/usr/bin/git -c a.b=c config filter.x.smudge /tmp/x",
                        "git config core.hooksPath $(echo /tmp/h)", "git config core.hooksPath",
                        # R36: the first word decides, and no action may follow a read
                        "git config core.hooksPath /tmp/h --list",
                        "git config --get user.name --add core.hooksPath /tmp/h",
                        "git config --list --unset-all user.name",
                        "git config --show-origin core.hooksPath /tmp/h",
                        "git config --global --get user.name"):
            for agent in ("ppl-code-worker", "ppl-verifier"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent, cwd=own))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("may not write git config", reason)
        for command in ("git config --get user.name", "git config --get-all remote.origin.url",
                        "git config --get-regexp ^user", "git config --list",
                        "git config -l --show-origin", "git config get user.name",
                        "git config list", "git log --grep config"):
            with self.subTest(read=command):
                self.assertIsNone(self.bash(command, agent="ppl-verifier", cwd=own))

    def test_ppl_behind_a_wrapper_the_walk_does_not_unwrap_is_still_judged(self):
        # rule: R-LANE-CROSS-TASK
        # R30: the walk does not unwrap timeout, xargs, sudo, find -exec, stdbuf or an
        # env -S string, so a command that spells ppl anywhere, or runs it under any name
        # shlex reads, is held to every literal mention of another lane's task, free text
        # included, and a PPL_TASK naming another lane is denied wherever it stands
        theirs = LANE_B
        for command in ("timeout 5 ppl show --task %s" % theirs,
                        "echo %s | xargs ppl show --task" % theirs,
                        "sudo ppl show --task %s" % theirs,
                        "find . -maxdepth 0 -exec ppl show --task %s \\;" % theirs,
                        "env -S 'ppl show --task %s'" % theirs,
                        "stdbuf -o0 ppl show --task %s" % theirs,
                        "timeout 5 'ppl' show --task %s" % theirs,
                        "ppl findings add --title 'same cause as %s'" % theirs,
                        "export PPL_TASK=%s" % theirs, "PPL_TASK=%s timeout 5 make" % theirs):
            for agent in (None, "ppl-verifier"):
                with self.subTest(command=command, agent=agent):
                    reason = self.deny_reason(self.bash(command, agent=agent))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    if agent is None:
                        self.assertIn(theirs, reason)
        # the lane's own task behind a wrapper is its own
        for command in ("timeout 5 ppl show --task %s" % LANE_A, "sudo ppl show",
                        "env -S 'ppl show --task %s'" % LANE_A):
            with self.subTest(own=command):
                self.assertIsNone(self.bash(command))

    def test_ppl_with_its_own_task_is_allowed(self):
        # a task with no lane is no other lane's run: a pending reflection names a destroyed
        # run's task, and a command that runs no ppl may name another lane's
        own, worktree = LANE_A, self.worktree[LANE_A]
        for command in ("ppl show --task %s" % own, "ppl show --task=%s" % own,
                        "PPL_TASK=%s ppl show" % own, "ppl show", "cd %s && ppl show" % worktree,
                        "ppl lessons record --task LANE-5000 --file /tmp/lessons.json",
                        "echo %s" % LANE_B, "git log --grep %s" % LANE_B):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command))
        for command in ("ppl show --task %s" % own, "ppl show"):
            with self.subTest(role=command):
                self.assertIsNone(self.bash(command, agent="ppl-verifier", cwd=worktree))

    def test_gh_from_the_start_directory_is_allowed_and_from_the_worktree_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # the lane's own facts, as ppl lane renders them: the start directory is where gh
        # runs, and the lane's worktree is in its write scope
        fixtures.install_variant("example-generic", "gh-lane",
                                 repo={"path": self.checkout, "default_branch": "main",
                                       "worktrees": self.checkout + "/.worktrees"},
                                 **fixtures.GITHUB_ISSUES)
        pins = {name: {"path": "/opt/pin/bin/" + name, "realpath": real("/opt/pin/bin/" + name)}
                for name in ("gh", "git", "ppl")}
        start = lanes.start_dir(LANE_A)
        facts = os_sandbox.facts(harness.load("gh-lane"), start, pins,
                                 worktree=self.worktree[LANE_A])
        store.write_json(start / os_sandbox.FACTS_FILE, facts)
        command = {"command": pins["gh"]["path"] + " auth status",
                   "dangerouslyDisableSandbox": True}
        for cwd in (str(start), real(start)):
            with self.subTest(cwd=cwd):
                self.assertIsNone(self.decide(None, "Bash", command, cwd))
        for cwd in (self.worktree[LANE_A], self.worktree[LANE_A] + "/src", self.checkout,
                    str(lanes.start_dir(LANE_B))):
            with self.subTest(cwd=cwd):
                reason = self.deny_reason(self.decide(None, "Bash", command, cwd))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(real(start), reason)


if __name__ == "__main__":
    unittest.main()

"""The hooks know their run from the `--task <id>` their own command line carries, rendered
into the lane's write-protected settings.json, never from a shared pointer."""
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
HOOKS = Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"
sys.path.insert(0, str(HOOKS))

import fixtures
from ppl import control, lanes, paths, run, store
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


if __name__ == "__main__":
    unittest.main()

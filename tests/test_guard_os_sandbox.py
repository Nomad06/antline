import contextlib
import io
import json
import os
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))

import fixtures
from ppl import escape, harness, os_sandbox, paths, run, store
import ppl_guard

TASK = "GH-7"
REPO = "/tmp/ghrepo"
WS = "/tmp/ws"
OTHER = "/tmp/other"
BRANCH = "gh-7-cart-total"
GH = "/opt/pin/gh"
GIT = "/opt/pin/git"
PPL = "/opt/pin/ppl"


def real(path):
    return os.path.realpath(str(path))


def scratch_file(name):
    return str(paths.home() / "scratch" / TASK / name)


def facts_doc(**overrides):
    """What harness.apply writes for a GitHub harness whose checkout is the run's repo."""
    home = paths.home()
    doc = {"version": 1,
           "workspace": real(WS),
           "checkout": real(REPO),
           "worktrees": real(REPO + "/.worktrees"),
           "default_branch": "main",
           "forge_repo": "o/r",
           "tracker_repo": "o/r",
           "scratch": real(home / "scratch"),
           "write_scope": [real(home / "runs"), real(home / "labels"), real(home / "scratch"),
                           real(REPO), real(REPO + "/.worktrees")],
           "pins": {"gh": {"path": GH, "realpath": real(GH)},
                    "git": {"path": GIT, "realpath": real(GIT)},
                    "ppl": {"path": PPL, "realpath": real(PPL)}},
           "written_sandbox": {"enabled": True},
           "written_allow": []}
    doc.update(overrides)
    return doc


def facts_path():
    return paths.home() / os_sandbox.FACTS_FILE


def setUpModule():
    if not run.state_path(TASK).exists():
        run.init(TASK, "https://github.com/o/r/issues/7", REPO)
    state = run.load(TASK)
    state["branch"] = BRANCH
    run.save(state)
    store.write_json(paths.run_dir(TASK) / "boundary.json", {"write": ["src/**"], "forbid": []})
    fixtures.install_variant("example-generic", "gh-issues", **fixtures.GITHUB_ISSUES)
    store.write_json(paths.run_dir(TASK) / "harness.json",
                     harness.snapshot(harness.load("gh-issues")))
    store.write_json(facts_path(), facts_doc())
    (paths.home() / "scratch" / TASK).mkdir(parents=True, exist_ok=True)
    for name in ("pr-body.md", "note.md", "reply.md"):
        Path(scratch_file(name)).write_text("text\n", encoding="utf-8")


def tearDownModule():
    """The data root is shared under discover, so the facts and the scratch files go."""
    facts_path().unlink(missing_ok=True)
    shutil.rmtree(str(paths.home() / "scratch" / TASK), ignore_errors=True)


def payload(agent, tool, tool_input, cwd=WS):
    return {"hook_event_name": "PreToolUse", "agent_type": agent,
            "tool_name": tool, "tool_input": tool_input, "cwd": cwd}


def flagged(agent, command, cwd=WS):
    return payload(agent, "Bash", {"command": command, "dangerouslyDisableSandbox": True}, cwd)


def pr_create():
    """Built per call: under discover every module points PPL_HOME elsewhere at import."""
    return ("%s pr create -R o/r --head %s --base main --title 'GH-7 add the cart total' "
            "--body-file %s" % (GH, BRANCH, scratch_file("pr-body.md")))


class OsSandboxCase(unittest.TestCase):
    def setUp(self):
        run.set_active(TASK)
        store.write_json(facts_path(), facts_doc())

    def deny_reason(self, result):
        self.assertIsNotNone(result, "the call was allowed")
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    def decide(self, command, agent=None, cwd=WS):
        return ppl_guard.decide(flagged(agent, command, cwd))

    @contextlib.contextmanager
    def facts_text(self, text):
        """os_sandbox.json replaced by `text`, or removed when `text` is None."""
        if text is None:
            facts_path().unlink()
        else:
            facts_path().write_text(text, encoding="utf-8")
        try:
            yield
        finally:
            store.write_json(facts_path(), facts_doc())

    @contextlib.contextmanager
    def no_active_run(self):
        paths.active_file().unlink()
        try:
            yield
        finally:
            run.set_active(TASK)

    @contextlib.contextmanager
    def run_state(self, **changes):
        state = run.load(TASK)
        saved = dict(state)
        state.update(changes)
        run.save(state)
        try:
            yield
        finally:
            run.save(saved)


class TestRoleEscape(OsSandboxCase):
    def test_a_ppl_role_cannot_leave_the_sandbox(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        result = self.decide("ls", agent="ppl-code-worker")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("outside the OS sandbox", self.deny_reason(result))

    def test_a_role_is_denied_even_without_a_run_or_facts(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        with self.facts_text(None), self.no_active_run():
            for agent in ("ppl-verifier", "ppl-review-responder"):
                with self.subTest(agent=agent):
                    result = self.decide(pr_create(), agent=agent, cwd=OTHER)
                    self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_role_is_denied_before_any_file_is_read(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        with mock.patch.object(ppl_guard, "active_state", side_effect=AssertionError), \
                mock.patch.object(ppl_guard, "_active_task_id", side_effect=AssertionError), \
                mock.patch.object(escape, "load_facts", side_effect=AssertionError):
            result = self.decide("ls", agent="ppl-test-worker")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_general_purpose_subagent_in_the_workspace_is_denied(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        for cwd in (WS, REPO):
            with self.subTest(cwd=cwd):
                result = self.decide("ls", agent="general-purpose", cwd=cwd)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_subagent_cannot_ride_the_orchestrator_shape(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        result = self.decide(pr_create(), agent="general-purpose")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_subagent_elsewhere_is_left_alone(self):
        # with neither the guard facts nor an active run, nothing is the pipeline's
        with self.facts_text(None), self.no_active_run():
            self.assertIsNone(self.decide("ls", agent="general-purpose", cwd=OTHER))
            self.assertIsNone(self.decide("ls", cwd=OTHER))
            call = flagged(None, "ls", OTHER)
            call["agent_id"] = "a1"
            self.assertIsNone(ppl_guard.decide(call))

    def test_a_subagent_anywhere_is_denied_while_facts_or_a_run_exist(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        # the cwd and the run's recorded repo are both in a role's reach, so neither scopes
        with self.subTest(facts=True, run=True):
            result = self.decide("ls", agent="general-purpose", cwd=OTHER)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        with self.subTest(facts=True, run=False), self.no_active_run():
            result = self.decide("ls", agent="Explore", cwd=OTHER)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        with self.subTest(facts=False, run=True), self.facts_text(None):
            result = self.decide("ls", agent="general-purpose", cwd=OTHER)
            self.assertIsNotNone(ppl_guard.decide(flagged("general-purpose", "ls", OTHER)))
            self.assertIn("ppl harness apply", self.deny_reason(result))
        with self.subTest(repo="elsewhere"), self.run_state(repo=OTHER):
            result = self.decide(pr_create(), agent="general-purpose", cwd=WS)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_payload_carrying_an_agent_id_is_denied(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        for agent_id in ("a1", 7):
            call = flagged(None, pr_create())
            call["agent_id"] = agent_id
            with self.subTest(agent_id=agent_id):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(ppl_guard.decide(call)))

    def test_an_unflagged_role_call_is_not_touched_by_escape(self):
        for flag in (None, False):
            tool_input = {"command": "ls"}
            if flag is not None:
                tool_input["dangerouslyDisableSandbox"] = flag
            with self.subTest(flag=flag), \
                    mock.patch.object(escape, "decide", side_effect=AssertionError) as checked:
                self.assertIsNone(ppl_guard.decide(payload("ppl-code-worker", "Bash", tool_input)))
                checked.assert_not_called()

    def test_any_tool_carrying_the_flag_is_flagged(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        for tool in ("Write", "PowerShell", "mcp__shell__run", "BashOutput"):
            call = payload("ppl-code-worker", tool, {"command": "ls", "file_path": REPO + "/x",
                                                     "dangerouslyDisableSandbox": True}, REPO)
            with self.subTest(tool=tool):
                self.assertTrue(escape.flagged(call))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(ppl_guard.decide(call)))
                orchestrator = dict(call, agent_type=None, cwd=WS)
                self.assertIsNotNone(ppl_guard.decide(orchestrator))
                del call["tool_input"]["dangerouslyDisableSandbox"]
                self.assertFalse(escape.flagged(call))

    def test_a_truthy_flag_that_is_not_true_still_counts(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        for value in ("true", 1, "yes"):
            with self.subTest(value=value):
                result = ppl_guard.decide(payload(
                    "ppl-code-worker", "Bash", {"command": "ls", "dangerouslyDisableSandbox": value}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))


class TestFailClosed(OsSandboxCase):
    def test_a_non_string_agent_type_with_the_flag_is_denied(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        for agent in (["x"], 7, {"name": "ppl-code-worker"}, ""):
            with self.subTest(agent=agent):
                result = self.decide("ls", agent=agent, cwd=OTHER)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_an_exception_on_a_flagged_call_denies(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        def broken(_payload):
            raise RuntimeError("boom")

        for agent in (None, "general-purpose", ["x"]):
            stdout = io.StringIO()
            stdin = io.StringIO(json.dumps(flagged(agent, pr_create())))
            with self.subTest(agent=agent), mock.patch.object(ppl_guard, "_decide", broken), \
                    mock.patch.object(sys, "stdin", stdin), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ppl_guard.main(), 0)
                reason = self.deny_reason(json.loads(stdout.getvalue()))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("RuntimeError", reason)

    def test_an_unflagged_unroled_call_keeps_the_internal_error_allow(self):
        def broken(_payload):
            raise RuntimeError("boom")

        stdout = io.StringIO()
        stdin = io.StringIO(json.dumps(payload(None, "Bash", {"command": "ls"})))
        with mock.patch.object(ppl_guard, "_decide", broken), \
                mock.patch.object(sys, "stdin", stdin), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ppl_guard.main(), 0)
        self.assertEqual(stdout.getvalue(), "")

    def test_missing_facts_deny_the_orchestrator_naming_apply(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        with self.facts_text(None):
            for agent, cwd in ((None, WS), (None, REPO), (None, OTHER), ("general-purpose", WS)):
                with self.subTest(agent=agent, cwd=cwd):
                    result = self.decide(pr_create(), agent=agent, cwd=cwd)
                    self.assertIn("OWNER_DECISION", self.deny_reason(result))
                    self.assertIn("ppl harness apply", self.deny_reason(result))

    def test_missing_facts_on_a_run_without_the_profile_name_the_real_fix(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        # a run whose harness has no OS sandbox profile never needs to leave the sandbox
        snapshot = paths.run_dir(TASK) / "harness.json"
        saved = snapshot.read_text(encoding="utf-8")
        fixtures.install_harness("example-maven")
        store.write_json(snapshot, harness.snapshot(harness.load("example-maven")))
        try:
            with self.facts_text(None):
                for agent in (None, "general-purpose"):
                    with self.subTest(agent=agent):
                        reason = self.deny_reason(self.decide("mvn -q verify", agent=agent))
                        self.assertIn("OWNER_DECISION", reason)
                        self.assertIn("without dangerouslyDisableSandbox", reason)
                        self.assertIn("no OS sandbox profile", reason)
        finally:
            snapshot.write_text(saved, encoding="utf-8")

    def test_corrupt_facts_deny(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        broken = dict(facts_doc())
        del broken["pins"]
        for text in ("{not json", "[]", json.dumps(dict(facts_doc(), version=2)),
                     json.dumps(broken), json.dumps(facts_doc(write_scope="/tmp")),
                     json.dumps(facts_doc(workspace="relative/ws")),
                     json.dumps(facts_doc(deny_write="/tmp")),
                     json.dumps(facts_doc(deny_write=["relative/dir"]))):
            with self.subTest(text=text[:40]), self.facts_text(text):
                result = self.decide(pr_create())
                self.assertIn("OWNER_DECISION", self.deny_reason(result))
                self.assertIn("ppl harness apply", self.deny_reason(result))
        # with no run active too: the guard cannot tell what is pipeline-scoped
        with self.facts_text("{not json"), self.no_active_run():
            self.assertIn("ppl harness apply", self.deny_reason(self.decide("ls", cwd=OTHER)))

    def test_unreadable_run_state_denies_a_flagged_call(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        state_file = run.state_path(TASK)
        saved = state_file.read_text(encoding="utf-8")
        state_file.write_text("{half", encoding="utf-8")
        try:
            result = self.decide(pr_create(), cwd=OTHER)
        finally:
            state_file.write_text(saved, encoding="utf-8")
        self.assertIn("OWNER_DECISION", self.deny_reason(result))
        self.assertIn("state", self.deny_reason(result))

    def test_a_snapshot_that_is_a_fifo_denies_without_blocking(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        # runs/ is sandbox-writable, and a hook that blocks past its timeout denies nothing
        snapshot = paths.run_dir(TASK) / "harness.json"
        saved = snapshot.read_text(encoding="utf-8")
        snapshot.unlink()
        os.mkfifo(str(snapshot))
        try:
            with self.facts_text(None):
                started = time.monotonic()
                result = self.decide(pr_create())
                self.assertLess(time.monotonic() - started, 5)
        finally:
            snapshot.unlink()
            snapshot.write_text(saved, encoding="utf-8")
        self.assertIn("OWNER_DECISION", self.deny_reason(result))
        self.assertIn("ppl harness apply", self.deny_reason(result))

    def test_extra_keys_in_the_facts_are_accepted(self):
        with self.facts_text(json.dumps(facts_doc(future_key={"a": 1}))):
            self.assertIsNone(self.decide(pr_create()))


class TestOrchestratorShape(OsSandboxCase):
    def test_the_orchestrator_anywhere_meets_the_shape_check(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide("ls", cwd=OTHER)))
        with self.no_active_run():
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide("ls", cwd=OTHER)))
            self.assertIsNone(self.decide(GH + " auth status", cwd=WS))
        with self.facts_text(None):
            result = self.decide("ls", cwd=OTHER)
            self.assertIn("ppl harness apply", self.deny_reason(result))
        # the run's recorded repo no longer widens or narrows anything
        with self.run_state(repo=OTHER):
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide("ls", cwd=WS)))

    def test_a_bare_gh_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        result = self.decide("gh pr view 12 -R o/r")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn(GH, self.deny_reason(result))

    def test_a_pin_spelled_another_way_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for spelling in ("/opt/pin/./gh", "/opt//pin/gh", "/opt/pin/gh/", "'/opt/pin/gh'x"):
            with self.subTest(spelling=spelling):
                result = self.decide(spelling + " auth status")
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_pin_whose_target_moved_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        pins = dict(facts_doc()["pins"], gh={"path": GH, "realpath": "/opt/cellar/gh-2.1/gh"})
        with self.facts_text(json.dumps(facts_doc(pins=pins))):
            result = self.decide(GH + " auth status")
        self.assertIn("ppl harness apply", self.deny_reason(result))

    def test_a_compound_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for joiner in (" ; ", " && ", " || ", " | ", " & ", "\n"):
            with self.subTest(joiner=joiner):
                result = self.decide(GH + " auth status" + joiner + "id")
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_redirect_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for tail in (" > /tmp/x", " 2>/dev/null", " < /etc/passwd", " <<EOF"):
            with self.subTest(tail=tail):
                result = self.decide(GH + " auth status" + tail)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_leading_assignment_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for command in ("GH_HOST=evil.example " + GH + " auth status",
                        "GH_REPO=x/y " + GH + " pr view 12 -R o/r"):
            with self.subTest(command=command):
                result = self.decide(command)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                self.assertIn("assignment", self.deny_reason(result))

    def test_globs_groups_and_subshells_are_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for command in (GH + " pr view 1* -R o/r", GH + " pr view 1? -R o/r",
                        GH + " pr view [1] -R o/r", GH + " pr view {1,2} -R o/r",
                        "(" + GH + " auth status)", GH + " auth status `id`"):
            with self.subTest(command=command):
                result = self.decide(command)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_dollar_inside_single_quotes_is_literal(self):
        command = pr_create().replace("'GH-7 add the cart total'", "'Fix $HOME'")
        self.assertIsNone(self.decide(command))
        self.assertIsNone(self.decide(GH + " api user --jq '.login'"))

    def test_a_dollar_inside_double_quotes_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for title in ('"Fix $HOME"', "Fix$HOME", '"it\'s $HOME"', "\\$HOME", '"$(id)"'):
            with self.subTest(title=title):
                command = pr_create().replace("'GH-7 add the cart total'", title)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_a_quote_inside_the_other_quote_does_not_hide_a_separator(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # each title ends a quoted word, runs `id`, and starts another
        for title in ("\"'\";id;\"'\"", "\\';id;\\'", "\"\\\"'\";id;\"'\\\"\""):
            with self.subTest(title=title):
                command = pr_create().replace("'GH-7 add the cart total'", title)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_a_comment_and_a_control_character_are_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for command in (pr_create() + " #--draft", GH + " auth status\r", GH + " auth\x0bstatus",
                        GH + " auth status 'unclosed"):
            with self.subTest(command=command):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_zsh_expansions_outside_quotes_are_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # under zsh's EXTENDED_GLOB, ^ ~ # glob, a word-start = names a command path, a
        # leading ~ is the home directory, and ! is history or negation
        for title in ("fix^x", "a~b", "~root", "fix#", "!x", "=ls"):
            with self.subTest(title=title):
                command = pr_create().replace("'GH-7 add the cart total'", title)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))
        quoted = pr_create().replace("'GH-7 add the cart total'", "'fix ^x ~y #z !w =v'")
        self.assertIsNone(self.decide(quoted))
        quoted = pr_create().replace("'GH-7 add the cart total'", '"fix ^x ~y #z !w =v"')
        self.assertIsNone(self.decide(quoted))

    def test_adjacent_single_quoted_strings_are_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # zsh's RC_QUOTES reads '' inside single quotes as a quote: 'a''b' is a'b there and
        # ab here, so the guard would check one body file while gh reads another
        body = scratch_file("pr-body.md")
        split = body[:-3] + "''" + body[-3:]
        command = pr_create().replace(body, "'" + split + "'")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_ppl_from_a_cwd_inside_the_write_scope_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # ppl's own gh and git calls inherit the cwd, so a role-writable repo there is
        # loaded outside the sandbox
        for command in (PPL + " tracker fetch --task GH-7", PPL + " doctor --forge"):
            with self.subTest(command=command):
                result = self.decide(command, cwd=REPO)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                self.assertIn("write", self.deny_reason(result))

    def test_gh_and_ppl_from_outside_the_workspace_are_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # a temp directory is sandbox-writable and in no write_scope entry, so a role could
        # plant a repository there for gh's or ppl's own git to load
        temp = real(tempfile.mkdtemp(prefix="ppl-cwd-"))
        self.addCleanup(shutil.rmtree, temp, True)
        for cwd in ("/private/tmp/claude/x", temp, OTHER, os.path.dirname(real(WS))):
            for command in (GH + " auth status", GH + " pr view 1 -R o/r",
                            PPL + " doctor --forge", PPL + " tracker fetch --task GH-7",
                            pr_create()):
                with self.subTest(cwd=cwd, command=command[:40]):
                    reason = self.deny_reason(self.decide(command, cwd=cwd))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn(facts_doc()["workspace"], reason)

    def test_gh_and_ppl_from_inside_the_workspace_are_allowed(self):
        sub = os.path.join(real(WS), "notes")
        for cwd in (WS, real(WS), sub):
            for command in (GH + " auth status", PPL + " doctor --forge"):
                with self.subTest(cwd=cwd, command=command):
                    self.assertIsNone(self.decide(command, cwd=cwd))

    def test_pr_create_with_head_base_and_body_file_is_allowed(self):
        self.assertIsNone(self.decide(pr_create()))
        self.assertIsNone(self.decide(pr_create() + " --draft"))
        spaced = pr_create().replace("--head " + BRANCH, "--head=" + BRANCH).replace(
            "-R o/r", "-R=o/r")
        self.assertIsNone(self.decide(spaced))

    def test_the_cwd_deny_reason_names_the_workspace(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # the orchestrator learns where to cd back to from the denial itself
        for cwd in (REPO, None):
            with self.subTest(cwd=cwd):
                reason = self.deny_reason(self.decide(pr_create(), cwd=cwd))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(facts_doc()["workspace"], reason)

    def test_pr_create_from_a_cwd_inside_the_write_scope_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for cwd in (REPO, REPO + "/src"):
            with self.subTest(cwd=cwd):
                result = self.decide(pr_create(), cwd=cwd)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                self.assertIn("write", self.deny_reason(result))
        # an [os_sandbox] allow_write entry that holds the workspace
        scope = facts_doc()["write_scope"] + [real("/tmp")]
        with self.facts_text(json.dumps(facts_doc(write_scope=scope))):
            self.assertIn("write", self.deny_reason(self.decide(pr_create(), cwd=WS)))

    def test_a_gh_call_with_no_cwd_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        call = flagged(None, pr_create())
        del call["cwd"]
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(ppl_guard.decide(call)))

    def test_pr_create_against_another_repo_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for repo in ("evil/r", "O/R", "github.com/o/r", "https://github.com/o/r"):
            with self.subTest(repo=repo):
                result = self.decide(pr_create().replace("-R o/r", "-R " + repo))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_repeated_flag_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # gh takes the last value, so a second -R would name another repo
        for extra in (" -R evil/r", " --head other", " --base other"):
            with self.subTest(extra=extra):
                result = self.decide(pr_create() + extra)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_pr_create_without_head_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for gone in ("--head " + BRANCH, "--base main", "-R o/r", "--title 'GH-7 add the cart total'",
                     "--body-file " + scratch_file("pr-body.md")):
            with self.subTest(gone=gone):
                result = self.decide(pr_create().replace(gone, ""))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_pr_create_with_another_head_or_base_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for old, new in (("--head " + BRANCH, "--head main"), ("--head " + BRANCH, "--head o:x"),
                         ("--base main", "--base release")):
            with self.subTest(new=new):
                result = self.decide(pr_create().replace(old, new))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        with self.run_state(branch=None):
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(pr_create())))
        with self.run_state(branch="main"):
            command = pr_create().replace("--head " + BRANCH, "--head main")
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_a_head_that_is_not_a_plain_branch_name_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # the run's state is role-writable, so a branch it records as owner:branch, which gh
        # reads as another fork's branch, is refused on its own spelling
        for branch in ("evil:" + BRANCH, "o/" + BRANCH, "-x", "+" + BRANCH):
            with self.subTest(branch=branch), self.run_state(branch=branch):
                command = pr_create().replace("--head " + BRANCH, "--head=" + branch)
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("plain branch name", reason)

    def test_issue_comment_needs_a_body_file_under_the_scratch_dir(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        with self.run_state(stage="merge_watch", mr={"merged_sha": "f" * 40}):
            good = GH + " issue comment 7 -R o/r --body-file " + scratch_file("note.md")
            self.assertIsNone(self.decide(good))
            outside = (str(paths.home() / "scratch" / "GH-8" / "note.md"), "/tmp/note.md",
                       "note.md", scratch_file("../../os_sandbox.json"),
                       scratch_file("missing.md"), scratch_file(""), "-")
            for path in outside:
                with self.subTest(path=path):
                    command = GH + " issue comment 7 -R o/r --body-file " + path
                    self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))
            command = GH + " issue comment 7 -R o/r"
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))
            command = GH + " issue comment 7 -R o/r --body 'x'"
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_a_body_file_that_links_out_of_the_scratch_dir_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        # a role can write the scratch directory, so a link there can point gh at a secret
        secret = paths.home() / "secret.txt"
        secret.write_text("token\n", encoding="utf-8")
        links = {"soft.md": lambda path: os.symlink(str(secret), path),
                 "hard.md": lambda path: os.link(str(secret), path)}
        try:
            for name, make in links.items():
                make(scratch_file(name))
                with self.subTest(name=name):
                    command = pr_create().replace(scratch_file("pr-body.md"), scratch_file(name))
                    self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))
        finally:
            for name in links:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(scratch_file(name))
            secret.unlink()

    def test_a_run_scratch_dir_that_is_a_link_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        run_dir = paths.home() / "scratch" / TASK
        kept = paths.home() / "scratch" / "kept"
        elsewhere = paths.home() / "elsewhere"
        elsewhere.mkdir(exist_ok=True)
        (elsewhere / "pr-body.md").write_text("secret\n", encoding="utf-8")
        run_dir.rename(kept)
        os.symlink(str(elsewhere), str(run_dir))
        try:
            result = self.decide(pr_create())
        finally:
            os.unlink(str(run_dir))
            kept.rename(run_dir)
            shutil.rmtree(str(elsewhere))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_an_unknown_gh_verb_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for tail in ("co 12", "pr new -R o/r", "pr merge 12 -R o/r", "pr diff 12 -R o/r",
                     "issue close 7 -R o/r", "repo delete o/r", "extension install x/y",
                     "alias set co 'pr checkout'", "pr view 12 -R o/r --web",
                     "pr view 12 -R o/r --repo evil/r", "auth status --show-token",
                     "auth token", "pr close 12 -R o/r --comment bye",
                     "pr close 12 -R o/r --delete-branch", "pr view -R o/r", "pr view 12 13 -R o/r",
                     "pr view 012 -R o/r", "pr checks 12 -R o/r -- x", "issue view 7 -Ro/r"):
            with self.subTest(tail=tail):
                result = self.decide(GH + " " + tail)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_the_read_verbs_of_the_table_are_allowed(self):
        for tail in ("issue view 7 -R o/r", "issue view 7 -R o/r --comments --json=title,body",
                     "issue view 7 --json title -R o/r", "pr view 12 -R o/r --json=reviews,headRefOid",
                     "pr checks 12 -R o/r", "pr close 12 -R o/r", "auth status",
                     "api user", "api user --jq .login", "api user --jq=.login"):
            with self.subTest(tail=tail):
                self.assertIsNone(self.decide(GH + " " + tail))

    def test_the_tracker_repo_and_the_forge_repo_are_told_apart(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        with self.facts_text(json.dumps(facts_doc(tracker_repo="o/issues", forge_repo="o/code"))):
            self.assertIsNone(self.decide(GH + " issue view 7 -R o/issues"))
            self.assertIsNone(self.decide(GH + " pr view 12 -R o/code"))
            self.assertIn("BOUNDARY_VIOLATION",
                          self.deny_reason(self.decide(GH + " issue view 7 -R o/code")))
            self.assertIn("BOUNDARY_VIOLATION",
                          self.deny_reason(self.decide(GH + " pr view 12 -R o/issues")))
        with self.facts_text(json.dumps(facts_doc(tracker_repo=None))):
            self.assertIn("BOUNDARY_VIOLATION",
                          self.deny_reason(self.decide(GH + " issue view 7 -R o/r")))

    def test_api_graphql_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        command = GH + " api graphql -f query='query { viewer { login } }'"
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_the_reply_route_with_a_body_file_is_allowed(self):
        route = "repos/o/r/pulls/12/comments/345/replies"
        self.assertIsNone(self.decide(
            "%s api %s -F body=@%s" % (GH, route, scratch_file("reply.md"))))

    def test_the_reply_route_takes_nothing_but_its_body_file(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        route = "repos/o/r/pulls/12/comments/345/replies"
        body = "-F body=@" + scratch_file("reply.md")
        for tail in ("%s" % route, "%s -f body=hi" % route, "%s %s -X DELETE" % (route, body),
                     "%s -F body=@/tmp/reply.md" % route, "%s -F title=@%s" % (route, scratch_file("reply.md")),
                     "repos/evil/r/pulls/12/comments/345/replies " + body,
                     "/repos/o/r/pulls/12/comments/345/replies " + body,
                     "repos/o/r/pulls/12/comments/345/replies/x " + body,
                     "%s %s --jq .id" % (route, body), "%s %s %s" % (route, body, body)):
            with self.subTest(tail=tail):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(GH + " api " + tail)))

    def test_api_post_to_an_issues_route_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        body = scratch_file("note.md")
        for tail in ("repos/o/r/issues/7/comments -F body=@" + body,
                     "repos/o/r/issues/7 -X PATCH -f state=closed",
                     "user -X PATCH -f bio=x", "user --jq env", "user --jq '$ENV'"):
            with self.subTest(tail=tail):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(GH + " api " + tail)))

    def test_ppl_tracker_fetch_for_the_active_task_is_allowed(self):
        for command in (PPL + " tracker fetch --task GH-7", PPL + " tracker fetch --task=GH-7",
                        PPL + " review fetch --pr 12", PPL + " review fetch --pr=12",
                        PPL + " doctor --forge"):
            with self.subTest(command=command):
                self.assertIsNone(self.decide(command))

    def test_ppl_tracker_fetch_for_another_task_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for command in (PPL + " tracker fetch --task GH-8", PPL + " tracker fetch --tas GH-7",
                        PPL + " tracker fetch --task GH-7 --task GH-8",
                        PPL + " review fetch --pr x", PPL + " doctor --forge --fix",
                        "ppl tracker fetch --task GH-7"):
            with self.subTest(command=command):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))
        with self.no_active_run():
            result = self.decide(PPL + " tracker fetch --task GH-7")
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_ppl_init_unsandboxed_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for tail in ("init GH-8 https://github.com/o/r/issues/8 /tmp/ghrepo", "harness apply x",
                     "stage enter briefing", "doctor"):
            with self.subTest(tail=tail):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(PPL + " " + tail)))

    def test_any_other_binary_is_denied(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        for command in ("ls", "curl https://api.github.com", "env " + GH + " auth status",
                        "command " + GH + " auth status", "/usr/bin/git fetch"):
            with self.subTest(command=command):
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_decide_takes_a_run_state_value_as_well_as_the_reader(self):
        call = flagged(None, pr_create())
        self.assertIsNone(escape.decide(call, paths.home(), run.load(TASK)))
        self.assertIsNone(escape.decide(call, paths.home(), ppl_guard.active_state))
        # with no run, the run's branch and scratch directory are unknown
        self.assertIsNotNone(escape.decide(call, paths.home(), None))

    def test_tracker_gate_still_applies_to_an_allowed_issue_comment_before_merge(self):
        # rule: R-OS-SANDBOX-ORCHESTRATOR-SHAPE
        command = GH + " issue comment 7 -R o/r --body-file " + scratch_file("note.md")
        self.assertIsNone(escape.decide(flagged(None, command), paths.home(),
                                        ppl_guard.active_state))
        result = self.decide(command)
        self.assertIn("OWNER_DECISION", self.deny_reason(result))
        self.assertIn("merge", self.deny_reason(result))


GUARD = Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks" / "ppl_guard.py"
HOOK_BOUND_SECONDS = 10


@contextlib.contextmanager
def within(seconds):
    """Raises TimeoutError in the block once `seconds` pass, so a blocking open fails the
    test instead of hanging the suite."""
    def expired(signum, frame):
        raise TimeoutError("still blocked after %d s" % seconds)
    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class TestNoBlockingRead(OsSandboxCase):
    """runs/ is sandbox-writable, and a hook that blocks past its timeout denies nothing,
    so a FIFO a role leaves where a run file belongs is refused, never waited on."""

    @contextlib.contextmanager
    def fifo_at(self, path):
        saved = path.read_bytes() if path.exists() else None
        if os.path.lexists(str(path)):
            path.unlink()
        os.mkfifo(str(path))
        try:
            yield
        finally:
            path.unlink()
            if saved is not None:
                path.write_bytes(saved)

    def run_hook(self, call):
        """main() in its own process, as Claude Code runs it, held to a bound."""
        env = dict(os.environ, PPL_HOME=str(paths.home()))
        try:
            done = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(call),
                                  capture_output=True, text=True, env=env,
                                  timeout=HOOK_BOUND_SECONDS)
        except subprocess.TimeoutExpired:
            self.fail("the hook was still running after %d s" % HOOK_BOUND_SECONDS)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout) if done.stdout.strip() else None

    def test_a_fifo_at_active_does_not_hold_a_role_escape_deny(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        with self.fifo_at(paths.active_file()):
            result = self.run_hook(flagged("ppl-code-worker", "ls"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_fifo_at_the_run_state_does_not_hold_a_role_escape_deny(self):
        # rule: R-OS-SANDBOX-ROLE-NO-ESCAPE
        with self.fifo_at(run.state_path(TASK)):
            result = self.run_hook(flagged("ppl-test-worker", "ls"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_fifo_run_file_denies_the_orchestrator_without_blocking(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        for path in (paths.active_file(), run.state_path(TASK)):
            with self.subTest(path=path.name), self.fifo_at(path):
                result = self.run_hook(flagged(None, pr_create()))
                self.assertIn("OWNER_DECISION", self.deny_reason(result))
                result = self.run_hook(flagged("general-purpose", "ls", OTHER))
                self.assertIsNotNone(result)

    def test_run_files_that_are_not_regular_are_refused_without_blocking(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        from ppl.errors import PplError
        with self.fifo_at(paths.active_file()), within(5):
            with self.assertRaises(PplError):
                run.active()
        with self.fifo_at(run.state_path(TASK)), within(5):
            with self.assertRaises(PplError):
                store.read_json(run.state_path(TASK))
        self.assertEqual(run.active(), TASK)
        self.assertEqual(run.load(TASK)["task_id"], TASK)


# What a role can leave where a run file belongs: a sparse file of this size reads in about
# 19 s, past the hook's 5 s timeout, after which the hook decides nothing.
HUGE_BYTES = 2 * 1024 ** 3


class TestHugeRunFile(OsSandboxCase):
    """A run file a role made huge is refused unread, so every decision still comes in time."""

    @contextlib.contextmanager
    def huge(self, path):
        saved = path.read_bytes() if path.exists() else None
        with open(str(path), "wb") as handle:
            handle.truncate(HUGE_BYTES)
        try:
            yield
        finally:
            path.unlink()
            if saved is not None:
                path.write_bytes(saved)

    def main_decision(self, call):
        """main() in this process, as the hook runs it, with how long it took."""
        stdout = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(call))), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            started = time.monotonic()
            self.assertEqual(ppl_guard.main(), 0)
            took = time.monotonic() - started
        return (json.loads(stdout.getvalue()) if stdout.getvalue() else None), took

    def test_a_huge_active_file_leaves_a_role_write_decided_within_a_second(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        call = payload("ppl-code-worker", "Write", {"file_path": OTHER + "/x.txt",
                                                    "content": "x"}, REPO)
        with self.huge(paths.active_file()):
            result, took = self.main_decision(call)
        self.assertLess(took, 1)
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_huge_active_file_leaves_a_role_flagged_call_decided_within_a_second(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        with self.huge(paths.active_file()):
            result, took = self.main_decision(flagged("ppl-code-worker", "ls", REPO))
        self.assertLess(took, 1)
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_huge_active_file_denies_an_explore_flagged_call_within_a_second(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        # the refused read is a deny with its reason, never "no active run"
        with self.huge(paths.active_file()):
            result, took = self.main_decision(flagged("Explore", "ls", OTHER))
        self.assertLess(took, 1)
        self.assertIn("OWNER_DECISION", self.deny_reason(result))
        self.assertIn("ACTIVE", self.deny_reason(result))

    def test_a_flagged_call_against_a_huge_active_file_writes_nothing_to_stderr(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        env = dict(os.environ, PPL_HOME=str(paths.home()))
        with self.huge(paths.active_file()):
            for agent in (None, "Explore"):
                with self.subTest(agent=agent):
                    done = subprocess.run([sys.executable, str(GUARD)],
                                          input=json.dumps(flagged(agent, pr_create())),
                                          capture_output=True, text=True, env=env,
                                          timeout=HOOK_BOUND_SECONDS)
                    self.assertEqual(done.stderr, "")
                    self.assertIn("OWNER_DECISION", self.deny_reason(json.loads(done.stdout)))

    def test_huge_run_files_deny_a_flagged_call_within_a_second(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        for path in (run.state_path(TASK), paths.run_dir(TASK) / "harness.json",
                     paths.run_dir(TASK) / "control.json", facts_path()):
            for agent in (None, "Explore"):
                with self.subTest(path=path.name, agent=agent), self.huge(path):
                    result, took = self.main_decision(flagged(agent, pr_create()))
                    self.assertLess(took, 1)
                    if agent is None and path.name == "harness.json":
                        # the orchestrator's pr create reads no snapshot, and stays allowed
                        self.assertIsNone(result)
                        continue
                    self.assertIn(self.deny_reason(result).split(" ", 1)[0],
                                  ("BOUNDARY_VIOLATION", "OWNER_DECISION"))

    def test_a_run_file_over_the_cap_is_refused_unread(self):
        # rule: R-OS-SANDBOX-FAIL-CLOSED
        from ppl.errors import PplError
        with self.huge(run.state_path(TASK)), within(5):
            with self.assertRaises(PplError) as caught:
                run.load(TASK, limit=store.HOOK_READ_LIMIT)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        with self.huge(paths.active_file()), within(5):
            with self.assertRaises(PplError):
                run.active(limit=store.HOOK_READ_LIMIT)
        self.assertEqual(store.HOOK_READ_LIMIT, 1024 * 1024)

    def test_the_cap_is_the_hooks_alone(self):
        # a CLI read of a document past the cap and a long log stay readable
        big = paths.run_dir(TASK) / "big.json"
        big.write_text(json.dumps({"x": "y" * (2 * 1024 * 1024)}), encoding="utf-8")
        self.addCleanup(big.unlink)
        self.assertEqual(len(store.read_json(big)["x"]), 2 * 1024 * 1024)
        log = paths.run_dir(TASK) / "log.jsonl"
        saved = log.read_bytes() if log.exists() else None
        self.addCleanup(lambda: log.write_bytes(saved) if saved is not None else log.unlink())
        line = json.dumps({"event": "x", "pad": "p" * 1000}) + "\n"
        log.write_text(line * 1500, encoding="utf-8")
        self.assertEqual(len(store.read_log(TASK)), 1500)

    def test_the_role_mcp_webfetch_and_edit_checks_read_no_run_file(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        calls = (payload("ppl-code-worker", "mcp__github__create_pull_request", {}, REPO),
                 payload("ppl-verifier", "WebFetch", {"url": "https://github.com/o/r",
                                                       "prompt": "x"}, REPO),
                 payload("ppl-code-worker", "Write", {"file_path": OTHER + "/x.txt",
                                                      "content": "x"}, REPO))
        for call in calls:
            with self.subTest(tool=call["tool_name"]), \
                    mock.patch.object(ppl_guard.run, "active", return_value=TASK) as active:
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(ppl_guard._decide(call)))
                self.assertFalse(active.called, "ACTIVE was read first")


class TestPlainArgv(unittest.TestCase):
    def test_a_plain_command_splits_like_the_shell(self):
        self.assertEqual(escape.plain_argv("/opt/pin/gh pr view 12 --title 'a $b `c`' \"c d\""),
                         ["/opt/pin/gh", "pr", "view", "12", "--title", "a $b `c`", "c d"])
        self.assertEqual(escape.plain_argv("a \"it's\" b\\ c 'x'\"y\" d=e"),
                         ["a", "it's", "b c", "xy", "d=e"])

    def test_anything_the_shell_would_expand_is_not_plain(self):
        for command in ("", "   ", "a;b", "a $b", "a \"$b\"", "a `b`", "a > b", "a\nb", "X=1 a",
                        "a *", "a {b,c}", "a 'b", "a # b", "a\\;b", "a b#", "a ~", "a ^b",
                        "a !b", "a =b", "a 'b''c'"):
            with self.subTest(command=command):
                self.assertIsNone(escape.plain_argv(command))


class TestEscapeFacts(unittest.TestCase):
    def test_escape_facts_file_matches_os_sandbox(self):
        self.assertEqual(escape.FACTS_FILE, os_sandbox.FACTS_FILE)

    def test_load_facts_reads_what_apply_writes(self):
        self.assertIsNone(escape.load_facts(paths.home() / "no-such-dir"))
        self.assertEqual(escape.load_facts(paths.home())["pins"]["gh"]["path"], GH)


GIT_BIN = shutil.which("git") or "/usr/bin/git"


# The one push refspec the guard takes: the run branch to itself, both named in full.
SPEC = "refs/heads/%s:refs/heads/%s" % (BRANCH, BRANCH)
HEAD_REF = "refs/heads/" + BRANCH
# The words every push carries first: hooks, tags and submodules off whatever the config says.
PUSH_FLAGS = ("--no-verify", "--no-follow-tags", "--no-recurse-submodules")
# The -c words every fetch and push outside the OS sandbox carries first, in this order and
# alone: no hook and no ssh command, credential helper, askpass, gpg, alternate-refs or
# fsmonitor program of the repository's own, no automatic gc or maintenance, and ssh the one
# transport, whatever its config says by the time git runs.
PINNED_CONFIG = ("-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=ssh",
                 "-c", "credential.helper=", "-c", "core.askPass=",
                 "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never",
                 "-c", "protocol.allow=never", "-c", "protocol.ssh.allow=always",
                 "-c", "protocol.git.allow=never", "-c", "push.gpgSign=false", "-c", "gpg.program=",
                 "-c", "core.alternateRefsCommand=", "-c", "core.fsmonitor=false",
                 "-c", "gc.auto=0", "-c", "maintenance.auto=false")


def spec(name):
    return "refs/heads/%s:refs/heads/%s" % (name, name)


def git(directory, *args):
    """The test's own git in `directory`, which never meets the guard."""
    done = subprocess.run([GIT_BIN, "-C", str(directory)] + list(args),
                          capture_output=True, text=True)
    if done.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr.strip()))
    return done.stdout.strip()


def new_repo(path, branch="main"):
    """A repository at `path` on `branch` with one commit and origin on the forge repo o/r,
    made with no template, so its local config holds only what git init and remote add
    write."""
    git(os.path.dirname(path), "init", "-q", "--template=", "-b", branch, path)
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
        "commit", "-q", "--allow-empty", "-m", "base")
    git(path, "remote", "add", "origin", "https://github.com/o/r.git")
    return path


class GitShapes(OsSandboxCase):
    """The orchestrator's git outside the OS sandbox, on real repositories: the checkout on the
    default branch, the run's repo a worktree of it on the run's branch, origin on the forge
    repo, and the git on PATH pinned."""

    def setUp(self):
        super().setUp()
        self.root = real(tempfile.mkdtemp(prefix="ppl-git-shapes-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.checkout = new_repo(os.path.join(self.root, "checkout"))
        self.repo = os.path.join(self.root, "worktrees", "r-" + TASK)
        git(self.checkout, "worktree", "add", "-q", "-b", BRANCH, self.repo)
        self.write_facts()
        state = run.load(TASK)
        saved = dict(state)
        state["repo"] = self.repo
        run.save(state)
        self.addCleanup(run.save, saved)

    def write_facts(self, git_path=GIT_BIN, **overrides):
        pins = dict(facts_doc()["pins"], git={"path": git_path, "realpath": real(git_path)})
        store.write_json(facts_path(), facts_doc(checkout=self.checkout, pins=pins, **overrides))

    def command(self, directory, *words, git_path=GIT_BIN):
        return " ".join([git_path] + list(PINNED_CONFIG) + ["-C", directory] + list(words))

    def push(self, *words):
        return self.command(self.repo, "push", *(PUSH_FLAGS + words))

    def fetch(self, *words):
        return self.command(self.checkout, "fetch", "--no-recurse-submodules", *words)

    def test_push_of_the_run_branch_is_allowed(self):
        for words in (("origin", SPEC), ("-u", "origin", SPEC), ("--set-upstream", "origin", SPEC)):
            with self.subTest(words=words):
                self.assertIsNone(self.decide(self.push(*words)))

    def test_branch_delete_of_the_run_branch_is_allowed(self):
        self.assertIsNone(self.decide(self.push("origin", "--delete", HEAD_REF)))

    def test_a_bare_branch_refspec_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # git takes the destination of a bare or unqualified refspec from config a role can
        # write, branch.<b>.merge under push.default=upstream, so a push names both refs in full
        for words in (("origin", BRANCH), ("-u", "origin", BRANCH),
                      ("origin", BRANCH + ":" + BRANCH), ("origin", HEAD_REF),
                      ("origin", BRANCH + ":" + HEAD_REF), ("origin", HEAD_REF + ":" + BRANCH),
                      ("origin", "HEAD:" + HEAD_REF), ("origin", "--delete", BRANCH),
                      ("origin", "--delete", "heads/" + BRANCH)):
            with self.subTest(words=words):
                reason = self.deny_reason(self.decide(self.push(*words)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("refs/heads/<branch>", reason)

    def test_a_branch_merge_key_cannot_steer_a_qualified_push(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # branch.<b>.merge stays a safe key: -u writes it, and a full refspec never reads it
        git(self.checkout, "config", "branch.%s.remote" % BRANCH, "origin")
        git(self.checkout, "config", "branch.%s.merge" % BRANCH, "refs/heads/main")
        self.assertIsNone(self.decide(self.push("origin", SPEC)))
        self.assertIsNone(self.decide(self.push("-u", "origin", SPEC)))
        reason = self.deny_reason(self.decide(self.push("origin", BRANCH)))
        self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_a_push_without_no_follow_tags_or_no_recurse_submodules_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # the operator's global config may follow tags or recurse into submodules, which the
        # guard cannot read, and only the command line overrides it
        for flags in (("--no-verify",), ("--no-verify", "--no-follow-tags"),
                      ("--no-verify", "--no-recurse-submodules"),
                      ("--no-verify", "--no-recurse-submodules", "--no-follow-tags"),
                      ("--no-follow-tags", "--no-verify", "--no-recurse-submodules"),
                      PUSH_FLAGS + ("--no-follow-tags",),
                      ("--no-verify", "--no-follow-tags", "--recurse-submodules=no")):
            for tail in (("origin", SPEC), ("-u", "origin", SPEC), ("origin", "--delete", HEAD_REF)):
                command = self.command(self.repo, "push", *(flags + tail))
                with self.subTest(command=command):
                    reason = self.deny_reason(self.decide(command))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn(" ".join(PUSH_FLAGS), reason)

    def test_the_accepted_push_lands_only_the_run_branch_whatever_the_global_config(self):
        # the guard allows the words, then git runs the same words against a local stand-in
        # for the forge repo, under a global config the guard cannot read that follows tags,
        # recurses into submodules and pushes to the upstream, with a role's merge key on main
        # and a role's annotated tag on the run branch
        from ppl import escape_git
        forge = os.path.join(self.root, "forge.git")
        git(self.root, "init", "-q", "--bare", "--template=", forge)
        git(self.checkout, "config", "branch.%s.remote" % BRANCH, "origin")
        git(self.checkout, "config", "branch.%s.merge" % BRANCH, "refs/heads/main")
        identity = ("-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false")
        git(self.repo, *(identity + ("commit", "-q", "--allow-empty", "-m", "work")))
        git(self.repo, *(identity + ("tag", "-a", "v9.9.9", "-m", "release")))
        global_config = os.path.join(self.root, "global.gitconfig")
        with open(global_config, "w", encoding="utf-8") as handle:
            handle.write("[push]\n\tfollowTags = true\n\trecurseSubmodules = on-demand\n"
                         "\tdefault = upstream\n[url \"%s\"]\n"
                         "\tinsteadOf = https://github.com/o/r.git\n" % forge)
        argv = escape.plain_argv(self.push("origin", SPEC))
        self.assertIsNone(escape_git.git_shape(argv, escape.load_facts(paths.home()),
                                               run.load(TASK)))
        # the stand-in is a local path, which the pinned words refuse, so the file transport
        # is let through here as the network one is for the real forge
        env = dict(os.environ, GIT_CONFIG_GLOBAL=global_config, GIT_CONFIG_NOSYSTEM="1",
                   GIT_ALLOW_PROTOCOL="file")
        done = subprocess.run(argv, capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(git(forge, "for-each-ref", "--format=%(refname)"), HEAD_REF)

    def test_fetch_of_the_default_branch_from_the_checkout_is_allowed(self):
        self.assertIsNone(self.decide(self.fetch("origin", "main")))
        for url in ("https://github.com/o/r", "git@github.com:o/r.git", "git@github.com:o/r"):
            git(self.checkout, "remote", "set-url", "origin", url)
            with self.subTest(url=url):
                self.assertIsNone(self.decide(self.fetch("origin", "main")))
        # a fetch reads the guard facts only, so it needs no run
        with self.no_active_run():
            self.assertIsNone(self.decide(self.fetch("origin", "main")))

    def test_a_fetch_without_no_recurse_submodules_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # a submodule the checkout holds would be fetched with its own config, which the
        # guard does not read
        for words in (("origin", "main"), ("origin", "main", "--no-recurse-submodules"),
                      ("--recurse-submodules=no", "origin", "main"),
                      ("--no-recurse-submodules", "--no-recurse-submodules", "origin", "main"),
                      ("--no-recurse-submodules", "upstream", "main")):
            with self.subTest(words=words):
                command = self.command(self.checkout, "fetch", *words)
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("--no-recurse-submodules origin main", reason)

    def test_the_fetch_allow_entry_is_the_shape_the_guard_allows(self):
        # the rendered allow entry and the guard's fetch shape are one string
        harness = {"repo": {"path": self.checkout, "default_branch": "main"},
                   "forge": {"kind": "github"}, "tracker": {"kind": "github-issues"}}
        pins = dict(facts_doc()["pins"], git={"path": GIT_BIN, "realpath": real(GIT_BIN)})
        entries = [entry[len("Bash("):-1]
                   for entry in os_sandbox.profile(harness, WS, pins)["allow"]
                   if entry.startswith("Bash(%s " % GIT_BIN)]
        self.assertEqual(entries, [self.fetch("origin", "main")])
        self.assertIsNone(self.decide(entries[0]))

    def test_gh_resolved_in_local_config_is_tolerated(self):
        # gh records the remote it resolved there, and it runs nothing
        git(self.checkout, "config", "remote.origin.gh-resolved", "base")
        self.assertIsNone(self.decide(self.push("origin", SPEC)))
        self.assertIsNone(self.decide(self.fetch("origin", "main")))

    def test_core_hookspath_in_local_config_is_tolerated(self):
        # the -c on the command line wins over it
        git(self.checkout, "config", "core.hooksPath", ".githooks")
        self.assertIsNone(self.decide(self.push("origin", SPEC)))
        self.assertIsNone(self.decide(self.fetch("origin", "main")))

    def test_a_push_of_the_default_branch_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for words in (("origin", spec("main")), ("-u", "origin", spec("main")),
                      ("origin", "--delete", "refs/heads/main"), ("origin", spec("Main"))):
            with self.subTest(words=words):
                reason = self.deny_reason(self.decide(self.push(*words)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("default branch", reason)
        # from a repo whose HEAD is on it as well
        with self.run_state(repo=self.checkout):
            command = self.command(self.checkout, "push", *(PUSH_FLAGS + ("origin", spec("main"))))
            self.assertIn("default branch", self.deny_reason(self.decide(command)))

    def test_a_colon_refspec_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # the one colon taken is the one between refs/heads/<branch> and itself
        for refspec in (BRANCH + ":main", ":main", "HEAD:main", ":refs/heads/main",
                        "refs/heads/%s:refs/heads/main" % BRANCH, SPEC + ":x"):
            with self.subTest(refspec=refspec):
                reason = self.deny_reason(self.decide(self.push("origin", refspec)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("refs/heads/<branch>:refs/heads/<branch>", reason)
        reason = self.deny_reason(self.decide(self.push("origin", "--delete", HEAD_REF + ":x")))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("':'", reason)
        reason = self.deny_reason(self.decide(self.fetch("origin", "main:main")))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("fetch", reason)

    def test_a_plus_refspec_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for words in (("origin", "+" + SPEC), ("-u", "origin", "+" + SPEC),
                      ("origin", "--delete", "+" + HEAD_REF)):
            with self.subTest(words=words):
                reason = self.deny_reason(self.decide(self.push(*words)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("'+'", reason)
        reason = self.deny_reason(self.decide(self.fetch("origin", "+main")))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("fetch", reason)

    def test_a_detached_head_push_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        git(self.repo, "checkout", "-q", "--detach")
        for words in (("origin", SPEC), ("origin", "--delete", HEAD_REF)):
            with self.subTest(words=words):
                reason = self.deny_reason(self.decide(self.push(*words)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("not on a branch", reason)

    def test_a_branch_head_is_not_on_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        git(self.checkout, "branch", "gh-7-other")
        reason = self.deny_reason(self.decide(self.push("origin", spec("gh-7-other"))))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("refs/heads/" + BRANCH, reason)
        # a HEAD on a tag names no branch, even when the word matches it
        git(self.repo, "tag", "gh-7-tag")
        git(self.repo, "symbolic-ref", "HEAD", "refs/tags/gh-7-tag")
        reason = self.deny_reason(self.decide(self.push("origin", spec("gh-7-tag"))))
        self.assertIn("not on a branch", reason)

    def test_a_branch_named_like_a_ref_namespace_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # with no local main, git reads heads/main as the remote's refs/heads/main, so a
        # delete of the run branch would delete the default branch on GitHub
        repo = new_repo(os.path.join(self.root, "solo"), branch="heads/main")
        with self.run_state(repo=repo):
            for words in (("origin", spec("heads/main")),
                          ("origin", "--delete", "refs/heads/heads/main")):
                with self.subTest(words=words):
                    command = self.command(repo, "push", *(PUSH_FLAGS + words))
                    reason = self.deny_reason(self.decide(command))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("heads/main", reason)
        for name in ("refs/heads/main", "tags/v1", "HEAD", "FETCH_HEAD", "head", "-f", ".x",
                     "a@{1}"):
            with self.subTest(name=name):
                command = self.push("origin", "--delete", "refs/heads/" + name)
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_a_push_without_the_hooks_off_flag_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        tail = " push %s origin %s" % (" ".join(PUSH_FLAGS), SPEC)
        for command, part in (
                ("%s -C %s%s" % (GIT_BIN, self.repo, tail), "core.hooksPath=/dev/null"),
                ("%s -c core.hooksPath=/tmp/hooks -C %s%s" % (GIT_BIN, self.repo, tail),
                 "core.hooksPath=/dev/null"),
                ("%s -c core.hookspath=/dev/null -C %s%s" % (GIT_BIN, self.repo, tail),
                 "core.hooksPath=/dev/null"),
                ("%s -C %s -c core.hooksPath=/dev/null%s" % (GIT_BIN, self.repo, tail),
                 "core.hooksPath=/dev/null"),
                (self.command(self.repo, "push", "origin", SPEC), "--no-verify"),
                (self.command(self.repo, "push", "-u", "--no-verify", "origin", SPEC),
                 "--no-verify")):
            with self.subTest(command=command):
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(part, reason)

    def test_another_dash_c_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        tail = " push %s origin %s" % (" ".join(PUSH_FLAGS), SPEC)
        for middle in ("-c core.sshCommand=/tmp/evil -C " + self.repo,
                       "-c core.hooksPath=/dev/null -C " + self.repo,
                       "-c protocol.ext.allow=always -C " + self.repo,
                       "-C %s -c core.sshCommand=/tmp/evil" % self.repo,
                       "--config-env=core.sshCommand=HOME -C " + self.repo,
                       "-C %s -C %s" % (self.repo, self.repo)):
            command = " ".join((GIT_BIN,) + PINNED_CONFIG) + " " + middle + tail
            with self.subTest(middle=middle):
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("those -c words first", reason)

    def test_a_shape_missing_any_pinned_config_word_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # each -c pair keeps one key of the repository's config, which a role can rewrite
        # between the guard's check and the call, from running a program with the credential
        pairs = [PINNED_CONFIG[index:index + 2] for index in range(0, len(PINNED_CONFIG), 2)]
        variants = [sum(pairs[:index] + pairs[index + 1:], ()) for index in range(len(pairs))]
        variants.append(sum([pairs[1], pairs[0]] + pairs[2:], ()))
        variants.append(sum(pairs[:-1], ()) + ("-c", "protocol.file.allow=always"))
        variants.append(sum(pairs[:2], ()) + ("-c", "credential.helper=/tmp/evil")
                        + sum(pairs[3:], ()))
        for config in variants:
            for directory, words in ((self.repo, ("push",) + PUSH_FLAGS + ("origin", SPEC)),
                                     (self.checkout, ("fetch", "--no-recurse-submodules",
                                                      "origin", "main"))):
                command = " ".join((GIT_BIN,) + config + ("-C", directory) + words)
                with self.subTest(command=command):
                    reason = self.deny_reason(self.decide(command))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("those -c words first", reason)

    def test_the_shapes_carry_the_pinned_config_word_for_word(self):
        from ppl import escape_git
        self.assertEqual(list(PINNED_CONFIG), escape_git.PINNED_CONFIG)
        pins = dict(facts_doc()["pins"], git={"path": GIT_BIN, "realpath": real(GIT_BIN)})
        prefix = " ".join((GIT_BIN,) + PINNED_CONFIG) + " -C "
        harness = {"repo": {"path": self.checkout, "default_branch": "main"},
                   "forge": {"kind": "github"}, "tracker": {"kind": "github-issues"}}
        for command in (os_sandbox.fetch_command(harness, pins), os_sandbox.push_command(pins),
                        os_sandbox.push_command(pins, delete=True)):
            with self.subTest(command=command):
                self.assertTrue(command.startswith(prefix), command)

    def test_push_and_fetch_options_outside_the_shape_are_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for words in (("--force", "origin", SPEC), ("-f", "origin", SPEC),
                      ("origin", SPEC, "--force"), ("--force-with-lease", "origin", SPEC),
                      ("--mirror", "origin"), ("--all", "origin"), ("--tags", "origin", SPEC),
                      ("--exec=/tmp/evil", "origin", SPEC),
                      ("--receive-pack=/tmp/evil", "origin", SPEC), ("--repo=origin", SPEC),
                      ("-d", "origin", HEAD_REF), ("origin", "-d", HEAD_REF),
                      ("origin", SPEC, SPEC), ("origin", "--", SPEC),
                      ("-u", "origin", "--delete", HEAD_REF), ("origin", SPEC, "-u"),
                      ("origin",), ()):
            with self.subTest(push=words):
                reason = self.deny_reason(self.decide(self.push(*words)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
        for words in (("--upload-pack=/tmp/evil", "origin", "main"),
                      ("origin", "main", "--upload-pack=/tmp/evil"), ("--all",), ("origin",),
                      ("origin", "release"), ("origin", "main", BRANCH),
                      ("--prune", "origin", "main"), ("origin", "main", "--recurse-submodules")):
            with self.subTest(fetch=words):
                reason = self.deny_reason(self.decide(self.fetch(*words)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
        for verb in ("pull", "clone", "remote", "config", "ls-remote"):
            with self.subTest(verb=verb):
                command = self.command(self.repo, verb, "origin")
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(command)))

    def test_a_push_or_fetch_in_another_directory_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        from ppl import escape_git

        def never(argv, **kwargs):
            raise AssertionError("git ran: %r" % (argv,))

        facts = escape.load_facts(paths.home())
        for command, part in (
                (self.command(self.checkout, "push", *(PUSH_FLAGS + ("origin", SPEC))),
                 "run's repo"),
                (self.command(self.repo + "/", "push", *(PUSH_FLAGS + ("origin", SPEC))),
                 "run's repo"),
                (self.command("worktrees/r-GH-7", "push", *(PUSH_FLAGS + ("origin", SPEC))),
                 "run's repo"),
                (self.command(self.repo, "fetch", "origin", "main"), "checkout"),
                (self.command(self.root, "fetch", "origin", "main"), "checkout")):
            with self.subTest(command=command):
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(part, reason)
                # git is never run on a directory the guard did not pick
                self.assertIsNotNone(escape_git.git_shape(
                    escape.plain_argv(command), facts, run.load(TASK), run_git=never))

    def test_core_sshcommand_in_local_config_denies(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        git(self.checkout, "config", "core.sshCommand", "/tmp/evil")
        for command in (self.push("origin", SPEC), self.push("origin", "--delete", HEAD_REF),
                        self.fetch("origin", "main")):
            with self.subTest(command=command):
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("core.sshcommand", reason)

    def test_an_include_in_local_config_denies(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # the included file sets nothing unsafe: the include itself is what denies
        included = os.path.join(self.root, "included")
        with open(included, "w", encoding="utf-8") as handle:
            handle.write("[user]\n\tname = t\n")
        for key in ("include.path", "includeIf.gitdir:/.path"):
            with self.subTest(key=key):
                git(self.checkout, "config", key, included)
                reason = self.deny_reason(self.decide(self.push("origin", SPEC)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(key.lower(), reason)
                git(self.checkout, "config", "--unset", key)

    def test_other_keys_in_local_config_deny(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for key, value in (("url.https://evil.example/.insteadOf", "https://github.com/"),
                           ("url.https://evil.example/.pushInsteadOf", "https://github.com/"),
                           ("remote.origin.pushurl", "https://evil.example/x.git"),
                           ("remote.origin.receivepack", "/tmp/evil"),
                           ("remote.origin.uploadpack", "/tmp/evil"),
                           ("remote.origin.push", "refs/heads/*:refs/heads/main"),
                           ("remote.origin.vcs", "evil"),
                           ("credential.helper", "!/tmp/evil"),
                           ("core.fsmonitor", "/tmp/evil"),
                           ("core.gitProxy", "/tmp/evil"),
                           ("http.proxy", "http://evil.example"),
                           ("protocol.ext.allow", "always"),
                           ("submodule.x.url", "https://evil.example/x.git"),
                           ("core.bareX", "true")):
            with self.subTest(key=key):
                git(self.checkout, "config", key, value)
                reason = self.deny_reason(self.decide(self.push("origin", SPEC)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(key.lower(), reason)
                git(self.checkout, "config", "--unset", key)

    def test_a_worktree_level_config_key_denies(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        git(self.checkout, "config", "extensions.worktreeConfig", "true")
        self.assertIsNone(self.decide(self.push("origin", SPEC)))
        git(self.repo, "config", "--worktree", "core.sshCommand", "/tmp/evil")
        reason = self.deny_reason(self.decide(self.push("origin", SPEC)))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("worktree", reason)
        self.assertIn("core.sshcommand", reason)

    def test_a_remote_url_to_another_repo_denies(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for url in ("https://github.com/evil/r.git", "https://github.com/o/r2",
                    "https://github.com/O/R.git", "https://evil.example/o/r.git",
                    "git@evil.example:o/r.git", "ssh://git@github.com/o/r.git",
                    "/tmp/elsewhere.git", "ext::sh -c id"):
            git(self.checkout, "remote", "set-url", "origin", url)
            for command in (self.push("origin", SPEC), self.fetch("origin", "main")):
                with self.subTest(url=url, command=command):
                    reason = self.deny_reason(self.decide(command))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("forge repo o/r", reason)
        # a second url, which a push reaches as well
        git(self.checkout, "remote", "set-url", "origin", "https://github.com/o/r.git")
        git(self.checkout, "config", "--add", "remote.origin.url", "https://evil.example/x.git")
        reason = self.deny_reason(self.decide(self.push("origin", SPEC)))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("evil.example", reason)
        # the same url twice is still two
        git(self.checkout, "config", "--replace-all", "remote.origin.url",
            "https://github.com/o/r.git")
        git(self.checkout, "config", "--add", "remote.origin.url", "https://github.com/o/r.git")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self.decide(self.push("origin", SPEC))))

    def test_a_remote_the_config_does_not_name_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for remote in ("upstream", "Origin", "https://github.com/o/r.git",
                       "git@github.com:o/r.git", "/tmp/elsewhere.git", "-v"):
            with self.subTest(remote=remote):
                reason = self.deny_reason(self.decide(self.push(remote, SPEC)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("origin", reason)

    def test_git_without_a_forge_repo_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        self.write_facts(forge_repo=None)
        for command in (self.push("origin", SPEC), self.fetch("origin", "main")):
            with self.subTest(command=command):
                reason = self.deny_reason(self.decide(command))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("forge repo", reason)

    def test_a_push_with_no_active_run_is_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        with self.no_active_run():
            for words in (("origin", SPEC), ("origin", "--delete", HEAD_REF)):
                with self.subTest(words=words):
                    reason = self.deny_reason(self.decide(self.push(*words)))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn("active run", reason)

    def test_a_hung_git_denies(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        from ppl import escape_git
        timeouts = []

        def hung(argv, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        facts = escape.load_facts(paths.home())
        for command in (self.push("origin", SPEC), self.fetch("origin", "main")):
            with self.subTest(command=command):
                result = escape_git.git_shape(escape.plain_argv(command), facts, run.load(TASK),
                                              run_git=hung)
                self.assertIsNotNone(result)
                self.assertIn("0.2 s", result)
        self.assertEqual(timeouts, [0.2, 0.2])

    def test_a_slow_pinned_git_is_cut_off_and_denied(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        slow = os.path.join(self.root, "slow-git")
        with open(slow, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexec sleep 5\n")
        os.chmod(slow, 0o755)
        self.write_facts(git_path=slow)
        command = self.command(self.repo, "push", *(PUSH_FLAGS + ("origin", SPEC)),
                               git_path=slow)
        started = time.monotonic()
        reason = self.deny_reason(self.decide(command))
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("0.2 s", reason)

    def test_a_git_error_never_reaches_the_hook_stderr(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # git says "fatal: ref HEAD is not a symbolic ref" on a detached HEAD
        git(self.repo, "checkout", "-q", "--detach")
        env = dict(os.environ, PPL_HOME=str(paths.home()))
        done = subprocess.run([sys.executable, str(GUARD)],
                              input=json.dumps(flagged(None, self.push("origin", SPEC))),
                              capture_output=True, text=True, env=env, timeout=HOOK_BOUND_SECONDS)
        self.assertEqual(done.stderr, "")
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(json.loads(done.stdout)))


class TestPinnedGitConfigHolds(unittest.TestCase):
    """What git itself does with the pinned -c words when a role rewrote the repository's
    local config after the guard read it: a real git, a throwaway bare remote standing in for
    the forge and markers a program touches when it runs. The environment is scrubbed, since
    GIT_SSH_COMMAND, GIT_ASKPASS and GIT_ALLOW_PROTOCOL outrank -c and the operator's own
    config is not the test's. The pinned words allow ssh alone, so a test that reaches the
    local stand-in sets GIT_ALLOW_PROTOCOL=file, a test-only override that lets the file
    transport through as the network lets ssh through for the real forge."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("no git on PATH")
        self.root = real(tempfile.mkdtemp(prefix="ppl-pinned-config-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.marks = os.path.join(self.root, "marks")
        os.mkdir(self.marks)
        self.bin = os.path.join(self.root, "bin")
        os.mkdir(self.bin)
        # the ssh the operator's PATH holds: it only says it ran
        self.script("ssh", "touch %s/operator-ssh\nexit 255\n" % self.marks)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("GIT_", "SSH_"))}
        self.env.update(HOME=self.root, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0",
                        GIT_CONFIG_GLOBAL=os.path.join(self.root, "global.gitconfig"),
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""))
        with open(self.env["GIT_CONFIG_GLOBAL"], "w", encoding="utf-8") as handle:
            handle.write("[user]\n\tname = t\n\temail = t@example.com\n"
                         "[commit]\n\tgpgSign = false\n")
        self.forge = os.path.join(self.root, "forge.git")
        self.git(self.root, "init", "-q", "--bare", "--template=", self.forge)
        # a forge that takes signed pushes, so a push signs when the config says so
        self.git(self.forge, "config", "receive.certNonceSeed", "seed")
        alternate = os.path.join(self.root, "alternate")
        self.git(self.root, "init", "-q", "--template=", "-b", BRANCH, alternate)
        self.git(alternate, "commit", "-q", "--allow-empty", "-m", "alternate")
        self.work = os.path.join(self.root, "work")
        self.git(self.root, "init", "-q", "--template=", "-b", BRANCH, self.work)
        # three packs, past the gc.autoPackLimit a role sets below
        for name in ("a", "b", "c"):
            with open(os.path.join(self.work, name), "w", encoding="utf-8") as handle:
                handle.write(name + "\n")
            self.git(self.work, "add", name)
            self.git(self.work, "commit", "-q", "-m", name)
            self.git(self.work, "repack", "-q")
        self.git(self.work, "remote", "add", "origin", self.forge)
        self.git(self.work, "push", "-q", "origin", SPEC, local=True)
        self.git(self.work, "commit", "-q", "--allow-empty", "-m", "work")
        with open(os.path.join(self.work, ".git", "objects", "info", "alternates"), "w",
                  encoding="utf-8") as handle:
            handle.write(os.path.join(alternate, ".git", "objects") + "\n")
        hooks = os.path.join(self.root, "hooks")
        os.mkdir(hooks)
        for hook in ("pre-push", "pre-auto-gc", "reference-transaction"):
            self.script(os.path.join(hooks, hook), "touch %s/hook-%s\n" % (self.marks, hook))
        # what a role's detached process writes once the guard has read the config
        for key, value in (("core.hooksPath", hooks),
                           ("core.sshCommand", "touch %s/ssh-command #" % self.marks),
                           ("credential.helper", "!touch %s/credential-helper" % self.marks),
                           ("core.askPass", self.marker_script("askpass", "echo x\n")),
                           ("protocol.ext.allow", "always"),
                           ("protocol.file.allow", "always"),
                           ("protocol.git.allow", "always"),
                           ("push.gpgSign", "true"),
                           ("gpg.program", self.marker_script("gpg", "cat >/dev/null\nexit 1\n")),
                           ("core.alternateRefsCommand", self.marker_script("alternate-refs")),
                           ("core.fsmonitor", self.marker_script("fsmonitor")),
                           ("gc.auto", "1"), ("gc.autoPackLimit", "1"),
                           ("gc.autoDetach", "false"), ("maintenance.auto", "true"),
                           ("maintenance.autoDetach", "false")):
            self.git(self.work, "config", key, value)

    def script(self, name, body):
        path = name if os.path.isabs(name) else os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def marker_script(self, mark, tail=""):
        return self.script("raced-" + mark, "touch %s/%s\n%s" % (self.marks, mark, tail))

    def git(self, directory, *args, pinned=False, local=False, stdin=None, check=True):
        from ppl import escape_git
        argv = (["git"] + (escape_git.PINNED_CONFIG if pinned else [])
                + ["-C", directory] + list(args))
        env = dict(self.env, GIT_ALLOW_PROTOCOL="file") if local else self.env
        done = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env,
                              timeout=60)
        if check and done.returncode != 0:
            raise AssertionError("git %s failed: %s" % (" ".join(args), done.stderr.strip()))
        return done

    def push(self, *words, pinned=True, local=False):
        from ppl import escape_git
        return self.git(self.work, *(["push"] + escape_git.PUSH_FLAGS + list(words)),
                        pinned=pinned, local=local, check=False)

    def fetch(self, pinned=True, local=False):
        from ppl import escape_git
        return self.git(self.work, "fetch", escape_git.FETCH_FLAG, "origin", BRANCH,
                        pinned=pinned, local=local, check=False)

    def set_origin(self, url):
        self.git(self.work, "remote", "set-url", "origin", url)

    def marks_left(self):
        return sorted(os.listdir(self.marks))

    def clear_marks(self):
        for name in os.listdir(self.marks):
            os.unlink(os.path.join(self.marks, name))

    def test_the_raced_config_runs_its_programs_without_the_pinned_words(self):
        # the control: the same config and the same commands, no pinned words
        self.assertNotEqual(self.push("origin", SPEC, pinned=False, local=True).returncode, 0)
        self.assertIn("gpg", self.marks_left())
        self.clear_marks()
        self.assertEqual(self.fetch(pinned=False, local=True).returncode, 0)
        # git 2.46 also runs the fsmonitor and the pre-auto-gc hook here, through its automatic
        # maintenance, which other versions may time otherwise, so only this mark is required
        self.assertIn("alternate-refs", self.marks_left())
        self.clear_marks()
        self.set_origin("ext::sh -c touch% " + os.path.join(self.marks, "ext"))
        self.assertNotEqual(self.push("origin", SPEC, pinned=False).returncode, 0)
        self.assertIn("ext", self.marks_left())
        self.git(self.work, "credential", "fill", check=False,
                 stdin="protocol=https\nhost=example.invalid\n\n")
        self.assertIn("credential-helper", self.marks_left())
        self.assertIn("askpass", self.marks_left())
        self.clear_marks()
        self.set_origin("git://example.invalid/o/r.git")
        self.git(self.work, "config", "core.gitProxy", self.marker_script("git-proxy"))
        self.push("origin", SPEC, pinned=False)
        self.assertIn("git-proxy", self.marks_left())

    def test_the_pinned_words_are_accepted_on_fetch_push_and_delete(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        # every pinned word, the empty values included, is one git takes, and none of the
        # raced config's programs runs on the way
        done = self.push("-u", "origin", SPEC, local=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.fetch(local=True).returncode, 0)
        self.git(self.work, "branch", "-q", "gone", pinned=True)
        gone = "refs/heads/gone"
        self.assertEqual(self.push("origin", gone + ":" + gone, local=True).returncode, 0)
        done = self.push("origin", "--delete", gone, local=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.git(self.forge, "for-each-ref", "--format=%(refname)").stdout,
                         HEAD_REF + "\n")
        self.assertFalse(self.marks_left())

    def test_the_pinned_words_refuse_every_transport_but_ssh(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        for url, transport in (("ext::sh -c touch% " + os.path.join(self.marks, "ext"), "ext"),
                               (self.forge, "file"), ("git://example.invalid/o/r.git", "git"),
                               ("https://github.com/o/r.git", "https")):
            with self.subTest(transport=transport):
                self.set_origin(url)
                self.git(self.work, "config", "core.gitProxy", self.marker_script("git-proxy"))
                for done in (self.push("origin", SPEC), self.fetch()):
                    self.assertNotEqual(done.returncode, 0)
                    self.assertIn("transport '%s' not allowed" % transport, done.stderr)
                self.assertFalse(self.marks_left())
        self.assertEqual(self.git(self.forge, "for-each-ref", "--format=%(refname)").stdout,
                         HEAD_REF + "\n")

    def test_the_pinned_words_run_no_helper_askpass_or_ssh_command(self):
        # rule: R-OS-SANDBOX-GIT-CONFIG
        done = self.git(self.work, "credential", "fill", pinned=True, check=False,
                        stdin="protocol=https\nhost=example.invalid\n\n")
        self.assertNotEqual(done.returncode, 0)
        self.set_origin("ssh://git@127.0.0.1:1/o/r.git")
        self.assertNotEqual(self.push("origin", SPEC).returncode, 0)
        self.assertNotEqual(self.fetch().returncode, 0)
        # the ssh on the operator's PATH ran, the repository's own programs did not
        self.assertEqual(self.marks_left(), ["operator-ssh"])
        for mark in ("ssh-command", "credential-helper", "askpass", "gpg"):
            self.assertNotIn(mark, self.marks_left())


# What the guard says when an edit tool aims at a path an unsandboxed command reads.
PROTECTED = "which a command outside the OS sandbox reads or runs"
# What it says when an edit tool aims outside the OS sandbox's write scope.
OUTSIDE_SCOPE = "writes only where sandboxed Bash may"
# What it says for a path under the sandbox profile's denyWrite.
DENY_WRITE = "which the OS sandbox profile denies sandboxed Bash to write"
# What it says for a .git directory or file inside the write scope.
GIT_DIR = "a .git directory or the .git file of a worktree"
EDIT_TOOLS = (("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"),
              ("NotebookEdit", "notebook_path"))


class ProtectedPathCase(OsSandboxCase):
    """Edit tools against a temporary HOME that holds the git, ssh and gh config, so the
    operator's own files are never the ones in play."""

    def setUp(self):
        super().setUp()
        self.home_dir = real(tempfile.mkdtemp(prefix="ppl-protected-home-"))
        self.addCleanup(shutil.rmtree, self.home_dir, True)
        patcher = mock.patch.dict(os.environ, {"HOME": self.home_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        for rel in (".ssh", ".config/git", ".config/gh", ".local/share/gh"):
            os.makedirs(self.at(rel))
        for rel in (".gitconfig", ".ssh/config", ".config/gh/hosts.yml"):
            with open(self.at(rel), "w", encoding="utf-8") as handle:
                handle.write("x\n")
        self.outside = real(tempfile.mkdtemp(prefix="ppl-protected-outside-"))
        self.addCleanup(shutil.rmtree, self.outside, True)

    def at(self, rel):
        return os.path.join(self.home_dir, rel)

    def edit(self, target, agent=None, tool="Edit", key="file_path", cwd=OTHER, **extra):
        tool_input = dict({key: target, "old_string": "a", "new_string": "b"}, **extra)
        return ppl_guard.decide(payload(agent, tool, tool_input, cwd))

    def assert_protected(self, reason):
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn(PROTECTED, reason)

    def assert_outside_scope(self, reason, target):
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn(OUTSIDE_SCOPE, reason)
        self.assertIn(target, reason)


class TestProtectedPaths(ProtectedPathCase):
    def test_a_role_cannot_edit_the_gitconfig(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        gitconfig = self.at(".gitconfig")
        self.assert_protected(self.deny_reason(self.edit(gitconfig, agent="ppl-code-worker")))
        # with no run active either, the facts alone put the role in scope
        with self.no_active_run():
            self.assert_protected(self.deny_reason(self.edit(gitconfig, agent="ppl-code-worker")))

    def test_the_orchestrator_cannot_edit_the_gh_config(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        hosts = self.at(".config/gh/hosts.yml")
        for tool, key in EDIT_TOOLS:
            with self.subTest(tool=tool):
                self.assert_protected(self.deny_reason(self.edit(hosts, tool=tool, key=key)))
        # the facts alone put the session in scope, whatever the run
        with self.subTest(run=False), self.no_active_run():
            self.assert_protected(self.deny_reason(self.edit(hosts)))

    def test_a_symlink_into_ssh_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        keys = os.path.join(self.outside, "keys")
        os.symlink(self.at(".ssh"), keys)
        config = os.path.join(self.outside, "config")
        os.symlink(self.at(".ssh/config"), config)
        dangling = os.path.join(self.outside, "dangling")
        os.symlink(self.at(".ssh/authorized_keys"), dangling)
        chained = os.path.join(self.outside, "chained")
        os.symlink(keys, chained)
        for path in (keys + "/config", keys + "/id_new", keys + "/new/dir/file", config,
                     dangling, chained + "/config"):
            with self.subTest(path=path):
                self.assert_protected(self.deny_reason(self.edit(path)))

    def test_tilde_and_dotdot_are_resolved(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        os.makedirs(self.at("work/sub"))
        gh_link = os.path.join(self.outside, "ghlink")
        os.symlink(self.at(".config/gh"), gh_link)
        user = pwd.getpwuid(os.getuid()).pw_name
        for path, cwd in (("~/.gitconfig", OTHER),
                          ("~/work/../.ssh/config", OTHER),
                          (self.at("work/sub/../../.ssh/config"), OTHER),
                          ("../../.ssh/config", self.at("work/sub")),
                          (".gitconfig", self.home_dir),
                          ("./.config/./gh//hosts.yml", self.home_dir),
                          # the kernel climbs from the link's target, not from the link
                          (gh_link + "/../gh/hosts.yml", OTHER),
                          # ~user is the passwd home, which ssh reads whatever HOME says
                          ("~%s/.ssh/config" % user, OTHER)):
            with self.subTest(path=path, cwd=cwd):
                self.assert_protected(self.deny_reason(self.edit(path, cwd=cwd)))

    def test_an_edit_outside_the_pipeline_scope_is_left_alone(self):
        with self.facts_text(None), self.no_active_run():
            for agent in (None, "general-purpose"):
                with self.subTest(agent=agent):
                    self.assertIsNone(self.edit(self.at(".gitconfig"), agent=agent))
                    self.assertIsNone(self.edit(self.at(".ssh/config"), agent=agent,
                                                tool="Write"))

    def test_an_edit_inside_the_write_scope_is_allowed(self):
        # the run's repo, the scratch directory and Claude Code's projects and plans
        self.assertIsNone(self.edit(REPO + "/src/cart.py", agent="ppl-code-worker", cwd=REPO,
                                    tool="Write"))
        for path in (scratch_file("new-body.md"), self.at(".claude/projects/x/memory/m.md"),
                     self.at(".claude/plans/p.md")):
            with self.subTest(path=path):
                self.assertIsNone(self.edit(path, tool="Write"))
        claude_home = os.path.join(self.outside, "claude-home")
        config_dir = os.path.join(self.outside, "claude-config")
        with mock.patch.dict(os.environ, {"PPL_CLAUDE_HOME": claude_home,
                                          "CLAUDE_CONFIG_DIR": config_dir}):
            for path in (claude_home + "/projects/p/memory/m.md", config_dir + "/plans/p.md"):
                with self.subTest(path=path):
                    self.assertIsNone(self.edit(path, tool="Write"))

    def test_an_edit_outside_the_write_scope_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # hooks, plugins and hook scripts run outside the sandbox, wherever they sit
        hook_script = os.path.join(self.outside, "hooks", "guard.sh")
        for path in (self.at(".claude/hooks/x.sh"), self.at(".claude/plugins/p/hooks/hooks.json"),
                     hook_script, self.at(".claude/CLAUDE.md"),
                     self.at(".claude/settings.json.bak"), os.path.join(self.outside, "notes.md"),
                     self.at(".sshx/config")):
            for agent in (None, "general-purpose", "ppl-code-worker"):
                with self.subTest(path=path, agent=agent):
                    reason = self.deny_reason(self.edit(path, agent=agent, tool="Write"))
                    self.assert_outside_scope(reason, path)

    def test_the_pinned_gits_system_config_and_helpers_are_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # Homebrew's git reads <prefix>/etc/gitconfig and runs its exec-path helpers, files
        # the operator owns, so a credential helper planted there would see the token
        brew = os.path.join(self.outside, "brew")
        cellar = os.path.join(brew, "Cellar", "git", "2.50.0")
        for directory in ("bin", "libexec/git-core"):
            os.makedirs(os.path.join(cellar, directory))
        os.makedirs(os.path.join(brew, "bin"))
        binary = os.path.join(cellar, "bin", "git")
        with open(binary, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        os.symlink(binary, os.path.join(brew, "bin", "git"))
        pins = dict(facts_doc()["pins"], git={"path": brew + "/bin/git", "realpath": binary})
        with self.facts_text(json.dumps(facts_doc(pins=pins))):
            for path in (brew + "/etc/gitconfig", cellar + "/libexec/git-core/git-remote-https",
                         cellar + "/libexec/git-core/git-credential-osxkeychain",
                         brew + "/share/git-core/templates/hooks/pre-commit"):
                with self.subTest(path=path):
                    reason = self.deny_reason(self.edit(path, tool="Write"))
                    self.assert_outside_scope(reason, path)

    def test_a_path_with_edge_whitespace_or_a_control_character_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # Claude Code trims JavaScript whitespace off a path before it resolves it, so a raw
        # reading could differ: a BOM-led absolute path joined to a cwd in the run's repo
        target = REPO + "/src/cart.py"
        for raw in (" " + self.at(".gitconfig"), self.at(".gitconfig") + chr(0xA0),
                    chr(0xFEFF) + target, target + chr(0x2028), chr(0x3000) + target,
                    chr(0x1680) + target, target + "\t", REPO + "/src/a" + chr(1) + "b.py",
                    target + chr(0x85)):
            with self.subTest(raw=raw):
                reason = self.deny_reason(self.edit(raw, agent="ppl-code-worker", cwd=REPO,
                                                    tool="Write"))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("starts or ends with whitespace or holds a control character",
                              reason)

    def test_a_git_directory_or_file_in_the_write_scope_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # a planted hook fires on the orchestrator's git, and a repointed .git file changes
        # what -C <run repo> means to the pinned push
        worktree = REPO + "/.worktrees/r-" + TASK
        for path in (worktree + "/.git", REPO + "/.git/config", REPO + "/.git/hooks/pre-commit",
                     REPO + "/.git/worktrees/x/gitdir", REPO + "/.GIT/info/attributes",
                     worktree + "/sub/.Git/hooks/post-checkout"):
            for agent in (None, "ppl-code-worker"):
                with self.subTest(path=path, agent=agent):
                    reason = self.deny_reason(self.edit(path, agent=agent, cwd=REPO,
                                                        tool="Write"))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn(GIT_DIR, reason)

    def test_a_source_file_next_to_a_git_directory_is_allowed(self):
        self.assertIsNone(self.edit(REPO + "/src/cart.py", agent="ppl-code-worker", cwd=REPO,
                                    tool="Write"))
        for path in (REPO + "/.gitignore", REPO + "/.github/workflows/ci.yml",
                     REPO + "/.gitattributes", REPO + "/src/git/x.py"):
            with self.subTest(path=path):
                call = payload("ppl-code-worker", "Write", {"file_path": path}, REPO)
                self.assertIsNone(escape.edit_denial(call, paths.home(), facts_doc()))

    def test_without_guard_facts_the_edit_check_stands_aside(self):
        # a GitLab or Jira harness that has not opted in writes no guard facts, and keeps
        # today's behaviour: the check denies nothing, for any session, run or no run
        with self.facts_text(None):
            for path in (self.at(".gitconfig"), self.at(".ssh/config"),
                         self.at(".claude/hooks/x.sh"), os.path.join(self.outside, "notes.md"),
                         WS + "/.claude/settings.local.json", " " + self.at(".gitconfig")):
                for agent in (None, "general-purpose"):
                    with self.subTest(path=path, agent=agent):
                        self.assertIsNone(self.edit(path, agent=agent, tool="Write"))
            with self.no_active_run():
                self.assertIsNone(self.edit(self.at(".gitconfig"), agent="ppl-code-worker"))
            # the older rules still hold an edit tool: a role outside the run's repo, and run state
            reason = self.deny_reason(self.edit(self.at(".gitconfig"), agent="ppl-code-worker"))
            self.assertIn("may write only inside run %s's repo" % TASK, reason)
            reason = self.deny_reason(self.edit(str(paths.run_dir(TASK) / "state.json")))
            self.assertIn("run state is written only by ppl", reason)

    def test_the_sandboxs_write_denials_hold_inside_the_write_scope(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # a pin whose directory lies in the write scope, and a workspace the harness's
        # allow_write puts there: the profile's denyWrite wins for Bash, so it wins here too
        pin_dir = os.path.join(os.path.dirname(scratch_file("x")), "bin")
        os.makedirs(pin_dir, exist_ok=True)
        self.addCleanup(shutil.rmtree, pin_dir, True)
        gh = os.path.join(pin_dir, "gh")
        with open(gh, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        pins = dict(facts_doc()["pins"], gh={"path": gh, "realpath": real(gh)})
        scope = facts_doc()["write_scope"] + [real(WS)]
        recorded = facts_doc(pins=pins, write_scope=scope,
                             deny_write=[real(WS), real(pin_dir), real("/opt/pin")])
        # facts written before apply recorded deny_write compute the same set
        for label, doc in (("recorded", recorded), ("computed", facts_doc(pins=pins,
                                                                          write_scope=scope))):
            with self.facts_text(json.dumps(doc)):
                for path in (os.path.join(pin_dir, "newtool"), WS + "/notes.md"):
                    with self.subTest(facts=label, path=path):
                        reason = self.deny_reason(self.edit(path, tool="Write"))
                        self.assertIn("BOUNDARY_VIOLATION", reason)
                        self.assertIn(DENY_WRITE, reason)
                self.assertIsNone(self.edit(scratch_file("other.md"), tool="Write"))

    def test_unreadable_facts_deny_every_edit(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        for text in ("{not json", "[]"):
            with self.facts_text(text), self.no_active_run():
                for path in (scratch_file("new-body.md"),
                             self.at(".claude/projects/x/memory/m.md")):
                    with self.subTest(text=text, path=path):
                        reason = self.deny_reason(self.edit(path, tool="Write"))
                        self.assertIn("OWNER_DECISION", reason)
                        self.assertIn("ppl harness apply", reason)

    def test_the_allowed_roots_are_the_write_scope_and_claude_projects_and_plans(self):
        from ppl import escape_paths
        roots = escape_paths.allowed_roots(facts_doc())
        for path in facts_doc()["write_scope"] + [self.at(".claude/projects"),
                                                  self.at(".claude/plans")]:
            self.assertIn(path, roots)
        self.assertNotIn(self.at(".claude"), roots)

    def test_the_workspace_claude_dir_the_pins_and_ppl_files_are_protected(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        home = paths.home()
        for path in (WS + "/.claude/settings.local.json", WS + "/.claude/new/x", GH, GIT, PPL,
                     str(home / "config.toml"), str(home / "harnesses" / "gh-issues" /
                                                    "harness.toml"),
                     str(home / os_sandbox.FACTS_FILE), self.at(".config/git/config"),
                     self.at(".local/share/gh/extensions/gh-x/gh-x")):
            with self.subTest(path=path):
                self.assert_protected(self.deny_reason(self.edit(path, tool="Write")))

    def test_a_pin_reached_through_its_link_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        binary = os.path.join(self.outside, "cellar", "gh")
        os.makedirs(os.path.dirname(binary))
        with open(binary, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        link = os.path.join(self.outside, "bin-gh")
        os.symlink(binary, link)
        pins = dict(facts_doc()["pins"], gh={"path": link, "realpath": binary})
        with self.facts_text(json.dumps(facts_doc(pins=pins))):
            for path in (link, binary):
                with self.subTest(path=path):
                    self.assert_protected(self.deny_reason(self.edit(path, tool="Write")))

    def test_a_case_variant_of_a_protected_path_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # macOS file systems ignore case, so .SSH is .ssh there
        for path in (self.at(".SSH/config"), self.at(".Config/GH/hosts.yml"),
                     self.at(".GitConfig"), self.at(".config/GH/new.yml"),
                     WS.upper() + "/.Claude/settings.local.json"):
            with self.subTest(path=path):
                self.assert_protected(self.deny_reason(self.edit(path)))

    def test_config_the_environment_moves_is_protected(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        moved = {"XDG_CONFIG_HOME": os.path.join(self.outside, "xdg-config"),
                 "XDG_DATA_HOME": os.path.join(self.outside, "xdg-data"),
                 "GH_CONFIG_DIR": os.path.join(self.outside, "gh-config"),
                 "GIT_CONFIG_GLOBAL": os.path.join(self.outside, "global.gitconfig")}
        with mock.patch.dict(os.environ, moved):
            for path in (moved["XDG_CONFIG_HOME"] + "/git/config",
                         moved["XDG_CONFIG_HOME"] + "/gh/hosts.yml",
                         moved["XDG_DATA_HOME"] + "/gh/extensions/gh-x/gh-x",
                         moved["GH_CONFIG_DIR"] + "/hosts.yml", moved["GIT_CONFIG_GLOBAL"]):
                with self.subTest(path=path):
                    self.assert_protected(self.deny_reason(self.edit(path)))
        # a relative value names no place the guard can protect, and moves nothing
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "rel"}):
            self.assert_protected(self.deny_reason(self.edit(self.at(".config/gh/hosts.yml"))))

    @unittest.skipUnless(os.path.isdir("/System/Volumes/Data"), "macOS firmlinks only")
    def test_a_firmlink_name_of_a_protected_path_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        # /System/Volumes/Data<path> is <path> under another name that realpath keeps, and a
        # protected directory that does not exist yet has no inode of its own to compare
        data = "/System/Volumes/Data"
        if not os.path.isdir(data + self.home_dir):
            self.skipTest("the temporary directory is not on the data volume")
        shutil.rmtree(self.at(".config/gh"))
        for path in (data + self.at(".config/gh/hosts.yml"), data + self.at(".ssh/config"),
                     data + self.at(".ssh/new_key"), data + self.at(".gitconfig")):
            with self.subTest(path=path):
                self.assert_protected(self.deny_reason(self.edit(path)))

    def test_claude_settings_mcp_config_and_ppl_code_are_protected(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        claude_home = os.path.join(self.outside, "claude-home")
        config_dir = os.path.join(self.outside, "claude-config")
        assets = os.path.join(self.outside, "assets")
        os.makedirs(os.path.join(assets, "defaults"))
        package = os.path.dirname(os.path.realpath(escape.__file__))
        targets = (self.at(".claude/settings.json"), self.at(".claude/settings.local.json"),
                   self.at(".claude.json"), WS + "/.mcp.json",
                   claude_home + "/settings.json", claude_home + "/settings.local.json",
                   self.outside + "/.claude.json", config_dir + "/settings.json",
                   config_dir + "/settings.local.json", config_dir + "/.claude.json",
                   os.path.join(package, "escape.py"), os.path.join(package, "new_module.py"),
                   os.path.join(package, "_assets", "hooks", "ppl_guard.py"),
                   os.path.join(assets, "hooks", "ppl_guard.py"))
        moved = {"PPL_CLAUDE_HOME": claude_home, "CLAUDE_CONFIG_DIR": config_dir,
                 "PPL_ROOT": assets}
        with mock.patch.dict(os.environ, moved):
            for agent in (None, "general-purpose"):
                for path in targets:
                    with self.subTest(agent=agent, path=path):
                        result = self.edit(path, agent=agent, tool="Write")
                        self.assert_protected(self.deny_reason(result))

    def test_a_hard_link_of_a_protected_file_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        scratch = os.path.dirname(scratch_file("x"))
        for rel in (".gitconfig", ".ssh/config", ".config/gh/hosts.yml"):
            name = rel.replace("/", "_")
            outside = os.path.join(self.outside, name)
            os.link(self.at(rel), outside)
            inside = os.path.join(scratch, "link" + name)
            os.link(self.at(rel), inside)
            self.addCleanup(os.unlink, inside)
            with self.subTest(rel=rel):
                # in the write scope, a file with another link may be another name of anything
                reason = self.deny_reason(self.edit(inside))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertTrue(PROTECTED in reason or "more than one link" in reason, reason)
                # outside it, the link is denied like any path there
                reason = self.deny_reason(self.edit(outside))
                self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_every_path_key_of_the_call_is_checked(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        for key in ("path", "notebook_path"):
            with self.subTest(key=key):
                result = self.edit(scratch_file("notes2.ipynb"), tool="NotebookEdit",
                                   **{key: self.at(".ssh/config")})
                self.assert_protected(self.deny_reason(result))

    def test_a_path_the_guard_cannot_place_is_denied(self):
        # rule: R-OS-SANDBOX-PROTECTED-PATHS
        for path, cwd in ((".gitconfig", None), (".gitconfig", ""), (".gitconfig", "rel/dir"),
                          (["~/.gitconfig"], OTHER), ("/tmp/a\x00b", OTHER)):
            with self.subTest(path=path, cwd=cwd):
                reason = self.deny_reason(self.edit(path, cwd=cwd))
                self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIsNone(self.edit(scratch_file("new-body.md"), cwd=None))

    def test_protected_paths_lists_the_config_the_pins_and_ppl_files(self):
        home = paths.home()
        found = escape.protected_paths(home, facts_doc())
        for path in (self.at(".gitconfig"), self.at(".config/git"), self.at(".ssh"),
                     self.at(".config/gh"), self.at(".local/share/gh"), real(WS + "/.claude"),
                     real(home / "config.toml"), real(home / "harnesses"),
                     real(home / os_sandbox.FACTS_FILE), real(GH), real(GIT), real(PPL)):
            self.assertIn(path, found)
        self.assertEqual(found, [real(path) for path in found])
        without = escape.protected_paths(home, None)
        self.assertIn(self.at(".ssh"), without)
        self.assertNotIn(real(WS + "/.claude"), without)


class TestGithubMcp(OsSandboxCase):
    def call(self, tool, agent="ppl-code-worker", cwd=REPO):
        return ppl_guard.decide(payload(agent, tool, {"owner": "o", "repo": "r"}, cwd))

    def assert_github_mcp_denied(self, reason):
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("GitHub MCP", reason)

    @contextlib.contextmanager
    def snapshot_forge(self, forge):
        """The run's harness snapshot with its forge table replaced, or the snapshot gone
        when `forge` is None."""
        snapshot = paths.run_dir(TASK) / "harness.json"
        saved = snapshot.read_text(encoding="utf-8")
        if forge is None:
            snapshot.unlink()
        else:
            store.write_json(snapshot, dict(json.loads(saved), forge=forge))
        try:
            yield
        finally:
            snapshot.write_text(saved, encoding="utf-8")

    def test_a_role_cannot_call_a_github_mcp_pull_request_tool(self):
        # rule: R-FORGE-GH-MCP-NO-ROLE
        for agent in ("ppl-code-worker", "ppl-review-responder", "ppl-briefer", "ppl-future-role"):
            with self.subTest(agent=agent):
                result = self.call("mcp__github__create_pull_request", agent)
                self.assert_github_mcp_denied(self.deny_reason(result))
        # global: no run, no facts and any cwd
        with self.facts_text(None), self.no_active_run():
            result = self.call("mcp__github__get_file_contents", cwd=OTHER)
            self.assert_github_mcp_denied(self.deny_reason(result))

    def test_a_numbered_github_server_is_matched(self):
        # rule: R-FORGE-GH-MCP-NO-ROLE
        for tool in ("mcp__github_2__create_pull_request", "mcp__github_10__get_me",
                     "mcp__GitHub__push_files", "mcp__GITHUB_3__create_branch",
                     "mcp__claude_ai_GitHub__create_pull_request",
                     "mcp__plugin_github_github__get_pull_request", "mcp__github-enterprise__x"):
            with self.subTest(tool=tool):
                self.assert_github_mcp_denied(self.deny_reason(self.call(tool)))

    def test_the_snapshot_server_name_is_matched(self):
        # rule: R-FORGE-GH-MCP-NO-ROLE
        with self.snapshot_forge({"kind": "github", "mcp_server": "corp-gh"}):
            result = self.call("mcp__corp-gh__create_pull_request")
            self.assert_github_mcp_denied(self.deny_reason(result))
            self.assertIsNone(self.call("mcp__corp-ghx__create_pull_request"))
        # a GitLab server name is no GitHub prefix, and a snapshot without one is tolerated
        with self.snapshot_forge({"kind": "gitlab", "mcp_server": "corp-gh"}):
            self.assertIsNone(self.call("mcp__corp-gh__create_pull_request"))
        for forge in (None, {"kind": "github"}, {"kind": "github", "mcp_server": 7}, "github"):
            with self.subTest(forge=forge), self.snapshot_forge(forge):
                result = self.call("mcp__github__create_pull_request")
                self.assert_github_mcp_denied(self.deny_reason(result))
                self.assertIsNone(self.call("mcp__corp-gh__create_pull_request"))

    def test_the_orchestrator_may_read_through_github_mcp(self):
        for tool in ("mcp__github__get_pull_request", "mcp__github__list_pull_requests",
                     "mcp__github_2__get_me"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.call(tool, agent=None, cwd=WS))

    def test_a_role_keeps_the_other_mcp_servers(self):
        for tool in ("mcp__atlassian__getJiraIssue", "mcp__GitLab-MCP__get_merge_request",
                     "mcp__gitea__get_pull_request", "mcp__notes__github_style_summary"):
            with self.subTest(tool=tool):
                self.assertIsNone(self.call(tool, agent="ppl-briefer"))

    def test_the_tracker_write_reason_still_names_the_tracker(self):
        reason = self.deny_reason(self.call("mcp__github__add_issue_comment"))
        self.assertIn("may not write the tracker", reason)


class TestWebFetchGithub(OsSandboxCase):
    def fetch(self, url, agent="ppl-verifier"):
        return ppl_guard.decide(payload(agent, "WebFetch", {"url": url, "prompt": "summarize"},
                                        REPO))

    def assert_webfetch_denied(self, reason):
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("WebFetch", reason)

    def test_a_role_cannot_webfetch_github(self):
        # rule: R-WEBFETCH-GITHUB-NO-ROLE
        for url in ("https://API.GitHub.com./repos", "https://github.com/o/r/pull/1",
                    "http://gist.github.com/x"):
            with self.subTest(url=url):
                self.assert_webfetch_denied(self.deny_reason(self.fetch(url)))
        with self.facts_text(None), self.no_active_run():
            result = self.fetch("https://github.com/o/r", agent="ppl-briefer")
            self.assert_webfetch_denied(self.deny_reason(result))

    def test_a_disguised_github_url_is_denied(self):
        # rule: R-WEBFETCH-GITHUB-NO-ROLE
        for url in ("https://user:pw@github.com/o/r", "https://evil.com@github.com/",
                    "https://github.com:443/o/r", "HTTPS://GITHUB.COM/", "https://github.com../x",
                    "https://\uff47\uff49\uff54\uff48\uff55\uff42.com/o/r",
                    "https://github\u3002com/o", "https://github\uff0ecom/o",
                    "https://%67ithub.com/o", "https://github.com\\@evil.com/",
                    "https:github.com/o", "https:/github.com/o", "https:\\\\github.com/o",
                    "github.com/o/r", "//github.com/o", " https://github.com/o",
                    "https://git\thub.com/", "https://git\nhub.com/", "https://xn--github-.com/x",
                    "https://[github.com/x",
                    # a URL parser maps these to nothing in a host
                    "https://git\u00adhub.com/o", "https://git\u200bhub.com/o",
                    "https://github\u2060.com/o", "https://\ufeffgithub.com/o"):
            with self.subTest(url=url):
                self.assert_webfetch_denied(self.deny_reason(self.fetch(url)))

    def test_a_role_may_webfetch_another_host(self):
        for url in ("https://docs.python.org/3/", "https://github.com.evil.com/x",
                    "https://notgithub.com/", "https://github.com@evil.com/",
                    "https://example.com/github.com", "https://evil.com/?u=https://github.com"):
            with self.subTest(url=url):
                self.assertIsNone(self.fetch(url))

    def test_the_orchestrator_may_webfetch_github(self):
        self.assertIsNone(ppl_guard.decide(payload(None, "WebFetch",
                                                   {"url": "https://github.com/o/r",
                                                    "prompt": "summarize"}, WS)))

    def test_webfetch_is_a_guarded_tool_name(self):
        from ppl import doctor
        self.assertIn("WebFetch", ppl_guard.GUARDED_TOOL_NAMES)
        self.assertIn("WebFetch", doctor.guarded_tool_names())
        self.assertTrue(doctor.matcher_covers(doctor.RECOMMENDED_MATCHER, "WebFetch"))
        self.assertIn("WebFetch", doctor.uncovered_by(["Edit|Write|MultiEdit|NotebookEdit|Bash"]))


if __name__ == "__main__":
    unittest.main()

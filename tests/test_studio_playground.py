import contextlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))
import panel_client
import ppl_guard
from ppl import config, harness, paths, run, store, tomlout
from ppl.boundary import TEST_PATTERNS
from ppl.errors import PplError
from ppl.studio import drafts, playground, project, stacks

TEST_SOURCE = "source/core/orders/src/test/java/FooTest.java"
MAIN_SOURCE = "source/core/orders/src/main/java/Foo.java"


class PlaygroundTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        self.repo = panel_client.make_repo({"Makefile": "test:\n\ttrue\n", MAIN_SOURCE: "class Foo {}\n"})
        info = project.inspect(str(self.repo))
        self.draft = stacks.draft("make", info)
        # TEST_SOURCE and MAIN_SOURCE are a Maven layout, so the draft names the Java defaults
        self.draft["doc"]["tests"] = {"sources": list(TEST_PATTERNS)}

    def check(self, role, tool, value, **kwargs):
        return playground.check("demo", self.draft["doc"], self.draft["fragments"], role, tool, value, **kwargs)


class TestPlayground(PlaygroundTestCase):
    def test_a_code_worker_writing_a_test_source_is_denied(self):
        result = self.check("ppl-code-worker", "Write", TEST_SOURCE)
        self.assertEqual(result["decision"], "deny", result)
        self.assertIn("test", result["reason"])

    def test_a_test_worker_running_the_focused_test_command_is_allowed(self):
        result = self.check("ppl-test-worker", "Bash", "make test TEST=FooTest")
        self.assertEqual(result["decision"], "allow", result)

    def test_a_test_worker_reading_a_main_source_is_denied(self):
        result = self.check("ppl-test-worker", "Bash", "cat %s" % MAIN_SOURCE)
        self.assertEqual(result["decision"], "deny", result)

    def test_the_operator_session_is_checked_without_a_role(self):
        result = self.check("operator", "Bash", "git add -A")
        self.assertEqual(result["decision"], "deny", result)

    def test_the_installed_harness_is_used_when_no_draft_is_given(self):
        harness.add(str(paths.defaults_dir() / "harnesses" / "example-generic"), name="demo")
        installed = harness.load("demo")
        installed["repo"]["path"] = str(self.repo)
        (paths.harness_dir("demo") / "harness.toml").write_text(tomlout.dumps(installed),
                                                                 encoding="utf-8")
        result = playground.check("demo", None, {}, "ppl-test-worker", "Bash", "make test TEST=FooTest")
        self.assertEqual(result["decision"], "allow", result)

    def test_the_playground_leaves_no_files_behind(self):
        # a temp directory of the test's own: another suite or studio sharing the system one
        # creates and removes its own ppl-playground-* entries while this test runs
        own = tmphome.mkdtemp(prefix="pplplay-")
        made = []
        real = tempfile.mkdtemp

        def spy(*args, **kwargs):
            path = real(*args, **kwargs)
            made.append(path)
            return path

        with mock.patch.object(tempfile, "tempdir", own), mock.patch.object(tempfile, "mkdtemp", spy):
            self.check("ppl-code-worker", "Write", TEST_SOURCE)
        built = [path for path in made if os.path.basename(path).startswith("ppl-playground-")]
        self.assertTrue(built)
        self.assertTrue(all(os.path.dirname(path) == own for path in built), built)
        self.assertEqual(os.listdir(own), [])
        self.assertFalse(paths.runs_dir().exists())

    def test_a_bad_role_tool_or_input_is_refused(self):
        for role, tool, value in (("ppl-intern", "Bash", "ls"), ("operator", "Rm", "x"),
                                  ("operator", "Bash", "x" * 5000), ("operator", "Bash", None)):
            with self.assertRaises(PplError) as caught:
                self.check(role, tool, value)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_parse_reads_the_hook_answer(self):
        deny = ('{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", '
                '"permissionDecisionReason": "BOUNDARY_VIOLATION see R-GIT-ADD-ALL"}}')
        self.assertEqual(playground.parse(0, deny, ""),
                         {"decision": "deny", "reason": "BOUNDARY_VIOLATION see R-GIT-ADD-ALL",
                          "rules": ["R-GIT-ADD-ALL"]})
        self.assertEqual(playground.parse(0, "", "")["decision"], "allow")
        self.assertEqual(playground.parse(1, "", "Traceback")["decision"], "error")

    def test_a_relative_path_reaches_the_hook_as_the_absolute_path_claude_code_sends(self):
        cases = (("ppl-code-worker", MAIN_SOURCE, "allow", ""),
                 ("ppl-code-worker", TEST_SOURCE, "deny", "may not write test sources"),
                 ("ppl-test-worker", TEST_SOURCE, "allow", ""),
                 ("ppl-test-worker", MAIN_SOURCE, "deny", "may only write test sources"))
        for role, relative, decision, words in cases:
            for value in (relative, str(self.repo / relative)):
                with self.subTest(role=role, value=value):
                    result = self.check(role, "Write", value)
                    self.assertEqual(result["decision"], decision, result)
                    self.assertIn(words, result["reason"])

    def test_a_path_outside_the_repo_is_denied_to_a_role(self):
        result = self.check("ppl-code-worker", "Write", "/etc/hosts")
        self.assertEqual(result["decision"], "deny", result)
        self.assertIn("only inside", result["reason"])

    def test_the_boundary_given_decides_what_a_code_worker_may_write(self):
        narrow = {"write": ["docs/**"], "forbid": []}
        forbidding = {"write": ["source/**"], "forbid": ["source/core/**"]}
        for boundary, decision, words in ((None, "allow", ""),
                                          (narrow, "deny", "outside the write boundary"),
                                          (forbidding, "deny", "forbidden path")):
            with self.subTest(boundary=boundary):
                result = self.check("ppl-code-worker", "Write", MAIN_SOURCE, boundary=boundary)
                self.assertEqual(result["decision"], decision, result)
                self.assertIn(words, result["reason"])

    def test_a_malformed_boundary_is_refused(self):
        for boundary in ("source/**", {"write": "source/**", "forbid": []}, {"write": [1], "forbid": []},
                         {"write": []}):
            with self.subTest(boundary=boundary):
                with self.assertRaises(PplError) as caught:
                    self.check("operator", "Bash", "ls", boundary=boundary)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_tracker_write_tool_is_denied_by_name(self):
        result = self.check("operator", "mcp__atlassian__editJiraIssue", "")
        self.assertEqual(result["decision"], "deny", result)
        self.assertIn("the tracker is written only after the merge", result["reason"])

    def test_the_tool_input_has_the_keys_claude_code_sends(self):
        self.assertEqual(playground.tool_input("Bash", "ls"), {"command": "ls"})
        self.assertEqual(playground.tool_input("Read", "/a"), {"file_path": "/a"})
        self.assertEqual(playground.tool_input("Write", "/a")["file_path"], "/a")
        self.assertEqual(playground.tool_input("Edit", "/a")["file_path"], "/a")
        self.assertEqual(playground.tool_input("MultiEdit", "/a")["file_path"], "/a")
        self.assertEqual(playground.tool_input("NotebookEdit", "/a")["notebook_path"], "/a")
        self.assertEqual(playground.tool_input("Grep", "/a")["path"], "/a")
        self.assertEqual(playground.tool_input("Glob", "/a")["path"], "/a")
        self.assertEqual(playground.tool_input("mcp__atlassian__editJiraIssue", "x"), {})

    def test_a_repo_path_with_a_tilde_is_the_home_directory_one(self):
        inside_home = Path(os.environ["HOME"]) / "work" / "proj"
        inside_home.mkdir(parents=True)
        doc = dict(self.draft["doc"], repo=dict(self.draft["doc"]["repo"], path="~/work/proj"))
        result = playground.check("demo", doc, self.draft["fragments"], "ppl-code-worker", "Write",
                                  MAIN_SOURCE)
        self.assertEqual(result["decision"], "allow", result)

    def test_a_relative_repo_path_is_refused(self):
        doc = dict(self.draft["doc"], repo=dict(self.draft["doc"]["repo"], path="work/proj"))
        with self.assertRaises(PplError) as caught:
            playground.check("demo", doc, self.draft["fragments"], "operator", "Bash", "ls")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_a_draft_the_harness_loader_refuses_is_an_error_envelope(self):
        with self.assertRaises(PplError) as caught:
            playground.check("demo", {}, {}, "operator", "Bash", "ls")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_a_name_that_is_not_installed_is_refused_when_no_draft_is_given(self):
        with self.assertRaises(PplError) as caught:
            playground.check("nothere", None, {}, "operator", "Bash", "ls")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("no installed harness", caught.exception.message)

    def test_an_installed_harness_is_not_shadowed_by_a_directory_of_the_same_name_here(self):
        harness.add(str(paths.defaults_dir() / "harnesses" / "example-generic"), name="demo")
        installed = harness.load("demo")
        installed["repo"]["path"] = str(self.repo)
        (paths.harness_dir("demo") / "harness.toml").write_text(tomlout.dumps(installed),
                                                                 encoding="utf-8")
        elsewhere = Path(tmphome.mkdtemp(prefix="pplcwd-"))
        shadow = dict(installed, repo=dict(installed["repo"], path=str(elsewhere / "other")))
        (elsewhere / "demo").mkdir()
        (elsewhere / "demo" / "harness.toml").write_text(
            tomlout.dumps({key: item for key, item in shadow.items() if not key.startswith("_")}),
            encoding="utf-8")
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(elsewhere)
        result = playground.check("demo", None, {}, "ppl-code-worker", "Write",
                                  str(self.repo / MAIN_SOURCE))
        self.assertEqual(result["decision"], "allow", result)

    def test_the_hook_and_the_child_get_the_throwaway_data_root_and_no_other_ppl_variable(self):
        calls = []
        real_run = subprocess.run

        def spy(command, **kwargs):
            calls.append(kwargs)
            return real_run(command, **kwargs)

        poisoned = {"PPL_LOCAL_SETTINGS_DIR": "/real/settings", "PPL_ELSEWHERE": "/real/else"}
        with mock.patch.dict(os.environ, poisoned), mock.patch.object(subprocess, "run", spy):
            self.check("operator", "Bash", "ls")
        self.assertEqual(len(calls), 2)
        for kwargs in calls:
            env = kwargs["env"]
            self.assertNotEqual(env["PPL_HOME"], os.environ["PPL_HOME"])
            self.assertTrue(os.path.basename(env["PPL_HOME"]).startswith("ppl-playground-"))
            self.assertEqual({key for key in env if key.startswith("PPL_")}, {"PPL_HOME", "PPL_ROOT"})
            self.assertEqual(env["PPL_ROOT"], str(paths.root()))
            self.assertGreater(kwargs["timeout"], 0)

    def test_a_hook_that_does_not_answer_in_time_is_reported_as_an_error(self):
        real_run = subprocess.run

        def stalled(command, **kwargs):
            if command[-1].endswith("ppl_guard.py"):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return real_run(command, **kwargs)

        with mock.patch.object(subprocess, "run", stalled):
            result = self.check("operator", "Bash", "ls")
        self.assertEqual(result["decision"], "error", result)
        self.assertIn("did not answer", result["reason"])

    def test_a_run_that_cannot_be_built_in_time_is_refused(self):
        def stalled(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with mock.patch.object(subprocess, "run", stalled):
            with self.assertRaises(PplError) as caught:
                self.check("operator", "Bash", "ls")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("seconds", caught.exception.message)

    def test_a_hook_internal_error_on_an_unroled_session_is_not_shown_as_a_plain_allow(self):
        result = playground.parse(0, "", "ppl_guard internal error: KeyError('x')\n")
        self.assertEqual(result["decision"], "error", result)
        self.assertIn("internal error", result["reason"])

    def test_parse_reports_a_hook_that_answered_something_else(self):
        self.assertEqual(playground.parse(0, "not json", "")["decision"], "error")
        self.assertEqual(playground.parse(0, "[1]", "")["decision"], "error")

    def test_every_tool_the_hook_acts_on_by_path_is_one_the_playground_sends(self):
        for tool in ppl_guard.WRITE_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, playground.TOOLS)
                self.assertEqual(ppl_guard.target_path(playground.tool_input(tool, "/a")), "/a")
        for tool in ppl_guard.READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(tool, playground.TOOLS)
                self.assertEqual(ppl_guard.read_target_path(playground.tool_input(tool, "/a"), {}), "/a")
        for tool in playground.TOOLS:
            with self.subTest(tool=tool, relative=True):
                joined = "/repo/x" if tool in ppl_guard.WRITE_TOOLS else "x"
                self.assertEqual(playground._as_sent(tool, "x", "/repo"), joined)

    def test_a_notebook_edit_is_checked_like_a_write(self):
        allowed = self.check("ppl-code-worker", "NotebookEdit", "source/core/orders/analysis.ipynb")
        self.assertEqual(allowed["decision"], "allow", allowed)
        denied = self.check("ppl-test-worker", "NotebookEdit", "source/core/orders/analysis.ipynb")
        self.assertEqual(denied["decision"], "deny", denied)
        self.assertIn("may only write test sources", denied["reason"])

    def direct_decision(self, role, tool, tool_input):
        """The hook called as Claude Code calls it, from the repo, with the input
        exactly as typed and a boundary that lets everything through."""
        if not paths.run_dir(playground.TASK).exists():
            scratch = Path(tmphome.mkdtemp(prefix="pplscratch-"))
            folder = drafts.write("demo", self.draft["doc"], self.draft["fragments"], root=scratch)
            run.init(playground.TASK, "https://tracker.invalid/x", str(self.repo), isolated=False,
                     model_check="skipped", harness={"name": "demo"})
            store.write_json(paths.run_dir(playground.TASK) / "harness.json",
                             harness.snapshot(harness.load(str(folder))))
            store.write_json(paths.run_dir(playground.TASK) / "boundary.json",
                             {"write": ["**"], "forbid": []})
        previous = os.getcwd()
        os.chdir(self.repo)
        try:
            answer = ppl_guard.decide({"hook_event_name": "PreToolUse", "agent_type": role,
                                       "tool_name": tool, "tool_input": tool_input,
                                       "cwd": str(self.repo)})
        finally:
            os.chdir(previous)
        return "allow" if answer is None else "deny"

    def test_a_tilde_write_path_is_not_expanded_because_the_hook_does_not_expand_it(self):
        literal = "~/x.java"
        direct = self.direct_decision("ppl-code-worker", "Write",
                                      {"file_path": literal, "content": ""})
        shown = self.check("ppl-code-worker", "Write", literal, boundary={"write": ["**"], "forbid": []})
        self.assertEqual(direct, "allow")
        self.assertEqual(shown["decision"], direct, shown)

    def test_a_read_path_goes_as_typed_because_the_hook_resolves_it_itself(self):
        for value, decision in (("~/x.java", "allow"), (MAIN_SOURCE, "deny"),
                                (str(self.repo / MAIN_SOURCE), "deny")):
            with self.subTest(value=value):
                direct = self.direct_decision("ppl-test-worker", "Read", {"file_path": value})
                shown = self.check("ppl-test-worker", "Read", value)
                self.assertEqual(direct, decision)
                self.assertEqual(shown["decision"], direct, shown)

    def test_a_value_naming_a_real_path_is_never_performed(self):
        outside = Path(tmphome.mkdtemp(prefix="pplscratch-"))
        inside = self.repo / "source" / "core" / "orders" / "src" / "main" / "java"
        before = sorted(str(path) for path in self.repo.rglob("*") if ".git" not in path.parts)
        for role in ("operator", "ppl-code-worker", "ppl-test-worker"):
            for tool, value in (("Write", str(outside / "written.txt")),
                                ("Bash", "touch %s" % (outside / "touched.txt")),
                                ("Bash", "echo x > %s" % (outside / "redirected.txt")),
                                ("Write", str(inside / "Created.java")),
                                ("Bash", "touch %s" % (inside / "Touched.java")),
                                ("Bash", "echo x > %s" % (inside / "Redirected.java")),
                                ("NotebookEdit", str(inside / "new.ipynb"))):
                with self.subTest(role=role, tool=tool, value=value):
                    self.check(role, tool, value)
        self.assertEqual(os.listdir(outside), [])
        self.assertEqual(sorted(str(path) for path in self.repo.rglob("*") if ".git" not in path.parts),
                         before)

    @contextlib.contextmanager
    def recorded_roots(self):
        roots = []
        real = tempfile.TemporaryDirectory

        def make(*args, **kwargs):
            made = real(*args, **kwargs)
            roots.append(made.name)
            return made

        with mock.patch.object(tempfile, "TemporaryDirectory", make):
            yield roots

    def assert_one_root_made_and_removed(self, roots):
        self.assertEqual(len(roots), 1, roots)
        self.assertFalse(os.path.exists(roots[0]), roots[0])

    def test_the_throwaway_root_is_removed_when_the_draft_is_refused(self):
        with self.recorded_roots() as roots:
            with self.assertRaises(PplError):
                playground.check("demo", {}, {}, "operator", "Bash", "ls")
        self.assert_one_root_made_and_removed(roots)

    def test_the_throwaway_root_is_removed_when_the_run_cannot_be_built(self):
        def failed(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "boom")

        with self.recorded_roots() as roots, mock.patch.object(subprocess, "run", failed):
            with self.assertRaises(PplError):
                self.check("operator", "Bash", "ls")
        self.assert_one_root_made_and_removed(roots)

    def test_the_throwaway_root_is_removed_when_the_hook_times_out(self):
        real_run = subprocess.run

        def stalled(command, **kwargs):
            if command[-1].endswith("ppl_guard.py"):
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return real_run(command, **kwargs)

        with self.recorded_roots() as roots, mock.patch.object(subprocess, "run", stalled):
            result = self.check("operator", "Bash", "ls")
        self.assertEqual(result["decision"], "error", result)
        self.assert_one_root_made_and_removed(roots)

    def test_the_throwaway_root_is_removed_when_an_exception_is_raised_mid_check(self):
        with self.recorded_roots() as roots, mock.patch.object(
                harness, "snapshot", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.check("operator", "Bash", "ls")
        self.assert_one_root_made_and_removed(roots)

    def test_the_throwaway_root_is_removed_when_the_hook_has_answered(self):
        with self.recorded_roots() as roots:
            self.check("operator", "Bash", "ls")
        self.assert_one_root_made_and_removed(roots)


class TestPlaygroundRoute(PlaygroundTestCase):
    def test_the_route_returns_the_decision(self):
        status, result, _ = panel_client.call("POST", "/api/playground", body={
            "name": "demo", "doc": self.draft["doc"], "fragments": self.draft["fragments"],
            "role": "ppl-code-worker", "tool": "Write", "value": TEST_SOURCE})
        self.assertEqual(status, 200)
        self.assertEqual(result["decision"], "deny")

    def test_a_check_without_a_role_is_a_400_envelope(self):
        status, result, _ = panel_client.call("POST", "/api/playground", body={
            "name": "demo", "doc": self.draft["doc"], "fragments": self.draft["fragments"],
            "tool": "Bash", "value": "ls"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error_code"], "SCHEMA_INVALID")

    def test_the_installed_harness_is_checked_when_the_body_has_no_doc(self):
        harness.add(str(paths.defaults_dir() / "harnesses" / "example-generic"), name="demo")
        status, result, _ = panel_client.call("POST", "/api/playground", body={
            "name": "demo", "role": "operator", "tool": "Bash", "value": "git add -A"})
        self.assertEqual(status, 200)
        self.assertEqual(result["decision"], "deny")

    def test_a_name_that_is_not_installed_is_a_400_envelope_when_the_body_has_no_doc(self):
        status, result, _ = panel_client.call("POST", "/api/playground", body={
            "name": "nothere", "role": "operator", "tool": "Bash", "value": "ls"})
        self.assertEqual(status, 400)
        self.assertEqual(result["error_code"], "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

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
import panel_client
from ppl import config, harness, paths, tomlout
from ppl.errors import PplError
from ppl.studio import playground, project, stacks

TEST_SOURCE = "source/core/orders/src/test/java/FooTest.java"
MAIN_SOURCE = "source/core/orders/src/main/java/Foo.java"


class PlaygroundTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        self.repo = panel_client.make_repo({"Makefile": "test:\n\ttrue\n", MAIN_SOURCE: "class Foo {}\n"})
        info = project.inspect(str(self.repo))
        self.draft = stacks.draft("make", info)

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
        before = {name for name in os.listdir(tempfile.gettempdir()) if name.startswith("ppl-playground-")}
        self.check("ppl-code-worker", "Write", TEST_SOURCE)
        after = {name for name in os.listdir(tempfile.gettempdir()) if name.startswith("ppl-playground-")}
        self.assertEqual(after, before)
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
        self.assertIn("Jira", result["reason"])

    def test_the_tool_input_has_the_keys_claude_code_sends(self):
        self.assertEqual(playground.tool_input("Bash", "ls"), {"command": "ls"})
        self.assertEqual(playground.tool_input("Read", "/a"), {"file_path": "/a"})
        self.assertEqual(playground.tool_input("Write", "/a")["file_path"], "/a")
        self.assertEqual(playground.tool_input("Edit", "/a")["file_path"], "/a")
        self.assertEqual(playground.tool_input("MultiEdit", "/a")["file_path"], "/a")
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

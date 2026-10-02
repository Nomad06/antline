import io
import json
import os
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import cli, config, harness, models, paths
from ppl.errors import PplError
from ppl.trackers import jira

READ_TOOLS_LINE = "read_tools = " + json.dumps(jira.DEFAULTS["read_tools"])


class HarnessTestCase(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def mutate(self, name, old, new):
        path = fixtures.install_harness(name) / "harness.toml"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return name


class TestLoadAndValidate(HarnessTestCase):
    def test_both_examples_load_clean(self):
        for name in ("example-generic", "example-maven"):
            fixtures.install_harness(name)
            doc = harness.load(name)
            self.assertEqual(doc["harness"]["name"], name)
            self.assertEqual(harness.validate(doc, name), [])

    def test_load_by_path(self):
        target = fixtures.install_harness("example-maven", as_name="other")
        self.assertEqual(harness.load(str(target))["_dir"], str(target))

    def test_unknown_name_is_harness_invalid(self):
        with self.assertRaises(PplError) as caught:
            harness.load("nope")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_bad_key_pattern_is_refused_not_a_traceback(self):
        name = self.mutate("example-maven", 'key_pattern = "^[A-Z][A-Z0-9]+-[0-9]+$"',
                           'key_pattern = "^DEMO-["')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("task.key_pattern", caught.exception.message)

    def test_unknown_forge_is_refused(self):
        name = self.mutate("example-maven", 'kind = "gitlab"', 'kind = "bitbucket"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("forge.kind", caught.exception.message)

    def test_missing_repo_path_is_refused(self):
        name = self.mutate("example-maven", 'path = "/home/dev/src/project"', 'path = ""')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("repo.path", caught.exception.message)

    def test_wrong_format_is_refused(self):
        name = self.mutate("example-maven", "format = 1", "format = 7")
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("format", caught.exception.message)

    def test_gitlab_without_mcp_server_is_refused(self):
        name = self.mutate("example-maven", 'mcp_server = "GitLab-MCP"', 'mcp_server = ""')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("forge.mcp_server", caught.exception.message)

    def test_shape_without_constraints_warns(self):
        name = self.mutate("example-generic",
                           'one_goal_of = ["build", "lint", "test", "integration"]',
                           'one_goal_of = []')
        warnings = harness.validate(harness.load(name), name)
        self.assertTrue(any("no required_flags and no one_goal_of" in w for w in warnings))

    def test_shape_rows_are_normalised(self):
        fixtures.install_harness("example-maven")
        rows = harness.shape_rows(harness.load("example-maven"))
        self.assertEqual(rows[0]["argv0"], "mvn")
        self.assertEqual(rows[0]["required_flags"], [["-pl", "^:[A-Za-z][\\w.-]*$"], ["-am", ""]])
        self.assertTrue(rows[0]["env_prefix"])

    def test_gate_without_name_or_command_is_refused(self):
        name = self.mutate("example-generic", 'command = "make lint"', 'command = ""')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("gate[1].command", caught.exception.message)

    def test_tracker_prefixes_that_keep_no_default_prefix_warn(self):
        # The gate then guards only the named server, and the briefer's read
        # tools are expanded from the same list: worth a word, not a refusal.
        name = self.mutate("example-generic",
                           'mcp_prefixes = ["mcp__atlassian__", "mcp__claude_ai_Atlassian_Rovo__"]',
                           'mcp_prefixes = ["mcp__nobody__"]')
        warnings = harness.validate(harness.load(name), name)
        self.assertTrue(any("tracker.mcp_prefixes" in w and "mcp__atlassian__" in w
                            for w in warnings), warnings)

    def test_tracker_prefixes_that_keep_a_default_prefix_add_no_warning(self):
        for prefixes in ('["mcp__atlassian__", "mcp__mine__"]',
                         '["mcp__claude_ai_Atlassian_Rovo__"]'):
            name = self.mutate("example-generic",
                               'mcp_prefixes = ["mcp__atlassian__", "mcp__claude_ai_Atlassian_Rovo__"]',
                               "mcp_prefixes = %s" % prefixes)
            warnings = harness.validate(harness.load(name), name)
            self.assertFalse(any("tracker.mcp_prefixes" in w for w in warnings), warnings)

    def test_no_focused_tests_gate_warns(self):
        name = self.mutate("example-generic", 'name = "focused-tests"', 'name = "unit-tests"')
        warnings = harness.validate(harness.load(name), name)
        self.assertTrue(any("focused-tests" in w and "ppl transition done" in w for w in warnings),
                        warnings)

    def test_gate_name_that_is_not_a_plain_word_is_refused(self):
        for bad in ("my gate", "../build", "a;b", "$(x)", "lint\\n"):
            name = self.mutate("example-generic", 'name = "lint"', 'name = "%s"' % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("gate[1].name", caught.exception.message, bad)

    def test_env_key_that_is_not_a_shell_identifier_is_refused(self):
        for bad in ('"JAVA HOME"', '"1JAVA"', '"A-B"', '"A;B"'):
            name = self.mutate("example-maven", "JAVA_HOME = ", "%s = " % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("[env]", caught.exception.message, bad)


class TestListAndAdd(HarnessTestCase):
    def test_list_names(self):
        fixtures.install_harness("example-generic")
        fixtures.install_harness("example-maven")
        self.assertEqual(harness.list_names(), ["example-generic", "example-maven"])

    def test_add_from_directory_copies_it(self):
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        target = harness.add(str(source), name="mine")
        self.assertEqual(target, paths.harness_dir("mine"))
        self.assertTrue((target / "harness.toml").is_file())
        self.assertEqual(harness.load("mine")["harness"]["name"], "example-generic")

    def test_add_refuses_an_existing_name(self):
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        harness.add(str(source), name="mine")
        with self.assertRaises(PplError) as caught:
            harness.add(str(source), name="mine")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_add_from_a_local_git_url_clones(self):
        repo = Path(tmphome.mkdtemp(prefix="pplharnessgit-"))
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        for item in source.iterdir():
            if item.is_dir():
                shutil.copytree(item, repo / item.name)
            else:
                (repo / item.name).write_bytes(item.read_bytes())
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "harness"], check=True)
        target = harness.add("file://" + str(repo), name="cloned")
        self.assertTrue((target / ".git").exists())
        self.assertEqual(harness.load("cloned")["harness"]["name"], "example-generic")


class TestMalformedInputIsRefusedNotATraceback(HarnessTestCase):
    def refused(self, name, field):
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn(field, caught.exception.message)

    def test_undecodable_file_is_refused(self):
        path = fixtures.install_harness("example-generic") / "harness.toml"
        path.write_bytes(b"[harness]\nname = \"\xff\xfe\"\n")
        self.refused("example-generic", "not valid TOML")

    def test_boolean_is_not_an_integer_format(self):
        name = self.mutate("example-maven", "format = 1", "format = true")
        self.refused(name, "harness.format")

    def test_tracker_list_with_a_non_string_is_refused(self):
        name = self.mutate("example-maven", READ_TOOLS_LINE,
                           "read_tools = [1]")
        self.refused(name, "tracker.read_tools")

    def test_tracker_prefix_not_shaped_like_an_mcp_server_is_refused(self):
        good = '"mcp__claude_ai_Atlassian_Rovo__"]'
        for bad in ('"mcp__corp_jira"]', '"corp_"]', '"mcp__x"]', '""]', '"mcp__a.b__"]'):
            name = self.mutate("example-maven", good, bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("tracker.mcp_prefixes[1]", caught.exception.message, bad)

    def test_both_example_harnesses_keep_well_shaped_tracker_prefixes(self):
        for name in ("example-maven", "example-generic"):
            fixtures.install_harness(name)
            harness.load(name)

    def test_both_example_harnesses_carry_the_adapter_default_tracker_lists(self):
        for name in ("example-maven", "example-generic"):
            fixtures.install_harness(name)
            tracker = harness.load(name)["tracker"]
            self.assertEqual(tracker["write_operations"], jira.DEFAULTS["write_operations"], name)
            self.assertEqual(tracker["read_tools"], jira.DEFAULTS["read_tools"], name)
            self.assertEqual(tracker["mcp_prefixes"], jira.DEFAULTS["mcp_prefixes"], name)

    def test_knowledge_skills_must_be_strings(self):
        name = self.mutate("example-maven", "skills = []", "skills = [1]")
        self.refused(name, "knowledge.skills")

    def test_a_read_tool_that_is_also_a_write_operation_is_refused(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        name = self.mutate("example-maven", READ_TOOLS_LINE,
                           'read_tools = ["getJiraIssue", "editJiraIssue"]')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("tracker.read_tools[1]", caught.exception.message)
        self.assertIn("editJiraIssue", caught.exception.message)

    def test_an_extra_tool_that_is_a_tracker_write_tool_is_refused(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        for bad in ("mcp__atlassian__transitionJiraIssue",
                    "mcp__claude_ai_Atlassian_Rovo__editJiraIssue",
                    "mcp__atlassian_2__addCommentToJiraIssue"):
            name = self.mutate("example-maven", "extra_tools = []",
                               'extra_tools = ["mcp__corp__code_search", "%s"]' % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("knowledge.extra_tools[1]", caught.exception.message, bad)
            self.assertIn(bad, caught.exception.message)

    def test_an_extra_tool_that_is_not_a_tracker_write_tool_is_accepted(self):
        name = self.mutate("example-maven", "extra_tools = []",
                           'extra_tools = ["mcp__corp__code_search", "mcp__GitLab-MCP__get_file.v2"]')
        self.assertEqual(harness.load(name)["knowledge"]["extra_tools"],
                         ["mcp__corp__code_search", "mcp__GitLab-MCP__get_file.v2"])

    def test_a_tool_entry_that_is_not_a_single_tool_name_is_refused(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        # One string holding several names, or a name with a newline, would
        # reach the tools line of an agent file as more than one tool.
        smuggled = "mcp__atlassian__editJiraIssue"
        extra_cases = ["mcp__corp__a, " + smuggled,
                       "mcp__corp__a,%s" % smuggled,
                       " mcp__corp__a",
                       "mcp__corp__a ",
                       "mcp__corp__a\\nmodel: other",
                       "mcp__corp__a\\n",
                       "mcp__corp__a]",
                       ""]
        for bad in extra_cases:
            name = self.mutate("example-maven", "extra_tools = []", 'extra_tools = ["%s"]' % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("knowledge.extra_tools[0]", caught.exception.message, bad)
        read_cases = ["getJiraIssue, " + smuggled, " getJiraIssue", "getJiraIssue\\n", "get Jira", ""]
        for bad in read_cases:
            name = self.mutate("example-maven",
                               READ_TOOLS_LINE,
                               'read_tools = ["getJiraIssue", "%s"]' % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("tracker.read_tools[1]", caught.exception.message, bad)

    def test_a_built_in_tool_cannot_be_granted_through_extra_tools(self):
        # rule: R-ROLE-TOOLS-READONLY
        for bad in ('"Write"', '"Bash"', '"Agent", "Write"'):
            name = self.mutate("example-maven", "extra_tools = []", "extra_tools = [%s]" % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("knowledge.extra_tools[0]", caught.exception.message, bad)

    def test_gate_that_is_not_an_array_of_tables_is_refused(self):
        fixtures.install_harness("example-generic")
        doc = harness.load("example-generic")
        for bad in ("oops", {"name": "build", "command": "make build"}):
            doc["gate"] = bad
            with self.assertRaises(PplError) as caught:
                harness.validate(doc, "example-generic")
            self.assertEqual(caught.exception.code, "HARNESS_INVALID")
            self.assertIn("array of tables", caught.exception.message)

    def test_non_string_gate_command_is_refused(self):
        name = self.mutate("example-generic", 'command = "make lint"', "command = 5")
        self.refused(name, "gate[1].command")

    def test_one_goal_of_must_be_a_list(self):
        name = self.mutate("example-generic", 'one_goal_of = ["build", "lint", "test", "integration"]',
                           'one_goal_of = "build"')
        self.refused(name, "guard.shape[0].one_goal_of")

    def test_env_prefix_must_be_a_boolean(self):
        name = self.mutate("example-generic", "env_prefix = true", 'env_prefix = "no"')
        self.refused(name, "guard.shape[0].env_prefix")

    def test_bad_free_args_regex_is_refused(self):
        name = self.mutate("example-generic", 'free_args = "^[A-Z_]+=[A-Za-z0-9_./:,-]*$"',
                           'free_args = "^("')
        self.refused(name, "guard.shape[0].free_args")

    def test_bad_flag_value_regex_is_refused(self):
        name = self.mutate("example-maven", '["-am", ""]', '["-am", "^("]')
        self.refused(name, "guard.shape[0]")

    def test_shape_rows_reject_non_table_guard(self):
        for doc in ({"guard": 5}, {"guard": {"shape": "x"}}, {"guard": {"shape": [5]}}):
            with self.assertRaises(PplError) as caught:
                harness.shape_rows(doc)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_shape_rows_of_a_doc_without_guard_is_empty(self):
        self.assertEqual(harness.shape_rows({}), [])


class TestAddEdges(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.example = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"

    def test_name_defaults_to_the_source_basename(self):
        copy = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "team-harness"
        shutil.copytree(self.example, copy)
        self.assertEqual(harness.add(str(copy)), paths.harness_dir("team-harness"))

    def test_name_default_survives_a_trailing_slash(self):
        copy = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "team-harness"
        shutil.copytree(self.example, copy)
        self.assertEqual(harness.add(str(copy) + "/"), paths.harness_dir("team-harness"))

    def test_directory_without_harness_toml_is_refused(self):
        empty = Path(tmphome.mkdtemp(prefix="pplharnesssrc-"))
        with self.assertRaises(PplError) as caught:
            harness.add(str(empty), name="empty")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertFalse(paths.harness_dir("empty").exists())

    def test_invalid_harness_leaves_nothing_behind(self):
        copy = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "broken"
        shutil.copytree(self.example, copy)
        broken = (copy / "harness.toml").read_text(encoding="utf-8").replace("format = 1", "format = 9")
        (copy / "harness.toml").write_text(broken, encoding="utf-8")
        with self.assertRaises(PplError):
            harness.add(str(copy), name="broken")
        self.assertFalse(paths.harness_dir("broken").exists())
        fixed = broken.replace("format = 9", "format = 1")
        (copy / "harness.toml").write_text(fixed, encoding="utf-8")
        self.assertTrue(harness.add(str(copy), name="broken").is_dir())

    def test_failed_copy_is_refused_and_leaves_nothing(self):
        copy = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "dangling"
        shutil.copytree(self.example, copy)
        os.symlink("no-such-file", copy / "dangling-link")
        with self.assertRaises(PplError) as caught:
            harness.add(str(copy), name="dangling")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("copy failed", caught.exception.message)
        self.assertFalse(paths.harness_dir("dangling").exists())
        (copy / "dangling-link").unlink()
        self.assertTrue(harness.add(str(copy), name="dangling").is_dir())

    def test_clone_never_prompts_and_is_bounded(self):
        completed = subprocess.CompletedProcess([], 1, "", "fatal: could not read Username")
        with mock.patch.object(harness.subprocess, "run", return_value=completed) as run:
            with self.assertRaises(PplError):
                harness.add("https://example.invalid/team/harness.git", name="private")
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["timeout"], 120)
        self.assertFalse(paths.harness_dir("private").exists())

    def test_clone_timeout_is_refused_and_leaves_nothing(self):
        def slow_clone(argv, **kwargs):
            Path(argv[-1]).mkdir(parents=True)
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

        with mock.patch.object(harness.subprocess, "run", side_effect=slow_clone):
            with self.assertRaises(PplError) as caught:
                harness.add("https://example.invalid/team/harness.git", name="slow")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("timed out", caught.exception.message)
        self.assertFalse(paths.harness_dir("slow").exists())

    def test_bad_name_is_refused(self):
        with self.assertRaises(PplError) as caught:
            harness.add(str(self.example), name="../escape")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_failed_clone_is_refused_and_leaves_nothing(self):
        missing = "file://" + tmphome.mkdtemp(prefix="pplnorepo-") + "/absent"
        with self.assertRaises(PplError) as caught:
            harness.add(missing, name="ghost")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("git clone failed", caught.exception.message)
        self.assertFalse(paths.harness_dir("ghost").exists())

    def test_clone_of_a_repo_without_harness_toml_leaves_nothing(self):
        repo = Path(tmphome.mkdtemp(prefix="pplharnessgit-"))
        (repo / "README").write_text("not a harness\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "x"], check=True)
        with self.assertRaises(PplError):
            harness.add("file://" + str(repo), name="plain")
        self.assertFalse(paths.harness_dir("plain").exists())


def call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class TestHarnessCli(HarnessTestCase):
    def test_list_prints_installed_names(self):
        fixtures.install_harness("example-generic")
        fixtures.install_harness("example-maven")
        code, out, _ = call("harness", "list")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["example-generic", "example-maven"])

    def test_list_of_an_empty_home_prints_nothing(self):
        code, out, _ = call("harness", "list")
        self.assertEqual((code, out), (0, ""))

    def test_validate_reports_valid(self):
        fixtures.install_harness("example-maven")
        code, out, err = call("harness", "validate", "example-maven")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.strip(), "harness example-maven is valid")

    def test_validate_prints_warnings(self):
        self.mutate("example-generic", 'one_goal_of = ["build", "lint", "test", "integration"]',
                    "one_goal_of = []")
        code, out, _ = call("harness", "validate", "example-generic")
        self.assertEqual(code, 0)
        self.assertIn("warning: guard.shape[0] (make) has no required_flags and no one_goal_of", out)
        self.assertIn("harness example-generic is valid", out)

    def test_validate_a_path(self):
        target = fixtures.install_harness("example-maven", as_name="elsewhere")
        code, out, _ = call("harness", "validate", str(target))
        self.assertEqual(code, 0)
        self.assertIn("harness example-maven is valid", out)

    def test_validate_warns_when_the_document_name_drifts_from_the_directory(self):
        fixtures.install_harness("example-maven", as_name="mine")
        code, out, _ = call("harness", "validate", "mine")
        self.assertEqual(code, 0)
        self.assertIn("warning: ", out)
        self.assertIn("mine", out.splitlines()[0])

    def test_validate_refuses_an_invalid_harness_with_exit_2(self):
        self.mutate("example-maven", 'kind = "gitlab"', 'kind = "bitbucket"')
        code, _, err = call("harness", "validate", "example-maven")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_INVALID")
        self.assertIn("forge.kind", json.loads(err)["message"])

    def test_show_prints_the_parsed_document(self):
        fixtures.install_harness("example-maven")
        code, out, _ = call("harness", "show", "example-maven")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["harness"]["name"], "example-maven")
        self.assertEqual(doc["forge"]["mcp_server"], "GitLab-MCP")
        self.assertEqual(doc["_dir"], str(paths.harness_dir("example-maven")))

    def test_add_copies_a_directory(self):
        source = fixtures.SOURCE / "defaults" / "harnesses" / "example-generic"
        code, out, _ = call("harness", "add", "mine", "--source", str(source))
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "added %s" % paths.harness_dir("mine"))
        self.assertTrue((paths.harness_dir("mine") / "harness.toml").is_file())

    def test_add_without_source_is_refused(self):
        code, _, err = call("harness", "add", "mine")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_validate_and_show_without_a_name_are_refused(self):
        for action in ("validate", "show"):
            code, _, err = call("harness", action)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def apply_dirs(self):
        agents = tmphome.mkdtemp(prefix="pplagents-")
        skills = tmphome.mkdtemp(prefix="pplskills-")
        patcher = mock.patch.dict(os.environ, {"PPL_AGENTS_DIR": agents, "PPL_SKILLS_DIR": skills})
        patcher.start()
        self.addCleanup(patcher.stop)
        return Path(agents), Path(skills)

    def test_apply_writes_agents_and_skill_and_warns_about_unverified_models(self):
        fixtures.install_harness("example-maven")
        fixtures.install_routing()
        agents, skills = self.apply_dirs()
        with mock.patch.object(models, "claude_version", lambda binary="claude": "9.9.9"):
            code, out, err = call("harness", "apply", "example-maven")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("wrote %s" % (agents / "ppl-code-worker.md"), out)
        self.assertIn("wrote %s" % (skills / "pipeline" / "SKILL.md"), out)
        self.assertIn("active harness: example-maven", out)
        self.assertIn("warning: models not verified: ", out)
        self.assertIn("(unverified)", out)
        self.assertIn("run ppl models verify", out)
        self.assertEqual(config.active_harness(), "example-maven")

    def test_apply_prints_no_warning_when_every_model_is_verified(self):
        fixtures.install_harness("example-maven")
        fixtures.install_routing()
        self.apply_dirs()
        verified = [{"tier": "t", "model": "m", "resolved_id": "m-1", "state": "verified"}]
        with mock.patch.object(models, "status", lambda risk=None, binary="claude": verified):
            code, out, _ = call("harness", "apply", "example-maven")
        self.assertEqual(code, 0)
        self.assertNotIn("warning", out)

    def test_apply_without_a_name_is_refused(self):
        code, _, err = call("harness", "apply")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_unknown_action_is_a_json_usage_error(self):
        code, _, err = call("harness", "frobnicate")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")



class TestSnapshotAndApplied(HarnessTestCase):
    def activate(self, name):
        paths.config_file().write_text('[harness]\nactive = "%s"\n' % name, encoding="utf-8")

    def test_snapshot_carries_exactly_what_the_guard_enforces(self):
        fixtures.install_harness("example-maven")
        doc = harness.load("example-maven")
        snap = harness.snapshot(doc)
        self.assertEqual(set(snap), {"format", "name", "task", "tracker", "forge", "shapes"})
        self.assertEqual(snap["format"], harness.FORMAT)
        self.assertEqual(snap["name"], "example-maven")
        self.assertEqual(snap["task"], {"key_pattern": doc["task"]["key_pattern"]})
        self.assertEqual(snap["shapes"], harness.shape_rows(doc))
        self.assertEqual(json.loads(json.dumps(snap)), snap)

    def test_snapshot_is_named_after_the_installed_directory(self):
        fixtures.install_harness("example-maven", as_name="mine")
        snap = harness.snapshot(harness.load("mine"))
        self.assertEqual(snap["name"], "mine")

    def test_snapshot_does_not_alias_the_loaded_harness(self):
        fixtures.install_harness("example-maven")
        doc = harness.load("example-maven")
        snap = harness.snapshot(doc)
        before = json.loads(json.dumps(snap))
        doc["tracker"]["mcp_prefixes"].append("mcp__later__")
        doc["tracker"]["write_operations"].append("later")
        doc["forge"]["kind"] = "changed"
        self.assertEqual(snap, before)
        snap["tracker"]["mcp_prefixes"].append("mcp__snap__")
        snap["tracker"]["kind"] = "changed"
        snap["forge"]["kind"] = "changed"
        self.assertNotIn("mcp__snap__", doc["tracker"]["mcp_prefixes"])
        self.assertEqual(doc["tracker"]["kind"], "jira")

    def test_require_applied_passes_for_the_active_installed_directory(self):
        fixtures.install_harness("example-maven")
        self.activate("example-maven")
        self.assertIsNone(harness.require_applied(harness.load("example-maven")))

    def test_require_applied_refuses_another_installed_harness_and_names_the_fix(self):
        fixtures.install_harness("example-maven")
        fixtures.install_harness("example-generic")
        self.activate("example-maven")
        with self.assertRaises(PplError) as caught:
            harness.require_applied(harness.load("example-generic"))
        self.assertEqual(caught.exception.code, "HARNESS_NOT_APPLIED")
        self.assertIn("ppl harness apply example-generic", caught.exception.message)
        self.assertIn("active: example-maven", caught.exception.message)

    def test_require_applied_with_no_config_says_none_is_active(self):
        fixtures.install_harness("example-maven")
        with self.assertRaises(PplError) as caught:
            harness.require_applied(harness.load("example-maven"))
        self.assertEqual(caught.exception.code, "HARNESS_NOT_APPLIED")
        self.assertIn("active: none", caught.exception.message)

    def test_require_applied_is_keyed_on_the_directory_not_the_document_name(self):
        # The document inside "mine" still says name = "example-maven".
        fixtures.install_harness("example-maven", as_name="mine")
        self.activate("example-maven")
        with self.assertRaises(PplError) as caught:
            harness.require_applied(harness.load("mine"))
        self.assertEqual(caught.exception.code, "HARNESS_NOT_APPLIED")
        self.assertIn("ppl harness apply mine", caught.exception.message)
        self.activate("mine")
        self.assertIsNone(harness.require_applied(harness.load("mine")))

    def test_require_applied_refuses_a_directory_outside_the_data_root(self):
        fixtures.install_harness("example-maven")
        self.activate("example-maven")
        outside = Path(tmphome.mkdtemp(prefix="pplharness-")) / "example-maven"
        shutil.copytree(fixtures.SOURCE / "defaults" / "harnesses" / "example-maven", outside)
        with self.assertRaises(PplError) as caught:
            harness.require_applied(harness.load(str(outside)))
        self.assertEqual(caught.exception.code, "HARNESS_NOT_APPLIED")
        self.assertIn("is not an installed harness", caught.exception.message)
        self.assertIn("ppl harness add", caught.exception.message)

    def test_a_document_name_that_differs_from_its_directory_is_a_validate_warning(self):
        fixtures.install_harness("example-maven", as_name="mine")
        doc = harness.load("mine")
        warnings = harness.validate(doc, "mine")
        self.assertTrue(any('"example-maven"' in w and '"mine"' in w for w in warnings), warnings)

    def test_a_matching_document_name_adds_no_warning(self):
        fixtures.install_harness("example-maven")
        self.assertEqual(harness.validate(harness.load("example-maven"), "example-maven"), [])


class TestRenderSkill(HarnessTestCase):
    def render(self, name, installed=None):
        fixtures.install_harness(name, as_name=installed)
        return harness.render_skill(harness.load(installed or name))

    def skeleton_root(self, skill_text):
        """A code root whose only skeleton is `skill_text`, templates kept so
        the agents would render."""
        root = Path(tmphome.mkdtemp(prefix="pplroot-"))
        (root / "defaults").mkdir()
        (root / "skill").mkdir()
        (root / "skill" / "SKILL.md.skeleton").write_text(skill_text, encoding="utf-8")
        shutil.copytree(fixtures.SOURCE / "templates", root / "templates")
        patcher = mock.patch.dict(os.environ, {"PPL_ROOT": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_marker_is_replaced(self):
        # rule: R-SKILL-GENERATED
        fixtures.install_harness("example-maven")
        rendered = harness.render_skill(harness.load("example-maven"))
        for path, text in rendered.items():
            self.assertNotIn("<!-- include:", text, path)
        self.assertIn("SKILL.md", rendered)
        self.assertIn("references/gates.md", rendered)
        self.assertIn("references/brief.md", rendered)

    def test_the_tracker_close_note_defaults_to_a_neutral_comment(self):
        for name in ("example-maven", "example-generic"):
            text = self.render(name)["SKILL.md"]
            self.assertIn("write to the tracker: %s." % harness.DEFAULT_TRACKER_CLOSE, text, name)

    def test_a_tracker_close_fragment_replaces_the_default(self):
        directory = fixtures.install_harness("example-generic")
        (directory / "skill" / "tracker-close.md").write_text(
            "a comment saying what was done,\nplus the custom fields the team tracks\n",
            encoding="utf-8")
        text = harness.render_skill(harness.load("example-generic"))["SKILL.md"]
        self.assertIn("write to the tracker: a comment saying what was done, plus the custom "
                      "fields the team tracks.", text)
        self.assertNotIn(harness.DEFAULT_TRACKER_CLOSE, text)

    def test_fragments_get_the_default_branch_from_the_harness(self):
        name = self.mutate("example-maven", 'default_branch = "master"', 'default_branch = "trunk"')
        directory = paths.harness_dir(name) / "skill"
        self.assertIn("origin/<default-branch>...HEAD",
                      (directory / "extra-gates.md").read_text(encoding="utf-8"))
        for fragment in ("baseline", "gates", "tracker-close"):
            path = directory / ("%s.md" % fragment)
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            path.write_text(old + "\nbranch <default-branch> here\n", encoding="utf-8")
        rendered = harness.render_skill(harness.load(name))
        for relative, text in rendered.items():
            self.assertNotIn("<default-branch>", text, relative)
        self.assertIn("origin/trunk...HEAD | grep -E 'src/test/", rendered["references/gates.md"])
        self.assertIn("branch trunk here", rendered["references/gates.md"])
        self.assertGreaterEqual(rendered["SKILL.md"].count("branch trunk here"), 2)

    def test_rendered_skill_carries_the_notice_and_the_forge_text(self):
        fixtures.install_harness("example-maven")
        text = harness.render_skill(harness.load("example-maven"))["SKILL.md"]
        self.assertTrue(text.startswith("---\n"))
        self.assertIn(harness.SKILL_NOTICE, text)
        self.assertIn("GitLab MCP", text)
        self.assertNotIn("pull request", text.lower())
        fixtures.install_harness("example-generic")
        text = harness.render_skill(harness.load("example-generic"))["SKILL.md"]
        self.assertIn("gh pr create", text)
        self.assertNotIn("merge request", text.lower())

    def test_gate_recipes_render_with_rc_files_and_evidence_lines(self):
        fixtures.install_harness("example-maven")
        gates = harness.render_skill(harness.load("example-maven"))["references/gates.md"]
        self.assertIn("mvn install -pl :<module> -am -DskipTests -Dmaven.gitcommitid.skip=true "
                      "> /tmp/ppl-<TASK-ID>/install.log 2>&1; echo $? > /tmp/ppl-<TASK-ID>/install.rc", gates)
        self.assertIn("ppl evidence add --check install --result <pass|fail from install.rc>", gates)
        self.assertIn("ppl evidence add --check focused-tests", gates)
        self.assertIn("ppl evidence add --check secret-scan", gates)
        self.assertIn("JAVA_HOME=/opt/jdk mvn", gates)

    def test_project_specific_gate_steps_come_from_the_harness(self):
        maven = self.render("example-maven")["references/gates.md"]
        generic = self.render("example-generic")["references/gates.md"]
        self.assertIn("ppl evidence add --check test-lint", maven)
        self.assertIn("git -C <worktree> diff --name-only origin/master...HEAD | "
                      "grep -E 'src/test/.*\\.java$'", maven)
        self.assertLess(maven.index("--check integration-tests"), maven.index("--check test-lint"))
        self.assertLess(maven.index("--check test-lint"), maven.index("--check secret-scan"))
        self.assertNotIn("test-lint", generic)
        self.assertNotIn("src/test/", generic)
        for gates in (maven, generic):
            self.assertIn("ppl evidence add --check secret-scan", gates)

    def test_write_skill_and_apply_record_the_active_harness(self):
        fixtures.install_harness("example-maven")
        fixtures.install_routing()
        agents = Path(tmphome.mkdtemp(prefix="pplagents-"))
        skills = Path(tmphome.mkdtemp(prefix="pplskills-"))
        result = harness.apply("example-maven", agents_dir=agents, skills_dir=skills)
        self.assertTrue((skills / "pipeline" / "SKILL.md").is_file())
        self.assertTrue((skills / "pipeline" / "references" / "gates.md").is_file())
        self.assertTrue((agents / "ppl-code-worker.md").is_file())
        self.assertEqual(result["active"], "example-maven")
        self.assertEqual(config.active_harness(), "example-maven")
        self.assertEqual(sorted(result["skill"]),
                         sorted(str(skills / "pipeline" / p) for p in
                                harness.render_skill(harness.load("example-maven"))))

    def test_the_notice_sits_right_after_the_frontmatter(self):
        text = self.render("example-maven")["SKILL.md"]
        head, sep, tail = text.partition("\n---\n")
        self.assertEqual(sep, "\n---\n")
        self.assertIn("\nname: pipeline\n", head + "\n")
        self.assertTrue(tail.startswith(harness.SKILL_NOTICE + "\n"), tail[:200])

    def test_the_skill_name_is_the_frontmatter_name(self):
        fixtures.install_harness("example-maven")
        text = harness.render_skill(harness.load("example-maven"), skill_name="pipeline-b")["SKILL.md"]
        self.assertIn("\nname: pipeline-b\n", text)
        self.assertNotIn("\nname: pipeline\n", text)

    def test_plain_files_are_copied_as_they_are_and_skeletons_lose_the_suffix(self):
        rendered = self.render("example-maven")
        self.assertFalse([path for path in rendered if path.endswith(".skeleton")])
        source = fixtures.SOURCE / "skill" / "references" / "brief.md"
        self.assertEqual(rendered["references/brief.md"], source.read_text(encoding="utf-8"))
        self.assertNotIn(harness.SKILL_NOTICE, rendered["references/gates.md"])

    def test_harness_values_land_in_the_skill(self):
        text = self.render("example-maven")["SKILL.md"]
        self.assertIn("--harness example-maven", text)
        self.assertIn("cd /home/dev/src/project\n", text)
        self.assertIn("git fetch origin master\n", text)
        self.assertIn("git worktree add /home/dev/src/project/.worktrees/<repo-name>-<TASK-ID> "
                      "-b <TASK-ID>-<short-slug> origin/master\n", text)
        self.assertIn("the names of tests that already fail on the base revision", text)
        self.assertIn("(no knowledge skills are configured for this harness)", text)
        generic = self.render("example-generic")
        self.assertIn("git fetch origin main\n", generic["SKILL.md"])
        self.assertIn("diff origin/main...HEAD", generic["references/gates.md"])
        self.assertIn("make test TEST=<test_class> > /tmp/ppl-<TASK-ID>/focused-tests.log 2>&1",
                      generic["references/gates.md"])

    def test_the_harness_name_in_the_skill_is_the_installed_directory(self):
        # The document inside "mine" still says name = "example-maven", and
        # ppl init --harness resolves a directory name.
        text = self.render("example-maven", installed="mine")["SKILL.md"]
        self.assertIn("--harness mine", text)
        self.assertNotIn("--harness example-maven", text)

    def test_the_baseline_fragment_stays_on_its_table_row(self):
        baseline = self.render("example-maven")["references/baseline.md"]
        row = next(line for line in baseline.splitlines() if line.startswith("| anything else"))
        self.assertIn("the names of tests that already fail on the base revision", row)
        self.assertIn("| No - ", row)

    def test_knowledge_skills_are_named(self):
        self.mutate("example-maven", "skills = []", 'skills = ["kb-core", "kb-build"]')
        text = harness.render_skill(harness.load("example-maven"))["SKILL.md"]
        self.assertIn("(`kb-core`, `kb-build`)", text)

    def test_an_unknown_marker_is_refused(self):
        # rule: R-SKILL-GENERATED
        self.skeleton_root("---\nname: pipeline\n---\n<!-- include: forge/nothing -->\n")
        fixtures.install_harness("example-maven")
        with self.assertRaises(PplError) as caught:
            harness.render_skill(harness.load("example-maven"))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("forge/nothing", caught.exception.message)

    def test_a_malformed_marker_is_refused_not_passed_through(self):
        # rule: R-SKILL-GENERATED
        self.skeleton_root("---\nname: pipeline\n---\n<!-- include:  vocab/noun -->\n")
        fixtures.install_harness("example-maven")
        with self.assertRaises(PplError) as caught:
            harness.render_skill(harness.load("example-maven"))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("SKILL.md", caught.exception.message)

    def test_a_failed_render_writes_nothing_and_activates_nothing(self):
        self.skeleton_root("---\nname: pipeline\n---\n<!-- include: forge/nothing -->\n")
        fixtures.install_harness("example-maven")
        fixtures.install_routing()
        agents = Path(tmphome.mkdtemp(prefix="pplagents-"))
        skills = Path(tmphome.mkdtemp(prefix="pplskills-"))
        with self.assertRaises(PplError):
            harness.apply("example-maven", agents_dir=agents, skills_dir=skills)
        self.assertEqual(list(agents.iterdir()), [])
        self.assertEqual(list(skills.iterdir()), [])
        self.assertIsNone(config.active_harness())

    def test_apply_records_the_installed_directory_name(self):
        fixtures.install_harness("example-maven", as_name="mine")
        fixtures.install_routing()
        skills = Path(tmphome.mkdtemp(prefix="pplskills-"))
        result = harness.apply("mine", agents_dir=tmphome.mkdtemp(prefix="pplagents-"),
                               skills_dir=skills)
        self.assertEqual(result["active"], "mine")
        self.assertEqual(config.active_harness(), "mine")
        self.assertIsNone(harness.require_applied(harness.load("mine")))

    def test_apply_refuses_a_harness_outside_the_data_root(self):
        fixtures.install_routing()
        outside = Path(tmphome.mkdtemp(prefix="pplharness-")) / "example-maven"
        shutil.copytree(fixtures.SOURCE / "defaults" / "harnesses" / "example-maven", outside)
        agents = Path(tmphome.mkdtemp(prefix="pplagents-"))
        with self.assertRaises(PplError) as caught:
            harness.apply(str(outside), agents_dir=agents,
                          skills_dir=tmphome.mkdtemp(prefix="pplskills-"))
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("ppl harness add", caught.exception.message)
        self.assertEqual(list(agents.iterdir()), [])
        self.assertIsNone(config.active_harness())

    def test_apply_keeps_the_agents_dir_environment_as_it_was(self):
        fixtures.install_harness("example-maven")
        fixtures.install_routing()
        with mock.patch.dict(os.environ, {"PPL_AGENTS_DIR": "/nonexistent/keep"}):
            harness.apply("example-maven", agents_dir=tmphome.mkdtemp(prefix="pplagents-"),
                          skills_dir=tmphome.mkdtemp(prefix="pplskills-"))
            self.assertEqual(os.environ["PPL_AGENTS_DIR"], "/nonexistent/keep")


if __name__ == "__main__":
    unittest.main()

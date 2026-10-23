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
from ppl import boundary, cli, config, forges, harness, models, os_sandbox, paths
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

    def test_no_focused_tests_gate_is_refused(self):
        # rule: R-HARNESS-FOCUSED-TESTS-REQUIRED
        name = self.mutate("example-generic", 'name = "focused-tests"', 'name = "unit-tests"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("focused-tests", caught.exception.message)
        self.assertIn("ppl transition done", caught.exception.message)

    def test_a_focused_tests_command_without_the_test_class_placeholder_is_refused(self):
        # rule: R-HARNESS-FOCUSED-TESTS-REQUIRED
        name = self.mutate("example-generic", 'command = "make test TEST=<test_class>"',
                           'command = "make test"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("<test_class>", caught.exception.message)
        self.assertIn("gate[2]", caught.exception.message)

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


class TestStrictValidation(HarnessTestCase):
    def refused(self, name):
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        return caught.exception.message

    def write(self, name, rel, text="note\n"):
        path = paths.harness_dir(name) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_gate_placeholder_the_orchestrator_cannot_fill_is_refused(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        name = self.mutate("example-generic", 'command = "make build"',
                           'command = "make build TARGET=<target>"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("gate[0].command", caught.exception.message)
        self.assertIn("<target>", caught.exception.message)
        self.assertIn("<test_class>", caught.exception.message)

    def test_a_misspelt_test_class_placeholder_is_refused(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        name = self.mutate("example-generic", 'command = "make test TEST=<test_class>"',
                           'command = "make test TEST=<test_class> ONLY=<testclass>"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("<testclass>", caught.exception.message)

    def test_the_run_time_placeholders_are_accepted_in_any_gate(self):
        name = self.mutate("example-generic", 'command = "make build"',
                           'command = "make build DIR=<worktree> T=<TASK-ID> S=<SHA> G=<GENERATION>"')
        self.assertEqual(harness.validate(harness.load(name), name), [])

    def test_a_placeholder_the_harness_gates_fragment_names_is_accepted(self):
        # example-maven's gates.md says what <module> stands for
        fixtures.install_harness("example-maven")
        doc = harness.load("example-maven")
        self.assertIn("<module>", doc["gate"][0]["command"])
        self.assertEqual(harness.validate(doc, "example-maven"), [])
        (paths.harness_dir("example-maven") / "skill" / "gates.md").unlink()
        self.assertIn("<module>", self.refused("example-maven"))

    def test_a_fragment_that_only_echoes_a_command_does_not_vouch_for_its_placeholder(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        name = self.mutate("example-generic", 'command = "make build"',
                           'command = "make build TARGET=<target>"')
        gates = paths.harness_dir(name) / "skill" / "gates.md"
        gates.write_text("Targets: `make build TARGET=<target>`, `make lint`.\n", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("<target>", caught.exception.message)
        gates.write_text("Targets: `make build TARGET=<target>`, where <target> is the make "
                         "target of the module.\n", encoding="utf-8")
        self.assertEqual(harness.validate(harness.load(name), name), [])

    def test_an_overlay_for_an_unknown_role_is_refused(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        fixtures.install_harness("example-generic")
        self.write("example-generic", "templates/ppl-codeworker.part")
        with self.assertRaises(PplError) as caught:
            harness.load("example-generic")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("templates/ppl-codeworker.part", caught.exception.message)
        self.assertIn("ppl-code-worker", caught.exception.message)

    def test_an_unknown_table_is_refused_naming_the_nearest_known_one(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        name = self.mutate("example-generic", "[[gate]]", "[[gates]]")
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("unknown table [gates]", caught.exception.message)
        self.assertIn("[gate]", caught.exception.message)

    def test_an_unknown_key_is_refused_naming_the_nearest_known_one(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        cases = (("[[guard.shape]]", "[[guard.shapes]]", "guard.shapes", "guard.shape"),
                 ('command = "make lint"', 'comand = "make lint"', "gate[1].comand",
                  "gate[1].command"),
                 ("one_goal_of = [", "one_goals_of = [", "guard.shape[0].one_goals_of",
                  "guard.shape[0].one_goal_of"),
                 ('default_branch = "main"', 'default_brench = "main"', "repo.default_brench",
                  "repo.default_branch"))
        for old, new, unknown, nearest in cases:
            with self.subTest(unknown=unknown):
                name = self.mutate("example-generic", old, new)
                with self.assertRaises(PplError) as caught:
                    harness.load(name)
                self.assertIn("unknown key %s" % unknown, caught.exception.message)
                self.assertIn(nearest, caught.exception.message)

    def test_a_github_key_on_a_jira_tracker_is_refused_naming_the_nearest_key(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        # tracker.repo is a key of the github-issues adapter, so only the check
        # against the jira adapter's own keys can refuse it here.
        self.assertIn("repo", harness.KNOWN_KEYS["tracker"])
        name = self.mutate("example-generic", 'kind = "jira"', 'kind = "jira"\nrepo = "o/r"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("unknown key tracker.repo", caught.exception.message)
        self.assertIn("nearest known key is tracker.read_tools", caught.exception.message)

    def test_a_jira_key_on_a_github_issues_tracker_is_refused_naming_the_nearest_key(self):
        tracker = dict(fixtures.GITHUB_ISSUES["tracker"], read_tools=["getJiraIssue"])
        fixtures.install_variant("example-generic", "gh-extra", tracker=tracker,
                                 task=fixtures.GITHUB_ISSUES["task"])
        with self.assertRaises(PplError) as caught:
            harness.load("gh-extra")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("unknown key tracker.read_tools", caught.exception.message)
        self.assertIn("nearest known key is tracker.", caught.exception.message)

    def test_a_github_issues_tracker_without_an_owner_name_repo_is_refused(self):
        tracker = dict(fixtures.GITHUB_ISSUES["tracker"], repo="https://github.com/o/r")
        fixtures.install_variant("example-generic", "gh-bad", tracker=tracker,
                                 task=fixtures.GITHUB_ISSUES["task"])
        with self.assertRaises(PplError) as caught:
            harness.load("gh-bad")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("tracker.repo 'https://github.com/o/r' is not an owner/name pair",
                      caught.exception.message)

    def test_a_harness_of_another_format_is_told_so_before_its_keys_are_judged(self):
        name = self.mutate("example-generic", "format = 1", 'format = 2\nnew_key = "x"')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("harness.format 2 is not supported", caught.exception.message)

    def test_a_top_level_key_outside_any_table_is_refused(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        name = self.mutate("example-generic", "[harness]", 'nmae = "x"\n[harness]')
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertIn("unknown key nmae", caught.exception.message)

    def test_env_keys_stay_free(self):
        name = self.mutate("example-maven", 'JAVA_HOME = "/opt/jdk"',
                           'JAVA_HOME = "/opt/jdk"\nMAVEN_OPTS = "-Xmx2g"\nANY_NAME_AT_ALL = "x"')
        self.assertEqual(harness.validate(harness.load(name), name), [])

    def test_a_file_under_skill_that_is_no_fragment_is_warned_about(self):
        fixtures.install_harness("example-generic")
        self.write("example-generic", "skill/gate.md")
        self.write("example-generic", "skill/notes/more.md")
        warnings = harness.validate(harness.load("example-generic"), "example-generic")
        for rel in ("skill/gate.md", "skill/notes/more.md"):
            self.assertTrue(any(rel in w and "never read" in w and "skill/gates.md" in w
                                for w in warnings), (rel, warnings))

    def test_a_file_under_templates_that_is_not_a_part_is_warned_about(self):
        fixtures.install_harness("example-generic")
        self.write("example-generic", "templates/ppl-code-worker.md")
        warnings = harness.validate(harness.load("example-generic"), "example-generic")
        self.assertTrue(any("templates/ppl-code-worker.md" in w and "never read" in w
                            and "templates/ppl-code-worker.part" in w for w in warnings), warnings)

    def test_dot_files_are_not_warned_about(self):
        fixtures.install_harness("example-generic")
        self.write("example-generic", "skill/.gitkeep")
        self.write("example-generic", "templates/.DS_Store")
        self.assertEqual(harness.validate(harness.load("example-generic"), "example-generic"), [])


class TestVerify(HarnessTestCase):
    def setUp(self):
        super().setUp()
        claude_home = tmphome.mkdtemp(prefix="pplclaudehome-")
        self.skills = Path(tmphome.mkdtemp(prefix="pplskills-"))
        patcher = mock.patch.dict(os.environ, {"PPL_CLAUDE_HOME": claude_home,
                                               "PPL_SKILLS_DIR": str(self.skills)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.claude_skills = Path(claude_home) / "skills"

    def overlay(self, name, role, size):
        path = paths.harness_dir(name) / "templates" / ("%s.part" % role)
        path.write_text("x" * size + "\n", encoding="utf-8")

    def test_both_examples_verify_clean(self):
        for name in ("example-generic", "example-maven"):
            fixtures.install_harness(name)
            self.assertEqual(harness.verify(name, name), {"errors": [], "warnings": []}, name)

    def test_the_caps_are_one_and_a_half_and_three_times_the_largest_example(self):
        self.assertEqual(harness.SKILL_SOFT_BYTES, harness.SKILL_MEASURED_BYTES * 3 // 2)
        self.assertEqual(harness.SKILL_HARD_BYTES, harness.SKILL_MEASURED_BYTES * 3)
        self.assertEqual(harness.AGENT_SOFT_BYTES, harness.AGENT_MEASURED_BYTES * 3 // 2)
        self.assertEqual(harness.AGENT_HARD_BYTES, harness.AGENT_MEASURED_BYTES * 3)
        self.assertEqual(harness.REFERENCE_SOFT_BYTES, harness.REFERENCE_MEASURED_BYTES * 3 // 2)
        self.assertEqual(harness.REFERENCE_HARD_BYTES, harness.REFERENCE_MEASURED_BYTES * 3)
        largest_skill = largest_agent = largest_reference = 0
        for name in ("example-generic", "example-maven"):
            fixtures.install_harness(name)
            doc = harness.load(name)
            rendered = harness.render_skill(doc)
            largest_skill = max(largest_skill, len(rendered["SKILL.md"].encode("utf-8")))
            largest_reference = max(largest_reference, *(len(text.encode("utf-8"))
                                                         for path, text in rendered.items()
                                                         if path.startswith("references/")))
            largest_agent = max(largest_agent, *(len(text.encode("utf-8"))
                                                 for text in harness.agent_texts(doc).values()))
        # the shipped text may grow a little before the caps are measured again
        self.assertLess(largest_skill, harness.SKILL_SOFT_BYTES)
        self.assertLess(largest_agent, harness.AGENT_SOFT_BYTES)
        self.assertLess(largest_reference, harness.REFERENCE_SOFT_BYTES)

    def test_an_agent_file_over_the_hard_cap_is_refused(self):
        # rule: R-HARNESS-SIZE-CAP
        fixtures.install_harness("example-generic")
        self.overlay("example-generic", "ppl-verifier", harness.AGENT_HARD_BYTES)
        result = harness.verify("example-generic", "example-generic")
        self.assertEqual(len(result["errors"]), 1, result)
        self.assertIn("ppl-verifier", result["errors"][0])
        self.assertIn("%d bytes" % harness.AGENT_HARD_BYTES, result["errors"][0])

    def test_an_agent_file_over_the_soft_cap_is_warned_about(self):
        fixtures.install_harness("example-generic")
        self.overlay("example-generic", "ppl-verifier", harness.AGENT_SOFT_BYTES)
        result = harness.verify("example-generic", "example-generic")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1, result)
        self.assertIn("ppl-verifier", result["warnings"][0])
        self.assertIn("%d bytes" % harness.AGENT_SOFT_BYTES, result["warnings"][0])

    def pad_skill_skeleton(self, size):
        """Point PPL_ROOT at a copy of the code root whose SKILL.md skeleton is `size` bytes
        longer. No harness fragment renders into SKILL.md since each stage moved to its own
        reference, so the skeleton is what grows it."""
        root = Path(tmphome.mkdtemp(prefix="pplroot-"))
        for part in ("defaults", "skill", "templates"):
            shutil.copytree(fixtures.SOURCE / part, root / part)
        skeleton = root / "skill" / "SKILL.md.skeleton"
        skeleton.write_text(skeleton.read_text(encoding="utf-8") + "word " * (size // 5),
                            encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"PPL_ROOT": str(root)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_skill_over_the_hard_cap_is_refused(self):
        # rule: R-HARNESS-SIZE-CAP
        fixtures.install_harness("example-generic")
        self.pad_skill_skeleton(harness.SKILL_HARD_BYTES)
        result = harness.verify("example-generic", "example-generic")
        self.assertEqual(len(result["errors"]), 1, result)
        self.assertIn("SKILL.md", result["errors"][0])
        self.assertIn("%d bytes" % harness.SKILL_HARD_BYTES, result["errors"][0])
        fixtures.install_routing()
        with self.assertRaises(PplError) as caught:
            harness.apply("example-generic", agents_dir=tmphome.mkdtemp(prefix="pplagents-"),
                          skills_dir=self.skills)
        self.assertEqual(caught.exception.message, result["errors"][0])
        self.assertEqual(list(self.skills.iterdir()), [])

    def pad_tracker_close(self, name, size):
        """The harness fragment a team grows most, rendered into
        references/stage-8-after-merge.md and nowhere else."""
        (paths.harness_dir(name) / "skill" / "tracker-close.md").write_text(
            "word " * (size // 5), encoding="utf-8")

    def test_a_harness_fragment_that_grows_a_reference_over_the_hard_cap_is_refused(self):
        # rule: R-HARNESS-SIZE-CAP
        fixtures.install_harness("example-generic")
        self.pad_tracker_close("example-generic", harness.REFERENCE_HARD_BYTES)
        result = harness.verify("example-generic", "example-generic")
        self.assertEqual(len(result["errors"]), 1, result)
        self.assertIn("references/stage-8-after-merge.md", result["errors"][0])
        self.assertIn("%d bytes" % harness.REFERENCE_HARD_BYTES, result["errors"][0])
        fixtures.install_routing()
        with self.assertRaises(PplError) as caught:
            harness.apply("example-generic", agents_dir=tmphome.mkdtemp(prefix="pplagents-"),
                          skills_dir=self.skills)
        self.assertEqual(caught.exception.message, result["errors"][0])
        self.assertEqual(list(self.skills.iterdir()), [])

    def test_a_reference_over_the_soft_cap_is_warned_about(self):
        fixtures.install_harness("example-generic")
        self.pad_tracker_close("example-generic", harness.REFERENCE_SOFT_BYTES)
        result = harness.verify("example-generic", "example-generic")
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1, result)
        self.assertIn("references/stage-8-after-merge.md", result["warnings"][0])
        self.assertIn("%d bytes" % harness.REFERENCE_SOFT_BYTES, result["warnings"][0])

    def test_apply_refuses_an_agent_file_over_the_hard_cap_and_writes_nothing(self):
        # rule: R-HARNESS-SIZE-CAP
        fixtures.install_harness("example-generic")
        fixtures.install_routing()
        self.overlay("example-generic", "ppl-code-worker", harness.AGENT_HARD_BYTES)
        agents = Path(tmphome.mkdtemp(prefix="pplagents-"))
        with self.assertRaises(PplError) as caught:
            harness.apply("example-generic", agents_dir=agents, skills_dir=self.skills)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("ppl-code-worker", caught.exception.message)
        self.assertEqual(list(agents.iterdir()), [])
        self.assertEqual(list(self.skills.iterdir()), [])
        self.assertIsNone(config.active_harness())

    def test_a_knowledge_skill_found_nowhere_is_warned_about(self):
        name = self.mutate("example-generic", "skills = []", 'skills = ["team-kb"]')
        result = harness.verify(name, name)
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(result["warnings"]), 1, result)
        self.assertIn("team-kb", result["warnings"][0])
        self.assertIn(str(self.skills), result["warnings"][0])

    def test_a_knowledge_skill_in_either_skills_directory_is_not_warned_about(self):
        name = self.mutate("example-generic", "skills = []", 'skills = ["team-kb", "lang-kb"]')
        (self.skills / "team-kb").mkdir()
        (self.skills / "team-kb" / "SKILL.md").write_text("x\n", encoding="utf-8")
        (self.claude_skills / "lang-kb").mkdir(parents=True)
        (self.claude_skills / "lang-kb" / "SKILL.md").write_text("x\n", encoding="utf-8")
        self.assertEqual(harness.verify(name, name), {"errors": [], "warnings": []})

    def test_a_focused_tests_command_no_shape_lets_the_test_worker_run_is_warned_about(self):
        name = self.mutate("example-generic", 'command = "make test TEST=<test_class>"',
                           'command = "pytest -q <test_class>"')
        self.assertEqual(harness.verify(name, name)["warnings"], [
            "the test writer cannot run the focused-tests command: no command shape starts "
            "with pytest"])

    def test_a_relative_repo_path_is_refused(self):
        name = self.mutate("example-generic", 'path = "/home/dev/src/project"',
                           'path = "src/project"')
        result = harness.verify(name, name)
        self.assertIn("absolute", result["errors"][0])

    def test_a_structural_error_comes_back_as_the_only_error(self):
        name = self.mutate("example-generic", 'kind = "github"', 'kind = "bitbucket"')
        result = harness.verify(name, name)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("forge.kind", result["errors"][0])


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

    def test_a_worktrees_value_that_is_not_a_path_string_is_refused(self):
        for value in ("1", '["/home/dev/src/project/.worktrees"]', "{ path = \"x\" }"):
            with self.subTest(value=value):
                name = self.mutate("example-maven", 'worktrees = "/home/dev/src/project/.worktrees"',
                                   "worktrees = %s" % value)
                self.refused(name, "repo.worktrees")

    def test_an_empty_worktrees_still_means_the_default_directory(self):
        name = self.mutate("example-maven", 'worktrees = "/home/dev/src/project/.worktrees"',
                           'worktrees = ""')
        self.assertEqual(harness.load(name)["repo"]["worktrees"], "")

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

    def test_a_github_issues_harness_cannot_grant_a_github_mcp_issue_write(self):
        knowledge = {"skills": [], "extra_tools": ["mcp__github__get_issue",
                                                   "mcp__github__add_issue_comment"]}
        fixtures.install_variant("example-generic", "gh-mcp", knowledge=knowledge,
                                 **fixtures.GITHUB_ISSUES)
        with self.assertRaises(PplError) as caught:
            harness.load("gh-mcp")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("knowledge.extra_tools[1] 'mcp__github__add_issue_comment' is a tracker "
                      "write tool", caught.exception.message)

    def test_a_jira_harness_cannot_grant_a_github_mcp_issue_write(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        # every adapter's write tools under its defaults, whatever tracker the harness names
        for bad in ("mcp__github__add_issue_comment", "mcp__corp_github__create_issue"):
            name = self.mutate("example-maven", "extra_tools = []",
                               'extra_tools = ["mcp__corp__code_search", "%s"]' % bad)
            with self.assertRaises(PplError) as caught:
                harness.load(name)
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("knowledge.extra_tools[1] %r is a tracker write tool" % bad,
                          caught.exception.message)

    def test_a_github_issues_harness_cannot_grant_a_jira_write_tool(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        for bad in ("mcp__atlassian__editJiraIssue",
                    "mcp__claude_ai_Atlassian_Rovo_2__transitionJiraIssue"):
            knowledge = {"skills": [], "extra_tools": ["mcp__corp__code_search", bad]}
            fixtures.install_variant("example-generic", "gh-jira-tool", knowledge=knowledge,
                                     **fixtures.GITHUB_ISSUES)
            with self.assertRaises(PplError) as caught:
                harness.load("gh-jira-tool")
            self.assertEqual(caught.exception.code, "HARNESS_INVALID", bad)
            self.assertIn("knowledge.extra_tools[1] %r is a tracker write tool" % bad,
                          caught.exception.message)

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

    def test_derived_name_is_the_last_part_of_a_directory_or_a_url_without_dot_git(self):
        directory = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "team-harness"
        for source, expected in ((str(directory), "team-harness"),
                                 (str(directory) + "/", "team-harness"),
                                 ("https://example.invalid/team/harness.git", "harness"),
                                 ("https://example.invalid/team/harness/", "harness"),
                                 ("git@example.invalid:team/harness.git", "harness"),
                                 ("https://example.invalid/team/harness.git.git", "harness.git")):
            self.assertEqual(harness.derived_name(source), expected, source)

    def test_add_refuses_a_reserved_name_unless_it_is_setups_own_staging(self):
        # rule: R-STUDIO-SAME-VALIDATION
        copy = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "demo.replacing"
        shutil.copytree(self.example, copy)
        for name in (None, "Demo.Replaced", "x.GIT"):
            with self.subTest(name=name):
                with self.assertRaises(PplError) as caught:
                    harness.add(str(copy), name=name)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(harness.list_names(), [])
        self.assertEqual(harness.add(str(copy), name="demo.replacing", staging=True),
                         paths.harness_dir("demo.replacing"))

    def test_check_name_takes_a_plain_name_and_refuses_the_reserved_endings_in_any_case(self):
        # rule: R-STUDIO-SAME-VALIDATION
        self.assertIsNone(harness.check_name("team-harness.v2"))
        for name in ("x.replacing", "X.Replacing", "x.REPLACED", "x.git", "demo\n", "", "-x",
                     "../x", "a/b", None, 3):
            with self.subTest(name=name):
                with self.assertRaises(PplError) as caught:
                    harness.check_name(name)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_add_refuses_a_name_with_a_trailing_newline(self):
        with self.assertRaises(PplError) as caught:
            harness.add(str(self.example), name="demo\n")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertFalse(paths.harnesses_dir().exists() and any(paths.harnesses_dir().iterdir()))

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

    def test_validate_json_prints_errors_and_warnings(self):
        self.mutate("example-generic", 'one_goal_of = ["build", "lint", "test", "integration"]',
                    "one_goal_of = []")
        code, out, err = call("harness", "validate", "example-generic", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), harness.verify("example-generic", "example-generic"))
        self.assertEqual(json.loads(out)["errors"], [])
        self.assertIn("no required_flags and no one_goal_of", json.loads(out)["warnings"][0])

    def test_validate_json_of_an_invalid_harness_prints_the_error_and_exits_2(self):
        self.mutate("example-maven", 'kind = "gitlab"', 'kind = "bitbucket"')
        code, out, err = call("harness", "validate", "example-maven", "--json")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_INVALID")
        result = json.loads(out)
        self.assertEqual(result["warnings"], [])
        self.assertIn("forge.kind", result["errors"][0])

    def test_validate_runs_the_checks_the_studio_runs(self):
        self.mutate("example-generic", 'path = "/home/dev/src/project"', 'path = "src/project"')
        code, _, err = call("harness", "validate", "example-generic")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_INVALID")
        self.assertIn("absolute", json.loads(err)["message"])

    def test_validate_refuses_an_agent_file_over_the_hard_cap(self):
        # rule: R-HARNESS-SIZE-CAP
        target = fixtures.install_harness("example-generic")
        (target / "templates" / "ppl-briefer.part").write_text(
            "x" * harness.AGENT_HARD_BYTES, encoding="utf-8")
        code, _, err = call("harness", "validate", "example-generic")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_INVALID")
        self.assertIn("ppl-briefer", json.loads(err)["message"])

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

    def test_add_refuses_a_name_setup_keeps_for_its_own_directories(self):
        # rule: R-STUDIO-SAME-VALIDATION
        source = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "x.replacing"
        shutil.copytree(fixtures.SOURCE / "defaults" / "harnesses" / "example-generic", source)
        for argv in (("harness", "add", "--source", str(source)),
                     ("harness", "add", "X.Replaced", "--source", str(source))):
            with self.subTest(argv=argv):
                code, _, err = call(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertEqual(harness.list_names(), [])

    def test_apply_refuses_a_worktrees_that_is_not_a_string_without_a_traceback(self):
        self.mutate("example-maven", 'worktrees = "/home/dev/src/project/.worktrees"', "worktrees = 1")
        fixtures.install_routing()
        agents, skills = self.apply_dirs()
        code, out, err = call("harness", "apply", "example-maven")
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_INVALID")
        self.assertIn("repo.worktrees", json.loads(err)["message"])
        self.assertEqual(list(agents.iterdir()), [])

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



class TestTestGlobs(HarnessTestCase):
    def install_sources(self, sources_toml):
        path = fixtures.install_harness("example-generic") / "harness.toml"
        text = path.read_text(encoding="utf-8")
        old = 'sources = ["tests/**", "test/**"]'
        self.assertIn(old, text)
        path.write_text(text.replace(old, sources_toml, 1) if sources_toml else
                        text.replace("[tests]\n" + old + "\n", "[tests]\n", 1), encoding="utf-8")

    def install_glob(self, glob):
        self.install_sources("sources = [%s]" % json.dumps(glob))

    def test_a_brace_glob_is_refused_asking_for_one_glob_per_alternative(self):
        # rule: R-HARNESS-TEST-GLOBS-SANE
        self.install_glob("**/*.{js,ts}")
        with self.assertRaises(PplError) as caught:
            harness.load("example-generic")
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("tests.sources[0] '**/*.{js,ts}'", caught.exception.message)
        self.assertIn("one glob per alternative", caught.exception.message)

    def test_glob_syntax_glob_match_reads_literally_is_refused(self):
        # rule: R-HARNESS-TEST-GLOBS-SANE
        for glob in ("**/test_[ab].py", "tests/\\*.py", "!tests/**"):
            with self.subTest(glob=glob):
                self.install_glob(glob)
                with self.assertRaises(PplError) as caught:
                    harness.load("example-generic")
                self.assertEqual(caught.exception.code, "HARNESS_INVALID")
                self.assertIn("tests.sources[0]", caught.exception.message)

    def test_an_absolute_dot_or_dotdot_glob_is_refused(self):
        # rule: R-HARNESS-TEST-GLOBS-SANE
        for glob in ("/tests/**", "./tests/**", "tests/", "tests//x", "../tests/**"):
            with self.subTest(glob=glob):
                self.install_glob(glob)
                with self.assertRaises(PplError) as caught:
                    harness.load("example-generic")
                self.assertEqual(caught.exception.code, "HARNESS_INVALID")
                self.assertIn("tests.sources[0]", caught.exception.message)

    def test_a_glob_that_matches_product_paths_is_refused(self):
        # rule: R-HARNESS-TEST-GLOBS-SANE
        for glob in ("**", "**/*", "*", "*/**", "**/*.py", "src/**"):
            with self.subTest(glob=glob):
                self.install_glob(glob)
                with self.assertRaises(PplError) as caught:
                    harness.load("example-generic")
                self.assertEqual(caught.exception.code, "HARNESS_INVALID")
                self.assertTrue(any("'%s'" % probe in caught.exception.message
                                    for probe in boundary.PRODUCT_PROBES), caught.exception.message)

    def test_an_empty_or_non_string_sources_list_is_refused(self):
        # rule: R-HARNESS-TEST-GLOBS-SANE
        for text in ("sources = []", 'sources = "tests/**"', 'sources = [""]', "sources = [7]", ""):
            with self.subTest(text=text):
                self.install_sources(text)
                with self.assertRaises(PplError) as caught:
                    harness.load("example-generic")
                self.assertEqual(caught.exception.code, "HARNESS_INVALID")
                self.assertIn("tests.sources", caught.exception.message)

    def test_sane_globs_are_accepted(self):
        name = self.mutate("example-generic", '"test/**"]', '"**/*_test.go"]')
        self.assertEqual(harness.validate(harness.load(name), name), [])


class TestOsSandboxTable(HarnessTestCase):
    def install(self, text, base="example-generic"):
        path = fixtures.install_harness(base) / "harness.toml"
        path.write_text(path.read_text(encoding="utf-8") + "\n[os_sandbox]\n" + text,
                        encoding="utf-8")
        return base

    def refused(self, text, base="example-generic"):
        name = self.install(text, base)
        with self.assertRaises(PplError) as caught:
            harness.load(name)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        return caught.exception.message

    def test_os_sandbox_is_a_known_table(self):
        name = self.install('enabled = true\nallowed_domains = ["pypi.org", "*.example.com"]\n'
                            'allow_write = ["/var/cache/x", "~/.npm"]\n')
        self.assertEqual(harness.validate(harness.load(name), name), [])

    def test_os_sandbox_unknown_key_is_refused(self):
        message = self.refused("allowed_domain = []\n")
        self.assertIn("os_sandbox.allowed_domain", message)
        self.assertIn("os_sandbox.allowed_domains", message)

    def test_allowed_domains_refuse_a_scheme_or_path(self):
        for host in ("https://pypi.org", "pypi.org/simple", "pypi.org:443", "", "a b"):
            with self.subTest(host=host):
                message = self.refused('allowed_domains = ["%s"]\n' % host)
                self.assertIn("os_sandbox.allowed_domains[0]", message)
        self.assertIn("os_sandbox.allowed_domains must", self.refused('allowed_domains = "x"\n'))

    def test_allow_write_refuses_a_relative_path(self):
        message = self.refused('allow_write = ["cache/x"]\n')
        self.assertIn("os_sandbox.allow_write[0]", message)

    def test_enabled_must_be_a_bool(self):
        self.assertIn("os_sandbox.enabled", self.refused('enabled = "yes"\n'))

    def test_enabled_false_on_a_github_harness_is_refused(self):
        message = self.refused("enabled = false\n")
        self.assertIn("os_sandbox.enabled", message)
        self.assertIn("--no-os-sandbox", message)

    def test_enabled_false_on_a_gitlab_jira_harness_is_accepted(self):
        path = fixtures.install_harness("example-generic") / "harness.toml"
        text = path.read_text(encoding="utf-8").replace('kind = "github"', 'kind = "gitlab"\n'
                                                       'mcp_server = "GitLab-MCP"')
        path.write_text(text + "\n[os_sandbox]\nenabled = false\n", encoding="utf-8")
        self.assertEqual(harness.validate(harness.load("example-generic"), "x"), [])

    def test_the_snapshot_records_whether_the_profile_is_needed(self):
        fixtures.install_harness("example-generic")
        self.assertEqual(harness.snapshot(harness.load("example-generic"))["os_sandbox"],
                         {"needed": True})


PINS = {"gh": {"path": "/opt/x/gh", "realpath": "/opt/x/gh"},
        "git": {"path": "/usr/bin/git", "realpath": "/usr/bin/git"},
        "ppl": {"path": "/opt/y/bin/ppl", "realpath": "/opt/y/bin/ppl"}}


class TestApplyOsSandbox(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.workspace = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplws-")))
        self.agents = tmphome.mkdtemp(prefix="pplagents-")
        self.skills = tmphome.mkdtemp(prefix="pplskills-")
        fixtures.install_routing()
        config.update("claude", {"workspace": str(self.workspace)})
        patcher = mock.patch.object(os_sandbox, "pins", lambda which=None: PINS)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings_path = self.workspace / ".claude" / "settings.local.json"
        self.facts_path = paths.home() / os_sandbox.FACTS_FILE

    def apply(self, name="example-generic"):
        return harness.apply(name, agents_dir=self.agents, skills_dir=self.skills)

    def settings(self):
        return json.loads(self.settings_path.read_text(encoding="utf-8"))

    def test_apply_writes_the_profile_and_the_facts_for_a_github_harness(self):
        fixtures.install_harness("example-generic")
        self.settings_path.parent.mkdir(parents=True)
        hooks = {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}
        self.settings_path.write_text(json.dumps({"hooks": hooks}), encoding="utf-8")
        self.apply()
        doc = self.settings()
        self.assertEqual(doc["hooks"], hooks)
        self.assertEqual(doc["sandbox"]["network"]["deniedDomains"], ["github.com", "*.github.com"])
        self.assertNotIn("ppl", doc["sandbox"])
        self.assertIn("Bash(/opt/x/gh auth status)", doc["permissions"]["allow"])
        facts = json.loads(self.facts_path.read_text(encoding="utf-8"))
        expected = os_sandbox.facts(harness.load("example-generic"), self.workspace, PINS)
        self.assertEqual({k: v for k, v in facts.items() if k in expected}, expected)
        self.assertEqual(facts["written_sandbox"], doc["sandbox"])
        self.assertEqual(facts["written_allow"], doc["permissions"]["allow"])

    def test_apply_removes_both_when_the_harness_no_longer_needs_them(self):
        fixtures.install_harness("example-generic")
        self.apply()
        fixtures.install_harness("example-maven")
        self.apply("example-maven")
        self.assertFalse(self.facts_path.exists())
        doc = self.settings()
        self.assertNotIn("sandbox", doc)
        self.assertEqual(doc.get("permissions", {}).get("allow", []), [])

    def test_reapply_replaces_ppl_entries_and_keeps_others(self):
        fixtures.install_harness("example-generic")
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text(json.dumps(
            {"model": "m", "permissions": {"allow": ["Bash(ls *)"]}}), encoding="utf-8")
        self.apply()
        self.apply()
        doc = self.settings()
        self.assertEqual(doc["model"], "m")
        allow = doc["permissions"]["allow"]
        self.assertEqual(allow.count("Bash(ls *)"), 1)
        self.assertEqual(allow.count("Bash(/opt/x/gh auth status)"), 1)
        self.assertEqual(len(allow), 10)

    def test_a_foreign_sandbox_key_refuses_and_leaves_the_file(self):
        fixtures.install_harness("example-generic")
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text('{"sandbox": {"enabled": false}}', encoding="utf-8")
        before = self.settings_path.read_bytes()
        with self.assertRaises(PplError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertEqual(self.settings_path.read_bytes(), before)
        self.assertFalse(self.facts_path.exists())
        self.assertIsNone(config.active_harness())

    def test_invalid_json_refuses_and_leaves_the_file(self):
        fixtures.install_harness("example-generic")
        self.settings_path.parent.mkdir(parents=True)
        self.settings_path.write_text("{oops", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertEqual(self.settings_path.read_text(encoding="utf-8"), "{oops")

    def test_global_hook_mode_still_writes_the_workspace_profile(self):
        config.update("claude", {"hooks": "global"})
        fixtures.install_harness("example-generic")
        self.apply()
        self.assertIn("sandbox", self.settings())

    def test_apply_without_a_workspace_refuses_for_a_github_harness(self):
        config.update("claude", {"workspace": ""})
        fixtures.install_harness("example-generic")
        with self.assertRaises(PplError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("workspace", caught.exception.message)
        self.assertIsNone(config.active_harness())

    def test_apply_without_a_workspace_is_fine_for_a_gitlab_harness(self):
        config.update("claude", {"workspace": ""})
        fixtures.install_harness("example-maven")
        self.apply("example-maven")
        self.assertEqual(config.active_harness(), "example-maven")

    def test_apply_creates_the_scratch_root(self):
        fixtures.install_harness("example-generic")
        self.apply()
        self.assertTrue(os_sandbox.scratch_root().is_dir())


class TestSnapshotAndApplied(HarnessTestCase):
    def activate(self, name):
        paths.config_file().write_text('[harness]\nactive = "%s"\n' % name, encoding="utf-8")

    def test_snapshot_carries_exactly_what_the_guard_enforces(self):
        fixtures.install_harness("example-maven")
        doc = harness.load("example-maven")
        snap = harness.snapshot(doc)
        self.assertEqual(set(snap), {"format", "name", "task", "tracker", "forge", "os_sandbox", "shapes",
                                    "tests"})
        self.assertEqual(snap["format"], harness.FORMAT)
        self.assertEqual(snap["name"], "example-maven")
        self.assertEqual(snap["task"], {"key_pattern": doc["task"]["key_pattern"]})
        self.assertEqual(snap["shapes"], harness.shape_rows(doc))
        self.assertEqual(json.loads(json.dumps(snap)), snap)

    def test_a_harness_without_a_tests_table_snapshots_the_java_defaults(self):
        fixtures.install_harness("example-maven")
        snap = harness.snapshot(harness.load("example-maven"))
        self.assertEqual(snap["tests"], {"sources": list(boundary.TEST_PATTERNS)})

    def test_the_snapshot_carries_the_harness_test_globs(self):
        fixtures.install_harness("example-generic")
        snap = harness.snapshot(harness.load("example-generic"))
        self.assertEqual(snap["tests"], {"sources": ["tests/**", "test/**"]})

    def test_a_github_issues_harness_validates_and_snapshots_its_table(self):
        fixtures.install_variant("example-generic", "gh-issues", **fixtures.GITHUB_ISSUES)
        doc = harness.load("gh-issues")
        # the variant keeps the example's harness.name, which only draws a warning
        warnings = harness.validate(doc, "gh-issues")
        self.assertEqual([w for w in warnings if not w.startswith("harness.name ")], [])
        self.assertEqual(doc["tracker"], fixtures.GITHUB_ISSUES["tracker"])
        snap = harness.snapshot(doc)
        self.assertEqual(snap["tracker"], fixtures.GITHUB_ISSUES["tracker"])
        self.assertEqual(snap["task"], {"key_pattern": "^GH-[0-9]+$"})
        self.assertEqual(snap["forge"], {"kind": "github"})
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
            text = self.render(name)["references/stage-8-after-merge.md"]
            self.assertIn("write to the tracker %s:" % harness.DEFAULT_TRACKER_CLOSE, text, name)

    def test_the_jira_steps_name_the_harness_read_tools(self):
        name = self.mutate("example-maven", 'read_tools = ["getJiraIssue", "getConfluenceContent", '
                           '"getConfluencePage", "searchJiraIssuesUsingJql", "searchConfluence", '
                           '"executeRead"]', 'read_tools = ["getJiraIssue", "corpReadIssue"]')
        rendered = harness.render_skill(harness.load(name))
        stage0 = " ".join(rendered["references/stage-0-admission.md"].split())
        self.assertIn("the Atlassian MCP read tools the harness lists in `tracker.read_tools` "
                      "(`getJiraIssue`, `corpReadIssue`)", stage0)
        for path, text in rendered.items():
            for fixed in ("getConfluenceContent", "searchJiraIssuesUsingJql",
                          "addOrEditJiraIssueComment"):
                self.assertNotIn(fixed, text, path)

    def test_a_tracker_close_fragment_replaces_the_default(self):
        directory = fixtures.install_harness("example-generic")
        (directory / "skill" / "tracker-close.md").write_text(
            "a comment saying what was done,\nplus the custom fields the team tracks\n",
            encoding="utf-8")
        rendered = harness.render_skill(harness.load("example-generic"))
        text = rendered["references/stage-8-after-merge.md"]
        self.assertIn("write to the tracker a comment saying what was done, plus the custom "
                      "fields the team tracks:", text)
        for relative, text in rendered.items():
            self.assertNotIn(harness.DEFAULT_TRACKER_CLOSE, text, relative)

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
        for relative in ("references/gates.md", "references/baseline.md",
                         "references/stage-1-baseline.md", "references/stage-8-after-merge.md"):
            self.assertIn("branch trunk here", rendered[relative], relative)

    def test_rendered_skill_carries_the_notice_and_the_forge_text(self):
        forge_files = ("SKILL.md", "references/stage-6-mr.md", "references/stage-7-review.md",
                       "references/stage-8-after-merge.md")
        maven = self.render("example-maven")
        self.assertTrue(maven["SKILL.md"].startswith("---\n"))
        self.assertIn(harness.SKILL_NOTICE, maven["SKILL.md"])
        self.assertIn("GitLab MCP", maven["references/stage-6-mr.md"])
        for relative in forge_files:
            self.assertNotIn("pull request", maven[relative].lower(), relative)
        generic = self.render("example-generic")
        self.assertIn("gh pr create", generic["references/stage-6-mr.md"])
        for relative in forge_files:
            self.assertNotIn("merge request", generic[relative].lower(), relative)

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
        maven = self.render("example-maven")
        self.assertIn("--harness example-maven", maven["references/stage-0-admission.md"])
        baseline = maven["references/stage-1-baseline.md"]
        self.assertIn("cd /home/dev/src/project\n", baseline)
        self.assertIn("git fetch origin master\n", baseline)
        self.assertIn("git worktree add /home/dev/src/project/.worktrees/<repo-name>-<TASK-ID> "
                      "-b <TASK-ID>-<short-slug> origin/master\n", baseline)
        self.assertIn("the names of tests that already fail on the base revision", baseline)
        self.assertIn("(no knowledge skills are configured for this harness)",
                      maven["references/stage-2-briefing.md"])
        generic = self.render("example-generic")
        self.assertIn("git fetch origin main\n", generic["references/stage-1-baseline.md"])
        self.assertIn("diff origin/main...HEAD", generic["references/gates.md"])
        self.assertIn("make test TEST=<test_class> > /tmp/ppl-<TASK-ID>/focused-tests.log 2>&1",
                      generic["references/gates.md"])

    def test_the_harness_name_in_the_skill_is_the_installed_directory(self):
        # The document inside "mine" still says name = "example-maven", and
        # ppl init --harness resolves a directory name.
        rendered = self.render("example-maven", installed="mine")
        self.assertIn("--harness mine", rendered["references/stage-0-admission.md"])
        for relative, text in rendered.items():
            self.assertNotIn("--harness example-maven", text, relative)

    def test_the_baseline_fragment_stays_on_its_table_row(self):
        baseline = self.render("example-maven")["references/baseline.md"]
        row = next(line for line in baseline.splitlines() if line.startswith("| anything else"))
        self.assertIn("the names of tests that already fail on the base revision", row)
        self.assertIn("| No - ", row)

    def test_knowledge_skills_are_named(self):
        self.mutate("example-maven", "skills = []", 'skills = ["kb-core", "kb-build"]')
        text = harness.render_skill(harness.load("example-maven"))["references/stage-2-briefing.md"]
        self.assertIn("(`kb-core`, `kb-build`)", text)

    def test_every_include_value_is_used_by_a_skeleton_or_a_forge_fragment(self):
        fixtures.install_harness("example-maven")
        offered = set(harness._values(harness.load("example-maven")))
        sources = list((fixtures.SOURCE / "skill").rglob("*.skeleton"))
        sources += list((fixtures.SOURCE.parent / "forges" / "fragments").rglob("*.md"))
        used = set()
        for path in sources:
            used.update(harness._INCLUDE_RE.findall(path.read_text(encoding="utf-8")))
        self.assertEqual(offered - used, set())

    def test_a_forge_fragment_may_include_a_sequence_and_nothing_else(self):
        # rule: R-SKILL-GENERATED
        fixtures.install_harness("example-maven")
        doc = harness.load("example-maven")
        gitlab = forges.get("gitlab")
        with mock.patch.object(gitlab, "fragment", lambda name: "<!-- include: vocab/noun -->"):
            with self.assertRaises(PplError) as caught:
                harness.render_skill(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("forge/stage6: unknown include marker vocab/noun", caught.exception.message)

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

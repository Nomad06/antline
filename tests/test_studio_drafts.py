import copy
import io
import json
import os
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import cli, config, harness, trackers
from ppl.errors import PplError
from ppl.studio import drafts, project, stacks


class DraftTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        self.repo = panel_client.make_repo({"Makefile": "test:\n\ttrue\n"})

    def preset(self, stack=None):
        info = project.inspect(str(self.repo))
        draft = stacks.draft(stack or info["stack"]["stack"], info)
        if not draft["doc"]["tests"]["sources"]:
            draft["doc"]["tests"]["sources"] = ["tests/**"]
        return draft


class TestCheck(DraftTestCase):
    def test_a_preset_draft_checks_clean(self):
        draft = self.preset()
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertEqual(result, {"ok": True, "errors": [], "warnings": [], "flags": []})

    def test_a_make_preset_asks_for_test_sources_before_install(self):
        info = project.inspect(str(self.repo))
        draft = stacks.draft("make", info)
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("tests.sources" in error for error in result["errors"]), result)

    def test_every_stack_preset_comes_back_unflagged(self):
        for stack in stacks.STACKS:
            draft = self.preset(stack)
            result = drafts.check("demo", draft["doc"], draft["fragments"])
            self.assertTrue(result["ok"], (stack, result))
            self.assertEqual(result["flags"], [], stack)

    def test_every_stack_preset_checks_without_a_warning(self):
        for stack in stacks.STACKS:
            draft = self.preset(stack)
            result = drafts.check("demo", draft["doc"], draft["fragments"])
            self.assertEqual(result["warnings"], [], stack)

    def set_focused_command(self, draft, command):
        for gate in draft["doc"]["gate"]:
            if gate["name"] == harness.FOCUSED_TESTS_GATE:
                gate["command"] = command

    def test_a_focused_tests_command_no_shape_starts_with_is_warned_about(self):
        draft = self.preset("node")
        self.set_focused_command(draft, "npx jest <test_class>")
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["warnings"], [
            "the test writer cannot run the focused-tests command: no command shape starts with npx"])

    def test_a_focused_tests_command_a_later_shape_starts_with_is_not_warned_about(self):
        draft = self.preset("node")
        self.set_focused_command(draft, "npx jest <test_class>")
        draft["doc"]["guard"]["shape"].append(dict(draft["doc"]["guard"]["shape"][0], argv0="npx"))
        self.assertEqual(drafts.check("demo", draft["doc"], draft["fragments"])["warnings"], [])

    def focused_warnings(self, command, env_prefix=None):
        draft = self.preset("node")
        self.set_focused_command(draft, command)
        if env_prefix is not None:
            draft["doc"]["guard"]["shape"][0]["env_prefix"] = env_prefix
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertTrue(result["ok"], result)
        return result["warnings"]

    def test_an_env_prefix_on_a_shape_that_takes_none_is_warned_about(self):
        self.assertEqual(self.focused_warnings("CI=1 npm test -- <test_class>"), [
            "the test writer cannot run the focused-tests command: no command shape starts "
            "with npm that takes an environment prefix"])

    def test_an_env_prefix_on_a_shape_that_takes_one_is_not_warned_about(self):
        self.assertEqual(self.focused_warnings("CI=1 npm test -- <test_class>", env_prefix=True), [])
        self.assertEqual(self.focused_warnings("npm test -- <test_class>", env_prefix=True), [])

    def test_an_env_prefix_ahead_of_a_program_no_shape_names_gets_the_plain_warning(self):
        self.assertEqual(self.focused_warnings("CI=1 npx jest <test_class>", env_prefix=True), [
            "the test writer cannot run the focused-tests command: no command shape starts "
            "with npx"])

    def test_a_quoted_env_value_with_a_space_does_not_name_a_half_word_as_the_program(self):
        command = 'FOO="a b" npm test -- <test_class>'
        self.assertEqual(self.focused_warnings(command, env_prefix=True), [])
        self.assertEqual(self.focused_warnings(command), [
            "the test writer cannot run the focused-tests command: no command shape starts "
            "with npm that takes an environment prefix"])

    def test_a_focused_tests_command_shlex_cannot_split_warns_with_its_first_word(self):
        self.assertEqual(self.focused_warnings('npm test "<test_class>'), [
            "the test writer cannot run the focused-tests command: no command shape starts "
            "with npm"])

    def test_a_draft_granting_a_tracker_write_tool_is_refused(self):
        # rule: R-STUDIO-SAME-VALIDATION
        draft = self.preset()
        draft["doc"]["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"])
        self.assertIn("tracker write tool", result["errors"][0])

    def test_a_draft_granting_a_joined_tool_list_is_refused(self):
        # rule: R-STUDIO-SAME-VALIDATION
        draft = self.preset()
        draft["doc"]["knowledge"]["extra_tools"] = ["mcp__docs__search, Write"]
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"])
        self.assertIn("not an MCP tool name", result["errors"][0])

    def test_a_shape_whose_free_args_take_anything_is_flagged(self):
        for free_args in (".*", ".+", r"\S+", "[^ ]+", ".{1,}"):
            with self.subTest(free_args=free_args):
                draft = self.preset()
                draft["doc"]["guard"]["shape"][0]["free_args"] = free_args
                result = drafts.check("demo", draft["doc"], draft["fragments"])
                self.assertTrue(result["ok"])
                self.assertIn("accepts any token", result["flags"][0]["reasons"][0])

    def test_a_narrow_free_args_class_is_not_flagged(self):
        for free_args in ("[a-z]+", r"\w+", "^[A-Z_]+=[A-Za-z0-9_./:,-]*$"):
            with self.subTest(free_args=free_args):
                draft = self.preset()
                draft["doc"]["guard"]["shape"][0]["free_args"] = free_args
                self.assertEqual(drafts.check("demo", draft["doc"], draft["fragments"])["flags"], [])

    def test_a_shell_as_argv0_without_a_required_flag_is_flagged(self):
        draft = self.preset()
        draft["doc"]["guard"]["shape"].append({"argv0": "sh", "env_prefix": False,
                                               "required_flags": [], "one_goal_of": ["-c"],
                                               "free_args": stacks.SAFE_FREE_ARGS})
        flagged = drafts.check("demo", draft["doc"], draft["fragments"])["flags"]
        self.assertEqual([item["argv0"] for item in flagged], ["sh"])

    def test_python_with_a_pinned_module_is_not_flagged(self):
        draft = self.preset("python")
        self.assertEqual(drafts.check("demo", draft["doc"], draft["fragments"])["flags"], [])

    def test_a_bad_name_or_an_unknown_fragment_is_refused(self):
        draft = self.preset()
        for name, fragments in (("../evil", draft["fragments"]),
                                ("demo", dict(draft["fragments"], **{"hooks/x.py": "x"}))):
            with self.assertRaises(PplError) as caught:
                drafts.write(name, draft["doc"], fragments)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_null_inside_the_document_is_dropped_before_toml(self):
        draft = self.preset()
        draft["doc"]["forge"]["mcp_server"] = None
        self.assertTrue(drafts.check("demo", draft["doc"], draft["fragments"])["ok"])

    def test_the_names_setup_keeps_for_its_own_directories_are_refused(self):
        # rule: R-STUDIO-SAME-VALIDATION
        for suffix in (".git", ".replacing", ".replaced"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(PplError) as caught:
                    drafts.check_name("demo" + suffix)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn(suffix, caught.exception.message)

    def test_the_reserved_endings_are_refused_in_any_letter_case(self):
        # rule: R-STUDIO-SAME-VALIDATION
        for name in ("Demo.Replacing", "DEMO.REPLACED", "x.GIT"):
            with self.subTest(name=name):
                with self.assertRaises(PplError) as caught:
                    drafts.check_name(name)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_overlapping_checks_of_one_name_each_answer_for_their_own_document(self):
        valid = self.preset()
        invalid = copy.deepcopy(valid)
        invalid["doc"]["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        threads, rounds = 8, 12
        start = threading.Barrier(threads)
        outcomes = []

        def work(index):
            draft = invalid if index % 2 else valid
            start.wait()
            for _ in range(rounds):
                try:
                    result = drafts.check("demo", draft["doc"], draft["fragments"])
                    outcomes.append((index % 2 == 0, result["ok"]))
                except Exception as exc:
                    outcomes.append((index % 2 == 0, exc))
        workers = [threading.Thread(target=work, args=(index,)) for index in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(len(outcomes), threads * rounds)
        self.assertEqual([outcome for outcome in outcomes if outcome[0] != outcome[1]], [])

    def test_a_name_with_a_trailing_newline_is_refused_and_leaves_no_directory(self):
        draft = self.preset()
        with self.assertRaises(PplError) as caught:
            drafts.check_name("demo\n")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        result = drafts.check("demo\n", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"])
        self.assertFalse(drafts.drafts_dir().exists() and any(drafts.drafts_dir().iterdir()))

    def test_a_relative_repo_path_is_refused(self):
        draft = self.preset()
        draft["doc"]["repo"]["path"] = "relative/repo"
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"])
        self.assertIn("absolute", result["errors"][0])

    def test_a_home_relative_repo_path_is_accepted(self):
        draft = self.preset()
        draft["doc"]["repo"]["path"] = "~/work/demo"
        self.assertTrue(drafts.check("demo", draft["doc"], draft["fragments"])["ok"])

    def test_a_joined_forge_server_name_is_refused_by_role_tool_composition(self):
        # rule: R-STUDIO-SAME-VALIDATION
        draft = self.preset()
        draft["doc"]["forge"] = {"kind": "gitlab", "mcp_server": "GitLab-MCP, Write"}
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"])
        self.assertIn("not a single tool name", result["errors"][0])
        with self.assertRaises(PplError) as caught:
            drafts.install("demo", draft["doc"], draft["fragments"], False, lambda line: None)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertNotIn("demo", harness.list_names())

    def test_write_refuses_bad_input_before_it_touches_the_previous_draft(self):
        draft = self.preset()
        target = drafts.write("demo", draft["doc"], draft["fragments"])

        def snapshot():
            return {path.relative_to(target).as_posix(): path.read_bytes()
                    for path in sorted(target.rglob("*")) if path.is_file()}
        before = snapshot()
        changed = copy.deepcopy(draft["doc"])
        changed["gate"][0]["command"] = "make all"
        broken = copy.deepcopy(draft["doc"])
        broken["task"] = {1: "not a toml key"}
        bad_inputs = (("a non-text fragment", changed, dict(draft["fragments"], **{"skill/gates.md": 5})),
                      ("an unknown fragment", changed, dict(draft["fragments"], **{"hooks/x.py": "x"})),
                      ("a fragment that is not UTF-8", changed,
                       dict(draft["fragments"], **{"skill/gates.md": "\ud800"})),
                      ("a document TOML cannot hold", broken, draft["fragments"]))
        for label, doc, fragments in bad_inputs:
            with self.subTest(label):
                with self.assertRaises(PplError) as caught:
                    drafts.write("demo", doc, fragments)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertEqual(snapshot(), before)
                self.assertEqual([path.name for path in drafts.drafts_dir().iterdir()], ["demo"])


def tree(folder):
    return {str(item.relative_to(folder)): item.read_bytes()
            for item in sorted(Path(folder).rglob("*")) if item.is_file()}


class TestCheckIsPrivate(DraftTestCase):
    def test_a_check_writes_no_directory_per_name_it_is_asked_about(self):
        draft = self.preset()
        for name in ("d", "de", "dem", "demo"):
            with self.subTest(name=name):
                self.assertTrue(drafts.check(name, draft["doc"], draft["fragments"])["ok"])
        refused = drafts.check("demo", None, draft["fragments"])
        self.assertFalse(refused["ok"])
        self.assertEqual(list(drafts.drafts_dir().iterdir()), [])

    def test_a_check_leaves_a_directory_of_the_same_name_alone(self):
        draft = self.preset()
        kept = drafts.write("demo", draft["doc"], draft["fragments"])
        (kept / "skill" / "gates.md").write_text("an earlier edit\n", encoding="utf-8")
        before = tree(drafts.drafts_dir())
        self.assertEqual(drafts.check("demo", draft["doc"], draft["fragments"]),
                         {"ok": True, "errors": [], "warnings": [], "flags": []})
        self.assertEqual(tree(drafts.drafts_dir()), before)
        self.assertEqual(sorted(item.name for item in drafts.drafts_dir().iterdir()), ["demo"])

    def test_a_check_works_before_the_drafts_directory_exists(self):
        draft = self.preset()
        self.assertFalse(drafts.drafts_dir().exists())
        self.assertTrue(drafts.check("demo", draft["doc"], draft["fragments"])["ok"])
        self.assertEqual(list(drafts.drafts_dir().iterdir()), [])

    def test_a_checked_copy_is_written_where_asked_and_verified(self):
        draft = self.preset()
        root = Path(tmphome.mkdtemp(prefix="pplcopy-"))
        target, result = drafts.checked_copy("demo", draft["doc"], draft["fragments"], root)
        self.assertEqual(target, root / "demo")
        self.assertTrue(result["ok"], result)
        self.assertEqual(drafts.verify(target, "demo"), result)
        refused, result = drafts.checked_copy("demo.git", draft["doc"], draft["fragments"], root)
        self.assertIsNone(refused)
        self.assertFalse(result["ok"])


def cli_validate(target):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        cli.main(["harness", "validate", str(target), "--json"])
    return json.loads(out.getvalue())


class TestSameAnswerAsTheCli(DraftTestCase):
    def drafts(self):
        clean = self.preset()
        warned = self.preset("node")
        for gate in warned["doc"]["gate"]:
            if gate["name"] == harness.FOCUSED_TESTS_GATE:
                gate["command"] = "npx jest <test_class>"
        unfocused = self.preset()
        unfocused["doc"]["gate"] = [gate for gate in unfocused["doc"]["gate"]
                                    if gate["name"] != harness.FOCUSED_TESTS_GATE]
        misspelt = self.preset()
        misspelt["doc"]["guard"]["shapes"] = misspelt["doc"]["guard"].pop("shape")
        relative = self.preset()
        relative["doc"]["repo"]["path"] = "relative/repo"
        oversized = self.preset()
        oversized["fragments"]["templates/ppl-verifier.part"] = "x" * harness.AGENT_HARD_BYTES
        return {"clean": clean, "warned": warned, "unfocused": unfocused, "misspelt": misspelt,
                "relative": relative, "oversized": oversized, "typo": self.typo_draft()}

    def typo_draft(self):
        """A node preset whose focused-tests command carries <testclass>, its gates.md
        regenerated from the commands the way presets and the extraction write it."""
        draft = self.preset("node")
        for gate in draft["doc"]["gate"]:
            if gate["name"] == harness.FOCUSED_TESTS_GATE:
                gate["command"] = "npm test -- <test_class> <testclass>"
        draft["fragments"].update(stacks.gate_fragments(draft["doc"]["gate"], stack="node"))
        self.assertIn("<testclass>", draft["fragments"]["skill/gates.md"])
        return draft

    def test_a_placeholder_typo_echoed_into_the_generated_gates_md_is_still_refused(self):
        # rule: R-HARNESS-UNKNOWN-KEY
        draft = self.typo_draft()
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertFalse(result["ok"], result)
        self.assertIn("<testclass>", result["errors"][0])

    def test_the_cli_and_the_studio_return_the_same_errors_and_warnings(self):
        # rule: R-STUDIO-SAME-VALIDATION
        for label, draft in self.drafts().items():
            with self.subTest(draft=label):
                root = Path(tmphome.mkdtemp(prefix="pplsame-"))
                target = drafts.write("demo", draft["doc"], draft["fragments"], root)
                studio = drafts.verify(target, "demo")
                cli_result = cli_validate(target)
                self.assertEqual(cli_result, {"errors": studio["errors"],
                                              "warnings": studio["warnings"]})
                self.assertEqual(studio["ok"], not studio["errors"])

    def test_a_draft_the_cli_refuses_is_refused_in_the_studio(self):
        # rule: R-STUDIO-SAME-VALIDATION
        for label in ("unfocused", "misspelt", "relative", "oversized", "typo"):
            draft = self.drafts()[label]
            with self.subTest(draft=label):
                result = drafts.check("demo", draft["doc"], draft["fragments"])
                self.assertFalse(result["ok"], result)
                self.assertEqual(len(result["errors"]), 1, result)
                self.assertEqual(result["flags"], [])


class TestInstall(DraftTestCase):
    def install(self, draft, replace=False):
        return drafts.install("demo", draft["doc"], draft["fragments"], replace, lambda line: None)

    def test_install_runs_the_setup_harness_step(self):
        draft = self.preset()
        result = self.install(draft)
        self.assertEqual(result["done"], ["harness"])
        installed = harness.load("demo")
        self.assertEqual(installed["repo"]["path"], draft["doc"]["repo"]["path"])
        self.assertEqual(config.section("harness").get("pending"), "demo")

    def test_install_hands_setup_an_absolute_draft_path(self):
        # an older Python returns a relative mkdtemp path for a relative data root
        relative = Path(os.path.relpath(tmphome.mkdtemp(prefix="pplrel-"), os.getcwd())) / ".install-x"
        relative.mkdir()
        seen = {}

        def run_step(step, answers, emit):
            seen.update(answers)
            return {"done": [step], "failed": [], "reasons": {}}

        with mock.patch.object(drafts.tempfile, "mkdtemp", return_value=str(relative)), \
                mock.patch.object(drafts.wizard, "run_step", run_step):
            self.install(self.preset())
        self.assertTrue(os.path.isabs(seen["harness_from"]), seen["harness_from"])

    def test_installing_a_changed_draft_again_needs_replace(self):
        draft = self.preset()
        self.install(draft)
        changed = copy.deepcopy(draft)
        changed["doc"]["gate"][0]["command"] = "make all"
        with self.assertRaises(PplError) as caught:
            self.install(changed)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertEqual(self.install(changed, replace=True)["done"], ["harness"])
        self.assertEqual(harness.load("demo")["gate"][0]["command"], "make all")

    def test_installing_the_same_draft_again_keeps_it(self):
        draft = self.preset()
        self.install(draft)
        self.assertEqual(self.install(draft)["done"], ["harness"])

    def draft_for(self, kind, repo=None):
        draft = copy.deepcopy(self.preset())
        offer = copy.deepcopy(draft["trackers"][kind])
        if repo:
            offer["tracker"]["repo"] = repo
            offer["task"] = trackers.get(kind).task_defaults(offer["tracker"])
        draft["doc"]["tracker"], draft["doc"]["task"] = offer["tracker"], offer["task"]
        return draft

    def test_a_github_issues_draft_replacing_a_jira_harness_installs_github_issues(self):
        self.install(self.draft_for("jira"))
        self.install(self.draft_for("github-issues", "o/r"), replace=True)
        installed = harness.load("demo")
        self.assertEqual(installed["tracker"], {"kind": "github-issues", "repo": "o/r",
                                                "task_prefix": "GH"})
        self.assertEqual(installed["task"]["key_pattern"], "^GH-[0-9]+$")

    def test_a_jira_draft_replacing_a_github_issues_harness_installs_jira(self):
        self.install(self.draft_for("github-issues", "o/r"))
        self.install(self.draft_for("jira"), replace=True)
        self.assertEqual(harness.load("demo")["tracker"]["kind"], "jira")

    def test_a_github_issues_draft_replacing_another_repo_installs_the_drafts_repo(self):
        self.install(self.draft_for("github-issues", "o/old"))
        self.install(self.draft_for("github-issues", "o/new"), replace=True)
        self.assertEqual(harness.load("demo")["tracker"]["repo"], "o/new")

    def install_with_a_check_in_between(self, draft, other):
        original = harness._copy

        def interleaved(name, source, target):
            drafts.check("demo", other["doc"], other["fragments"])
            return original(name, source, target)
        with mock.patch.object(harness, "_copy", interleaved):
            return self.install(draft)

    def test_a_concurrent_check_does_not_change_what_install_installs(self):
        draft = self.preset()
        other = copy.deepcopy(draft)
        other["doc"]["gate"][0]["command"] = "make OTHER-TAB"
        self.install_with_a_check_in_between(draft, other)
        self.assertEqual(harness.load("demo")["gate"][0]["command"],
                         draft["doc"]["gate"][0]["command"])

    def test_install_leaves_no_private_copy_behind(self):
        draft = self.preset()
        self.install(draft)
        broken = copy.deepcopy(draft)
        broken["doc"]["forge"] = {"kind": "gitlab", "mcp_server": "GitLab-MCP, Write"}
        with self.assertRaises(PplError):
            self.install(broken, replace=True)
        self.assertEqual(sorted(path.name for path in drafts.drafts_dir().iterdir()), [])

    def test_install_refuses_a_reserved_name_before_it_writes_anything(self):
        # rule: R-STUDIO-SAME-VALIDATION
        draft = self.preset()
        with self.assertRaises(PplError) as caught:
            drafts.install("demo.replacing", draft["doc"], draft["fragments"], False,
                           lambda line: None)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(harness.list_names(), [])

    def test_install_refuses_an_invalid_draft_before_setup_runs(self):
        # rule: R-STUDIO-SAME-VALIDATION
        draft = self.preset()
        draft["doc"]["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        with self.assertRaises(PplError) as caught:
            self.install(draft)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertNotIn("demo", harness.list_names())


class TestDraftRoutes(DraftTestCase):
    def test_the_check_route_returns_the_check(self):
        draft = self.preset()
        status, result, _ = panel_client.call("POST", "/api/harness/draft/check",
                                              body={"name": "demo", "doc": draft["doc"],
                                                    "fragments": draft["fragments"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])

    def test_the_install_route_installs_as_a_setup_job(self):
        draft = self.preset()
        status, payload, _ = panel_client.call("POST", "/api/harness/draft/install",
                                               body={"name": "demo", "doc": draft["doc"],
                                                     "fragments": draft["fragments"]})
        self.assertEqual(status, 200)
        job = self.wait(payload)
        self.assertEqual(job["state"], "done", job)
        self.assertIn("demo", harness.list_names())

    def test_the_install_route_takes_replace_only_when_it_is_true(self):
        draft = self.preset()
        body = {"name": "demo", "doc": draft["doc"], "fragments": draft["fragments"]}
        status, payload, _ = panel_client.call("POST", "/api/harness/draft/install", body=body)
        self.assertEqual(self.wait(payload)["state"], "done")
        changed = copy.deepcopy(draft)
        changed["doc"]["gate"][0]["command"] = "make all"
        body = {"name": "demo", "doc": changed["doc"], "fragments": changed["fragments"]}
        for replace in ("no", "yes"):
            with self.subTest(replace=replace):
                status, payload, _ = panel_client.call("POST", "/api/harness/draft/install",
                                                       body=dict(body, replace=replace))
                job = self.wait(payload)
                self.assertEqual(job["state"], "failed", job)
                self.assertEqual(job["error"]["error_code"], "SETUP_INCOMPLETE")
        self.assertNotEqual(harness.load("demo")["gate"][0]["command"], "make all")
        status, payload, _ = panel_client.call("POST", "/api/harness/draft/install",
                                               body=dict(body, replace=True))
        self.assertEqual(self.wait(payload)["state"], "done")
        self.assertEqual(harness.load("demo")["gate"][0]["command"], "make all")

    def test_the_install_route_refuses_a_reserved_name_at_once(self):
        # rule: R-STUDIO-SAME-VALIDATION
        status, payload, _ = panel_client.call("POST", "/api/harness/draft/install",
                                               body={"name": "demo.replacing", "doc": {}, "fragments": {}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_the_install_route_refuses_a_bad_name_at_once(self):
        status, payload, _ = panel_client.call("POST", "/api/harness/draft/install",
                                               body={"name": "a/b", "doc": {}, "fragments": {}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

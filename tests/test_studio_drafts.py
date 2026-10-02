import copy
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import config, harness
from ppl.errors import PplError
from ppl.studio import drafts, project, stacks


class DraftTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        self.repo = panel_client.make_repo({"Makefile": "test:\n\ttrue\n"})

    def preset(self, stack=None):
        info = project.inspect(str(self.repo))
        return stacks.draft(stack or info["stack"]["stack"], info)


class TestCheck(DraftTestCase):
    def test_a_preset_draft_checks_clean(self):
        draft = self.preset()
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertEqual(result, {"ok": True, "errors": [], "warnings": [], "flags": []})

    def test_every_stack_preset_comes_back_unflagged(self):
        for stack in stacks.STACKS:
            draft = self.preset(stack)
            result = drafts.check("demo", draft["doc"], draft["fragments"])
            self.assertTrue(result["ok"], (stack, result))
            self.assertEqual(result["flags"], [], stack)

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
        for suffix in (".git", ".replacing", ".replaced"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(PplError) as caught:
                    drafts.check_name("demo" + suffix)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn(suffix, caught.exception.message)

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

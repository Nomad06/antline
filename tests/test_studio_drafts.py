import copy
import os
import sys
import unittest
from pathlib import Path

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
        draft = self.preset()
        draft["doc"]["guard"]["shape"][0]["free_args"] = ".*"
        result = drafts.check("demo", draft["doc"], draft["fragments"])
        self.assertTrue(result["ok"])
        self.assertIn("accepts any token", result["flags"][0]["reasons"][0])

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

    def test_the_install_route_refuses_a_bad_name_at_once(self):
        status, payload, _ = panel_client.call("POST", "/api/harness/draft/install",
                                               body={"name": "a/b", "doc": {}, "fragments": {}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

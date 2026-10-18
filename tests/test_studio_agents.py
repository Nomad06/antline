import copy
import importlib
import os
import sys
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import boundary, config, harness, models, paths, routing, rules, tomlout
from ppl.errors import PplError
from ppl.studio import drafts, enforced, files


def blocking_rows():
    with open(paths.root() / "rules.toml", "rb") as handle:
        return [row for row in tomllib.load(handle)["rule"] if row["enforcement"] == "blocking"]


class TestEnforced(unittest.TestCase):
    def ids(self, role):
        return {row["id"] for row in enforced.for_role(role)}

    def test_a_role_gets_its_own_rules_and_not_another_roles(self):
        self.assertIn("R-ROLE-CODE-WORKER-TESTS", self.ids("ppl-code-worker"))
        self.assertNotIn("R-ROLE-CODE-WORKER-TESTS", self.ids("ppl-test-worker"))

    def test_every_role_gets_the_rules_that_hold_everywhere(self):
        everywhere = {row["id"] for row in blocking_rows() if row["scope"] == "global, no run required"}
        self.assertTrue(everywhere)
        for role in routing.REQUIRED_ROLES:
            self.assertTrue(everywhere <= self.ids(role), role)

    def test_writing_and_read_only_rules_follow_the_kind_of_role(self):
        writing = {row["id"] for row in blocking_rows() if row["scope"] == "active run, any writing role"}
        read_only = {row["id"] for row in blocking_rows() if row["scope"] == "active run, read-only roles"}
        self.assertTrue(writing <= self.ids("ppl-test-worker"))
        self.assertFalse(writing & self.ids("ppl-verifier"))
        self.assertTrue(read_only <= self.ids("ppl-verifier"))
        self.assertFalse(read_only & self.ids("ppl-code-worker"))

    def test_every_blocking_scope_in_the_table_is_classified(self):
        for row in blocking_rows():
            known = (row["scope"] in enforced.NOT_A_ROLE
                     or any(enforced.applies(row["scope"], role) for role in routing.REQUIRED_ROLES))
            self.assertTrue(known, "classify the scope %r of %s in ppl/studio/enforced.py"
                            % (row["scope"], row["id"]))

    def test_an_unknown_role_is_refused(self):
        with self.assertRaises(PplError):
            enforced.for_role("ppl-intern")

    def test_the_guard_rule_on_the_ppl_cli_and_the_operator_halt_are_on_every_role_page(self):
        for role in routing.REQUIRED_ROLES:
            self.assertTrue({"R-GUARD-PPL-CLI-DENIED", "R-OPERATOR-HALT-HOOK"} <= self.ids(role), role)

    def test_the_dispatch_leak_rule_is_on_the_test_worker_page_only(self):
        for role in routing.REQUIRED_ROLES:
            on_page = "R-TEST-WORKER-DISPATCH-LEAK" in self.ids(role)
            self.assertEqual(on_page, role == "ppl-test-worker", role)

    def test_the_read_only_tool_rule_is_on_read_only_role_pages_only(self):
        for role, rule in boundary.ROLE_RULES.items():
            if rule["may_write"]:
                self.assertNotIn("R-ROLE-TOOLS-READONLY", self.ids(role), role)
            else:
                self.assertIn("R-ROLE-TOOLS-READONLY", self.ids(role), role)

    def test_the_writing_roles_come_from_the_guards_role_table(self):
        self.addCleanup(importlib.reload, enforced)
        changed = {"ppl-verifier": {"may_write": True, "tests": "deny"}}
        with mock.patch.dict(boundary.ROLE_RULES, changed):
            importlib.reload(enforced)
            self.assertIn("R-ROLE-FORBIDDEN-PATHS", self.ids("ppl-verifier"))
        importlib.reload(enforced)
        self.assertNotIn("R-ROLE-FORBIDDEN-PATHS", self.ids("ppl-verifier"))

    def test_the_rule_table_is_read_through_the_matrix_loader(self):
        with mock.patch.object(rules, "default_path", return_value=paths.home() / "no-rules.toml"):
            with self.assertRaises(PplError) as caught:
                enforced.for_role("ppl-verifier")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_rule_carries_what_the_page_shows(self):
        rows = enforced.for_role("ppl-verifier")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row), {"id", "statement", "event", "residual"})


class AgentsTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        harness.add(str(paths.defaults_dir() / "harnesses" / "example-generic"), name="demo")
        config.set_active_harness("demo")


class TestRender(AgentsTestCase):
    def test_render_takes_a_routing_document(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["params"]["effort"] = "max"
        text = routing.render("ppl-verifier", harness.load("demo"), config=doc)
        self.assertIn("effort: max", text)
        self.assertNotIn("effort: max", routing.render("ppl-verifier", harness.load("demo")))

    def test_role_takes_a_routing_document(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["params"]["effort"] = "max"
        self.assertEqual(routing.role("ppl-verifier", doc)["params"]["effort"], "max")
        self.assertNotEqual(routing.role("ppl-verifier")["params"]["effort"], "max")

    def test_a_routing_document_handed_in_is_left_as_it_was(self):
        doc = routing.load()
        before = copy.deepcopy(doc)
        routing.render("ppl-code-worker", harness.load("demo"), config=doc)
        self.assertEqual(doc, before)


class TestAgentsApi(AgentsTestCase):
    def test_the_overview_lists_every_role_with_model_tools_and_verification(self):
        status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual([row["role"] for row in data["roles"]], list(routing.REQUIRED_ROLES))
        worker = next(row for row in data["roles"] if row["role"] == "ppl-code-worker")
        self.assertIn("Edit", worker["tools"])
        self.assertEqual(worker["verified"], "unverified")
        self.assertEqual(data["harness"], "demo")
        self.assertEqual(data["routing_hash"], files.read("routing")["hash"])

    def test_the_overview_reports_the_state_of_each_models_verification(self):
        model = routing.summary()[0]["model"]
        rows = [{"tier": None, "model": model, "resolved_id": None, "state": "verified"}]
        with mock.patch.object(models, "status", return_value=rows):
            status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        for row in data["roles"]:
            self.assertEqual(row["verified"], "verified" if row["model"] == model else "unverified")

    def test_the_overview_still_answers_when_the_models_cannot_be_checked(self):
        with mock.patch.object(models, "status", side_effect=PplError("SCHEMA_INVALID", "broken")):
            status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual({row["verified"] for row in data["roles"]}, {"unverified"})

    def test_without_an_active_harness_the_overview_lists_the_base_tools(self):
        doc = config.load()
        doc["harness"].pop("active", None)
        config.save(doc)
        status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertIsNone(data["harness"])
        worker = next(row for row in data["roles"] if row["role"] == "ppl-code-worker")
        self.assertEqual(worker["tools"], list(routing.BASE_TOOLS["ppl-code-worker"]["tools"]))

    def test_a_role_comes_back_with_base_notes_rendered_file_and_enforced_rules(self):
        status, data, _ = panel_client.call("GET", "/api/agents/ppl-test-worker")
        self.assertEqual(status, 200)
        self.assertTrue(data["base"].strip())
        self.assertIn("make test", data["overlay"]["text"])
        self.assertIn("name: ppl-test-worker", data["rendered"])
        self.assertTrue(data["enforced"])

    def test_a_missing_rule_table_is_an_error_answer_not_a_crash(self):
        with mock.patch.object(rules, "default_path", return_value=paths.home() / "no-rules.toml"):
            status, payload, _ = panel_client.call("GET", "/api/agents/ppl-verifier")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def break_the_harness(self):
        (paths.harness_dir("demo") / "harness.toml").write_text("this is [not toml\n", encoding="utf-8")

    def test_the_overview_reports_no_harness_error_when_the_harness_loads(self):
        status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertIsNone(data["harness_error"])

    def test_an_invalid_active_harness_does_not_block_the_routing_editor(self):
        self.break_the_harness()
        status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertIsNone(data["harness"])
        self.assertIn("not valid TOML", data["harness_error"])
        self.assertEqual([row["role"] for row in data["roles"]], list(routing.REQUIRED_ROLES))
        worker = next(row for row in data["roles"] if row["role"] == "ppl-code-worker")
        self.assertEqual(worker["tools"], list(routing.BASE_TOOLS["ppl-code-worker"]["tools"]))
        self.assertEqual(data["routing_hash"], files.read("routing")["hash"])

    def test_a_hand_edited_effort_that_would_add_frontmatter_lines_is_refused_by_the_role_route(self):
        current = files.read("routing")
        text = current["text"].replace('effort = "xhigh"', 'effort = "high\\ntools: [x]"')
        self.assertNotEqual(text, current["text"])
        paths.routing_file().write_text(text, encoding="utf-8")
        status, payload, _ = panel_client.call("GET", "/api/agents/ppl-verifier")
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))

    def test_a_rule_table_that_is_not_utf8_is_an_error_answer_not_a_crash(self):
        broken = paths.home() / "rules.toml"
        broken.write_bytes(b'[[rule]]\nstatement = "caf\xe9"\n')
        with mock.patch.object(rules, "default_path", return_value=broken):
            status, payload, _ = panel_client.call("GET", "/api/agents/ppl-verifier")
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))

    def test_a_role_and_its_preview_still_refuse_an_invalid_active_harness(self):
        self.break_the_harness()
        status, payload, _ = panel_client.call("GET", "/api/agents/ppl-verifier")
        self.assertEqual((status, payload["error_code"]), (400, "HARNESS_INVALID"))
        status, payload, _ = panel_client.call("POST", "/api/agents/ppl-verifier/preview", body={})
        self.assertEqual((status, payload["error_code"]), (400, "HARNESS_INVALID"))

    def test_the_overview_hashes_the_bytes_it_parsed(self):
        read = files.read

        def edit_then_read(key):
            if key == "routing":
                current = read(key)["text"]
                paths.routing_file().write_text(current.replace('effort = "high"', 'effort = "low"'),
                                                encoding="utf-8")
            return read(key)

        with mock.patch.object(files, "read", edit_then_read), \
                mock.patch.object(models, "status", return_value=[]):
            status, data, _ = panel_client.call("GET", "/api/agents")
        self.assertEqual(status, 200)
        self.assertEqual(data["routing_hash"], files.read("routing")["hash"])
        self.assertEqual(data["routing"]["roles"]["ppl-briefer"]["params"]["effort"], "low")

    def test_a_role_names_the_file_its_project_notes_live_in(self):
        key = "harness:demo:templates/ppl-test-worker.part"
        status, data, _ = panel_client.call("GET", "/api/agents/ppl-test-worker")
        self.assertEqual(data["overlay"], files.read(key))
        self.assertEqual(data["harness"], "demo")
        self.assertIn("R-ROLE-TEST-WORKER-MAIN", {row["id"] for row in data["enforced"]})

    def test_preview_renders_draft_notes_and_routing_without_writing(self):
        before = files.read("harness:demo:templates/ppl-code-worker.part")
        doc = routing.load()
        doc["roles"]["ppl-code-worker"]["params"]["effort"] = "max"
        status, data, _ = panel_client.call("POST", "/api/agents/ppl-code-worker/preview",
                                            body={"overlay": "- Use pnpm, never npm.\n", "routing": doc})
        self.assertEqual(status, 200)
        self.assertIn("- Use pnpm, never npm.", data["rendered"])
        self.assertIn("effort: max", data["rendered"])
        self.assertEqual(files.read("harness:demo:templates/ppl-code-worker.part"), before)
        self.assertNotIn('effort = "max"', paths.routing_file().read_text(encoding="utf-8"))

    def test_preview_of_the_routing_alone_keeps_the_notes_on_disk(self):
        doc = routing.load()
        doc["roles"]["ppl-test-worker"]["params"]["effort"] = "max"
        status, data, _ = panel_client.call("POST", "/api/agents/ppl-test-worker/preview",
                                            body={"routing": doc})
        self.assertEqual(status, 200)
        self.assertIn("effort: max", data["rendered"])
        self.assertIn("make test", data["rendered"])

    def test_preview_of_the_notes_alone_keeps_the_routing_on_disk(self):
        status, data, _ = panel_client.call("POST", "/api/agents/ppl-verifier/preview",
                                            body={"overlay": "- Read the migration notes.\n"})
        self.assertEqual(status, 200)
        self.assertIn("- Read the migration notes.", data["rendered"])
        self.assertIn("effort: xhigh", data["rendered"])

    def test_every_preview_renders_in_a_scratch_root_of_its_own_and_removes_it(self):
        roots = []
        write = drafts.write

        def recording(name, doc, fragments, root=None):
            roots.append(root)
            return write(name, doc, fragments, root=root)

        with mock.patch.object(drafts, "write", recording):
            for _ in range(2):
                status, _, _ = panel_client.call("POST", "/api/agents/ppl-code-worker/preview",
                                                 body={"overlay": "- Use pnpm, never npm.\n"})
                self.assertEqual(status, 200)
        self.assertEqual(len(set(roots)), 2)
        for root in roots:
            self.assertEqual(Path(root).parent, drafts.drafts_dir())
            self.assertFalse(Path(root).exists())
        self.assertEqual(list(drafts.drafts_dir().iterdir()), [])

    def test_preview_leaves_the_draft_of_the_same_name_alone(self):
        kept = drafts.write("demo", harness.load("demo"), {"skill/gates.md": "kept\n"})
        status, _, _ = panel_client.call("POST", "/api/agents/ppl-code-worker/preview",
                                         body={"overlay": "- Use pnpm, never npm.\n"})
        self.assertEqual(status, 200)
        self.assertEqual((kept / "skill" / "gates.md").read_text(encoding="utf-8"), "kept\n")
        self.assertEqual([item.name for item in drafts.drafts_dir().iterdir()], ["demo"])

    def test_previews_write_no_agent_file(self):
        doc = routing.load()
        doc["roles"]["ppl-code-worker"]["params"]["effort"] = "max"
        for body in ({"overlay": "- Use pnpm, never npm.\n"}, {"routing": doc}, {}):
            status, _, _ = panel_client.call("POST", "/api/agents/ppl-code-worker/preview", body=body)
            self.assertEqual(status, 200)
        self.assertEqual(list(self.agents_dir.iterdir()), [])

    def test_notes_with_a_nul_are_refused_by_the_preview_and_the_save(self):
        key = "harness:demo:templates/ppl-verifier.part"
        before = files.read(key)
        status, payload, _ = panel_client.call("POST", "/api/agents/ppl-verifier/preview",
                                               body={"overlay": "- a\u0000b\n"})
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))
        status, payload, _ = panel_client.call("PUT", "/api/agents/ppl-verifier/overlay",
                                               body={"text": "- a\u0000b\n", "hash": before["hash"]})
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))
        self.assertEqual(files.read(key), before)

    def test_preview_refuses_notes_that_are_not_text(self):
        status, payload, _ = panel_client.call("POST", "/api/agents/ppl-verifier/preview",
                                               body={"overlay": ["- one"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_preview_refuses_a_routing_document_that_is_not_one(self):
        for bad in ("text", ["x"], {"roles": 5}, {"roles": {}}):
            status, payload, _ = panel_client.call("POST", "/api/agents/ppl-verifier/preview",
                                                   body={"routing": bad})
            self.assertEqual(status, 400, bad)
            self.assertEqual(payload["error_code"], "SCHEMA_INVALID", bad)

    def test_saving_routing_validates_and_writes(self):
        doc = routing.load()
        doc["roles"]["ppl-briefer"]["params"]["effort"] = "low"
        status, data, _ = panel_client.call("PUT", "/api/routing",
                                            body={"config": doc, "hash": files.read("routing")["hash"]})
        self.assertEqual(status, 200)
        self.assertEqual(routing.load()["roles"]["ppl-briefer"]["params"]["effort"], "low")
        self.assertEqual(data["hash"], files.read("routing")["hash"])

    def test_saving_routing_with_an_unknown_effort_is_refused(self):
        before = files.read("routing")
        doc = routing.load()
        doc["roles"]["ppl-briefer"]["params"]["effort"] = "turbo"
        status, payload, _ = panel_client.call("PUT", "/api/routing",
                                               body={"config": doc, "hash": before["hash"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        self.assertEqual(files.read("routing"), before)

    def test_saving_routing_with_a_stale_hash_is_refused(self):
        status, payload, _ = panel_client.call("PUT", "/api/routing",
                                               body={"config": routing.load(), "hash": "0" * 64})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "CHANGED_ON_DISK")

    def test_saving_routing_that_cannot_be_written_as_toml_is_refused(self):
        before = files.read("routing")
        doc = routing.load()
        doc["roles"]["ppl-briefer"]["params"]["effort"] = None
        status, payload, _ = panel_client.call("PUT", "/api/routing",
                                               body={"config": doc, "hash": before["hash"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        self.assertEqual(files.read("routing"), before)

    def test_saving_routing_with_both_a_document_and_text_is_refused(self):
        before = files.read("routing")
        status, payload, _ = panel_client.call("PUT", "/api/routing",
                                               body={"config": routing.load(), "text": before["text"],
                                                     "hash": before["hash"]})
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))
        self.assertEqual(files.read("routing"), before)

    def test_saving_a_model_that_would_add_frontmatter_lines_is_refused(self):
        before = files.read("routing")
        injected = "haiku\ntools: [Read, Edit, Write, Bash]"
        doc = routing.load()
        doc["tiers"]["light"]["model"] = injected
        for body in ({"config": doc}, {"text": tomlout.dumps(doc)}):
            status, payload, _ = panel_client.call("PUT", "/api/routing",
                                                   body=dict(body, hash=before["hash"]))
            self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))
        self.assertEqual(files.read("routing"), before)

    def test_saving_a_model_id_with_brackets_works(self):
        doc = routing.load()
        doc["tiers"]["good"]["model"] = "opus[1m]"
        status, _, _ = panel_client.call("PUT", "/api/routing",
                                         body={"config": doc, "hash": files.read("routing")["hash"]})
        self.assertEqual(status, 200)
        self.assertEqual(routing.load()["tiers"]["good"]["model"], "opus[1m]")

    def test_saving_routing_without_a_document_is_refused(self):
        status, payload, _ = panel_client.call("PUT", "/api/routing",
                                               body={"hash": files.read("routing")["hash"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_saving_raw_routing_text_works_in_expert_mode(self):
        current = files.read("routing")
        text = current["text"].replace('effort = "high"', 'effort = "medium"', 1)
        status, _, _ = panel_client.call("PUT", "/api/routing", body={"text": text, "hash": current["hash"]})
        self.assertEqual(status, 200)
        self.assertEqual(files.read("routing")["text"], text)

    def test_saving_project_notes_writes_the_part_file(self):
        key = "harness:demo:templates/ppl-verifier.part"
        status, data, _ = panel_client.call("PUT", "/api/agents/ppl-verifier/overlay",
                                            body={"text": "- Read the migration notes.\n",
                                                  "hash": files.read(key)["hash"]})
        self.assertEqual(status, 200)
        self.assertEqual(files.read(key)["text"], "- Read the migration notes.\n")

    def test_saving_project_notes_with_a_stale_hash_is_refused(self):
        key = "harness:demo:templates/ppl-verifier.part"
        before = files.read(key)
        status, payload, _ = panel_client.call("PUT", "/api/agents/ppl-verifier/overlay",
                                               body={"text": "- Read the migration notes.\n",
                                                     "hash": "0" * 64})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "CHANGED_ON_DISK")
        self.assertEqual(files.read(key), before)

    def test_saving_project_notes_without_a_hash_is_refused(self):
        key = "harness:demo:templates/ppl-verifier.part"
        before = files.read(key)
        status, payload, _ = panel_client.call("PUT", "/api/agents/ppl-verifier/overlay",
                                               body={"text": "- Read the migration notes.\n"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        self.assertEqual(files.read(key), before)

    def test_saving_project_notes_without_an_active_harness_is_refused(self):
        doc = config.load()
        doc["harness"].pop("active", None)
        config.save(doc)
        status, payload, _ = panel_client.call("PUT", "/api/agents/ppl-verifier/overlay",
                                               body={"text": "- Read the migration notes.\n",
                                                     "hash": "0" * 64})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_an_unknown_role_is_refused(self):
        status, payload, _ = panel_client.call("GET", "/api/agents/ppl-intern")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_without_an_active_harness_there_is_no_preview(self):
        doc = config.load()
        doc["harness"].pop("active", None)
        config.save(doc)
        status, data, _ = panel_client.call("GET", "/api/agents/ppl-verifier")
        self.assertIsNone(data["overlay"])
        self.assertIsNone(data["rendered"])
        status, payload, _ = panel_client.call("POST", "/api/agents/ppl-verifier/preview", body={})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()

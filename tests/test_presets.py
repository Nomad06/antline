import io
import json
import os
import sys
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import cli, config, paths, presets, routing, tomlout
from ppl.errors import PplError
from ppl.studio import files


class TestPresets(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()

    def test_balanced_is_the_example_routing(self):
        with open(paths.defaults_dir() / "routing.example.toml", "rb") as handle:
            example = tomllib.load(handle)
        self.assertEqual(presets.matching(example), "balanced")

    def test_every_preset_covers_every_role_with_a_declared_tier_and_a_known_effort(self):
        tiers = routing.tiers(routing.load())
        for name, roles in presets.PRESETS.items():
            self.assertEqual(sorted(roles), sorted(routing.REQUIRED_ROLES), name)
            for role, spec in roles.items():
                self.assertIn(spec["tier"], tiers, (name, role))
                self.assertIn(spec["effort"], routing.EFFORT_LEVELS, (name, role))
                for tier in spec.get("by_risk", {}).values():
                    self.assertIn(tier, tiers, (name, role))

    def test_applying_a_preset_rewrites_the_roles_and_keeps_the_tiers(self):
        before = routing.tiers(routing.load())
        routing.apply_preset("economy")
        after = routing.load()
        self.assertEqual(presets.matching(after), "economy")
        self.assertEqual(routing.tiers(after), before)
        self.assertNotIn("by_risk", after["roles"]["ppl-code-worker"])

    def test_an_unknown_preset_is_refused(self):
        with self.assertRaises(PplError) as caught:
            routing.apply_preset("cheapest")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_preset_naming_a_tier_routing_does_not_declare_is_refused(self):
        doc = routing.load()
        del doc["tiers"]["heavy"]
        paths.routing_file().write_text(tomlout.dumps(doc), encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            routing.apply_preset("max")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("heavy", caught.exception.message)

    def test_validate_config_refuses_an_unknown_effort(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["params"]["effort"] = "turbo"
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertIn("turbo", caught.exception.message)

    def test_validate_config_refuses_an_unknown_tier(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["tier"] = "giant"
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def write_routing(self, doc):
        paths.routing_file().write_text(tomlout.dumps(doc), encoding="utf-8")

    def broken_params(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["params"] = "fast"
        self.write_routing(doc)
        return doc

    def test_a_preset_keeps_a_role_param_other_than_effort(self):
        doc = routing.load()
        doc["roles"]["ppl-code-worker"]["params"]["temperature"] = 0.2
        self.write_routing(doc)
        routing.apply_preset("economy")
        params = routing.load()["roles"]["ppl-code-worker"]["params"]
        self.assertEqual(params, {"effort": "medium", "temperature": 0.2})

    def test_a_preset_replaces_a_provider_and_model_a_role_pinned_itself(self):
        doc = routing.load()
        entry = doc["roles"]["ppl-test-worker"]
        del entry["tier"]
        entry.update(provider="claude-code-subagent", model="pinned")
        self.write_routing(doc)
        routing.apply_preset("economy")
        entry = routing.load()["roles"]["ppl-test-worker"]
        self.assertEqual(entry["tier"], "average")
        self.assertNotIn("provider", entry)
        self.assertNotIn("model", entry)

    def test_a_refused_preset_leaves_routing_toml_byte_identical(self):
        doc = routing.load()
        del doc["tiers"]["heavy"]
        doc["roles"]["ppl-verifier"]["params"] = "fast"
        self.write_routing(doc)
        broken = paths.routing_file().read_bytes()
        for name in ("cheapest", "max", "economy"):
            with self.subTest(name):
                with self.assertRaises(PplError) as caught:
                    routing.apply_preset(name)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertEqual(paths.routing_file().read_bytes(), broken)

    def test_apply_preset_refuses_params_that_are_not_a_table_and_names_the_role(self):
        self.broken_params()
        with self.assertRaises(PplError) as caught:
            routing.apply_preset("economy")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("ppl-verifier", caught.exception.message)

    def test_matching_and_summary_refuse_params_that_are_not_a_table(self):
        doc = self.broken_params()
        for call in (presets.matching, routing.summary):
            with self.subTest(call.__name__):
                with self.assertRaises(PplError) as caught:
                    call(doc)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("ppl-verifier", caught.exception.message)

    def test_validate_config_refuses_a_missing_role(self):
        doc = routing.load()
        del doc["roles"]["ppl-verifier"]
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("ppl-verifier", caught.exception.message)

    def test_validate_config_refuses_params_that_are_not_a_table(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["params"] = "fast"
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("params", caught.exception.message)

    def test_validate_config_refuses_a_by_risk_that_is_not_a_table(self):
        doc = routing.load()
        doc["roles"]["ppl-code-worker"]["by_risk"] = "good"
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("by_risk", caught.exception.message)

    def test_validate_config_refuses_a_by_risk_tier_that_is_not_declared(self):
        doc = routing.load()
        doc["roles"]["ppl-code-worker"]["by_risk"] = {"high": "giant"}
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("giant", caught.exception.message)

    def test_validate_config_refuses_a_role_whose_provider_is_unknown(self):
        doc = routing.load()
        entry = doc["roles"]["ppl-verifier"]
        del entry["tier"]
        entry.update(provider="nowhere", model="pinned")
        with self.assertRaises(PplError) as caught:
            routing.validate_config(doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("nowhere", caught.exception.message)

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_the_cli_refuses_a_preset_when_params_is_not_a_table(self):
        self.broken_params()
        code, _, err = self.run_cli("routing", "preset", "economy")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_the_cli_asks_for_a_preset_name(self):
        code, _, err = self.run_cli("routing", "preset")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_routing_apply_refuses_a_positional_harness_name(self):
        code, out, err = self.run_cli("routing", "apply", "myharness")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("--harness", json.loads(err)["message"])
        self.assertEqual(list(self.agents_dir.iterdir()), [])

    def test_routing_show_refuses_a_positional_role_name(self):
        code, out, err = self.run_cli("routing", "show", "ppl-verifier")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("--role", json.loads(err)["message"])

    def test_the_cli_applies_a_preset(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["routing", "preset", "max"]), 0)
        self.assertEqual(presets.matching(routing.load()), "max")

    def test_the_preset_document_writes_nothing_and_is_what_the_cli_writes(self):
        before = paths.routing_file().read_bytes()
        document = routing.preset_document("economy")
        self.assertEqual(presets.matching(document), "economy")
        self.assertEqual(paths.routing_file().read_bytes(), before)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["routing", "preset", "economy"]), 0)
        self.assertEqual(paths.routing_file().read_text(encoding="utf-8"), tomlout.dumps(document))
        self.assertFalse((paths.home() / "studio").exists())

    def test_summary_names_the_model_each_role_resolves_to(self):
        rows = {row["role"]: row for row in routing.summary()}
        tiers = routing.tiers(routing.load())
        self.assertEqual(rows["ppl-verifier"]["model"], tiers["good"]["model"])
        self.assertEqual(rows["ppl-code-worker"]["by_risk"], {"high": "good"})


class TestPresetRoutes(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()

    def test_the_presets_route_lists_the_presets_and_the_current_one(self):
        status, data, _ = panel_client.call("GET", "/api/routing/presets")
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in data["presets"]], list(presets.PRESETS))
        self.assertEqual(data["current"], "balanced")
        self.assertEqual(list(data["effort_levels"]), list(routing.EFFORT_LEVELS))
        self.assertEqual(len(data["roles"]), len(routing.REQUIRED_ROLES))

    def test_presets_view_carries_measured_costs(self):
        _, data, _ = panel_client.call("GET", "/api/routing/presets")
        self.assertEqual(data["measured"], {})
        from unittest import mock
        measured = {"balanced": {"runs": 3, "tokens_mean": 12345.0, "usd_mean": None}}
        with mock.patch("ppl.metrics.preset_costs", return_value=measured):
            _, data, _ = panel_client.call("GET", "/api/routing/presets")
        self.assertEqual(data["measured"], measured)

    def choose(self, name, hash_value):
        return panel_client.call("POST", "/api/routing/preset",
                                 body={"name": name, "hash": hash_value})

    def test_choosing_a_preset_through_the_api_applies_it(self):
        _, view, _ = panel_client.call("GET", "/api/routing/presets")
        self.assertEqual(view["routing_hash"], files.read("routing")["hash"])
        status, data, _ = self.choose("economy", view["routing_hash"])
        self.assertEqual(status, 200)
        self.assertEqual(data["current"], "economy")
        self.assertEqual(data["routing_hash"], files.read("routing")["hash"])

    def test_a_preset_through_the_api_keeps_the_replaced_routing_as_one_history_version(self):
        before = files.read("routing")
        status, _, _ = self.choose("economy", before["hash"])
        self.assertEqual(status, 200)
        versions = files.versions("routing")
        self.assertEqual(len(versions), 1)
        self.assertEqual(files.version_text("routing", versions[0]["version"]), before["text"])
        self.assertEqual(presets.matching(routing.load()), "economy")

    def test_a_preset_with_a_stale_hash_is_refused_and_writes_nothing(self):
        # rule: R-STUDIO-CHANGED-ON-DISK
        loaded = files.read("routing")
        changed = loaded["text"] + "\n# edited in a terminal\n"
        paths.routing_file().write_text(changed, encoding="utf-8")
        status, payload, _ = self.choose("economy", loaded["hash"])
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "CHANGED_ON_DISK")
        self.assertEqual(paths.routing_file().read_text(encoding="utf-8"), changed)
        self.assertEqual(files.versions("routing"), [])

    def test_a_preset_without_a_hash_is_refused_and_writes_nothing(self):
        before = paths.routing_file().read_bytes()
        status, payload, _ = panel_client.call("POST", "/api/routing/preset", body={"name": "economy"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        self.assertEqual(paths.routing_file().read_bytes(), before)

    def test_params_that_are_not_a_table_answer_400_not_500(self):
        doc = routing.load()
        doc["roles"]["ppl-verifier"]["params"] = "fast"
        paths.routing_file().write_text(tomlout.dumps(doc), encoding="utf-8")
        for method, path, body in (("GET", "/api/routing/presets", None),
                                   ("POST", "/api/routing/preset", {"name": "economy"})):
            with self.subTest(method):
                status, payload, _ = panel_client.call(method, path, body=body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
                self.assertIn("ppl-verifier", payload["message"])

    def test_an_unknown_preset_through_the_api_is_refused(self):
        status, payload, _ = panel_client.call("POST", "/api/routing/preset", body={"name": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

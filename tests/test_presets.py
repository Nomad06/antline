import io
import os
import sys
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import cli, config, paths, presets, routing, tomlout
from ppl.errors import PplError


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

    def test_the_cli_applies_a_preset(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["routing", "preset", "max"]), 0)
        self.assertEqual(presets.matching(routing.load()), "max")

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

    def test_choosing_a_preset_through_the_api_applies_it(self):
        status, data, _ = panel_client.call("POST", "/api/routing/preset", body={"name": "economy"})
        self.assertEqual(status, 200)
        self.assertEqual(data["current"], "economy")

    def test_an_unknown_preset_through_the_api_is_refused(self):
        status, payload, _ = panel_client.call("POST", "/api/routing/preset", body={"name": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

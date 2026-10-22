import copy
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import config, harness, paths
from ppl.studio import drafts, files

EXAMPLE = str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "defaults" / "harnesses" / "example-generic")


class HarnessApiTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        harness.add(EXAMPLE, name="demo")
        config.set_active_harness("demo")

    def get(self, name="demo"):
        status, data, _ = panel_client.call("GET", "/api/harnesses/" + name)
        self.assertEqual(status, 200, data)
        return data


class TestHarnessApi(HarnessApiTestCase):
    def test_the_list_names_the_installed_harnesses_and_the_active_one(self):
        status, data, _ = panel_client.call("GET", "/api/harnesses")
        self.assertEqual(status, 200)
        self.assertEqual(data, {"names": ["demo"], "active": "demo"})

    def test_a_harness_comes_back_with_its_fragments_hashes_and_check(self):
        data = self.get()
        self.assertEqual(data["doc"]["harness"]["name"], "example-generic")
        self.assertIn("skill/gates.md", data["fragments"])
        self.assertEqual(data["hashes"]["harness.toml"], files.read("harness:demo:harness.toml")["hash"])
        self.assertTrue(data["check"]["ok"])
        self.assertTrue(data["active"])

    def test_saving_writes_the_toml_and_the_changed_fragments_and_keeps_history(self):
        data = self.get()
        doc = copy.deepcopy(data["doc"])
        doc["gate"][0]["command"] = "make all"
        fragments = dict(data["fragments"], **{"skill/gates.md": "Targets: `make all`.\n"})
        status, result, _ = panel_client.call("PUT", "/api/harnesses/demo",
                                              body={"doc": doc, "fragments": fragments,
                                                    "hashes": data["hashes"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(harness.load("demo")["gate"][0]["command"], "make all")
        self.assertEqual(files.read("harness:demo:skill/gates.md")["text"], "Targets: `make all`.\n")
        self.assertEqual(len(files.versions("harness:demo:harness.toml")), 1)
        self.assertEqual(files.versions("harness:demo:skill/baseline.md"), [])

    def test_saving_an_invalid_harness_is_refused_and_writes_nothing(self):
        data = self.get()
        doc = copy.deepcopy(data["doc"])
        doc["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        status, payload, _ = panel_client.call("PUT", "/api/harnesses/demo",
                                               body={"doc": doc, "fragments": data["fragments"],
                                                     "hashes": data["hashes"]})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "HARNESS_INVALID")
        self.assertEqual(files.versions("harness:demo:harness.toml"), [])

    def test_saving_with_a_stale_hash_is_refused(self):
        data = self.get()
        hashes = dict(data["hashes"], **{"harness.toml": "0" * 64})
        status, payload, _ = panel_client.call("PUT", "/api/harnesses/demo",
                                               body={"doc": data["doc"], "fragments": data["fragments"],
                                                     "hashes": hashes})
        self.assertEqual(status, 409)
        self.assertEqual(payload["error_code"], "CHANGED_ON_DISK")

    def test_raw_toml_is_saved_as_written(self):
        data = self.get()
        text = data["text"].replace('command = "make build"', 'command = "make release"')
        status, _, _ = panel_client.call("PUT", "/api/harnesses/demo",
                                         body={"text": text, "fragments": data["fragments"],
                                               "hashes": data["hashes"]})
        self.assertEqual(status, 200)
        self.assertEqual(files.read("harness:demo:harness.toml")["text"], text)

    def test_a_broken_harness_toml_is_still_shown_for_repair(self):
        (paths.harness_dir("demo") / "harness.toml").write_text("not = [toml", encoding="utf-8")
        data = self.get()
        self.assertIsNone(data["doc"])
        self.assertEqual(data["text"], "not = [toml")
        self.assertFalse(data["check"]["ok"])

    def test_there_is_no_activate_route_so_activation_goes_through_apply(self):
        harness.add(EXAMPLE, name="other")
        for name in ("other", "nope"):
            status, payload, _ = panel_client.call("POST", "/api/harnesses/%s/activate" % name,
                                                   body={})
            self.assertEqual(status, 404, name)
        self.assertEqual(config.active_harness(), "demo")

    def test_applying_makes_the_harness_the_active_one(self):
        harness.add(EXAMPLE, name="other")
        status, payload, _ = panel_client.call("POST", "/api/harnesses/other/apply", body={})
        job = self.wait(payload)
        self.assertEqual(job["state"], "done", job)
        status, data, _ = panel_client.call("GET", "/api/harnesses")
        self.assertEqual(data, {"names": ["demo", "other"], "active": "other"})
        self.assertTrue(self.get("other")["active"])
        self.assertFalse(self.get("demo")["active"])

    def test_applying_a_broken_harness_fails_the_job_and_activates_nothing(self):
        harness.add(EXAMPLE, name="other")
        (paths.harness_dir("other") / "harness.toml").write_text("not = [toml", encoding="utf-8")
        status, payload, _ = panel_client.call("POST", "/api/harnesses/other/apply", body={})
        job = self.wait(payload)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["error_code"], "HARNESS_INVALID")
        self.assertEqual(config.active_harness(), "demo")

    def test_apply_runs_as_a_job_into_the_configured_dirs(self):
        status, payload, _ = panel_client.call("POST", "/api/harnesses/demo/apply", body={})
        job = self.wait(payload)
        self.assertEqual(job["state"], "done", job)
        self.assertTrue((self.agents_dir / "ppl-code-worker.md").is_file())
        self.assertTrue((self.skills_dir / "pipeline" / "SKILL.md").is_file())

    def test_adding_from_a_directory_runs_as_a_job(self):
        status, payload, _ = panel_client.call("POST", "/api/harnesses",
                                               body={"source": EXAMPLE, "name": "copy"})
        job = self.wait(payload)
        self.assertEqual(job["result"], {"name": "copy"})
        self.assertIn("copy", harness.list_names())

    def test_creating_from_a_draft_installs_it_under_its_name(self):
        data = self.get()
        status, result, _ = panel_client.call("POST", "/api/harnesses/new",
                                              body={"name": "fresh", "doc": data["doc"],
                                                    "fragments": data["fragments"]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["name"], "fresh")
        self.assertIn("fresh", harness.list_names())
        status, payload, _ = panel_client.call("POST", "/api/harnesses/new",
                                               body={"name": "fresh", "doc": data["doc"],
                                                     "fragments": data["fragments"]})
        self.assertEqual(status, 400)


class TestHarnessApiEdges(HarnessApiTestCase):
    def put(self, **body):
        return panel_client.call("PUT", "/api/harnesses/demo", body=body)

    def test_an_unknown_harness_is_not_found_on_every_route(self):
        for method, path in (("GET", "/api/harnesses/nope"), ("PUT", "/api/harnesses/nope"),
                             ("POST", "/api/harnesses/nope/check"),
                             ("POST", "/api/harnesses/nope/apply")):
            status, payload, _ = panel_client.call(method, path, body=None if method == "GET" else {})
            self.assertEqual((status, payload["error_code"]), (404, "NOT_FOUND"), path)

    def test_a_document_with_a_date_is_sent_as_text_only(self):
        path = paths.harness_dir("demo") / "harness.toml"
        path.write_text(path.read_text(encoding="utf-8") + "\nreleased = 2026-10-02\n", encoding="utf-8")
        data = self.get()
        self.assertIsNone(data["doc"])
        self.assertIn("released = 2026-10-02", data["text"])

    def test_a_save_names_the_document_or_the_text_never_both(self):
        data = self.get()
        status, payload, _ = self.put(doc=data["doc"], text=data["text"],
                                      fragments=data["fragments"], hashes=data["hashes"])
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))
        status, payload, _ = self.put(fragments=data["fragments"], hashes=data["hashes"])
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))

    def test_a_save_with_broken_toml_is_refused_and_writes_nothing(self):
        data = self.get()
        status, payload, _ = self.put(text="not = [toml", fragments=data["fragments"],
                                      hashes=data["hashes"])
        self.assertEqual((status, payload["error_code"]), (400, "HARNESS_INVALID"))
        self.assertEqual(files.read("harness:demo:harness.toml")["text"], data["text"])

    def test_a_save_refuses_a_fragment_the_harness_does_not_have(self):
        data = self.get()
        for rel in ("skill/other.md", "harness.toml", "../escape.md"):
            status, payload, _ = self.put(doc=data["doc"], hashes=data["hashes"],
                                          fragments=dict(data["fragments"], **{rel: "x\n"}))
            self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"), rel)
        self.assertEqual(files.versions("harness:demo:harness.toml"), [])
        self.assertFalse((paths.harness_dir("demo").parent / "escape.md").exists())

    def test_a_save_refuses_fragments_and_hashes_that_are_not_objects(self):
        data = self.get()
        status, payload, _ = self.put(doc=data["doc"], hashes=data["hashes"], fragments=["x"])
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))
        status, payload, _ = self.put(doc=data["doc"], hashes="x", fragments=data["fragments"])
        self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"))

    def test_a_fragment_changed_on_disk_since_the_page_loaded_refuses_the_save(self):
        data = self.get()
        (paths.harness_dir("demo") / "skill" / "gates.md").write_text("changed\n", encoding="utf-8")
        status, payload, _ = self.put(doc=data["doc"], hashes=data["hashes"],
                                      fragments=data["fragments"])
        self.assertEqual((status, payload["error_code"]), (409, "CHANGED_ON_DISK"))

    def test_checking_never_rewrites_a_wizard_draft_of_the_same_name(self):
        data = self.get()
        drafts.write("demo", data["doc"], {"skill/gates.md": "wizard\n"})
        kept = (drafts.drafts_dir() / "demo" / "skill" / "gates.md").read_text(encoding="utf-8")
        status, result, _ = panel_client.call("POST", "/api/harnesses/demo/check",
                                              body={"doc": data["doc"], "fragments": data["fragments"]})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual((drafts.drafts_dir() / "demo" / "skill" / "gates.md").read_text(encoding="utf-8"),
                         kept)
        self.assertEqual(sorted(item.name for item in drafts.drafts_dir().iterdir()), ["demo"])

    def test_checking_reports_a_problem_as_a_result_not_an_error(self):
        data = self.get()
        doc = copy.deepcopy(data["doc"])
        doc["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        status, result, _ = panel_client.call("POST", "/api/harnesses/demo/check",
                                              body={"doc": doc, "fragments": data["fragments"]})
        self.assertEqual(status, 200)
        self.assertFalse(result["ok"])
        self.assertTrue(result["errors"])

    def test_creating_an_invalid_draft_installs_nothing_and_leaves_no_scratch(self):
        data = self.get()
        doc = copy.deepcopy(data["doc"])
        doc["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        status, payload, _ = panel_client.call("POST", "/api/harnesses/new",
                                               body={"name": "fresh", "doc": doc,
                                                     "fragments": data["fragments"]})
        self.assertEqual((status, payload["error_code"]), (400, "HARNESS_INVALID"))
        self.assertEqual(harness.list_names(), ["demo"])
        self.assertEqual(list(drafts.drafts_dir().iterdir()), [])

    def test_creating_leaves_no_scratch_and_the_copy_is_the_checked_draft(self):
        data = self.get()
        fragments = dict(data["fragments"], **{"skill/gates.md": "Fresh gates.\n"})
        status, result, _ = panel_client.call("POST", "/api/harnesses/new",
                                              body={"name": "fresh", "doc": data["doc"],
                                                    "fragments": fragments})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["check"]["ok"])
        self.assertEqual(files.read("harness:fresh:skill/gates.md")["text"], "Fresh gates.\n")
        self.assertEqual(harness.load("fresh")["harness"]["name"], "fresh")
        self.assertEqual(list(drafts.drafts_dir().iterdir()), [])

    def test_creating_refuses_a_bad_name(self):
        data = self.get()
        for name in ("../x", "x.git", "", None):
            status, payload, _ = panel_client.call("POST", "/api/harnesses/new",
                                                   body={"name": name, "doc": data["doc"],
                                                         "fragments": data["fragments"]})
            self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"), name)

    def test_adding_needs_a_source_and_a_valid_name_before_any_job_starts(self):
        for body in ({}, {"source": "  "}, {"source": 5}, {"source": EXAMPLE, "name": "../x"}):
            status, payload, _ = panel_client.call("POST", "/api/harnesses", body=body)
            self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"), body)

    def source_named(self, name):
        source = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / name
        shutil.copytree(EXAMPLE, source)
        return source

    def test_adding_without_a_name_checks_the_name_derived_from_the_source(self):
        sources = [str(self.source_named(name)) for name in ("Demo.Replacing", "demo.replaced")]
        sources.append("https://example.invalid/team/demo.git.git")
        for source in sources:
            for given in ({}, {"name": ""}, {"name": None}):
                status, payload, _ = panel_client.call("POST", "/api/harnesses",
                                                       body=dict(given, source=source))
                self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"),
                                 (source, given))
        self.assertEqual(harness.list_names(), ["demo"])

    def test_adding_without_a_name_installs_under_the_derived_name(self):
        status, payload, _ = panel_client.call("POST", "/api/harnesses",
                                               body={"source": str(self.source_named("team"))})
        self.assertEqual(self.wait(payload)["result"], {"name": "team"})
        self.assertIn("team", harness.list_names())

    def test_adding_refuses_a_relative_local_source(self):
        for source in ("defaults/harnesses/example-generic", "./example-generic", "example-generic"):
            status, payload, _ = panel_client.call("POST", "/api/harnesses",
                                                   body={"source": source, "name": "rel"})
            self.assertEqual((status, payload["error_code"]), (400, "SCHEMA_INVALID"), source)
        self.assertEqual(harness.list_names(), ["demo"])

    def test_adding_takes_a_source_that_is_absolute_after_the_home_is_expanded(self):
        shutil.copytree(EXAMPLE, Path(os.environ["HOME"]) / "mine")
        status, payload, _ = panel_client.call("POST", "/api/harnesses", body={"source": "~/mine"})
        self.assertEqual(self.wait(payload)["result"], {"name": "mine"})

    def test_adding_a_directory_without_a_harness_fails_the_job_and_installs_nothing(self):
        empty = tmphome.mkdtemp(prefix="pplempty-")
        status, payload, _ = panel_client.call("POST", "/api/harnesses", body={"source": empty,
                                                                               "name": "hollow"})
        job = self.wait(payload)
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["error_code"], "HARNESS_INVALID")
        self.assertEqual(harness.list_names(), ["demo"])


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import config, harness, lessons, memory, paths, routing, store
from ppl.studio import files

APPLY = "run ppl harness apply demo to render it"


class LessonsApiCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        config.ensure()
        harness.add(str(paths.defaults_dir() / "harnesses" / "example-generic"), name="demo")
        config.set_active_harness("demo")
        row = {"id": "M-1", "role": "ppl-code-worker", "cue": "a change touches the cart",
               "lesson": "run the cart tests", "sources": ["DEMO-1:finding:F-1"],
               "accepted_at": "2026-10-28T00:00:00Z", "seen": 1, "status": "active"}
        self.memory_path = paths.harness_dir("demo") / "memory.toml"
        self.memory_path.write_text(memory.dumps([row]), encoding="utf-8")
        store.write_json(lessons.proposals_path("demo", "DEMO-9"), {
            "task": "DEMO-9", "harness": "demo", "recorded_at": "2026-10-28T00:00:00Z",
            "no_entry": False, "assessments": [], "proposals": [
                {"id": "DEMO-9.1", "role": "ppl-verifier", "cue": "an invoice total changes",
                 "lesson": "check the rounding", "status": "pending",
                 "evidence": [{"kind": "finding", "ref": "F-9"}]}]})

    def entries(self):
        return memory.parse(self.memory_path.read_bytes(), "memory.toml", routing.LESSON_ROLES)


class TestLessonsApi(LessonsApiCase):
    def test_the_listing_shows_pending_proposals_and_memory(self):
        status, data, _ = panel_client.call("GET", "/api/lessons")
        self.assertEqual(status, 200)
        self.assertEqual(data["harness"], "demo")
        self.assertEqual([p["id"] for p in data["proposals"]], ["DEMO-9.1"])
        self.assertEqual(data["proposals"][0]["task"], "DEMO-9")
        self.assertEqual([m["id"] for m in data["memory"]], ["M-1"])

    def test_the_listing_without_an_active_harness_is_null(self):
        config.set_active_harness("")
        status, data, _ = panel_client.call("GET", "/api/lessons")
        self.assertEqual((status, data), (200, {"harness": None}))

    def test_accept_through_the_studio_writes_memory_and_history(self):
        status, data, _ = panel_client.call("POST", "/api/lessons/DEMO-9.1/accept", body={})
        self.assertEqual(status, 200)
        self.assertEqual(data["apply"], APPLY)
        self.assertEqual(data["entry"]["id"], "M-2")
        self.assertEqual([e["id"] for e in self.entries()], ["M-1", "M-2"])
        self.assertTrue(files.versions("harness:demo:memory.toml"))
        _, after, _ = panel_client.call("GET", "/api/lessons")
        self.assertEqual(after["proposals"], [])

    def test_accept_takes_the_owners_edit(self):
        status, data, _ = panel_client.call("POST", "/api/lessons/DEMO-9.1/accept",
                                            body={"lesson": "round once"})
        self.assertEqual(status, 200)
        self.assertEqual(data["entry"]["lesson"], "round once")

    def test_reject_without_a_reason_is_refused(self):
        status, data, _ = panel_client.call("POST", "/api/lessons/DEMO-9.1/reject", body={})
        self.assertEqual(status, 400)
        self.assertEqual(data["error_code"], "SCHEMA_INVALID")
        status, data, _ = panel_client.call("POST", "/api/lessons/DEMO-9.1/reject",
                                            body={"reason": "too vague"})
        self.assertEqual((status, data["apply"]), (200, APPLY))
        status, data, _ = panel_client.call("POST", "/api/lessons/DEMO-9.1/reject",
                                            body={"reason": "again"})
        self.assertEqual(data["error_code"], "ILLEGAL_TRANSITION")

    def test_retire_and_edit_go_through_the_same_checks(self):
        status, data, _ = panel_client.call("POST", "/api/memory/M-1/edit",
                                            body={"lesson": "run all the cart tests"})
        self.assertEqual((status, data["entry"]["lesson"]), (200, "run all the cart tests"))
        status, data, _ = panel_client.call("POST", "/api/memory/M-1/edit", body={})
        self.assertEqual((status, data["error_code"]), (400, "SCHEMA_INVALID"))
        status, data, _ = panel_client.call("POST", "/api/memory/M-1/retire", body={})
        self.assertEqual((status, data["apply"]), (200, APPLY))
        self.assertEqual(self.entries()[0]["status"], "retired")
        status, data, _ = panel_client.call("POST", "/api/memory/M-1/retire", body={})
        self.assertEqual(status, 400)
        status, data, _ = panel_client.call("POST", "/api/memory/M-1/edit", body={"cue": "x"})
        self.assertEqual(status, 400)

    def test_a_write_without_the_header_is_refused(self):
        before = self.memory_path.read_bytes()
        base = panel_client.session_headers()
        base["Content-Type"] = "application/json"
        for path in ("/api/lessons/DEMO-9.1/accept", "/api/memory/M-1/retire"):
            status, _, _ = panel_client.call("POST", path, body={}, base=base)
            self.assertEqual(status, 403, path)
        self.assertEqual(self.memory_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

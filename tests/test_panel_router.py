import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl.panel import jobs
from ppl.panel.router import Router


def ok(params, body, query):
    return {"params": params}


class TestRouter(unittest.TestCase):
    def setUp(self):
        self.router = Router()
        self.router.add("GET", "/api/things", ok)
        self.router.add("GET", "/api/things/<name>", ok)
        self.router.add("PUT", "/api/things/<name>", ok)

    def test_a_literal_path_matches(self):
        fn, params = self.router.match("GET", "/api/things")
        self.assertIs(fn, ok)
        self.assertEqual(params, {})

    def test_a_placeholder_binds_one_segment(self):
        fn, params = self.router.match("GET", "/api/things/demo-1.x")
        self.assertIs(fn, ok)
        self.assertEqual(params, {"name": "demo-1.x"})

    def test_a_placeholder_never_binds_dots_or_odd_characters(self):
        for path in ("/api/things/..", "/api/things/.", "/api/things/a%2Fb", "/api/things/a b"):
            self.assertEqual(self.router.match("GET", path), (None, None), path)

    def test_a_longer_path_does_not_match(self):
        self.assertEqual(self.router.match("GET", "/api/things/a/b"), (None, None))

    def test_another_method_on_a_known_path_is_reported(self):
        self.assertEqual(self.router.match("POST", "/api/things/a"), (False, None))


class TestCoreRoutes(panel_client.StudioTestCase):
    def test_a_job_is_read_back_through_the_api(self):
        job_id = jobs.JOBS.start("demo", lambda emit: {"answer": 42})
        state = self.wait({"job": job_id})
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["result"], {"answer": 42})

    def test_an_unknown_job_is_refused(self):
        status, payload, _ = panel_client.call("GET", "/api/jobs/nope")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_an_unknown_route_is_not_found(self):
        status, payload, _ = panel_client.call("GET", "/api/nothing-here")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_a_method_the_route_does_not_take_is_refused(self):
        status, payload, _ = panel_client.call("PUT", "/api/jobs/x", body={})
        self.assertEqual(status, 405)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_put_passes_the_same_write_guard_as_post(self):
        headers = panel_client.write_headers()
        del headers["X-PPL"]
        status, payload, _ = panel_client.call("PUT", "/api/jobs/x", body={}, base=headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")

    def assert_malformed_path_refused(self, method, body=None):
        status, payload, _ = panel_client.call(method, "//[", body=body)
        self.assertEqual(status, 400)
        self.assertEqual(payload, {"error_code": "SCHEMA_INVALID",
                                   "message": "malformed request path"})

    def test_a_get_with_a_path_urlparse_refuses_is_a_bad_request(self):
        self.assert_malformed_path_refused("GET")

    def test_a_post_with_a_path_urlparse_refuses_is_a_bad_request(self):
        self.assert_malformed_path_refused("POST", body={})

    def test_a_put_with_a_path_urlparse_refuses_is_a_bad_request(self):
        self.assert_malformed_path_refused("PUT", body={})


if __name__ == "__main__":
    unittest.main()

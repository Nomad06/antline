import io
import os
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl.panel import jobs, server
from ppl.panel.router import Raw, Router


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

    def test_a_raw_answer_must_be_bytes(self):
        with self.assertRaises(TypeError):
            Raw("<p>text</p>", "text/html")


class TestCoreRoutes(panel_client.StudioTestCase):
    def use_routes(self, *routes):
        """Serve only these (method, pattern, fn) routes for this test."""
        router = Router()
        for method, pattern, fn in routes:
            router.add(method, pattern, fn)
        patcher = mock.patch.object(server, "ROUTES", router)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recording_routes(self):
        """POST and PUT /api/things/<name>, each call recorded in the returned list."""
        calls = []

        def record(params, body, query):
            calls.append(body)
            return {"stored": params["name"]}
        self.use_routes(("POST", "/api/things/<name>", record),
                        ("PUT", "/api/things/<name>", record))
        return calls

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
        # rule: R-STUDIO-JSON-ONLY
        headers = panel_client.write_headers()
        del headers["X-PPL"]
        status, payload, _ = panel_client.call("PUT", "/api/jobs/x", body={}, base=headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")

    def test_a_put_without_the_session_cookie_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        calls = self.recording_routes()
        headers = panel_client.write_headers()
        del headers["Cookie"]
        status, payload, _ = panel_client.call("PUT", "/api/things/demo", body={"a": 1},
                                               base=headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")
        self.assertEqual(calls, [])

    def test_a_put_with_the_session_hands_its_body_to_the_route(self):
        calls = self.recording_routes()
        status, payload, _ = panel_client.call("PUT", "/api/things/demo", body={"a": 1})
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"stored": "demo"})
        self.assertEqual(calls, [{"a": 1}])

    def test_a_raw_answer_is_served_with_its_content_type(self):
        self.use_routes(("GET", "/api/page",
                         lambda params, body, query: Raw(b"<p>hi</p>", "text/html")))
        status, payload, headers = panel_client.call("GET", "/api/page")
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"<p>hi</p>")
        self.assertEqual(headers["Content-Type"], "text/html")

    def test_an_answer_json_cannot_encode_is_an_internal_error(self):
        self.use_routes(*((method, "/api/bad", lambda params, body, query: Path("x"))
                          for method in ("GET", "POST", "PUT")))
        for method, body in (("GET", None), ("POST", {}), ("PUT", {})):
            with self.subTest(method=method), redirect_stderr(io.StringIO()):
                status, payload, _ = panel_client.call(method, "/api/bad", body=body)
                self.assertEqual(status, 500)
                self.assertEqual(payload["error_code"], "INTERNAL_ERROR")
                self.assertTrue(payload["message"].startswith("TypeError: "), payload)

    def test_a_job_whose_result_json_cannot_encode_still_reads_back(self):
        with redirect_stderr(io.StringIO()):
            state = self.wait({"job": jobs.JOBS.start("unencodable", lambda emit: Path("x"))})
        self.assertEqual(state["state"], "failed")
        self.assertIsNone(state["result"])
        self.assertEqual(state["error"]["error_code"], "INTERNAL_ERROR")

    def test_a_body_length_that_cannot_be_read_safely_is_refused(self):
        calls = self.recording_routes()
        lengths = ("-1", str(server.MAX_BODY + 1), "9" * 30, "twelve")
        for method in ("POST", "PUT"):
            for length in lengths:
                with self.subTest(method=method, length=length):
                    status, payload, _ = panel_client.call(
                        method, "/api/things/demo", body={"a": 1},
                        headers={"Content-Length": length})
                    self.assertEqual(status, 400)
                    self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
        self.assertEqual(calls, [])

    def test_a_body_at_the_cap_is_read(self):
        calls = self.recording_routes()
        padding = server.MAX_BODY - len('{"a": ""}')
        status, _, _ = panel_client.call("PUT", "/api/things/demo", body={"a": "x" * padding})
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)

    def assert_malformed_path_refused(self, method, body=None):
        status, payload, _ = panel_client.call(method, "http://[", body=body)
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

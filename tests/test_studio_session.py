import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl.panel import session


class TestStudioSession(panel_client.StudioTestCase):
    def test_a_request_without_the_session_cookie_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        status, payload, _ = panel_client.call("GET", "/api/runs", base={"Host": panel_client.HOST})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_a_wrong_session_cookie_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        status, payload, _ = panel_client.call(
            "GET", "/api/runs", base={"Host": panel_client.HOST, "Cookie": "ppl_session=wrong"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_a_cookie_from_an_earlier_start_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        stale = panel_client.session_headers()
        session.start(panel_client.PORT)
        status, payload, _ = panel_client.call("GET", "/api/runs", base=stale)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_a_request_with_the_session_cookie_is_served(self):
        status, payload, _ = panel_client.call("GET", "/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual(payload, [])

    def test_the_url_token_is_exchanged_for_a_cookie(self):
        token = session.token()
        status, _, headers = panel_client.call("GET", "/?t=" + token,
                                               base={"Host": panel_client.HOST})
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/")
        self.assertIn("ppl_session=" + token, headers["Set-Cookie"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def test_a_wrong_url_token_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        status, payload, _ = panel_client.call("GET", "/?t=wrong", base={"Host": panel_client.HOST})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_every_start_makes_a_new_token(self):
        first = session.start(panel_client.PORT)
        second = session.start(panel_client.PORT)
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(second), 40)
        self.assertEqual(session.url(), "http://127.0.0.1:%d/?t=%s" % (panel_client.PORT, second))

    def test_a_foreign_host_header_is_refused(self):
        # rule: R-STUDIO-HOST-CHECK
        headers = panel_client.session_headers()
        headers["Host"] = "studio.attacker.example:%d" % panel_client.PORT
        status, payload, _ = panel_client.call("GET", "/api/runs", base=headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")

    def test_a_loopback_host_on_another_port_is_refused(self):
        # rule: R-STUDIO-HOST-CHECK
        headers = panel_client.session_headers()
        headers["Host"] = "127.0.0.1:9999"
        status, payload, _ = panel_client.call("GET", "/api/runs", base=headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")

    def test_the_host_check_fails_closed_before_any_start(self):
        # rule: R-STUDIO-HOST-CHECK
        saved = dict(session._state)
        self.addCleanup(session._state.update, saved)
        session._state["port"] = None
        self.assertFalse(session.host_ok({"Host": panel_client.HOST}))

    def test_localhost_is_accepted_as_the_host(self):
        headers = panel_client.session_headers()
        headers["Host"] = "localhost:%d" % panel_client.PORT
        status, _, _ = panel_client.call("GET", "/api/runs", base=headers)
        self.assertEqual(status, 200)

    def test_a_post_without_a_json_content_type_is_refused(self):
        # rule: R-STUDIO-JSON-ONLY
        headers = panel_client.write_headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        status, payload, _ = panel_client.call("POST", "/api/runs/DEMO-1/control",
                                               body={"command": "pause"}, base=headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")

    def test_a_json_content_type_with_a_charset_passes_the_guard(self):
        headers = panel_client.write_headers()
        headers["Content-Type"] = "application/json; charset=utf-8"
        status, payload, _ = panel_client.call("POST", "/api/runs/DEMO-1/control",
                                               body={"command": "pause"}, base=headers)
        # past the guard, refused only because the run does not exist
        self.assertEqual(status, 400)
        self.assertNotEqual(payload["error_code"], "BOUNDARY_VIOLATION")


if __name__ == "__main__":
    unittest.main()

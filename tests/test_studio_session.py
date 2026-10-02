import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl import paths, run
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

    def test_a_malformed_foreign_cookie_does_not_hide_the_session(self):
        valid = "%s=%s" % (session.COOKIE, session.token())
        for cookie in (valid + "; a=b c",
                       "a=b c; " + valid,
                       "foo; " + valid,
                       "foo=; " + valid,
                       'j={"x":1}; ' + valid,
                       "%s=stale; %s" % (session.COOKIE, valid),
                       "%s; %s=stale" % (valid, session.COOKIE)):
            with self.subTest(cookie=cookie):
                status, payload, _ = panel_client.call(
                    "GET", "/api/runs", base={"Host": panel_client.HOST, "Cookie": cookie})
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

    def test_the_exchange_redirect_has_no_fragment_of_its_own(self):
        # The browser keeps the fragment it opened with (`/?t=..#/setup`) across a
        # redirect only when the Location carries none.
        status, _, headers = panel_client.call("GET", "/?t=" + session.token(),
                                               base={"Host": panel_client.HOST})
        self.assertEqual(status, 302)
        self.assertNotIn("#", headers["Location"])

    def test_the_url_token_is_not_exchanged_under_a_foreign_host(self):
        # rule: R-STUDIO-HOST-CHECK
        base = {"Host": "studio.attacker.example:%d" % panel_client.PORT}
        status, payload, headers = panel_client.call("GET", "/?t=" + session.token(), base=base)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")
        self.assertNotIn("Set-Cookie", headers)

    def test_a_wrong_url_token_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        status, payload, _ = panel_client.call("GET", "/?t=wrong", base={"Host": panel_client.HOST})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_a_non_ascii_url_token_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        for query in ("%C3%A9", "%FF"):
            with self.subTest(query=query):
                status, payload, _ = panel_client.call("GET", "/?t=" + query,
                                                       base={"Host": panel_client.HOST})
                self.assertEqual(status, 403)
                self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_a_quoted_non_ascii_cookie_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        base = {"Host": panel_client.HOST, "Cookie": '%s="\xe9"' % session.COOKIE}
        status, payload, _ = panel_client.call("GET", "/api/runs", base=base)
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

    def test_header_names_are_matched_without_regard_to_case(self):
        base = {"host": panel_client.HOST,
                "cookie": "%s=%s" % (session.COOKIE, session.token())}
        status, payload, _ = panel_client.call("GET", "/api/runs", base=base)
        self.assertEqual(status, 200)
        self.assertEqual(payload, [])

    def _post_control(self, base):
        task = "DEMO-1"
        run.init(task, "https://j/x", "/tmp/repo")
        status, payload, _ = panel_client.call("POST", "/api/runs/%s/control" % task,
                                               body={"command": "pause"}, base=base)
        return status, payload, paths.run_dir(task) / "control.json"

    def test_a_post_without_the_session_cookie_is_refused(self):
        # rule: R-STUDIO-SESSION-TOKEN
        headers = panel_client.write_headers()
        del headers["Cookie"]
        status, payload, control_file = self._post_control(headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")
        self.assertFalse(control_file.exists())

    def test_a_post_with_a_foreign_host_is_refused(self):
        # rule: R-STUDIO-HOST-CHECK
        headers = panel_client.write_headers()
        headers["Host"] = "studio.attacker.example:%d" % panel_client.PORT
        status, payload, control_file = self._post_control(headers)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")
        self.assertFalse(control_file.exists())

    def test_a_post_with_the_session_writes_the_control_file(self):
        status, payload, control_file = self._post_control(panel_client.write_headers())
        self.assertEqual(status, 200)
        self.assertEqual(payload["command"], "pause")
        self.assertTrue(control_file.exists())

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

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
import panel_client
from ppl.panel import api_static, server

STATIC = api_static.STATIC
PINNED = "72284e8e9079c87817145df1110f74e8a2aa040b2fc384922e18dfcb46fc1fd7"
IMPORT_RE = re.compile(r"""(?:\bimport\b[^"';]*?\bfrom\s*|\bimport\s*\(?\s*)["']([^"']+)["']""")


def modules():
    return [rel for rel in api_static.MANIFEST if rel.endswith(".js")]


class TestStaticFiles(unittest.TestCase):
    def test_every_static_file_is_in_the_manifest_and_every_entry_exists(self):
        on_disk = sorted(path.relative_to(STATIC).as_posix() for path in STATIC.rglob("*")
                         if path.is_file() and "__pycache__" not in path.parts)
        self.assertEqual(on_disk, sorted(api_static.MANIFEST))

    def test_every_import_resolves_to_a_manifest_file(self):
        for rel in modules():
            text = (STATIC / rel).read_text(encoding="utf-8")
            for spec in IMPORT_RE.findall(text):
                self.assertTrue(spec.startswith("./") or spec.startswith("../"), (rel, spec))
                target = os.path.normpath(os.path.join(os.path.dirname(rel), spec))
                self.assertIn(target.replace(os.sep, "/"), api_static.MANIFEST, (rel, spec))

    def test_no_studio_file_loads_anything_from_another_host(self):
        for rel in api_static.MANIFEST:
            if rel.startswith("vendor/"):
                continue
            text = (STATIC / rel).read_text(encoding="utf-8")
            for needle in ("http://", "https://", "//cdn"):
                self.assertNotIn(needle, text, rel)

    def test_the_vendored_bundle_is_the_pinned_release_and_imports_nothing(self):
        data = (STATIC / "vendor" / "htm-preact.mjs").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), PINNED)
        self.assertEqual(IMPORT_RE.findall(data.decode("ascii")), [])

    def test_both_vendor_licenses_ship(self):
        self.assertIn("Apache License", (STATIC / "vendor" / "LICENSE-htm").read_text(encoding="ascii"))
        self.assertIn("The MIT License",
                      (STATIC / "vendor" / "LICENSE-preact").read_text(encoding="ascii"))

    def test_no_studio_module_writes_markup_from_a_string(self):
        sinks = ("innerHTML", "outerHTML", "dangerouslySetInnerHTML", "insertAdjacentHTML",
                 "document.write", "createContextualFragment")
        for rel in modules():
            if rel.startswith("vendor/"):
                continue
            text = (STATIC / rel).read_text(encoding="utf-8")
            for sink in sinks:
                self.assertNotIn(sink, text, (rel, sink))

    def test_static_files_are_ascii_lf_and_tab_free(self):
        for rel in api_static.MANIFEST:
            data = (STATIC / rel).read_bytes()
            data.decode("ascii")
            self.assertNotIn(b"\r", data, rel)
            self.assertNotIn(b"\t", data, rel)

    @unittest.skipUnless(shutil.which("node"), "node is not installed, the syntax check needs it")
    def test_every_module_parses(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplnode-"))
        for rel in modules():
            copy = scratch / (rel.replace("/", "__") + ".mjs")
            copy.write_bytes((STATIC / rel).read_bytes())
            result = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, (rel, result.stderr))

    def test_the_shell_declares_an_empty_icon_so_the_browser_asks_for_no_favicon(self):
        text = (STATIC / "index.html").read_text(encoding="ascii")
        self.assertIn('<link rel="icon" href="data:,">', text)

    @unittest.skipUnless(shutil.which("node"), "node is not installed, the client check needs it")
    def test_the_client_turns_a_stopped_server_into_a_lost_session(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplnode-"))
        copy = scratch / "api.mjs"
        copy.write_bytes((STATIC / "api.js").read_bytes())
        script = """
import { get, ApiError } from "%s";
const events = [];
globalThis.window = { dispatchEvent: (event) => events.push(event.type) };
globalThis.CustomEvent = class { constructor(type) { this.type = type; } };
const outcome = async (fetchImpl) => {
  events.length = 0;
  globalThis.fetch = fetchImpl;
  try {
    await get("/api/setup");
    return { thrown: false, events: [...events] };
  } catch (e) {
    return { thrown: e instanceof ApiError, status: e.status, code: e.code, events: [...events] };
  }
};
const reply = (status, payload) => async () => ({ status, ok: status < 400, json: async () => payload });
console.log(JSON.stringify({
  offline: await outcome(async () => { throw new TypeError("Failed to fetch"); }),
  session: await outcome(reply(403, { error_code: "SESSION_REQUIRED", message: "no session" })),
  refused: await outcome(reply(400, { error_code: "SCHEMA_INVALID", message: "bad" })),
}));
""" % copy.as_uri()
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        outcomes = json.loads(result.stdout)
        self.assertEqual(outcomes["offline"], {"thrown": True, "status": 0, "code": "OFFLINE",
                                               "events": ["ppl-session-lost"]})
        self.assertEqual(outcomes["session"], {"thrown": True, "status": 403,
                                               "code": "SESSION_REQUIRED",
                                               "events": ["ppl-session-lost"]})
        self.assertEqual(outcomes["refused"], {"thrown": True, "status": 400,
                                               "code": "SCHEMA_INVALID", "events": []})


class TestStaticRoutes(panel_client.StudioTestCase):
    def test_the_shell_is_served_at_the_root(self):
        status, body, headers = panel_client.call("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn(b'src="/static/app.js"', body)

    def test_the_run_list_moved_to_runs(self):
        status, body, _ = panel_client.call("GET", "/runs")
        self.assertEqual(status, 200)
        self.assertEqual(body, server.PAGE.read_bytes())

    def test_a_static_module_is_served_with_its_content_type(self):
        status, body, headers = panel_client.call("GET", "/static/pages/home.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(body, (STATIC / "pages" / "home.js").read_bytes())

    def test_a_name_outside_the_manifest_is_not_served(self):
        for path in ("/static/server.py", "/static/pages/../../server.py", "/static/vendor/x.js"):
            status, payload, _ = panel_client.call("GET", path)
            self.assertEqual(status, 404, path)

    def test_static_files_need_the_session_too(self):
        # rule: R-STUDIO-SESSION-TOKEN
        status, payload, _ = panel_client.call("GET", "/static/app.js", base={"Host": panel_client.HOST})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_an_unlisted_static_name_answers_the_error_envelope(self):
        status, payload, _ = panel_client.call("GET", "/static/server.py")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "NOT_FOUND")
        self.assertIn("message", payload)

    def test_the_shell_and_the_run_list_need_the_session(self):
        # rule: R-STUDIO-SESSION-TOKEN
        for path in ("/", "/runs"):
            with self.subTest(path=path):
                status, payload, _ = panel_client.call("GET", path, base={"Host": panel_client.HOST})
                self.assertEqual(status, 403)
                self.assertEqual(payload["error_code"], "SESSION_REQUIRED")

    def test_a_foreign_host_is_refused_on_the_shell_the_run_list_and_static_files(self):
        # rule: R-STUDIO-HOST-CHECK
        headers = panel_client.session_headers()
        headers["Host"] = "studio.attacker.example:%d" % panel_client.PORT
        for path in ("/", "/runs", "/static/app.js"):
            with self.subTest(path=path):
                status, payload, _ = panel_client.call("GET", path, base=headers)
                self.assertEqual(status, 403)
                self.assertEqual(payload["error_code"], "BOUNDARY_VIOLATION")

    def test_the_shell_the_run_list_and_every_static_file_are_revalidated(self):
        paths = ["/", "/runs"] + ["/static/" + rel for rel in api_static.MANIFEST]
        for path in paths:
            with self.subTest(path=path):
                status, _, headers = panel_client.call("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Cache-Control"], "no-cache")

    def test_a_wrong_case_or_percent_encoded_name_is_not_served(self):
        status, payload, _ = panel_client.call("GET", "/static/App.js")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error_code"], "NOT_FOUND")
        for path in ("/static/pages/%2e%2e%2fserver.py", "/static/ui%2fcontrols.js"):
            with self.subTest(path=path):
                status, payload, _ = panel_client.call("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("error_code", payload)

    def test_a_static_file_takes_no_write(self):
        status, payload, _ = panel_client.call("POST", "/static/app.js", body={})
        self.assertEqual(status, 405)


if __name__ == "__main__":
    unittest.main()

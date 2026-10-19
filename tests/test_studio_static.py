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

    @unittest.skipUnless(shutil.which("node"), "node is not installed, the list field check needs it")
    def test_a_comma_list_field_keeps_what_is_typed_until_blur(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplnode-"))
        copy = scratch / "listfield.mjs"
        copy.write_bytes((STATIC / "ui" / "listfield.js").read_bytes())
        script = """
import { fieldInput, fieldBlur, idleField, shownText } from "%s";
const typed = "tests/unit, tests/integration";
let field = idleField;
let list = [];
const shown = [];
for (const ch of typed) {
  const next = fieldInput(shownText(field, list) + ch);
  field = next;
  list = next.list;
  shown.push(shownText(field, list));
}
const typedList = list;
field = fieldBlur();
const blurred = shownText(field, list);
const cleared = fieldInput("");
console.log(JSON.stringify({ shown, typedList, blurred, cleared, clearedShown: shownText(cleared, cleared.list) }));
""" % copy.as_uri()
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        typed = "tests/unit, tests/integration"
        self.assertEqual(out["shown"], [typed[:i] for i in range(1, len(typed) + 1)])
        self.assertEqual(out["typedList"], ["tests/unit", "tests/integration"])
        self.assertEqual(out["blurred"], typed)
        self.assertEqual(out["cleared"]["list"], [])
        self.assertEqual(out["clearedShown"], "")

    @unittest.skipUnless(shutil.which("node"), "node is not installed, the test path check needs it")
    def test_the_example_test_path_follows_the_first_glob(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplnode-"))
        copy = scratch / "testpath.mjs"
        copy.write_bytes((STATIC / "ui" / "testpath.js").read_bytes())
        script = """
import { exampleTestPath } from "%s";
console.log(JSON.stringify([["tests/**"], ["**/*_test.go"], [], undefined, ["**/*.test.ts", "tests/**"],
  ["**/__tests__/**"], ["src/test/**"], ["**/*Test.java"], ["spec/**/*.rb"]].map(exampleTestPath)));
""" % copy.as_uri()
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ["tests/example", "example_test.go", "src/test/FooTest.java",
                          "src/test/FooTest.java", "example.test.ts", "__tests__/example",
                          "src/test/example", "exampleTest.java", "spec/example.rb"])

    @unittest.skipUnless(shutil.which("node"), "node is not installed, the test sources check needs it")
    def test_emptying_the_test_sources_field_restores_what_the_draft_started_with(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplnode-"))
        copy = scratch / "testpath.mjs"
        copy.write_bytes((STATIC / "ui" / "testpath.js").read_bytes())
        script = """
import { testsAfterEdit } from "%s";
console.log(JSON.stringify([testsAfterEdit(undefined, []), testsAfterEdit(undefined, ["a/**"]) ,
  testsAfterEdit({ sources: [] }, []), testsAfterEdit({ sources: ["x/**"] }, []),
  testsAfterEdit({ sources: [] }, ["t/**"])].map((v) => (v === undefined ? "none" : v))));
""" % copy.as_uri()
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         ["none", {"sources": ["a/**"]}, {"sources": []}, {"sources": []},
                          {"sources": ["t/**"]}])

    def test_the_harness_form_edits_test_sources_as_a_list_field(self):
        source = (STATIC / "ui" / "harness-form.js").read_text()
        self.assertIn("Test sources", source)
        self.assertIn("Java defaults", source)
        block = source[source.index('label="Test sources"'):source.index("<h3>Build gates")]
        self.assertIn("<${ListInput}", block)
        self.assertIn("testsAfterEdit(draft.doc.tests, list)", block)
        self.assertIn("delete d.tests", block)

    def test_the_harness_form_offers_the_tracker_kind(self):
        source = (STATIC / "ui" / "harness-form.js").read_text()
        self.assertIn('from "./trackerkind.js"', source)
        block = source[source.index('label="Tracker"'):source.index("<h3>Test sources")]
        self.assertIn("disabled=${!offers}", block)
        self.assertIn("swap(e.target.value)", block)
        for kind in ("jira", "github-issues"):
            self.assertIn('<option value="%s">' % kind, block)
        for label in ("Repository", "Task prefix", "Jira MCP prefixes"):
            self.assertIn(label, block)
        self.assertIn("<${ListInput}", block[block.index("Jira MCP prefixes"):])
        self.assertIn("swapTracker(doc, parked.current, offers, kind)", source)
        self.assertIn("const offers = draft.trackers", source)

    @unittest.skipUnless(shutil.which("node"), "node is not installed, the tracker check needs it")
    def test_swapping_the_tracker_kind_keeps_the_rest_of_the_form(self):
        scratch = Path(tmphome.mkdtemp(prefix="pplnode-"))
        copy = scratch / "trackerkind.mjs"
        copy.write_bytes((STATIC / "ui" / "trackerkind.js").read_bytes())
        script = """
import { retargetGithub, swapTracker } from "%s";
const offers = {
  jira: { tracker: { kind: "jira", mcp_prefixes: ["mcp__atlassian__"] }, task: { key_pattern: "^J-1$", url_template: "j" } },
  "github-issues": { tracker: { kind: "github-issues", task_prefix: "GH" },
    task: { key_pattern: "^GH-[0-9]+$", url_template: "g/<repo>/issues/<TASK-ID-number>" } },
};
const doc = { tracker: { kind: "jira", mcp_prefixes: ["mcp__edited__"] }, task: { key_pattern: "^EDIT-1$", url_template: "j" },
  tests: { sources: ["mine/**"] }, gate: [{ name: "a", command: "b" }] };
const frozen = JSON.stringify(doc);
const toGithub = swapTracker(doc, {}, offers, "github-issues");
const typed = retargetGithub(toGithub.doc, offers["github-issues"], "o/r", "ISS");
const backToJira = swapTracker(typed, toGithub.parked, offers, "jira");
const again = swapTracker(backToJira.doc, backToJira.parked, offers, "github-issues");
console.log(JSON.stringify({
  untouched: JSON.stringify(doc) === frozen,
  toGithub: toGithub.doc, parkedJira: toGithub.parked.jira,
  typed: typed.tracker, typedTask: typed.task,
  backToJira: backToJira.doc, again: again.doc.tracker, againTask: again.doc.task,
  same: swapTracker(doc, {}, offers, "jira").doc === doc,
  unknown: swapTracker(doc, {}, offers, "linear"), noOffers: swapTracker(doc, {}, undefined, "github-issues"),
  cleared: retargetGithub(typed, offers["github-issues"], "", "GH").tracker,
}));
""" % copy.as_uri()
        result = subprocess.run(["node", "--input-type=module", "-e", script],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        self.assertTrue(out["untouched"])
        self.assertEqual(out["toGithub"]["tests"], {"sources": ["mine/**"]})
        self.assertEqual(out["toGithub"]["gate"], [{"name": "a", "command": "b"}])
        self.assertEqual(out["toGithub"]["tracker"], {"kind": "github-issues", "task_prefix": "GH"})
        self.assertEqual(out["parkedJira"], {"tracker": {"kind": "jira", "mcp_prefixes": ["mcp__edited__"]},
                                             "task": {"key_pattern": "^EDIT-1$", "url_template": "j"}})
        self.assertEqual(out["typed"], {"kind": "github-issues", "task_prefix": "ISS", "repo": "o/r"})
        self.assertEqual(out["typedTask"], {"key_pattern": "^ISS-[0-9]+$",
                                            "url_template": "g/o/r/issues/<TASK-ID-number>"})
        self.assertEqual(out["backToJira"]["tracker"], out["parkedJira"]["tracker"])
        self.assertEqual(out["backToJira"]["task"], out["parkedJira"]["task"])
        self.assertEqual(out["backToJira"]["tests"], {"sources": ["mine/**"]})
        self.assertEqual(out["again"], out["typed"])
        self.assertEqual(out["againTask"], out["typedTask"])
        self.assertTrue(out["same"])
        self.assertIsNone(out["unknown"])
        self.assertIsNone(out["noOffers"])
        self.assertEqual(out["cleared"], {"kind": "github-issues", "task_prefix": "GH"})

    def test_the_playground_example_uses_the_example_test_path(self):
        source = (STATIC / "pages" / "playground.js").read_text()
        self.assertIn('from "../ui/testpath.js"', source)
        self.assertIn("exampleTestPath(", source)
        self.assertNotIn('value: "src/test/FooTest.java"', source)

    def test_the_harness_form_routes_list_inputs_through_the_list_field_rules(self):
        source = (STATIC / "ui" / "harness-form.js").read_text()
        self.assertIn('from "./listfield.js"', source)
        body = source[source.index("function ListInput"):source.index("function withEnv")]
        for call in ("shownText(field, list)", "fieldInput(e.target.value)", "setField(next)",
                     "setField(fieldBlur())", "onList(next.list)"):
            self.assertIn(call, body)
        self.assertIn("<${ListInput}", source)
        self.assertNotIn('.join(", ")', source)

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

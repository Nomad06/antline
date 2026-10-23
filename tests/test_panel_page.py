import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "ppl" / "panel" / "index.html"

FREE_TEXT_FIELDS = ("outcome", "next_safe_action", "needs_you")

NODE = shutil.which("node")


class TestPanelPage(unittest.TestCase):
    def setUp(self):
        self.assertTrue(PAGE.exists(), "panel page is missing")
        self.text = PAGE.read_text(encoding="utf-8")

    # --- brief-verbatim tests ---

    def test_loads_nothing_from_the_network(self):
        self.assertEqual(re.findall(r'src="https?://', self.text), [])
        self.assertEqual(re.findall(r'href="https?://', self.text), [])

    def test_has_all_four_control_buttons(self):
        for label in ("pause", "resume", "stop", "destroy"):
            self.assertIn('data-command="%s"' % label, self.text)

    def test_destroy_asks_for_a_scope(self):
        self.assertIn("local", self.text)
        self.assertIn("full", self.text)
        self.assertIn("confirm", self.text.lower())

    def test_destroy_reports_a_worktree_that_could_not_be_removed(self):
        # run.destroy archives the run either way, so a silent panel made an
        # orphaned worktree look exactly like a clean destroy.
        self.assertIn("worktree_error", self.text)

    def test_sends_the_ppl_header(self):
        self.assertIn('"X-PPL": "1"', self.text)

    def test_is_ascii_only(self):
        self.text.encode("ascii")

    # --- global constraints: no tabs, no CRLF (ascii-only misses both) ---

    def test_has_no_tabs(self):
        self.assertNotIn("\t", self.text)

    def test_has_no_crlf(self):
        self.assertNotIn("\r", self.text)

    # --- gap 2: an error-status row must be handled distinctly, not just
    # fall through the normal renderRun path and print "undefined" for
    # fields (outcome, budgets, mr, ...) that an error row never has ---

    def test_error_status_is_handled_as_a_distinct_branch(self):
        self.assertIn('run.status === "error"', self.text)

    def test_error_branch_returns_before_reaching_budget_line(self):
        # budgetLine(run.budgets) would read undefined properties off the
        # error row's empty {} budgets object and print the literal string
        # "undefined" into the page. Proving the error check appears (and
        # therefore returns) before the budgetLine call inside renderRun
        # shows an error row can never reach it.
        error_check = self.text.index('run.status === "error"')
        budget_call = self.text.index("budgetLine(run.budgets")
        self.assertLess(error_check, budget_call)

    def test_error_row_shows_the_task_id_and_the_reason(self):
        self.assertIn("esc(run.task_id)", self.text)
        self.assertIn("esc(run.needs_you)", self.text)

    # --- gap 3: needs_you must be shown prominently ---

    def test_needs_you_is_rendered_with_the_needs_class(self):
        self.assertIn('"needs"', self.text)
        self.assertIn("NEEDS YOU", self.text)

    # --- gap 1: next_offset is an opaque byte offset. Never recompute it,
    # only round-trip exactly what the server last handed back, and reset
    # to 0 when the user reopens a closed log pane ---

    def test_log_offset_resets_to_zero_on_reopen(self):
        self.assertIn("offset: 0", self.text)

    def test_log_offset_is_only_ever_assigned_from_the_server_response(self):
        # Every ".offset =" assignment in the file must be one of the two
        # legitimate ones: the reopen literal above, or copying the
        # server's next_offset back verbatim. Nothing else may compute it
        # (e.g. from a line count or a running byte total).
        assignments = re.findall(r"\.offset\s*=\s*([^;]+);", self.text)
        for rhs in assignments:
            self.assertTrue(
                rhs.strip() == "0" or "data.next_offset" in rhs,
                "unexpected offset assignment: %r" % rhs)
        self.assertIn("data.next_offset", self.text)

    def test_log_fetch_sends_back_the_stored_offset(self):
        self.assertIn("offset=", self.text)
        self.assertIn(".offset", self.text)

    # --- escaping: outcome, next_safe_action and needs_you are free text
    # from a Jira ticket or an exception and must never be concatenated
    # into the page unescaped, including inside attributes ---

    def test_free_text_fields_are_escaped_where_rendered(self):
        for field in FREE_TEXT_FIELDS:
            self.assertIn("esc(run.%s)" % field, self.text,
                          "run.%s is not wrapped in esc() anywhere" % field)

    def test_free_text_fields_are_never_concatenated_raw(self):
        for field in FREE_TEXT_FIELDS:
            unescaped_after_plus = re.findall(
                r"\+\s*run\.%s\b" % field, self.text)
            for match_text in unescaped_after_plus:
                self.fail("run.%s concatenated without esc(): %r" % (field, match_text))
            unescaped_before_plus = re.findall(
                r"run\.%s\b\s*\+" % field, self.text)
            self.assertEqual(unescaped_before_plus, [],
                             "run.%s concatenated without esc()" % field)

    def test_esc_escapes_quotes_for_safe_attribute_use(self):
        # esc() is used to build attribute values (data-task="...", href="...").
        # The brief's esc() only replaces & < > and leaves a bare " free to
        # break out of an attribute. An error row's task_id in particular is
        # raw folder.name, not validated against the DEMO-12345 shape, so an
        # unescaped quote in it is a real injection path, not a theoretical one.
        self.assertIn('&quot;', self.text)
        self.assertIn('/"/g', self.text)

    # --- "do not stack requests if one is slow" ---

    def test_refresh_guards_against_overlapping_polls(self):
        self.assertIn("refreshing", self.text)

    def test_polls_on_a_modest_interval(self):
        match = re.search(r"setInterval\(refresh,\s*(\d+)\)", self.text)
        self.assertIsNotNone(match, "no setInterval(refresh, ...) found")
        interval = int(match.group(1))
        self.assertGreaterEqual(interval, 2000)
        self.assertLessEqual(interval, 15000)

    # --- review round 1: mr.url reaching an href needs a scheme check, not
    # just esc()'s attribute-breakout protection ---

    def test_mr_url_goes_through_a_scheme_check_before_becoming_a_link(self):
        self.assertIn("safeHref", self.text)
        self.assertIn("https?", self.text)

    # --- review round 1: a dead /api/runs must show visibly, not leave
    # stale rows looking current ---

    def test_refresh_has_a_catch_and_a_stale_notice(self):
        self.assertIn("catch", self.text)
        self.assertIn("last update failed", self.text)
        self.assertIn('id="status"', self.text)

    # --- review round 1 minors ---

    def test_task_ids_are_uri_encoded_in_every_fetch_path(self):
        # An error row's task_id is an unvalidated directory name. A "?" or
        # "#" in it must not be able to mis-route the request.
        self.assertIn("encodeURIComponent", self.text)
        self.assertNotIn('"/api/runs/" + task +', self.text)

    def test_log_open_registers_state_before_the_opening_fetch(self):
        registration = self.text.index("logState[task] = fresh;")
        first_use_in_fetch = self.text.index("fresh.offset")
        self.assertLess(registration, first_use_in_fetch)

    def test_log_text_buffer_is_capped(self):
        self.assertIn("LOG_TEXT_CAP", self.text)
        self.assertIn(".slice(", self.text)

    def test_logstate_is_pruned_when_a_run_flips_to_error(self):
        self.assertIn('statusByTask.get(task) === "error") delete logState[task]', self.text)


@unittest.skipUnless(NODE, "node not available on this machine; static tests above still cover the source")
class TestPanelPageBehavior(unittest.TestCase):
    """
    The tests above grep the HTML/JS source, which cannot see behaviour
    that only exists once the code actually runs (a stray `if (false)`
    would satisfy an ordering check; a value that reaches the DOM through
    an intermediate variable is invisible to a regex over run.<field>).
    These tests instead execute the page's own pure rendering functions
    under Node against fixture JSON - the same fixtures server.py would
    hand the page - and assert on the real output.

    This does not weaken the "no third-party script, offline-only" rule:
    that rule is about what ships to the browser and what ppl needs to
    run, not about what a developer's own machine may use to test a
    browser page with. ppl itself stays Python-stdlib-only.
    """

    @classmethod
    def setUpClass(cls):
        html = PAGE.read_text(encoding="utf-8")
        script_match = re.search(r"<script>(.*)</script>", html, re.DOTALL)
        cls.full_js = script_match.group(1)
        # Keep only the function/const definitions. The DOM-wiring tail
        # (document.addEventListener, the initial refresh() call,
        # setInterval) needs a real document/fetch and is not part of what
        # most of these tests exercise. Slicing it off keeps them runnable
        # under a bare `node` with nothing stubbed. The one test that does
        # need the click handler (test_stale_fetch_does_not_reopen_a_pane_*)
        # uses cls.full_js directly with its own minimal document/fetch
        # stubs instead.
        cls.pure_js, _, _ = cls.full_js.partition("document.addEventListener")

    def _node_eval(self, snippet):
        return self._run_node(self.pure_js + "\n" + snippet)

    def _run_node(self, script):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            path = handle.name
        try:
            result = subprocess.run([NODE, path], capture_output=True, text=True, timeout=10)
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertEqual(result.returncode, 0, "node failed:\n%s" % result.stderr)
        return result.stdout

    def test_esc_produces_the_escaped_forms(self):
        cases = {"quote": '"', "apostrophe": "'", "tag": "<img>", "amp": "&"}
        call = ", ".join("%s: esc(%s)" % (name, json.dumps(value)) for name, value in cases.items())
        out = self._node_eval("console.log(JSON.stringify({%s}));" % call)
        result = json.loads(out)
        self.assertEqual(result["quote"], "&quot;")
        self.assertEqual(result["apostrophe"], "&#39;")
        self.assertEqual(result["tag"], "&lt;img&gt;")
        self.assertEqual(result["amp"], "&amp;")

    @staticmethod
    def _base_run(**overrides):
        run = {
            "task_id": "DEMO-1", "tracker_url": "https://j/x", "stage": "review",
            "status": "running", "risk": "low", "outcome": "ok",
            "next_safe_action": "wait for CI", "candidate_sha": "abc123",
            "budgets": {"dispatch_used": 1, "dispatch_hard": 5, "retry_used": 0,
                        "retry_max": 3, "usd_used": 0.1, "usd_max": 0},
            "mr": None, "control": None, "needs_you": "",
        }
        run.update(overrides)
        return run

    def test_render_run_neutralises_markup_in_free_text_fields(self):
        run = self._base_run(
            outcome="<script>alert(1)</script>",
            next_safe_action='<img src=x onerror=alert(2)>')
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(run))
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", html)

    def test_render_run_shows_the_tokens_line_escaped(self):
        run = self._base_run(tokens="verifier 7 (out 2) <b>", usd="usd 1.2/5.0")
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(run))
        self.assertIn("tokens: verifier 7 (out 2) &lt;b&gt;", html)

    def test_render_run_prints_the_rows_usd_instead_of_the_raw_budget(self):
        run = self._base_run(tokens="none recorded", usd="usd 1.2/5.0")
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(run))
        self.assertIn("retry 0/3, usd 1.2/5.0", html)
        self.assertNotIn("0.1/", html)

    def test_render_run_falls_back_to_the_raw_budget_without_a_usd_string(self):
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(self._base_run()))
        self.assertIn("usd 0.1/unlimited", html)
        self.assertNotIn("undefined", html)

    def test_render_run_refuses_a_javascript_scheme_mr_url(self):
        run = self._base_run(mr={"url": "javascript:alert(1)", "iid": 7})
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(run))
        self.assertNotIn('href="javascript:', html.lower())
        self.assertIn("MR !7", html)

    def test_render_run_links_a_real_https_mr_url(self):
        run = self._base_run(mr={"url": "https://gitlab.example/mr/7", "iid": 7})
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(run))
        self.assertIn('href="https://gitlab.example/mr/7"', html)

    def test_render_error_row_via_the_real_error_row_shape(self):
        from ppl.panel import server
        row = server._error_row('weird"task', ValueError("bad json"))
        html = self._node_eval("console.log(renderRun(%s));" % json.dumps(row))
        self.assertNotIn("undefined", html)
        self.assertIn(row["task_id"].replace('"', "&quot;"), html)
        self.assertIn("bad json", html)

    def test_stale_fetch_does_not_reopen_a_pane_the_operator_closed(self):
        # Round-2 regression test for the race the review found: open a log
        # pane, close it before the opening fetch resolves, then let that
        # stale fetch resolve. This exercises the real, unmodified click
        # handler (document.addEventListener's actual callback) rather than
        # a copy of the guard condition, using a hand-rolled document/fetch
        # stub instead of jsdom (not installed, and installing it would
        # need network access this environment does not have) - enough
        # surface to capture the click listener and simulate two clicks and
        # one controllable fetch, no more.
        harness = r"""
"use strict";
let clickHandler = null;
const statusEl = { hidden: true, textContent: "" };
const runsEl = { className: "", innerHTML: "", querySelectorAll: () => [] };
global.document = {
  getElementById: (id) => (id === "status" ? statusEl : (id === "runs" ? runsEl : null)),
  addEventListener: (type, fn) => { if (type === "click") clickHandler = fn; },
};

const pendingLogFetches = [];
global.fetch = (url) => {
  if (url === "/api/runs") {
    return Promise.resolve({ ok: true, json: async () => [] });
  }
  if (typeof url === "string" && url.indexOf("/log?offset=") !== -1) {
    return new Promise((resolve) => { pendingLogFetches.push(resolve); });
  }
  return Promise.resolve({ ok: true, json: async () => ({}) });
};

""" + self.full_js + r"""

function delay(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function main() {
  // Let the page's own initial refresh() (called at script load, against
  // the /api/runs stub above) fully settle before touching logState.
  await delay(20);

  const pre = { hidden: true, textContent: "" };
  const row = { dataset: { task: "DEMO-1" }, querySelector: (sel) => (sel === "pre.log" ? pre : null) };
  // The real handler starts with event.target.closest("button") to find
  // the clicked button, then calls .closest(".run") again on that same
  // button to find its row - so this one stub object must answer both
  // selectors, matching what a real button element's closest() would do.
  const button = {
    dataset: { command: "log" },
    closest: (sel) => (sel === "button" ? button : (sel === ".run" ? row : null)),
  };

  // Click 1: open. Runs synchronously up to its "await fetch(...)" and
  // suspends there - by the time this call returns, logState.DEMO-1 is
  // already registered (open: true) and one fetch is queued in
  // pendingLogFetches, still unresolved.
  const openPromise = clickHandler({ target: button });
  if (pendingLogFetches.length !== 1) {
    throw new Error("expected exactly one pending log fetch after the open click, got " + pendingLogFetches.length);
  }

  // Click 2: close, before the opening fetch resolves. The close branch
  // has no await before its return, so this applies synchronously.
  clickHandler({ target: button });
  if (pre.hidden !== true) {
    throw new Error("pane should already be hidden right after the close click");
  }

  // Now resolve the stale opening fetch with content an operator who
  // closed the pane should never see appear.
  pendingLogFetches[0]({ json: async () => ({ lines: ["late line, should not appear"], next_offset: 42 }) });
  await openPromise;

  console.log(JSON.stringify({ hidden: pre.hidden, text: pre.textContent }));
}

main().then(() => process.exit(0)).catch((err) => { console.error(err); process.exit(1); });
"""
        out = self._run_node(harness)
        result = json.loads(out)
        self.assertTrue(result["hidden"], "a stale fetch reopened a pane the operator had closed")
        self.assertEqual(result["text"], "", "a stale fetch wrote log content into a closed pane")


if __name__ == "__main__":
    unittest.main()

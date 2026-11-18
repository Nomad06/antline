import io
import itertools
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

# A single home directory shared across this module's tests (like
# tests/test_routing.py), but the environment variable itself is pinned in
# setUp/tearDown rather than left to whichever module happened to set it last
# at import time under `unittest discover`.

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
import panel_client
HOME = tmphome.mkdtemp(prefix="pplhome-")
os.environ["PPL_HOME"] = HOME

from ppl import (control, findings, lanes, paths, run, stall, status as status_mod, store,
                 usage as usage_mod)
from ppl.errors import PplError
from ppl.panel import server, session

COUNTER = itertools.count(950)


def make_repo():
    repo = tmphome.mkdtemp(prefix="pplrepo-")
    subprocess.run(["git", "init", "-q", "-b", "master", repo], check=True)
    Path(repo, "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "base"], check=True)
    return repo


def head_sha(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def _make_handler(method, path, headers=None, body=b""):
    # BaseHTTPRequestHandler normally does its own socket I/O from __init__,
    # so building one bypasses that and wires the pieces do_GET/do_POST
    # actually touch: headers, rfile, wfile, path. This exercises the real
    # Handler class over a real request/response cycle without binding a
    # socket, per the "do not leave a server running" rule.
    base = dict(panel_client.session_headers(), **{"Content-Type": "application/json"})
    return panel_client.make_handler(method, path, headers, body, base=base)


class TestPanelApi(unittest.TestCase):
    def setUp(self):
        self._saved_home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = HOME
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "https://j/x", "/tmp/repo")

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = self._saved_home

    def test_list_runs_includes_the_new_run(self):
        ids = [r["task_id"] for r in server.list_runs()]
        self.assertIn(self.task, ids)

    def test_list_runs_summarises_without_leaking_internals(self):
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        for field in ("stage", "status", "needs_you", "budgets", "mr"):
            self.assertIn(field, row)

    def test_a_run_row_carries_its_tokens_line(self):
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        self.assertEqual(row["tokens"], "none recorded")
        store.append_log(self.task, {"event": "usage", "role": "ppl-verifier", "agent_id": "a1",
                                     "models": {"m": {"input": 5, "cache_write": 0,
                                                      "cache_read": 0, "output": 2}}})
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        self.assertEqual(row["tokens"], "verifier 7 (out 2)")
        self.assertEqual(row["usd"], "usd 0.0 (no cap) + unpriced m, not in config.toml [prices]")

    def test_an_error_row_has_empty_tokens_and_usd(self):
        row = server._error_row("X-1", ValueError("bad"))
        self.assertEqual((row["tokens"], row["usd"]), ("", ""))

    def test_list_runs_carries_the_tracker_url(self):
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        self.assertEqual(row["tracker_url"], "https://j/x")

    def test_detail_includes_the_status_card(self):
        detail = server.run_detail(self.task)
        self.assertIn("Needs you:", detail["card"])

    def test_detail_card_shows_the_tracker_url(self):
        detail = server.run_detail(self.task)
        self.assertIn("Task:     %s  https://j/x" % self.task, detail["card"])

    def test_list_runs_surfaces_the_open_findings_count(self):
        repo = make_repo()
        state = run.load(self.task)
        state["repo"] = repo
        state["base_sha"] = head_sha(repo)
        run.save(state)
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        self.assertEqual(row["open_findings"], 0)
        findings.add(self.task, "blocking", "e", head_sha(repo), "a.txt", owner="team-x")
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        self.assertEqual(row["open_findings"], 1)

    def test_detail_includes_the_open_findings_list(self):
        repo = make_repo()
        state = run.load(self.task)
        state["repo"] = repo
        state["base_sha"] = head_sha(repo)
        run.save(state)
        record = findings.add(self.task, "minor", "e", head_sha(repo), "a.txt", owner="team-x")
        detail = server.run_detail(self.task)
        self.assertEqual([f["finding_id"] for f in detail["findings"]], [record["finding_id"]])

    def test_control_pause_writes_the_inbox(self):
        server.handle_control(self.task, {"command": "pause"})
        self.assertEqual(control.read(self.task)["command"], "pause")

    def test_control_destroy_local_archives_the_run(self):
        result = server.handle_control(self.task, {"command": "destroy", "scope": "local"})
        self.assertEqual(result["result"]["status"], "archived")
        self.assertNotIn(self.task, [r["task_id"] for r in server.list_runs()])

    def test_control_destroy_full_queues_the_remote_half(self):
        result = server.handle_control(self.task, {"command": "destroy", "scope": "full"})
        self.assertEqual(result["result"]["status"], "queued_remote_cleanup")

    def test_the_panel_destroy_with_a_why_reflects(self):
        with mock.patch("ppl.lessons.destroy_with_reflection",
                        return_value={"status": "archived", "reflection": "pending"}) as spy:
            result = server.handle_control(self.task, {"command": "destroy", "scope": "local",
                                                       "why": "spec was wrong"})
        spy.assert_called_once_with(self.task, "spec was wrong")
        self.assertEqual(result["result"]["reflection"], "pending")

    def test_the_panel_destroy_with_a_why_bundles_a_bare_run_after_the_command(self):
        # Unmocked: control.set_command has recorded the destroy before the bundle is built.
        from ppl import lessons
        result = server.handle_control(self.task, {"command": "destroy", "scope": "local",
                                                   "why": " gave up "})["result"]
        self.assertEqual(result["reflection"], "pending")
        bundle = Path(result["archive"]) / "lessons" / "bundle"
        outcome = json.loads((bundle / "outcome.json").read_text(encoding="utf-8"))
        self.assertEqual(outcome["ended"], "destroyed")
        self.assertIsNone(outcome["blocker"])
        self.assertEqual(outcome["why"], "gave up")
        self.assertEqual(lessons.find_bundle(self.task), bundle)
        self.assertTrue(lessons.pending_path(self.task).is_file())
        self.assertEqual(json.loads((Path(result["archive"]) / "control.json")
                                    .read_text(encoding="utf-8"))["command"], "destroy")

    def test_a_failed_bundle_leaves_the_run_halted_and_says_so(self):
        from ppl import lessons
        with mock.patch.object(lessons, "build_bundle",
                               side_effect=PplError("SCHEMA_INVALID", "no bundle")):
            with self.assertRaises(PplError) as caught:
                server.handle_control(self.task, {"command": "destroy", "scope": "local",
                                                  "why": "gave up"})
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        message = caught.exception.message
        self.assertIn("no bundle", message)
        self.assertIn("Nothing was archived", message)
        self.assertIn("destroy without a reason", message)
        self.assertEqual(control.read(self.task)["command"], "destroy")
        self.assertTrue(paths.run_dir(self.task).is_dir())
        result = server.handle_control(self.task, {"command": "destroy", "scope": "local"})
        self.assertEqual(result["result"]["status"], "archived")
        self.assertFalse(paths.run_dir(self.task).exists())

    def test_the_panel_destroy_without_a_why_does_not(self):
        with mock.patch("ppl.lessons.destroy_with_reflection") as spy:
            for why in (None, ""):
                task = "DEMO-%d" % next(COUNTER)
                run.init(task, "https://j/x", "/tmp/r")
                body = {"command": "destroy", "scope": "local"}
                if why is not None:
                    body["why"] = why
                result = server.handle_control(task, body)
                self.assertEqual(result["result"]["status"], "archived")
        spy.assert_not_called()

    def test_the_panel_destroy_refuses_a_why_that_is_not_text(self):
        with self.assertRaises(PplError) as caught:
            server.handle_control(self.task, {"command": "destroy", "scope": "local",
                                              "why": 5})
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertTrue(paths.run_dir(self.task).is_dir())

    def test_the_panel_destroy_with_scope_full_ignores_a_why(self):
        with mock.patch("ppl.lessons.destroy_with_reflection") as spy:
            result = server.handle_control(self.task, {"command": "destroy", "scope": "full",
                                                       "why": "x"})
        spy.assert_not_called()
        self.assertEqual(result["result"]["status"], "queued_remote_cleanup")

    def test_control_destroy_without_scope_is_refused(self):
        # rule: R-DESTROY-SCOPE
        with self.assertRaises(PplError):
            server.handle_control(self.task, {"command": "destroy"})

    def test_control_rejects_an_unknown_command(self):
        with self.assertRaises(PplError):
            server.handle_control(self.task, {"command": "rm -rf"})

    # --- minor: pause/resume must not create an orphan run dir ---

    def test_control_on_an_uninitialised_task_does_not_create_an_orphan_run_dir(self):
        ghost_task = "DEMO-%d" % next(COUNTER)
        with self.assertRaises(PplError):
            server.handle_control(ghost_task, {"command": "pause"})
        self.assertFalse(paths.run_dir(ghost_task).exists())

    def test_signal_refuses_when_start_time_does_not_match(self):
        # rule: R-PANEL-SIGTERM-PID
        state = run.load(self.task)
        state["orchestrator"]["started_at"] = "definitely not the real start time"
        run.save(state)
        with self.assertRaises(PplError) as ctx:
            server.handle_signal(self.task)
        self.assertIn("start time", ctx.exception.message)

    def test_signal_refuses_when_pid_is_gone(self):
        # rule: R-PANEL-SIGTERM-PID
        state = run.load(self.task)
        state["orchestrator"]["pid"] = 999999
        run.save(state)
        with self.assertRaises(PplError):
            server.handle_signal(self.task)

    # --- defect: os.kill in handle_signal was unguarded ---

    def test_signal_wraps_a_process_that_exits_between_the_check_and_the_kill(self):
        # Simulate the race the brief worries about: process_started_at
        # reports a start time that matches (the pid "is" the recorded
        # process as far as the checks are concerned), but the pid does not
        # actually exist by the time os.kill runs, so ProcessLookupError
        # must come back as a PplError, not an unhandled OSError.
        state = run.load(self.task)
        state["orchestrator"]["pid"] = 999999
        run.save(state)
        started_at = state["orchestrator"]["started_at"]
        with mock.patch.object(run, "process_started_at", return_value=started_at):
            with self.assertRaises(PplError) as ctx:
                server.handle_signal(self.task)
        self.assertEqual(ctx.exception.code, "EXTERNAL_BLOCKED")
        self.assertIn("999999", ctx.exception.message)

    def test_post_requires_the_ppl_header(self):
        self.assertFalse(server.origin_ok({"Origin": "https://evil.example"}))
        self.assertTrue(server.origin_ok({"Origin": "http://127.0.0.1:8787"}))
        self.assertTrue(server.origin_ok({}))

    # --- defect: origin_ok was a prefix match, not a host check ---

    def test_origin_ok_rejects_a_lookalike_domain_that_starts_with_127_0_0_1(self):
        self.assertFalse(server.origin_ok({"Origin": "http://127.0.0.1.evil.com"}))

    def test_origin_ok_rejects_a_lookalike_domain_that_starts_with_localhost(self):
        self.assertFalse(server.origin_ok({"Origin": "http://localhost.evil.com"}))

    def test_origin_ok_accepts_a_bare_loopback_origin_with_no_port(self):
        self.assertTrue(server.origin_ok({"Origin": "http://127.0.0.1"}))

    def test_origin_ok_accepts_an_https_loopback_origin(self):
        self.assertTrue(server.origin_ok({"Origin": "https://127.0.0.1:8787"}))

    def test_origin_ok_accepts_ipv6_loopback(self):
        self.assertTrue(server.origin_ok({"Origin": "http://[::1]:8787"}))

    def test_origin_ok_rejects_a_malformed_origin_instead_of_raising(self):
        self.assertFalse(server.origin_ok({"Origin": "http://[::1"}))

    # --- read_log ---

    def test_read_log_returns_lines_from_the_offset(self):
        # next_offset is a byte position (see the windowing fix below), not
        # a line count: the caller just round-trips whatever value it was
        # handed back, so this only asserts on the round trip, not on the
        # literal number.
        first = server.read_log(self.task, 0)
        self.assertGreaterEqual(first["next_offset"], 1)
        store.append_log(self.task, {"event": "custom"})
        second = server.read_log(self.task, first["next_offset"])
        self.assertEqual(len(second["lines"]), 1)
        self.assertGreater(second["next_offset"], first["next_offset"])

    def test_read_log_on_a_run_with_no_log_file_is_empty_not_an_error(self):
        result = server.read_log("DEMO-1", 0)
        self.assertEqual(result, {"lines": [], "next_offset": 0})

    # --- defect: read_log re-read and re-scanned the whole file every call ---

    def test_read_log_tails_a_large_log_without_rereading_it_from_the_start(self):
        # Large enough that re-reading the whole file on every poll would be
        # visibly wasteful for a run that has been going a while. Write
        # directly rather than through store.append_log (which is not the
        # thing under test here) to keep the test itself fast.
        log_path = paths.run_dir(self.task) / "log.jsonl"
        with open(log_path, "a", encoding="utf-8") as handle:
            for i in range(5000):
                handle.write(json.dumps({"event": "filler", "i": i}) + "\n")
        caught_up = server.read_log(self.task, 0)
        self.assertEqual(len(caught_up["lines"]), 5001)  # the "init" line plus 5000 filler lines
        size_after_filler = log_path.stat().st_size
        self.assertEqual(caught_up["next_offset"], size_after_filler)

        # Nothing new since the last poll: a seek straight to EOF, not a
        # rescan of all 5000+ lines to conclude there is nothing new.
        idle = server.read_log(self.task, caught_up["next_offset"])
        self.assertEqual(idle["lines"], [])
        self.assertEqual(idle["next_offset"], size_after_filler)

        # One more line appended: only that one line comes back, proving
        # the offset is honoured as a resume point rather than re-derived
        # by scanning from the start.
        store.append_log(self.task, {"event": "tail-check"})
        tail = server.read_log(self.task, idle["next_offset"])
        self.assertEqual(len(tail["lines"]), 1)
        self.assertIn("tail-check", tail["lines"][0])
        self.assertGreater(tail["next_offset"], idle["next_offset"])

    def test_read_log_does_not_actually_read_bytes_before_the_offset(self):
        # Instrument the read path directly: seek to a real mid-file offset
        # and confirm the bytes handed back start exactly there, which is
        # only true if the implementation seeks rather than reading
        # everything and slicing.
        log_path = paths.run_dir(self.task) / "log.jsonl"
        with open(log_path, "a", encoding="utf-8") as handle:
            for i in range(200):
                handle.write(json.dumps({"event": "filler", "i": i}) + "\n")
        raw = log_path.read_bytes()
        midpoint = len(raw) // 2
        # Land on a line boundary so the slice below is a clean set of lines.
        newline_after_mid = raw.index(b"\n", midpoint) + 1
        expected_tail_lines = raw[newline_after_mid:].decode("utf-8").splitlines()
        result = server.read_log(self.task, newline_after_mid)
        self.assertEqual(result["lines"], expected_tail_lines)

    # --- defect: control.read's invalid marker must surface, not disappear ---

    def test_list_runs_surfaces_an_invalid_control_file(self):
        control_path = paths.run_dir(self.task) / "control.json"
        control_path.write_text("not json", encoding="utf-8")
        row = [r for r in server.list_runs() if r["task_id"] == self.task][0]
        self.assertNotEqual(row["needs_you"], "")
        self.assertIn("invalid", row["needs_you"].lower())

    def test_detail_control_shows_the_invalid_marker(self):
        control_path = paths.run_dir(self.task) / "control.json"
        control_path.write_text("not json", encoding="utf-8")
        detail = server.run_detail(self.task)
        self.assertTrue(detail["control"]["invalid"])

    # --- defect: one corrupt run must not blank the whole panel ---

    def test_list_runs_survives_a_corrupt_state_json_in_another_run(self):
        other_task = "DEMO-%d" % next(COUNTER)
        run.init(other_task, "https://j/y", "/tmp/repo2")
        (paths.run_dir(other_task) / "state.json").write_text("{not valid json", encoding="utf-8")
        ids = [r["task_id"] for r in server.list_runs()]
        self.assertIn(self.task, ids)
        self.assertIn(other_task, ids)

    def test_list_runs_survives_a_state_json_that_is_valid_json_but_not_an_object(self):
        # Valid JSON but not a dict (e.g. a truncated write left "[]"
        # behind): state["task_id"] would raise TypeError, not ValueError,
        # so this is a distinct failure shape from the malformed-JSON case
        # above and needs its own coverage.
        other_task = "DEMO-%d" % next(COUNTER)
        run.init(other_task, "https://j/z", "/tmp/repo3")
        (paths.run_dir(other_task) / "state.json").write_text("[]", encoding="utf-8")
        ids = [r["task_id"] for r in server.list_runs()]
        self.assertIn(self.task, ids)
        self.assertIn(other_task, ids)
        bad_row = [r for r in server.list_runs() if r["task_id"] == other_task][0]
        self.assertEqual(bad_row["status"], "error")

    def test_list_runs_does_not_vanish_a_run_whose_state_json_is_the_literal_null(self):
        # store.read_json returns None both for "file absent" and for a
        # file that decodes to the JSON value null. state_file.is_file()
        # above already rules out the first case, so any None reaching
        # this point is the second. Treating it as "nothing to see" (the
        # original `if state is None: continue`) made the run disappear
        # instead of showing up as broken.
        other_task = "DEMO-%d" % next(COUNTER)
        run.init(other_task, "https://j/null", "/tmp/repo4")
        (paths.run_dir(other_task) / "state.json").write_text("null", encoding="utf-8")
        rows = server.list_runs()
        ids = [r["task_id"] for r in rows]
        self.assertIn(self.task, ids)
        self.assertIn(other_task, ids)
        bad_row = [r for r in rows if r["task_id"] == other_task][0]
        self.assertEqual(bad_row["status"], "error")

    def test_list_runs_flags_a_state_json_that_is_a_bare_json_string(self):
        other_task = "DEMO-%d" % next(COUNTER)
        run.init(other_task, "https://j/str", "/tmp/repo5")
        (paths.run_dir(other_task) / "state.json").write_text('"just a string"', encoding="utf-8")
        rows = server.list_runs()
        ids = [r["task_id"] for r in rows]
        self.assertIn(self.task, ids)
        self.assertIn(other_task, ids)
        bad_row = [r for r in rows if r["task_id"] == other_task][0]
        self.assertEqual(bad_row["status"], "error")

    # --- defect: the archive dir is not a run ---

    def test_list_runs_ignores_archive(self):
        server.handle_control(self.task, {"command": "destroy", "scope": "local"})
        ids = [r["task_id"] for r in server.list_runs()]
        self.assertNotIn("_archive", ids)

    # --- Handler: real cross-origin and missing-header rejection ---

    def test_handler_post_without_ppl_header_is_rejected(self):
        # rule: R-PANEL-LOOPBACK
        handler = _make_handler(
            "POST", "/api/runs/%s/control" % self.task,
            headers={"Origin": "http://127.0.0.1:8787",
                     "Content-Length": "0"})
        handler.do_POST()
        response = handler.wfile.getvalue()
        self.assertIn(b"403", response)
        self.assertIn(b"BOUNDARY_VIOLATION", response)

    def test_handler_post_from_a_foreign_origin_is_rejected(self):
        # rule: R-PANEL-LOOPBACK
        handler = _make_handler(
            "POST", "/api/runs/%s/control" % self.task,
            headers={"X-PPL": "1", "Origin": "https://evil.example",
                     "Content-Length": "0"})
        handler.do_POST()
        response = handler.wfile.getvalue()
        self.assertIn(b"403", response)
        self.assertIn(b"BOUNDARY_VIOLATION", response)

    def test_handler_post_with_valid_header_and_no_origin_succeeds(self):
        payload = json.dumps({"command": "pause"}).encode("utf-8")
        handler = _make_handler(
            "POST", "/api/runs/%s/control" % self.task,
            headers={"X-PPL": "1", "Content-Length": str(len(payload))},
            body=payload)
        handler.do_POST()
        response = handler.wfile.getvalue()
        self.assertIn(b"200", response)
        self.assertEqual(control.read(self.task)["command"], "pause")

    def test_handler_post_with_malformed_json_body_returns_400_not_a_crash(self):
        payload = b"{not json"
        handler = _make_handler(
            "POST", "/api/runs/%s/control" % self.task,
            headers={"X-PPL": "1", "Content-Length": str(len(payload))},
            body=payload)
        handler.do_POST()
        response = handler.wfile.getvalue()
        self.assertIn(b"400", response)
        self.assertIn(b"SCHEMA_INVALID", response)

    def test_handler_get_with_non_numeric_offset_returns_400_not_a_crash(self):
        handler = _make_handler("GET", "/api/runs/%s/log?offset=notanumber" % self.task)
        handler.do_GET()
        response = handler.wfile.getvalue()
        self.assertIn(b"400", response)
        self.assertIn(b"SCHEMA_INVALID", response)

    # --- minor: route matching only checked the trailing segment ---

    def test_handler_post_control_requires_the_api_runs_prefix(self):
        # Same segment count and trailing "control" as the real route, but
        # the wrong first two segments - only checking len(parts) and
        # parts[-1] (the pre-fix code) would have matched this anyway.
        payload = json.dumps({"command": "pause"}).encode("utf-8")
        handler = _make_handler(
            "POST", "/api/notruns/%s/control" % self.task,
            headers={"X-PPL": "1", "Content-Length": str(len(payload))},
            body=payload)
        handler.do_POST()
        response = handler.wfile.getvalue()
        self.assertIn(b"404", response)
        self.assertNotIn(b'"command"', response)

    def test_handler_get_log_requires_the_api_runs_prefix(self):
        handler = _make_handler("GET", "/notapi/runs/%s/log" % self.task)
        handler.do_GET()
        response = handler.wfile.getvalue()
        self.assertIn(b"404", response)

    # --- minor: serve() must validate port rather than let OSError escape ---

    def test_serve_rejects_a_negative_port(self):
        with self.assertRaises(PplError):
            server.serve(port=-1)

    def test_serve_rejects_a_port_out_of_range(self):
        with self.assertRaises(PplError):
            server.serve(port=99999)

    def test_serve_rejects_a_non_integer_port(self):
        with self.assertRaises(PplError):
            server.serve(port="8787")

    def serve_with_a_stub_socket(self, **kwargs):
        """serve() with its listener and the browser stubbed, so it returns.
        Returns the browser stub, the printed line and the session URL."""
        with mock.patch.dict(session._state), \
                mock.patch.object(server, "ThreadingHTTPServer") as listener, \
                mock.patch.object(server.webbrowser, "open") as browser, \
                redirect_stdout(io.StringIO()) as printed:
            server.serve(8799, **kwargs)
            url = session.url()
        listener.assert_called_once_with(("127.0.0.1", 8799), server.Handler)
        listener.return_value.serve_forever.assert_called_once_with()
        return browser, printed.getvalue(), url

    def test_serve_opens_the_browser_on_the_session_url_and_the_fragment(self):
        browser, printed, url = self.serve_with_a_stub_socket(open_browser=True,
                                                              fragment="#/setup")
        browser.assert_called_once_with(url + "#/setup")

    def test_serve_prints_the_link_with_the_fragment_so_a_copied_link_opens_the_wizard(self):
        browser, printed, url = self.serve_with_a_stub_socket(open_browser=True,
                                                              fragment="#/setup")
        link = printed.split()[3]
        self.assertEqual(link, url + "#/setup")
        browser.assert_called_once_with(link)

    def test_serve_prints_the_bare_link_without_a_fragment(self):
        _, printed, url = self.serve_with_a_stub_socket()
        self.assertEqual(printed.split()[3], url)
        self.assertNotIn("#", printed)

    def test_serve_opens_the_bare_session_url_without_a_fragment(self):
        browser, _, url = self.serve_with_a_stub_socket(open_browser=True)
        browser.assert_called_once_with(url)

    def test_ctrl_c_ends_serve_with_one_line_and_closes_the_server(self):
        with mock.patch.dict(session._state), \
                mock.patch.object(server, "ThreadingHTTPServer") as listener, \
                mock.patch.object(server.webbrowser, "open"), \
                redirect_stdout(io.StringIO()) as printed:
            listener.return_value.serve_forever.side_effect = KeyboardInterrupt
            server.serve(8799)
        listener.return_value.server_close.assert_called_once_with()
        self.assertEqual(printed.getvalue().splitlines()[-1], "studio stopped")

    def test_serve_opens_no_browser_unless_asked(self):
        browser, _, _ = self.serve_with_a_stub_socket(fragment="#/setup")
        browser.assert_not_called()

    def test_handler_over_a_real_loopback_socket_serves_api_runs(self):
        # This builds the same ThreadingHTTPServer(("127.0.0.1", ...), Handler)
        # that serve() builds, on an OS-assigned ephemeral port, to prove the
        # Handler works end to end over a real socket without leaving one
        # listening.
        import socket
        import threading
        import time
        from http.server import ThreadingHTTPServer

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        try:
            self.assertEqual(httpd.server_address[0], "127.0.0.1")
            port = httpd.server_address[1]
            token = session.start(port)
            request = ("GET /api/runs HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                       "Cookie: %s=%s\r\n\r\n" % (port, session.COOKIE, token))
            thread = threading.Thread(target=httpd.serve_forever)
            thread.daemon = True
            thread.start()
            time.sleep(0.05)
            with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
                sock.sendall(request.encode("ascii"))
                sock.settimeout(2)
                data = sock.recv(65536)
            self.assertIn(b"200", data)
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestLanesApi(panel_client.StudioTestCase):
    def _lane(self, number, task, stage="verify", pid=None):
        run.init(task, "https://j/x", "/tmp/repo")
        state = run.load(task)
        state["stage"] = stage
        store.write_json(run.state_path(task), state)
        return {"lane": number, "task": task, "harness": "h", "harness_sha": "a" * 40,
                "repo": "/r", "worktree": "/w", "start_dir": "/s", "session_id": "sess-secret",
                "pid": pid, "started": "t"}

    def test_api_lanes_lists_every_lane_with_stage_and_spend(self):
        lanes._write([self._lane(2, "LN-2", "verify"), self._lane(1, "LN-1", "implement")])
        status, payload, _ = panel_client.call("GET", "/api/lanes")
        self.assertEqual(status, 200)
        self.assertEqual([row["lane"] for row in payload["lanes"]], [1, 2])
        first = payload["lanes"][0]
        self.assertEqual((first["task"], first["harness"], first["stage"], first["alive"]),
                         ("LN-1", "h", "implement", False))
        self.assertEqual(first["resume"], "ppl lane resume LN-1")
        self.assertTrue(first["usd"].startswith("usd "))
        self.assertEqual(payload["machine_max"], 3)
        self.assertEqual(payload["day"]["cap"], 0.0)
        self.assertIn("issues", payload["day"])
        self.assertIsNone(payload["error"])

    def test_api_lanes_with_an_unreadable_registry_returns_an_error_row_not_a_500(self):
        lanes.registry_path().parent.mkdir(parents=True, exist_ok=True)
        lanes.registry_path().write_text("not json", encoding="utf-8")
        status, payload, _ = panel_client.call("GET", "/api/lanes")
        self.assertEqual(status, 200)
        self.assertEqual(payload["lanes"], [])
        self.assertIn("not valid JSON", payload["error"])

    def test_api_lanes_leaves_out_the_registrys_internal_fields(self):
        lanes._write([self._lane(1, "LN-1", pid=4242)])
        _, payload, _ = panel_client.call("GET", "/api/lanes")
        self.assertEqual(sorted(payload["lanes"][0]),
                         sorted(["lane", "task", "harness", "stage", "outcome", "alive", "usd",
                                 "resume"]))
        text = json.dumps(payload)
        for secret in ("sess-secret", "4242", "/s", "worktree", "start_dir", "session_id"):
            self.assertNotIn(secret, text)

    def test_api_lanes_returns_a_script_stage_as_plain_data(self):
        lanes._write([self._lane(1, "LN-1", stage="<script>alert(1)</script>")])
        status, payload, headers = panel_client.call("GET", "/api/lanes")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(payload["lanes"][0]["stage"], "<script>alert(1)</script>")

    def test_api_lanes_is_get_only(self):
        status, payload, _ = panel_client.call("POST", "/api/lanes", {})
        self.assertEqual((status, payload["error_code"]), (405, "SCHEMA_INVALID"))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a 000 file")
    def test_api_lanes_with_a_registry_it_cannot_open_returns_the_error_not_a_500(self):
        lanes._write([self._lane(1, "LN-1")])
        os.chmod(lanes.registry_path(), 0)
        self.addCleanup(os.chmod, lanes.registry_path(), 0o600)
        status, payload, _ = panel_client.call("GET", "/api/lanes")
        self.assertEqual(status, 200)
        self.assertEqual(payload["lanes"], [])
        self.assertTrue(payload["error"])

    def test_a_lane_whose_spend_cannot_be_read_says_so_and_the_response_stays_200(self):
        # a failure reads "usd unreadable", never what no spend prints, and any exception the
        # lane's own files raise stays in its row
        lanes._write([self._lane(1, "LN-1"), self._lane(2, "LN-2")])
        real = status_mod.usd_text
        for error in (PplError("SCHEMA_INVALID", "bad state"), RecursionError("deep"),
                      AttributeError("list has no get")):
            def usd_text(task):
                if task == "LN-1":
                    raise error
                return real(task)
            with self.subTest(error=type(error).__name__):
                with mock.patch.object(status_mod, "usd_text", usd_text):
                    code, payload, _ = panel_client.call("GET", "/api/lanes")
                self.assertEqual(code, 200)
                self.assertEqual(payload["lanes"][0]["usd"], "usd unreadable")
                self.assertTrue(payload["lanes"][1]["usd"].startswith("usd "))

    def test_the_day_error_path_keeps_the_lanes_and_the_response_200(self):
        lanes._write([self._lane(1, "LN-1")])
        with mock.patch.object(usage_mod, "spent_today", side_effect=PplError("SCHEMA_INVALID", "bad runs")):
            code, payload, _ = panel_client.call("GET", "/api/lanes")
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["lanes"]), 1)
        self.assertEqual(payload["day"]["error"], "bad runs")
        self.assertIsNone(payload["day"]["usd"])

    def test_the_day_reports_counts_of_uncounted_runs_not_their_names(self):
        lanes._write([self._lane(1, "LN-1")])
        issues = {"blocked": [], "unpriced": [], "unreadable": ["SECRET-1", "SECRET-2"],
                  "unpriced_other": []}
        with mock.patch.object(usage_mod, "spent_today", return_value=(1.5, issues)):
            _, payload, _ = panel_client.call("GET", "/api/lanes")
        self.assertEqual(payload["day"]["issues"]["unreadable"], 2)
        self.assertNotIn("SECRET", json.dumps(payload))


class TestPanelRowOsSandbox(unittest.TestCase):
    def test_the_panel_row_carries_os_sandboxed(self):
        saved = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = HOME
        self.addCleanup(lambda: os.environ.__setitem__("PPL_HOME", saved) if saved
                        else os.environ.pop("PPL_HOME", None))
        task = "DEMO-%d" % next(COUNTER)
        run.init(task, "https://j/x", "/tmp/repo", os_sandboxed=True)
        row = [r for r in server.list_runs() if r["task_id"] == task][0]
        self.assertIs(row["os_sandboxed"], True)
        self.assertIsNone(server._error_row("X-1", ValueError("bad"))["os_sandboxed"])


class TestPanelStall(unittest.TestCase):
    """The panel's run row names a stall, and serve's ticker announces one every minute."""

    def setUp(self):
        saved = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = HOME
        self.addCleanup(lambda: os.environ.__setitem__("PPL_HOME", saved) if saved
                        else os.environ.pop("PPL_HOME", None))
        self.enterContext(mock.patch.object(server, "_last_tick_error", None))
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "https://j/x", "/tmp/repo")

    def stalled(self, minutes=16):
        """The run in stage gates, in progress, its newest event a session stop `minutes` old."""
        state = run.load(self.task)
        state["status"], state["stage"] = "in_progress", "gates"
        run.save(state)
        at = (datetime.now(timezone.utc) - timedelta(minutes=minutes, seconds=5)).isoformat()
        with open(paths.run_dir(self.task) / "log.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "session", "kind": "stop", "at": at}) + "\n")

    def row(self):
        return [r for r in server.list_runs() if r["task_id"] == self.task][0]

    def test_a_stalled_run_says_so_in_needs_you(self):
        self.stalled()
        self.assertEqual(self.row()["needs_you"], "idle 16 min (stopped)")

    def test_a_run_that_has_not_stalled_needs_nothing(self):
        self.stalled(minutes=3)
        self.assertEqual(self.row()["needs_you"], "")

    def test_another_reason_wins_over_the_stall(self):
        self.stalled()
        control.set_command(self.task, "pause", None, by="cli")
        found = [{"task_id": self.task, "stage": "gates", "kind": "stopped",
                  "since": store.now(), "minutes": 16}]
        with mock.patch.object(stall, "scan", return_value=found):
            self.assertEqual(self.row()["needs_you"], "operator pause")

    def test_the_rows_read_every_log_through_one_scan(self):
        self.stalled()
        with mock.patch.object(stall, "scan", wraps=stall.scan) as scan:
            server.list_runs()
        scan.assert_called_once_with()

    def test_a_broken_stall_table_leaves_the_rows_without_a_stall(self):
        self.stalled()
        with mock.patch.object(stall, "scan", side_effect=PplError("SCHEMA_INVALID", "bad")):
            row = self.row()
        self.assertEqual((row["needs_you"], row["stage"]), ("", "gates"))

    def test_the_tick_announces_a_stall_once(self):
        self.stalled()
        # other tests of this class leave stalled runs in the shared home: look at this one
        with mock.patch.object(stall, "notify") as notify:
            first = [s for s in server.stall_tick() if s["task_id"] == self.task]
            second = [s for s in server.stall_tick() if s["task_id"] == self.task]
        self.assertEqual([(s["kind"], s["minutes"]) for s in first], [("stopped", 16)])
        self.assertEqual(second, [])
        self.assertEqual([c.args for c in notify.call_args_list
                          if c.args[0]["task_id"] == self.task], [(first[0],)])
        stalls = [e for e in store.read_log(self.task) if e["event"] == "stall"]
        self.assertEqual(len(stalls), 1)

    def test_the_tick_announces_and_survives_an_error(self):
        err = io.StringIO()
        with mock.patch.object(stall, "scan", side_effect=RuntimeError("boom")), \
                redirect_stderr(err):
            self.assertEqual(server.stall_tick(), [])
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("boom", err.getvalue())

    def test_a_broken_notify_table_is_said_once_and_announces_nothing(self):
        self.stalled()
        err = io.StringIO()
        broken = PplError("SCHEMA_INVALID", "notify.command must be a list")
        with mock.patch.object(server.config, "notify_command", side_effect=broken), \
                mock.patch.object(stall, "notify") as notify, redirect_stderr(err):
            self.assertEqual(server.stall_tick(), [])
            self.assertEqual(server.stall_tick(), [])
        notify.assert_not_called()
        self.assertEqual(err.getvalue().count("\n"), 1)
        self.assertIn("notify.command must be a list", err.getvalue())
        self.assertEqual([e for e in store.read_log(self.task) if e["event"] == "stall"], [])

    def test_a_broken_stall_table_is_said_again_after_a_clean_tick(self):
        err = io.StringIO()
        broken = PplError("SCHEMA_INVALID", "stall.keep_awake must be true or false")
        with mock.patch.object(stall, "notify"), redirect_stderr(err):
            with mock.patch.object(server.config, "stall_settings", side_effect=broken):
                server.stall_tick()
            server.stall_tick()
            with mock.patch.object(server.config, "stall_settings", side_effect=broken):
                server.stall_tick()
        self.assertEqual(err.getvalue().count("keep_awake"), 2)

    def test_serve_starts_no_ticker_at_interval_zero(self):
        for interval in (0, None):
            with mock.patch.dict(session._state), \
                    mock.patch.object(server, "ThreadingHTTPServer"), \
                    mock.patch.object(server.threading, "Thread") as thread, \
                    redirect_stdout(io.StringIO()):
                server.serve(8799, stall_interval=interval)
            thread.assert_not_called()

    def test_serve_starts_one_daemon_ticker_and_stops_it_on_return(self):
        with mock.patch.dict(session._state), \
                mock.patch.object(server, "ThreadingHTTPServer"), \
                mock.patch.object(server.threading, "Thread", wraps=server.threading.Thread) \
                as thread, mock.patch.object(server, "stall_tick") as tick, \
                redirect_stdout(io.StringIO()):
            server.serve(8799, stall_interval=60)
            thread.assert_called_once()
            self.assertTrue(thread.call_args.kwargs["daemon"])
            for item in server.threading.enumerate():
                if item.name == "ppl-stall-ticker":
                    item.join(5)
                    self.assertFalse(item.is_alive())
        tick.assert_not_called()

    def test_the_ticker_ticks_every_interval_until_stopped(self):
        stop = server.threading.Event()
        calls = []

        def tick():
            calls.append(1)
            if len(calls) == 3:
                stop.set()
            return []

        with mock.patch.object(server, "stall_tick", side_effect=tick):
            server._tick_until(stop, 0.01)
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()

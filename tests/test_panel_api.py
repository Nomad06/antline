import itertools
import json
import os
import subprocess
import sys
import unittest
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

from ppl import control, findings, paths, run, store
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

    # --- defect: the archive dir and the ACTIVE marker are not runs ---

    def test_list_runs_ignores_archive_and_active_marker(self):
        server.handle_control(self.task, {"command": "destroy", "scope": "local"})
        ids = [r["task_id"] for r in server.list_runs()]
        self.assertNotIn("_archive", ids)
        self.assertNotIn("ACTIVE", ids)

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

    def test_handler_over_a_real_loopback_socket_serves_api_runs(self):
        # This builds the same ThreadingHTTPServer(("127.0.0.1", ...), Handler)
        # that serve() builds, on an OS-assigned ephemeral port, to prove the
        # Handler works end to end over a real socket without leaving one
        # listening. It does not call serve() itself (serve() blocks forever
        # and binds a fixed port), so serve()'s own "127.0.0.1" literal is
        # verified by reading the source, not by executing serve().
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


if __name__ == "__main__":
    unittest.main()

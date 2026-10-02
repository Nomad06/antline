import json
import os
import signal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import control, findings, paths, run, status, store
from ..errors import PplError

PAGE = Path(__file__).with_name("index.html")


def _needs_you(state, command):
    # control.read can hand back the invalid marker (unparsable JSON, an
    # unknown command, a destroy with no scope) instead of a real record or
    # None. That marker's "command" key is None, so a naive `in HALTING`
    # check quietly reads it as nothing to report, in the one place an
    # operator looks to find out something is wrong. Surface it instead.
    if command and command.get("invalid"):
        return "control file is invalid: %s" % command["reason"]
    if command and command["command"] in control.HALTING:
        return "operator %s" % command["command"]
    if state["status"] == "blocked":
        return "task is blocked"
    if state["stage"] in ("escalation", "parking"):
        return "escalated"
    return ""


def _error_row(task_id, exc):
    return {
        "task_id": task_id,
        "tracker_url": None,
        "outcome": None,
        "stage": "unknown",
        "status": "error",
        "risk": None,
        "budgets": {},
        "mr": None,
        "candidate_sha": None,
        "next_safe_action": None,
        "control": None,
        "open_findings": None,
        "needs_you": "run state is unreadable: %s" % exc,
    }


def list_runs():
    rows = []
    root = paths.runs_dir()
    if not root.is_dir():
        return rows
    for folder in sorted(root.iterdir()):
        # runs/_archive holds destroyed runs one directory deeper, and
        # runs/ACTIVE is a marker file, not a run directory. Skip both
        # explicitly instead of relying only on the state.json probe below.
        if not folder.is_dir() or folder.name == "_archive":
            continue
        state_file = folder / "state.json"
        if not state_file.is_file():
            continue
        try:
            state = store.read_json(state_file)
            # state_file.is_file() above already ruled out genuine absence,
            # for which store.read_json's None default exists. A state.json
            # holding the literal JSON value null decodes to that same
            # None, so treating None here as "nothing to see" would make a
            # broken run vanish instead of showing up as broken - exactly
            # what _error_row below exists to prevent. Route every non-dict
            # result (null, a list, a bare string, ...) there instead.
            if not isinstance(state, dict):
                raise PplError("SCHEMA_INVALID",
                               "state.json for %s is not a JSON object" % folder.name)
            command = control.read(state["task_id"])
            rows.append({
                "task_id": state["task_id"],
                "tracker_url": state.get("tracker_url"),
                "outcome": state.get("last_durable_fact"),
                "stage": state["stage"],
                "status": state["status"],
                "risk": state.get("risk"),
                "budgets": state["budgets"],
                "mr": state.get("mr"),
                "candidate_sha": state.get("candidate_sha"),
                "next_safe_action": state.get("next_safe_action"),
                "control": (command or {}).get("command"),
                "needs_you": _needs_you(state, command),
                # A count only, not the finding bodies themselves - a triage
                # signal for the fleet-wide row. run_detail below carries the
                # full list for the one run an operator opens.
                "open_findings": len(findings.list_open(state["task_id"])),
            })
        except (OSError, ValueError, KeyError, TypeError, PplError) as exc:
            # One run's corrupt state.json must not blank the whole panel:
            # every healthy run still needs to show up, with this one
            # flagged rather than silently dropped. Covers invalid JSON
            # (ValueError), JSON that parsed but is not a dict (TypeError on
            # subscript, KeyError if it is a dict missing task_id), and a
            # task_id that fails paths.check_task_id inside control.read
            # (PplError).
            rows.append(_error_row(folder.name, exc))
    return rows


def run_detail(task_id):
    state = run.load(task_id)
    parking = store.read_json(paths.run_dir(task_id) / "parking.json")
    return {"state": state, "card": status.card(task_id), "parking": parking,
            "control": control.read(task_id), "findings": findings.list_open(task_id)}


def read_log(task_id, offset=0):
    # log.jsonl grows without bound over a multi-day run and the panel
    # polls this repeatedly, so a poll for only the newest lines must not
    # pay for reading the whole file every time. offset/next_offset are
    # therefore byte positions into the file (opaque to the caller, which
    # just round-trips the next_offset it was handed back), not a line
    # count: a seek-then-read-to-EOF costs only the new bytes, where
    # re-reading the whole file and slicing by line index would still pay
    # the full cost on every call as the file grows. Chosen over a
    # capped-read-plus-truncation-flag because the caller here only ever
    # wants "everything since last time," so there is nothing to cap.
    path = paths.run_dir(task_id) / "log.jsonl"
    if not path.is_file():
        return {"lines": [], "next_offset": 0}
    offset = max(0, offset)
    with open(path, "rb") as handle:
        handle.seek(offset)
        chunk = handle.read()
    lines = chunk.decode("utf-8", errors="replace").splitlines()
    return {"lines": lines, "next_offset": offset + len(chunk)}


def handle_control(task_id, body):
    # destroy and stop already fail cleanly on an unknown task_id (they
    # reach run.load through run.destroy / control's own park() call), but
    # pause and resume do not: control.set_command writes control.json
    # unconditionally, which would create an orphan runs/<task_id>/ for a
    # task that was never initialised. Fail the same way for every command.
    run.load(task_id)
    command = body.get("command")
    scope = body.get("scope")
    record = control.set_command(task_id, command, scope, by="panel")
    if command == "destroy":
        record["result"] = run.destroy(task_id, scope)
    return record


def handle_signal(task_id):
    state = run.load(task_id)
    orchestrator = state.get("orchestrator") or {}
    pid = orchestrator.get("pid")
    if not pid:
        raise PplError("SCHEMA_INVALID", "no orchestrator pid recorded for %s" % task_id)
    current = run.process_started_at(pid)
    if not current:
        raise PplError("EXTERNAL_BLOCKED",
                       "process %s is not running, nothing to signal" % pid)
    if current != orchestrator.get("started_at"):
        raise PplError("EXTERNAL_BLOCKED",
                       "process %s start time does not match the recorded one, the pid was "
                       "recycled. Refusing to signal an unrelated process." % pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        # The process can exit in the window between the start-time check
        # above and this call (ProcessLookupError), or be owned by another
        # user (PermissionError). Either is an external fact, not a bug in
        # this request, and must reach the operator as a clean error rather
        # than a dropped connection and a bare traceback.
        raise PplError("EXTERNAL_BLOCKED",
                       "could not signal process %s: %s" % (pid, exc))
    store.append_log(task_id, {"event": "sigterm", "pid": pid})
    return {"signalled": pid}


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def origin_ok(headers):
    origin = headers.get("Origin")
    if not origin:
        return True
    # A prefix match (origin.startswith("http://127.0.0.1")) also accepts
    # http://127.0.0.1.evil.com and http://localhost.evil.com, domains
    # anyone can register - the very thing this check exists to stop.
    # Parse the Origin and compare the hostname for exact equality instead.
    try:
        hostname = urlparse(origin).hostname
    except ValueError:
        return False
    return hostname in _LOOPBACK_HOSTS


class Handler(BaseHTTPRequestHandler):
    server_version = "ppl-panel"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, payload, content_type="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if not parts:
                return self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            if parts == ["api", "runs"]:
                return self._send(200, list_runs())
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "runs":
                return self._send(200, run_detail(parts[2]))
            if (len(parts) == 4 and parts[0] == "api" and parts[1] == "runs"
                    and parts[3] == "log"):
                offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
                return self._send(200, read_log(parts[2], offset))
            self._send(404, {"error_code": "SCHEMA_INVALID", "message": "no such route"})
        except PplError as err:
            self._send(400, err.as_dict())
        except ValueError:
            # e.g. ?offset=notanumber. Brief-verbatim code left this outside
            # any handler, so a malformed query string dropped the
            # connection with a bare traceback instead of a 400.
            self._send(400, {"error_code": "SCHEMA_INVALID",
                             "message": "offset must be an integer"})

    def do_POST(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if self.headers.get("X-PPL") != "1" or not origin_ok(self.headers):
            return self._send(403, {"error_code": "BOUNDARY_VIOLATION",
                                    "message": "panel requests must come from the panel"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise PplError("SCHEMA_INVALID", "request body must be a JSON object")
            if (len(parts) == 4 and parts[0] == "api" and parts[1] == "runs"
                    and parts[3] == "control"):
                return self._send(200, handle_control(parts[2], body))
            if (len(parts) == 4 and parts[0] == "api" and parts[1] == "runs"
                    and parts[3] == "signal"):
                return self._send(200, handle_signal(parts[2]))
            self._send(404, {"error_code": "SCHEMA_INVALID", "message": "no such route"})
        except PplError as err:
            self._send(400, err.as_dict())
        except ValueError as exc:
            # Malformed JSON body or a non-numeric Content-Length. Brief-
            # verbatim code read these before the try block, so they dropped
            # the connection with a bare traceback instead of a 400.
            self._send(400, {"error_code": "SCHEMA_INVALID",
                             "message": "malformed request: %s" % exc})


def serve(port=8787):
    if isinstance(port, bool) or not isinstance(port, int) or not (0 < port < 65536):
        raise PplError("SCHEMA_INVALID",
                       "port must be an integer between 1 and 65535, got: %r" % (port,))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("ppl panel on http://127.0.0.1:%d  (loopback only, ctrl-c to stop)" % port)
    httpd.serve_forever()

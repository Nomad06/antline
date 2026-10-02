"""Drive the panel's Handler in-process, the way tests/test_panel_api.py
always has, with the session cookie, Host and JSON headers a browser on the
studio sends. No socket is bound and no server is left running."""
import http.client
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

from ppl import models
from ppl.panel import server, session

PORT = 8787
HOST = "127.0.0.1:%d" % PORT


def ensure_session():
    if session.token() is None or session.port() != PORT:
        session.start(PORT)
    return session.token()


def session_headers():
    ensure_session()
    return {"Cookie": "%s=%s" % (session.COOKIE, session.token()), "Host": HOST}


def write_headers():
    headers = session_headers()
    headers.update({"Content-Type": "application/json", "X-PPL": "1"})
    return headers


def _message(*layers):
    """The headers as the http.client.HTTPMessage serve() hands the Handler,
    so a lookup ignores case the way it does in production. A later layer
    replaces a header an earlier one set, whatever its case."""
    message = http.client.HTTPMessage()
    for layer in layers:
        for name, value in layer.items():
            del message[name]
            message[name] = value
    return message


def make_handler(method, path, headers=None, body=b"", base=None):
    merged = _message(session_headers() if base is None else base, headers or {})
    if body and "Content-Length" not in merged:
        merged["Content-Length"] = str(len(body))
    handler = server.Handler.__new__(server.Handler)
    handler.headers = merged
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.path = path
    handler.command = method
    handler.request_version = "HTTP/1.1"
    handler.client_address = ("127.0.0.1", 12345)
    handler.requestline = "%s %s HTTP/1.1" % (method, path)
    handler.close_connection = True
    return handler


def parse(handler):
    raw = handler.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ", 2)[1])
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()
    if headers.get("Content-Type", "").startswith("application/json") and body:
        return status, json.loads(body), headers
    return status, body, headers


def call(method, path, body=None, headers=None, base=None):
    data = b"" if body is None else json.dumps(body).encode("utf-8")
    if base is None:
        base = session_headers() if method == "GET" else write_headers()
    handler = make_handler(method, path, headers, data, base)
    getattr(handler, "do_" + method)()
    return parse(handler)


class StudioTestCase(unittest.TestCase):
    """A fresh HOME, data root, Claude home and agents/skills dirs per test,
    the session started and the Claude version pinned. The agents and skills
    dirs point at temp dirs because apply falls back to ~/.claude/agents and
    ~/.claude/skills when no workspace is configured: a test that needs the
    workspace-derived dirs pops the two variables itself."""

    _ENV = ("HOME", "PPL_HOME", "PPL_CLAUDE_HOME", "PPL_AGENTS_DIR", "PPL_SKILLS_DIR")

    def setUp(self):
        self._saved_env = {name: os.environ.get(name) for name in self._ENV}
        os.environ["HOME"] = tmphome.mkdtemp(prefix="pplfakehome-")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        os.environ["PPL_CLAUDE_HOME"] = tmphome.mkdtemp(prefix="pplclaudehome-")
        self.agents_dir = Path(tmphome.mkdtemp(prefix="pplagents-"))
        self.skills_dir = Path(tmphome.mkdtemp(prefix="pplskills-"))
        os.environ["PPL_AGENTS_DIR"] = str(self.agents_dir)
        os.environ["PPL_SKILLS_DIR"] = str(self.skills_dir)
        ensure_session()
        patcher = mock.patch.object(models, "claude_version", lambda binary="claude": "9.9.9")
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

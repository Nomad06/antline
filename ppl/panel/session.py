"""The studio's session: one random token per `ppl serve` start, carried in
an HttpOnly cookie, plus the Host and body checks every request passes. The
token is the only thing that tells the studio's own pages from any other
page in the same browser, so every route asks for it."""
import secrets
from http import cookies

COOKIE = "ppl_session"
_state = {"token": None, "port": None}


def start(port):
    _state["token"] = secrets.token_urlsafe(32)
    _state["port"] = port
    return _state["token"]


def token():
    return _state["token"]


def port():
    return _state["port"]


def url():
    return "http://127.0.0.1:%d/?t=%s" % (_state["port"], _state["token"])


def matches(given):
    expected = _state["token"]
    return bool(expected) and isinstance(given, str) and secrets.compare_digest(given, expected)


def _cookie_token(headers):
    jar = cookies.SimpleCookie()
    try:
        jar.load(headers.get("Cookie") or "")
    except cookies.CookieError:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


def has_session(headers):
    return matches(_cookie_token(headers))


def host_ok(headers):
    """A missing Host is no browser's request and is let through, a present
    one must name loopback on the serving port, which a DNS-rebinding page
    on its own hostname cannot do. Before any start there is no serving
    port, so nothing passes."""
    if _state["port"] is None:
        return False
    host = headers.get("Host")
    if host is None:
        return True
    return host in ("127.0.0.1:%d" % _state["port"], "localhost:%d" % _state["port"])


def json_body(headers):
    value = (headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    return value == "application/json"


def cookie_header():
    return "%s=%s; Path=/; HttpOnly; SameSite=Strict" % (COOKIE, _state["token"])

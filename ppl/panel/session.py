"""The studio's session: one random token per `ppl serve` start, carried in
an HttpOnly cookie, plus the Host and body checks every request passes. The
token is the only thing that tells the studio's own pages from any other
page in the same browser, so every route asks for it."""
import secrets

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
    """compare_digest raises on a non-ASCII str, and a token is always ASCII,
    so anything else is refused before the comparison."""
    expected = _state["token"]
    return (bool(expected) and isinstance(given, str) and given.isascii()
            and secrets.compare_digest(given, expected))


def _cookie_values(headers):
    """Every value the Cookie header carries under the session name. Cookies
    are not kept apart by port, so the header also holds whatever other apps
    on 127.0.0.1 set: a malformed one of theirs must not hide this one, which
    SimpleCookie does by dropping the whole header."""
    values = []
    for piece in (headers.get("Cookie") or "").split(";"):
        name, sep, value = piece.partition("=")
        if not sep or name.strip() != COOKIE:
            continue
        values.append(value.strip())
    return values


def has_session(headers):
    return any(matches(value) for value in _cookie_values(headers))


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

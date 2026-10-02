"""The route table for the studio's /api routes. A pattern is a path whose
segments are literal words or <name> placeholders, matched segment by
segment. A placeholder binds one segment of name characters only, never a
`/`, `.` or `..`, so a route can never be handed a path."""

_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz"
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class Raw:
    """A route's answer that is not JSON: the bytes, their content type and
    any extra response headers."""

    def __init__(self, body, content_type, headers=None):
        if not isinstance(body, bytes):
            raise TypeError("a Raw body is bytes, got %s" % type(body).__name__)
        self.body = body
        self.content_type = content_type
        self.headers = dict(headers or {})


class Router:
    def __init__(self):
        self._routes = []

    def add(self, method, pattern, fn):
        parts = tuple(part for part in pattern.split("/") if part)
        self._routes.append((method, parts, fn))

    def match(self, method, path):
        """(fn, params) for the route, (False, None) when the path exists under
        another method only, (None, None) when no route has this path."""
        segments = [segment for segment in path.split("/") if segment]
        other_method = False
        for route_method, parts, fn in self._routes:
            params = _bind(parts, segments)
            if params is None:
                continue
            if route_method == method:
                return fn, params
            other_method = True
        return (False, None) if other_method else (None, None)


def _bind(parts, segments):
    if len(parts) != len(segments):
        return None
    params = {}
    for part, segment in zip(parts, segments):
        if part.startswith("<") and part.endswith(">"):
            if segment in (".", "..") or not set(segment) <= _NAME_CHARS:
                return None
            params[part[1:-1]] = segment
        elif part != segment:
            return None
    return params

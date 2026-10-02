"""A small TOML writer for the documents ppl itself writes: config.toml and
harness.toml. Supports strings, ints, floats, booleans, lists of scalars,
lists of lists of scalars, tables (nested dicts) and arrays of tables. A dict
inside a list is written as an inline table. Keys starting with "_" at the
top level are left out (a loaded harness carries its directory as "_dir").
The output is ASCII only."""
import re

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\t": "\\t", "\n": "\\n",
                  "\f": "\\f", "\r": "\\r"}


def _string(value):
    out = []
    for char in value:
        code = ord(char)
        if char in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[char])
        elif code < 0x20 or code == 0x7f or 0x7f < code <= 0xffff:
            out.append("\\u%04x" % code)
        elif code > 0xffff:
            out.append("\\U%08x" % code)
        else:
            out.append(char)
    return '"%s"' % "".join(out)


def _key(key):
    if not isinstance(key, str):
        raise TypeError("cannot write a %s key as TOML" % type(key).__name__)
    return key if _BARE_KEY_RE.match(key) else _string(key)


def _scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join("%s = %s" % (_key(k), _scalar(v)) for k, v in value.items()) + "}"
    raise TypeError("cannot write %r as TOML" % (value,))


def _is_table_array(value):
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def _table(path, doc, out):
    """Write the table at `path` (a list of keys, empty for the root): its
    plain keys under its header, then its sub-tables, then its arrays of
    tables. The header is written even for an empty table, or the table
    would not survive a round trip."""
    name = ".".join(_key(k) for k in path)
    scalars = [(k, v) for k, v in doc.items() if not isinstance(v, dict) and not _is_table_array(v)]
    if path:
        out.append("[%s]" % name)
    for key, value in scalars:
        out.append("%s = %s" % (_key(key), _scalar(value)))
    if path or scalars:
        out.append("")
    for key, value in doc.items():
        if isinstance(value, dict):
            _table(path + [key], value, out)
    for key, value in doc.items():
        if _is_table_array(value):
            for item in value:
                out.append("[[%s]]" % ".".join(_key(k) for k in path + [key]))
                for k, v in item.items():
                    out.append("%s = %s" % (_key(k), _scalar(v)))
                out.append("")


def dumps(doc):
    out = []
    _table([], {k: v for k, v in doc.items() if not k.startswith("_")}, out)
    return "\n".join(out).rstrip() + "\n"

"""The harness memory file: memory.toml, the lessons a harness has learned.

Each entry is one owner-accepted lesson for one role, `when <cue>: <lesson>`,
with the evidence it came from. The file holds clean text only: lesson text
is data, never instructions, so everything that reaches a prompt passes
through clean(), and parse() refuses a file whose text differs from its own
cleaned form. This module imports no routing: the roles a file may name are
passed in.
"""
import hashlib
import re
import stat
import tomllib
import unicodedata
from pathlib import Path

from . import tomlout
from .errors import PplError

FILE = "memory.toml"
FORMAT = 1
MAX_ACTIVE = 12
MAX_TEXT = 300
STATUSES = ("active", "retired")
KEYS = ("id", "role", "cue", "lesson", "sources", "accepted_at", "seen", "status")
ID_RE = re.compile(r"M-[1-9][0-9]*")

_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_FENCE_RE = re.compile(r"`{3,}")
_HEADING_RE = re.compile(r"[ \t]*#+")
_WHEN_RE = re.compile(r"\Awhen\s+", re.IGNORECASE)


def _once(text):
    text = _COMMENT_RE.sub(" ", text)
    text = text.replace("<!-- include:", " ")
    text = _FENCE_RE.sub(" ", text)
    # headings go line by line, before the line breaks are collapsed
    text = "\n".join(_HEADING_RE.sub(" ", line, count=1) if _HEADING_RE.match(line) else line
                    for line in text.splitlines())
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in text)
    return " ".join(text.split())


def clean(text):
    """Plain one-line text: no HTML comment (an unclosed one runs to the end),
    include marker, fence of three backticks or more, or leading `#` run, and
    all whitespace, newlines and control characters included, one space. Each
    removal can expose another (a comment split around a marker), so it
    repeats until the text stops changing."""
    text = str(text)
    while True:
        out = _once(text)
        if out == text:
            return out
        text = out


def clean_cue(text):
    """clean(), then without one leading "when ": the render writes "When <cue>"."""
    return clean(_WHEN_RE.sub("", clean(text), count=1))


def normalize_cue(text):
    """The form two cues are compared in: case and trailing punctuation ignored."""
    return clean_cue(text).lower().rstrip(".,;:!? ")


def _fail(source, message):
    raise PplError("HARNESS_INVALID", "%s: %s" % (source, message))


def _check_text(source, where, entry, key, cleaner):
    value = entry[key]
    if not isinstance(value, str):
        _fail(source, "%s.%s must be a string" % (where, key))
    if cleaner(value) != value:
        _fail(source, "%s.%s is not clean text (no headings, comments, include markers, "
              "fences, line breaks or repeated spaces%s): %r"
              % (where, key, ", no leading 'when '" if key == "cue" else "", value[:60]))
    if not value:
        _fail(source, "%s.%s is empty" % (where, key))
    if len(value) > MAX_TEXT:
        _fail(source, "%s.%s is %d characters, at most %d" % (where, key, len(value), MAX_TEXT))


def _check_entry(source, index, entry, roles):
    where = "entry[%d]" % index
    if not isinstance(entry, dict):
        _fail(source, "%s must be a table" % where)
    for key in entry:
        if key not in KEYS:
            _fail(source, "%s has unknown key %s, the keys are %s" % (where, key, ", ".join(KEYS)))
    for key in KEYS:
        if key not in entry:
            _fail(source, "%s is missing %s" % (where, key))
    ident = entry["id"]
    if not isinstance(ident, str) or not ID_RE.fullmatch(ident):
        _fail(source, "%s.id %r is not of the form M-<number> with no leading zero" % (where, ident))
    where = "entry %s" % ident
    role = entry["role"]
    if not isinstance(role, str) or role not in roles:
        _fail(source, "%s.role %r is not one of %s" % (where, role, ", ".join(roles)))
    _check_text(source, where, entry, "cue", clean_cue)
    _check_text(source, where, entry, "lesson", clean)
    sources = entry["sources"]
    if (not isinstance(sources, list) or not sources
            or not all(isinstance(s, str) and s for s in sources)):
        _fail(source, "%s.sources must be a non-empty list of non-empty strings" % where)
    if not isinstance(entry["accepted_at"], str):
        _fail(source, "%s.accepted_at must be a string" % where)
    seen = entry["seen"]
    if type(seen) is not int or seen < 1:
        _fail(source, "%s.seen must be an integer of at least 1" % where)
    if entry["status"] not in STATUSES:
        _fail(source, "%s.status %r is not one of %s" % (where, entry["status"], ", ".join(STATUSES)))


def parse(text, source, roles):
    """The entries of a memory.toml text, each held to the file's rules, or a
    HARNESS_INVALID naming `source`. An empty text is no memory."""
    if isinstance(text, (bytes, bytearray)):
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as exc:
            _fail(source, "not valid UTF-8: %s" % exc)
    if text == "":
        return []
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        _fail(source, "not valid TOML: %s" % exc)
    for key in doc:
        if key not in ("format", "entry"):
            _fail(source, "unknown top-level key %s, the file holds format and [[entry]]" % key)
    if "format" not in doc or type(doc["format"]) is not int:
        _fail(source, "format = %d is required" % FORMAT)
    if doc["format"] != FORMAT:
        _fail(source, "format %s is not supported, this ppl reads format %d"
              % (doc["format"], FORMAT))
    entries = doc.get("entry", [])
    if not isinstance(entries, list):
        _fail(source, "entry must be an array of tables, write [[entry]]")
    seen_ids = set()
    counts = {}
    for index, entry in enumerate(entries):
        _check_entry(source, index, entry, roles)
        if entry["id"] in seen_ids:
            _fail(source, "id %s is used twice" % entry["id"])
        seen_ids.add(entry["id"])
        if entry["status"] == "active":
            counts[entry["role"]] = counts.get(entry["role"], 0) + 1
            if counts[entry["role"]] > MAX_ACTIVE:
                _fail(source, "role %s has more than %d active entries: retire one"
                      % (entry["role"], MAX_ACTIVE))
    return [{key: entry[key] for key in KEYS} for entry in entries]


def read(harness_dir, roles):
    """(entries, raw bytes) of the harness's memory.toml, ([], b"") when it has none."""
    path = Path(harness_dir) / FILE
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [], b""
    except OSError as exc:
        _fail(str(path), "cannot be read: %s" % exc)
    if not stat.S_ISREG(info.st_mode):
        _fail(str(path), "is not a regular file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        _fail(str(path), "cannot be read: %s" % exc)
    return parse(data, str(path), roles), data


def digest(data):
    return hashlib.sha256(data).hexdigest()


def active(entries, role=None):
    return [e for e in entries
            if e["status"] == "active" and (role is None or e["role"] == role)]


def dumps(entries):
    return tomlout.dumps({"format": FORMAT,
                          "entry": [{key: e[key] for key in KEYS} for e in entries]})


def next_id(entries):
    numbers = [int(e["id"][2:]) for e in entries]
    return "M-%d" % (max(numbers) + 1 if numbers else 1)

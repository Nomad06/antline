"""The rule matrix: one row per control rule, loaded and validated.

`rules.toml` is the register of every rule this pipeline claims to hold -
what the rule says, where it is checked, which test proves it denies, and,
when nothing checks it, what the residual actually is. `tests/
test_rule_matrix.py` reads the table through this module and fails the suite
when a row claims more than the code delivers or when DESIGN.md claims
something the table does not carry.

Three enforcement values, and the distance between them is the whole point:

  blocking  code refuses the action, and a named test proves the refusal
  soft      code notices or warns, but the action still goes through
  n/a       nothing in this pipeline enforces it

An `n/a` row must say which kind of "not applicable" it is, because "there is
nothing here to build" and "this is needed and was never built" read
identically otherwise - and that is exactly how a real hole passes for a
normal exception. `GAP:` is the second one and is counted. `PROPERTY:` is a
stated fact with nothing for this code to enforce.

The table is found under `paths.root()` rather than `paths.home()`: it ships
with the code it describes, and the suite redirects `PPL_HOME` to a temporary
directory, where the table it needs to read would not be.
"""
import re
import tomllib
from pathlib import Path

from . import paths
from .errors import PplError

FIELDS = ("id", "statement", "scope", "event", "enforcement", "hook_or_check",
          "deny_test", "allow_test", "owner", "residual")

REQUIRED_TEXT = ("id", "statement", "scope", "event", "enforcement", "owner")

ENFORCEMENT = ("blocking", "soft", "n/a")

RESIDUAL_KINDS = ("GAP:", "PROPERTY:")

ID_RE = re.compile(r"^R-[A-Z0-9]+(?:-[A-Z0-9]+)*$")

# How a document sentence points at a row: a trailing marker inside the
# sentence, before its final period. Chosen over matching statement text
# because it survives rewording, and over a separate index section because an
# index is a second document that drifts from the first.
#
# A citation of an `n/a` row must say so in the marker itself - `[R-X n/a]`.
# Without that, a sentence claiming something is enforced could cite a row
# that admits it is not built, and read as covered to everyone but the person
# who opens the table. It also makes a later downgrade visible in the diff of
# the document, not only in the diff of the table.
MARKER_RE = re.compile(r"\[(R-[A-Z0-9-]+)( n/a)?\]")

NA_DISCLOSURE = " n/a"

# The words that make a sentence a claim about what the code does. A sentence
# in DESIGN.md carrying any of these must name the row it rests on.
# round 2 (review item 4, cycle 2 wave C): the six role-write rows the
# whole-branch review falsified all phrase themselves with a word from this
# second line, none of which the first line covers - "only ... may" stands
# for the two-part "only X may Y" construction, matched specially by the
# scanner in tests/test_rule_matrix.py rather than as a literal substring.
KEYWORDS = ("enforced", "enforces", "denies", "denied", "refuses", "refused",
            "rejected", "cannot", "impossible", "must not",
            "may not", "may only", "only ... may", "never", "prevents",
            "stops", "binds", "ensures", "guarantee")

NO_CHECK = "none"


def default_path():
    return paths.root() / "rules.toml"


def load(path=None):
    path = Path(path) if path else default_path()
    try:
        with open(path, "rb") as handle:
            doc = tomllib.load(handle)
    except FileNotFoundError:
        raise PplError("SCHEMA_INVALID", "no rule matrix at %s" % path)
    except OSError as exc:
        raise PplError("SCHEMA_INVALID", "rule matrix at %s could not be read: %s" % (path, exc))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PplError("SCHEMA_INVALID",
                       "rule matrix at %s is not valid TOML: %s" % (path, exc))
    return validate(doc, path)


def _fail(what, message):
    raise PplError("SCHEMA_INVALID", "%s: %s" % (what, message))


def _check_row(entry, what):
    for field in FIELDS:
        if field not in entry:
            _fail(what, "is missing field %r" % field)
        if not isinstance(entry[field], str):
            _fail(what, "field %r must be a string" % field)
    for field in REQUIRED_TEXT:
        if not entry[field].strip():
            _fail(what, "field %r must not be empty" % field)
    if not ID_RE.match(entry["id"]):
        _fail(what, "id %r must look like R-SOME-RULE" % entry["id"])
    if entry["enforcement"] not in ENFORCEMENT:
        _fail(what, "enforcement %r must be one of %s"
              % (entry["enforcement"], ", ".join(ENFORCEMENT)))


def _check_enforcement(entry, what):
    enforcement = entry["enforcement"]
    if enforcement in ("blocking", "soft") and not entry["hook_or_check"].strip():
        _fail(what, "a %s rule must name its hook_or_check" % enforcement)
    if enforcement == "blocking" and not entry["deny_test"].strip():
        _fail(what, "a blocking rule must name a deny_test that proves the refusal")
    if enforcement != "n/a":
        return
    residual = entry["residual"].strip()
    if not residual:
        _fail(what, "an n/a rule must carry a residual: 'not applicable' and 'needed "
                    "but not built' must not look alike")
    if not residual.startswith(RESIDUAL_KINDS):
        _fail(what, "an n/a residual must start with %s so a real hole cannot read as a "
                    "normal exception" % " or ".join(RESIDUAL_KINDS))
    if entry["hook_or_check"].strip() not in ("", NO_CHECK):
        _fail(what, "an n/a rule must leave hook_or_check as %r: naming a check makes an "
                    "unenforced rule read as covered" % NO_CHECK)
    if entry["deny_test"].strip():
        _fail(what, "an n/a rule must not name a deny_test: there is no refusal to prove")


def validate(doc, source="rules.toml"):
    entries = doc.get("rule") if isinstance(doc, dict) else None
    if not isinstance(entries, list) or not entries:
        raise PplError("SCHEMA_INVALID",
                       "rule matrix %s must carry at least one [[rule]] entry" % source)
    seen = set()
    out = []
    for index, entry in enumerate(entries):
        what = "rule matrix %s entry %d" % (source, index + 1)
        if not isinstance(entry, dict):
            _fail(what, "must be a table")
        _check_row(entry, what)
        what = "rule %s" % entry["id"]
        if entry["id"] in seen:
            _fail(what, "is declared twice")
        seen.add(entry["id"])
        _check_enforcement(entry, what)
        out.append(dict(entry))
    return out


def by_id(entries):
    return {entry["id"]: entry for entry in entries}


def gaps(entries):
    """The rows that are missing enforcement, as opposed to not needing any."""
    return [e for e in entries
            if e["enforcement"] == "n/a" and e["residual"].strip().startswith("GAP:")]

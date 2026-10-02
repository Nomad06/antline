"""A harness draft on disk. The wizard and the studio write the review
form's document and fragments into ~/.ppl/studio/drafts/<name>/, so
harness.load, validate, render_skill and the setup harness step read a draft
exactly the way they read an installed harness."""
import re
import shlex
import shutil
import tempfile
import threading
import uuid
from pathlib import Path

from .. import harness, paths, routing, tomlout
from ..errors import PplError
from . import stacks, wizard

FRAGMENTS = (("skill/gates.md", "skill/baseline.md", "skill/extra-gates.md",
              "skill/tracker-close.md")
             + tuple("templates/%s.part" % role for role in routing.REQUIRED_ROLES))
INTERPRETERS = stacks.INTERPRETERS
# Endings that git and setup keep for their own directories: a source ending in
# .git is cloned, and setup removes <name>.replacing and <name>.replaced. Compared
# in lower case because a case-insensitive filesystem makes Demo.Replacing the
# same directory as demo.replacing.
_RESERVED_SUFFIXES = (".git", ".replacing", ".replaced")
# One check at a time: every check of a name rewrites the same draft directory,
# so two overlapping ones would trip over each other and a check could read back
# another request's document. Install works on a private copy and never takes it.
_CHECK_LOCK = threading.Lock()
# A free_args class that matches every probe takes any token. The probes stay
# short and the plain word comes first: a catastrophic regex a user typed stalls
# the whole server (re holds the GIL), and all() stops at the first refusal.
_PROBES = ("x", ";", "$(id)", "/etc/passwd", "a" * 12)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def drafts_dir():
    return paths.home() / "studio" / "drafts"


def check_name(name):
    if not isinstance(name, str) or not harness._NAME_RE.fullmatch(name):
        raise PplError("SCHEMA_INVALID", "harness name %r must be letters, digits, '.', '_' or "
                       "'-', starting with a letter or digit" % (name,))
    if name.lower().endswith(_RESERVED_SUFFIXES):
        raise PplError("SCHEMA_INVALID", "harness name %r must not end in %s: git and setup use "
                       "those endings for their own directories"
                       % (name, ", ".join(_RESERVED_SUFFIXES)))


def _without_nulls(value):
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value if item is not None]
    return value


def _prepare(name, doc, fragments):
    """The TOML text and the fragment bytes of a draft, every input checked
    before anything touches the disk."""
    check_name(name)
    if not isinstance(doc, dict) or not isinstance(fragments, dict):
        raise PplError("SCHEMA_INVALID", "a draft is a document object and a fragments object")
    unknown = sorted(rel for rel in fragments if rel not in FRAGMENTS)
    if unknown:
        raise PplError("SCHEMA_INVALID", "unknown fragment %s (fragments: %s)"
                       % (", ".join(unknown), ", ".join(FRAGMENTS)))
    text = toml_text(name, doc)
    bodies = {}
    for rel, fragment in fragments.items():
        if not isinstance(fragment, str):
            raise PplError("SCHEMA_INVALID", "fragment %s must be text" % rel)
        if fragment.strip():
            body = fragment if fragment.endswith("\n") else fragment + "\n"
            try:
                bodies[rel] = body.encode("utf-8")
            except UnicodeEncodeError:
                raise PplError("SCHEMA_INVALID", "fragment %s is not valid UTF-8 text" % rel)
    return text, bodies


def document(name, doc):
    """The draft document as it is written: private keys and nulls dropped,
    harness.name set to the name it is saved under."""
    if not isinstance(doc, dict):
        raise PplError("SCHEMA_INVALID", "a draft is a document object and a fragments object")
    clean = _without_nulls({key: value for key, value in doc.items() if not str(key).startswith("_")})
    if isinstance(clean.get("harness"), dict):
        clean["harness"] = dict(clean["harness"], name=name)
    return clean


def toml_text(name, doc):
    try:
        return tomlout.dumps(document(name, doc))
    except (TypeError, ValueError) as exc:
        raise PplError("SCHEMA_INVALID", "the draft cannot be written as TOML: %s" % exc)


def write(name, doc, fragments, root=None):
    """The draft as <root>/<name>/, root being drafts_dir() unless given. It
    is built in a sibling directory and renamed into place, so a refused or
    failed write leaves the previous draft as it was."""
    text, bodies = _prepare(name, doc, fragments)
    root = Path(root) if root is not None else drafts_dir()
    root.mkdir(parents=True, exist_ok=True)
    staging = root / (".writing-" + uuid.uuid4().hex)
    try:
        (staging / "skill").mkdir(parents=True)
        (staging / "templates").mkdir()
        (staging / "harness.toml").write_text(text, encoding="utf-8")
        for rel, body in bodies.items():
            (staging / rel).write_bytes(body)
        target = root / name
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target


def flags(doc):
    """The command shapes the review form marks red: free_args that takes any
    token, a shell or interpreter as argv0 with no required flag pinning what
    it runs, or a shape with neither a goal nor a required flag."""
    found = []
    for index, row in enumerate(harness.shape_rows(doc, "draft")):
        reasons = []
        free = row["free_args"]
        if free and all(re.fullmatch(free, probe) is not None for probe in _PROBES):
            reasons.append("free_args %s accepts any token, so this shape lets any argument "
                           "through" % free)
        if stacks.is_interpreter(row["argv0"]) and not row["required_flags"]:
            reasons.append("%s runs whatever it is given: pin what it may run with a required "
                           "flag" % row["argv0"])
        if not row["required_flags"] and not row["one_goal_of"]:
            reasons.append("no goal and no required flag: any %s line passes" % row["argv0"])
        if reasons:
            found.append({"shape": index, "argv0": row["argv0"], "reasons": reasons})
    return found


def _refused(exc):
    return {"ok": False, "errors": [exc.message], "warnings": [], "flags": []}


def _require_absolute_repo(loaded):
    path = loaded["repo"]["path"]
    try:
        absolute = Path(path).expanduser().is_absolute()
    except RuntimeError:
        absolute = False
    if not absolute:
        raise PplError("HARNESS_INVALID", "repo.path %r must be an absolute path or start with "
                       "~/: setup would resolve a relative one against the working directory "
                       "of ppl serve" % path)


def _assignments_and_program(command):
    """(count of leading NAME=value words, program) of a command split as the
    guard splits one. A command shlex cannot split gives (None, its first word)."""
    try:
        words = shlex.split(command)
    except ValueError:
        words = None
    if words is None:
        raw = command.split()
        return None, raw[0] if raw else ""
    count = 0
    while count < len(words) and _ASSIGNMENT_RE.match(words[count]):
        count += 1
    return count, words[count] if count < len(words) else ""


def _unrunnable_focused_tests(loaded):
    """A warning when the test writer's shell would refuse the focused-tests
    command: the guard takes leading NAME=value words only for a shape with
    env_prefix on, and the program must be the argv0 of a shape."""
    rows = harness.shape_rows(loaded, "draft")
    for gate in loaded.get("gate") or []:
        if gate["name"] == harness.FOCUSED_TESTS_GATE:
            count, program = _assignments_and_program(gate["command"])
            if not program:
                return []
            named = [row for row in rows if row["argv0"] == program]
            if count is not None and any(row["env_prefix"] or not count for row in named):
                return []
            text = ("the test writer cannot run the focused-tests command: no command shape "
                    "starts with %s" % program)
            if count and named:
                text += " that takes an environment prefix"
            return [text]
    return []


def verify(target, name):
    """The check of a harness directory on disk, a draft or a copy of an
    installed harness: loaded back, validated, its skill rendered and every
    role's tools composed. A problem comes back as ok False with the
    message, never a raise."""
    try:
        loaded = harness.load(str(target))
        warnings = harness.validate(loaded, name)
        warnings.extend(_unrunnable_focused_tests(loaded))
        _require_absolute_repo(loaded)
        harness.render_skill(loaded)
        for role in routing.REQUIRED_ROLES:
            routing.role_tools(role, loaded)
    except PplError as exc:
        return _refused(exc)
    except UnicodeDecodeError as exc:
        return _refused(PplError("HARNESS_INVALID", "a harness file is not UTF-8 text: %s" % exc))
    return {"ok": True, "errors": [], "warnings": warnings, "flags": flags(loaded)}


def checked_copy(name, doc, fragments, root):
    """(path, result) of a draft written to <root>/<name>/ and verified, or
    (None, a refusal) when it cannot be written."""
    try:
        target = write(name, doc, fragments, root)
    except PplError as exc:
        return None, _refused(exc)
    return target, verify(target, name)


def check(name, doc, fragments):
    """Validate a draft the way ppl will use it: written to its draft directory,
    then loaded back, validated, its skill rendered and every role's tools
    composed. A draft problem comes back as ok False with the message, never a
    raise."""
    with _CHECK_LOCK:
        return checked_copy(name, doc, fragments, drafts_dir())[1]


def check_private(name, doc, fragments):
    """The checks of `check` on a scratch copy that is removed after, so a
    draft of the same name that the wizard keeps in drafts_dir() is never
    rewritten, and two checks never meet."""
    drafts_dir().mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=drafts_dir(), prefix=".check-"))
    try:
        return checked_copy(name, doc, fragments, scratch)[1]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def install(name, doc, fragments, replace, emit):
    """Install a checked draft through setup's harness step, the step `ppl setup
    harness --answer harness_from=<draft dir>` runs. The step reads a private
    copy of the draft written for this call alone, so a check of the same name
    from another request cannot change what was checked and what is installed."""
    check_name(name)
    drafts_dir().mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(dir=drafts_dir(), prefix=".install-"))
    try:
        target, result = checked_copy(name, doc, fragments, private)
        if not result["ok"]:
            raise PplError("HARNESS_INVALID", "; ".join(result["errors"]))
        loaded = harness.load(str(target))
        answers = {"harness_from": str(target), "harness_name": name,
                   "repo": loaded["repo"]["path"],
                   "default_branch": loaded["repo"]["default_branch"],
                   "task_url_template": loaded["task"]["url_template"],
                   "key_pattern": loaded["task"]["key_pattern"], "forge": loaded["forge"]["kind"]}
        if loaded["forge"]["kind"] == "gitlab":
            answers["gitlab_mcp_server"] = loaded["forge"]["mcp_server"]
        if replace:
            answers["replace_harness"] = "yes"
        return wizard.run_step("harness", answers, emit)
    finally:
        shutil.rmtree(private, ignore_errors=True)

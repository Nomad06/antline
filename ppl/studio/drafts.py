"""A harness draft on disk. A check or an install writes the review form's
document and fragments into a private directory under ~/.ppl/studio/drafts/,
removed after, so harness.load, validate, render_skill and the setup harness
step read a draft exactly the way they read an installed harness, and two
requests never share a directory."""
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .. import harness, paths, tomlout
from ..errors import PplError
from . import stacks, wizard

FRAGMENTS = harness.FRAGMENTS
INTERPRETERS = stacks.INTERPRETERS
# A free_args class that matches every probe takes any token. The probes stay
# short and the plain word comes first: a catastrophic regex a user typed stalls
# the whole server (re holds the GIL), and all() stops at the first refusal.
_PROBES = ("x", ";", "$(id)", "/etc/passwd", "a" * 12)


def drafts_dir():
    return paths.home() / "studio" / "drafts"


def check_name(name):
    harness.check_name(name)


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


def verify(target, name):
    """The check of a harness directory on disk, a draft or a copy of an
    installed harness: harness.verify, the one `ppl harness validate` runs,
    plus the flags of its shapes. A problem comes back as ok False with the
    message, never a raise."""
    result = harness.verify(target, name)
    if result["errors"]:
        return dict(result, ok=False, flags=[])
    return dict(result, ok=True, flags=flags(harness.load(str(target))))


def checked_copy(name, doc, fragments, root):
    """(path, result) of a draft written to <root>/<name>/ and verified, or
    (None, a refusal) when it cannot be written."""
    try:
        target = write(name, doc, fragments, root)
    except PplError as exc:
        return None, _refused(exc)
    return target, verify(target, name)


def check(name, doc, fragments):
    """Validate a draft the way ppl will use it: written to a scratch directory
    of its own, then loaded back, validated, its skill rendered and every
    role's tools composed, and the directory removed. A draft problem comes
    back as ok False with the message, never a raise."""
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
    # absolute, because setup's harness step refuses a relative source
    private = Path(os.path.abspath(tempfile.mkdtemp(dir=drafts_dir(), prefix=".install-")))
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
        answers["tracker"] = loaded["tracker"]["kind"]
        if loaded["tracker"]["kind"] == "github-issues":
            answers["tracker_repo"] = loaded["tracker"]["repo"]
        if replace:
            answers["replace_harness"] = "yes"
        return wizard.run_step("harness", answers, emit)
    finally:
        shutil.rmtree(private, ignore_errors=True)

"""A harness draft on disk. The wizard and the studio write the review
form's document and fragments into ~/.ppl/studio/drafts/<name>/, so
harness.load, validate, render_skill and the setup harness step read a draft
exactly the way they read an installed harness."""
import re
import shutil
import tempfile
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
# .git is cloned, and setup removes <name>.replacing and <name>.replaced.
_RESERVED_SUFFIXES = (".git", ".replacing", ".replaced")
# A free_args class that matches every probe takes any token. The probes stay
# short and the plain word comes first: a catastrophic regex a user typed stalls
# the whole server (re holds the GIL), and all() stops at the first refusal.
_PROBES = ("x", ";", "$(id)", "/etc/passwd", "a" * 12)


def drafts_dir():
    return paths.home() / "studio" / "drafts"


def check_name(name):
    if not isinstance(name, str) or not harness._NAME_RE.fullmatch(name):
        raise PplError("SCHEMA_INVALID", "harness name %r must be letters, digits, '.', '_' or "
                       "'-', starting with a letter or digit" % (name,))
    if name.endswith(_RESERVED_SUFFIXES):
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
    clean = _without_nulls({key: value for key, value in doc.items() if not str(key).startswith("_")})
    if isinstance(clean.get("harness"), dict):
        clean["harness"] = dict(clean["harness"], name=name)
    try:
        text = tomlout.dumps(clean)
    except (TypeError, ValueError) as exc:
        raise PplError("SCHEMA_INVALID", "the draft cannot be written as TOML: %s" % exc)
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


def _verify(target, name):
    """The check of a draft already written to `target`: loaded back,
    validated, its skill rendered and every role's tools composed."""
    try:
        loaded = harness.load(str(target))
        warnings = harness.validate(loaded, name)
        _require_absolute_repo(loaded)
        harness.render_skill(loaded)
        for role in routing.REQUIRED_ROLES:
            routing.role_tools(role, loaded)
    except PplError as exc:
        return _refused(exc)
    return {"ok": True, "errors": [], "warnings": warnings, "flags": flags(loaded)}


def _checked(name, doc, fragments, root):
    try:
        target = write(name, doc, fragments, root)
    except PplError as exc:
        return None, _refused(exc)
    return target, _verify(target, name)


def check(name, doc, fragments):
    """Validate a draft the way ppl will use it: written to its draft directory,
    then loaded back, validated, its skill rendered and every role's tools
    composed. A draft problem comes back as ok False with the message, never a
    raise."""
    return _checked(name, doc, fragments, drafts_dir())[1]


def install(name, doc, fragments, replace, emit):
    """Install a checked draft through setup's harness step, the step `ppl setup
    harness --answer harness_from=<draft dir>` runs. The step reads a private
    copy of the draft written for this call alone, so a check of the same name
    from another request cannot change what was checked and what is installed."""
    check_name(name)
    drafts_dir().mkdir(parents=True, exist_ok=True)
    private = Path(tempfile.mkdtemp(dir=drafts_dir(), prefix=".install-"))
    try:
        target, result = _checked(name, doc, fragments, private)
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

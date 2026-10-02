"""A harness draft on disk. The wizard and the studio write the review
form's document and fragments into ~/.ppl/studio/drafts/<name>/, so
harness.load, validate, render_skill and the setup harness step read a draft
exactly the way they read an installed harness."""
import re
import shutil

from .. import harness, paths, routing, tomlout
from ..errors import PplError
from . import stacks, wizard

FRAGMENTS = (("skill/gates.md", "skill/baseline.md", "skill/extra-gates.md",
              "skill/tracker-close.md")
             + tuple("templates/%s.part" % role for role in routing.REQUIRED_ROLES))
INTERPRETERS = stacks.INTERPRETERS
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Short on purpose: a catastrophic regex a user typed must not hold a server
# thread, and "" plus twelve letters is enough to tell `.*` from a word class.
_PROBES = ("", "a" * 12)


def drafts_dir():
    return paths.home() / "studio" / "drafts"


def check_name(name):
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PplError("SCHEMA_INVALID", "harness name %r must be letters, digits, '.', '_' or "
                       "'-', starting with a letter or digit" % (name,))


def _without_nulls(value):
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_without_nulls(item) for item in value if item is not None]
    return value


def write(name, doc, fragments):
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
    target = drafts_dir() / name
    shutil.rmtree(target, ignore_errors=True)
    (target / "skill").mkdir(parents=True)
    (target / "templates").mkdir()
    (target / "harness.toml").write_text(text, encoding="utf-8")
    for rel, fragment in fragments.items():
        if not isinstance(fragment, str):
            raise PplError("SCHEMA_INVALID", "fragment %s must be text" % rel)
        if fragment.strip():
            body = fragment if fragment.endswith("\n") else fragment + "\n"
            (target / rel).write_text(body, encoding="utf-8")
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


def check(name, doc, fragments):
    """Validate a draft the way ppl will use it: written to its draft directory,
    loaded back, validated, its skill rendered and every role's tools composed.
    A draft problem comes back as ok False with the message, never a raise."""
    try:
        target = write(name, doc, fragments)
        loaded = harness.load(str(target))
        warnings = harness.validate(loaded, name)
        harness.render_skill(loaded)
        for role in routing.REQUIRED_ROLES:
            routing.role_tools(role, loaded)
    except PplError as exc:
        return {"ok": False, "errors": [exc.message], "warnings": [], "flags": []}
    return {"ok": True, "errors": [], "warnings": warnings, "flags": flags(loaded)}


def install(name, doc, fragments, replace, emit):
    """Install a checked draft through setup's harness step, the step `ppl setup
    harness --answer harness_from=<draft dir>` runs."""
    result = check(name, doc, fragments)
    if not result["ok"]:
        raise PplError("HARNESS_INVALID", "; ".join(result["errors"]))
    target = drafts_dir() / name
    loaded = harness.load(str(target))
    answers = {"harness_from": str(target), "harness_name": name,
               "repo": loaded["repo"]["path"], "default_branch": loaded["repo"]["default_branch"],
               "task_url_template": loaded["task"]["url_template"],
               "key_pattern": loaded["task"]["key_pattern"], "forge": loaded["forge"]["kind"]}
    if loaded["forge"]["kind"] == "gitlab":
        answers["gitlab_mcp_server"] = loaded["forge"]["mcp_server"]
    if replace:
        answers["replace_harness"] = "yes"
    return wizard.run_step("harness", answers, emit)

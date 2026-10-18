"""A harness is a directory outside the repository that holds everything
project-specific: build gates, guard shapes, task key shape, tracker and forge
selection, knowledge skills, role prompt overlays. ppl reads it at `ppl init`
and `ppl harness apply`, never at hook time: `ppl init --harness` freezes the
slice the guard enforces into runs/<task>/harness.json (see snapshot) and the
hook reads that file."""
import copy
import difflib
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from pathlib import Path

from . import forges, paths, routing, trackers
from .errors import PplError
from .trackers import is_mcp_prefix

FORMAT = 1
FORGES = ("gitlab", "github")
TRACKERS = ("jira",)
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Endings that git and setup keep for their own directories: a source ending in
# .git is cloned, and setup stages a harness in <name>.replacing and retires the
# installed one to <name>.replaced, removing both. Compared in lower case because
# a case-insensitive filesystem makes Demo.Replacing the same directory as
# demo.replacing.
RESERVED_SUFFIXES = (".git", ".replacing", ".replaced")
_MCP_TOOL_RE = re.compile(r"mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+")
_BARE_TOOL_RE = re.compile(r"[A-Za-z0-9_.-]+")
# A gate name becomes a file name in the rendered recipe and the evidence
# check name, an [env] key the left side of an assignment in a shell line.
_GATE_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# verdict.REQUIRED_CHECKS names it, the done gate wants a pass receipt for it.
FOCUSED_TESTS_GATE = "focused-tests"
TEST_CLASS_PLACEHOLDER = "<test_class>"
# What the orchestrator fills in a gate command at run time: the focused test's
# name and the placeholders of the rendered recipe (_RC_IDIOM, _EVIDENCE and the
# `git -C <worktree>` line). A project placeholder such as example-maven's
# <module> is accepted when skill/gates.md or skill/extra-gates.md names it,
# since those fragments are where the orchestrator reads what to put there.
RUN_TIME_PLACEHOLDERS = (TEST_CLASS_PLACEHOLDER, "<worktree>", "<TASK-ID>", "<SHA>",
                         "<GENERATION>")
_PLACEHOLDER_RE = re.compile(r"<[A-Za-z_][A-Za-z0-9_-]*>")
_PLACEHOLDER_FRAGMENTS = ("skill/gates.md", "skill/extra-gates.md")
SKILL_FRAGMENTS = ("skill/gates.md", "skill/baseline.md", "skill/extra-gates.md",
                   "skill/tracker-close.md")
FRAGMENTS = SKILL_FRAGMENTS + tuple("templates/%s.part" % role for role in routing.REQUIRED_ROLES)
# Every table and key harness.toml may hold, listed once: validate refuses any
# other and names the nearest of these. [env] (None) takes any shell variable
# name, and a key starting with _ is ppl's own (load adds _dir).
KNOWN_KEYS = {
    "harness": ("name", "format"),
    "repo": ("path", "default_branch", "worktrees"),
    "task": ("key_pattern", "url_template"),
    "tracker": ("kind", "mcp_prefixes", "write_operations", "read_tools"),
    "forge": ("kind", "mcp_server"),
    "knowledge": ("skills", "extra_tools"),
    "env": None,
    "gate": ("name", "command"),
    "guard": ("shape",),
    "guard.shape": ("argv0", "env_prefix", "required_flags", "one_goal_of", "free_args"),
}
# The rendered SKILL.md is always in the orchestrator's context and an agent file
# is a role's whole prompt. Measured 2026-10-18 on the two shipped examples: the
# largest SKILL.md is example-maven's, the largest agent file ppl-verifier's.
# Over the soft cap is a warning, over the hard cap a refusal.
SKILL_MEASURED_BYTES = 38114
AGENT_MEASURED_BYTES = 5313
SKILL_SOFT_BYTES = SKILL_MEASURED_BYTES * 3 // 2
SKILL_HARD_BYTES = SKILL_MEASURED_BYTES * 3
AGENT_SOFT_BYTES = AGENT_MEASURED_BYTES * 3 // 2
AGENT_HARD_BYTES = AGENT_MEASURED_BYTES * 3
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_CLONE_TIMEOUT_SECONDS = 120
_KIND_WORDS = {str: "a non-empty string", int: "an integer", list: "a list"}


def _fail(source, message):
    raise PplError("HARNESS_INVALID", "harness %s: %s" % (source, message))


def _text(value):
    return isinstance(value, str) and bool(value)


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _absolute(path):
    """Absolute and normalised but symlinks kept, so a caller that passed a path
    gets the same path back (on macOS a tmp dir sits behind /var -> /private/var)."""
    return Path(os.path.abspath(path))


def check_name(name):
    """Refuse SCHEMA_INVALID unless name is a plain directory name, the whole
    string and no trailing newline, that does not end in a reserved ending."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise PplError("SCHEMA_INVALID", "harness name %r must be letters, digits, '.', '_' or "
                       "'-', starting with a letter or digit" % (name,))
    if name.lower().endswith(RESERVED_SUFFIXES):
        raise PplError("SCHEMA_INVALID", "harness name %r must not end in %s: git and setup use "
                       "those endings for their own directories"
                       % (name, ", ".join(RESERVED_SUFFIXES)))


def resolve_dir(name_or_path):
    candidate = Path(name_or_path).expanduser()
    if candidate.is_dir() and (candidate / "harness.toml").is_file():
        return _absolute(candidate)
    if _NAME_RE.match(str(name_or_path)) and (paths.harness_dir(name_or_path) / "harness.toml").is_file():
        return _absolute(paths.harness_dir(name_or_path))
    _fail(name_or_path, "no harness.toml under %s or %s"
          % (candidate, paths.harness_dir(str(name_or_path))))


def _require(doc, table, key, source, kind=str, allow_empty=False):
    section = doc.get(table)
    if not isinstance(section, dict):
        _fail(source, "missing [%s] table" % table)
    if key not in section:
        _fail(source, "%s.%s is required" % (table, key))
    value = section[key]
    wrong = not isinstance(value, kind) or (kind is int and isinstance(value, bool))
    if wrong or (kind is str and not value and not allow_empty):
        _fail(source, "%s.%s must be %s" % (table, key, _KIND_WORDS[kind]))
    if kind is list and not _strings(value):
        _fail(source, "%s.%s must hold only strings" % (table, key))
    return value


def _check_granted_tools(doc, knowledge, source):
    """The briefer is composed from tracker.read_tools and knowledge.extra_tools,
    and its tools line is a comma-joined list: an entry must be exactly one tool
    name, so one string cannot smuggle a second tool, a newline or a key past
    the write-tool check. A harness grants MCP tools only, never a built-in
    one, and never a tracker write tool."""
    tracker = doc["tracker"]
    writes = set(tracker["write_operations"])
    for index, tool in enumerate(tracker["read_tools"]):
        if not _BARE_TOOL_RE.fullmatch(tool):
            _fail(source, "tracker.read_tools[%d] %r is not a bare tool name (letters, digits, "
                  "'_', '.' and '-' only)" % (index, tool))
        if tool in writes:
            _fail(source, "tracker.read_tools[%d] %r is also a tracker.write_operations entry, "
                  "no role may hold a tracker write tool" % (index, tool))
    pattern = trackers.get(tracker["kind"]).write_tool_regex(tracker["mcp_prefixes"], list(writes))
    for index, tool in enumerate(knowledge.get("extra_tools", [])):
        if not _MCP_TOOL_RE.fullmatch(tool):
            _fail(source, "knowledge.extra_tools[%d] %r is not an MCP tool name "
                  "(mcp__<server>__<tool>), a harness cannot grant a built-in tool or a list"
                  % (index, tool))
        if pattern.match(tool):
            _fail(source, "knowledge.extra_tools[%d] %r is a tracker write tool, "
                  "no role may hold one" % (index, tool))


def _nearest(word, known):
    return difflib.get_close_matches(word, list(known), n=1, cutoff=0.0)[0]


def _check_row_keys(row, where, known, source):
    for key in row:
        if key not in known:
            _fail(source, "unknown key %s%s, the nearest known key is %s%s"
                  % (where, key, where, _nearest(key, known)))


def _check_known_keys(doc, source):
    """Refuse a table or key ppl never reads: a typo such as [[gates]] or
    [[guard.shapes]] would otherwise drop what it holds without a word."""
    tables = [name for name in KNOWN_KEYS if "." not in name]
    for key, value in doc.items():
        if key.startswith("_") or key in tables:
            continue
        what = "table [%s]" % key if isinstance(value, (dict, list)) else "key %s" % key
        _fail(source, "unknown %s, the nearest known table is [%s]" % (what, _nearest(key, tables)))
    for table in tables:
        value = doc.get(table)
        if KNOWN_KEYS[table] is None:
            continue
        if isinstance(value, dict):
            _check_row_keys(value, "%s." % table, KNOWN_KEYS[table], source)
        elif table == "gate" and isinstance(value, list):
            for index, row in enumerate(value):
                if isinstance(row, dict):
                    _check_row_keys(row, "gate[%d]." % index, KNOWN_KEYS["gate"], source)
    shapes = (doc.get("guard") or {}).get("shape") if isinstance(doc.get("guard"), dict) else None
    for index, row in enumerate(shapes if isinstance(shapes, list) else []):
        if isinstance(row, dict):
            _check_row_keys(row, "guard.shape[%d]." % index, KNOWN_KEYS["guard.shape"], source)


def _fragment_text(doc, rel):
    path = Path(doc["_dir"]) / rel
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _check_gate_commands(doc, gates, source):
    """Refuse a harness no run can close: a focused-tests gate is what `ppl
    transition done` wants a pass receipt for, and it runs one named test. Refuse
    a placeholder the orchestrator would leave in the command as written."""
    focused = [index for index, gate in enumerate(gates) if gate["name"] == FOCUSED_TESTS_GATE]
    if not focused:
        _fail(source, "no [[gate]] is named %s: ppl transition done requires a pass receipt under "
              "that exact name, so a run with this harness could never close" % FOCUSED_TESTS_GATE)
    for index in focused:
        if TEST_CLASS_PLACEHOLDER not in gates[index]["command"]:
            _fail(source, "gate[%d] (%s) command must carry %s, the one test the test writer "
                  "names for the task" % (index, FOCUSED_TESTS_GATE, TEST_CLASS_PLACEHOLDER))
    # A fragment that only echoes a command, as the studio's generated gates.md
    # does, says nothing about what goes in its placeholders, so the echoes are
    # cut first, the longest command first so a shorter one cannot split it.
    echoes = sorted({gate["command"] for gate in gates}, key=len, reverse=True)
    named = set()
    if doc.get("_dir"):
        for rel in _PLACEHOLDER_FRAGMENTS:
            text = _fragment_text(doc, rel)
            for command in echoes:
                text = text.replace(command, "")
            named.update(_PLACEHOLDER_RE.findall(text))
    for index, gate in enumerate(gates):
        for placeholder in _PLACEHOLDER_RE.findall(gate["command"]):
            if placeholder not in RUN_TIME_PLACEHOLDERS and placeholder not in named:
                _fail(source, "gate[%d].command uses %s, which the orchestrator would run as "
                      "written: it fills only %s, and a project placeholder must be named in %s "
                      "to say what goes there" % (index, placeholder, ", ".join(RUN_TIME_PLACEHOLDERS),
                                                   " or ".join(_PLACEHOLDER_FRAGMENTS)))


def _check_files(doc, source):
    """Refuse a role overlay for no role, warn about any other file under skill/
    or templates/ that nothing reads. Dot files are left alone."""
    warnings = []
    root = Path(doc["_dir"])
    for folder in ("skill", "templates"):
        if not (root / folder).is_dir():
            continue
        for path in sorted((root / folder).rglob("*")):
            rel = path.relative_to(root)
            if any(part.startswith(".") for part in rel.parts) or not path.is_file():
                continue
            rel = rel.as_posix()
            if rel in FRAGMENTS:
                continue
            if folder == "templates" and path.parent == root / folder and path.suffix == ".part":
                _fail(source, "%s is an overlay for no role, so it never renders: name it after "
                      "one of %s" % (rel, ", ".join(routing.REQUIRED_ROLES)))
            known = SKILL_FRAGMENTS if folder == "skill" else FRAGMENTS[len(SKILL_FRAGMENTS):]
            warnings.append("%s is never read, delete it or rename it to one of: %s"
                            % (rel, ", ".join(known)))
    return warnings


def validate(doc, source):
    """Raise HARNESS_INVALID on a structural error, return a list of warnings.
    The checks of the harness directory run when the document carries its _dir,
    as one from load does."""
    warnings = []
    fmt = _require(doc, "harness", "format", source, kind=int)
    if fmt != FORMAT:
        _fail(source, "harness.format %s is not supported, this ppl reads format %s" % (fmt, FORMAT))
    _check_known_keys(doc, source)
    _require(doc, "harness", "name", source)
    _require(doc, "repo", "path", source)
    _require(doc, "repo", "default_branch", source)
    # an empty value means the default directory beside the repo, as _values reads it
    if "worktrees" in doc["repo"] and not isinstance(doc["repo"]["worktrees"], str):
        _fail(source, "repo.worktrees must be a string, the directory the run worktrees are "
              "made in")
    pattern = _require(doc, "task", "key_pattern", source)
    try:
        re.compile(pattern)
    except re.error as exc:
        _fail(source, "task.key_pattern is not a valid regex: %s" % exc)
    _require(doc, "task", "url_template", source)
    kind = _require(doc, "tracker", "kind", source)
    if kind not in TRACKERS:
        _fail(source, "tracker.kind %r is not one of %s" % (kind, ", ".join(TRACKERS)))
    for key in ("mcp_prefixes", "write_operations", "read_tools"):
        _require(doc, "tracker", key, source, kind=list)
    for index, prefix in enumerate(doc["tracker"]["mcp_prefixes"]):
        if not is_mcp_prefix(prefix):
            _fail(source, "tracker.mcp_prefixes[%d] %r must look like mcp__<name>__"
                  % (index, prefix))
    defaults = trackers.get(kind).DEFAULTS["mcp_prefixes"]
    if not set(defaults) & set(doc["tracker"]["mcp_prefixes"]):
        warnings.append("tracker.mcp_prefixes keeps none of the %s defaults (%s): the tracker "
                        "gate covers only the prefixes listed, and the briefer's read tools are "
                        "named under them too" % (kind, ", ".join(defaults)))
    forge = _require(doc, "forge", "kind", source)
    if forge not in FORGES:
        _fail(source, "forge.kind %r is not one of %s" % (forge, ", ".join(FORGES)))
    if forge == "gitlab":
        _require(doc, "forge", "mcp_server", source)
    knowledge = doc.get("knowledge") or {}
    if not isinstance(knowledge, dict):
        _fail(source, "[knowledge] must be a table")
    for key in ("skills", "extra_tools"):
        if not _strings(knowledge.get(key, [])):
            _fail(source, "knowledge.%s must be a list of strings" % key)
    _check_granted_tools(doc, knowledge, source)
    env = doc.get("env") or {}
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        _fail(source, "[env] must be a table of string values")
    for key in env:
        if not _ENV_KEY_RE.fullmatch(key):
            _fail(source, "[env] key %r is not a shell variable name (a letter or '_', then "
                  "letters, digits and '_')" % key)
    gates = doc.get("gate") or []
    if not isinstance(gates, list):
        _fail(source, "gate must be an array of tables, write [[gate]]")
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict) or not _text(gate.get("name")):
            _fail(source, "gate[%d].name is required" % index)
        if not _GATE_NAME_RE.fullmatch(gate["name"]):
            _fail(source, "gate[%d].name %r is not a plain word (letters, digits, '_', '.' and "
                  "'-' only), it names the gate's log and rc files" % (index, gate["name"]))
        if not _text(gate.get("command")):
            _fail(source, "gate[%d].command is required" % index)
    _check_gate_commands(doc, gates, source)
    if doc.get("_dir"):
        warnings.extend(_check_files(doc, source))
    for index, row in enumerate(shape_rows(doc, source)):
        if not row["required_flags"] and not row["one_goal_of"]:
            warnings.append("guard.shape[%d] (%s) has no required_flags and no one_goal_of: "
                            "any %s line will pass" % (index, row["argv0"], row["argv0"]))
    if doc.get("_dir") and installed_name(doc) != doc["harness"]["name"]:
        warnings.append('harness.name "%s" differs from its directory name "%s": ppl identifies '
                        "a harness by the directory name"
                        % (doc["harness"]["name"], installed_name(doc)))
    return warnings


def shape_rows(doc, source="harness"):
    guard = doc.get("guard") or {}
    if not isinstance(guard, dict):
        _fail(source, "[guard] must be a table")
    rows = guard.get("shape") or []
    if not isinstance(rows, list):
        _fail(source, "guard.shape must be an array of tables, write [[guard.shape]]")
    normalised = []
    for index, row in enumerate(rows):
        where = "guard.shape[%d]" % index
        if not isinstance(row, dict) or not _text(row.get("argv0")):
            _fail(source, "%s.argv0 is required" % where)
        flags = row.get("required_flags") or []
        if not isinstance(flags, list):
            _fail(source, "%s.required_flags must be a list" % where)
        for flag in flags:
            if not (isinstance(flag, list) and len(flag) == 2 and all(isinstance(x, str) for x in flag)):
                _fail(source, "%s.required_flags entries are [flag, value_regex]" % where)
            if flag[1]:
                try:
                    re.compile(flag[1])
                except re.error as exc:
                    _fail(source, "%s flag %s regex: %s" % (where, flag[0], exc))
        goals = row.get("one_goal_of") or []
        if not _strings(goals):
            _fail(source, "%s.one_goal_of must be a list of strings" % where)
        free = row.get("free_args", "")
        if not isinstance(free, str):
            _fail(source, "%s.free_args must be a string" % where)
        if free:
            try:
                re.compile(free)
            except re.error as exc:
                _fail(source, "%s.free_args regex: %s" % (where, exc))
        env_prefix = row.get("env_prefix", False)
        if not isinstance(env_prefix, bool):
            _fail(source, "%s.env_prefix must be true or false" % where)
        normalised.append({"argv0": row["argv0"], "env_prefix": env_prefix,
                           "required_flags": [list(f) for f in flags],
                           "one_goal_of": list(goals),
                           "free_args": free})
    return normalised


def load(name_or_path):
    directory = resolve_dir(name_or_path)
    path = directory / "harness.toml"
    try:
        with open(path, "rb") as handle:
            doc = tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        _fail(str(path), "not valid TOML: %s" % exc)
    doc["_dir"] = str(directory)
    validate(doc, str(path))
    return doc


def list_names():
    root = paths.harnesses_dir()
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "harness.toml").is_file())


def _clone(name, source, target):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(["git", "clone", "-q", "--", source, str(target)],
                                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                env=env, timeout=_CLONE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _fail(name, "git clone timed out after %d seconds" % _CLONE_TIMEOUT_SECONDS)
    except OSError as exc:
        _fail(name, "git clone could not start: %s" % exc)
    if result.returncode != 0:
        _fail(name, "git clone failed: %s" % result.stderr.strip())


def _copy(name, source, target):
    src = Path(source).expanduser()
    if not (src / "harness.toml").is_file():
        _fail(name, "%s holds no harness.toml" % src)
    try:
        shutil.copytree(src, target)
    except OSError as exc:
        _fail(name, "copy failed: %s" % exc)


def is_git_source(source):
    return "://" in source or source.endswith(".git") or source.startswith("git@")


def derived_name(source):
    """The name `add` installs a source under when it is given none: the last
    part of the URL or directory, without a trailing .git."""
    base = source.rstrip("/") if is_git_source(source) else str(_absolute(Path(source).expanduser()))
    return Path(base).name.removesuffix(".git")


def add(source, name=None, staging=False):
    """Install a git URL or a local directory as the harness `name`, by default
    the name derived from the source. The name must pass check_name: only
    setup, staging a harness in its own <name>.replacing, passes staging."""
    if not name:
        name = derived_name(source)
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        _fail(name, "not a valid harness name")
    if not staging:
        check_name(name)
    target = paths.harness_dir(name)
    if target.exists():
        _fail(name, "already exists at %s" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if is_git_source(source):
            _clone(name, source, target)
        else:
            _copy(name, source, target)
        load(str(target))
    except PplError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def installed_name(doc):
    """The directory name of a loaded harness. It is the identity ppl uses:
    `ppl harness apply <name>` and config.toml [harness].active both name it."""
    return Path(doc["_dir"]).name


def snapshot(doc):
    """The slice of a harness the guard hook enforces, frozen into the run
    directory at ppl init so a later edit of the harness cannot change what
    the cage does mid-run."""
    return {"format": FORMAT,
            "name": installed_name(doc),
            "task": {"key_pattern": doc["task"]["key_pattern"]},
            "tracker": copy.deepcopy(doc["tracker"]),
            "forge": copy.deepcopy(doc["forge"]),
            "shapes": shape_rows(doc)}


def require_applied(doc):
    """Refuse unless the loaded harness is the installed directory that
    config.toml [harness].active names. The agents and the skill on disk were
    rendered from that directory, so a copy elsewhere with the same document
    name is not the applied harness."""
    from . import config
    active = config.active_harness()
    directory = os.path.abspath(doc["_dir"])
    if active and directory == os.path.abspath(paths.harness_dir(active)):
        return
    if os.path.dirname(directory) == os.path.abspath(paths.harnesses_dir()):
        name = installed_name(doc)
        raise PplError("HARNESS_NOT_APPLIED",
                       "harness %r is not the applied one (active: %s). The agents and skill on "
                       "disk were rendered for another project: run `ppl harness apply %s` first."
                       % (name, active or "none", name))
    raise PplError("HARNESS_NOT_APPLIED",
                   "harness at %s is not an installed harness, add it with `ppl harness add` "
                   "and apply it" % directory)


# The orchestrator skill on disk is a build artifact: skill/ holds skeletons
# (`*.skeleton`) with include markers and plain files copied as they are, and
# render_skill fills every marker from the forge adapter and the harness.
SKILL_NOTICE = "<!-- generated by ppl harness apply - do not edit by hand -->"
_INCLUDE_RE = re.compile(r"<!-- include: ([a-z0-9-]+)/([a-z0-9-]+) -->")
_INCLUDE_OPEN = "<!-- include:"
_SKELETON_SUFFIX = ".skeleton"
_RC_IDIOM = "%s > /tmp/ppl-<TASK-ID>/%s.log 2>&1; echo $? > /tmp/ppl-<TASK-ID>/%s.rc"
_EVIDENCE = ("ppl evidence add --check %s --result <pass|fail from %s.rc> --sha <SHA> \\\n"
             "  --log /tmp/ppl-<TASK-ID>/%s.log --rc /tmp/ppl-<TASK-ID>/%s.rc --generation <GENERATION> \\\n"
             "  --from orchestrator")
_DEFAULT_BASELINE = ("Record what was true before you touched anything: the build, the tests "
                     "and the failures already present on the base revision.")
# What the orchestrator writes to the tracker once the merge is observed. A team
# that also fills tracker fields says so in its harness's skill/tracker-close.md.
DEFAULT_TRACKER_CLOSE = "a short comment saying what was done and how to check it"
# Documentation placeholders a fragment may use. `<worktree>`, `<TASK-ID>` and the
# like stay for the orchestrator to fill at run time. This one is the harness's
# own and is filled at render time.
_DEFAULT_BRANCH_PLACEHOLDER = "<default-branch>"
_FRONTMATTER = "---\n"
_FRONTMATTER_END = "\n---\n"


def _gate_recipes(doc):
    env = " ".join("%s=%s" % (key, shlex.quote(value))
                   for key, value in sorted((doc.get("env") or {}).items()))
    lines = ["mkdir -p /tmp/ppl-<TASK-ID>", "git -C <worktree> rev-parse HEAD", ""]
    for gate in doc.get("gate") or []:
        name = gate["name"]
        command = ("%s %s" % (env, gate["command"])).strip()
        lines.append("# %s" % name)
        lines.append(_RC_IDIOM % (command, name, name))
        lines.append(_EVIDENCE % (name, name, name, name))
        lines.append("")
    return "\n".join(lines).rstrip()


def _fragment_file(doc, name):
    path = Path(doc["_dir"]) / "skill" / ("%s.md" % name)
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return text.replace(_DEFAULT_BRANCH_PLACEHOLDER, doc["repo"]["default_branch"])


def _values(doc):
    forge = forges.get(doc["forge"]["kind"])
    skills = (doc.get("knowledge") or {}).get("skills") or []
    repo = doc["repo"]
    return {
        ("vocab", "noun"): forge.VOCABULARY["noun"],
        ("vocab", "short"): forge.VOCABULARY["short"],
        ("forge", "stage6"): forge.fragment("stage6").strip(),
        ("forge", "stage7"): forge.fragment("stage7").strip(),
        ("forge", "review-loop"): forge.fragment("review-loop").strip(),
        ("forge", "author-lookup"): forge.author_lookup(),
        # ppl init --harness resolves a directory name, so the skill names the
        # installed directory, not the document's own name.
        ("harness", "name"): installed_name(doc),
        ("harness", "repo"): repo["path"],
        ("harness", "default-branch"): repo["default_branch"],
        ("harness", "worktrees"): repo.get("worktrees") or (repo["path"].rstrip("/") + "/.worktrees"),
        ("harness", "gates"): _fragment_file(doc, "gates"),
        # One line: the baseline sentence also fills a table cell.
        ("harness", "baseline"): " ".join(_fragment_file(doc, "baseline").split()) or _DEFAULT_BASELINE,
        ("harness", "gate-recipes"): _gate_recipes(doc),
        # Project-specific recipe steps with their evidence lines, spliced in
        # as written. A marker inside the fragment is refused, not resolved.
        ("harness", "extra-gates"): _fragment_file(doc, "extra-gates"),
        # One line: it ends a sentence of the closing section.
        ("harness", "tracker-close"): (" ".join(_fragment_file(doc, "tracker-close").split())
                                       or DEFAULT_TRACKER_CLOSE),
        ("knowledge", "skills"): (", ".join("`%s`" % s for s in skills) if skills
                                  else "no knowledge skills are configured for this harness"),
    }


def _render_text(text, values, source):
    def replace(match):
        key = (match.group(1), match.group(2))
        if key not in values:
            raise PplError("SCHEMA_INVALID", "%s: unknown include marker %s/%s" % (source, key[0], key[1]))
        return values[key]
    return _INCLUDE_RE.sub(replace, text)


def _with_frontmatter(text, skill_name):
    """Name the skill and put the generated notice right after the
    frontmatter: parsers anchor the opening delimiter at byte 0."""
    head, sep, tail = text.partition(_FRONTMATTER_END)
    if not text.startswith(_FRONTMATTER) or not sep:
        raise PplError("SCHEMA_INVALID", "SKILL.md: the skeleton must open with a --- frontmatter block")
    head = re.sub(r"^name: .*$", "name: %s" % skill_name, head, count=1, flags=re.MULTILINE)
    return head + sep + SKILL_NOTICE + "\n" + tail


def render_skill(doc, skill_name="pipeline"):
    """relative path -> text of every file of the skill, nothing written.
    Refuses SCHEMA_INVALID on an include marker it cannot resolve, so a
    marker never reaches the disk."""
    if not _NAME_RE.match(skill_name or ""):
        raise PplError("SCHEMA_INVALID", "skill name %r is not a plain directory name" % skill_name)
    values = _values(doc)
    root = paths.root() / "skill"
    rendered = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        relative = relative.as_posix()
        text = path.read_text(encoding="utf-8")
        if relative.endswith(_SKELETON_SUFFIX):
            relative = relative[:-len(_SKELETON_SUFFIX)]
            text = _render_text(text, values, relative)
            if relative == "SKILL.md":
                text = _with_frontmatter(text, skill_name)
        if _INCLUDE_OPEN in text:
            line = next(line for line in text.splitlines() if _INCLUDE_OPEN in line)
            raise PplError("SCHEMA_INVALID", "%s: include marker left unresolved: %s"
                           % (relative, line.strip()))
        rendered[relative] = text
    if "SKILL.md" not in rendered:
        raise PplError("SCHEMA_INVALID", "no SKILL.md.skeleton under %s" % root)
    return rendered


def _write_files(rendered, target):
    written = []
    for relative, text in rendered.items():
        path = target / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(str(path))
    return written


def write_skill(doc, skills_dir, skill_name="pipeline"):
    return _write_files(render_skill(doc, skill_name), Path(skills_dir) / skill_name)


def apply(name, agents_dir=None, skills_dir=None):
    """Render the six agents and the skill for an installed harness, then
    record it as the active one. Everything is rendered before anything is
    written, so a bad marker or a bad routing.toml leaves the disk as it was."""
    from . import config
    doc = load(name)
    if os.path.dirname(os.path.abspath(doc["_dir"])) != os.path.abspath(paths.harnesses_dir()):
        _fail(name, "%s is not an installed harness, add it with `ppl harness add` and apply it "
              "by name" % doc["_dir"])
    config.active_harness()
    skill_files = render_skill(doc)
    _size_warnings(skill_files, {role: routing.render(role, doc) for role in routing.REQUIRED_ROLES})
    skill_target = (Path(skills_dir) if skills_dir is not None else config.skills_dir()) / "pipeline"
    agents = routing.apply(doc, agents_dir)
    skill = _write_files(skill_files, skill_target)
    active = installed_name(doc)
    config.set_active_harness(active)
    return {"agents": agents, "skill": skill, "active": active}


def agent_texts(doc):
    """role -> the agent file apply would write, rendered with the shipped
    routing.example.toml so the size check does not depend on routing.toml."""
    example = routing.loads((paths.defaults_dir() / "routing.example.toml")
                            .read_text(encoding="utf-8"))
    return {role: routing.render(role, doc, example) for role in routing.REQUIRED_ROLES}


def _size_warnings(skill_files, agents):
    """Refuse HARNESS_INVALID when the rendered SKILL.md or an agent file is over
    its hard cap, return a warning for each one over its soft cap."""
    outputs = [("the rendered SKILL.md", skill_files["SKILL.md"], SKILL_SOFT_BYTES,
                SKILL_HARD_BYTES, "skill/ fragments")]
    outputs += [("the rendered agent file %s.md" % role, text, AGENT_SOFT_BYTES, AGENT_HARD_BYTES,
                 "templates/%s.part" % role) for role, text in agents.items()]
    warnings = []
    for what, text, soft, hard, fix in outputs:
        size = len(text.encode("utf-8"))
        if size > hard:
            raise PplError("HARNESS_INVALID", "%s is %d bytes, over the hard cap of %d bytes: "
                           "shorten the %s" % (what, size, hard, fix))
        if size > soft:
            warnings.append("%s is %d bytes, over the soft cap of %d bytes: every byte of it is "
                            "read on every run, shorten the %s" % (what, size, soft, fix))
    return warnings


def require_absolute_repo(doc):
    path = doc["repo"]["path"]
    try:
        absolute = Path(path).expanduser().is_absolute()
    except RuntimeError:
        absolute = False
    if not absolute:
        raise PplError("HARNESS_INVALID", "repo.path %r must be an absolute path or start with "
                       "~/: setup would resolve a relative one against the working directory "
                       "of whatever runs it, ppl serve included" % path)


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


def unrunnable_focused_tests(doc):
    """A warning when the test writer's shell would refuse the focused-tests
    command, the one gate the skill hands the test worker: the guard takes
    leading NAME=value words only for a shape with env_prefix on, and the
    program must be the argv0 of a shape."""
    rows = shape_rows(doc)
    for gate in doc.get("gate") or []:
        if gate["name"] == FOCUSED_TESTS_GATE:
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


def _missing_skills(doc):
    """A warning per knowledge skill found in neither the configured skills
    directory nor ~/.claude/skills. Only a warning: a plugin skill lives elsewhere."""
    from . import config, doctor
    roots = []
    try:
        roots.append(config.skills_dir())
    except PplError:
        pass
    if doctor.claude_home() / "skills" not in roots:
        roots.append(doctor.claude_home() / "skills")
    warnings = []
    for name in (doc.get("knowledge") or {}).get("skills") or []:
        if not any((root / name / "SKILL.md").is_file() for root in roots):
            warnings.append("knowledge.skills names %s, which is not under %s: the briefer is "
                            "told to load a skill it may not find (fine for a plugin skill)"
                            % (name, " or ".join(str(root) for root in roots)))
    return warnings


def verify(name_or_path, source):
    """{"errors": [...], "warnings": [...]} for the harness at `name_or_path`,
    what `ppl harness validate` prints and the studio shows for a draft or a
    file it saves: loaded and validated, repo.path absolute, the skill and
    every agent file rendered, each held to its size cap. A problem comes
    back as the one error, never a raise."""
    try:
        doc = load(str(name_or_path))
        warnings = validate(doc, source)
        require_absolute_repo(doc)
        warnings += _size_warnings(render_skill(doc), agent_texts(doc))
        warnings += unrunnable_focused_tests(doc)
        warnings += _missing_skills(doc)
    except PplError as exc:
        return {"errors": [exc.message], "warnings": []}
    except UnicodeDecodeError as exc:
        return {"errors": ["harness %s: a harness file is not UTF-8 text: %s" % (source, exc)],
                "warnings": []}
    except (TypeError, ValueError) as exc:
        return {"errors": ["harness %s: a value has the wrong type and cannot be rendered: %s"
                           % (source, exc)], "warnings": []}
    return {"errors": [], "warnings": warnings}

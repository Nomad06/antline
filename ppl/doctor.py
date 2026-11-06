"""Where the PreToolUse guard hook is registered, in the settings of the session's own and the
Claude home's, and whether each registration can fire. In a lane the session's own settings are
the settings.json `ppl lane` rendered into the lane's start directory (lane_settings_path), whose
hooks carry the lane's task on their command line; `ppl init` refuses a run whose lane settings
register no guard naming its task (lane_guard_problem). Outside a lane `ppl doctor` checks the
machine alone: the setup steps, the models, the harnesses and the Claude Code binary.

Claude Code scopes a `.claude/settings.local.json` hook to sessions launched
inside that directory tree; a hook in `~/.claude/settings.json` fires for
every session on the machine. Both are real, both are files on disk, and
both can be inspected without running a session - that is what this module
does.

The same files answer a second question the registration alone cannot: whether
the matcher each guard entry is registered on passes every tool name the hook
acts on. A name the matcher never passes is a denial that cannot fire however
correct the code behind it - the Jira gate was dead this way when the Atlassian
server turned out to register under a different prefix - so `inspect()` reports
the uncovered names per file, and `ppl init` refuses to create a run while any
registered matcher leaves one uncovered (`matcher_problem`).

Known blind spots, not closed by this module (DESIGN 5.4's residual list):
`PPL_CLAUDE_HOME` fully substitutes for `~/.claude` when set, so a stray
value in the environment makes this check inspect an unrelated (and likely
empty) directory and report clear; and `_has_guard` matches the literal
substring `ppl_guard.py` in a hook's command, so another pipeline checkout's
guard hook at a different path would be read as this one's.
"""
import base64
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import lanes
from .errors import PplError

GUARD_MARKER = "ppl_guard.py"
DISPATCH_MARKER = "ppl_dispatch.py"
USAGE_MARKER = "ppl_usage.py"
HOOK_EVENTS = (("PreToolUse", GUARD_MARKER), ("SubagentStart", DISPATCH_MARKER),
               ("SubagentStop", USAGE_MARKER))
_VERSION_PROBE = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")

# The matcher README.md "Hooks" registers the guard on: match everything, let the hook
# decide. An explicit tool list is fragile (a tracker prefix it does not name, a read
# tool it leaves out), and each gap is a denial that can never fire behind a doctor
# that says "registered".
# `matcher_covers` still judges any explicit matcher an operator registers instead
# against `guarded_tool_names()`, and a test holds this string to that same check.
RECOMMENDED_MATCHER = ".*"
# A GitHub MCP issue write. The tracker gate and the role ban match these under any
# mcp__<server>__ prefix, and they are no guarded names: many installs have no GitHub
# MCP server, so a matcher that cannot pass this one draws a warning, never a refusal.
GITHUB_ISSUE_WRITE_PROBE = "mcp__github__add_issue_comment"


def claude_home():
    return Path(os.environ.get("PPL_CLAUDE_HOME", Path.home() / ".claude"))


def global_settings_path():
    return claude_home() / "settings.json"


def lane_settings_path(entry):
    """The settings.json `ppl lane` rendered into the start directory of the lane registry entry
    `entry`: the hooks its session runs, each ending in --task <task>, and its OS sandbox
    profile."""
    return Path(str(entry.get("start_dir"))) / "settings.json"


def _lane_at(cwd=None):
    """The registry entry of the lane the session is in, else None: with `cwd`, the lane whose
    start directory or worktree holds it; without, the lane a ppl call is in, resolved as
    cli.resolve_task resolves its run (PPL_TASK, then the process cwd). A registry that cannot
    be read is no lane here: ppl init and ppl lane refuse it on their own."""
    try:
        if cwd is not None:
            return lanes.for_path(cwd)
        task = lanes.task_for_call(None)
        return lanes.entry(task) if task else None
    except PplError:
        return None


def local_settings_path(cwd=None):
    """The settings file of the session's own: in a lane its settings.json
    (lane_settings_path), else <cwd>/.claude/settings.local.json."""
    entry = _lane_at(cwd)
    if entry is not None:
        return lane_settings_path(entry)
    # PPL_LOCAL_SETTINGS_DIR exists only so tests can pin this without
    # changing the process's actual working directory, which is shared with
    # everything else running in the same interpreter.
    base = cwd or os.environ.get("PPL_LOCAL_SETTINGS_DIR") or os.getcwd()
    return Path(base) / ".claude" / "settings.local.json"


def session_dir(cwd=None):
    """The directory the session starts in: the lane's start directory in a lane, else `cwd`
    (PPL_LOCAL_SETTINGS_DIR, then the process cwd, when None)."""
    entry = _lane_at(cwd)
    if entry is not None:
        return Path(str(entry.get("start_dir")))
    return Path(cwd or os.environ.get("PPL_LOCAL_SETTINGS_DIR") or os.getcwd())


def _sides(cwd=None):
    """[(name, path)] of the two settings files a guard registration is read from. In a lane,
    the two sources its session loads (R56): its settings.json ("local") and its start
    directory's .claude/settings.local.json ("start_local"), the local source of its cwd, since
    --setting-sources local keeps the Claude home's settings.json and a project settings.json
    out. Elsewhere, the directory's .claude/settings.local.json ("local") and the Claude home's
    settings.json ("global")."""
    entry = _lane_at(cwd)
    if entry is not None:
        start = Path(str(entry.get("start_dir")))
        return [("local", lane_settings_path(entry)),
                ("start_local", start / ".claude" / "settings.local.json")]
    return [("local", local_settings_path(cwd)), ("global", global_settings_path())]


def hook_remedy(cwd=None):
    """How the hook entries of the session are written again: in a lane ppl lane renders them,
    pinned to the interpreter that runs it, so the owner starts the lane again outside it."""
    entry = _lane_at(cwd)
    return ("ppl lane renders the hook entries, pinned to the interpreter that runs it (Python "
            "3.11 or newer): %s" % lanes.restart_hint(entry.get("task") if entry else None))


def _guard_matchers(doc):
    """The matcher of every PreToolUse entry whose hooks run the guard.

    An entry with no matcher key contributes None, which Claude Code reads as
    every tool, the same as "" or "*".
    """
    matchers = []
    if not doc:
        return matchers
    pre_tool_use = (doc.get("hooks") or {}).get("PreToolUse")
    if not isinstance(pre_tool_use, list):
        return matchers
    for entry in pre_tool_use:
        if not isinstance(entry, dict):
            continue
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict) and GUARD_MARKER in str(hook.get("command", "")):
                matchers.append(entry.get("matcher"))
                break
    return matchers


def _has_guard(doc):
    return bool(_guard_matchers(doc))


# A registered guard that cannot execute (an interpreter older than 3.11, a moved
# checkout) allows every call while the file checks above stay green, so
# `ppl doctor` runs each registered guard command once on a payload the cage
# allows and that writes nothing: an unroled Read of /dev/null.
HOOK_RUN_TIMEOUT_SECONDS = 10
# The command a hook remedy names, also in the interpreter floor's own line, so a problem that
# already names it is not told it twice.
HOOK_RUN_REMEDY = "ppl lane"


def _guard_commands(doc):
    """The command of every guard hook in a parsed settings document, in order."""
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
    commands = []
    for entry in entries if isinstance(entries, list) else []:
        for hook in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
            command = hook.get("command") if isinstance(hook, dict) else None
            if isinstance(command, str) and GUARD_MARKER in command:
                commands.append(command)
    return commands


def _command_task(command):
    """The task a guard command names after its script as `--task <id>`, else None."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    scripts = [index for index, word in enumerate(words) if os.path.basename(word) == GUARD_MARKER]
    if not scripts:
        return None
    try:
        return lanes.hook_task(words[scripts[0] + 1:])
    except ValueError:
        return None


def lane_guard_problem(entry):
    """None when the lane's settings.json (lane_settings_path) registers the guard and every
    guard command there ends in --task <the lane's task>, else the message ppl init refuses
    with: a run starts only under its own lane's guard."""
    task = entry.get("task")
    path = lane_settings_path(entry)
    remedy = ("ppl lane renders it with every hook ending in --task %s: %s"
              % (task, lanes.restart_hint(task)))
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ("lane %s's %s cannot be read (%s), so the guard of its session cannot be "
                "confirmed: %s" % (task, path, exc, remedy))
    commands = _guard_commands(doc)
    if not commands:
        return ("lane %s's %s registers no guard hook (%s), so its session would run unguarded: "
                "%s" % (task, path, GUARD_MARKER, remedy))
    for command in commands:
        if _command_task(command) != task:
            return ("lane %s's %s registers the guard hook `%s`, whose command line does not end "
                    "in --task %s, so it does not bind its calls to this run: %s"
                    % (task, path, command, task, remedy))
    return None


def registered_guard_commands(cwd=None):
    """Every guard command in the two settings files of the session (_sides), each once.
    A file that cannot be read or parsed contributes none, `inspect` reports it."""
    commands = []
    for _, path in _sides(cwd):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for command in _guard_commands(doc):
            if command not in commands:
                commands.append(command)
    return commands


def guard_probe_session_id():
    """The session_id that marks the probe below, read from the hook file so the two
    cannot drift: the guard does not log a deny of it against any run."""
    _hooks_on_path()
    import ppl_guard
    return ppl_guard.DOCTOR_PROBE_SESSION_ID


def _run_problem(command, workspace):
    """None when `command` takes the synthetic payload to exit 0 with no stderr,
    else what went wrong, with the first stderr line."""
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/dev/null"},
                          "cwd": workspace, "session_id": guard_probe_session_id()})
    try:
        # The command is the settings file's own hook string, which Claude Code
        # also runs through a shell, so it is run the same way.
        result = subprocess.run(command, shell=True, input=payload, capture_output=True,
                                text=True, timeout=HOOK_RUN_TIMEOUT_SECONDS,
                                cwd=workspace if os.path.isdir(workspace) else None)
    except subprocess.TimeoutExpired:
        return "did not finish in %s seconds" % HOOK_RUN_TIMEOUT_SECONDS
    except OSError as exc:
        return "could not be started: %s" % exc
    first = next((line.strip() for line in result.stderr.splitlines() if line.strip()), None)
    if result.returncode == 0 and first is None:
        return None
    if first is None:
        return "exited %d" % result.returncode
    return "exited %d: %s" % (result.returncode, first)


def hook_runs(cwd=None, workspace=None):
    """{"ok": True} when every registered guard command executes clean,
    {"ok": False, "problem": ...} naming the first one that does not and the
    remedy, {"ok": None} when no guard is registered. `cwd` picks the session's
    settings file (local_settings_path: in a lane the lane's guard, with its
    --task), `workspace` is the payload's cwd (default: the session's directory,
    session_dir). Kept out of `inspect`, which `ppl init` calls on every run:
    this is the one place doctor runs what a settings file names, so the
    interpreter's version is asked here too, before the hook runs."""
    commands = registered_guard_commands(cwd)
    if not commands:
        return {"ok": None, "problem": None}
    workspace = str(workspace or session_dir(cwd))
    for command in commands:
        problem = _interpreter_problem(command) or _run_problem(command, workspace)
        if problem:
            message = "the registered guard hook `%s` %s" % (command, problem.rstrip("."))
            # The interpreter floor's own line already names the remedy.
            if HOOK_RUN_REMEDY not in problem:
                message += ". %s" % hook_remedy(cwd)
            return {"ok": False, "problem": message + "."}
    return {"ok": True, "problem": None}


def _hooks_on_path():
    from . import paths
    hooks_dir = str(paths.PACKAGE_ASSETS / "hooks")
    if hooks_dir not in sys.path:
        # Appended, not inserted: the hooks directory must never shadow a ppl module.
        sys.path.append(hooks_dir)


def guarded_tool_names():
    """Every tool name the guard hook acts on, read from the hook file itself
    (ppl/_assets/hooks/ppl_guard.py:GUARDED_TOOL_NAMES) so this check cannot drift from the cage."""
    _hooks_on_path()
    import ppl_guard
    return tuple(ppl_guard.GUARDED_TOOL_NAMES)


def _python_floor():
    _hooks_on_path()
    import ppl_python_floor
    return tuple(ppl_python_floor.MINIMUM)


def _hooks_of(doc, event, marker):
    """Every hook under `event` in a parsed settings document whose command
    names `marker`, in order."""
    hooks = doc.get("hooks") if isinstance(doc, dict) else None
    entries = hooks.get(event) if isinstance(hooks, dict) else None
    found = []
    for entry in entries if isinstance(entries, list) else []:
        inner = entry.get("hooks") if isinstance(entry, dict) else None
        for hook in inner if isinstance(inner, list) else []:
            if isinstance(hook, dict) and marker in str(hook.get("command", "")):
                found.append(hook)
    return found


def _python_version(interpreter):
    """(version, None) of `interpreter`, or (None, why it gave none)."""
    try:
        result = subprocess.run([interpreter, "-I", "-c", _VERSION_PROBE], capture_output=True,
                                text=True, stdin=subprocess.DEVNULL,
                                timeout=HOOK_RUN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, "which did not answer in %s seconds" % HOOK_RUN_TIMEOUT_SECONDS
    except OSError as exc:
        return None, "which could not be started: %s" % exc
    version = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        return None, "which did not answer as a Python interpreter"
    return version, None


def _past_env(words):
    """The program `env` runs in `words`: past its options, the argument of -u,
    and any NAME=value."""
    index = 1
    while index < len(words):
        word = words[index]
        if word == "--":
            index += 1
            break
        if word in ("-u", "--unset"):
            index += 2
        elif word.startswith("-") or _ENV_ASSIGNMENT_RE.match(word):
            index += 1
        else:
            break
    return words[index] if index < len(words) else ""


def _interpreter_problem(command):
    """None when the first word of a hook command answers as a Python that
    meets the hooks' floor, else what is wrong with it. It runs that word, so
    only hook_runs calls it."""
    floor = _python_floor()
    shown = "%d.%d" % floor
    try:
        words = shlex.split(command)
    except ValueError:
        return "has a command that does not split into words"
    first = _past_env(words) if words and os.path.basename(words[0]) == "env" else \
        (words[0] if words else "")
    if first.endswith(tuple(marker for _, marker in HOOK_EVENTS)):
        return ("runs the script itself, so its shebang picks the interpreter: name a Python "
                "%s or newer first" % shown)
    version, why = _python_version(first)
    if version is None:
        return "runs under %s, %s" % (first, why)
    if tuple(int(part) for part in version.split(".")[:2]) < floor:
        return "runs under %s, Python %s, older than the %s the hooks need" % (first, version, shown)
    return None


def _shape_problems(doc, path):
    """(marker, problem) for every guard, dispatch and usage hook in a parsed
    settings document that Claude Code would not run as a bounded command:
    read from the document only, never run."""
    problems = []
    for event, marker in HOOK_EVENTS:
        for hook in _hooks_of(doc, event, marker):
            found = []
            if hook.get("type") != "command":
                found.append('has type %r, not "command", so Claude Code never runs it'
                             % (hook.get("type"),))
            timeout = hook.get("timeout")
            if timeout is None:
                found.append("has no timeout")
            elif isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
                found.append("has timeout %r, not a positive number of seconds" % (timeout,))
            found += _command_problems(str(hook.get("command", "")), marker)
            problems += [(marker, "%s: the %s hook %s" % (path, marker, text)) for text in found]
    return problems


def _command_problems(command, marker):
    """What keeps a hook command from running, judged from its words alone:
    nothing it names is run here (hook_runs asks the interpreter's version)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return ["has a command that does not split into words"]
    found = []
    first = words[0] if words else ""
    if not os.path.isabs(first):
        found.append("runs %r, not an absolute interpreter path, so which one runs depends on "
                     "the PATH Claude Code has" % first)
    elif not (os.path.isfile(first) and os.access(first, os.X_OK)):
        found.append("runs %s, which is not an executable file" % first)
    scripts = [word for word in words[1:] if os.path.basename(word) == marker]
    if not scripts:
        found.append("names no %s script after its interpreter" % marker)
    elif not (os.path.isabs(scripts[0]) and os.path.isfile(scripts[0])):
        found.append("runs the script %s, which is not an absolute path to a file" % scripts[0])
    return found


def lane_hook_problems(start_dir):
    """What keeps a ppl hook of the lane's settings.json in `start_dir` from running as the
    cage, one line each, [] when nothing does: for the guard, dispatch and usage hooks, an
    interpreter or script that does not exist (_command_problems), and an interpreter older
    than the floor or not a Python (_interpreter_problem, which runs it once to ask). A
    pinned interpreter that vanished makes the shell exit 127, a non-blocking error, so every
    call is allowed. A settings.json that cannot be read, or that registers none of a hook,
    counts too [R-GUARD-PYTHON-FLOOR]."""
    from . import store
    path = Path(str(start_dir)) / "settings.json"
    try:
        doc = store.read_json(path)
    except (PplError, OSError) as exc:
        return ["%s cannot be read: %s" % (path, getattr(exc, "message", None) or exc)]
    if not isinstance(doc, dict):
        return ["%s is missing or not an object" % path]
    problems, asked = [], {}
    for event, marker in HOOK_EVENTS:
        hooks = _hooks_of(doc, event, marker)
        if not hooks:
            problems.append("%s registers no %s hook" % (path, marker))
        for hook in hooks:
            command = str(hook.get("command", ""))
            found = _command_problems(command, marker)
            if not found:
                interpreter = command.split(" ", 1)[0]
                if interpreter not in asked:
                    asked[interpreter] = _interpreter_problem(command)
                found = [asked[interpreter]] if asked[interpreter] else []
            problems += ["%s: the %s hook %s" % (path, marker, text) for text in found]
    return problems


def _readable_settings(cwd):
    """(path, parsed document) of each of the session's two settings files (_sides) that
    exists and parses into an object. `inspect` reports the others."""
    found = []
    for _, path in _sides(cwd):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict):
            found.append((path, doc))
    return found


def guard_shape_problem(cwd=None):
    """None when every guard entry in the two settings files is a command with
    a timeout, an absolute executable interpreter and its script, else the
    message `ppl init` refuses with: an entry of another shape never fires, or
    fires unbounded. The interpreter's version is left to `ppl doctor`."""
    problems = [text for path, doc in _readable_settings(cwd)
                for marker, text in _shape_problems(doc, path) if marker == GUARD_MARKER]
    if not problems:
        return None
    return ("%s. %s. `ppl doctor` runs the hook and reports an interpreter older than Python "
            "3.11." % ("; ".join(problems), hook_remedy(cwd)))


def _hook_warning(cwd, event, marker, name, consequence):
    """None unless a guard is registered while the `event` hook (`marker`) is
    in no settings file, or is there in the wrong shape, else the problems,
    `consequence` and the fix."""
    settings = _readable_settings(cwd)
    if not any(_hooks_of(doc, "PreToolUse", GUARD_MARKER) for _, doc in settings):
        return None
    problems = [text for path, doc in settings
                for found, text in _shape_problems(doc, path) if found == marker]
    if not any(_hooks_of(doc, event, marker) for _, doc in settings):
        problems.append("the %s %s hook (%s) is registered in neither %s nor %s"
                        % ((event, name, marker) + tuple(path for _, path in _sides(cwd))))
    if not problems:
        return None
    return ("%s. %s. %s." % ("; ".join(problems), consequence, hook_remedy(cwd)))


def dispatch_warning(cwd=None):
    """None unless a guard is registered while the SubagentStart dispatch hook
    is in no settings file, or is there in the wrong shape: the dispatch cap
    then counts only the dispatches recorded by hand. `ppl init` warns."""
    return _hook_warning(cwd, "SubagentStart", DISPATCH_MARKER, "dispatch",
                         "The dispatch cap counts only the dispatches recorded by hand until "
                         "it fires")


def usage_warning(cwd=None):
    """None unless a guard is registered while the SubagentStop usage hook is
    in no settings file, or is there in the wrong shape: no tokens are then
    recorded and every cost number stays undefined. `ppl init` warns."""
    return _hook_warning(cwd, "SubagentStop", USAGE_MARKER, "usage",
                         "The tokens are not recorded, so the cost metrics stay undefined")


def _hook_scripts(command, marker):
    """The absolute script paths named `marker` among a hook command's words."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    return [word for word in words[1:]
            if os.path.basename(word) == marker and os.path.isabs(word)]


def hook_location_warning(cwd=None):
    """None unless a registered guard, dispatch or usage command runs a script
    outside the hooks directory of this install (`paths.root()/hooks`): a
    clone or an older install left behind keeps running its own copy of the
    hook. `ppl init` and `ppl doctor` warn. It never raises: `ppl init` prints
    it after the run exists. A path that does not resolve (a symlink loop
    raises RuntimeError on Python 3.11 and 3.12) is skipped."""
    from . import paths
    try:
        hooks_dir = (paths.root() / "hooks").resolve()
    except (PplError, OSError, RuntimeError):
        return None
    found = []
    for path, doc in _readable_settings(cwd):
        for event, marker in HOOK_EVENTS:
            for hook in _hooks_of(doc, event, marker):
                for script in _hook_scripts(str(hook.get("command", "")), marker):
                    try:
                        inside = Path(script).resolve().is_relative_to(hooks_dir)
                    except (OSError, RuntimeError):
                        continue
                    if not inside:
                        found.append("%s: the %s hook runs %s" % (path, marker, script))
    if not found:
        return None
    return ("%s, not the hook of this install under %s. %s." % ("; ".join(found), hooks_dir,
                                                                hook_remedy(cwd)))


def matcher_covers(matcher, tool_name):
    """Whether a Claude Code PreToolUse matcher passes `tool_name` to its hooks.

    Claude Code reads a missing, empty or "*" matcher as every tool and anything
    else as a regular expression against the tool name. Coverage is judged as a
    full match, the stricter of the two readings an implementation could take: a
    matcher that passes here fires under either, while a search would call
    `mcp__atlassian` (no `.*`) covered on the strength of a prefix. A pattern
    that does not compile covers nothing - fail closed.
    """
    if matcher is None:
        return True
    text = str(matcher).strip()
    if text in ("", "*"):
        return True
    try:
        return re.fullmatch(text, tool_name) is not None
    except re.error:
        return False


def uncovered_by(matchers):
    """The guarded tool names none of `matchers` covers - the union over every
    entry that registers the guard, since the hook fires when any one matches."""
    return [name for name in guarded_tool_names()
            if not any(matcher_covers(matcher, name) for matcher in matchers)]


def matcher_warning(cwd=None):
    """None unless a settings file registers the guard only on matchers that cannot
    pass GITHUB_ISSUE_WRITE_PROBE, else a warning naming each such file and the gap.
    `ppl doctor` prints it and `ppl init` writes it to stderr, neither refuses."""
    missing = []
    for path, doc in _readable_settings(cwd):
        matchers = _guard_matchers(doc)
        if matchers and not any(matcher_covers(matcher, GITHUB_ISSUE_WRITE_PROBE)
                                for matcher in matchers):
            missing.append(str(path))
    if not missing:
        return None
    return ("%s registers the guard hook on a matcher that never passes %s to it: GitHub MCP "
            "issue writes would not reach the guard, so neither the tracker gate nor the role "
            "ban could hold them. If a GitHub MCP server is registered, set that entry's matcher "
            "to \"%s\" (README.md \"Hooks\")."
            % (" and ".join(missing), GITHUB_ISSUE_WRITE_PROBE, RECOMMENDED_MATCHER))


def _location(path):
    """{"state": "yes"|"no"|"unknown", "detail", "uncovered"} for one settings file.

    "no" is a confirmed absence: the file does not exist, or it parses cleanly
    into a JSON object with no guard entry. "unknown" means this check could
    not reach a confirmed answer at all - the file exists but is not valid
    JSON (a hand-added `//` comment is an ordinary edit, not a contrived
    case), is not a JSON object, or could not be read (permissions, a race
    with a concurrent write, anything else). Folding "unknown" into "no" is
    exactly the failure this module exists to remove - reporting "clear" when
    the true answer is "cannot tell" - so it is never done here; the caller
    decides what "unknown" should mean for its own purposes.

    "uncovered" is the list of guarded tool names the file's guard matchers
    never pass to the hook - empty for a complete registration, None whenever
    the state is not a confirmed yes, since there is no matcher to judge.

    "dispatch_hook" and "usage_hook" are "registered" or "missing" for the
    SubagentStart dispatch hook and the SubagentStop usage hook, and
    "hook_shape" is "ok" or the problems of the guard, dispatch and usage
    entries (`_shape_problems`), None when the file holds none of them. All
    three are None for a file that could not be read.
    """
    def result(state, detail=None, uncovered=None, dispatch_hook=None, usage_hook=None,
               hook_shape=None):
        return {"state": state, "detail": detail, "uncovered": uncovered,
                "dispatch_hook": dispatch_hook, "usage_hook": usage_hook,
                "hook_shape": hook_shape}

    if not path.exists():
        return result("no", dispatch_hook="missing", usage_hook="missing")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return result("unknown", "%s could not be read: %s" % (path, exc))
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        return result("unknown", "%s is not valid JSON: %s" % (path, exc))
    if not isinstance(doc, dict):
        return result("unknown", "%s does not contain a JSON object" % path)
    hooks = {"dispatch_hook": "missing", "usage_hook": "missing", "hook_shape": None}
    if _hooks_of(doc, "SubagentStart", DISPATCH_MARKER):
        hooks["dispatch_hook"] = "registered"
    if _hooks_of(doc, "SubagentStop", USAGE_MARKER):
        hooks["usage_hook"] = "registered"
    if any(_hooks_of(doc, event, marker) for event, marker in HOOK_EVENTS):
        hooks["hook_shape"] = "; ".join(text for _, text in _shape_problems(doc, path)) or "ok"
    matchers = _guard_matchers(doc)
    if not matchers:
        return result("no", **hooks)
    return result("yes", uncovered=uncovered_by(matchers), **hooks)


def _location_state(path):
    """("yes"|"no"|"unknown", detail) - the registration half of `_location`."""
    location = _location(path)
    return location["state"], location["detail"]


def inspect(cwd=None):
    """The full picture: per-location state plus the combined registration.

    The two sides answer different questions, so an unreadable side is not
    interchangeable with the other one (round 3 finding): a confirmed local yes says
    nothing about whether the global side, which fires in every session on the machine,
    is clear, so "local yes, global unreadable" is "unknown", not "local". The reverse
    asymmetry is fine: a confirmed *global* yes holds on its own, whatever the local side
    reads, so "global yes, local unreadable" is "global".

    So: `registration` is `"both"` only when both sides are confirmed yes;
    `"local"` when local is a confirmed yes and global is a confirmed no
    (not merely non-yes - an unreadable global is not a confirmed no);
    `"global"` when global is a confirmed yes, regardless of the local
    side's readability; `"unknown"` whenever the global side could not be
    read, or (with global confirmed no) the local side could not be read
    either; and `"none"` only when both sides are confirmed absent.

    `matcher` is the coverage verdict over the confirmed registrations (see
    `_matcher_verdict`), and each side carries the `uncovered` names behind it.

    In a lane the second side is not the Claude home's settings.json, which the lane session
    does not load, but its start directory's .claude/settings.local.json, which it does
    (_sides, R56): the result then carries `start_local` in place of `global`, and a
    registration only there reads "start_local".
    """
    (own_name, own_path), (other_name, other_path) = _sides(cwd)
    own = _location(own_path)
    other = _location(other_path)
    own_state, other_state = own["state"], other["state"]
    if other_state == "unknown":
        where = "unknown"
    elif own_state == "yes" and other_state == "yes":
        where = "both"
    elif own_state == "yes":
        where = own_name
    elif other_state == "yes":
        where = other_name
    elif own_state == "unknown":
        where = "unknown"
    else:
        where = "none"
    return {
        "registration": where,
        "matcher": _matcher_verdict(own, other),
        own_name: dict(own, path=str(own_path)),
        other_name: dict(other, path=str(other_path)),
    }


def _matcher_verdict(*locations):
    """"ok" when every confirmed registration covers every guarded tool name,
    "incomplete" when at least one leaves some uncovered, None when nothing is
    registered at all - an unarmed hook has no matcher to be wrong about."""
    registered = [loc for loc in locations if loc["state"] == "yes"]
    if not registered:
        return None
    return "incomplete" if any(loc["uncovered"] for loc in registered) else "ok"


def registration(cwd=None):
    """Convenience wrapper for callers that only need the combined state."""
    return inspect(cwd)["registration"]


def matcher_problem(inspected):
    """None when every confirmed registration's matcher covers every guarded
    tool name, else a message naming the file, the names and the fix.

    A registered hook whose matcher never passes a tool name to it is a denial
    that cannot fire wherever the hook lives. Sides that are not a confirmed yes
    contribute nothing: an unarmed hook is the documented unregistered state, and
    an unreadable file is reported as "unknown".
    """
    problems = []
    for side in (inspected.get("local"), inspected.get("global"), inspected.get("start_local")):
        if not side or side.get("state") != "yes" or not side.get("uncovered"):
            continue
        names = side["uncovered"]
        shown = ", ".join(names[:6])
        if len(names) > 6:
            shown += " and %d more" % (len(names) - 6)
        problems.append("%s registers the guard hook on a matcher that never passes %d of the "
                        "tool names it acts on to it (%s)" % (side["path"], len(names), shown))
    if not problems:
        return None
    return ("%s. A denial the hook never sees cannot fire, however the code behind it reads. "
            "Set that entry's matcher to \"%s\" (README.md \"Hooks\") and re-run "
            "ppl doctor." % ("; ".join(problems), RECOMMENDED_MATCHER))


# The setup steps a run cannot start without. paths, claude and forge are
# reported by `inspect_setup` and not required: a run works with hooks registered
# by hand and a forge nobody checked.
REQUIRED_STEPS = ("models", "harness", "apply")


def _problem(exc):
    return exc.message if isinstance(exc, PplError) else "%s: %s" % (type(exc).__name__, exc)


def setup_state():
    """The cheap half of `inspect_setup`: the recorded steps, `config_problem`
    when config.toml cannot be read, and the active harness. `ppl init` checks
    this alone, so it never pays for the model rows or a forge check.

    It never raises. `harness` carries only `active` and `problem` here.
    """
    from . import config, setup
    report = {}
    state = {"done": set(), "skipped": set()}
    try:
        state = config.steps_state()
    except Exception as exc:
        report["config_problem"] = _problem(exc)
    report["setup"] = {"done": sorted(state["done"]), "skipped": sorted(state["skipped"]),
                       "missing": [s for s in setup.STEPS if s not in state["done"]]}
    report["harness"] = {"active": None, "problem": None}
    try:
        report["harness"]["active"] = config.active_harness()
    except Exception as exc:
        report["harness"]["problem"] = _problem(exc)
    return report


def inspect_setup(env=None):
    """What `ppl setup` left behind, for `ppl doctor` to report: the roots, the
    recorded steps (`setup_state`), the model rows, the active harness and its
    forge and tracker checks.

    It never raises. Whatever cannot be read (a broken config.toml, a missing
    routing file, an invalid harness, a forge check that blows up) lands in the
    report as a problem string instead: `ppl doctor` reports, `ppl init` refuses.
    `config_problem`, `models_problem` and `code_root_problem` are present only when
    there is one.
    """
    # ppl.setup imports this module, so these stay function-local
    from . import config, forges, harness, models, paths, trackers
    report = {"code_root": None, "data_root": str(paths.home())}
    try:
        report["code_root"] = str(paths.root())
    except PplError as exc:
        report["code_root_problem"] = exc.message
    report.update(setup_state())
    report["models"] = []
    try:
        if paths.routing_file().is_file():
            report["models"] = models.status()
    except Exception as exc:
        report["models_problem"] = _problem(exc)
    entry = dict(report["harness"], path=None, valid=False, repo_exists=None)
    forge = None
    tracker = None
    if entry["active"]:
        doc = None
        try:
            doc = harness.load(entry["active"])
            repo_exists = Path(doc["repo"]["path"]).is_dir()
            entry.update(path=doc["_dir"], valid=True, repo_exists=repo_exists)
        except Exception as exc:
            entry["problem"] = _problem(exc)
        if entry["valid"]:
            forge, tracker = gh_checks(doc, env)
    report["harness"] = entry
    report["forge"] = forge
    report["tracker"] = tracker
    return report


def setup_problem(report, skip_model_check):
    """None when the setup is complete enough to start a run, else the message
    `ppl init` refuses with. The models step is waived by --skip-model-check,
    the harness and apply steps never are. `report` is `setup_state()` or
    `inspect_setup()`."""
    if report.get("config_problem"):
        return ("the setup state could not be read: %s. Fix config.toml, then run `ppl setup`."
                % report["config_problem"])
    required = [step for step in report["setup"]["missing"] if step in REQUIRED_STEPS
                and (step != "models" or not skip_model_check)]
    if not required:
        return None
    commands = ["`ppl setup %s`" % step for step in required]
    run = commands[0] if len(commands) == 1 else ", ".join(commands[:-1]) + " and " + commands[-1]
    message = "setup is incomplete: %s. Run %s." % (", ".join(required), run)
    if "models" in required and "models" in report["setup"]["skipped"]:
        message += (" The models step was skipped: pass --skip-model-check to start this run "
                    "without verified models.")
    return message


def claude_binary():
    """{"binary", "path"}: the Claude Code binary config.toml names (claude when it names none)
    and where PATH finds it, None when PATH finds none. It never raises."""
    from . import config
    try:
        binary = config.section("claude").get("binary") or "claude"
    except Exception:
        binary = "claude"
    if not isinstance(binary, str):
        return {"binary": None, "path": None}
    return {"binary": binary, "path": shutil.which(binary)}


SKIPPED_IN_SANDBOX = "skipped inside the OS sandbox, run ppl doctor --forge outside it"


def _in_os_sandbox(env):
    return (os.environ if env is None else env).get("SANDBOX_RUNTIME") == "1"


def _gh_kinds(doc):
    return doc["forge"]["kind"] == "github", doc["tracker"]["kind"] == "github-issues"


def gh_checks(doc, env=None):
    """(forge, tracker) check results of a loaded harness. Inside the OS sandbox the gh
    checks are skipped, since GitHub is closed there by design."""
    from . import forges, trackers
    skipped = {"ok": None, "detail": SKIPPED_IN_SANDBOX}
    forge_gh, tracker_gh = _gh_kinds(doc)
    inside = _in_os_sandbox(env)
    settings = doc["forge"]
    forge = {"kind": settings["kind"]}
    if inside and forge_gh:
        forge.update(skipped)
    else:
        try:
            forge.update(forges.get(settings["kind"]).doctor_check(
                settings, claude_home().parent / ".claude.json", str(Path.home())))
        except Exception as exc:
            forge.update(ok=False, detail="the forge check failed: %s" % _problem(exc))
    table = doc["tracker"]
    tracker = {"kind": table["kind"]}
    if inside and tracker_gh:
        tracker.update(skipped)
    else:
        try:
            tracker.update(trackers.get(table["kind"]).doctor_check(table))
        except Exception as exc:
            tracker.update(ok=False, detail="the tracker check failed: %s" % _problem(exc))
    return forge, tracker


def forge_report(env=None):
    """What `ppl doctor --forge` prints: only the forge and tracker checks."""
    from . import config, harness
    result = {"forge": None, "tracker": None}
    try:
        active = config.active_harness()
        if not active:
            result["problem"] = "no active harness"
            return result
        result["forge"], result["tracker"] = gh_checks(harness.load(active), env)
    except Exception as exc:
        result["problem"] = _problem(exc)
    return result


def _servers(doc):
    servers = doc.get("mcpServers") if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {name: config for name, config in servers.items() if isinstance(name, str)}


def _json_object(path):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def operator_mcp_servers(project_dir=None, claude_json=None):
    """{name: config} from the operator's own ~/.claude.json only: the project entry for
    `project_dir` (local scope, keyed by the path and its realpath) over the top-level
    mcpServers (user scope). Never <project_dir>/.mcp.json or a settings file: those are
    controlled by whoever commits to the project, and a lane's --strict-mcp-config launch
    never asks the operator to approve what they name."""
    base = Path(project_dir) if project_dir else session_dir()
    claude_json = Path(claude_json) if claude_json else claude_home().parent / ".claude.json"
    user = _json_object(claude_json)
    if user is None:
        return {}
    found = _servers(user)
    projects = user.get("projects")
    if isinstance(projects, dict):
        for key in dict.fromkeys((str(base), os.path.realpath(base))):
            found.update(_servers(projects.get(key)))
    return found


def mcp_server_names(cwd=None):
    """The names, never a command, args or env, of the MCP servers a lane's session loads: those
    of the mcp.json ppl lane rendered into its start directory, the only ones its
    --strict-mcp-config launch loads. [] outside a lane."""
    entry = _lane_at(cwd)
    if entry is None:
        return []
    return sorted(_servers(_json_object(Path(str(entry.get("start_dir"))) / "mcp.json")))


def mcp_servers_warning(cwd=None):
    """One line naming the MCP servers a lane's session loads, when the lane has OS sandbox
    facts: they run outside the sandbox and the edit-tool boundary cannot see them. None
    outside a lane, without facts or without servers."""
    from . import os_sandbox
    entry = _lane_at(cwd)
    try:
        if entry is None or os_sandbox.load_facts(entry.get("task")) is None:
            return None
    except PplError:
        return None
    names = mcp_server_names(cwd)
    if not names:
        return None
    return ("MCP servers run outside the OS sandbox and can write anywhere: %s. Register no "
            "file-writing MCP server for a pipeline lane. The list leaves out plugin MCP "
            "servers and claude.ai connectors, which run outside it as well." % ", ".join(names))


# The variables gh and the GitHub tooling read a token from. Every sandboxed command, a role's
# included, inherits the session's environment, so a token there is one a role can read.
TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
# How a GitHub token starts, whatever variable holds it.
TOKEN_PREFIXES = ("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")


def github_token_names(env=None):
    """The names, never a value, of the variables in `env` (this process's environment by
    default) that hold a GitHub token: TOKEN_VARIABLES when set, then any other whose value
    starts with a TOKEN_PREFIXES prefix."""
    env = os.environ if env is None else env
    named = [name for name in TOKEN_VARIABLES if env.get(name)]
    found = sorted(name for name, value in env.items()
                   if name not in named and isinstance(value, str)
                   and value.lstrip().startswith(TOKEN_PREFIXES))
    return named + found


def github_token_warning(env=None):
    """One line naming the GitHub token variables set in the session's environment, never a
    value, or None."""
    names = github_token_names(env)
    if not names:
        return None
    return ("%s %s a GitHub token in this session's environment, which every sandboxed "
            "command, a role's included, inherits and can read: unset %s for pipeline sessions "
            "and let the pinned gh read its own login"
            % (", ".join(names), "holds" if len(names) == 1 else "hold",
               "it" if len(names) == 1 else "them"))


# The interpreters whose script, not their own name, says which hook runs.
_INTERPRETER = re.compile(r"(?:python[0-9.]*|node|nodejs|bash|sh|zsh|dash|ruby|perl|deno|bun"
                          r"|php)")
# The interpreter options after which what runs is inline code, a module or stdin, never a
# script file worth naming.
_INLINE_OPTIONS = ("-c", "-e", "--eval", "-s", "-", "-m", "-p", "--print")
INLINE_LABEL = "<inline command>"


def _filename_like(word):
    return (bool(word) and not any(char.isspace() for char in word)
            and not any(char in word for char in "='\"`"))


def _hook_label(hook):
    """A hook as doctor names it: the command's first word, or the script an interpreter runs,
    by its base name alone, past any NAME=value words with or without env. Inline code, a
    module or stdin is named by the interpreter alone, and a word that does not look like a
    file name by INLINE_LABEL, so no argument, which may carry a token, is ever kept."""
    if not isinstance(hook, dict):
        return "an unreadable hook"
    command = hook.get("command")
    if hook.get("type", "command") != "command" or not isinstance(command, str):
        return "a %s hook" % hook.get("type") if isinstance(hook.get("type"), str) else \
            "an unreadable hook"
    try:
        words = shlex.split(command)
    except ValueError:
        return INLINE_LABEL
    if words and os.path.basename(words[0]) == "env":
        first = _past_env(words)
        words = words[words.index(first):] if first else []
    while words and _ENV_ASSIGNMENT_RE.match(words[0]):
        words = words[1:]
    if not words:
        return "an empty command"
    if not _filename_like(words[0]):
        return INLINE_LABEL
    name = os.path.basename(words[0])
    if not _INTERPRETER.fullmatch(name):
        return name
    for word in words[1:]:
        if word in _INLINE_OPTIONS or word.startswith(("-c", "-e")) and len(word) > 2 \
                and not word.startswith("--"):
            return name
        if word.startswith("-"):
            continue
        return os.path.basename(word) if _filename_like(word) else INLINE_LABEL
    # no script: the interpreter reads its code from stdin
    return name


def _may_pass_bash(matcher):
    """Whether a PreToolUse matcher may pass Bash, erring toward yes: a missing, empty or "*"
    matcher, one a search finds in "Bash", and one that does not compile all count."""
    if matcher is None:
        return True
    text = str(matcher).strip()
    if text in ("", "*"):
        return True
    try:
        return re.search(text, "Bash") is not None
    except re.error:
        return True


def _enabled_plugins(documents):
    """The plugin names the settings documents enable, a later document overriding an earlier
    one, as Claude Code layers user, project, local and managed settings."""
    state = {}
    for doc in documents:
        enabled = doc.get("enabledPlugins") if isinstance(doc, dict) else None
        if isinstance(enabled, dict):
            state.update((name, value) for name, value in enabled.items()
                         if isinstance(name, str))
    return [name for name, value in state.items() if value is True]


def _plugin_hook_files(names):
    """The hooks/hooks.json of each install of an enabled plugin, from Claude Code's plugin
    registry."""
    registry = _json_object(claude_home() / "plugins" / "installed_plugins.json") or {}
    plugins = registry.get("plugins")
    files = []
    for name in names:
        installs = plugins.get(name) if isinstance(plugins, dict) else None
        for install in installs if isinstance(installs, list) else []:
            root = install.get("installPath") if isinstance(install, dict) else None
            if isinstance(root, str) and root:
                files.append(Path(root) / "hooks" / "hooks.json")
    return files


def _settings_sources(cwd=None):
    """Every settings file the session loads, each once. In a lane (R56): its settings.json,
    its start directory's .claude/settings.local.json and the managed one, which no
    --setting-sources keeps out, and never the Claude home's or a project settings.json.
    Elsewhere: the user's, the directory's project and local ones and the managed one."""
    if _lane_at(cwd) is not None:
        return tuple(path for _, path in _sides(cwd)) + (managed_settings_path(),)
    base = session_dir(cwd)
    return tuple(dict.fromkeys((global_settings_path(), base / ".claude" / "settings.json",
                                base / ".claude" / "settings.local.json",
                                local_settings_path(cwd), managed_settings_path())))


def foreign_bash_hooks(cwd=None):
    """Every PreToolUse hook other than ppl's guard whose matcher passes Bash, from the settings
    the session loads (_settings_sources: in a lane its settings.json, its start directory's
    .claude/settings.local.json and the managed settings, R56; elsewhere the user, project,
    local and managed settings) and the hooks/hooks.json of each plugin they enable, as
    {"source": file, "hook": _hook_label}. Such a hook may rewrite a Bash call with
    updatedInput, and whether that keeps dangerouslyDisableSandbox is unverified."""
    documents = [(path, _json_object(path)) for path in _settings_sources(cwd)]
    sources = documents + [(path, _json_object(path)) for path in
                           _plugin_hook_files(_enabled_plugins([doc for _, doc in documents]))]
    found = []
    for path, doc in sources:
        hooks = doc.get("hooks") if isinstance(doc, dict) else None
        entries = hooks.get("PreToolUse") if isinstance(hooks, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not _may_pass_bash(entry.get("matcher")):
                continue
            inner = entry.get("hooks")
            for hook in inner if isinstance(inner, list) else []:
                if isinstance(hook, dict) and GUARD_MARKER in str(hook.get("command", "")):
                    continue
                found.append({"source": str(path), "hook": _hook_label(hook)})
    return found


def foreign_bash_hooks_warning(cwd=None):
    """One line naming the foreign Bash hooks, or None. Not a refusal: Claude Code merges hooks
    from every source and has no per-workspace switch short of disableAllHooks, which would
    drop ppl's guard as well."""
    hooks = foreign_bash_hooks(cwd)
    if not hooks:
        return None
    return ("PreToolUse hooks other than ppl's guard see every Bash call: %s. One that "
            "rewrites a command with updatedInput may keep or drop dangerouslyDisableSandbox, "
            "which is unverified, so remove or disable them for pipeline sessions before a "
            "GitHub run" % ", ".join("%s (%s)" % (item["hook"], item["source"])
                                     for item in hooks))


def managed_settings_path():
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


# The reason Claude Code's proxy gives for a host on its deny list when the settings give it
# no other one.
DENY_LIST_REASON = "host is on the deny list"


def probe_github(env):
    """"denied" on a 403 whose reason is the deny list's and "open" on a 200 from a CONNECT to
    api.github.com:443 through the env's HTTPS_PROXY, else "unknown". The proxy URL is read
    here and never returned."""
    url = env.get("HTTPS_PROXY") or env.get("https_proxy")
    if not url:
        return "unknown"
    try:
        parts = urlsplit(url)
        request = "CONNECT api.github.com:443 HTTP/1.1\r\nHost: api.github.com:443\r\n"
        if parts.username is not None:
            user = "%s:%s" % (unquote(parts.username), unquote(parts.password or ""))
            request += "Proxy-Authorization: Basic %s\r\n" % base64.b64encode(
                user.encode("utf-8")).decode("ascii")
        with socket.create_connection((parts.hostname, parts.port or 80), timeout=3) as conn:
            conn.settimeout(3)
            conn.sendall((request + "\r\n").encode("ascii"))
            data = b""
            while b"\n" not in data and len(data) < 1024:
                chunk = conn.recv(256)
                if not chunk:
                    break
                data += chunk
    except (OSError, ValueError):
        return "unknown"
    from . import os_sandbox
    line = data.split(b"\n", 1)[0].decode("ascii", "replace").strip()
    words = line.split(None, 2)
    status = words[1] if len(words) > 1 else ""
    reason = words[2] if len(words) > 2 else ""
    if status == "200":
        return "open"
    # any 403 is not enough: an unlisted host is refused with another reason, and only the
    # deny list's reason, ppl's own or the default, says GitHub is closed
    if status == "403" and (DENY_LIST_REASON in reason or os_sandbox.DENY_REASON in reason):
        return "denied"
    return "unknown"


def _native_windows():
    return os.name == "nt"


def _under(path, scope):
    scope = scope.rstrip(os.sep) or os.sep
    return path == scope or path.startswith(scope + os.sep)


def _exposed(facts, env):
    scope = [e for e in facts.get("write_scope") or [] if isinstance(e, str)]
    candidates = []
    for pin in (facts.get("pins") or {}).values():
        if isinstance(pin, dict):
            candidates += [pin.get("path"), pin.get("realpath")]
    candidates += [d for d in (env.get("PATH") or "").split(os.pathsep) if d]
    found = []
    for item in candidates:
        if not isinstance(item, str) or not item or item in found:
            continue
        forms = {os.path.abspath(item), os.path.realpath(item)}
        if any(_under(form, entry) for form in forms for entry in scope):
            found.append(item)
    return found


def _protected(recorded):
    """escape_paths.protected_paths with the recorded facts when they hold a workspace and
    every pin, else without them."""
    from . import escape_paths, paths
    pins = recorded.get("pins")
    usable = (isinstance(recorded.get("workspace"), str) and isinstance(pins, dict)
              and all(isinstance(pins.get(name), dict)
                      and isinstance(pins[name].get("path"), str)
                      and isinstance(pins[name].get("realpath"), str)
                      for name in escape_paths.PIN_NAMES))
    return escape_paths.protected_paths(paths.home(), recorded if usable else None)


def _settings_path(entry, base):
    """A path from a settings file as Claude Code reads it: ~ expanded, // read as /, and a
    relative one under the workspace, `realpath`-ed."""
    text = os.path.expanduser(entry)
    if text.startswith("//"):
        text = text[1:]
    return os.path.realpath(os.path.join(str(base), text))


def _covers(entries, protected, base):
    """The first of `entries` that is, holds or lies under a protected path, with that path."""
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, str) or not entry:
            continue
        real = _settings_path(entry, base)
        for target in protected:
            if _under(target, real) or _under(real, target):
                return entry, target
    return None


def _undoing(cwd, recorded=None):
    """What in a settings source undoes the profile: sandbox.excludedCommands, the sandbox or
    the unsandboxed retry switched off, a write grant (sandbox.filesystem.allowWrite or
    permissions.additionalDirectories) over a protected path, and any unix socket allowance.
    ppl's own block in the session's settings, the one the facts record, is not another source.
    The sources are the ones the session loads (_settings_sources): in a lane its settings.json,
    its start directory's .claude/settings.local.json and the managed settings (R56)."""
    recorded = recorded or {}
    base = session_dir(cwd)
    local = local_settings_path(cwd)
    sources = _settings_sources(cwd)
    protected = _protected(recorded)
    found = []
    for path in sources:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(doc, dict):
            continue
        permissions = doc.get("permissions")
        if isinstance(permissions, dict):
            hit = _covers(permissions.get("additionalDirectories"), protected, base)
            if hit:
                found.append("%s sets permissions.additionalDirectories %s, which covers %s"
                             % ((path,) + hit))
        block = doc.get("sandbox")
        if not isinstance(block, dict):
            continue
        if block.get("excludedCommands"):
            found.append("%s sets sandbox.excludedCommands" % path)
        if block.get("enabled") is False:
            found.append("%s sets sandbox.enabled to false" % path)
        if block.get("allowUnsandboxedCommands") is False:
            found.append("%s sets sandbox.allowUnsandboxedCommands to false" % path)
        network = block.get("network")
        if isinstance(network, dict):
            if network.get("allowUnixSockets"):
                found.append("%s sets sandbox.network.allowUnixSockets" % path)
            if network.get("allowAllUnixSockets") is True:
                found.append("%s sets sandbox.network.allowAllUnixSockets" % path)
        filesystem = block.get("filesystem")
        if isinstance(filesystem, dict) and not (path == local
                                                 and block == recorded.get("written_sandbox")):
            hit = _covers(filesystem.get("allowWrite"), protected, base)
            if hit:
                found.append("%s sets sandbox.filesystem.allowWrite %s, which covers %s"
                             % ((path,) + hit))
    return found


def os_sandbox_facts(cwd=None, env=None, probe=None):
    """Whether Claude Code's OS sandbox closes GitHub for this session. `state` is "on" only
    with the env marker, ppl's profile in the session's settings (in a lane its settings.json,
    whose sandbox block must equal written_sandbox in the lane's os_sandbox.json), a denied
    live probe and nothing undoing it. Nothing here carries the proxy URL."""
    from . import os_sandbox
    env = dict(os.environ) if env is None else env
    marker = env.get("SANDBOX_RUNTIME") == "1"
    entry = _lane_at(cwd)
    try:
        recorded = (os_sandbox.load_facts(entry.get("task")) if entry is not None else None) or {}
    except PplError:
        recorded = {}
    written = recorded.get("written_sandbox")
    try:
        local = json.loads(local_settings_path(cwd).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        local = None
    profile = (isinstance(local, dict) and isinstance(written, dict)
               and local.get("sandbox") == written)
    windows = _native_windows()
    live = "unknown"
    if not windows:
        live = (probe or (lambda: probe_github(env)))()
    undoing = _undoing(cwd, recorded)
    if windows:
        state = "unavailable"
    elif marker and profile and live == "denied" and not undoing:
        state = "on"
    else:
        state = "off"
    return {"state": state, "env_marker": marker, "profile": profile, "live": live,
            "undoing": undoing, "exposed": _exposed(recorded, env)}


def report(task_id=None, cwd=None, env=None, probe=None):
    """What `ppl doctor` prints as JSON. The lane is found as every ppl command finds its run
    (lanes.task_for_call: `task_id`, else PPL_TASK of `env`, else the lane holding `cwd`, the
    process cwd by default), and `task` is that run when its state can be read, else None.
    `lane_problem` says why a lane was named and cannot be confirmed (a registry that cannot be
    read, a task with no entry), and `start_dir` is the confirmed lane's start directory.
    Outside a lane (`lane` None) it checks the machine alone: the setup report, the models, the
    harnesses, the Claude Code binary and the GitHub token variables. In a lane it adds where
    the guard is registered in the lane's settings.json and the Claude home's, the lane guard's
    task, every hook's interpreter and script (lane_hooks, what ppl lane resume refuses on),
    the OS sandbox facts, the foreign Bash hooks, the MCP servers and whether the lane's guard
    runs."""
    from . import run
    problem = None
    try:
        task_id = lanes.task_for_call(task_id, cwd, env)
    except PplError as err:
        task_id, problem = None, err.message
    entry = None
    if task_id:
        try:
            entry = lanes.entry(task_id)
        except PplError as err:
            problem = ("lane %s cannot be confirmed, the lane registry cannot be read (%s): "
                       "repair %s, then run ppl lane resume %s"
                       % (task_id, err.message, lanes.registry_path(), task_id))
        else:
            if entry is None:
                problem = ("lane %s is not registered: run ppl lane resume %s"
                           % (task_id, task_id))
    try:
        if task_id:
            run.load(task_id)
    except PplError:
        task_id = None
    result = {"lane": entry.get("task") if entry else None, "task": task_id}
    if problem:
        result["lane_problem"] = problem
    result.update(inspect_setup(env))
    result["claude_binary"] = claude_binary()
    result["github_token_env"] = github_token_names(env)
    if entry is None:
        return result
    where = str(entry.get("start_dir"))
    result["start_dir"] = where
    result.update(inspect(where))
    result["lane_guard_problem"] = lane_guard_problem(entry)
    result["lane_hooks"] = lane_hook_problems(where)
    result["os_sandbox"] = os_sandbox_facts(where, env, probe)
    result["foreign_bash_hooks"] = foreign_bash_hooks(where)
    result["mcp_servers"] = mcp_server_names(where)
    runs = hook_runs(cwd=where, workspace=where)
    result["hook_runs"] = runs["ok"]
    if runs["problem"]:
        result["hook_runs_problem"] = runs["problem"]
    return result

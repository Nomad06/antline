"""Whether the PreToolUse guard hook is actually registered where the
`isolated` flag (DESIGN 5.4) assumes it is - a fact `isolated` cannot check
about itself, only assert. `ppl doctor` reports it; `ppl init --isolated`
refuses on the configurations that make that assertion silently wrong: hook
registered only globally, hook registered both locally and globally (the
global copy still fires everywhere), or registration that could not be
confirmed at all because a settings file exists but could not be read or
parsed - including a global file that is unreadable even when the local
side is a confirmed yes, since a working local registration says nothing
about whether the global side is actually clear.

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
registered matcher leaves one uncovered, isolated or not (`matcher_problem`).

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
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

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


def local_settings_path(cwd=None):
    # PPL_LOCAL_SETTINGS_DIR exists only so tests can pin this without
    # changing the process's actual working directory, which is shared with
    # everything else running in the same interpreter.
    base = cwd or os.environ.get("PPL_LOCAL_SETTINGS_DIR") or os.getcwd()
    return Path(base) / ".claude" / "settings.local.json"


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
HOOK_RUN_REMEDY = "ppl setup claude"


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


def registered_guard_commands(cwd=None):
    """Every guard command in the local and the global settings file, each once.
    A file that cannot be read or parsed contributes none, `inspect` reports it."""
    commands = []
    for path in (local_settings_path(cwd), global_settings_path()):
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
    cannot drift: the guard does not log a deny of it against the active run."""
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
    remedy, {"ok": None} when no guard is registered. `cwd` picks the local
    settings file, `workspace` is the payload's cwd (default: `cwd`, then the
    process cwd). Kept out of `inspect`, which `ppl init` calls on every run:
    this is the one place doctor runs what a settings file names, so the
    interpreter's version is asked here too, before the hook runs."""
    commands = registered_guard_commands(cwd)
    if not commands:
        return {"ok": None, "problem": None}
    workspace = str(workspace or cwd or os.getcwd())
    for command in commands:
        problem = _interpreter_problem(command) or _run_problem(command, workspace)
        if problem:
            message = "the registered guard hook `%s` %s" % (command, problem.rstrip("."))
            # The interpreter floor's own line already names the remedy.
            if HOOK_RUN_REMEDY not in problem:
                message += ". Run `%s` to register it with the interpreter that runs ppl" \
                    % HOOK_RUN_REMEDY
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


def _readable_settings(cwd):
    """(path, parsed document) of each settings file that exists and parses
    into an object. `inspect` reports the others."""
    found = []
    for path in (local_settings_path(cwd), global_settings_path()):
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
    return ("%s. Run `ppl setup claude` to write the hook entries again (README.md \"Hooks\"), "
            "and `ppl doctor`, which runs the hook and reports an interpreter older than Python "
            "3.11." % "; ".join(problems))


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
                        % (event, name, marker, local_settings_path(cwd), global_settings_path()))
    if not problems:
        return None
    return ("%s. %s: run `ppl setup claude` to write the hook entries again."
            % ("; ".join(problems), consequence))


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
    return ("%s, not the hook of this install under %s. Run `ppl setup claude` to point the "
            "entries at this install's hooks." % ("; ".join(found), hooks_dir))


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
    interchangeable with the other one (round 3 finding): `isolated=true`
    asserts both "the hook fires here" (needs a confirmed *local* yes) and
    "the hook does not fire elsewhere" (needs a confirmed *global* no). A
    confirmed local yes says nothing about whether the global side is
    actually clear, so it must not paper over an unreadable global file -
    "local yes, global unreadable" is "unknown", not "local", because the
    thing isolation actually depends on (no global registration) was never
    confirmed. The reverse asymmetry is fine: a confirmed *global* yes is
    disqualifying on its own regardless of whether the local side could be
    read, so "global yes, local unreadable" is "global", which already
    refuses under `isolated=true` - there is nothing an unreadable local file
    could tell us that would make an armed global hook acceptable.

    So: `registration` is `"both"` only when both sides are confirmed yes;
    `"local"` when local is a confirmed yes and global is a confirmed no
    (not merely non-yes - an unreadable global is not a confirmed no);
    `"global"` when global is a confirmed yes, regardless of the local
    side's readability; `"unknown"` whenever the global side could not be
    read, or (with global confirmed no) the local side could not be read
    either; and `"none"` only when both sides are confirmed absent.

    `matcher` is the coverage verdict over the confirmed registrations (see
    `_matcher_verdict`), and each side carries the `uncovered` names behind it.
    """
    local_path = local_settings_path(cwd)
    global_path = global_settings_path()
    local = _location(local_path)
    global_ = _location(global_path)
    local_state, global_state = local["state"], global_["state"]
    if global_state == "unknown":
        where = "unknown"
    elif local_state == "yes" and global_state == "yes":
        where = "both"
    elif local_state == "yes":
        where = "local"
    elif global_state == "yes":
        where = "global"
    elif local_state == "unknown":
        where = "unknown"
    else:
        where = "none"
    return {
        "registration": where,
        "matcher": _matcher_verdict(local, global_),
        "local": dict(local, path=str(local_path)),
        "global": dict(global_, path=str(global_path)),
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

    Unlike `mismatch` this does not depend on `isolated`: a registered hook
    whose matcher never passes a tool name to it is a denial that cannot fire
    wherever the hook lives. Sides that are not a confirmed yes contribute
    nothing - an unarmed hook is the documented unregistered state, and an
    unreadable file is `mismatch`'s business.
    """
    problems = []
    for side in (inspected.get("local"), inspected.get("global")):
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


def mismatch(isolated, inspected):
    """None when `inspected` is consistent with `isolated`, else a message naming a fix.

    `inspected` is normally the dict `inspect()` returns; a bare registration
    string (one of "local"/"global"/"both"/"none"/"unknown") is also accepted,
    for callers - and tests - that already have just the combined state and
    do not need the per-file detail in the "unknown" message.

    Three combinations are silently wrong and worth refusing over, all only
    under isolated=true (the default, and the documented recommended path):

    - "global": the hook is confirmed registered only globally. That denies
      5.4's own claim - the Jira gate and the pause would reach every session
      on the machine, not just this run, while asserting the opposite.
    - "unknown": this check could not confirm the global side is clear (see
      `inspect()`), so isolated=true's "the hook does not fire elsewhere"
      half cannot be confirmed either. Treating "unknown" as "fine" here
      would be the exact bug this module exists to remove, now living inside
      the fix for it.
    - "both": the hook fires locally, which is what isolated=true wants, but
      it is *also* registered globally, which means it fires everywhere else
      too - the same violation as "global" alone, just with a working local
      registration sitting next to it. A deliberate migration in progress
      (global first, local added afterward) looks identical to this from a
      file on disk, and the refusal is the same either way: isolation is not
      what this run has until the global registration is gone.

    "none" is not flagged: an unarmed hook is the pre-existing, openly
    documented unregistered state (README.md "Hooks"), not a mismatch between a flag and
    reality.
    """
    if not isolated:
        return None
    if isinstance(inspected, dict):
        where = inspected["registration"]
        broken = [loc["detail"] for loc in (inspected.get("local"), inspected.get("global"))
                 if loc and loc.get("detail")]
    else:
        where = inspected
        broken = []
    if where == "unknown":
        detail = "; ".join(broken) if broken else "a settings file could not be confirmed"
        return ("the guard hook's registration could not be confirmed (%s). Isolation cannot "
                "be verified from a settings file this check cannot read or parse - fix it, or "
                "re-run with --no-isolated to accept the cwd-scoped fallback knowing this "
                "could not be checked." % detail)
    if where == "global":
        return ("the guard hook is registered only globally (%s), not in this directory's "
                "settings.local.json (%s). With isolated=true, that means the Jira gate and "
                "the operator pause reach every Claude Code session on this machine, not "
                "just this run - the opposite of what isolated=true asserts. Fix by "
                "registering the hook locally instead (see README.md \"Hooks\"), or by "
                "passing --no-isolated to accept the cwd-scoped fallback."
                % (global_settings_path(), local_settings_path()))
    if where == "both":
        return ("the guard hook is registered both locally (%s) and globally (%s). The global "
                "registration still fires in every session on this machine, so isolation is "
                "not what this run has, regardless of the local one - the same problem as "
                "\"global\" alone, not made safer by also having a working local copy. Remove "
                "the global registration for isolation to mean anything, or pass "
                "--no-isolated to accept that the hook reaches every session."
                % (local_settings_path(), global_settings_path()))
    return None


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


def workspace():
    """The configured workspace, where Claude Code sessions start, else the cwd."""
    from . import config
    try:
        return config.section("claude").get("workspace") or os.getcwd()
    except Exception:
        return os.getcwd()


SKIPPED_IN_SANDBOX = "skipped inside the OS sandbox, run ppl doctor --forge outside it"


def _in_os_sandbox(env):
    return (os.environ if env is None else env).get("SANDBOX_RUNTIME") == "1"


def _gh_kinds(doc):
    return doc["forge"]["kind"] == "github", doc["tracker"]["kind"] == "github-issues"


def gh_checks(doc, env=None):
    """(forge, tracker) check results of a loaded harness. Inside the OS sandbox the gh
    checks are skipped, since GitHub is closed there by design."""
    from . import config, forges, trackers
    skipped = {"ok": None, "detail": SKIPPED_IN_SANDBOX}
    forge_gh, tracker_gh = _gh_kinds(doc)
    inside = _in_os_sandbox(env)
    settings = doc["forge"]
    forge = {"kind": settings["kind"]}
    if inside and forge_gh:
        forge.update(skipped)
    else:
        try:
            workspace = config.section("claude").get("workspace") or str(Path.home())
            forge.update(forges.get(settings["kind"]).doctor_check(
                settings, claude_home().parent / ".claude.json", workspace))
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


def managed_settings_path():
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def probe_github(env):
    """"denied" on a 403 and "open" on a 200 from a CONNECT to api.github.com:443 through the
    env's HTTPS_PROXY, else "unknown". The proxy URL is read here and never returned."""
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
    words = data.split(b"\n", 1)[0].split()
    status = words[1] if len(words) > 1 else b""
    return {b"403": "denied", b"200": "open"}.get(status, "unknown")


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


def _undoing(cwd):
    base = Path(cwd or os.environ.get("PPL_LOCAL_SETTINGS_DIR") or os.getcwd())
    sources = (global_settings_path(), base / ".claude" / "settings.json",
               local_settings_path(cwd), managed_settings_path())
    found = []
    for path in sources:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        block = doc.get("sandbox") if isinstance(doc, dict) else None
        if not isinstance(block, dict):
            continue
        if block.get("excludedCommands"):
            found.append("%s sets sandbox.excludedCommands" % path)
        if block.get("enabled") is False:
            found.append("%s sets sandbox.enabled to false" % path)
        if block.get("allowUnsandboxedCommands") is False:
            found.append("%s sets sandbox.allowUnsandboxedCommands to false" % path)
    return found


def os_sandbox_facts(cwd=None, env=None, probe=None):
    """Whether Claude Code's OS sandbox closes GitHub for this session. `state` is "on" only
    with the env marker, ppl's profile in the workspace local settings, a denied live probe
    and nothing undoing it. Nothing here carries the proxy URL."""
    from . import os_sandbox
    env = dict(os.environ) if env is None else env
    marker = env.get("SANDBOX_RUNTIME") == "1"
    try:
        recorded = os_sandbox.load_facts() or {}
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
    undoing = _undoing(cwd)
    if windows:
        state = "unavailable"
    elif marker and profile and live == "denied" and not undoing:
        state = "on"
    else:
        state = "off"
    return {"state": state, "env_marker": marker, "profile": profile, "live": live,
            "undoing": undoing, "exposed": _exposed(recorded, env)}


def report(task_id=None, cwd=None, env=None, probe=None):
    """What `ppl doctor` prints as JSON: the hook registration, the setup
    report, the active run's isolation and whether the registered guard runs.
    `cwd` is the directory whose .claude/settings.local.json is read, the
    process cwd by default."""
    from . import run
    inspected = inspect(cwd)
    task_id = task_id or run.active()
    isolated = None
    if task_id:
        try:
            isolated = run.load(task_id).get("isolated")
        except PplError:
            task_id = None
    result = dict(inspected)
    result.update(inspect_setup(env))
    result["os_sandbox"] = os_sandbox_facts(cwd, env, probe)
    result["task"] = task_id
    result["isolated"] = isolated
    result["mismatch"] = mismatch(isolated, inspected) if isolated is not None else None
    runs = hook_runs(cwd=cwd, workspace=workspace())
    result["hook_runs"] = runs["ok"]
    if runs["problem"]:
        result["hook_runs_problem"] = runs["problem"]
    return result

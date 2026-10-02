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
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .errors import PplError

GUARD_MARKER = "ppl_guard.py"

# The matcher README.md "Hooks" registers the guard on: match everything, let the hook
# decide. An explicit tool list is fragile (a tracker prefix it does not name, a read
# tool it leaves out), and each gap is a denial that can never fire behind a doctor
# that says "registered".
# `matcher_covers` still judges any explicit matcher an operator registers instead
# against `guarded_tool_names()`, and a test holds this string to that same check.
RECOMMENDED_MATCHER = ".*"


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


def _run_problem(command, workspace):
    """None when `command` takes the synthetic payload to exit 0 with no stderr,
    else what went wrong, with the first stderr line."""
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/dev/null"},
                          "cwd": workspace})
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
    process cwd). Kept out of `inspect`, which `ppl init` calls on every run."""
    commands = registered_guard_commands(cwd)
    if not commands:
        return {"ok": None, "problem": None}
    workspace = str(workspace or cwd or os.getcwd())
    for command in commands:
        problem = _run_problem(command, workspace)
        if problem:
            message = "the registered guard hook `%s` %s" % (command, problem.rstrip("."))
            # The interpreter floor's own line already names the remedy.
            if HOOK_RUN_REMEDY not in problem:
                message += ". Run `%s` to register it with the interpreter that runs ppl" \
                    % HOOK_RUN_REMEDY
            return {"ok": False, "problem": message + "."}
    return {"ok": True, "problem": None}


def guarded_tool_names():
    """Every tool name the guard hook acts on, read from the hook file itself
    (hooks/ppl_guard.py:GUARDED_TOOL_NAMES) so this check cannot drift from the cage."""
    hooks_dir = str(Path(__file__).resolve().parents[1] / "hooks")
    if hooks_dir not in sys.path:
        # Appended, not inserted: the hooks directory must never shadow a ppl module.
        sys.path.append(hooks_dir)
    import ppl_guard
    return tuple(ppl_guard.GUARDED_TOOL_NAMES)


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
    """
    def result(state, detail=None, uncovered=None):
        return {"state": state, "detail": detail, "uncovered": uncovered}

    if not path.exists():
        return result("no")
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
    matchers = _guard_matchers(doc)
    if not matchers:
        return result("no")
    return result("yes", uncovered=uncovered_by(matchers))


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


def inspect_setup():
    """What `ppl setup` left behind, for `ppl doctor` to report: the roots, the
    recorded steps (`setup_state`), the model rows, the active harness and its
    forge check.

    It never raises. Whatever cannot be read (a broken config.toml, a missing
    routing file, an invalid harness, a forge check that blows up) lands in the
    report as a problem string instead: `ppl doctor` reports, `ppl init` refuses.
    `config_problem`, `models_problem` and `code_root_problem` are present only when
    there is one.
    """
    # ppl.setup imports this module, so these stay function-local
    from . import config, forges, harness, models, paths
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
    if entry["active"]:
        doc = None
        try:
            doc = harness.load(entry["active"])
            repo_exists = Path(doc["repo"]["path"]).is_dir()
            entry.update(path=doc["_dir"], valid=True, repo_exists=repo_exists)
        except Exception as exc:
            entry["problem"] = _problem(exc)
        if entry["valid"]:
            settings = doc["forge"]
            forge = {"kind": settings["kind"]}
            try:
                workspace = config.section("claude").get("workspace") or str(Path.home())
                check = forges.get(settings["kind"]).doctor_check(
                    settings, claude_home().parent / ".claude.json", workspace)
                forge.update(check)
            except Exception as exc:
                forge.update(ok=False, detail="the forge check failed: %s" % _problem(exc))
    report["harness"] = entry
    report["forge"] = forge
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

"""ppl setup: the onboarding wizard. Six steps run in order, each skippable
and re-runnable, and config.toml [setup] records every step as it finishes,
so a quit resumes at the first step not done. Interactive answers come from
`read`, non-interactive ones from `answers`, both through Prompter. Every
file the wizard writes is a plain file the user can edit afterwards."""
import copy
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

from . import config, doctor, forges, harness, models, paths, routing, tomlout
from .errors import PplError

STEPS = ("paths", "claude", "models", "harness", "forge", "apply")
ANSWER_KEYS = ("claude_binary", "workspace", "hooks", "verify", "harness_from", "harness_name",
               "replace_harness", "repo", "default_branch", "task_url_template", "key_pattern",
               "forge", "gitlab_mcp_server")
TIER_PREFIX = "tier."
HOOK_MODES = ("local", "global", "manual")
EXAMPLE_HARNESSES = ("example-generic", "example-maven")
DISPATCH_MARKER = "ppl_dispatch.py"
_HOOK_TIMEOUT_SECONDS = 5
_YES, _NO = ("yes", "y"), ("no", "n")
_SKIP, _QUIT = "s", "q"
STEP_INTROS = {
    "paths": "Creates the data root and copies the example config.toml and routing.toml into it.",
    "claude": "Names the Claude Code binary and an isolated workspace, and registers the two hooks.",
    "models": "Picks a model per tier and proves each one answers with a cheap probe.",
    "harness": "Installs the project harness: build gates, guard shapes, task key and tracker.",
    "forge": "Chooses the git forge and checks that ppl can reach it.",
    "apply": "Renders the agent files and the orchestrator skill for the harness.",
}


class Quit(Exception):
    pass


class Skip(Exception):
    pass


class Failed(Exception):
    """The step did not complete, for the reason given. The wizard goes on."""


class Declined(Failed):
    """The user declined work whose result is already in place: an earlier
    done stays."""


class Prompter:
    """One question at a time, keyed by its answer key. A given answer always
    wins. Non-interactive, a missing answer takes the default unless the
    question is `required` or has none, then the run stops with
    SETUP_INCOMPLETE naming the key. Interactive, Enter takes the default,
    `s` skips the step and `q` (or the end of input) quits the wizard."""

    def __init__(self, answers, non_interactive, read, emit):
        self.answers = answers or {}
        self.non_interactive = non_interactive
        self.read = read
        self.emit = emit
        self.current_step = ""

    def ask(self, key, prompt, default="", required=False, choices=None):
        if key in self.answers:
            return self._checked(key, str(self.answers[key]).strip(), choices)
        if self.non_interactive:
            if required or not default:
                raise PplError("SETUP_INCOMPLETE",
                               "no answer for %r (%s): pass --answer %s=<value> or run "
                               "ppl setup interactively" % (key, prompt, key))
            return self._checked(key, default, choices, "default")
        shown = "%s: %s" % (self.current_step, prompt)
        if default:
            shown += " [%s]" % default
        shown += " (s=skip step, q=quit): "
        while True:
            try:
                raw = self.read(shown).strip()
            except EOFError:
                raise Quit()
            if raw == _QUIT:
                raise Quit()
            if raw == _SKIP:
                raise Skip()
            value = raw or default
            if not value:
                continue
            if choices and value.lower() not in choices:
                self.emit("choose one of: %s" % ", ".join(choices))
                continue
            return value.lower() if choices else value

    def _checked(self, key, value, choices, what="answer"):
        if not value:
            raise PplError("SETUP_INCOMPLETE", "%s %s is empty" % (what, key))
        if choices and value.lower() not in choices:
            raise PplError("SETUP_INCOMPLETE", "%s %s=%r is not one of: %s"
                           % (what, key, value, ", ".join(choices)))
        return value.lower() if choices else value


def hook_entries(code_root):
    """The PreToolUse guard entry and the SubagentStart dispatch entry, both
    on absolute paths into the code checkout, run by the interpreter running
    this setup. Bare python3 is whatever the PATH Claude Code spawns hooks with
    finds first, and one older than 3.11 cannot run the hooks."""
    hooks_dir = Path(os.path.abspath(code_root)) / "hooks"

    def entry(matcher, script):
        command = "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(hooks_dir / script)))
        return {"matcher": matcher,
                "hooks": [{"type": "command", "command": command, "timeout": _HOOK_TIMEOUT_SECONDS}]}
    return {"PreToolUse": entry(doctor.RECOMMENDED_MATCHER, doctor.GUARD_MARKER),
            "SubagentStart": entry("", DISPATCH_MARKER)}


_MARKERS = {"PreToolUse": doctor.GUARD_MARKER, "SubagentStart": DISPATCH_MARKER}


def _is_ours(hook, marker):
    return isinstance(hook, dict) and marker in str(hook.get("command", ""))


def _bad_settings(path, why):
    raise PplError("SETUP_INCOMPLETE", "%s %s, fix or remove it and run ppl setup claude again"
                   % (path, why))


def write_local_settings(workspace, code_root):
    """Merge the two hook entries into <workspace>/.claude/settings.local.json.
    A guard or dispatch hook already there, matched by its script name, is
    replaced rather than duplicated. Every other key, entry and hook stays."""
    path = Path(workspace) / ".claude" / "settings.local.json"
    doc = {}
    if path.is_file():
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _bad_settings(path, "could not be read as JSON (%s)" % exc)
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        _bad_settings(path, "is not a JSON object with a hooks table")
    hooks = doc.setdefault("hooks", {})
    for event, own in hook_entries(code_root).items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list) or not all(
                isinstance(e, dict) and isinstance(e.get("hooks", []), list) for e in existing):
            _bad_settings(path, "has a hooks.%s that is not a list of entries with hook lists" % event)
        marker = _MARKERS[event]
        kept = []
        for entry in existing:
            others = [h for h in entry.get("hooks", []) if not _is_ours(h, marker)]
            if len(others) == len(entry.get("hooks", [])):
                kept.append(entry)
            elif others:
                kept.append(dict(entry, hooks=others))
        hooks[event] = kept + [own]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    return path


def _derived_dir(current, key, old_workspace, workspace, *parts):
    """The workspace-relative agents or skills dir, unless the user pointed
    config.toml somewhere else by hand: that value is kept."""
    value = current.get(key) or ""
    if not value or (old_workspace and value == str(Path(old_workspace, *parts))):
        return str(Path(workspace, *parts))
    return value


def _step_paths(p):
    config.ensure()
    p.emit("code root: %s" % paths.root())
    p.emit("data root: %s" % paths.home())


def _step_claude(p):
    current = config.section("claude")
    binary = p.ask("claude_binary", "Claude Code binary", current.get("binary") or "claude")
    configured = current.get("workspace") or ""
    answer = p.ask("workspace", "Isolated workspace directory for pipeline sessions",
                   configured or str(Path.home() / "ppl-workspace"), required=not configured)
    workspace = Path(os.path.abspath(Path(answer).expanduser()))
    hooks = p.ask("hooks", "Register the hooks how? local (write the workspace settings), global "
                  "or manual (print the entries for you to paste)", current.get("hooks") or "local",
                  choices=HOOK_MODES)
    workspace.mkdir(parents=True, exist_ok=True)
    if hooks == "local":
        p.emit("hooks registered in %s" % write_local_settings(workspace, paths.root()))
    else:
        where = doctor.global_settings_path() if hooks == "global" else "the settings file you choose"
        p.emit("add these entries to %s:" % where)
        p.emit(json.dumps({"hooks": {event: [entry] for event, entry in
                                     hook_entries(paths.root()).items()}}, indent=2))
    config.update("claude", {
        "binary": binary, "workspace": str(workspace), "hooks": hooks,
        "agents_dir": _derived_dir(current, "agents_dir", configured, workspace, ".claude", "agents"),
        "skills_dir": _derived_dir(current, "skills_dir", configured, workspace, ".claude", "skills")})


def _step_models(p, probe):
    config.ensure()
    doc = routing.load()
    tiers = routing.tiers(doc)
    unknown = sorted(key[len(TIER_PREFIX):] for key in p.answers
                     if key.startswith(TIER_PREFIX) and key[len(TIER_PREFIX):] not in tiers)
    if unknown:
        raise PplError("SETUP_INCOMPLETE", "unknown tier %s, routing.toml has: %s"
                       % (", ".join(unknown), ", ".join(sorted(tiers))))
    changed = False
    for tier, entry in sorted(tiers.items()):
        model = p.ask(TIER_PREFIX + tier, "Model for tier %s" % tier, entry["model"])
        if model != entry["model"]:
            entry["model"] = model
            changed = True
    if changed:
        paths.routing_file().write_text(tomlout.dumps(doc), encoding="utf-8")
    verify = p.ask("verify", "Verify the models now, one cheap probe per model? yes/no", "yes",
                   choices=_YES + _NO)
    binary = config.section("claude").get("binary") or "claude"
    if verify in _NO:
        # Declining keeps an earlier done only while every routed model is
        # still verified, a changed tier is not.
        if all(row["state"] == "verified" for row in models.status(binary=binary)):
            raise Declined("verification declined, every routed model is already verified")
        raise Failed("models not verified: ppl init refuses until `ppl models verify` passes")
    # Every tier this step just configured is probed, a tier no role routes to
    # today included, plus any model a role names directly. ppl init still
    # checks only the models a run would use.
    configured = {entry["model"] for entry in tiers.values()} | set(routing.models_in_use())
    report = models.verify(sorted(configured), force=True, runner=probe, binary=binary)
    for row in models.status(binary=binary, all_tiers=True):
        p.emit("%-8s %-14s %-32s %s" % (row["tier"] or "-", row["model"],
                                       row["resolved_id"] or "-", row["state"]))
    if report["failed"]:
        details = report.get("details") or {}
        raise Failed("probe failed for %s" % ", ".join(
            "%s (%s)" % (model, details.get(model) or "no detail") for model in report["failed"]))
    p.emit("verified, total cost %.4f USD" % report["cost_usd"])


def _pending_harness():
    for name in (config.section("harness").get("pending"), config.active_harness()):
        if isinstance(name, str) and name in harness.list_names():
            return name
    return None


_FRAGMENT_GLOBS = ("skill/*.md", "templates/*.part")


def _fragments(directory):
    root = Path(directory)
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for pattern in _FRAGMENT_GLOBS for path in sorted(root.glob(pattern)) if path.is_file()}


def _same_harness(candidate, current):
    """The same document, skill fragments and role templates. The directory
    each one was loaded from is not part of it."""
    return (tomlout.dumps(candidate) == tomlout.dumps(current)
            and _fragments(candidate["_dir"]) == _fragments(current["_dir"]))


def _is_installed_source(source, name):
    if source == name:
        return True
    candidate = Path(source).expanduser()
    target = paths.harness_dir(name)
    return candidate.is_dir() and target.is_dir() and os.path.samefile(candidate, target)


def _resolve_source(source, installed):
    if source in EXAMPLE_HARNESSES:
        return str(paths.defaults_dir() / "harnesses" / source)
    if source in installed:
        return str(paths.harness_dir(source))
    return source


def _write_harness(doc, before):
    if doc != before:
        (Path(doc["_dir"]) / "harness.toml").write_text(tomlout.dumps(doc), encoding="utf-8")


def _apply_answers(p, doc, name):
    doc["harness"]["name"] = name
    repo = doc["repo"]
    old_path = repo["path"]
    answer = p.ask("repo", "Path of the main checkout", old_path,
                   required=not Path(old_path).expanduser().is_dir())
    repo["path"] = os.path.abspath(Path(answer).expanduser())
    repo["default_branch"] = p.ask("default_branch", "Default branch", repo["default_branch"])
    if repo["path"] != old_path or not repo.get("worktrees"):
        repo["worktrees"] = repo["path"].rstrip("/") + "/.worktrees"
    task = doc["task"]
    task["url_template"] = p.ask("task_url_template", "Task URL, <TASK-ID> as the placeholder",
                                 task["url_template"])
    task["key_pattern"] = p.ask("key_pattern", "Task key regex", task["key_pattern"])


def _load_installed(name):
    """The installed harness, or None with the reason it does not load."""
    try:
        return harness.load(name), None
    except PplError as exc:
        return None, exc.message


def _keep_installed(p, candidate, name, current, problem):
    """An installed harness stays when the candidate is identical to it.
    Otherwise rebuilding would drop every gate and shape added by hand, so a
    non-interactive run refuses unless it was given replace_harness, and an
    interactive one asks. An installed harness that does not load counts as
    different."""
    if current is not None and _same_harness(candidate, current):
        p.emit("harness %s is already installed and identical, kept" % name)
        return True
    if p.non_interactive and "replace_harness" not in p.answers:
        if problem:
            raise PplError("SETUP_INCOMPLETE", "installed harness %s is not valid (%s) - pass "
                           "harness_from=%s after fixing it, or let the wizard replace it"
                           % (name, problem, name))
        raise PplError("SETUP_INCOMPLETE", "harness %s is already installed, pass harness_from=%s "
                       "to keep it or remove it first" % (name, name))
    keep = p.ask("replace_harness", "Replace installed harness %s? yes/no" % name, "no",
                 choices=_YES + _NO) in _NO
    if keep:
        p.emit("kept the installed harness %s as it is" % name)
    return keep


def _install(p, source, name, installed):
    """Stage `source` beside the installed harnesses and apply the answers
    there. It moves into place only once it loaded, validated and was not
    kept out, so a bad source or a refusal leaves the installed one as it was."""
    target = paths.harness_dir(name)
    staging = paths.harness_dir("%s.replacing" % name)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        harness.add(source, name=staging.name, staging=True)
        candidate = harness.load(staging.name)
        before = copy.deepcopy(candidate)
        _apply_answers(p, candidate, name)
        current, problem = _load_installed(name) if installed else (None, None)
        # The forge step rewrites [forge] on the installed copy, so the
        # candidate takes the same forge answers before the compare. Without
        # an answer the installed forge is kept, and nothing is asked here.
        quiet = Prompter(p.answers, True, None, p.emit)
        _apply_forge(quiet, candidate["forge"], (current or candidate)["forge"])
        harness.validate(candidate, name)
        if installed and _keep_installed(p, candidate, name, current, problem):
            return
        _write_harness(candidate, before)
        if not target.exists():
            staging.rename(target)
            return
        retired = paths.harness_dir("%s.replaced" % name)
        shutil.rmtree(retired, ignore_errors=True)
        target.rename(retired)
        try:
            staging.rename(target)
        except OSError:
            retired.rename(target)
            raise
        shutil.rmtree(retired)
        p.emit("replaced the installed harness %s" % name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _check_source(source, installed):
    """Refuse a source that is not an example or installed harness name, a git
    URL or an absolute path: a relative one would resolve against the working
    directory of whatever runs setup, ppl serve included."""
    if source in EXAMPLE_HARNESSES or source in installed or harness.is_git_source(source):
        return
    try:
        absolute = Path(source).expanduser().is_absolute()
    except RuntimeError:
        absolute = False
    if not absolute:
        raise PplError("SCHEMA_INVALID", "harness source %r must be %s, an installed harness, "
                       "a git URL or an absolute path" % (source, " or ".join(EXAMPLE_HARNESSES)))


def _step_harness(p):
    installed = harness.list_names()
    source = p.ask("harness_from",
                   "Source (example-generic, example-maven, an absolute directory, or a git URL)",
                   _pending_harness() or EXAMPLE_HARNESSES[0])
    _check_source(source, installed)
    name = p.ask("harness_name", "Name for this harness",
                 Path(source.rstrip("/")).name.removesuffix(".git"))
    harness.check_name(name)
    if name in installed and _is_installed_source(source, name):
        doc = harness.load(name)
        before = copy.deepcopy(doc)
        _apply_answers(p, doc, name)
        harness.validate(doc, name)
        _write_harness(doc, before)
    else:
        _install(p, _resolve_source(source, installed), name, name in installed)
    doc = harness.load(name)
    warnings = harness.validate(doc, name)
    config.update("harness", {"pending": name})
    p.emit("harness %s at %s" % (name, doc["_dir"]))
    if not Path(doc["repo"]["path"]).is_dir():
        p.emit("warning: %s does not exist yet" % doc["repo"]["path"])
    for warning in warnings:
        p.emit("warning: %s" % warning)


def _target_harness():
    name = _pending_harness()
    if not name:
        raise Failed("no harness configured, run `ppl setup harness` first")
    return name


def _apply_forge(p, forge, offered):
    """[forge] from the forge answers, `offered` holding the kind and server
    proposed when there is none. A github forge carries no GitLab server."""
    forge["kind"] = p.ask("forge", "Git forge: gitlab or github", offered["kind"],
                          choices=harness.FORGES)
    if forge["kind"] == "gitlab":
        forge["mcp_server"] = p.ask("gitlab_mcp_server", "GitLab MCP server name as registered "
                                    "in Claude Code", offered.get("mcp_server") or "GitLab-MCP")
    else:
        forge.pop("mcp_server", None)


def _step_forge(p, forge_check):
    name = _target_harness()
    doc = harness.load(name)
    before = copy.deepcopy(doc)
    forge = doc["forge"]
    _apply_forge(p, forge, dict(forge))
    harness.validate(doc, name)
    _write_harness(doc, before)
    workspace = config.section("claude").get("workspace") or str(Path.home())
    claude_json = doctor.claude_home().parent / ".claude.json"
    result = forge_check(dict(forge), claude_json, workspace)
    line = "%s: %s" % (forge["kind"], result.get("detail", ""))
    if not result.get("ok"):
        raise Failed(line)
    p.emit(line)


def _step_apply(p):
    name = _target_harness()
    result = harness.apply(name)
    doc = config.load()
    doc["harness"].pop("pending", None)
    config.save(doc)
    p.emit("rendered %d agent files into %s and the skill into %s for harness %s"
           % (len(result["agents"]), config.agents_dir(), config.skills_dir(), result["active"]))


def _default_forge_check(settings, claude_json, workspace):
    return forges.get(settings["kind"]).doctor_check(settings, claude_json, workspace)


def _check_steps(names, what):
    unknown = [name for name in names if name not in STEPS]
    if unknown:
        raise PplError("SCHEMA_INVALID", "unknown %s step: %s (steps: %s)"
                       % (what, ", ".join(unknown), ", ".join(STEPS)))


def run(steps=None, answers=None, non_interactive=False, read=input, emit=print,
        probe=None, forge_check=None, skip=()):
    """Run the named steps, or every step not done yet (all of them when
    every one is done), in STEPS order. Returns the steps done, skipped and
    failed, the reason each failed step gave, and whether the user quit."""
    _check_steps(steps or [], "setup")
    _check_steps(skip, "--skip")
    probe = probe or models.probe
    forge_check = forge_check or _default_forge_check
    p = Prompter(answers, non_interactive, read, emit)
    handlers = {"paths": lambda: _step_paths(p), "claude": lambda: _step_claude(p),
                "models": lambda: _step_models(p, probe), "harness": lambda: _step_harness(p),
                "forge": lambda: _step_forge(p, forge_check), "apply": lambda: _step_apply(p)}
    if steps:
        todo = [step for step in STEPS if step in steps]
    else:
        done = config.steps_state()["done"]
        todo = [step for step in STEPS if step not in done] or list(STEPS)
    result = {"done": [], "skipped": [], "failed": [], "reasons": {}, "quit": False}
    for step in todo:
        p.current_step = step
        emit("== %s" % step)
        if step in skip:
            outcome, reason = "skipped", None
        else:
            outcome, reason = _attempt(step, handlers[step], emit)
        if outcome == "quit":
            result["quit"] = True
            break
        done_before = step in config.steps_state()["done"]
        if outcome == "declined":
            outcome = "skipped" if done_before else "failed"
        if outcome == "skipped":
            # Skipping a step done earlier leaves it as it is, done.
            if not done_before:
                _record(step, "skipped")
        else:
            # A failed re-run takes back an earlier done.
            _record(step, "done" if outcome == "done" else None)
        result[outcome].append(step)
        if outcome == "failed":
            result["reasons"][step] = reason
    return result


def _attempt(step, handler, emit):
    """(outcome, reason): done, failed, skipped, declined or quit, and why it
    failed. A file the step cannot read or write fails the step, not the
    wizard, so the result still prints."""
    emit(STEP_INTROS[step])
    try:
        handler()
        return "done", None
    except Skip:
        return "skipped", None
    except Quit:
        return "quit", None
    except Declined as exc:
        emit(str(exc))
        return "declined", str(exc)
    except Failed as exc:
        emit(str(exc))
        return "failed", str(exc)
    except OSError as exc:
        emit(_cannot_access(exc))
        return "failed", _cannot_access(exc)


def _cannot_access(exc):
    return "cannot read or write %s: %s" % (exc.filename or "a file", exc.strerror or exc)


def _record(step, state):
    try:
        config.mark_step(step, state)
    except OSError as exc:
        raise PplError("SETUP_INCOMPLETE", _cannot_access(exc))

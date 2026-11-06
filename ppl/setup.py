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
import subprocess
import sys
import tempfile
from pathlib import Path

from . import (config, doctor, escape_git, forges, harness, lane_harness, models, os_sandbox,
               paths, routing, tomlout, trackers)
from .errors import PplError

STEPS = ("paths", "claude", "models", "harness", "forge", "apply")
ANSWER_KEYS = ("claude_binary", "verify", "harness_from", "harness_name", "replace_harness",
               "repo", "default_branch", "task_url_template", "key_pattern", "forge",
               "gitlab_mcp_server", "tracker", "tracker_repo")
TIER_PREFIX = "tier."
EXAMPLE_HARNESSES = ("example-generic", "example-maven")
DISPATCH_MARKER = doctor.DISPATCH_MARKER
USAGE_MARKER = doctor.USAGE_MARKER
_HOOK_TIMEOUT_SECONDS = 5
_YES, _NO = ("yes", "y"), ("no", "n")
_SKIP, _QUIT = "s", "q"
STEP_INTROS = {
    "paths": "Creates the data root and copies the example config.toml and routing.toml into it.",
    "claude": "Names the Claude Code binary, and takes back what an older ppl wrote into its "
              "workspace.",
    "models": "Picks a model per tier and proves each one answers with a cheap probe.",
    "harness": "Installs the project harness: build gates, guard shapes, task key and tracker.",
    "forge": "Chooses the git forge and the issue tracker and checks that ppl can reach both.",
    "apply": "Checks the harness at its commit and makes it the default for ppl lane.",
}
# The [claude] keys of the single workspace an older ppl rendered into, retired with it (D18).
LEGACY_CLAUDE_KEYS = ("workspace", "hooks", "agents_dir", "skills_dir")


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


def hook_entries(code_root, task_id=None):
    """The PreToolUse guard entry, the SubagentStart dispatch entry and the
    SubagentStop usage entry, each on an absolute path into the assets
    directory of the installed package or the clone, run by the interpreter
    running this setup. With `task_id` each command ends in ` --task <id>`, the run a lane's
    hooks belong to. Bare python3 is whatever the PATH Claude Code spawns
    hooks with finds first, and one older than 3.11 cannot run the hooks."""
    hooks_dir = Path(os.path.abspath(code_root)) / "hooks"

    def entry(matcher, script):
        command = "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(hooks_dir / script)))
        if task_id is not None:
            command += " --task %s" % shlex.quote(task_id)
        return {"matcher": matcher,
                "hooks": [{"type": "command", "command": command, "timeout": _HOOK_TIMEOUT_SECONDS}]}
    return {"PreToolUse": entry(doctor.RECOMMENDED_MATCHER, doctor.GUARD_MARKER),
            "SubagentStart": entry("", DISPATCH_MARKER),
            "SubagentStop": entry("", USAGE_MARKER)}


_MARKERS = {"PreToolUse": doctor.GUARD_MARKER, "SubagentStart": DISPATCH_MARKER,
            "SubagentStop": USAGE_MARKER}


def _is_ours(hook, marker):
    """A hook whose command runs ppl's `marker` script: one of its words has that base name."""
    command = hook.get("command") if isinstance(hook, dict) else None
    try:
        words = shlex.split(command) if isinstance(command, str) else []
    except ValueError:
        return False
    return any(os.path.basename(word) == marker for word in words)


def _replace_file(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _recorded_facts():
    """The guard facts an older `ppl harness apply` wrote to PPL_HOME, {} when there are none
    or they cannot be read: then no sandbox block or allow entry counts as ppl's."""
    path = paths.home() / os_sandbox.FACTS_FILE
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _without_ppl_hooks(hooks):
    """`hooks` with every guard, dispatch and usage hook taken out, matched by ppl's script
    names, an entry left empty dropped, and what was taken out."""
    removed = []
    for event, marker in _MARKERS.items():
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            ours = [hook for hook in inner if _is_ours(hook, marker)] \
                if isinstance(inner, list) else []
            if not ours:
                kept.append(entry)
                continue
            removed += ["the %s hook %s" % (event, hook.get("command")) for hook in ours]
            others = [hook for hook in inner if not _is_ours(hook, marker)]
            if others:
                kept.append(dict(entry, hooks=others))
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)
    return removed


def _clean_settings(path, recorded):
    """Take ppl's hook entries out of an old workspace's settings.local.json, with the sandbox
    block only when it equals the one apply recorded and the allow entries apply recorded.
    Everything else in the file stays. Returns (removed, left): a file that is a symbolic link
    (a dotfiles checkout, say) is left as it is, and what ppl would have removed from it is
    named in `left` for the owner to remove by hand. Failed, with the file untouched, when it
    is no JSON object."""
    if not path.is_file():
        return [], []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Failed("%s could not be read as JSON (%s): fix or remove it, then run ppl setup "
                     "claude again" % (path, exc))
    if not isinstance(doc, dict):
        raise Failed("%s is not a JSON object: fix or remove it, then run ppl setup claude "
                     "again" % path)
    removed = []
    hooks = doc.get("hooks")
    if isinstance(hooks, dict):
        removed += _without_ppl_hooks(hooks)
        if not hooks:
            doc.pop("hooks")
    written = recorded.get("written_sandbox")
    if written is not None and doc.get("sandbox") == written:
        doc.pop("sandbox")
        removed.append("the sandbox block ppl wrote")
    allowed = recorded.get("written_allow")
    permissions = doc.get("permissions")
    if isinstance(allowed, list) and isinstance(permissions, dict) \
            and isinstance(permissions.get("allow"), list):
        ours = [entry for entry in permissions["allow"] if entry in allowed]
        removed += ["the permissions.allow entry %s" % entry for entry in ours]
        permissions["allow"] = [entry for entry in permissions["allow"] if entry not in allowed]
        if not permissions["allow"]:
            permissions.pop("allow")
        if not permissions:
            doc.pop("permissions")
    if removed and path.is_symlink():
        return [], ["%s as it is, a link to %s: remove %s there by hand"
                    % (path, os.path.realpath(path), "; ".join(removed))]
    if removed:
        _replace_file(path, doc)
    return ["%s from %s" % (item, path) for item in removed], []


def _generated_agent(path):
    """True for an agent file an older `ppl harness apply` wrote: the notice right after the
    opening ---, where routing.render puts it."""
    try:
        head = path.read_text(encoding="utf-8").split("\n", 2)[:2]
    except (OSError, UnicodeDecodeError):
        return False
    return head[:1] == ["---"] and head[1:] in ([routing.GENERATED_NOTICE],
                                                [routing.LEGACY_GENERATED_NOTICE])


def _clean_agents(directory):
    removed = []
    for path in sorted(Path(directory).glob("ppl-*.md")):
        if path.is_file() and not path.is_symlink() and _generated_agent(path):
            path.unlink()
            removed.append(str(path))
    return removed


def _clean_skill(directory):
    """Remove the rendered pipeline skill directory when its SKILL.md carries the notice right
    after the frontmatter, where harness.render_skill puts it."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return []
    try:
        text = (directory / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    tail = text.partition("\n---\n")[2]
    if not any(tail.startswith(notice + "\n")
               for notice in (harness.SKILL_NOTICE, harness.LEGACY_SKILL_NOTICE)):
        return []
    shutil.rmtree(directory)
    return [str(directory)]


def _old_dir(current, key, workspace, kind):
    value = current.get(key)
    if isinstance(value, str) and value:
        return Path(value).expanduser()
    return Path(workspace) / ".claude" / kind if workspace else None


def clean_old_workspace():
    """Take back what an older ppl wrote for the single workspace config.toml names (D18): its
    hook entries, sandbox block and allow entries in <workspace>/.claude/settings.local.json,
    the rendered pipeline skill and the ppl-* agent files, each matched by ppl's own markers,
    and PPL_HOME/os_sandbox.json, then the [claude] workspace, hooks, agents_dir and skills_dir
    keys. Returns (removed, left, kept): every path or entry removed, what was left for the
    owner (a settings file that is a link), and why nothing was removed while a run from
    before lanes is not finished (lane_launch._legacy_run_problem): its session still runs
    under those hooks and that skill."""
    from . import lane_launch
    current = config.section("claude")
    facts = paths.home() / os_sandbox.FACTS_FILE
    if not any(key in current for key in LEGACY_CLAUDE_KEYS) and not os.path.lexists(facts):
        return [], [], None
    try:
        problem = lane_launch._legacy_run_problem()
    except PplError as exc:
        problem = "the run runs/ACTIVE names cannot be read: %s" % exc.message
    if problem:
        return [], [], problem
    workspace = current.get("workspace") if isinstance(current.get("workspace"), str) else ""
    removed, left = [], []
    if workspace:
        removed, left = _clean_settings(Path(workspace) / ".claude" / "settings.local.json",
                                        _recorded_facts())
    agents = _old_dir(current, "agents_dir", workspace, "agents")
    if agents is not None:
        removed += _clean_agents(agents)
    skills = _old_dir(current, "skills_dir", workspace, "skills")
    if skills is not None:
        removed += _clean_skill(skills / "pipeline")
    if os.path.lexists(facts):
        facts.unlink()
        removed.append(str(facts))
    config.remove("claude", LEGACY_CLAUDE_KEYS)
    return removed, left, None


def _step_paths(p):
    config.ensure()
    p.emit("code root: %s" % paths.root())
    p.emit("data root: %s" % paths.home())


def _step_claude(p):
    current = config.section("claude")
    binary = p.ask("claude_binary", "Claude Code binary", current.get("binary") or "claude")
    config.update("claude", {"binary": binary})
    removed, left, kept = clean_old_workspace()
    if kept:
        p.emit("what an older ppl wrote for the workspace %s stays as it is: %s. Run ppl setup "
               "claude again once that run is finished or destroyed"
               % (current.get("workspace") or "-", kept))
    for item in removed:
        p.emit("removed %s" % item)
    for item in left:
        p.emit("left %s" % item)


def _step_models(p, probe):
    config.ensure()
    doc = routing.load(unfilled=True, path=paths.machine_routing_file())
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
        paths.machine_routing_file().write_text(tomlout.dumps(doc), encoding="utf-8")
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
    kept out, so a bad source or a refusal leaves the installed one as it was.
    A plain directory is imported into git (D8) after the answers are written, so
    the harness it installs is committed as it stands and `ppl harness apply` takes it.
    A git source keeps its own history, and answers that change it leave it for the
    owner to commit."""
    target = paths.harness_dir(name)
    staging = paths.harness_dir("%s.replacing" % name)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        harness.add(source, name=staging.name, staging=True)
        candidate = harness.load(staging.name)
        before = copy.deepcopy(candidate)
        _apply_answers(p, candidate, name)
        current, problem = _load_installed(name) if installed else (None, None)
        # The forge step rewrites [forge] and [tracker] on the installed copy, so
        # the candidate takes the same answers before the compare. Without an
        # answer the installed forge and tracker are kept, and nothing is asked here.
        quiet = Prompter(p.answers, True, None, p.emit)
        _apply_forge(quiet, candidate["forge"], (current or candidate)["forge"])
        _apply_tracker(quiet, candidate, (current or candidate)["tracker"])
        harness.validate(candidate, name)
        if installed and _keep_installed(p, candidate, name, current, problem):
            return
        _write_harness(candidate, before)
        lane_harness.import_directory(staging, name)
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


SUGGESTED_TRACKER = {"github": "github-issues", "gitlab": "jira"}
_GIT_TIMEOUT_SECONDS = 10


def _origin_repo(checkout):
    """The owner/name of the checkout's GitHub origin remote, or None. A role may write the
    checkout's config, so the read carries escape_git.OPERATOR_PINNED_CONFIG."""
    try:
        done = subprocess.run(["git", *escape_git.OPERATOR_PINNED_CONFIG, "-C", str(checkout),
                               "remote", "get-url", "origin"],
                              capture_output=True, text=True, timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return trackers.get("github-issues").repo_from_remote(done.stdout.strip())


def _offered_kind(p, offered, forge_kind):
    """The tracker kind a question defaults to. A non-interactive run keeps the current one.
    An interactive one suggests the forge's own tracker while the current tracker is still
    the untouched Jira example, and keeps any tracker the user already set up."""
    current = offered["kind"]
    if p.non_interactive or offered != dict(trackers.get(current).DEFAULTS, kind=current):
        return current
    return SUGGESTED_TRACKER[forge_kind]


def _apply_tracker(p, doc, offered):
    """[tracker] and [task] from the tracker answers, `offered` holding the table proposed
    when there is none. A kind change writes the new kind's table and its task defaults, a
    github-issues repo comes from the answer, the offered table or the checkout's origin."""
    kind = p.ask("tracker", "Issue tracker: %s" % " or ".join(trackers.KINDS),
                 _offered_kind(p, offered, doc["forge"]["kind"]), choices=trackers.KINDS)
    changed = kind != doc["tracker"]["kind"]
    table = doc["tracker"]
    if changed:
        table = dict(copy.deepcopy(trackers.get(kind).DEFAULTS), kind=kind)
    if kind == "github-issues":
        default = (offered.get("repo") if offered["kind"] == kind else None) \
            or _origin_repo(doc["repo"]["path"]) or ""
        repo = p.ask("tracker_repo", "Repository the GitHub issues live in, owner/name",
                     default, required=not default)
        table = dict(kind=kind, repo=repo, **{key: value for key, value in table.items()
                                              if key not in ("kind", "repo")})
    doc["tracker"] = table
    if changed:
        doc["task"].update(trackers.get(kind).task_defaults(table))


def _step_forge(p, forge_check, tracker_check):
    name = _target_harness()
    doc = harness.load(name)
    before = copy.deepcopy(doc)
    forge = doc["forge"]
    _apply_forge(p, forge, dict(forge))
    _apply_tracker(p, doc, copy.deepcopy(doc["tracker"]))
    harness.validate(doc, name)
    _write_harness(doc, before)
    claude_json = doctor.claude_home().parent / ".claude.json"
    checks = (("forge", forge["kind"], forge_check(dict(forge), claude_json, str(Path.home()))),
              ("tracker", doc["tracker"]["kind"], tracker_check(copy.deepcopy(doc["tracker"]))))
    lines = [("%s: %s" % (kind, result.get("detail", "")), result.get("ok"))
             for _, kind, result in checks]
    failed = [line for line, ok in lines if not ok]
    for line, ok in lines:
        if ok:
            p.emit(line)
    if failed:
        raise Failed("; ".join(failed))


def _step_apply(p):
    name = _target_harness()
    try:
        result = harness.apply(name)
    except PplError as exc:
        # an uncommitted change in the harness repository (OWNER_DECISION), say after an
        # in-place forge answer, fails this step with its reason and the wizard goes on
        raise Failed(exc.message)
    config.remove("harness", ["pending"])
    p.emit("harness %s at %s is the default for ppl lane" % (result["active"], result["commit"]))
    for warning in result["warnings"]:
        p.emit("warning: %s" % warning)


def _default_forge_check(settings, claude_json, workspace):
    return forges.get(settings["kind"]).doctor_check(settings, claude_json, workspace)


def _default_tracker_check(table):
    return trackers.get(table["kind"]).doctor_check(table)


def _check_steps(names, what):
    unknown = [name for name in names if name not in STEPS]
    if unknown:
        raise PplError("SCHEMA_INVALID", "unknown %s step: %s (steps: %s)"
                       % (what, ", ".join(unknown), ", ".join(STEPS)))


def run(steps=None, answers=None, non_interactive=False, read=input, emit=print,
        probe=None, forge_check=None, tracker_check=None, skip=()):
    """Run the named steps, or every step not done yet (all of them when
    every one is done), in STEPS order. Returns the steps done, skipped and
    failed, the reason each failed step gave, and whether the user quit."""
    _check_steps(steps or [], "setup")
    _check_steps(skip, "--skip")
    probe = probe or models.probe
    forge_check = forge_check or _default_forge_check
    tracker_check = tracker_check or _default_tracker_check
    p = Prompter(answers, non_interactive, read, emit)
    handlers = {"paths": lambda: _step_paths(p), "claude": lambda: _step_claude(p),
                "models": lambda: _step_models(p, probe), "harness": lambda: _step_harness(p),
                "forge": lambda: _step_forge(p, forge_check, tracker_check),
                "apply": lambda: _step_apply(p)}
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

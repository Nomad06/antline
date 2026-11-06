"""`ppl lane`: start a task window, resume it, list the running ones, and close one after its
merge. `start` reserves a registry slot, exports the harness at its commit into the lane's start
directory, creates the run's worktree and branch, renders the session files there and execs
claude with flags that load only those files. A failure after the export undoes, newest first,
exactly what the call created (the registry slot, the start directory, the worktree, the branch)
and re-raises: nothing it did not make is removed.

CLI-side only: no hook imports this module."""
import os
import re
import shutil
import sys
import uuid
from pathlib import Path

from . import (config, escape_git, harness, lane_harness, lane_render, lanes, lessons, os_sandbox,
               paths, routing, run, store, trackers, usage)
from .errors import PplError

VERBS = ("list", "resume", "close")
SKILL = "/antline:pipeline"
_SLUG = re.compile(r"[a-z0-9-]{1,40}")
_PLACEHOLDER = re.compile(r"(<TASK-ID-number>|<TASK-ID>)")
_GROUPS = {"<TASK-ID>": r"([A-Z][A-Z0-9]+-[0-9]+)", "<TASK-ID-number>": r"([0-9]+)"}
# What may follow the task in a link: a trailing slash, then a query or a fragment.
_LINK_TAIL = r"/?(?:[?#].*)?"


def _task_prefix(doc):
    tracker = doc.get("tracker") or {}
    prefix = tracker.get("task_prefix")
    if not prefix and tracker.get("kind") in trackers.KINDS:
        prefix = trackers.get(tracker["kind"]).DEFAULTS.get("task_prefix")
    if not (isinstance(prefix, str) and prefix):
        raise PplError("SCHEMA_INVALID", "task.url_template names <TASK-ID-number> and the "
                       "harness gives no tracker.task_prefix to put before it")
    return prefix


def task_from_link(doc, link):
    """The task id `link` names. A bare id matching the harness's task.key_pattern is returned
    as it is. Otherwise the link must have the form of task.url_template, where <TASK-ID> stands
    for the id and <TASK-ID-number> for its digits after tracker.task_prefix and a hyphen; a
    trailing slash, query or fragment may follow."""
    pattern = doc["task"]["key_pattern"]
    template = doc["task"]["url_template"]
    if not isinstance(link, str):
        link = ""
    if paths.TASK_ID_RE.match(link) and re.match(pattern, link):
        return link
    pieces = _PLACEHOLDER.split(template)
    placeholders = [piece for piece in pieces if piece in _GROUPS]
    regex = "".join(_GROUPS.get(piece) or re.escape(piece) for piece in pieces)
    match = re.fullmatch(regex + _LINK_TAIL, link) if placeholders else None
    if match is None:
        raise PplError("SCHEMA_INVALID", "%r is neither a task id matching %s nor a link of the "
                       "form %s (task.url_template)" % (link, pattern, template))
    if placeholders[0] == "<TASK-ID>":
        task_id = match.group(1)
    else:
        task_id = "%s-%s" % (_task_prefix(doc), match.group(1))
    if not (paths.TASK_ID_RE.match(task_id) and re.match(pattern, task_id)):
        raise PplError("SCHEMA_INVALID", "the link %s names the task %s, which does not match "
                       "the harness key pattern %s" % (link, task_id, pattern))
    return task_id


def claude_argv(binary, session_id, start, worktree, task_id, link, agents_json, resume=False):
    """The launch line. A resume names the session with --resume instead of --session-id and
    sends no prompt: the session already has its own."""
    start = Path(start)
    argv = [binary, "--resume" if resume else "--session-id", session_id,
            "--setting-sources", "local", "--settings", str(start / "settings.json"),
            "--agents", agents_json, "--plugin-dir", str(start / "plugin"),
            "--strict-mcp-config", "--mcp-config", str(start / "mcp.json"),
            "--add-dir", str(worktree), "--permission-mode", "acceptEdits", "-n", task_id]
    if not resume:
        argv.append("%s %s" % (SKILL, link))
    return argv


def _binary():
    value = config.section("claude").get("binary") or "claude"
    if not isinstance(value, str):
        raise PplError("SCHEMA_INVALID", "config.toml at %s: claude.binary must be a string"
                       % paths.config_file())
    return value


def _lane_env(entry):
    """What ppl sets on top of the environment the claude process inherits. PPL_HOME is made
    absolute, since claude starts in the lane's start directory."""
    return {"PPL_LANE": str(entry["lane"]), "PPL_TASK": entry["task"],
            "PPL_HOME": os.path.abspath(str(paths.home()))}


def _merged(state):
    mr = state.get("mr") if isinstance(state, dict) else None
    return bool(isinstance(mr, dict) and mr.get("merged_sha"))


def _legacy_run_problem():
    """Why the run the old single-run pointer (runs/ACTIVE) names blocks a lane, or None: its
    state.json exists (a destroyed run's is archived) and its merge is not recorded. Nothing
    writes the pointer now, so the run it names was started before lanes (D19)."""
    task_id = run.legacy_active_task()
    if not task_id:
        return None
    state = store.read_json(run.state_path(task_id), None, lanes.READ_LIMIT)
    if state is None or _merged(state):
        return None
    return ("run %s was started before lanes and is not finished: finish it, or destroy it "
            "with `ppl destroy --scope local --task %s`, then run ppl lane again"
            % (task_id, task_id))


def _day_cap_problem():
    """(code, reason) for why the day's spend blocks a new lane, or None [R-BUDGET-USD-PER-DAY]."""
    return usage.day_cap_problem()


def _inside(path, root):
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _resolve_harness(name, cwd):
    """The installed harness a lane runs: `name`, else the one whose [repo] path holds `cwd`,
    else the default (`[harness] active`)."""
    installed = harness.list_names()
    if name:
        harness.check_name(name)
        if name not in installed:
            raise PplError("SETUP_INCOMPLETE", "no installed harness is called %s; installed: %s"
                           % (name, ", ".join(installed) or "none"))
        return name
    here = os.path.realpath(cwd or os.getcwd())
    holding = []
    for candidate in installed:
        try:
            doc = harness.load(paths.harness_dir(candidate))
        except PplError:
            continue
        if _inside(here, os.path.realpath(doc["repo"]["path"])):
            holding.append(candidate)
    if len(holding) > 1:
        raise PplError("SCHEMA_INVALID", "the harnesses %s all hold %s in their [repo] path: pass "
                       "--harness <name>" % (", ".join(holding), here))
    if holding:
        return holding[0]
    active = config.active_harness()
    if active in installed:
        return active
    raise PplError("SETUP_INCOMPLETE", "ppl lane needs --harness <name>: no installed harness "
                   "holds %s in its [repo] path and none is the default; installed: %s"
                   % (here, ", ".join(installed) or "none"))


def _layout(doc, task_id, slug):
    """(checkout, worktree, branch, default branch) of the run."""
    repo = doc["repo"]
    checkout = repo["path"]
    worktrees = repo.get("worktrees") or (checkout.rstrip("/") + "/.worktrees")
    name = os.path.basename(checkout.rstrip("/"))
    return (checkout, os.path.join(worktrees, "%s-%s" % (name, task_id)),
            "%s-%s" % (task_id, slug), repo["default_branch"])


# Every git below runs in the project checkout outside any sandbox, through lane_harness.git,
# so each call carries escape_git.OPERATOR_PINNED_CONFIG and GIT_NO_REPLACE_OBJECTS=1, and
# _check_config runs before them.
_git = lane_harness.git


def _check_config(doc, checkout):
    """Refuse a checkout whose local or worktree config sets a key outside
    escape_git.GIT_SAFE_KEYS. A sandboxed role of any lane may write the checkout, and the git
    ppl lane runs there is the operator's own, outside any sandbox: such a key could run a
    command (a filter, a pager, an upload-pack) or send git elsewhere (a url rewrite, an ssh
    command, a proxy) that no -c word of the lane's covers. On a GitHub forge origin must also
    have exactly one url, the forge repo on github.com, as the guard asks of the orchestrator's
    fetch."""
    entries, problem = escape_git.local_config(checkout, timeout=lane_harness.GIT_TIMEOUT_SECONDS,
                                               pins=escape_git.OPERATOR_PINNED_CONFIG)
    if problem:
        raise PplError("SETUP_INCOMPLETE", "the git config of the checkout %s cannot be checked: "
                       "%s" % (checkout, problem))
    unsafe = escape_git.unsafe_entries(entries)
    if unsafe:
        named = ", ".join("%s (%s)" % (key[:120], scope) for scope, key, _ in unsafe)
        raise PplError("OWNER_DECISION",
                       "the git config of the checkout %s sets %s, which could run a command or "
                       "send git elsewhere when ppl lane runs git there outside any sandbox, and "
                       "a sandboxed role can write that config. Inspect them (`git -C %s config "
                       "--list --show-scope`), remove each you did not set yourself (`git -C %s "
                       "config --unset <key>`, with --worktree for a worktree key), then run ppl "
                       "lane again. Only the keys of escape_git.GIT_SAFE_KEYS may be set there."
                       % (checkout, named, checkout, checkout))
    if doc["forge"]["kind"] != "github":
        return
    forge_repo, _ = os_sandbox.repos(doc)
    problem = ("the harness's forge is GitHub and origin in %s names no github.com repository"
               % checkout) if not forge_repo else escape_git.remote_url_problem(
                   entries, checkout, "origin", forge_repo)
    if problem:
        raise PplError("OWNER_DECISION", "%s. A sandboxed role can rewrite that url, and the "
                       "lane's base is fetched from it: set it back with `git -C %s remote "
                       "set-url origin <the forge repo's url>`, then run ppl lane again"
                       % (problem, checkout))


def _branch_exists(checkout, branch):
    return _git(checkout, ["rev-parse", "--verify", "--quiet",
                           "refs/heads/" + branch]).returncode == 0


def _delete_branch(checkout, branch):
    if not _branch_exists(checkout, branch):
        return
    result = _git(checkout, ["branch", "-D", branch])
    if result.returncode != 0:
        raise PplError("SETUP_INCOMPLETE", lane_harness.stderr_text(result))


def _remove_worktree(checkout, worktree):
    result = _git(checkout, ["worktree", "remove", "--force", worktree])
    if result.returncode != 0:
        raise PplError("SETUP_INCOMPLETE", lane_harness.stderr_text(result))


def _add_worktree(doc, checkout, worktree, branch, default, undo):
    """Create the run's branch and worktree from a fresh default branch: origin's after a fetch
    when the checkout has an origin, else the local one, each resolved by its full ref name. The checkout's config is checked
    before the first git there. Refuses a branch or a worktree path that already exists, so the
    undo steps only ever remove what this call made."""
    _check_config(doc, checkout)
    remotes = _git(checkout, ["remote"])
    if remotes.returncode != 0:
        raise PplError("SETUP_INCOMPLETE", "the harness's [repo] path %s is not a git "
                       "checkout: %s" % (checkout, lane_harness.stderr_text(remotes)))
    if _branch_exists(checkout, branch):
        raise PplError("ILLEGAL_TRANSITION",
                       "the branch %s already exists in %s, and a lane starts its run on a new "
                       "branch: delete it with `git -C %s branch -D %s` once nothing on it is "
                       "needed, or pass another --slug" % (branch, checkout, checkout, branch))
    if os.path.lexists(worktree):
        raise PplError("ILLEGAL_TRANSITION", "the worktree path %s already exists: remove it "
                       "(`git -C %s worktree remove %s`) and run ppl lane again"
                       % (worktree, checkout, worktree))
    base = "refs/heads/" + default
    if "origin" in remotes.stdout.split():
        # no submodule recursion: a submodule's own config is read by no check, and the child
        # fetch drops the pinned words. The refspec is explicit, so no configured
        # remote.origin.fetch decides where origin's default branch lands.
        fetch = _git(checkout, ["fetch", "--quiet", escape_git.FETCH_FLAG, "origin",
                                "+refs/heads/%s:refs/remotes/origin/%s" % (default, default)],
                     timeout=lane_harness.FETCH_TIMEOUT_SECONDS)
        if fetch.returncode != 0:
            raise PplError("EXTERNAL_BLOCKED", "git fetch origin %s failed in %s: %s"
                           % (default, checkout, lane_harness.stderr_text(fetch)))
        base = "refs/remotes/origin/" + default
    # the base by its full name, as a sha: a short name would let a tag or a branch a role
    # wrote under the same name win where the operator's config turns the ambiguity warning off
    resolved = _git(checkout, ["rev-parse", "--verify", "--quiet", "--end-of-options",
                               base + "^{commit}"])
    sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not sha:
        raise PplError("SETUP_INCOMPLETE", "%s names no commit in %s, so the run has no base"
                       % (base, checkout))
    # git can leave the new branch behind when the checkout fails, so the branch undo goes
    # first. The branch did not exist a moment ago, so a branch there now is this call's
    undo.append(("delete the branch %s" % branch, lambda: _delete_branch(checkout, branch)))
    added = _git(checkout, ["worktree", "add", "--quiet", worktree, "-b", branch, sha])
    if added.returncode != 0:
        raise PplError("SETUP_INCOMPLETE", "git worktree add %s failed in %s: %s"
                       % (worktree, checkout, lane_harness.stderr_text(added)))
    undo.append(("remove the worktree %s" % worktree,
                 lambda: _remove_worktree(checkout, worktree)))


def _make_run_dirs(task_id, undo):
    """Make the run's directories under runs/ and scratch/, which the lane's OS sandbox lets
    its Bash write but not create, since making one writes its parent (os_sandbox.write_scope).
    One left by an earlier run of the task is kept, and one that is a link or no directory is
    refused: the profile resolves each path it allows, so a link there would widen it."""
    for directory in (paths.run_dir(task_id), os_sandbox.scratch_dir(task_id)):
        if os.path.islink(str(directory)) or (os.path.lexists(str(directory))
                                              and not os.path.isdir(str(directory))):
            raise PplError("SETUP_INCOMPLETE", "%s is a link or not a directory: remove it, "
                           "then run ppl lane again" % directory)
        if not os.path.lexists(str(directory)):
            directory.mkdir(parents=True)
            undo.append(("remove %s" % directory, lambda made=directory: _remove_tree(made)))


def _remove_tree(path):
    if os.path.lexists(str(path)):
        shutil.rmtree(str(path))


def _undo(steps, error):
    """Run the undo steps newest first, each on its own. One that fails is named in the error
    being raised (a warning line for an error that is not a PplError), never raised over it."""
    failed = []
    for what, step in reversed(steps):
        try:
            step()
        except Exception as exc:
            failed.append("could not %s: %s" % (what, exc))
    if not failed:
        return
    note = "Undoing the lane start left work for you: %s" % "; ".join(failed)
    if isinstance(error, PplError):
        error.message = "%s. %s" % (error.message, note)
        error.args = (error.message,)
    else:
        sys.stderr.write("warning: %s\n" % note)


def _exec(argv, extra, start):
    """Replace this process with claude in `start`. Returns only by raising: SETUP_INCOMPLETE
    naming the binary, with the cwd put back."""
    env = dict(os.environ, **extra)
    previous = os.getcwd()
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(str(start))
    try:
        os.execvpe(argv[0], argv, env)
    except OSError as exc:
        raise PplError("SETUP_INCOMPLETE", "could not run the claude binary %s ([claude] binary "
                       "in %s): %s. Set it with ppl setup claude."
                       % (argv[0], paths.config_file(), exc.strerror or exc))
    finally:
        os.chdir(previous)


def start(link, harness_name=None, slug="work", cwd=None, exec_=True):
    """Start the lane for the task `link` names and exec claude in its start directory. With
    exec_=False return {"argv", "env", "cwd"} instead: the launch line, what ppl adds to the
    inherited environment, and the directory to run it in."""
    if not (isinstance(slug, str) and _SLUG.fullmatch(slug)):
        raise PplError("SCHEMA_INVALID", "--slug %r must be 1 to 40 of a-z, 0-9 and '-'" % (slug,))
    problem = _legacy_run_problem()
    if problem:
        raise PplError("OWNER_DECISION", problem)
    problem = _day_cap_problem()
    if problem:
        raise PplError(*problem)
    name = _resolve_harness(harness_name, cwd)
    binary = _binary()
    # the machine's routing.toml by its own path: PPL_TASK of a lane this is run from would
    # otherwise hand it that lane's frozen copy (D15)
    routing_config = routing.load(path=paths.machine_routing_file())
    installed = paths.harness_dir(name)
    sha = lane_harness.commit(installed)
    staging = paths.home() / "lanes" / (".staging-%s" % uuid.uuid4())
    undo = [("remove %s" % staging, lambda: _remove_tree(staging))]
    try:
        doc = harness.load(lane_harness.export(installed, sha, staging / "harness", name))
        task_id = task_from_link(doc, link)
        checkout, worktree, branch, default = _layout(doc, task_id, slug)
        mcp = lane_render.mcp_source(project_dir=checkout)
        session_id = str(uuid.uuid4())
        entry = lanes.claim(task_id, name, sha, checkout, worktree, session_id,
                            harness.lanes_max(doc), config.lanes_max())
        undo.append(("release lane %s" % entry["lane"], lambda: lanes.release(task_id)))
        # after the claim, so a task whose lane still runs is told that instead
        start_dir = lanes.start_dir(task_id)
        if os.path.lexists(str(start_dir)):
            raise PplError("ILLEGAL_TRANSITION", "an earlier lane of %s left its start directory "
                           "%s: remove it (`ppl destroy --scope local --task %s` does, with the "
                           "run), then run ppl lane again" % (task_id, start_dir, task_id))
        _add_worktree(doc, checkout, worktree, branch, default, undo)
        try:
            os.rename(str(staging), str(start_dir))
        except OSError as exc:
            raise PplError("SETUP_INCOMPLETE", "could not move %s to %s: %s"
                           % (staging, start_dir, exc))
        undo.append(("remove %s" % start_dir, lambda: _remove_tree(start_dir)))
        doc = harness.load(start_dir / "harness" / name)
        os_sandbox.scratch_root().mkdir(parents=True, exist_ok=True)
        _make_run_dirs(task_id, undo)
        # what ppl destroy removes once ppl lane close has freed the slot: the start directory
        # is out of every lane's write scope, unlike the run's state.json
        store.write_json(lanes.record_path(task_id), {"task": task_id, "worktree": worktree,
                                                      "checkout": checkout, "branch": branch,
                                                      "harness": name})
        files = lane_render.render(doc, task_id, entry["lane"], start_dir, worktree,
                                   routing_config, mcp)
        argv = claude_argv(binary, session_id, start_dir, worktree, task_id, link,
                           files["agents_json"])
        extra = _lane_env(entry)
        if not exec_:
            return {"argv": argv, "env": extra, "cwd": str(start_dir)}
        lanes.set_pid(task_id, os.getpid())
        _exec(argv, extra, start_dir)
    except BaseException as error:
        _undo(undo, error)
        raise


def _lane(task_id):
    paths.check_task_id(task_id)
    entry = lanes.entry(task_id)
    if entry is None:
        raise PplError("SCHEMA_INVALID", "no lane runs %s: ppl lane list shows the lanes, and "
                       "ppl lane <link> starts one" % task_id)
    return entry


def resume(task_id, exec_=True):
    """Re-launch the lane's session with the flags it started with: no new claim and no
    re-render. With exec_=False return {"argv", "env", "cwd"}, as start does."""
    entry = _lane(task_id)
    start_dir = Path(entry["start_dir"])
    path = start_dir / "agents.json"
    try:
        agents_json = store.read_regular_text(path, lane_render.MAX_AGENTS_BYTES)
    except FileNotFoundError:
        raise PplError("SETUP_INCOMPLETE", "%s is missing, so lane %s cannot get its agents back"
                       % (path, task_id))
    argv = claude_argv(_binary(), entry["session_id"], start_dir, entry["worktree"], task_id,
                       None, agents_json, resume=True)
    extra = _lane_env(entry)
    if not exec_:
        return {"argv": argv, "env": extra, "cwd": str(start_dir)}
    lanes.set_pid(task_id, os.getpid())
    try:
        _exec(argv, extra, start_dir)
    except PplError:
        lanes.set_pid(task_id, entry.get("pid"))
        raise


def close(task_id):
    """Free the lane of a merged run whose reflection is not pending. Only the registry entry
    goes: the start directory and the worktree stay until ppl destroy --scope local. The lane's
    unrecorded claims go back to pending (R60)."""
    entry = _lane(task_id)
    state = store.read_json(run.state_path(task_id), None, lanes.READ_LIMIT)
    if not _merged(state):
        raise PplError("ILLEGAL_TRANSITION", "run %s has no recorded merge, so its lane stays: "
                       "ppl merge observed records it, and ppl destroy --scope local --task %s "
                       "frees the lane of a run that will not merge" % (task_id, task_id))
    if any(marker.get("task") == task_id for marker in lessons.pending()):
        raise PplError("ILLEGAL_TRANSITION", "the reflection of %s is pending: record it with "
                       "ppl lessons record --task %s, then close the lane" % (task_id, task_id))
    released = lanes.release(task_id)
    result = {"task": task_id, "lane": entry["lane"], "released": released,
              "kept": [entry["start_dir"], entry["worktree"]]}
    result.update(lessons.hand_back(task_id))
    return result


def _alive(pid):
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # the process exists and belongs to another user
        return True
    return True


def _run_facts(task_id):
    """(stage, outcome) of the lane's run, both None before ppl init. The outcome is the run's
    last durable fact, as the panel shows it. Display only: the lane's sandbox can write its
    state.json, so whatever reading it raises is that lane's row and stops no other."""
    try:
        state = store.read_json(run.state_path(task_id), None, lanes.READ_LIMIT)
    except Exception as exc:
        return "unknown", "run state is unreadable: %s" % (getattr(exc, "message", None) or exc)
    if not isinstance(state, dict):
        return (None, None) if state is None else ("unknown", "run state is not an object")
    return state.get("stage"), state.get("last_durable_fact")


def listing():
    """Every registry entry, lowest lane first, plus `alive` (the recorded claude process
    exists), the run's `stage` and `outcome`, and `resume`, the command that reopens it."""
    rows = []
    for item in sorted(lanes.read(), key=lambda item: str(item.get("lane"))):
        row = dict(item)
        row["alive"] = _alive(item.get("pid"))
        row["stage"], row["outcome"] = _run_facts(item.get("task"))
        row["resume"] = "ppl lane resume %s" % item.get("task")
        rows.append(row)
    return rows


def text(rows):
    """`ppl lane list` as a table."""
    if not rows:
        return "no lanes are running"
    table = [("lane", "task", "harness", "stage", "window", "resume")]
    for row in rows:
        table.append((str(row.get("lane")), str(row.get("task")), str(row.get("harness")),
                      str(row.get("stage") or "-"), "open" if row["alive"] else "closed",
                      row["resume"]))
    widths = [max(len(line[column]) for line in table) for column in range(len(table[0]) - 1)]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths))
                     + "  " + line[-1] for line in table)

"""The OS sandbox model: what Claude Code's sandbox profile, the guard's facts and the
write scope are for a harness. Pure computation, nothing here writes a settings file."""
import json
import os
import shutil
import sys
from pathlib import Path

from . import paths
from .errors import PplError

DENIED_DOMAINS = ("github.com", "*.github.com")
FACTS_FILE = "os_sandbox.json"
PIN_NAMES = ("gh", "git", "ppl")
DENY_REASON = ("GitHub is reached only by the orchestrator's plain gh, git and ppl commands "
               "outside the sandbox. A role never reaches it.")


def _table(doc):
    table = doc.get("os_sandbox")
    return table if isinstance(table, dict) else {}


def needed(doc):
    """True for a GitHub forge or tracker, or when [os_sandbox] enabled is set."""
    return ((doc.get("forge") or {}).get("kind") == "github"
            or (doc.get("tracker") or {}).get("kind") == "github-issues"
            or _table(doc).get("enabled") is True)


def run_needs(task_id):
    return dir_needs(paths.run_dir(task_id))


def dir_needs(directory):
    """True when the harness snapshot in a run directory says it needs the profile. A missing
    or unreadable snapshot is False."""
    from . import store
    try:
        doc = store.read_json(Path(directory) / "harness.json")
    except (PplError, OSError):
        return False
    return isinstance(doc, dict) and (doc.get("os_sandbox") or {}).get("needed") is True


def scratch_root():
    return paths.home() / "scratch"


def scratch_dir(task_id):
    return scratch_root() / task_id


def _real(path):
    return os.path.realpath(os.path.expanduser(str(path)))


def pins(which=shutil.which):
    """Each pinned binary as {name: {"path": as found, "realpath": resolved}}.
    A binary that cannot be found refuses SETUP_INCOMPLETE naming it. The ppl fallback is
    sys.argv[0] when it is an executable file that is not a .py script."""
    found = {}
    for name in PIN_NAMES:
        path = which(name)
        if path:
            path = os.path.abspath(path)
        elif name == "ppl" and sys.argv and sys.argv[0]:
            candidate = os.path.abspath(sys.argv[0])
            if (os.path.isfile(candidate) and os.access(candidate, os.X_OK)
                    and not candidate.endswith(".py")):
                path = candidate
        if not path:
            raise PplError("SETUP_INCOMPLETE",
                           "the OS sandbox profile pins %s but no %s is on PATH. Put %s on "
                           "PATH, then run ppl lane again." % (name, name, name))
        found[name] = {"path": path, "realpath": os.path.realpath(path)}
    return found


def _repo(doc):
    repo = doc["repo"]
    checkout = repo["path"]
    worktrees = repo.get("worktrees") or (checkout.rstrip("/") + "/.worktrees")
    return checkout, worktrees


# What git in a worktree writes in the checkout's common .git besides its own worktrees/<id>:
# objects, branch and stash refs, and their reflogs (commit, add, branch, stash, checkout,
# rebase, reset and switch, as test_git_in_a_worktree_writes_only_objects_refs_logs_and_its_git_dir
# runs them).
_COMMON_GIT_WRITES = ("objects", "refs", "logs")


def worktree_git_dir(doc, worktree):
    """The lane's own git dir in the checkout, `<checkout>/.git/worktrees/<id>`, `realpath`-ed:
    the gitdir line of `<worktree>/.git`, which git worktree add wrote. SETUP_INCOMPLETE when
    that file does not name one there, since the lane's sandbox could not then be scoped to
    its own worktree's git."""
    marker = os.path.join(str(worktree), ".git")
    try:
        with open(marker, encoding="utf-8") as handle:
            line = handle.readline().strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise PplError("SETUP_INCOMPLETE", "%s cannot be read (%s), so the lane's sandbox "
                       "cannot be scoped to its worktree's git dir" % (marker, exc))
    if not line.startswith("gitdir:"):
        raise PplError("SETUP_INCOMPLETE", "%s has no gitdir line, so %s is not a worktree "
                       "git worktree add made" % (marker, worktree))
    git_dir = os.path.realpath(os.path.join(str(worktree), line[len("gitdir:"):].strip()))
    worktrees = os.path.join(checkout(doc), ".git", "worktrees")
    if os.path.dirname(git_dir) != worktrees:
        raise PplError("SETUP_INCOMPLETE", "%s names the git dir %s, which is not in %s, the "
                       "worktrees of the checkout %s" % (marker, git_dir, worktrees,
                                                         checkout(doc)))
    return git_dir


def write_scope(doc, worktree=None, task=None):
    """Where a sandboxed command may write, each entry resolved. For a lane, given its
    `worktree` and its `task`: the run's own directory runs/<task>, labels and the run's
    scratch/<task>, the parts of the checkout's common .git that git in a worktree writes
    (objects, refs, logs) and its own git dir there (worktree_git_dir), and the lane's own
    worktree, so no lane's Bash writes another lane's run, the checkout's working tree, another
    lane's worktree or git dir, or the shared config, hooks, info and packed-refs. The ppl CLI
    in a lane writes its own run's files and nothing else under runs/ or scratch/, and ppl lane
    makes both directories before the session starts, since making one needs a write in its
    parent. With no worktree, outside a lane: runs, labels and scratch whole, the whole
    checkout and the whole worktrees root. [os_sandbox] allow_write is added to either."""
    checkout_dir, worktrees = _repo(doc)
    if worktree is None:
        scope = [paths.runs_dir(), paths.home() / "labels", scratch_root(), checkout_dir,
                 worktrees]
    else:
        if task is None:
            raise ValueError("a lane's write scope needs its task")
        scope = [paths.run_dir(task), paths.home() / "labels", scratch_dir(task)]
        git_dir = os.path.join(checkout(doc), ".git")
        scope.extend(os.path.join(git_dir, name) for name in _COMMON_GIT_WRITES)
        scope.extend([worktree_git_dir(doc, worktree), worktree])
    scope.extend(_table(doc).get("allow_write") or [])
    return [_real(entry) for entry in scope]


def repos(doc):
    """(forge_repo, tracker_repo), each "owner/name" or None: the GitHub Issues repo of the
    tracker, and the forge's, which is that repo or else the checkout's github.com origin."""
    from . import setup
    checkout, _ = _repo(doc)
    tracker_repo = None
    if (doc.get("tracker") or {}).get("kind") == "github-issues":
        tracker_repo = doc["tracker"].get("repo") or None
    forge_repo = None
    if (doc.get("forge") or {}).get("kind") == "github":
        forge_repo = tracker_repo or setup._origin_repo(_real(checkout))
    return forge_repo, tracker_repo


def _inside(path, parent):
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def is_within(path, parent):
    """True when the `realpath`-ed `path` is `parent` or lies under it."""
    return _inside(path, parent)


def layout_problem(doc, workspace):
    """A refusal message when the checkout or the worktrees lie inside `workspace`, the lane's
    start directory, or contain it, else None. The profile write-denies the whole start
    directory, so such a layout would deny the run its own repository."""
    root = workspace_dir(workspace)
    for key, entry in zip(("[repo] path", "[repo] worktrees"), _repo(doc)):
        real = _real(entry)
        if _inside(real, root) or _inside(root, real):
            return ("%s %s and the lane's start directory %s overlap, and the OS sandbox keeps "
                    "the start directory wholly write-denied (it holds the session's settings, "
                    "agents, skill and guard facts), so the run could not write its own "
                    "repository. Move the checkout or PPL_HOME so neither lies inside the other, "
                    "then run ppl lane again." % (key, real, root))
    return None


def repo_problem(doc):
    """A message when a GitHub forge or tracker names no repository the guard could allow, else
    None. Such a harness renders -R <owner/name> and every GitHub command is denied."""
    forge_repo, _ = repos(doc)
    tracker = doc.get("tracker") or {}
    if tracker.get("kind") == "github-issues" and not tracker.get("repo"):
        return ("[tracker] repo is not set for the github-issues tracker, so the OS sandbox "
                "guard has no repository to allow. Set repo = \"owner/name\" in the harness "
                "[tracker] table, then run ppl lane again.")
    if (doc.get("forge") or {}).get("kind") == "github" and not forge_repo:
        return ("forge_repo cannot be derived: the checkout %s has no github.com origin remote "
                "and the tracker names no GitHub repository, so the OS sandbox guard has no "
                "repository to allow. Add a github.com origin to the checkout, or use a "
                "github-issues tracker with [tracker] repo = \"owner/name\", then run ppl "
                "lane again." % checkout(doc))
    return None


def checkout(doc):
    """The checkout as the facts hold it, `realpath`-ed."""
    return _real(_repo(doc)[0])


def workspace_dir(workspace):
    """The workspace as the facts hold it, `realpath`-ed."""
    return _real(workspace)


def facts(doc, workspace, pin_map, worktree=None, task=None):
    """The slice the guard reads, the shape of os_sandbox.json: a lane's, with its start
    directory as `workspace` and its own `worktree` and run (`task`) in the write scope, or
    PPL_HOME's.
    deny_write is the profile's denyWrite, `realpath`-ed, which the guard holds edit tools to
    as well."""
    _, worktrees = _repo(doc)
    forge_repo, tracker_repo = repos(doc)
    return {"version": 1,
            "workspace": workspace_dir(workspace),
            "checkout": checkout(doc),
            "worktrees": _real(worktrees),
            "default_branch": doc["repo"]["default_branch"],
            "forge_repo": forge_repo,
            "tracker_repo": tracker_repo,
            "scratch": _real(scratch_root()),
            "write_scope": write_scope(doc, worktree, task),
            "deny_write": list(dict.fromkeys(
                os.path.realpath(entry)
                for entry in _lane_deny_write(doc, workspace, pin_map, worktree))),
            "pins": {name: dict(pin_map[name]) for name in PIN_NAMES}}


def _git_outside(pin_map, directory, verb):
    """The pinned git with escape_git.PINNED_CONFIG in `directory`, then `verb` and the flags
    escape_git asks of it."""
    from . import escape_git
    flags = [escape_git.FETCH_FLAG] if verb == "fetch" else escape_git.PUSH_FLAGS
    return " ".join([pin_map["git"]["path"]] + escape_git.PINNED_CONFIG
                    + ["-C", directory, verb] + flags)


def fetch_command(doc, pin_map):
    """The orchestrator's fetch of the default branch into the checkout, outside the OS
    sandbox: word for word the shape escape_git.git_shape lets through and ppl's allow entry,
    the checkout `realpath`-ed as the facts hold it."""
    return "%s origin %s" % (_git_outside(pin_map, checkout(doc), "fetch"),
                             doc["repo"]["default_branch"])


def push_command(pin_map, delete=False):
    """The orchestrator's push, or remote delete, of the run's branch from its worktree,
    outside the OS sandbox, `<worktree>` and `<branch>` left for the orchestrator to fill.
    Both name the ref in full, as escape_git.git_shape asks."""
    push = _git_outside(pin_map, "<worktree>", "push")
    if delete:
        return "%s origin --delete refs/heads/<branch>" % push
    return "%s -u origin refs/heads/<branch>:refs/heads/<branch>" % push


def _allow_entries(doc, pin_map):
    """The nine read-only commands, no GitHub write: those go through the permission prompt."""
    gh, ppl = pin_map["gh"]["path"], pin_map["ppl"]["path"]
    return ["Bash(%s)" % entry for entry in (
        gh + " issue view *", gh + " pr view *", gh + " pr checks *",
        gh + " api user --jq .login", gh + " auth status", fetch_command(doc, pin_map),
        ppl + " tracker fetch --task *", ppl + " review fetch --pr *",
        ppl + " doctor --forge")]


def deny_write(workspace, pin_map):
    """The profile's filesystem.denyWrite: the workspace and the directory of each pin's path
    and realpath, each as given and `realpath`-ed, then every path escape_paths protects from
    edit tools (the git, ssh and gh config, Claude Code's settings, ppl's config, harnesses,
    guard facts and code, the workspace's .claude and .mcp.json, the pins). A deny wins over an
    allow from any settings source, so the boundary does not rest on no other source granting
    a write there."""
    from . import escape_paths
    entries = [_real(workspace)]
    for name in PIN_NAMES:
        for key in ("path", "realpath"):
            directory = os.path.dirname(pin_map[name][key])
            for entry in (directory, os.path.realpath(directory)):
                if entry not in entries:
                    entries.append(entry)
    protected = escape_paths.protected_paths(
        paths.home(), {"workspace": _real(workspace), "pins": pin_map})
    entries.extend(entry for entry in protected if entry not in entries)
    return entries


# What holds a GitHub or ssh credential a sandboxed command could read and copy into a commit
# or a scratch body the orchestrator publishes: gh's config, git's credential store, netrc and
# the ssh keys, under each home directory, and where GH_CONFIG_DIR or XDG_CONFIG_HOME moves gh's
# config or git's store.
_SECRETS = (".config/gh", ".git-credentials", ".config/git/credentials", ".netrc")
_MOVED_SECRETS = (("GH_CONFIG_DIR", ""), ("XDG_CONFIG_HOME", "gh"),
                  ("XDG_CONFIG_HOME", "git/credentials"))


def deny_read(doc):
    """The profile's filesystem.denyRead, each entry as found and `realpath`-ed: the
    credential files of _SECRETS under each home directory, ~/.ssh unless [os_sandbox]
    allow_ssh_read is true, and the moved ones of _MOVED_SECRETS."""
    from . import escape_paths
    names = _SECRETS + (() if _table(doc).get("allow_ssh_read") is True else (".ssh",))
    homes = dict.fromkeys([os.path.expanduser("~")] + escape_paths.home_dirs())
    found = [os.path.join(home, name) for home in homes for name in names]
    for variable, name in _MOVED_SECRETS:
        value = os.environ.get(variable)
        if value and os.path.isabs(value):
            found.append(os.path.join(value, name) if name else value)
    entries = []
    for entry in found:
        for form in (entry, os.path.realpath(entry)):
            if form not in entries:
                entries.append(form)
    return entries


def shared_git(doc):
    """What every lane on the checkout and the operator's own git read and run from its .git,
    and git in a worktree never writes: config, hooks and info, each as found and
    `realpath`-ed. A lane's profile write-denies them inside its allowance of that .git, so
    no lane plants a hook, a core.hooksPath or an attribute there [R-LANE-SHARED-GIT-DIR]."""
    git_dir = os.path.join(checkout(doc), ".git")
    entries = []
    for name in ("config", "hooks", "info"):
        for entry in (os.path.join(git_dir, name), os.path.realpath(os.path.join(git_dir, name))):
            if entry not in entries:
                entries.append(entry)
    return entries


def _lane_deny_write(doc, workspace, pin_map, worktree):
    """deny_write, and for a lane (`worktree` given) the checkout's shared .git parts and the
    gitdir and commondir files of its own git dir, which tell git where the worktree and the
    common dir are, so no lane points its own, or another's, git elsewhere."""
    denied = deny_write(workspace, pin_map)
    if worktree is not None:
        own = worktree_git_dir(doc, worktree)
        for entry in shared_git(doc) + [os.path.join(own, "gitdir"),
                                         os.path.join(own, "commondir")]:
            if entry not in denied:
                denied.append(entry)
    return denied


def profile(doc, workspace, pin_map, worktree=None, task=None):
    """The sandbox settings block and the permissions.allow entries ppl owns, for a lane
    whose own `worktree` and `task` are given or for the workspace (write_scope). A lane's denyWrite also
    holds the checkout's shared .git parts (shared_git) and its own git dir's gitdir and
    commondir, which its allowWrite would otherwise take in: a denyWrite entry wins over an
    allowWrite one."""
    denied = _lane_deny_write(doc, workspace, pin_map, worktree)
    sandbox = {"enabled": True,
               "failIfUnavailable": True,
               "allowUnsandboxedCommands": True,
               "network": {"deniedDomains": list(DENIED_DOMAINS),
                           "deniedDomainReasons": {d: DENY_REASON for d in DENIED_DOMAINS},
                           "allowedDomains": list(_table(doc).get("allowed_domains") or [])},
               "filesystem": {"allowWrite": write_scope(doc, worktree, task), "denyWrite": denied,
                              "denyRead": deny_read(doc)}}
    return {"sandbox": sandbox, "allow": _allow_entries(doc, pin_map)}


def rendered_by_lane(task_id=None):
    """What a lane whose guard facts are missing or unreadable is told: ppl lane renders them,
    and only when the lane starts, so rendering them again is the owner's step outside the
    lane."""
    from . import lanes
    return "ppl lane renders it into the lane's start directory when the lane starts: %s" \
        % lanes.restart_hint(task_id)


def facts_path(task_id=None):
    """The guard facts of the lane of `task_id`, else of the lane this call is in (resolved as
    every ppl command resolves its run, lanes.task_for_call): <start>/os_sandbox.json, which
    ppl lane renders and no lane's sandbox can write. None outside a lane: no machine-wide file
    is read."""
    from . import lanes
    task = task_id or lanes.task_for_call(None)
    return lanes.start_dir(task) / FACTS_FILE if task else None


def load_facts(task_id=None):
    """The parsed guard facts of a lane (facts_path), None when there are none: outside a lane,
    or a lane whose harness needs no OS sandbox. Extra keys are kept."""
    path = facts_path(task_id)
    if path is None:
        return None
    task = path.parent.name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PplError("SETUP_INCOMPLETE", "cannot read %s: %s. %s"
                       % (path, exc, rendered_by_lane(task)))
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise PplError("SETUP_INCOMPLETE", "%s is not a JSON object. %s"
                       % (path, rendered_by_lane(task)))
    return doc


def pinned_repo(key, snapshot_repo, task_id=None):
    """The repository a ppl command run outside the OS sandbox reaches for the run of `task_id`
    (the lane this call is in when None): the `key` (tracker_repo or forge_repo) of the lane's
    guard facts, which a run snapshot naming another one cannot change, since a role can write
    the snapshot but not the facts. SCHEMA_INVALID when they differ, SETUP_INCOMPLETE when the
    lane has no facts: the snapshot alone is never trusted."""
    doc = load_facts(task_id)
    if doc is None:
        raise PplError("SETUP_INCOMPLETE",
                       "%s has no guard facts naming its %s, and a command outside the OS "
                       "sandbox never takes the repository from the run's harness snapshot, which "
                       "a role can write: run it for a lane ppl lane started on a GitHub harness"
                       % ("run %s" % task_id if task_id else "this call's lane", key))
    repo = doc.get(key)
    if not (isinstance(repo, str) and repo) or repo != snapshot_repo:
        raise PplError("SCHEMA_INVALID",
                       "the run's harness snapshot names %r as its repository and %s names %r "
                       "as %s, so this command reaches neither: restore the run's harness.json, "
                       "which a role can write, since %s is what ppl lane rendered for the lane"
                       % (snapshot_repo, FACTS_FILE, repo, key, FACTS_FILE))
    return repo


def gh_path(task_id=None):
    """The gh the guard facts of the lane of `task_id` pin (load_facts: the lane this call is
    in when None), else a bare gh. A command that reaches GitHub for a run passes its task, so
    its gh and its repository (pinned_repo) come from the same lane."""
    try:
        doc = load_facts(task_id)
        path = doc["pins"]["gh"]["path"] if doc else None
    except (PplError, KeyError, TypeError):
        path = None
    return path if isinstance(path, str) and path else "gh"

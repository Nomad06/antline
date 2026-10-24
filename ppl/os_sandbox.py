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
                           "PATH, then run ppl harness apply again." % (name, name, name))
        found[name] = {"path": path, "realpath": os.path.realpath(path)}
    return found


def _repo(doc):
    repo = doc["repo"]
    checkout = repo["path"]
    worktrees = repo.get("worktrees") or (checkout.rstrip("/") + "/.worktrees")
    return checkout, worktrees


def write_scope(doc):
    """Where a sandboxed command may write, each entry resolved."""
    checkout, worktrees = _repo(doc)
    scope = [paths.home() / "runs", paths.home() / "labels", scratch_root(), checkout, worktrees]
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


def checkout(doc):
    """The checkout as the facts hold it, `realpath`-ed."""
    return _real(_repo(doc)[0])


def workspace_dir(workspace):
    """The workspace as the facts hold it, `realpath`-ed."""
    return _real(workspace)


def facts(doc, workspace, pin_map):
    """The slice the guard reads, the shape of PPL_HOME/os_sandbox.json. deny_write is the
    profile's denyWrite, `realpath`-ed, which the guard holds edit tools to as well."""
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
            "write_scope": write_scope(doc),
            "deny_write": list(dict.fromkeys(os.path.realpath(entry)
                                             for entry in deny_write(workspace, pin_map))),
            "pins": {name: dict(pin_map[name]) for name in PIN_NAMES}}


def _git_outside(pin_map, directory, verb):
    """The pinned git with hooks off in `directory`, then `verb` and the flags escape_git asks
    of it."""
    from . import escape_git
    flags = [escape_git.FETCH_FLAG] if verb == "fetch" else escape_git.PUSH_FLAGS
    return " ".join([pin_map["git"]["path"]] + escape_git.HOOKS_OFF
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
    and realpath, each as given and `realpath`-ed."""
    entries = [_real(workspace)]
    for name in PIN_NAMES:
        for key in ("path", "realpath"):
            directory = os.path.dirname(pin_map[name][key])
            for entry in (directory, os.path.realpath(directory)):
                if entry not in entries:
                    entries.append(entry)
    return entries


def profile(doc, workspace, pin_map):
    """The sandbox settings block and the permissions.allow entries ppl owns."""
    denied = deny_write(workspace, pin_map)
    sandbox = {"enabled": True,
               "failIfUnavailable": True,
               "allowUnsandboxedCommands": True,
               "network": {"deniedDomains": list(DENIED_DOMAINS),
                           "deniedDomainReasons": {d: DENY_REASON for d in DENIED_DOMAINS},
                           "allowedDomains": list(_table(doc).get("allowed_domains") or [])},
               "filesystem": {"allowWrite": write_scope(doc), "denyWrite": denied}}
    return {"sandbox": sandbox, "allow": _allow_entries(doc, pin_map)}


def load_facts():
    """The parsed PPL_HOME/os_sandbox.json, None when absent. Extra keys are kept."""
    path = paths.home() / FACTS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PplError("SETUP_INCOMPLETE", "cannot read %s: %s. Run ppl harness apply."
                       % (path, exc))
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise PplError("SETUP_INCOMPLETE", "%s is not a JSON object. Run ppl harness apply."
                       % path)
    return doc


def pinned_repo(key, snapshot_repo):
    """The repository a ppl command run outside the OS sandbox reaches. With the guard facts
    present it is their `key` (tracker_repo or forge_repo), and a run snapshot naming another
    one is refused SCHEMA_INVALID, since a role can write the snapshot but not the facts.
    Without the facts, the snapshot's."""
    doc = load_facts()
    if doc is None:
        return snapshot_repo
    repo = doc.get(key)
    if not (isinstance(repo, str) and repo) or repo != snapshot_repo:
        raise PplError("SCHEMA_INVALID",
                       "the run's harness snapshot names %r as its repository and %s names %r "
                       "as %s, so this command reaches neither: run ppl harness apply, or "
                       "restore the run's harness.json" % (snapshot_repo, FACTS_FILE, repo, key))
    return repo


def gh_path():
    """The pinned gh when the facts exist, else a bare gh."""
    try:
        doc = load_facts()
        path = doc["pins"]["gh"]["path"] if doc else None
    except (PplError, KeyError, TypeError):
        path = None
    return path if isinstance(path, str) and path else "gh"

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


def facts(doc, workspace, pin_map):
    """The slice the guard reads, the shape of PPL_HOME/os_sandbox.json."""
    from . import setup
    checkout, worktrees = _repo(doc)
    tracker_repo = None
    if (doc.get("tracker") or {}).get("kind") == "github-issues":
        tracker_repo = doc["tracker"].get("repo") or None
    forge_repo = None
    if (doc.get("forge") or {}).get("kind") == "github":
        forge_repo = tracker_repo or setup._origin_repo(_real(checkout))
    return {"version": 1,
            "workspace": _real(workspace),
            "checkout": _real(checkout),
            "worktrees": _real(worktrees),
            "default_branch": doc["repo"]["default_branch"],
            "forge_repo": forge_repo,
            "tracker_repo": tracker_repo,
            "scratch": _real(scratch_root()),
            "write_scope": write_scope(doc),
            "pins": {name: dict(pin_map[name]) for name in PIN_NAMES}}


def _allow_entries(doc, pin_map):
    """The nine read-only commands, no GitHub write: those go through the permission prompt."""
    gh, git, ppl = (pin_map[name]["path"] for name in PIN_NAMES)
    checkout, _ = _repo(doc)
    fetch = "%s -c core.hooksPath=/dev/null -C %s fetch origin %s" % (
        git, _real(checkout), doc["repo"]["default_branch"])
    return ["Bash(%s)" % entry for entry in (
        gh + " issue view *", gh + " pr view *", gh + " pr checks *",
        gh + " api user --jq .login", gh + " auth status", fetch,
        ppl + " tracker fetch --task *", ppl + " review fetch --pr *",
        ppl + " doctor --forge")]


def profile(doc, workspace, pin_map):
    """The sandbox settings block and the permissions.allow entries ppl owns."""
    deny_write = [_real(workspace)]
    for name in PIN_NAMES:
        for key in ("path", "realpath"):
            directory = os.path.dirname(pin_map[name][key])
            for entry in (directory, os.path.realpath(directory)):
                if entry not in deny_write:
                    deny_write.append(entry)
    sandbox = {"enabled": True,
               "failIfUnavailable": True,
               "allowUnsandboxedCommands": True,
               "network": {"deniedDomains": list(DENIED_DOMAINS),
                           "deniedDomainReasons": {d: DENY_REASON for d in DENIED_DOMAINS},
                           "allowedDomains": list(_table(doc).get("allowed_domains") or [])},
               "filesystem": {"allowWrite": write_scope(doc), "denyWrite": deny_write}}
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


def gh_path():
    """The pinned gh when the facts exist, else a bare gh."""
    try:
        doc = load_facts()
        path = doc["pins"]["gh"]["path"] if doc else None
    except (PplError, KeyError, TypeError):
        path = None
    return path if isinstance(path, str) and path else "gh"

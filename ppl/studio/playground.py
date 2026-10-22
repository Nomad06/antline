"""Dry-run the cage on one tool call. A throwaway data root holds a fake
active run whose harness.json is the draft's snapshot, the real hook runs in
a subprocess against it, and the directory is deleted when the hook has
answered: a check never touches the real runs/."""
import json
import os
import re
import subprocess
import sys
import tempfile

from .. import harness, paths, routing
from ..errors import PplError
from . import drafts, files

TASK = "STUDIO-1"
TOOLS = ("Bash", "Read", "Write", "Edit", "MultiEdit", "NotebookEdit", "Grep", "Glob")
MAX_INPUT = 4096
TIMEOUT_SECONDS = 30
DEFAULT_BOUNDARY = {"write": ["src/**", "lib/**", "source/**"], "forbid": []}
_MCP_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+$")
_RULE_RE = re.compile(r"\bR-[A-Z0-9]+(?:-[A-Z0-9]+)+\b")
_WRITE_PATH_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
_INTERNAL_ERROR = "ppl_guard internal error"


def tool_input(tool, value):
    if tool == "Bash":
        return {"command": value}
    if tool == "Write":
        return {"file_path": value, "content": ""}
    if tool in ("Edit", "MultiEdit"):
        return {"file_path": value, "old_string": "", "new_string": ""}
    if tool == "NotebookEdit":
        return {"notebook_path": value, "new_source": ""}
    if tool == "Read":
        return {"file_path": value}
    if tool in ("Grep", "Glob"):
        return {"pattern": "*", "path": value}
    return {}


def _check_inputs(role, tool, value, boundary):
    if role != "operator" and role not in routing.REQUIRED_ROLES:
        raise PplError("SCHEMA_INVALID", "unknown role %r (roles: %s, operator)"
                       % (role, ", ".join(routing.REQUIRED_ROLES)))
    if tool not in TOOLS and not (isinstance(tool, str) and _MCP_TOOL_RE.match(tool)):
        raise PplError("SCHEMA_INVALID", "unknown tool %r (tools: %s, or an MCP tool name)"
                       % (tool, ", ".join(TOOLS)))
    if not isinstance(value, str) or len(value) > MAX_INPUT:
        raise PplError("SCHEMA_INVALID", "the command or path must be text of at most %d "
                       "characters" % MAX_INPUT)
    if not (isinstance(boundary, dict)
            and all(isinstance(boundary.get(key), list) for key in ("write", "forbid"))
            and all(isinstance(item, str) for key in ("write", "forbid") for item in boundary[key])):
        raise PplError("SCHEMA_INVALID", "the boundary is {\"write\": [globs], \"forbid\": [globs]}")


def _as_sent(tool, value, repo):
    """What the hook would resolve. Claude Code gives it an absolute path. The
    hook reads a write path against its own working directory and never
    expands a ~, so a write path that is not absolute is joined to the repo
    and a ~ stays a name. A read path the hook resolves itself against the
    payload's cwd, expanding ~, so it goes as typed."""
    if tool not in _WRITE_PATH_TOOLS or os.path.isabs(value):
        return value
    return os.path.join(repo, value)


def _environment(home):
    """The environment of both subprocesses: the throwaway data root is the
    only PPL_ variable that names a place to keep data. The Claude home, the
    agents and skills directories and any other PPL_ variable are dropped, and
    PPL_ROOT, the assets directory, and PYTHONPATH are pinned to the package this
    process runs."""
    inherited = {key: item for key, item in os.environ.items() if not key.startswith("PPL_")}
    parent = str(paths.package_parent())
    if inherited.get("PYTHONPATH"):
        parent = parent + os.pathsep + inherited["PYTHONPATH"]
    return dict(inherited, PPL_HOME=home, PPL_ROOT=str(paths.root()), PYTHONPATH=parent)


def parse(returncode, stdout, stderr):
    if returncode != 0:
        return {"decision": "error", "rules": [],
                "reason": (stderr or "").strip()[-300:] or "the hook exited with %d" % returncode}
    text = (stdout or "").strip()
    if not text:
        if _INTERNAL_ERROR in (stderr or ""):
            return {"decision": "error", "reason": stderr.strip()[-300:], "rules": []}
        return {"decision": "allow", "reason": "", "rules": []}
    try:
        output = json.loads(text).get("hookSpecificOutput") or {}
    except (ValueError, AttributeError):
        return {"decision": "error", "reason": "the hook answered %s" % text[:300], "rules": []}
    reason = output.get("permissionDecisionReason") or ""
    return {"decision": output.get("permissionDecision") or "allow", "reason": reason,
            "rules": sorted(set(_RULE_RE.findall(reason)))}


def check(name, doc, fragments, role, tool, value, boundary=None):
    drafts.check_name(name)
    boundary = DEFAULT_BOUNDARY if boundary is None else boundary
    _check_inputs(role, tool, value, boundary)
    if doc is None:
        fragments = files.harness_fragments(name)
        loaded = harness.load(str(paths.harness_dir(name)))
        doc = {key: item for key, item in loaded.items() if not key.startswith("_")}
    with tempfile.TemporaryDirectory(prefix="ppl-playground-") as home:
        # the draft lives in the throwaway root too, so two checks never share a directory
        draft = drafts.write(name, doc, fragments or {}, root=os.path.join(home, "draft"))
        loaded = harness.load(str(draft))
        harness.require_absolute_repo(loaded)
        repo = os.path.expanduser(loaded["repo"]["path"])
        environment = _environment(home)
        spec = {"task": TASK, "repo": repo, "snapshot": harness.snapshot(loaded),
                "boundary": boundary}
        try:
            child = subprocess.run([sys.executable, "-m", "ppl.studio.playground_child"],
                                   input=json.dumps(spec), capture_output=True, text=True,
                                   env=environment, cwd=str(paths.package_parent()), timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise PplError("SCHEMA_INVALID", "the playground could not build its run within %d "
                           "seconds" % TIMEOUT_SECONDS)
        if child.returncode != 0:
            raise PplError("SCHEMA_INVALID", "the playground could not build its run: %s"
                           % child.stderr.strip()[-300:])
        payload = {"hook_event_name": "PreToolUse", "tool_name": tool,
                   "tool_input": tool_input(tool, _as_sent(tool, value, repo)), "cwd": repo}
        if role != "operator":
            payload["agent_type"] = role
        try:
            hook = subprocess.run([sys.executable, str(paths.root() / "hooks" / "ppl_guard.py")],
                                  input=json.dumps(payload), capture_output=True, text=True,
                                  env=environment, timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return {"decision": "error", "rules": [],
                    "reason": "the hook did not answer within %d seconds" % TIMEOUT_SECONDS}
    return parse(hook.returncode, hook.stdout, hook.stderr)

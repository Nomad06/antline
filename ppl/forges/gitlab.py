"""GitLab through the GitLab MCP server the harness names in forge.mcp_server."""
import json
from pathlib import Path

from . import read_fragment

KIND = "gitlab"
VOCABULARY = {"noun": "merge request", "short": "MR", "id_word": "iid"}
_REVIEW_READ_TOOLS = ("mr_discussions", "get_merge_request_diffs")
_DENIED = {
    "resolve_merge_request_thread": "only the remark author may resolve a thread",
    "merge_merge_request": "the pipeline ends at approval, a human performs the merge",
    "approve_merge_request": "the pipeline never approves its own merge request",
}


def _prefix(settings):
    return "mcp__%s__" % settings.get("mcp_server", "GitLab-MCP")


def role_tools(role, settings):
    if role == "ppl-review-responder":
        return [_prefix(settings) + name for name in _REVIEW_READ_TOOLS]
    return []


def denied_tools(settings):
    return {_prefix(settings) + name: reason for name, reason in _DENIED.items()}


def denied_bash():
    return []


def bash_shapes(role):
    return []


def fragment(name):
    return read_fragment(KIND, name)


def author_lookup():
    return "call the GitLab MCP's `whoami` once and pass its username to `ppl mr set --author`"


def _names(servers):
    return set(servers) if isinstance(servers, dict) else set()


def _servers(path):
    """The MCP server names a config file registers, or None when the file is
    not a readable JSON object. Valid JSON of another shape counts as
    unreadable, and a non-object mcpServers or project entry registers nothing."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    found = _names(doc.get("mcpServers"))
    projects = doc.get("projects")
    if isinstance(projects, dict):
        for project in projects.values():
            if isinstance(project, dict):
                found.update(_names(project.get("mcpServers")))
    return found


def doctor_check(settings, claude_json_path, workspace):
    name = settings.get("mcp_server", "GitLab-MCP")
    seen = set()
    unreadable = []
    for path in (Path(claude_json_path), Path(workspace) / ".mcp.json"):
        if not path.exists():
            continue
        servers = _servers(path)
        if servers is None:
            unreadable.append(str(path))
        else:
            seen.update(servers)
    if name in seen:
        return {"ok": True, "detail": "MCP server %s is registered" % name}
    detail = "MCP server %s is not registered in %s or %s/.mcp.json" % (
        name, claude_json_path, workspace)
    if unreadable:
        detail += " (unreadable: %s)" % ", ".join(unreadable)
    return {"ok": False, "detail": detail}

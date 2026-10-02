"""Git forge adapters: what the review responder may call, what the cage
denies, what the skill says in Stages 6 and 7. Selected by harness forge.kind."""
from importlib import import_module
from pathlib import Path

KINDS = ("gitlab", "github")
DEFAULT_SETTINGS = {"gitlab": {"kind": "gitlab", "mcp_server": "GitLab-MCP"},
                    "github": {"kind": "github"}}


def get(kind):
    if kind not in KINDS:
        raise ValueError("unknown forge kind %r" % kind)
    return import_module("ppl.forges.%s" % kind)


def all_denied_tools():
    """Every tool either adapter denies under its default settings. The guard
    uses this when no run is active, so a resolve or merge tool is never
    allowed because a run was not recorded."""
    union = {}
    for kind in KINDS:
        union.update(get(kind).denied_tools(DEFAULT_SETTINGS[kind]))
    return union


def read_fragment(kind, name):
    path = Path(__file__).resolve().parent / "fragments" / kind / ("%s.md" % name)
    return path.read_text(encoding="utf-8")

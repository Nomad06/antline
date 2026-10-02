"""Issue tracker adapters. v1 ships jira, the slot is harness tracker.kind."""
import re
from importlib import import_module

KINDS = ("jira",)

_MCP_PREFIX = re.compile(r"mcp__[A-Za-z0-9_-]+__")


def get(kind):
    if kind not in KINDS:
        raise ValueError("unknown tracker kind %r" % kind)
    return import_module("ppl.trackers.%s" % kind)


def is_mcp_prefix(prefix):
    """True for a prefix shaped mcp__<name>__, the form Claude Code gives a server's tools."""
    return isinstance(prefix, str) and bool(_MCP_PREFIX.fullmatch(prefix))

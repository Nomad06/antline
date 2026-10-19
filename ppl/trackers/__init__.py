"""Issue tracker adapters, the slot is harness tracker.kind. Every adapter module
exports KIND, TRANSPORT ("mcp" or "cli"), DEFAULTS, KEYS (the [tracker] keys it
reads, "kind" among them) and the functions validate(table) -> (errors, warnings),
usable(table), mcp_write_regex(table), mcp_read_tools(table) and
write_denials(table), the Bash write patterns of a cli adapter, doctor_check(table, runner)
-> {"ok", "detail"}, task_defaults(table) -> {"key_pattern", "url_template"} and fragment(name), the
text of its skill fragment `admission` (how to read the task) or `close` (how to post the
note after the merge). The guard imports
this package, so it parses on Python 3.9 and carries no annotations."""
import re
from importlib import import_module
from pathlib import Path

_MODULES = {"jira": "jira", "github-issues": "github_issues"}
KINDS = tuple(_MODULES)
DEFAULT_KIND = "jira"

_MCP_PREFIX = re.compile(r"mcp__[A-Za-z0-9_-]+__")


def get(kind):
    if kind not in KINDS:
        raise ValueError("unknown tracker kind %r" % kind)
    return import_module("ppl.trackers.%s" % _MODULES[kind])


def usable(table):
    """True when the tracker table is one its adapter can gate with. Never raises:
    a non-dict, an unknown kind or a malformed list is False, an absent kind is jira."""
    if not isinstance(table, dict):
        return False
    kind = table.get("kind", DEFAULT_KIND)
    if not isinstance(kind, str) or kind not in KINDS:
        return False
    return bool(get(kind).usable(table))


def is_mcp_prefix(prefix):
    """True for a prefix shaped mcp__<name>__, the form Claude Code gives a server's tools."""
    return isinstance(prefix, str) and bool(_MCP_PREFIX.fullmatch(prefix))


def read_fragment(kind, name):
    path = Path(__file__).resolve().parent / "fragments" / kind / ("%s.md" % name)
    return path.read_text(encoding="utf-8")

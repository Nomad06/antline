"""Jira through the Atlassian MCP server, which registers under different
names on different machines. The harness may override every list here, and the
lists cover the names the server has used across builds, so an older or newer
registration is gated."""
import re

from . import is_mcp_prefix

KIND = "jira"
TRANSPORT = "mcp"
KEYS = ("kind", "mcp_prefixes", "write_operations", "read_tools")
_BARE_TOOL = re.compile(r"[A-Za-z0-9_.-]+")

DEFAULTS = {
    "mcp_prefixes": ["mcp__atlassian__", "mcp__claude_ai_Atlassian_Rovo__"],
    "write_operations": ["addOrEditJiraIssueComment", "addCommentToJiraIssue",
                         "editJiraIssue", "transitionJiraIssue", "addWorklogToJiraIssue",
                         "createJiraIssue", "executeWrite", "executeDestructive",
                         "createConfluenceContent", "updateConfluenceContent",
                         "addGraphContext", "createConfluencePage", "updateConfluencePage",
                         "createConfluenceFooterComment", "createConfluenceInlineComment"],
    "read_tools": ["getJiraIssue", "getConfluenceContent", "getConfluencePage",
                   "searchJiraIssuesUsingJql", "searchConfluence", "executeRead"],
}


def write_tool_regex(prefixes, operations):
    """Matches <prefix><operation> for every prefix, so it accepts every name
    write_tool_names produces. A prefix shaped mcp__<name>__ also matches the
    numbered duplicate claude.ai gives a second registration of the same server
    (mcp__name_2__), any other prefix is matched literally. No prefix or no
    operation matches nothing."""
    shaped = []
    heads = []
    for prefix in prefixes:
        if is_mcp_prefix(prefix):
            shaped.append(re.escape(prefix[len("mcp__"):-2]) + r"(?:_\d+)?")
        else:
            heads.append(re.escape(prefix))
    if shaped:
        heads.insert(0, "mcp__(?:%s)__" % "|".join(shaped))
    if not heads or not operations:
        return re.compile(r"(?!)")
    return re.compile(r"^(?:%s)(?:%s)$" % ("|".join(heads), "|".join(map(re.escape, operations))))


def write_tool_names(prefixes, operations):
    return tuple(prefix + operation for prefix in prefixes for operation in operations)


def read_tool_names(prefixes, read_tools):
    return [prefix + tool for prefix in prefixes for tool in read_tools]


def _string_list(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item for item in value))


def usable(table):
    """A prefix that is not shaped mcp__<name>__ would gate nothing and still replace
    the defaults, so it makes the whole table unusable."""
    return (isinstance(table, dict)
            and _string_list(table.get("mcp_prefixes"))
            and all(is_mcp_prefix(prefix) for prefix in table["mcp_prefixes"])
            and _string_list(table.get("write_operations")))


def mcp_write_regex(table):
    return write_tool_regex(table["mcp_prefixes"], table["write_operations"])


def mcp_read_tools(table):
    return read_tool_names(table["mcp_prefixes"], table["read_tools"])


def write_denials(table):
    """Jira is written through MCP only, so Bash holds no write shape."""
    return []


def validate(table):
    """(errors, warnings) for the [tracker] table. The texts are the harness's own."""
    errors = []
    warnings = []
    for key in ("mcp_prefixes", "write_operations", "read_tools"):
        if key not in table:
            return ["tracker.%s is required" % key], warnings
        if not isinstance(table[key], list):
            return ["tracker.%s must be a list" % key], warnings
        if not all(isinstance(item, str) for item in table[key]):
            return ["tracker.%s must hold only strings" % key], warnings
    for index, prefix in enumerate(table["mcp_prefixes"]):
        if not is_mcp_prefix(prefix):
            errors.append("tracker.mcp_prefixes[%d] %r must look like mcp__<name>__"
                          % (index, prefix))
    defaults = DEFAULTS["mcp_prefixes"]
    if not set(defaults) & set(table["mcp_prefixes"]):
        warnings.append("tracker.mcp_prefixes keeps none of the %s defaults (%s): the tracker "
                        "gate covers only the prefixes listed, and the briefer's read tools are "
                        "named under them too" % (KIND, ", ".join(defaults)))
    writes = set(table["write_operations"])
    for index, tool in enumerate(table["read_tools"]):
        if not _BARE_TOOL.fullmatch(tool):
            errors.append("tracker.read_tools[%d] %r is not a bare tool name (letters, digits, "
                          "'_', '.' and '-' only)" % (index, tool))
        if tool in writes:
            errors.append("tracker.read_tools[%d] %r is also a tracker.write_operations entry, "
                          "no role may hold a tracker write tool" % (index, tool))
    return errors, warnings

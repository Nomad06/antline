"""Jira through the Atlassian MCP server, which registers under different
names on different machines. The harness may override every list here, and the
lists cover the names the server has used across builds, so an older or newer
registration is gated."""
import re

from . import is_mcp_prefix

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

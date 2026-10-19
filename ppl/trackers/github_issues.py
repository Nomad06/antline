"""GitHub Issues through the gh CLI. No MCP tool reads or writes the tracker, so
the briefer holds no tracker tool and every write is a Bash command: write_denials
are the patterns the guard matches a whole Bash command against, for any repo."""
import re

from ..errors import PplError

KIND = "github-issues"
TRANSPORT = "cli"
KEYS = ("kind", "repo", "task_prefix")
DEFAULTS = {"task_prefix": "GH"}

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PREFIX = re.compile(r"[A-Z][A-Z0-9]*")
_TASK_ID = re.compile(r"[A-Z][A-Z0-9]*-([1-9][0-9]*)")
_NOTHING = re.compile(r"(?!)")

# The patterns run on the raw command, as the forge's do: no split into commands,
# no env prefix stripped, no unquoting. Each one is anchored at a `gh` word and
# reads only the command that word starts, up to its first unquoted separator.
#
# The flag grammar follows forges/github.py. A word is quoted pieces, escapes and
# bare runs glued together, each piece told apart by its first character and
# matched possessively, so a word has one parse and a padded command cannot make
# a pattern backtrack past the hook's timeout. Flags may sit between gh and the
# subcommand (`gh -R o/r issue close 5`) and between issue and its verb, and each
# flag may take one spaced value. That value is a word the shell leaves as it is
# (bare, or one quoted string with no escape or expansion) and never a flag, the
# subcommand or a write verb. So a value is never a verb in disguise, and the word
# gh reads as the verb is the word read here as the verb, or is unreadable and
# denied. A redirect (`2>/dev/null`) may sit among the flags too.
_PIECE = r"(?:'[^']*'|\"(?:[^\"\\]|\\[\s\S])*+\"|\\[\s\S]|[^\s'\"\\;&|<>()]+)"
_WORD = _PIECE + "++"
_END = r"(?![^\s;&|()<>])"
_FLAG_AHEAD = r"(?:[\"']?-[A-Za-z-]|\\-|-[\"'\\])"
_LITERAL_WORD = r"(?:[^\s\\\"'`$;&|<>()]++|'[^']*+'|\"[^\"\\$`]*+\")" + _END
_REDIRECT = r"(?>(?:[0-9]*|&)(?:<<<|<<-?|<&|<>|<|>>|>&|>\||>)\s*(?:" + _WORD + r")?)"
# A word the shell rewrites before gh sees it: an escape, an expansion, or a quote
# that is not the word's last character, so `cl\ose`, `cl"ose"` and `$VERB` cannot
# be read as the verb they become. A closing quote alone is prose ending a string.
_UNREADABLE = r"(?=[^\s;&|<>()]*?(?:[\\`$]|[\"'][^\s;&|<>()]))"


def _quotable(words):
    """The words, alone or wearing the one quote or backslash the shell removes."""
    return r"[\"'\\]?(?:" + words + r")[\"']?" + _END


def _flags(stop_words):
    value = r"(?!" + _FLAG_AHEAD + r")(?!" + stop_words + r")" + _LITERAL_WORD
    flag = r"(?=" + _FLAG_AHEAD + r")" + _WORD + r"(?:\s+" + value + r")?"
    return r"(?>(?:\s+(?>" + flag + r")|\s*" + _REDIRECT + r")*)"


_SUBCOMMAND = _quotable("issue|api")
_WRITE_VERB = _quotable("comment|create|new|edit|close|reopen|delete|transfer|pin|unpin"
                        "|lock|unlock|develop")
_READ_VERB = _quotable("view|list|ls|status")
_GH = r"\bgh[\"']?" + _END
_ROOT = _flags(_SUBCOMMAND) + r"\s+"
_GH_ISSUE = _GH + _ROOT + _quotable("issue") + _flags(_WRITE_VERB)
# The gh api checks read from right after gh, since gh api takes its own flags
# before the subcommand too (`gh -X POST api ...`).
_GH_API = _GH + r"(?=" + _ROOT + _quotable("api") + r")"

# A step through the rest of one command: a run that cannot start the target,
# taken in one possessive move, a quoted string or escape that does not hold it,
# or one character that starts no target. `first` is the class of characters the
# target can start with. The scan ends at the first unquoted separator, an fd
# redirect's `&` and `>|` excepted. With in_quotes the target is also looked for
# inside a quoted string, for a route or a GraphQL document, and without it only
# where a word starts, for a flag.
_FREE_AMP = r"(?<=[<>])&|&(?=>)|(?<=>)\|"


def _scan(target, first, in_quotes):
    stop = r"(?!" + target + r")"
    run = r"[^" + first + r";&|\n'\"\\]++"
    if in_quotes:
        single = r"'(?:[^'" + first + r"]++|" + stop + r"[" + first + r"])*+"
        double = r"\"(?:[^\"\\" + first + r"]++|\\[\s\S]|" + stop + r"[" + first + r"])*+"
        step = (run + r"|" + stop + r"[" + first + r"]|\\" + stop + r"[\s\S]|" + single
                + r"'|" + double + r"\"|" + _FREE_AMP)
        tail = r"(?:" + single + r"|" + double + r"|\\)?" + target
    else:
        step = (run + r"|" + stop + r"(?:[" + first + r"]|\\[\s\S]|'[^']*+'|\"(?:[^\"\\]++"
                r"|\\[\s\S])*+\")|" + _FREE_AMP)
        tail = target
    return r"(?:" + step + r")*+" + tail


def _has(target, first, in_quotes=False):
    return r"(?=" + _scan(target, first, in_quotes) + r")"


def _lacks(target, first, in_quotes=False):
    return r"(?!" + _scan(target, first, in_quotes) + r")"


# An expansion outside single quotes: `$`, a backtick, or a process substitution.
_EXPANSION = (r"(?=(?:[^;&|\n'\"\\$`<>]++|'[^']*+'|\"(?:[^\"\\$`]++|\\[\s\S])*+\"|\\[\s\S]"
              r"|[<>](?!\()|" + _FREE_AMP + r")*+(?:[$`]|[<>]\(|\"(?:[^\"\\$`]++"
              r"|\\[\s\S])*+[$`]))")

# A word start, bare or under the quote or backslash the shell removes.
_TOKEN = r"(?<![^\s])[\"'\\]?"
_SEGMENT = r"[^\s\"'`;&|<>()/]*+/"
# Any owner and name, a `{owner}` placeholder, a full or GHES URL, a repository
# id, and dot segments, since the route is looked for anywhere in a word.
_ROUTE = r"(?i:repos|repositories)/(?:" + _SEGMENT + r")+?(?i:issues)(?![A-Za-z0-9_-])"
_PULLS = (_TOKEN + r"(?:https?://[^\s/\"']++(?:/api/v3)?)?/?repos/(?:" + _SEGMENT
          + r")+?pulls/")
# -X, a cluster ending in it (-iX), and --method, the value spaced, glued or after
# =. Atomic, so a quote the value wears is never given back to read `'GET'` as a
# method other than GET.
_METHOD = _TOKEN + r"(?>(?:-i*X[\"']?(?:\s+|=)?|--method[\"']?(?:\s+|=))[\"']?)"
_GET = r"(?i:get|head)[\"']?" + _END
_BODY = _TOKEN + r"(?:-i*[fF]|--(?:field|raw-field|input)(?![A-Za-z0-9-]))"
_FILE_BODY = (_TOKEN + r"(?:--input(?![A-Za-z0-9-])|(?:-i*F|--field)(?:[\"']?(?:\s+|=))?"
              r"[\"']?[^\s=]*=[\"']?@)")
_GRAPHQL = r"(?<![A-Za-z0-9_-])(?i:graphql)(?![A-Za-z0-9_-])"
# The mutation behind every gh issue verb denied above, plus the sub-issue and
# duplicate ones. A GraphQL document names its operation with the literal
# `mutation` keyword, so that word is looked for first.
MUTATIONS = ("addComment", "updateIssueComment", "deleteIssueComment", "createIssue",
             "updateIssue", "closeIssue", "reopenIssue", "deleteIssue", "transferIssue",
             "pinIssue", "unpinIssue", "lockLockable", "unlockLockable",
             "addLabelsToLabelable", "removeLabelsFromLabelable", "clearLabelsFromLabelable",
             "addAssigneesToAssignable", "removeAssigneesFromAssignable",
             "updateIssueIssueType", "createLinkedBranch", "deleteLinkedBranch", "addSubIssue",
             "removeSubIssue", "reprioritizeSubIssue", "markIssueAsDuplicate",
             "unmarkIssueAsDuplicate")
_MUTATION_WORD = r"(?<![A-Za-z0-9_])mutation(?![A-Za-z0-9_])"
_MUTATION = r"(?<![A-Za-z0-9_])(?:" + "|".join(MUTATIONS) + r")(?![A-Za-z0-9_])"
_MUTATION_FIRST = "".join(sorted(set(name[0] for name in MUTATIONS)))

_DENIALS = tuple(re.compile(pattern) for pattern in (
    # gh issue <write verb>, or a verb the shell rewrites
    _GH_ISSUE + r"\s+(?:" + _WRITE_VERB + r"|(?!" + _READ_VERB + r")" + _UNREADABLE + r")",
    # a gh subcommand the shell rewrites (`gh $SUB close 5`)
    _GH + _ROOT + r"(?!" + _SUBCOMMAND + r")" + _UNREADABLE,
    # an expansion anywhere in a gh issue call, which may become any word
    _GH + r"(?=" + _ROOT + _quotable("issue") + r")" + _EXPANSION,
    # an issue route with a method other than GET or HEAD
    _GH_API + _has(_ROUTE, "rR", True) + _has(_METHOD + r"(?!" + _GET + r")[^\s;&|]", "-"),
    # an issue route with a field or a body file and no GET, gh's implicit POST
    _GH_API + _has(_ROUTE, "rR", True) + _has(_BODY, "-") + _lacks(_METHOD + _GET, "-"),
    # an issue mutation in a GraphQL document, whatever the endpoint is spelled
    (_GH_API + _has(_MUTATION_WORD, "m", True)
     + _has(_MUTATION, _MUTATION_FIRST, True)),
    # a GraphQL document read from a file or stdin, which the guard cannot read
    _GH_API + _has(_GRAPHQL, "gG", True) + _has(_FILE_BODY, "-"),
    # an expansion that may hide the route or a flag, unless a pull request route
    # names the endpoint, as the review loop's reply does
    _GH_API + _EXPANSION + _lacks(_PULLS, "hr/"),
))


def validate(table):
    """(errors, warnings) for the [tracker] table."""
    errors = []
    if "repo" not in table:
        errors.append("tracker.repo is required, the owner/name of the GitHub repository the "
                      "issues live in")
    elif not (isinstance(table["repo"], str) and _REPO.fullmatch(table["repo"])):
        errors.append("tracker.repo %r is not an owner/name pair such as octo/widgets"
                      % (table["repo"],))
    prefix = table.get("task_prefix", DEFAULTS["task_prefix"])
    if not (isinstance(prefix, str) and _PREFIX.fullmatch(prefix)):
        errors.append("tracker.task_prefix %r is not a capital letter followed by capital "
                      "letters or digits, such as GH" % (prefix,))
    return errors, []


def usable(table):
    return isinstance(table, dict) and not validate(table)[0]


def mcp_write_regex(table):
    """No MCP tool writes GitHub Issues here, so the pattern matches nothing."""
    return _NOTHING


def mcp_read_tools(table):
    return []


def write_denials(table):
    """The Bash write patterns. They match a write to any repo, not only the one the
    table names, so `-R other/repo` is still a tracker write."""
    return list(_DENIALS)


def issue_number(task_id):
    """The issue number of a task id shaped <PREFIX>-<number>, such as 123 for GH-123."""
    match = _TASK_ID.fullmatch(task_id) if isinstance(task_id, str) else None
    if not match:
        raise PplError("SCHEMA_INVALID", "task id %r is not <PREFIX>-<issue number>, such as "
                       "GH-123" % (task_id,))
    return int(match.group(1))

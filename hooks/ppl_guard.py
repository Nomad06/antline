"""PreToolUse cage. Runs on every tool call, so it stays cheap and never raises."""
import json
import os
import re
import shlex
import sys
from pathlib import Path

# The floor module sits beside this file, and the script's own directory is not
# on sys.path under PYTHONSAFEPATH, -P or -I. Appended, so it shadows nothing.
_HOOKS_DIR = str(Path(__file__).resolve().parent)
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)

import ppl_python_floor

# Before any ppl import and any regex compile, both of which need 3.11: under
# an older interpreter the hook would die here and allow every call
# [R-GUARD-PYTHON-FLOOR]. Everything in this file parses on 3.9.
if __name__ == "__main__" and ppl_python_floor.refuse_old_python(
        sys.version_info, "ppl_guard", "PreToolUse", sys.stdin, sys.stdout, sys.stderr):
    sys.exit(0)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ppl import boundary, control, gitfacts, paths, run, store
from ppl import forges as _forges
from ppl.errors import PplError
from ppl.trackers import is_mcp_prefix, jira as _jira

# The Atlassian remote MCP server is registered under different names on different
# machines: `mcp__atlassian__` where it was added by hand, `mcp__claude_ai_Atlassian_Rovo__`
# where it came through claude.ai. A second registration of the same server gets a
# numbered name (`mcp__atlassian_2__`, `mcp__claude_ai_Atlassian_Rovo_2__`), so the gate
# accepts the numbered duplicate of every prefix. It matches the tracker write
# *operation* under any prefix the run's harness snapshot names - an exact-name table
# naming one prefix is silently dead under the other, and the registered PreToolUse
# matcher (README.md) must list every prefix too, or the hook never even sees the call
# [R-JIRA-GATE]. The constants below are the adapter's defaults, used when the run has
# no usable snapshot and by the guarded-name list further down.
JIRA_MCP_PREFIXES = tuple(_jira.DEFAULTS["mcp_prefixes"])
JIRA_WRITE_OPERATIONS = tuple(_jira.DEFAULTS["write_operations"])
# The enumerated names under each default prefix, for the rule matrix and the tests
# that need concrete tool names. `is_tracker_write_tool` is the check the cage runs.
JIRA_WRITE_TOOLS = _jira.write_tool_names(JIRA_MCP_PREFIXES, JIRA_WRITE_OPERATIONS)
_DEFAULT_TRACKER = {"kind": "jira",
                    "mcp_prefixes": list(JIRA_MCP_PREFIXES),
                    "write_operations": list(JIRA_WRITE_OPERATIONS)}


def default_tracker():
    """The adapter defaults as a tracker table, a fresh copy on every call."""
    return {"kind": _DEFAULT_TRACKER["kind"],
            "mcp_prefixes": list(_DEFAULT_TRACKER["mcp_prefixes"]),
            "write_operations": list(_DEFAULT_TRACKER["write_operations"])}


def _string_list(value):
    return (isinstance(value, list) and bool(value)
            and all(isinstance(item, str) and item for item in value))


def _usable_tracker(tracker):
    """A prefix that is not shaped mcp__<name>__ would gate nothing and still replace
    the defaults, so it makes the whole table unusable."""
    return (isinstance(tracker, dict)
            and _string_list(tracker.get("mcp_prefixes"))
            and all(is_mcp_prefix(prefix) for prefix in tracker["mcp_prefixes"])
            and _string_list(tracker.get("write_operations")))


def tracker_from_snapshot(task_id):
    """The tracker table frozen into the run's harness snapshot, or the defaults
    when the snapshot is missing or the table is malformed in any way. Never
    raises: a decide() that raised would be allowed by main() [R-JIRA-GATE]."""
    snapshot = load_snapshot(task_id)
    tracker = snapshot.get("tracker") if snapshot else None
    return tracker if _usable_tracker(tracker) else default_tracker()


def is_tracker_write_tool(tool, tracker):
    """True for a tracker write operation under any prefix the tracker table names.
    An unusable table or entry falls back to the defaults instead of raising."""
    if not isinstance(tool, str):
        return False
    if not _usable_tracker(tracker):
        tracker = _DEFAULT_TRACKER
    try:
        pattern = _jira.write_tool_regex(tracker["mcp_prefixes"], tracker["write_operations"])
    except Exception:
        pattern = _jira.write_tool_regex(_DEFAULT_TRACKER["mcp_prefixes"],
                                         _DEFAULT_TRACKER["write_operations"])
    return bool(pattern.match(tool))


def is_jira_write_tool(tool):
    """True for a Jira write operation under any default Atlassian MCP server prefix."""
    return is_tracker_write_tool(tool, default_tracker())


# The union of both adapters' denials under their default settings: what applies
# when no run is active. With a run, denied_tools() adds the harness forge's names
# under its configured server name and keeps the union [R-FORGE-DENIALS-FROM-ADAPTER].
DENIED_TOOLS = _forges.all_denied_tools()
# Every adapter's denied Bash patterns, applied to every session whatever forge
# the active run names.
_ALL_DENIED_BASH = tuple(pair for kind in _forges.KINDS
                         for pair in _forges.get(kind).denied_bash())

# The structured read tools. The test worker's read isolation
# (R-TEST-WORKER-READ-TOOLS, in decide) acts on exactly these three.
READ_TOOLS = ("Read", "Grep", "Glob")

# The tool that starts a subagent, under its current and its older name. Its
# PreToolUse payload carries the dispatch prompt and the subagent type before the
# subagent exists, and a deny stops the spawn (R-TEST-WORKER-DISPATCH-LEAK).
AGENT_TOOLS = ("Agent", "Task")

# Every tool name this cage acts on. `ppl doctor` judges the registered PreToolUse
# matcher against this list: a name the matcher never passes to the hook is a denial
# that cannot fire, however correct the code behind it reads [R-GUARD-MATCHER]. The
# recommended registration is the match-all matcher `.*` (README.md "Hooks"), so this
# list is what an operator who insists on an explicit matcher is held to - a recipe
# that leaves out a live tracker prefix or READ_TOOLS leaves those denials dead.
GUARDED_TOOL_NAMES = (
    ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
    + tuple(DENIED_TOOLS)
    + JIRA_WRITE_TOOLS
    # The first numbered duplicate of every default prefix, per operation - the shape
    # a second registration of the same server gets, which is_jira_write_tool accepts.
    + tuple(prefix[:-2] + "_2__" + operation
            for prefix in JIRA_MCP_PREFIXES
            for operation in JIRA_WRITE_OPERATIONS)
    + READ_TOOLS
    + AGENT_TOOLS
)

# Global git flags (-C <dir>, --git-dir=<x>, -c k=v, ...) sit between "git" and the
# subcommand. Without skipping them, any global flag defeats every pattern below at
# once - and "-C <worktree>" in particular is the natural thing to type when working
# across the pipeline's own worktrees, not an adversarial trick.
# The flag grammar has one parse per token, or it backtracks exponentially: `git`
# followed by 34 bare flags took 14 s, and a hook that runs past its timeout denies
# nothing. A flag value is one shell word: quoted pieces, backslash escapes and bare
# runs glued together (`"/my dir"/sub`, `a\'b`, `"a\"b"`), each piece told apart by
# its first character and matched possessively, so the word has one parse. A flag
# git documents as taking a value consumes the next word whatever it is (`-C -odd`).
# Any other flag takes any word after `=` (`--exec-path=-x`), and a spaced value
# only when the word is not a flag itself (`-` and `-1` are values, `-x`, `--x` and
# `'-x'` are not), so a flag token is always a flag of its own. An empty `--flag=` is a
# value-less flag, so `--git-dir= add` still reaches the verb. Backtracking stays
# possible, so a value-less flag before the subcommand (`--no-pager add -A`) still
# matches, and a config value naming a verb (`-c push.default=x`) is still a value.
# The shell unquotes before git runs, so a flag, the verb and the dangerous option
# may each wear one quote or backslash (`'add'`, `\-C`, `'-A'`): a guard that does
# not tolerate that is bypassed by two keystrokes.
_SHELL_WORD_PIECE = r"(?:'[^']*'|\"(?:[^\"\\]|\\[\s\S])*+\"|\\[\s\S]|[^\s'\"\\]+)"
_SHELL_WORD = _SHELL_WORD_PIECE + "++"
_SHELL_WORD_OR_EMPTY = _SHELL_WORD_PIECE + "*+"
_SHELL_QUOTE = r"[\"'\\]?"
# What may follow the dangerous option: anything but a word character, so a
# terminator glued to it (a semicolon, `-A>/dev/null`, `-A)`) is still the
# option, and `-Ax` is a different flag.
_OPTION_END = r"(?!\w)"
# A bare `git stash` is the one that pushes without a tag: the verb must be
# followed by the end of the command or a terminator (a separator, a redirect, a
# comment, a substitution or an expansion), not by a subcommand.
_BARE_END = r"\s*(?:[;&|)<>#\n`$]|\d[<>]|$)"


def _quotable(literal):
    return _SHELL_QUOTE + literal + _SHELL_QUOTE


_GIT_VALUE_FLAG = _quotable(
    r"(?:-[Cc]|--(?:git-dir|work-tree|namespace|super-prefix|config-env|attr-source))")
_GIT_OTHER_FLAG = _quotable(r"(?:-[A-Za-z]|--[A-Za-z-]+)")
_GIT_FLAGS = (r"(?:\s+(?:" + _GIT_VALUE_FLAG
              + r"(?:=" + _SHELL_WORD_OR_EMPTY + r"|\s+" + _SHELL_WORD + r")"
              r"|(?!" + _GIT_VALUE_FLAG + r"(?:[=\s]|$))" + _GIT_OTHER_FLAG
              + r"(?:=" + _SHELL_WORD_OR_EMPTY
              + r"|\s+(?!" + _SHELL_QUOTE + r"-[A-Za-z-])" + _SHELL_WORD + r")?))*")

# A dangerous short option may ride in a cluster (`-vA` is add-all, `-fu` is a
# force push). The cluster is read in linear time: a lookahead finds the letter
# once, then a possessive run takes the letters, so a 4 KB cluster that ends in
# a digit fails in one pass. `-[A-Za-z]*f[A-Za-z]*` tried every split and took
# 8 s on that shape, past the hook budget even under the role cap. The add
# cluster is spelled from add's own short options, so `-Ax`, which git refuses,
# is not read as add-all. The push cluster admits `4` and `6`, push's address
# family flags, so `-f4` is still a force push.
_ADD_ALL_CLUSTER = r"-(?=[nvfipeuAN]*A)[nvfipeuAN]++"
_FORCE_CLUSTER = r"-(?=[A-Za-z46]*f)[A-Za-z46]++"
# Plain options may sit between the verb and the dangerous option (`add -v -A`,
# `add -- .`, `reset -q --hard`, `stash -q`): a run of flag words, none of them
# the dangerous option itself, so the run has one parse. A cluster carrying it
# (`-vA`) may be read by the run too, and is given back in one step when the
# dangerous option has to follow. An option value is read in its `=` form only.
# A spaced one (`--chmod +x`) would bring back the value-or-flag ambiguity, so
# it stays a residual.
_GIT_PLAIN_OPTIONS = (r"(?:\s+" + _SHELL_QUOTE + r"(?!(?:-A|--all|--hard)(?!\w))"
                      r"(?:--|-{1,2}[A-Za-z][A-Za-z-]*)" + _SHELL_QUOTE
                      + r"(?:=" + _SHELL_WORD_OR_EMPTY + r")?)*")

# What makes a `stash drop` unnamed: the options after the verb are read
# possessively, then one lookahead asks for a stash@{N} word on the same line.
# Possessive, because a backtracking run could give an option back and find no
# ref after the shorter run (`drop -q stash@{0}` would deny), and one parse per
# token keeps a padded command inside the hook's budget. Spaces and tabs only:
# a newline ends the command, so `drop` then `stash@{0}` on the next line is a
# bare drop.
_STASH_DROP_UNNAMED = (r"[\"'\\]?+(?:[ \t]+" + _SHELL_QUOTE + r"(?:-{1,2}[A-Za-z][A-Za-z-]*|--)"
                       + _SHELL_QUOTE + r")*+"
                       + r"(?![ \t]+" + _SHELL_QUOTE + r"stash@\{\d+\}" + _SHELL_QUOTE
                       + r"(?![^\s;&|)<>#]))")

DENIED_COMMANDS = (
    # `.`, `./`, `..` and `./*` are add-all, a dot that starts a path
    # (`./src`, `.gitignore`, `./.github`, `../x`) is not.
    (re.compile(r"\bgit" + _GIT_FLAGS + r"\s+" + _quotable("add") + _GIT_PLAIN_OPTIONS + r"\s+"
                + _quotable(r"(?:" + _ADD_ALL_CLUSTER + r"|--all|\.(?![./]*\w))") + _OPTION_END),
     "git add -A can sweep a live credential into a commit, add explicit paths"),
    (re.compile(r"\bgit" + _GIT_FLAGS + r"\s+" + _quotable("stash") + _GIT_PLAIN_OPTIONS
                + _BARE_END),
     "the stash stack is shared across worktrees, use git stash push -u -m <tag>"),
    # pop and clear remove entries from the stack every session shares: clear
    # empties it, pop takes the top entry, whoever pushed it.
    (re.compile(r"\bgit" + _GIT_FLAGS + r"\s+" + _quotable("stash") + r"\s+" + _SHELL_QUOTE
                + r"(?:pop|clear)\b"),
     "the stash stack is shared, pop and clear remove other sessions' entries, "
     "use git stash apply <sha> instead"),
    # The shared-stack recipe is push -m <tag>, apply <sha>, then drop the entry by
    # the index it is re-found at, so `drop stash@{N}` is legitimate. A drop that
    # names no stash@{N} takes the top entry, likely another session's.
    (re.compile(r"\bgit" + _GIT_FLAGS + r"\s+" + _quotable("stash") + r"\s+" + _SHELL_QUOTE
                + r"drop\b" + _STASH_DROP_UNNAMED),
     "a bare git stash drop takes the top entry, which is likely another session's, "
     "drop yours by the stash@{N} it is re-found at"),
    # `f` is force in every push cluster. The lookbehind keeps `--force-with-lease`
    # and `--follow-tags` out of this alternative, `--force` has its own. A `+`
    # leading a refspec (`+main`, `+HEAD:main`) force-pushes that ref with no
    # flag at all, and a `+` that leads no refspec (`1 + 2`) is not one. The
    # refspec is looked for inside the push's own command only, up to the next
    # separator, so `git push origin main && git log --grep +x` is no force push.
    # The `&` of an fd redirect (`2>&1`, `>&f`, `&>f`) is no separator. The
    # span's three alternatives never match the same character: one takes no
    # `&`, one an `&` after `>`, one an `&` before `>` with no `>` behind it.
    (re.compile(r"\bgit" + _GIT_FLAGS + r"\s+" + _quotable("push") + r"(?!\w)"
                r"(?:.*(?:--force\b|(?<=\s)" + _quotable(_FORCE_CLUSTER) + _OPTION_END + r")"
                + r"|(?:[^;&|\n]|(?<=>)&|(?<!>)&(?=>))*(?<=\s)[\"']?\+[^\s\"'+-])"),
     "force push is irreversible on a shared ref"),
    (re.compile(r"\bgit" + _GIT_FLAGS + r"\s+" + _quotable("reset") + _GIT_PLAIN_OPTIONS
                + r"\s+" + _SHELL_QUOTE + r"--hard\b"),
     "hard reset discards worker output that has no receipt"),
)

# The patterns above are linear per token but a long tail still costs time per
# `git` start, and a hook that runs past its timeout denies nothing: 16 KB of
# `git -a ` took 4.8 s, 4 KB takes 0.3 s. A role has no business sending a 4 KB
# command line (a script inside its write boundary does the same work), so a
# role's command is refused on length before any pattern runs
# [R-GUARD-ROLE-COMMAND-LENGTH]. The unroled session keeps no cap.
ROLE_COMMAND_LENGTH_LIMIT = 4096

# ppl writes run state only through its own API (store.write_json via run.save /
# control.set_command / etc), never through a shell one-liner, so pipeline work never
# needs a Bash command to write inside runs/. A command that names the runs
# directory together with a write-shaped construct is denied outright, and so is one
# whose write target resolves under it however it is spelled (bash_write_lands_in_runs):
# a false deny here is recoverable (use the ppl CLI), a false allow is not (unaudited
# run-state mutation, the same class of hole this module exists to close for git).
#
# Round 2 (whole-branch review, wave A): `touch`, `truncate`, `ln` and
# `install` were missing. The coordinator found this by probing
# ppl-code-worker specifically, after ppl-test-worker's read allowlist
# (built for an unrelated purpose) denied all four by accident and nearly
# read as coverage - a worker with no read allowlist has nothing else
# standing in front of them. All four are ordinary shell verbs a worker
# would plausibly reach for, not obfuscation: `touch` can create a file
# inside a forbidden path, `truncate -s 0` destroys a file's content in
# place, `ln -sf` replaces a source with a symlink pointing anywhere, and
# `install` writes its destination argument like `cp`.
#
# Round 3 (post-branch review): `rsync` and `patch` write their destination
# like `cp`/`install`, and a handful of git subcommands rewrite tree paths
# with no redirect or file verb anywhere in the command - `git rm`, `git mv`,
# `git checkout <ref> -- <path>`, `git restore`, `git apply <diff>`, and the
# whole-tree rewriters `merge`/`rebase`/`cherry-pick`/`pull`/`revert`/`clean`.
# None had a shape here, so `git checkout origin/x -- src/test/Foo.java`
# from a code worker extracted no target at all. Named here for the
# runs-root and unparseable-command checks - `_git_write_targets` below is
# what extracts the path. (`>>?` already covers bash's `&>` and `&>>`.)
BASH_WRITE_SHAPES = re.compile(
    r">>?|\btee\b|\bsed\b[^\n]*-i\b|\bcp\b|\bmv\b|\bdd\b[^\n]*\bof=|\brm\b"
    r"|\btouch\b|\btruncate\b|\bln\b|\binstall\b|\brsync\b|\bpatch\b"
    r"|\bgit\b[^\n]*\b(?:rm|mv|checkout|switch|restore|apply|merge|rebase|cherry-pick"
    r"|pull|revert|clean)\b"
)

# Round 1 review (C4): the registry, the lease, the done gate - all of it is
# theatre if a worker role can just run the ppl CLI itself through the Bash
# tool it already holds (ppl-code-worker and ppl-test-worker both carry it).
# `ppl findings status <id> --status fixed`, `ppl evidence add ... --result
# pass`, `ppl transition done` - none of these write a path BASH_WRITE_SHAPES
# or the runs-root check above would ever see, because they write through
# ppl's own API, not a shell redirect. A worker marking its own blocking
# finding fixed, or opening its own done gate, is not a false allow at the
# edge of the cage - it is the cage's whole promise undone from inside.
#
# Fixed by identity, not by shape: every role in boundary.ROLE_RULES is denied
# every STATE-CHANGING ppl subcommand, full stop, regardless of scope or cwd -
# only the orchestrator (agent_type None) drives ppl. The read-only
# subcommands a role might genuinely need to look something up - `ppl show
# --task <id>`, `ppl findings list` - stay allowed, everything else is
# deny-by-default, including a pair the CLI does not take (`ppl show <id>`),
# the same conservative direction as R-TEST-WORKER-NO-DIFF: a false deny
# costs one clarifying round, a false allow is a role opening its own gate
# [R-GUARD-PPL-CLI-DENIED].
#
# Round 2 review: PPL_CLI_RE.search() read only the FIRST "ppl ..."
# occurrence, and "no match at all" meant allow - both fail open (`ppl show
# --json && ppl findings status ...` hid a second invocation behind a
# read-only one - `ppl --task X findings status ...`, a reordered flag, did
# not match the old pattern shape at all). Fixed by scanning every
# occurrence of the word "ppl" and anchoring the subcommand match at that
# exact position with `.match(command, pos)`, never `.search()` - an
# unparseable shape now denies rather than defaults to allow. Also
# tightened during that fix: a bare `\bppl\b` also matched "ppl" inside an
# unrelated token like the git stash tag in `git stash push -u -m ppl-700`,
# so the word now additionally requires whitespace, a shell separator, or
# end of string right after it.
#
# Round 3 review: three more shapes probed, three already denied for free
# (a variable set to ppl and then invoked, `bash -c 'ppl transition done'`,
# `echo 'ppl transition done' | sh` - the literal word survives in all
# three, so the scan above already catches them) and three did not -
# `python3 -m ppl.cli
# findings status ...`, `p''pl transition done`, and a read-only ppl call
# hiding a python3 -m one after `&&`. Of those, `python3 -m ppl[.cli]` and a
# bare `ppl.cli` token are a realistic accident (a worker whose PATH lacks
# `ppl` reaches for `python3 -m` with no intent to evade anything), so
# PPL_INVOCATION_RE below matches those shapes too, feeding the exact same
# read-only allowlist and the exact same fail-closed default. `p''pl` (or
# base64, or any other quote/encoding trick) is not: a lexical matcher
# cannot win an arms race against arbitrary shell quoting, and chasing this
# one instance would buy a longer regex and a false sense of completeness,
# not a closed class - left uncaught, named honestly below rather than
# quietly patched over.
#
# What this whole file actually buys, stated once rather than left to be
# inferred from "necessary but not sufficient": this cage stops shortcuts,
# not a determined role. Every lexical check in this module is a speed
# bump against an agent taking the easy path, not a proof no path exists.
# The second fence is narrower than the cage: `require_current_generation`
# (ppl/cli.py) refuses a write that cannot produce the run's current lease
# generation, and it guards exactly `ppl brief challenge`, `ppl evidence add`,
# `ppl verdict`, `ppl findings add|status|delete`, `ppl review answered` and
# `ppl apply-patch`.
# Every other state-changing command, `ppl transition done` and `ppl stage
# enter` among them, takes no generation, so for those this lexical check is
# the only fence a role meets, and it is a speed bump, not a proof.
PPL_INVOCATION_RE = re.compile(
    r"\bpython3?\s+-m\s+ppl(?:\.cli)?(?=[\s;&|]|$)"
    r"|\bppl\.cli(?=[\s;&|]|$)"
    # Not after a dot: `~/.ppl`, the default data root, is a directory, while
    # `bin/ppl` and `./ppl` are the launcher.
    r"|(?<!\.)\bppl(?=[\s;&|]|$)"
)
PPL_SUBCOMMAND_RE = re.compile(r"\s+([A-Za-z][\w-]*)(?:\s+([A-Za-z][\w-]*))?")

# (command, action) pairs that are read-only, i.e. never reach store.write_json,
# run.save, or an append_log-backed mutation. `action` is None for a command
# that takes no positional action word at all (ppl show, ppl status, ...).
PPL_READ_ONLY_SUBCOMMANDS = frozenset({
    ("show", None), ("status", None), ("doctor", None), ("metrics", None),
    ("findings", "list"), ("control", "get"), ("routing", "show"),
    ("approval", "check"), ("mr", "state"), ("review", "pending"),
})


def _ppl_cli_is_state_changing(command):
    """True when `command` contains ANY invocation of the ppl CLI - directly
    as `ppl`, or as `python3 -m ppl[.cli]` / a bare `ppl.cli` token - that is
    not provably read-only. Lexical, like every other Bash check in this
    module: it looks for a fixed set of literal shapes, at every occurrence,
    not only the first. See this module's own limit, spelled out above the
    regexes, before reading this as more than a speed bump.
    """
    for word in PPL_INVOCATION_RE.finditer(command):
        shape = PPL_SUBCOMMAND_RE.match(command, word.end())
        if not shape or (shape.group(1), shape.group(2)) not in PPL_READ_ONLY_SUBCOMMANDS:
            return True
    return False

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")

# R-TEST-WORKER-READ-TOOLS: Read, Grep and Glob arrive as structured tool
# calls carrying a path argument, so this half is exact - a field is read,
# not guessed at. Grep and Glob may omit "path" entirely (searches the
# session's cwd), and an omitted path is not "unscoped, therefore allowed":
# the fallback below still resolves to a real directory (the cwd), and if
# even that is unavailable the caller denies rather than skip the check.
READ_PATH_KEYS = ("file_path", "path", "notebook_path")


def read_target_path(tool_input, payload):
    for key in READ_PATH_KEYS:
        if tool_input.get(key):
            return str(tool_input[key])
    return payload.get("cwd") or None


# R-TEST-WORKER-BASH-READS: Bash arrives as one opaque string, so this half
# is lexical. Round 1 review of this task found the first version was a
# BLOCKLIST - deny a fixed set of read verbs, allow everything else - and
# ruled it unsound in principle, not merely incomplete: the design's own
# sentence is "any command shape that cannot be classified is denied",
# and a blocklist can never satisfy that, because an unrecognised command is
# by construction unclassified and therefore allowed under a blocklist. Naming
# more verbs (nl, tac, od, xxd, strings, ...) would not fix it either -
# `python3 -c "print(open('x').read())"` alone subsumes every reader anyone
# could think of.
#
# Fixed by inversion: this is now an ALLOWLIST. The test worker's Bash is
# denied unless the WHOLE command is accounted for, segment by segment, by
# the small set of shapes it genuinely needs - the build/test shapes the
# run's harness snapshot declares (runs/<task>/harness.json, see
# ShapeMatcher below), the "echo $? redirected to a file" rc-idiom the gate
# recipes chain after each build, the repo commands the role genuinely needs
# (git status, git add of test paths, git commit, ls), and reading test
# sources, which the exact half already gets right for Read/Grep/Glob and
# this half now mirrors for Bash.
# Anything else - including a shape this cannot parse at all - denies. A
# command substitution (`$(` or a backtick) or a process substitution (`<(`
# or `>(`) anywhere in the raw string denies the whole command outright,
# before any segment is even looked at: these are the constructs that run a
# second command from inside one word of an otherwise-allowed shape (a
# module name, a commit message, a -D value, a file argument), and bash runs
# a process substitution even mid-word, where shlex sees one ordinary token.

# A shell separator between simple commands. Deliberately does not include a
# bare "&" (backgrounding): "2>&1", an ordinary and necessary part of the
# gates recipe's own redirect idiom, contains a lone "&" that a separator set
# including it would cut in half. Nothing in the allowed shapes below ends in
# a bare "&", so a segment that does simply fails to match any of them and
# denies like any other unrecognised shape - no special handling needed.
# The `|` of a `>|` clobber redirect is not a pipe.
_SEGMENT_SEPARATOR_RE = re.compile(r"(;|\n|&&|\|\||(?<!>)\|)")


def _segments_with_pipe_flag(command):
    """Every non-empty segment of `command`, paired with whether it is fed by
    a genuine pipe (`|`), as opposed to `;`, `&&`, `||` or a newline.

    Round 2 review (C2, whole-branch): the original `_read_verb_allowed`
    treated ANY empty path list as "filtering a pipe", including a `grep -r`
    or a bare `rg` that reads the tree on its own with no upstream pipe at
    all - the allow test that pinned that premise was a real pipe
    (`echo hello | grep hello`), and the code generalised past what the test
    proved. Recovering which separator preceded a segment is what lets
    `_read_verb_allowed` tell those apart.
    """
    parts = _SEGMENT_SEPARATOR_RE.split(command)
    segments = []
    preceded_by_pipe = False
    for part in parts:
        if part in (";", "\n", "&&", "||", "|"):
            preceded_by_pipe = part == "|"
            continue
        text = part.strip()
        if text:
            segments.append((text, preceded_by_pipe))
    return segments


def _segments(command):
    return [text for text, _ in _segments_with_pipe_flag(command)]


def _shlex_tokens(text):
    try:
        return shlex.split(text)
    except ValueError:
        return None


# Redirect tokens this module strips off the end of a segment before
# classifying the command itself - "2>&1" is one token to shlex (no internal
# whitespace), "> file" or "2> file" are two.
#
# Round 3 (post-branch review): bash's `&>` / `&>>` (stdout and stderr into
# ONE file) and the csh-style `>&` are redirect operators too, and were
# missing both here and in the redirect extraction below - so
# `echo x &> <test source>` extracted no target at all and walked past the
# write boundary for every role, and for an unroled session. An ordinary
# idiom, the same class as the "2>&1" this already handled - not a quoting
# trick of the kind the module comment above declines to chase.
_REDIR_FD_RE = re.compile(r"^\d*>&\d+$")
_REDIR_OP_RE = re.compile(r"^(?:\d*>\||\d*>>?|&>>?|>&)$")


def _strip_trailing_redirect(tokens):
    tokens = list(tokens)
    while tokens:
        if _REDIR_FD_RE.match(tokens[-1]):
            tokens.pop()
            continue
        if len(tokens) >= 2 and _REDIR_OP_RE.match(tokens[-2]):
            tokens.pop()
            tokens.pop()
            continue
        break
    return tokens


def _resolve_path_against_cwd(raw_path, cwd):
    """`raw_path` resolved to an absolute path against `cwd`, or None when it
    cannot be pinned down at all: empty, carrying a shell variable or command
    substitution marker, or relative with no cwd to resolve it against.
    Shared by the test-worker's read-scoping check below and the Bash
    write-boundary check (R-GUARD-BASH-WRITE-BOUNDARY) - both need the same
    "resolve, or admit you cannot" discipline, just applied to a different
    verdict once resolution succeeds.
    """
    if not raw_path or "$" in raw_path or "`" in raw_path:
        return None
    if raw_path == "~" or raw_path.startswith("~/"):
        # `~/x` is $HOME/x, not <cwd>/~/x - joining it onto cwd used to land
        # it inside the repo and deny a write that is not inside the repo.
        raw_path = os.path.expanduser(raw_path)
    if os.path.isabs(raw_path):
        return os.path.normpath(raw_path)
    if not cwd:
        return None
    return os.path.normpath(os.path.join(cwd, raw_path))


def _path_is_provably_test_scoped(raw_path, state, cwd):
    """True only when `raw_path`, resolved against `cwd`, is confirmed to
    land on a test source inside the active run's repo. Unlike a deny-model
    check, ambiguity here means False, not True: a path this cannot resolve
    (a shell variable, a command substitution - though those are already
    denied for the whole command above), or one that resolves outside the
    repo, is not "safe because it is not our business", it is simply not
    proven, and this is an allowlist.
    """
    resolved = _resolve_path_against_cwd(raw_path, cwd)
    if resolved is None:
        return False
    if not targets_repo(resolved, state):
        return False
    rel = relative_to_repo(resolved, state)
    return boundary.path_is_test_scoped(rel)


# --- Shape 1: the build/test invocations the run's harness declares in its
# [[guard.shape]] rows. ppl init freezes them into runs/<task>/harness.json,
# and the hook reads that snapshot, never the live harness, so an edit to the
# harness mid-run cannot widen what the cage allows. Flags and goals are
# accepted in whichever order they appear, since a gate recipe may put its
# goal right after argv0 or at the very end. A missing, unreadable or
# malformed snapshot yields no shapes, so every build denies. ---

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S+$")
_ENV_EXPORT_RE = re.compile(r"^export\s+[A-Za-z_][A-Za-z0-9_]*=\S+$")
# The worker picks the token a harness regex runs against, so no token longer
# than this ever reaches one. That bounds a regex whose cost grows polynomially
# with the token. It does not bound a regex with nested quantifiers, which
# backtracks exponentially on a short token: harness regexes are operator-authored,
# see R-GUARD-SHAPES-FROM-SNAPSHOT's residual. The hook has a 5 s budget, and a
# hook that runs out of time denies nothing.
_SHAPE_TOKEN_MAX = 4096
# The substitutions bash runs from inside a single word, see the
# R-TEST-WORKER-BASH-READS comment above.
_SUBSTITUTION_MARKERS = ("$(", "`", "<(", ">(")


class ShapeMatcher:
    """The build-tool shapes a harness declares, token by token: an optional
    env prefix, argv0 exactly, every required flag (with its value matching
    the declared regex), exactly one goal from one_goal_of when the list is
    non-empty, and any other token matching free_args. A regex must match
    the whole token, anchored or not, so an unanchored one is no prefix
    match. A token longer than _SHAPE_TOKEN_MAX denies before any regex
    runs. Anything else denies. An empty matcher allows nothing
    [R-GUARD-SHAPES-FROM-SNAPSHOT]."""

    def __init__(self, rows):
        self.rows = rows

    @classmethod
    def from_rows(cls, rows):
        """A malformed row (a hand-edited snapshot) is skipped, never raised:
        the hook allows on an exception, so raising here would fail open.
        Any exception counts, since re.compile alone can raise OverflowError
        (a huge repeat count) or RecursionError (deep nesting) besides
        re.error."""
        compiled = []
        for row in rows if isinstance(rows, list) else []:
            try:
                compiled.append(cls._compile(row))
            except Exception:
                continue
        return cls(compiled)

    @staticmethod
    def _compile(row):
        if not isinstance(row, dict):
            raise TypeError("a shape row must be an object")
        argv0 = row["argv0"]
        if not isinstance(argv0, str) or not argv0:
            raise TypeError("argv0 must be a non-empty string")
        env_prefix = row.get("env_prefix", False)
        if not isinstance(env_prefix, bool):
            raise TypeError("env_prefix must be a boolean")
        entries = row.get("required_flags") or []
        if not isinstance(entries, list):
            raise TypeError("required_flags must be a list")
        flags = {}
        for entry in entries:
            if not (isinstance(entry, list) and len(entry) == 2
                    and all(isinstance(part, str) for part in entry)):
                raise TypeError("a required flag is [flag, value_regex]")
            flag, pattern = entry
            flags[flag] = re.compile(pattern) if pattern else None
        goals = row.get("one_goal_of") or []
        if not (isinstance(goals, list) and all(isinstance(goal, str) for goal in goals)):
            raise TypeError("one_goal_of must be a list of strings")
        free = row.get("free_args") or ""
        if not isinstance(free, str):
            raise TypeError("free_args must be a string")
        return {"argv0": argv0, "env_prefix": env_prefix, "flags": flags,
                "goals": frozenset(goals), "free": re.compile(free) if free else None}

    def allows(self, tokens):
        if any(len(tok) > _SHAPE_TOKEN_MAX for tok in tokens):
            return False
        return any(self._row_allows(row, tokens) for row in self.rows)

    @staticmethod
    def _row_allows(row, tokens):
        i = 0
        if row["env_prefix"]:
            while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
                i += 1
        tokens = tokens[i:]
        if not tokens or tokens[0] != row["argv0"]:
            return False
        rest = tokens[1:]
        flags = row["flags"]
        seen = set()
        goal = None
        i = 0
        while i < len(rest):
            tok = rest[i]
            if tok in flags:
                pattern = flags[tok]
                if pattern is None:
                    seen.add(tok)
                    i += 1
                    continue
                if i + 1 >= len(rest) or not pattern.fullmatch(rest[i + 1]):
                    return False
                seen.add(tok)
                i += 2
                continue
            if tok in row["goals"] and goal is None:
                goal = tok
                i += 1
                continue
            if row["free"] is not None and row["free"].fullmatch(tok):
                i += 1
                continue
            return False
        if len(seen) != len(flags):
            return False
        if row["goals"] and goal is None:
            return False
        return True


def load_snapshot(task_id):
    """runs/<task>/harness.json, or None when missing or unreadable. The caller
    treats None as 'no harness shapes' [R-GUARD-SNAPSHOT-FAIL-CLOSED]."""
    if not task_id:
        return None
    try:
        doc = store.read_json(paths.run_dir(task_id) / "harness.json", None)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def harness_shapes(task_id):
    """The run's ShapeMatcher, or an empty one on any failure at all, so a
    snapshot that breaks the reader or the compiler denies every build
    instead of raising into main(), which would allow the call."""
    try:
        snapshot = load_snapshot(task_id)
        return ShapeMatcher.from_rows((snapshot or {}).get("shapes") or [])
    except Exception:
        return ShapeMatcher([])


def _active_task_id():
    """run.active(), or None when the ACTIVE file cannot be read at all, so the
    forge denials fall back to the union instead of raising into main()."""
    try:
        return run.active()
    except Exception:
        return None


def forge_from_snapshot(task_id):
    """(adapter module, forge table) for the forge the run's harness snapshot
    names, or (None, None) when there is no snapshot or its forge table is not
    an object naming a known kind. Never raises."""
    try:
        snapshot = load_snapshot(task_id)
        settings = (snapshot or {}).get("forge")
        if not isinstance(settings, dict) or settings.get("kind") not in _forges.KINDS:
            return None, None
        return _forges.get(settings["kind"]), settings
    except Exception:
        return None, None


def denied_tools(task_id):
    """The union, plus the snapshot adapter's names under the run's own server
    name. The union stays in, so a renamed server never un-denies the default
    names [R-FORGE-DENIALS-FROM-ADAPTER]."""
    adapter, settings = forge_from_snapshot(task_id)
    if adapter is None:
        return DENIED_TOOLS
    try:
        extra = adapter.denied_tools(settings)
    except Exception:
        return DENIED_TOOLS
    merged = dict(DENIED_TOOLS)
    if isinstance(extra, dict):
        merged.update(extra)
    return merged


def forge_bash_denial(command, task_id):
    """The reason any forge adapter denies `command` as Bash, or None. The
    union applies whatever forge the run names, like denied_tools(), so a
    never-merge rule does not depend on which run is active. A project on one
    forge never needs the other's merge command [R-FORGE-GH-NO-APPROVE-MERGE].
    `task_id` is unused and kept so every forge lookup takes the same
    arguments."""
    if not isinstance(command, str):
        return None
    for pattern, reason in _ALL_DENIED_BASH:
        if pattern.search(command):
            return reason
    return None


_DEV_NULL = "/dev/null"


def _strip_harmless_redirect(tokens):
    """(tokens without their trailing redirects, clean). Only an fd duplication
    (`2>&1`) or a redirect into /dev/null, spaced or glued, is harmless. Any
    other trailing redirect writes a file, so clean is False. Only the
    read-only review responder uses this. The test worker keeps
    _strip_trailing_redirect, since its gate recipes log to files."""
    tokens = list(tokens)
    clean = True
    while tokens:
        last = tokens[-1]
        if _REDIR_FD_RE.match(last):
            tokens.pop()
            continue
        if len(tokens) >= 2 and _REDIR_OP_RE.match(tokens[-2]):
            clean = clean and last == _DEV_NULL
            del tokens[-2:]
            continue
        glued = _REDIR_GLUED_RE.match(last)
        if glued:
            clean = clean and glued.group(1) == _DEV_NULL
            tokens.pop()
            continue
        break
    return tokens, clean


def review_responder_bash_denied(command, task_id):
    """The review responder's Bash is an allowlist of the forge's read shapes,
    like the test worker's [R-REVIEW-RESPONDER-BASH-SHAPES]. No forge, or a
    forge that grants no shapes, means every command denies. A substitution
    marker anywhere in the raw string denies before any segment is read. So
    do a trailing redirect into a file, a `<` or `>` inside any other token,
    and any failure inside this check."""
    try:
        if not isinstance(command, str):
            return True
        if any(marker in command for marker in _SUBSTITUTION_MARKERS):
            return True
        adapter, _ = forge_from_snapshot(task_id)
        rows = adapter.bash_shapes("ppl-review-responder") if adapter else []
        shapes = ShapeMatcher.from_rows(rows)
        for segment, _pipe in _segments_with_pipe_flag(command):
            tokens = _shlex_tokens(segment)
            if tokens is None or not tokens:
                return True
            body, clean = _strip_harmless_redirect(tokens)
            # shlex keeps `--jq=.x>/tmp/x` one token, bash splits it at `>`.
            # No read shape needs `<` or `>`, so either one anywhere denies.
            if not clean or any("<" in tok or ">" in tok for tok in body):
                return True
            if not shapes.allows(body):
                return True
        return False
    except Exception:
        return True


# --- Shape 2: echo, which the recipe's own exit-code idiom depends on
# ("echo $? redirected to a file", already split into its own segment above
# by the leading separator). echo never reads a file by itself - it prints
# its own arguments - so any invocation of it is allowed rather than only
# the exact rc-idiom shape. The substitution guard above already denies the
# ways echo's arguments could run another command (`echo $(cat main.java)`,
# `echo <(cat main.java)`). ---

def _echo_allowed(tokens):
    return bool(tokens) and tokens[0] == "echo"


# --- Shape 3: the repo commands the role genuinely needs - git status,
# git add of test paths only, git commit, ls. Not git diff/show: the worker
# has no documented need for either, so neither is in the derived allowlist
# at all (round 1 review of this task - the earlier version tried to keep a
# restricted git diff/show and that was still more surface than derived). ---

_GIT_STATUS_FLAGS = frozenset({"-s", "--short", "-b", "--branch", "--porcelain"})
_GIT_COMMIT_FLAGS_VALUE = frozenset({"-m"})
_GIT_COMMIT_FLAGS_NO_VALUE = frozenset({"-q", "-a", "--no-verify"})


def _git_status_allowed(tokens):
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "status":
        return False
    return all(t in _GIT_STATUS_FLAGS for t in tokens[2:])


def _git_commit_allowed(tokens):
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] != "commit":
        return False
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if tok in _GIT_COMMIT_FLAGS_VALUE:
            if i + 1 >= len(tokens):
                return False
            i += 2
            continue
        if tok in _GIT_COMMIT_FLAGS_NO_VALUE:
            i += 1
            continue
        return False
    return True


def _git_add_allowed(tokens, state, cwd):
    if len(tokens) < 3 or tokens[0] != "git" or tokens[1] != "add":
        return False
    return all(_path_is_provably_test_scoped(t, state, cwd) for t in tokens[2:])


def _ls_allowed(tokens):
    return bool(tokens) and tokens[0] == "ls"


# --- Shape 4: reading test sources - the same verbs the earlier blocklist
# named, now used to recognise an ALLOWED shape instead of a denied one. A
# verb here with no file argument at all is filtering a pipe, not reading a
# path from disk, and is allowed on that basis - a definite classification,
# not an unclassifiable one. ---

_READ_VERBS = frozenset({"cat", "head", "tail", "less", "grep", "rg", "awk", "sed"})
_READ_VALUE_FLAGS = {
    "head": frozenset({"-n", "-c"}),
    "tail": frozenset({"-n", "-c"}),
    "grep": frozenset({"-e", "-f", "-A", "-B", "-C", "-m", "--include", "--exclude"}),
    "rg": frozenset({"-e", "-f", "-A", "-B", "-C", "-m", "--include", "--exclude"}),
    "sed": frozenset({"-e", "-f"}),
    "awk": frozenset({"-F", "-v", "-f"}),
}
_READ_PATTERN_VERBS = frozenset({"grep", "rg", "awk", "sed"})
_READ_PATTERN_FLAGS = frozenset({"-e", "-f"})

# Round 2 review (C2, whole-branch): `rg` is recursive by default with no
# path operand at all - it does not need a flag to walk the tree, and it
# does not read stdin unless given a literal "-" argument. GNU/BSD `grep`
# only behaves the same way once `-r` or `-R` is given (bundled into a
# short-option group like "-rn" or "-nr", not only standalone). Neither
# fact has anything to do with whether the command is fed by a pipe: a
# piped `grep -r pattern` still walks "." and ignores stdin. So a verb
# this set names is a tree-read whenever it is present, regardless of the
# pipe flag. A bare `grep`/`head`/`tail`/`awk`/`sed` with no path operand
# and NO recursive flag is the only shape that is actually reading stdin,
# and even that is a genuine pipe filter only when a real "|" fed it -
# not merely because the path list happens to be empty.
_GREP_RECURSIVE_FLAGS = frozenset({"-r", "-R", "--recursive"})


def _grep_marks_recursive(tok):
    if tok in _GREP_RECURSIVE_FLAGS:
        return True
    # a bundled short-option group, e.g. "-rn" / "-nr" - "-r"/"-R" can sit
    # anywhere in it, not only first, and it takes no value so bundling it
    # with any other no-value flag is ordinary grep usage.
    return tok.startswith("-") and not tok.startswith("--") and (
        "r" in tok[1:] or "R" in tok[1:])


def _read_verb_allowed(tokens, state, cwd, preceded_by_pipe):
    if not tokens or tokens[0] not in _READ_VERBS:
        return False
    verb = tokens[0]
    value_flags = _READ_VALUE_FLAGS.get(verb, frozenset())
    has_pattern_arg = verb in _READ_PATTERN_VERBS
    consumed_pattern = not has_pattern_arg
    recursive = verb == "rg"
    paths = []
    skip_next = False
    for tok in tokens[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-") and tok != "-":
            if verb == "grep" and _grep_marks_recursive(tok):
                recursive = True
            if has_pattern_arg and tok in _READ_PATTERN_FLAGS:
                consumed_pattern = True
            if tok in value_flags:
                skip_next = True
            continue
        if not consumed_pattern:
            consumed_pattern = True
            continue
        paths.append(tok)
    if not paths:
        if recursive:
            # rg with no path, or grep -r/-R with no path operand: this
            # reads the tree from cwd, piped or not - not a pipe filter.
            return False
        return preceded_by_pipe
    return all(_path_is_provably_test_scoped(p, state, cwd) for p in paths)


def _segment_allowed(segment, state, cwd, preceded_by_pipe, shapes):
    if _ENV_EXPORT_RE.match(segment):
        return True
    tokens = _shlex_tokens(segment)
    if tokens is None or not tokens:
        return False
    if _echo_allowed(tokens):
        return True
    body = _strip_trailing_redirect(tokens)
    if shapes.allows(body):
        return True
    if _git_status_allowed(body):
        return True
    if _git_commit_allowed(body):
        return True
    if _git_add_allowed(body, state, cwd):
        return True
    if _ls_allowed(body):
        return True
    if _read_verb_allowed(body, state, cwd, preceded_by_pipe):
        return True
    return False


def bash_denies_main_source_read(command, state, cwd, shapes):
    """True unless every segment of `command`, run by ppl-test-worker, is one
    of the small set of shapes this role is derived to need. `shapes` is the
    ShapeMatcher built from the run's harness snapshot. See the module
    comment above for why this must be an allowlist rather than a blocklist,
    and for exactly which shapes are recognised.
    """
    if any(marker in command for marker in _SUBSTITUTION_MARKERS):
        return True
    for segment, preceded_by_pipe in _segments_with_pipe_flag(command):
        if not _segment_allowed(segment, state, cwd, preceded_by_pipe, shapes):
            return True
    return False


# --- R-GUARD-BASH-WRITE-BOUNDARY (whole-branch review, C1): the write
# boundary below (`is_role` / `hits_repo` / `boundary.check_path`) only ever
# ran for Edit/Write/MultiEdit/NotebookEdit, and both ppl-code-worker and
# ppl-test-worker hold Bash. `echo pwned > source/Main.java` walked straight
# past a hook that denies the identical write through Write, for a role, for
# an unroled session, from any cwd. This section extracts the target path
# out of every write-shaped Bash construct BASH_WRITE_SHAPES already knows
# how to spot (a redirect, tee, sed -i, cp, mv, dd, rm, touch, truncate, ln,
# install) and runs it through
# the same per-role decision - boundary.check_path for a role, an outright
# deny for a session with no role - whenever that target resolves inside the
# active run's repo.
#
# Unlike the structured-path branch of decide() below, landing OUTSIDE the
# repo is not itself a violation here: a role's Bash legitimately writes
# build logs and rc files outside the repo (references/gates.md's own
# recipes redirect build output to a log file, then the exit code to an rc
# file, both outside the repo), so this only ever fires for a target that
# actually resolves inside the run's repo - the same "the target path is
# the honest question, not the cwd" principle R-UNROLED-REPO-WRITE already
# applies to structured writes.
#
# Fail closed on anything this cannot pin down to a concrete path - a
# command substitution anywhere in the command, an ambiguous `sed -i`
# (GNU's optional attached suffix and BSD's mandatory separate one cannot be
# told apart lexically), a tee/cp/mv/rm/redirect missing its argument, or a
# relative path after a `cd` this cannot resolve - Task 8's own discipline
# applied to writes instead of reads. That fail-closed deny only fires when
# the command is otherwise pipeline-scoped (a ppl-* role, or a session whose
# cwd sits inside the run's repo): an unrelated session working somewhere
# else on the machine must not be denied a `sed -i` just because some other
# run happens to be active, which would be exactly the leak into other
# projects this cage exists not to cause.
# `>|` (clobber, writes even under noclobber) is tried before `>`, or its `|`
# would be read as the start of the target.
_REDIR_GLUED_RE = re.compile(r"^(?:\d*>\||\d*>>?|&>>?|>&)(.+)$")


def _positionals(args, value_flags=frozenset()):
    """The non-flag operands of `args`. A flag named in `value_flags` consumes
    the token after it, and everything after a bare `--` is an operand, dashes
    or not.
    """
    out = []
    skip = literal = False
    for tok in args:
        if skip:
            skip = False
            continue
        if literal or tok == "-" or not tok.startswith("-"):
            out.append(tok)
            continue
        if tok == "--":
            literal = True
            continue
        if tok in value_flags:
            skip = True
    return out


# git's own global options sit between "git" and the subcommand, and these
# take a value. `-C <dir>` matters most: every path operand after it is relative to
# <dir>, from whatever cwd the command ran in.
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--exec-path",
                                     "--namespace", "--super-prefix", "--config-env"})
# Subcommands that rewrite the working tree from history or from a diff with
# no path operand this could name: the whole tree is the target.
_GIT_TREE_REWRITERS = frozenset({"merge", "rebase", "cherry-pick", "pull", "revert", "am",
                                 "clean"})
_GIT_APPLY_DRY_RUN_FLAGS = frozenset({"--check", "--stat", "--numstat", "--summary"})


def _git_write_targets(args, cwd):
    """(raw, cwd, removal) triples for every working-tree path a `git <args>`
    call writes, in the shape _bash_write_targets collects - [(None, cwd,
    False)] when it writes paths this cannot name, [] when it writes none.
    `removal` marks a path git takes away: a git rm path, a git mv source.

    Round 3 (post-branch review): `git checkout origin/x -- src/test/Foo.java`,
    `git rm`, `git mv`, `git restore` and `git apply <diff>` all put tree paths
    on disk with no redirect or file verb anywhere in the command, so none of
    them had a shape here. The first four name their paths on the command
    line and get the same per-role boundary decision a redirect does. `git
    apply` and the whole-tree rewriters (`merge`, `rebase`, `pull`, ...) do
    not - their paths live inside the diff or the ref - and fail closed like
    any other target this cannot pin down, which for a role is a deny and for
    an unroled session is a deny only when its cwd sits inside the run's repo
    (the orchestrator's Stage 1 `git worktree add`, run from the main checkout
    beside the repo, is not a shape this parses at all). Read-only
    subcommands, `add`, `commit`, and `stash push`/`stash apply` (the shapes
    references/gates.md sends a role to) write no tree path and pass.
    """
    i = 0
    while i < len(args) and args[i].startswith("-"):
        tok = args[i]
        if tok in _GIT_GLOBAL_VALUE_FLAGS:
            if i + 1 >= len(args):
                return [(None, cwd, False)]
            if tok == "-C":
                cwd = _resolve_path_against_cwd(args[i + 1], cwd)
            i += 2
            continue
        i += 1
    if i >= len(args):
        return []
    sub, rest = args[i], args[i + 1:]
    if sub in ("rm", "restore"):
        files = _positionals(rest, value_flags={"-s", "--source", "--pathspec-from-file"})
        return ([(f, cwd, sub == "rm") for f in files] if files
                else [(None, cwd, False)])
    if sub == "mv":
        # like mv: the LAST positional is what gets written, and every source
        # is removed from where it was
        files = _positionals(rest)
        if len(files) < 2:
            return [(None, cwd, False)]
        return [(f, cwd, True) for f in files[:-1]] + [(files[-1], cwd, False)]
    if sub in ("checkout", "switch"):
        # `checkout <ref> -- <paths>` overwrites exactly those paths. Without
        # `--` the operand may be a ref, and switching refs rewrites the
        # whole tree - not a path this can name.
        if "--" in rest:
            files = rest[rest.index("--") + 1:]
            return [(f, cwd, False) for f in files] if files else [(None, cwd, False)]
        return [(None, cwd, False)]
    if sub == "apply":
        if any(t in _GIT_APPLY_DRY_RUN_FLAGS for t in rest):
            return []
        return [(None, cwd, False)]
    if sub in _GIT_TREE_REWRITERS:
        return [(None, cwd, False)]
    if sub == "reset" and any(t in ("--merge", "--keep") for t in rest):
        # --hard is denied outright by DENIED_COMMANDS (R-GIT-RESET-HARD) -
        # --merge and --keep also rewrite the tree, --soft and --mixed do not.
        return [(None, cwd, False)]
    return []


# A quoted or backslash-escaped span of shell text: single quotes take nothing
# as an escape, double quotes take a backslash. The two inner alternatives
# start on different characters and the run is possessive, so one pass is linear.
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\[\s\S])*+\"|\\[\s\S]")
# Stands in for a protected character: a word character, never a metacharacter
# or a digit, since a quoted digit is no fd number.
_QUOTED = "_"
_GROUP_OPENERS = "({ \t"
# A group closer on the quote mask: a `)` anywhere, since it is a
# metacharacter, a `}` only as a word of its own. The two alternatives start on
# different characters.
_GROUP_CLOSER_RE = re.compile(r"\)|(?<![^ \t])\}(?![^ \t)}><&])")
_GROUP_KIND = {")": "(", "}": "{"}


def _outside_quotes(text):
    """`text` with every character a quote or a backslash protects replaced by
    _QUOTED, so a metacharacter the shell acts on is found by its position,
    the length unchanged. None when a quote or an escape is left open."""
    masked = _QUOTED_SPAN_RE.sub(lambda m: _QUOTED * len(m.group()), text)
    if "'" in masked or '"' in masked or "\\" in masked:
        return None
    return masked


def _grouping(segment):
    """(openers, body, tail) of one raw segment, read outside quotes, or None
    when a quote is left open. The `(` and `{` that open it are taken off its
    start, so `(cd` reads as `cd`, while `"(ok)"` stays a word. The tail is
    everything from the first closer on, as (closer, the raw text after it up
    to the next closer) pairs, so `echo x) > f` is the body `echo x` and the
    tail `[(")", " > f")]`."""
    mask = _outside_quotes(segment)
    if mask is None:
        return None
    start = len(mask) - len(mask.lstrip(_GROUP_OPENERS))
    openers = [c for c in mask[:start] if c in "({"]
    closers = [m.start() for m in _GROUP_CLOSER_RE.finditer(mask, start)]
    if not closers:
        return openers, segment[start:], []
    ends = closers[1:] + [len(segment)]
    tail = [(mask[c], segment[c + 1:e]) for c, e in zip(closers, ends)]
    return openers, segment[start:closers[0]], tail


# A redirect operator core, longest first. An optional fd digit run and a dup
# target are read around it. Matched on the quote mask, so a `>` inside quotes
# is never one.
_REDIR_CORE_RE = re.compile(r"&>>|&>|>>|>\||>&|>")
_WORD_END_RE = re.compile(r"[\s;&|<>]")


def _segment_redirects(body):
    """(words text, [raw target or None]) of one raw segment body. Redirect
    operators are found outside quotes, so `echo x>f` writes f and a quoted
    `grep ">x"` does not, which shlex alone cannot tell apart once it has
    dropped the quotes. A pure fd duplication (`2>&1`, `>&2`) names no file.
    The words text is the body with the operators and their targets removed,
    left for shlex to tokenise."""
    mask = _outside_quotes(body)
    if mask is None or ">" not in mask:
        return body, []
    words = []
    targets = []
    n = len(body)
    i = 0
    while i < n:
        m = _REDIR_CORE_RE.match(mask, i)
        # `>(` is a process substitution, not a file redirect - it runs a
        # command, named by neither check, like `$(` (see _SUBSTITUTION_MARKERS)
        if not m or (m.group() == ">" and m.end() < n and mask[m.end()] == "("):
            words.append(body[i])
            i += 1
            continue
        core = m.group()
        j = m.end()
        if core[0] != "&":
            # a pure fd digit run (`2>`, preceded by a boundary) belongs to the
            # operator, not the preceding word, so drop it off words
            k = len(words)
            while k > 0 and words[k - 1].isdigit():
                k -= 1
            if k < len(words) and (k == 0 or words[k - 1] in " \t"):
                del words[k:]
        if core in (">", ">>", ">|") or core in ("&>", "&>>"):
            # an fd dup written `>&` is the one case that names no file
            target, j = _redirect_operand(body, mask, j)
            targets.append(target)
        else:
            # core == ">&": a digit run after it is a dup, a `-` after it
            # closes the fd, and a word is a file
            k = j
            while k < n and mask[k].isdigit():
                k += 1
            if k < n and mask[k] == "-":
                k += 1
            if k > j and (k >= n or _WORD_END_RE.match(mask[k])):
                j = k
            else:
                target, j = _redirect_operand(body, mask, j)
                targets.append(target)
        i = j
    return "".join(words), targets


def _redirect_operand(body, mask, j):
    """(the word at mask[j:] as a shell-unquoted target, or None, next index).
    Leading spaces are skipped, the word ends at the next unquoted separator."""
    n = len(body)
    while j < n and mask[j] in " \t":
        j += 1
    start = j
    while j < n and not _WORD_END_RE.match(mask[j]):
        j += 1
    if j == start:
        return None, j
    piece = _shlex_tokens(body[start:j])
    return (piece[0] if piece else None), j


def _cd_target(args, cwd):
    """Where `cd`/`pushd` with `args` lands, or None when it cannot be pinned
    down: no operand or more than one, `-` (the previous directory) or a
    pushd stack rotation (`+N`, `-N`)."""
    if len(args) != 1 or args[0] == "-" or args[0][:1] in ("+", "-"):
        return None
    return _resolve_path_against_cwd(args[0], cwd)


# env's own options. -C/--chdir moves the command env runs, -S hands env a
# command line of its own, which is not parsed, like `sh -c '...'`.
_ENV_FLAGS = frozenset({"-i", "--ignore-environment", "-0", "--null", "-v", "--debug", "-"})
_ENV_UNSET_FLAGS = frozenset({"-u", "--unset"})
_ENV_CHDIR_FLAGS = frozenset({"-C", "--chdir"})


def _env_command(tokens, i, cwd):
    """(index of the command `env` runs, the cwd it runs in), reading env's own
    options from tokens[i:]. The index is len(tokens) for an -S command line,
    which is not parsed, and None when env's own options cannot be read. An
    index, not a slice, so nested wrappers stay linear."""
    run_cwd = cwd
    while i < len(tokens):
        tok = tokens[i]
        if tok in _ENV_CHDIR_FLAGS or tok in _ENV_UNSET_FLAGS:
            if i + 1 >= len(tokens):
                return None, cwd
            if tok in _ENV_CHDIR_FLAGS:
                run_cwd = _resolve_path_against_cwd(tokens[i + 1], run_cwd)
            i += 2
            continue
        if tok.startswith("--chdir=") or (tok.startswith("-C") and len(tok) > 2):
            value = tok[len("--chdir="):] if tok.startswith("--chdir=") else tok[2:]
            run_cwd = _resolve_path_against_cwd(value, run_cwd)
            i += 1
            continue
        if tok in ("-S", "--split-string") or tok.startswith(("-S", "--split-string=")):
            return len(tokens), run_cwd
        if tok in _ENV_FLAGS or tok.startswith("--unset=") or _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        if tok == "--":
            return i + 1, run_cwd
        if tok.startswith("-"):
            return None, cwd
        break
    return i, run_cwd

# Wrapper verbs that run the command after them, with the options of their
# own that are skipped. `command`, `builtin` and `time` run a builtin in the
# current shell, so a cd behind them still moves it. `exec`, `nohup` and `nice`
# start a new process, and a cd there moves nothing the shell keeps.
_SHELL_WRAPPERS = frozenset({"command", "builtin", "time"})
_WRAPPER_FLAGS = {"command": frozenset({"-p"}), "builtin": frozenset(),
                  "time": frozenset({"-p"}), "exec": frozenset({"-c", "-l"}),
                  "nohup": frozenset(), "nice": frozenset()}
_NICE_ADJUSTMENT_RE = re.compile(r"-n-?\d+|-\d+|--adjustment=-?\d+")


def _verb_name(tok):
    """The command a word names: `/bin/cp` and `/usr/bin/nohup` run cp and
    nohup."""
    return os.path.basename(tok) if "/" in tok else tok


def _wrapped_command(verb, tokens, i):
    """Index of the command wrapper `verb` runs, reading its own options from
    tokens[i:], len(tokens) when it runs none (`command -v`), or None when its
    options cannot be read."""
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            return i + 1
        if not tok.startswith("-") or tok == "-":
            break
        if tok in _WRAPPER_FLAGS[verb]:
            i += 1
        elif verb == "command" and tok in ("-v", "-V"):
            return len(tokens)
        elif (verb, tok) in (("exec", "-a"), ("nice", "-n"), ("nice", "--adjustment")):
            if i + 1 >= len(tokens):
                return None
            i += 2
        elif verb == "nice" and _NICE_ADJUSTMENT_RE.fullmatch(tok):
            i += 1
        else:
            return None
    return i

def _placed_in_directory(files, cwd, slash_is_contents):
    """(dest/<name>, cwd, True) for each source a copy or a move puts inside
    the destination files[-1] when that names a directory on disk. A recursive
    copy merges into that path, so one that holds runs/ (cp -r x/.ppl ~,
    cp -r x/runs ~/.ppl) takes runs/ over like a removal would. A source named
    by its contents (rsync src/, cp -r src/.) places no name of its own."""
    dest = _resolve_path_against_cwd(files[-1], cwd)
    if not dest or not os.path.isdir(dest):
        return []
    placed = []
    for src in files[:-1]:
        if slash_is_contents and src.endswith("/"):
            continue
        name = os.path.basename(src.rstrip("/"))
        if name not in ("", ".", ".."):
            placed.append((os.path.join(dest, name), cwd, True))
    return placed


def _segment_write_targets(tokens, segment, cwd, dirstack, targets):
    """Append the write targets of one segment's `tokens` to `targets`, and
    return the shell's cwd after it: `cd` and `pushd` move it, `popd` pops
    `dirstack`. Wrapper verbs (`env`, `command`, `builtin`, `exec`, `nohup`,
    `nice`, `time`) are taken off, nested ones too, before the verb is read.
    `env -C` moves only the command env runs: a redirect on the segment is the
    shell's own and stays on the shell's cwd, the one the segment starts in,
    a cd's own redirect included. Redirects are split off the raw body before
    the tokens reach here, see _bash_write_targets."""
    i = 0
    while i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i]):
        i += 1
    arg_cwd = cwd
    in_shell = True
    while i < len(tokens):
        wrapper = _verb_name(tokens[i])
        if wrapper != "env" and wrapper not in _WRAPPER_FLAGS:
            break
        # a wrapper spelled by its path is a program, never the shell's builtin
        if wrapper not in _SHELL_WRAPPERS or wrapper != tokens[i]:
            in_shell = False
        if wrapper == "env":
            i, arg_cwd = _env_command(tokens, i + 1, arg_cwd)
        else:
            i = _wrapped_command(wrapper, tokens, i + 1)
        if i is None:
            if BASH_WRITE_SHAPES.search(segment):
                targets.append((None, cwd, False))
            i = len(tokens)
    verb = _verb_name(tokens[i]) if i < len(tokens) else ""
    if i < len(tokens) and verb != tokens[i]:
        in_shell = False
    args = tokens[i + 1:]

    if verb in ("cd", "pushd", "popd") and in_shell:
        if verb == "popd":
            # on an empty stack popd fails and the shell stays where it is
            if args:
                return None
            return dirstack.pop() if dirstack else cwd
        if verb == "pushd":
            dirstack.append(cwd)
        return _cd_target(args, cwd)

    if verb == "sed" and any(t == "-i" or t.startswith("-i") for t in args):
        targets.append((None, arg_cwd, False))

    if verb == "tee":
        files = [t for t in args if not t.startswith("-")]
        if files:
            targets.extend((f, arg_cwd, False) for f in files)
        else:
            targets.append((None, arg_cwd, False))

    if verb == "dd":
        for t in args:
            if t.startswith("of="):
                val = t[len("of="):]
                targets.append((val if val else None, arg_cwd, False))

    if verb in ("cp", "mv", "install", "ln"):
        # `ln [-s] [-f] target linkname` writes linkname, not target - the
        # LAST positional argument, exactly like cp/mv/install's
        # destination. Denying the source/target argument instead would
        # be a false deny that looks like a pass, so this is one rule for
        # all four rather than a special case for ln.
        files = [t for t in args if not t.startswith("-")]
        if len(files) >= 2:
            targets.append((files[-1], arg_cwd, False))
            if verb != "ln":
                targets.extend(_placed_in_directory(files, arg_cwd, False))
            if verb == "mv":
                # a moved source is removed from where it was
                targets.extend((f, arg_cwd, True) for f in files[:-1])
        else:
            targets.append((None, arg_cwd, False))

    if verb in ("rm", "touch"):
        files = [t for t in args if not t.startswith("-")]
        if files:
            targets.extend((f, arg_cwd, verb == "rm") for f in files)
        else:
            targets.append((None, arg_cwd, False))

    if verb == "truncate":
        files = []
        skip_next = False
        for t in args:
            if skip_next:
                skip_next = False
                continue
            if t in ("-s", "--size"):
                skip_next = True
                continue
            if t.startswith("-"):
                continue
            files.append(t)
        if files:
            targets.extend((f, arg_cwd, False) for f in files)
        else:
            targets.append((None, arg_cwd, False))

    if verb == "rsync":
        # like cp/mv/install: the LAST positional is the destination, and a
        # --delete removes what the destination holds beyond the source
        files = [t for t in args if not t.startswith("-")]
        if len(files) >= 2:
            deletes = any(t == "--del" or t.startswith("--delete") for t in args)
            targets.append((files[-1], arg_cwd, deletes))
            targets.extend(_placed_in_directory(files, arg_cwd, True))
        else:
            targets.append((None, arg_cwd, False))

    if verb == "patch":
        # the paths a patch writes come from the diff, not the command
        # line - nothing lexical can name them, so this fails closed.
        targets.append((None, arg_cwd, False))

    if verb == "git":
        targets.extend(_git_write_targets(args, arg_cwd))
    return cwd


# A bare `&` outside quotes runs the list before it in the background and ends
# it like a semicolon. The `&` of `&&`, `&>`, `>&`, `<&` and `|&` is none.
_BARE_AMP_RE = re.compile(r"(?<![<>&|])&(?![>&])")


def _write_segments(command):
    """(segment, starts a list, backgrounded) for every segment the write walk
    reads: those of _segments, each also cut at a bare `&`. A list starts after
    `;`, a newline or a bare `&`, and a bare `&` runs the list before it in a
    subshell, so a cd in that list does not move the shell."""
    starts = True
    for part in _SEGMENT_SEPARATOR_RE.split(command):
        if part in (";", "\n"):
            starts = True
            continue
        if part in ("&&", "||", "|"):
            starts = False
            continue
        mask = _outside_quotes(part)
        cuts = [m.start() for m in _BARE_AMP_RE.finditer(mask)] if mask else []
        begin = 0
        for end in cuts + [len(part)]:
            text = part[begin:end].strip()
            if text:
                background = end < len(part)
                yield text, starts, background
                starts = background
            begin = end + 1


def _close_groups(tail, cwd, list_cwd, base_cwd, groups, open_cwds, targets):
    """Append the redirect targets of a segment's tail (see _grouping) to
    `targets` and return the cwd and the list's start cwd after it. Each
    closer pops its group off `groups`, a list of (kind, cwd it opened in,
    the start cwd of the list it opened in): a redirect after the closer
    opens in that cwd, a `)` gives the cwd back and a `}` does not. A tail
    that also holds a word that is neither a closer nor a redirect (`< in`, a
    comment) still closes its groups, since what follows a closer
    does not change where the group ends, but each of its redirects is
    resolved at the cwd, at base_cwd and at every cwd a group is still open
    in. A closer with no open group or of the other kind fails closed the
    same way, and then no group is closed and the cwd stays.
    `open_cwds` counts the open groups per cwd, so that set is not rebuilt
    from the whole stack."""
    readable = len(tail) <= len(groups)
    plain = True
    found = []
    for depth, (closer, text) in enumerate(tail, 1):
        if readable and groups[-depth][0] != _GROUP_KIND[closer]:
            readable = False
        if not text or text.isspace():
            found.append(())
            continue
        words, redirects = _segment_redirects(text)
        if words and not words.isspace():
            plain = False
        found.append(redirects)
    wide = not (readable and plain)
    if wide and any(found):
        where = list(dict.fromkeys([cwd, base_cwd] + list(open_cwds)))
        targets.extend((target, at, False) for redirects in found
                       for target in redirects for at in where)
    if not readable:
        return cwd, list_cwd
    for redirects in found:
        kind, opened_in, list_cwd = groups.pop()
        if open_cwds[opened_in] > 1:
            open_cwds[opened_in] -= 1
        else:
            del open_cwds[opened_in]
        if not wide:
            targets.extend((target, opened_in, False) for target in redirects)
        if kind == "(":
            cwd = opened_in
    return cwd, list_cwd


def _bash_write_targets(command, base_cwd):
    """(raw, cwd, removal) for every path a write-shaped Bash construct in
    `command` targets: the cwd in effect at that point, and whether the
    construct removes the path (rm, a mv or git mv source, git rm, the
    destination of rsync --delete). `cd`, `pushd` and `popd` are tracked
    across `;`/`&&`/`||`/newline-separated segments, through a `(` subshell,
    which gives the cwd back at its `)`, and through a `{` group, which does
    not. A redirect after a group's `)` or `}` opens in the cwd the group
    opened in. Grouping is read off the raw text outside quotes, see _grouping
    and _close_groups. A raw target of None means a write-shaped construct was
    found but its target could not be identified at all (see the module
    comment above) - the caller must treat that the same as a target that
    fails to resolve: deny, not skip. A `sh -c` or `bash -c` string is one
    opaque word: nothing inside it is read.
    """
    if "$(" in command or "`" in command:
        if BASH_WRITE_SHAPES.search(command):
            return [(None, base_cwd, False)]
        return []
    targets = []
    cwd = list_cwd = base_cwd
    groups = []
    open_cwds = {}
    dirstack = []
    for segment, starts, background in _write_segments(command):
        if starts:
            list_cwd = cwd
        grouped = _grouping(segment)
        if grouped:
            words_text, redirects = _segment_redirects(grouped[1])
            tokens = _shlex_tokens(words_text)
        else:
            tokens = None
        if tokens is None:
            if BASH_WRITE_SHAPES.search(segment):
                targets.append((None, cwd, False))
            if background:
                cwd = list_cwd
            continue
        openers, _, tail = grouped
        for kind in openers:
            groups.append((kind, cwd, list_cwd))
            open_cwds[cwd] = open_cwds.get(cwd, 0) + 1
            list_cwd = cwd
        # a cd's own redirect is the shell's, opened in the pre-cd cwd
        targets.extend((target, cwd, False) for target in redirects)
        if tokens:
            cwd = _segment_write_targets(tokens, segment, cwd, dirstack, targets)
        if tail:
            cwd, list_cwd = _close_groups(tail, cwd, list_cwd, base_cwd, groups,
                                          open_cwds, targets)
        if background:
            cwd = list_cwd
    return targets

def _lands_in_runs(path, runs_root, control_inbox=True, removal=True):
    """True when `path` resolves, through symlinks, under the runs directory and,
    with `control_inbox`, is not control.json, the operator's inbox. The name is
    read off the resolved path, so a link named control.json does not carry a
    write to another file. With `removal`, a path that holds the runs directory
    (`rm -rf ~/.ppl`, `mv ~ x`) lands in it too: only a removal takes runs/
    away with its parent, a copy into ~ writes beside it."""
    real_runs = os.path.realpath(str(runs_root))
    real_path = os.path.realpath(path)

    def holds(outer, inner):
        return inner == outer or inner.startswith(outer.rstrip("/") + "/")

    if removal and holds(real_path, real_runs):
        return True
    return (holds(real_runs, real_path)
            and not (control_inbox and os.path.basename(real_path) == "control.json"))


def bash_write_lands_in_runs(command, cwd, control_inbox=True):
    """True when a write-shaped construct in `command` has a target that resolves
    under the runs directory, whatever the spelling: absolute, `~`, after a `cd`,
    relative to the session cwd, or through a symlinked alias. It needs no run,
    so it holds for every session [R-RUNS-BASH-WRITE]. control.json is spared
    only with `control_inbox`, which the caller passes for the unroled operator
    session alone: a role that rewrites it can lift the operator's pause. A
    target this cannot resolve (a variable, a substitution) is not judged here,
    and bash_write_boundary_deny fails closed on it for a pipeline-scoped session.
    BASH_WRITE_SHAPES is not run over the whole command first: its git alternative
    rescans the line from every `git`, which is quadratic, and this check runs on
    every Bash call. A substitution leaves no target to resolve, so it returns at once.
    A target repeated at the same cwd is resolved once, since each resolution walks
    the file system."""
    if not isinstance(command, str) or "$(" in command or "`" in command:
        return False
    if not isinstance(cwd, str):
        cwd = None
    runs_root = str(paths.runs_dir())
    for raw, seg_cwd, removal in dict.fromkeys(_bash_write_targets(command, cwd)):
        resolved = _resolve_path_against_cwd(raw, seg_cwd) if raw else None
        if resolved and _lands_in_runs(resolved, runs_root, control_inbox, removal):
            return True
    return False


def structured_write_lands_in_runs(path, cwd):
    """The structured-tool half: `path` resolved against the session cwd first,
    the raw path when it cannot be resolved [R-RUNS-WRITE-PROTECTION]."""
    resolved = _resolve_path_against_cwd(path, cwd) or path
    return _lands_in_runs(resolved, str(paths.runs_dir()))


def bash_write_boundary_deny(command, payload, agent, is_role, task_id, state):
    """The Bash equivalent of the structured Edit/Write/MultiEdit/
    NotebookEdit write-boundary checks below (R-GUARD-BASH-WRITE-BOUNDARY).
    Returns a deny dict or None.
    """
    targets = _bash_write_targets(command, payload.get("cwd"))
    if not targets:
        return None
    pipeline_scoped = is_role or in_scope(payload, state)
    bound = None
    if is_role:
        bound = store.read_json(paths.run_dir(task_id) / "boundary.json",
                                {"write": [], "forbid": []})
    for raw, seg_cwd, _removal in targets:
        resolved = _resolve_path_against_cwd(raw, seg_cwd) if raw else None
        if resolved is None:
            if pipeline_scoped:
                return deny("BOUNDARY_VIOLATION",
                            "a write-shaped Bash command's target could not be "
                            "resolved, denied rather than allowed: %s" % command)
            continue
        if not targets_repo(resolved, state):
            continue
        if is_role:
            try:
                boundary.check_path(agent, relative_to_repo(resolved, state), bound)
            except PplError as err:
                return deny(err.code, err.message)
        else:
            return deny("BOUNDARY_VIOLATION",
                        "%s may not write repo code via Bash: %s is inside run "
                        "%s's repo, which only ppl-code-worker and ppl-test-worker "
                        "write, inside their boundary"
                        % (agent or "a session with no role", resolved, task_id))
    return None


def deny(code, reason):
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "%s %s" % (code, reason)}}


def target_path(tool_input):
    for key in ("file_path", "path", "notebook_path"):
        if tool_input.get(key):
            return str(tool_input[key])
    return None


def relative_to_repo(path, state):
    """Path relative to the run's repo, resolved through symlinks on both sides.

    macOS resolves /tmp to /private/tmp (and similar aliasing exists elsewhere), so
    comparing raw strings can mismatch a genuine in-repo path against the repo path
    the run recorded. Both sides go through realpath so the slice lines up whichever
    form either one arrived in.
    """
    repo = (state or {}).get("repo")
    if not repo:
        return path.lstrip("/")
    real_repo = os.path.realpath(str(repo)).rstrip("/")
    real_path = os.path.realpath(path)
    if real_path == real_repo:
        return ""
    prefix = real_repo + "/"
    if real_path.startswith(prefix):
        return real_path[len(prefix):]
    return path.lstrip("/")


def active_state():
    """The pipeline run this machine is currently executing, if any.

    Returns (task_id, state). task_id is None only when there is no active run at
    all. state is None both when there is no active run AND when there is one but
    its state.json could not be read or parsed into a usable record - the caller
    must tell those two apart, because "no active run" permits everything and "an
    active run whose state we cannot trust" must not.

    run.load raises PplError when state.json is simply missing, but a state.json
    that exists and is corrupt (a half-written file from a crashed process) makes
    the underlying json.load raise json.JSONDecodeError, and a permission problem
    raises OSError; neither is a PplError. This hook must never raise regardless of
    which of those it hits.
    """
    task_id = run.active()
    if not task_id:
        return None, None
    try:
        state = run.load(task_id)
    except (PplError, OSError, ValueError):
        return task_id, None
    if not isinstance(state, dict):
        return task_id, None
    return task_id, state


def _inside(path, repo):
    real_repo = os.path.realpath(str(repo)).rstrip("/")
    real_path = os.path.realpath(path)
    return real_path == real_repo or real_path.startswith(real_repo + "/")


def targets_repo(path, state):
    """True when the write target lands inside the active run's repo.

    This, not the session cwd, is what scopes the write-boundary check. A
    session's cwd is fixed when it launches and the documented workflow creates
    the worktree beside the session rather than under it, so scoping writes by
    cwd left the boundary inert in exactly the arrangement the skill prescribes.
    The target path is also the honest question: what makes a write dangerous is
    where it lands, not where the caller happened to be standing.
    """
    repo = (state or {}).get("repo")
    if not repo or not path:
        return False
    return _inside(path, repo)


def in_scope(payload, state):
    """True when this tool call's session sits inside the active run's worktree.

    ACTIVE is global state, so without a scope check a paused run would deny
    every tool call in every unrelated Claude session on the machine. cwd is no
    longer the only way into scope (see decide), but it is still the way the
    orchestrator's own session - which carries no agent_type and may call a
    non-write tool - is recognised. Both sides go through realpath for the same
    symlink-aliasing reason as relative_to_repo.
    """
    if not state or not state.get("repo"):
        return False
    cwd = payload.get("cwd") or ""
    if not cwd:
        return False
    return _inside(cwd, state["repo"])


# A dispatch prompt line in patch syntax. The test worker never needs one.
DIFF_SYNTAX_RE = re.compile(r"^(?:diff --git |@@ -\d+(?:,\d+)? \+\d+|(?:\+\+\+|---) [ab]/)")
# An added line counts as implementation text from this length and alphanumeric
# content on, after whitespace is collapsed: shorter lines (a brace, `return x`)
# match ordinary prose by accident.
LEAK_LINE_MIN_CHARS = 24
LEAK_LINE_MIN_ALNUM = 12
# One shared line can be a coincidence or a quoted name, two cannot.
LEAK_LINES_TO_DENY = 2
LEAK_GIT_TIMEOUT_SECONDS = 2.0
_LEAK_EDGE = " \t`>*-+"


def _leak_key(line):
    """A line as the leak check compares it: whitespace collapsed and the
    markdown a prompt wraps code in (bullets, quotes, backticks) stripped."""
    return " ".join(line.split()).strip(_LEAK_EDGE)


def _significant(key):
    return (len(key) >= LEAK_LINE_MIN_CHARS
            and sum(ch.isalnum() for ch in key) >= LEAK_LINE_MIN_ALNUM)


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def dispatches_test_worker(tool, tool_input):
    return tool in AGENT_TOOLS and tool_input.get("subagent_type") == "ppl-test-worker"


def dispatch_leak_deny(prompt, task_id, state):
    """Deny a ppl-test-worker dispatch whose prompt carries the implementation.

    R-TEST-WORKER-DISPATCH-LEAK: the test worker writes tests from behaviour,
    and its own reads of main sources are already denied, so the dispatch
    prompt was the one channel left. Two detectors: patch syntax, and lines the
    implementation added since base_sha (committed or not, test sources
    excluded) found verbatim in the prompt. Lines the brief itself carries are
    the agreed interfaces and stay allowed. It fails closed: a dispatch that
    cannot be checked is not started.
    """
    if not isinstance(prompt, str) or not prompt:
        return None
    lines = prompt.splitlines()
    for line in lines:
        if DIFF_SYNTAX_RE.match(line.strip()):
            return deny("BOUNDARY_VIOLATION",
                        "ppl-test-worker dispatch prompt carries a diff (%r). Pass acceptance "
                        "criteria, behavior and the brief's interfaces only." % line.strip()[:40])
    try:
        added = gitfacts.added_lines(state.get("repo"), state.get("base_sha"),
                                     timeout=LEAK_GIT_TIMEOUT_SECONDS)
    except PplError as err:
        return deny("BOUNDARY_VIOLATION",
                    "ppl-test-worker dispatch could not be checked for implementation "
                    "content: %s" % err.message)
    brief = store.read_json(paths.run_dir(task_id) / "brief.json", {})
    agreed = {_leak_key(line) for text in _strings(brief) for line in text.splitlines()}
    in_prompt = {_leak_key(line) for line in lines}
    hits = {}
    for rel, added_here in sorted(added.items()):
        if boundary.path_is_test_scoped(rel):
            continue
        for line in added_here:
            key = _leak_key(line)
            if _significant(key) and key in in_prompt and key not in agreed:
                hits.setdefault(key, rel)
    if len(hits) < LEAK_LINES_TO_DENY:
        return None
    first, rel = next(iter(hits.items()))
    return deny("BOUNDARY_VIOLATION",
                "ppl-test-worker dispatch prompt carries the implementation: %d added "
                "line(s), first from %s (%r). Pass acceptance criteria, behavior and the "
                "brief's interfaces only." % (len(hits), rel, first[:40]))


def _decide(payload):
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    agent = payload.get("agent_type")
    active = _active_task_id()

    denied = denied_tools(active)
    if isinstance(tool, str) and tool in denied:
        return deny("BOUNDARY_VIOLATION", denied[tool])

    runs_root = str(paths.runs_dir())

    if tool == "Bash":
        command = tool_input.get("command", "")
        if (isinstance(agent, str) and agent in boundary.ROLE_RULES
                and isinstance(command, str) and len(command) > ROLE_COMMAND_LENGTH_LIMIT):
            return deny("BOUNDARY_VIOLATION",
                        "a role's Bash command may not exceed %d characters, write a script "
                        "inside your write boundary instead" % ROLE_COMMAND_LENGTH_LIMIT)
        for pattern, reason in DENIED_COMMANDS:
            if pattern.search(command):
                return deny("BOUNDARY_VIOLATION", reason)
        reason = forge_bash_denial(command, active)
        if reason:
            return deny("BOUNDARY_VIOLATION", reason)
        # The literal spelling is the cheap first pass, the resolved target the
        # real one, so `~/.ppl/runs/...` is the same path as its absolute form.
        is_role_call = isinstance(agent, str) and agent in boundary.ROLE_RULES
        if ((runs_root in command and BASH_WRITE_SHAPES.search(command))
                or bash_write_lands_in_runs(command, payload.get("cwd"),
                                            control_inbox=not is_role_call)):
            return deny("BOUNDARY_VIOLATION",
                        "run state is written only by ppl, use the ppl CLI")
        # R-GUARD-PPL-CLI-DENIED: every role in boundary.ROLE_RULES, global, no run required -
        # a worker driving its own ppl CLI is not a boundary edge case, it is
        # the whole cage from the inside.
        if agent in boundary.ROLE_RULES and _ppl_cli_is_state_changing(command):
            return deny("BOUNDARY_VIOLATION",
                        "%s may not run a state-changing ppl command, the orchestrator "
                        "drives ppl; read-only lookups (ppl show --task <id>, ppl findings "
                        "list, ...) are fine" % agent)

    path = target_path(tool_input) if tool in WRITE_TOOLS else None
    if path and structured_write_lands_in_runs(path, payload.get("cwd")):
        return deny("BOUNDARY_VIOLATION",
                    "run state is written only by ppl, use the ppl CLI")

    task_id, state = active_state()
    if state is None:
        # state.json for the active run is missing, corrupt, or malformed. We
        # cannot scope by cwd without a readable repo path, but a ppl-* agent_type
        # is pipeline-internal and never appears in an unrelated session, so a
        # tool call carrying one is denied outright rather than silently falling
        # through every scoped check because state could not be read.
        if task_id and agent in boundary.ROLE_RULES:
            return deny("OWNER_DECISION",
                        "run %s state is unreadable, cannot verify write boundary "
                        "or pause status. Repair or restore state.json to continue."
                        % task_id)
        if task_id and dispatches_test_worker(tool, tool_input):
            return deny("OWNER_DECISION",
                        "run %s state is unreadable, cannot check the ppl-test-worker "
                        "dispatch for implementation content. Repair or restore "
                        "state.json to continue." % task_id)
        return None

    is_role = agent in boundary.ROLE_RULES

    # R-TEST-WORKER-DISPATCH-LEAK. ppl-test-worker is a pipeline role name, so a
    # dispatch of it is this run's business wherever the dispatching session sits,
    # and the check runs before any cwd-based scoping.
    if dispatches_test_worker(tool, tool_input):
        try:
            leak = dispatch_leak_deny(tool_input.get("prompt"), task_id, state)
        except Exception as err:
            # main() allows a session with no role on an internal error, which
            # here would let an unchecked dispatch through. This check fails
            # closed instead.
            return deny("BOUNDARY_VIOLATION",
                        "ppl-test-worker dispatch could not be checked for implementation "
                        "content: %s: %s" % (type(err).__name__, err))
        if leak:
            return leak

    if tool == "Bash":
        # R-GUARD-BASH-WRITE-BOUNDARY (whole-branch review, C1): a
        # write-shaped Bash construct (redirect, tee, sed -i, cp, mv, dd, rm)
        # targeting a path inside the active run's repo gets the same
        # per-role decision a structured Write does, from any cwd, for a
        # role or for an unroled session. See the function's own docstring
        # for exactly what this does and does not cover.
        bash_command = tool_input.get("command", "")
        bash_result = bash_write_boundary_deny(bash_command, payload, agent,
                                               is_role, task_id, state)
        if bash_result:
            return bash_result

    hits_repo = targets_repo(path, state)
    # Three independent ways into scope, because cwd alone was none of them in
    # practice: a ppl-* agent_type is pipeline-internal wherever its session
    # sits, a write aimed into the run's repo is the run's business whoever
    # launched it, and cwd still catches the orchestrator's own session doing
    # something that has no target path at all (a Jira write, a Bash call).
    scoped = is_role or hits_repo or in_scope(payload, state)
    # DESIGN 5.4: an isolated run (the default - see `ppl init --isolated`) is
    # one whose guard hook is assumed to be registered only in its own launch
    # directory, so it never fires in an unrelated session at all. Under that
    # assumption, the unroled main session - the orchestrator, which carries no
    # agent_type - can safely be scoped by "this run is active" rather than by
    # cwd for the Jira gate and the operator pause: the documented workflow
    # creates the worktree beside the orchestrator's session rather than under
    # it, so cwd never lined up for exactly that session. This does not widen
    # the write-boundary tail below, which stays gated on `scoped` alone - an
    # unroled session's ordinary file write outside any run's repo is still
    # untouched. The accepted residual: if the operator's actual hook
    # registration is broader than an isolated launch despite the flag, both
    # gates reach their other projects' own unroled sessions too, for as long
    # as this run is active.
    halt_scoped = scoped or (agent is None and bool(state.get("isolated", True)))
    if not halt_scoped:
        return None

    if is_tracker_write_tool(tool, tracker_from_snapshot(task_id)):
        if state.get("stage") != "merge_watch":
            return deny("OWNER_DECISION",
                        "Jira is written only after the merge is observed, current stage is %s"
                        % state.get("stage"))
        mr = state.get("mr")
        if not (isinstance(mr, dict) and mr.get("merged_sha")):
            return deny("OWNER_DECISION",
                        "Jira is written only after the merge is observed: the run is in "
                        "merge_watch but no merge is recorded, run `ppl merge observed --sha "
                        "<merged sha>` first")

    record = control.read(task_id)
    if record and record.get("invalid"):
        # control.read's own invalid marker carries command=None, so testing only
        # `record["command"] in HALTING` would miss it and permit every tool call
        # against a run whose halt state cannot be trusted. Fail closed instead,
        # matching control.enforce's own treatment of an unreadable control file.
        return deny("OWNER_DECISION", record["reason"])
    if record and record["command"] in control.HALTING:
        return deny("OWNER_DECISION",
                    "run %s is %sd by the operator" % (task_id, record["command"]))

    if not scoped:
        return None

    # R-TEST-WORKER-READ-TOOLS / R-TEST-WORKER-BASH-READS: the test worker
    # writes tests from behaviour, never from the implementation (5.3/5.4).
    # Placed before the write-only checks below because these are read
    # checks, on tools (Read, Grep, Glob, Bash) the write-boundary logic
    # never looks at.
    if agent == "ppl-test-worker":
        if tool == "Bash":
            command = tool_input.get("command", "")
            if bash_denies_main_source_read(command, state, payload.get("cwd"),
                                            harness_shapes(task_id)):
                return deny("BOUNDARY_VIOLATION",
                            "ppl-test-worker may not read main sources via Bash: %s"
                            % command)
        elif tool in READ_TOOLS:
            # A relative path is the session's, so it resolves against the
            # payload cwd, never the hook process's own.
            target = _resolve_path_against_cwd(read_target_path(tool_input, payload),
                                               payload.get("cwd"))
            if not target:
                return deny("BOUNDARY_VIOLATION",
                            "ppl-test-worker's %s call names no resolvable path" % tool)
            if targets_repo(target, state):
                rel = relative_to_repo(target, state)
                if not boundary.path_is_test_scoped(rel):
                    return deny("BOUNDARY_VIOLATION",
                                "ppl-test-worker may not read %s: only test sources are "
                                "visible to it, reading the implementation is the exact "
                                "channel this cage exists to close" % target)

    if agent == "ppl-review-responder" and tool == "Bash":
        command = tool_input.get("command", "")
        if review_responder_bash_denied(command, task_id):
            return deny("BOUNDARY_VIOLATION",
                        "ppl-review-responder may run only the forge's read commands: %s"
                        % command)

    if not path:
        return None

    if is_role:
        if not hits_repo:
            # check_path alone would not close this: its test-worker branch
            # returns as soon as a path looks like a test source, so a
            # test-shaped path outside the tree would pass. A pipeline role has
            # no business writing anywhere but the run's own repo.
            return deny("BOUNDARY_VIOLATION",
                        "%s may write only inside run %s's repo, not %s"
                        % (agent, task_id, path))
        try:
            bound = store.read_json(paths.run_dir(task_id) / "boundary.json",
                                    {"write": [], "forbid": []})
            boundary.check_path(agent, relative_to_repo(path, state), bound)
        except PplError as err:
            return deny(err.code, err.message)
        return None

    if hits_repo:
        # DESIGN 5.3: the orchestrator writes state through ppl and nothing else.
        # It carries no agent_type, and neither does any other session, so the
        # discriminator is the target path: inside an active run's repo it is
        # repo code that only a worker role may touch, and outside it this
        # branch is never reached at all.
        return deny("BOUNDARY_VIOLATION",
                    "%s may not write repo code: %s is inside run %s's repo, which only "
                    "ppl-code-worker and ppl-test-worker write, inside their boundary"
                    % (agent or "a session with no role", path, task_id))
    return None


def log_denial(payload, result):
    """Append one deny event to the active run's log. Best effort: whatever goes
    wrong here, the decision already made stands [R-GUARD-DENIAL-LOGGED]."""
    try:
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        task_id = run.active()
        if not task_id:
            return
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            tool_input = {}
        target = tool_input.get("command") or target_path(tool_input)
        agent = payload.get("agent_type")
        store.append_log(task_id, {
            "event": "deny",
            "role": agent if isinstance(agent, str) else None,
            "tool": payload.get("tool_name"),
            "code": reason.split(" ", 1)[0],
            "target": str(target)[:200] if target else None})
    except Exception:
        pass


def decide(payload):
    result = _decide(payload)
    if result and isinstance(payload, dict):
        log_denial(payload, result)
    return result


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        result = decide(payload)
    except Exception as err:
        # Claude Code reads exit 0 with no output as allow. A ppl-* role is
        # refused instead, an unroled session keeps the allow so a hook defect
        # cannot lock the operator out of unrelated sessions
        # [R-GUARD-INTERNAL-ERROR-DENIES-ROLES]. The deny goes out before the
        # diagnostic, and a broken stderr is swallowed, since a nonzero exit
        # would not block the call either.
        agent = payload.get("agent_type") if isinstance(payload, dict) else None
        if isinstance(agent, str) and agent in boundary.ROLE_RULES:
            refusal = deny(
                "BOUNDARY_VIOLATION",
                "guard internal error, role call refused: %s" % type(err).__name__)
            sys.stdout.write(json.dumps(refusal))
            sys.stdout.flush()
            log_denial(payload, refusal)
        try:
            sys.stderr.write("ppl_guard internal error: %s\n" % err)
        except Exception:
            pass
        return 0
    if result:
        sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

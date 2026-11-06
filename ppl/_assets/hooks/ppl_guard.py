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

# parents[3] holds the ppl package this hook ships in. It goes first on sys.path
# for the import of ppl alone, so that copy wins over any other installed ppl,
# and comes off again before anything else is imported. Installed, parents[3]
# is site-packages, where a top-level module named like a stdlib one (an old
# backport) would shadow the stdlib for every later import and crash the hook,
# and Claude Code allows the call when a hook crashes. The modules of ppl
# resolve through ppl.__path__, not sys.path.
_PACKAGE_PARENT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PACKAGE_PARENT))
try:
    import ppl
finally:
    sys.path.remove(str(_PACKAGE_PARENT))
_PPL_FILE = getattr(ppl, "__file__", None)
if not _PPL_FILE or not Path(_PPL_FILE).resolve().is_relative_to(_PACKAGE_PARENT):
    sys.stderr.write("ppl_guard: imported ppl from %s, not the one beside this hook in %s\n"
                     % (_PPL_FILE, _PACKAGE_PARENT))

from ppl import (boundary, control, escape, escape_paths, gitfacts, lanes, paths, run, store,
                 trackers)
from ppl import forges as _forges
from ppl.errors import PplError
from ppl.trackers import jira as _jira

# Every run file the hook reads is read through this cap, so one a role made huge is refused
# unread instead of holding the hook past its timeout [R-OS-SANDBOX-FAIL-CLOSED].
_READ_LIMIT = store.HOOK_READ_LIMIT

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


def snapshot_tracker(task_id):
    """The tracker table as the run's harness snapshot holds it, otherwise unchecked,
    or None when there is no readable snapshot or what it holds is not a table."""
    snapshot = load_snapshot(task_id)
    tracker = snapshot.get("tracker") if snapshot else None
    return tracker if isinstance(tracker, dict) else None


def tracker_from_snapshot(task_id):
    """The tracker table frozen into the run's harness snapshot, or the defaults
    when the snapshot is missing, the table is malformed in any way or reading it
    fails. Never raises: a decide() that raised would be allowed by main(), and the
    defaults leave every tracker gated [R-JIRA-GATE]."""
    try:
        tracker = snapshot_tracker(task_id)
        usable = trackers.usable(tracker)
    except Exception:
        return default_tracker()
    return tracker if usable else default_tracker()


def is_tracker_write_tool(tool, tracker):
    """True for a tracker write operation under any prefix the tracker table names.
    An unusable table or entry falls back to the defaults instead of raising."""
    if not isinstance(tool, str):
        return False
    if not trackers.usable(tracker):
        tracker = _DEFAULT_TRACKER
    try:
        pattern = trackers.get(tracker.get("kind", trackers.DEFAULT_KIND)).mcp_write_regex(tracker)
    except Exception:
        pattern = _jira.mcp_write_regex(_DEFAULT_TRACKER)
    return bool(pattern.match(tool))


def is_jira_write_tool(tool):
    """True for a Jira write operation under any default Atlassian MCP server prefix."""
    return is_tracker_write_tool(tool, default_tracker())


# Before the merge is recorded every known tracker is shut, whatever tracker the run
# names: nothing in the pipeline writes one earlier, and a run on one tracker has no
# business writing another. These are each adapter's MCP write tools under that
# adapter's own defaults, by kind [R-JIRA-GATE].
_DEFAULT_MCP_WRITES = tuple(
    (kind, trackers.get(kind).mcp_write_regex(dict(trackers.get(kind).DEFAULTS, kind=kind)))
    for kind in trackers.KINDS)


def tracker_mcp_write(tool, tracker):
    """True for a write tool of the run's tracker, under the prefixes its own table
    names, or of any other known tracker under that adapter's defaults. A Jira
    table's prefixes still replace the Jira defaults rather than extend them."""
    if not isinstance(tool, str):
        return False
    try:
        if is_tracker_write_tool(tool, tracker):
            return True
        kind = (tracker.get("kind", trackers.DEFAULT_KIND) if trackers.usable(tracker)
                else _DEFAULT_TRACKER["kind"])
        return any(pattern.match(tool) for other, pattern in _DEFAULT_MCP_WRITES
                   if other != kind)
    except Exception:
        return any(pattern.match(tool) for _, pattern in _DEFAULT_MCP_WRITES)


def any_tracker_write_tool(tool, task_id):
    """True for a tracker MCP write tool no role may call: every adapter's under its
    defaults, the run's own kind included, and the run tracker's under the prefixes
    its snapshot names [R-JIRA-NO-ROLE-TOOL]. Never raises: the defaults are read
    first, tracker_from_snapshot falls back to them on any failure, and
    tracker_mcp_write falls back to every adapter's."""
    # every tracker write tool is mcp__-shaped, so other tools skip the snapshot read
    if not isinstance(tool, str) or not tool.startswith("mcp__"):
        return False
    if default_tracker_write_tool(tool):
        return True
    return tracker_mcp_write(tool, tracker_from_snapshot(task_id))


def default_tracker_write_tool(tool):
    """True for a tracker MCP write tool under an adapter's defaults, read from no file."""
    return (isinstance(tool, str)
            and any(pattern.match(tool) for _, pattern in _DEFAULT_MCP_WRITES))


# What a role is told for a tracker write through either transport.
_ROLE_TRACKER_WRITE = ("%s may not write the tracker, the orchestrator does after the merge "
                       "is observed")


# Every CLI adapter's Bash write checks under its defaults. Each check has only
# search(command), which answers a hit or None. They apply on every run whatever
# tracker it names, and to every role with or without a run [R-TRACKER-CLI-GATE]
# [R-TRACKER-CLI-NO-ROLE].
ALL_CLI_TRACKER_DENIALS = tuple(
    check for kind in trackers.KINDS if trackers.get(kind).TRANSPORT == "cli"
    for check in trackers.get(kind).write_denials(dict(trackers.get(kind).DEFAULTS, kind=kind)))


def tracker_cli_write(command):
    """Why the Bash `command` may write a known tracker through its CLI, as the write
    check reads it, or None. A check that fails reads as a write, so a defect denies
    instead of allowing."""
    if not isinstance(command, str):
        return None
    for check in ALL_CLI_TRACKER_DENIALS:
        try:
            hit = check.search(command)
            if hit:
                reason = getattr(hit, "reason", None)
                return str(reason) if reason else "a tracker write check matched"
        except Exception as err:
            return "the tracker write check failed: %s" % type(err).__name__
    return None


def _with_reading(message, why):
    """`message`, then what the write check read, so an over-deny says what to respell."""
    return "%s (%s)" % (message, why) if why else message


def tracker_gate_denial(state, why=None):
    """The OWNER_DECISION deny for a tracker write while the run has not observed its
    merge: before merge_watch, or in it before `ppl merge observed` recorded the
    merged sha. None once it has [R-JIRA-GATE] [R-TRACKER-CLI-GATE]. `why` is the
    write check's reading of a Bash command, added after the message."""
    stage = state.get("stage")
    if stage != "merge_watch":
        return deny("OWNER_DECISION", _with_reading(
            "the tracker is written only after the merge is observed, current stage is %s"
            % stage, why))
    mr = state.get("mr")
    if not (isinstance(mr, dict) and mr.get("merged_sha")):
        return deny("OWNER_DECISION", _with_reading(
            "the tracker is written only after the merge is observed: the run is in "
            "merge_watch but no merge is recorded, run `ppl merge observed --sha "
            "<merged sha>` first", why))
    return None


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

# The tool that fetches a URL. It runs outside the OS sandbox, so a role's fetch of
# GitHub is denied here [R-WEBFETCH-GITHUB-NO-ROLE].
FETCH_TOOLS = ("WebFetch",)

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
    + FETCH_TOOLS
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
# git subcommands that only read, hyphenated or spelled like a writer's prefix: a word of
# a writer below that is one of these (merge-base, not merge) is no write shape.
_GIT_READ_ONLY_WORDS = ("merge-base", "rev-parse", "rev-list", "cat-file", "ls-files", "ls-tree",
                        "ls-remote", "for-each-ref", "show-ref", "name-rev", "describe",
                        "cherry", "check-ignore", "check-attr", "diff-tree", "diff-files",
                        "diff-index", "var", "count-objects", "verify-commit", "verify-tag")
# git's hyphenated plumbing that writes the index, refs, objects or the tree, and that the
# write walk cannot place (_git_write_targets): judged as a write shape like apply.
_GIT_PLUMBING_WRITERS = frozenset({"update-index", "update-ref", "checkout-index", "read-tree",
                                   "merge-file", "write-tree", "commit-tree", "mktree",
                                   "pack-refs", "prune-packed", "unpack-objects"})
BASH_WRITE_SHAPES = re.compile(
    r">>?|\btee\b|\bsed\b[^\n]*-i\b|\bcp\b|\bmv\b|\bdd\b[^\n]*\bof=|\brm\b"
    r"|\btouch\b|\btruncate\b|\bln\b|\binstall\b|\brsync\b|\bpatch\b"
    r"|\bgit\b[^\n]*\b(?!(?:" + "|".join(re.escape(word) for word in _GIT_READ_ONLY_WORDS)
    + r")(?![\w-]))(?:rm|mv|checkout|switch|restore|apply|merge|rebase|cherry-pick"
    r"|pull|revert|clean|symbolic-ref|"
    + "|".join(re.escape(word) for word in sorted(_GIT_PLUMBING_WRITERS))
    + r"|hash-object(?=[^\n]*\s-w\b))\b"
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
# read-only allowlist and the exact same fail-closed default. Any module of the
# package counts, `python3 -m ppl.__main__` (which runs cli.main) among them,
# by any python, with -m glued, inside a cluster of short flags (-Im) or behind other
# interpreter flags. `p''pl` (or
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
    r"\bpython[0-9.]*\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*m\s*ppl(?:\.\w+)*(?=[\s;&|]|$)"
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
    as `ppl`, or as `python3 -m ppl[.<module>]` / a bare `ppl.cli` token - that is
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


# Glob characters. A segment holding one that starts with `.` or `[` can expand
# to `.` or `..` (bash before 5.2 matches `.*` against both), which climbs out
# of the directory the token names.
_GLOB_CHARS = ("*", "?", "[")


def _expansion_problem(raw_path):
    """Why the shell would open something other than what `raw_path` spells, or
    None [R-TEST-WORKER-BASH-READS]. The guard judges a read path as written, so
    a path the shell rewrites is unclassifiable. A plain glob such as
    `tests/*.py` stays allowed: all it can expand to sits where it says. shlex
    has already dropped the quotes, so a quoted brace is refused too, a false
    deny in the accepted direction."""
    if "{" in raw_path or "}" in raw_path:
        return "brace expansion"
    if "(" in raw_path or ")" in raw_path:
        return "a pattern group or a zsh glob qualifier"
    if "$" in raw_path or "`" in raw_path:
        return "parameter or command expansion"
    if raw_path.startswith("="):
        return "zsh command-path expansion"
    if raw_path.startswith("~") and raw_path != "~" and not raw_path.startswith("~/"):
        return "tilde expansion to another directory"
    segments = raw_path.split("/")
    if ".." in segments:
        return "a .. segment, which a symlink makes the kernel resolve elsewhere"
    for segment in segments:
        if segment[:1] in (".", "[") and any(char in segment for char in _GLOB_CHARS):
            return "a glob that can match . or .."
    return None


def _path_is_provably_test_scoped(raw_path, state, cwd, patterns):
    """True only when `raw_path`, resolved against `cwd`, is confirmed to
    land on a test source inside the active run's repo, by the run's test
    `patterns`. Unlike a deny-model check, ambiguity here means False, not
    True: a path the shell would rewrite (_expansion_problem), one this
    cannot resolve, or one that resolves outside the repo, is not "safe
    because it is not our business", it is simply not proven, and this is
    an allowlist.
    """
    if _expansion_problem(raw_path):
        return False
    resolved = _resolve_path_against_cwd(raw_path, cwd)
    if resolved is None:
        return False
    if not targets_repo(resolved, state):
        return False
    rel = relative_to_repo(resolved, state)
    return boundary.path_is_test_scoped(rel, patterns)


# --- Shape 1: the build/test invocations the run's harness declares in its
# [[guard.shape]] rows. ppl init freezes them into runs/<task>/harness.json,
# and the hook reads that snapshot, never the live harness, so an edit to the
# harness mid-run cannot widen what the cage allows. Flags and goals are
# accepted in whichever order they appear, since a gate recipe may put its
# goal right after argv0 or at the very end. A missing, unreadable or
# malformed snapshot yields no shapes, so every build denies. ---

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S+$")
_ENV_EXPORT_RE = re.compile(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=\S+$")
# Variables a test-worker export or a shape's env prefix may not set: IFS
# re-splits words after the guard judged them, and the other two feed grep and
# rg options from outside the command line (GREP_OPTIONS, which BSD grep
# honours, and an rg config file that can add --pre).
_REFUSED_ENV = frozenset({"IFS", "GREP_OPTIONS", "RIPGREP_CONFIG_PATH"})
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
                if tokens[i].split("=", 1)[0] in _REFUSED_ENV:
                    return False
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
        doc = store.read_json(paths.run_dir(task_id) / "harness.json", None, _READ_LIMIT)
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


def test_patterns(task_id):
    """The run's test globs: the Java defaults when runs/<task>/harness.json is
    missing or predates [tests], None when it cannot be trusted. Never raises
    [R-TEST-PATTERNS-FROM-SNAPSHOT]. Unlike load_snapshot it tells a missing
    file from an unreadable one."""
    try:
        return boundary.run_test_patterns(task_id, _READ_LIMIT)
    except Exception:
        return None


def unknown_patterns_deny(task_id):
    """A pipeline write, or a test-worker dispatch, while the run's test globs
    are unknown: neither can be judged, so the owner restores the snapshot."""
    return deny("OWNER_DECISION", boundary.unknown_patterns_reason(task_id))


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


def github_mcp_prefixes(task_id):
    """The mcp__<server>__ prefix of the GitHub MCP server the run's harness snapshot names
    in forge.mcp_server, as a list, empty when the snapshot names none or is not there.
    Never raises [R-FORGE-GH-MCP-NO-ROLE]."""
    return escape.forge_github_prefixes(forge_from_snapshot(task_id)[1])


def facts_file(argv):
    """The guard facts the session of the hook whose command line is `argv` runs under: in a
    lane, the os_sandbox.json ppl lane renders into its start directory, lanes.start_dir of
    the --task the command line names, where every registry entry records it; in no lane,
    PPL_HOME/os_sandbox.json, which ppl harness apply writes. None for a command line that
    binds no lane cleanly, whose calls lane_denial refuses. Found from argv alone, so neither
    the registry nor a run file is read for it, and one a role made huge or unreadable holds
    no edit back."""
    try:
        task = lanes.hook_task(argv)
    except Exception:
        return None
    if task is None:
        return paths.home() / escape.FACTS_FILE
    return lanes.start_dir(task) / escape.FACTS_FILE


def _names_a_lane(argv):
    """True when the hook's command line carries a --task, well formed or not: a session ppl
    lane started, whatever the registry says of it."""
    try:
        return lanes.hook_task(argv) is not None
    except Exception:
        return True


def protected_edit_denial(payload, argv=()):
    """(code, reason) when an edit tool call may not write its target, else None: while the
    guard facts of the session (facts_file) exist it writes only inside the OS sandbox's write
    scope and Claude Code's projects and plans directories, and never a protected path, a
    lane's whole start directory among them [R-OS-SANDBOX-PROTECTED-PATHS]. In a lane whose
    harness has no OS sandbox, and so no facts, it still never writes a protected path every
    session has: Claude Code's settings, ppl's code, config, routing, harnesses and facts, the
    lanes' start directories and registry, and the git, ssh and gh config. Facts that cannot
    be read put every session in scope with no write scope to check, so every edit is refused
    until they are repaired. No run file is read, nor the registry."""
    home = paths.home()
    try:
        facts = escape.load_facts(facts_file(argv))
    except ValueError as err:
        return ("OWNER_DECISION",
                "the guard's OS sandbox facts are unusable (%s), so an edit tool's target cannot "
                "be checked against the write scope: %s" % (err, escape.APPLY))
    reason = escape.edit_denial(payload, home, facts, _names_a_lane(argv))
    return ("BOUNDARY_VIOLATION", reason) if reason else None


def _github_mcp_denial(agent, tool):
    return deny("BOUNDARY_VIOLATION",
                "%s may not call %s: no role reaches a GitHub MCP server, which runs outside "
                "the OS sandbox" % (agent, tool))


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


def _git_add_allowed(tokens, state, cwd, patterns):
    if len(tokens) < 3 or tokens[0] != "git" or tokens[1] != "add":
        return False
    return all(_path_is_provably_test_scoped(t, state, cwd, patterns) for t in tokens[2:])


def _ls_allowed(tokens):
    return bool(tokens) and tokens[0] == "ls"


# --- Shape 4: reading test sources - the same verbs the earlier blocklist
# named, now used to recognise an ALLOWED shape instead of a denied one. A
# verb here with no file argument at all is filtering a pipe, not reading a
# path from disk, and is allowed on that basis - a definite classification,
# not an unclassifiable one. ---

def _options(flags="", patterns="", values="", dirs="", glued=""):
    """An option table: each argument a space-separated list of option names
    of one kind, see _READ_VERB_OPTIONS."""
    table = {}
    for kind, names in (("flag", flags), ("pattern", patterns), ("value", values),
                        ("dirs", dirs), ("glued", glued)):
        for name in names.split():
            table[name] = kind
    return table


# The test worker's read verbs and every option the guard lets them take
# [R-TEST-WORKER-BASH-READS]. sed and awk are not here: they are interpreters
# whose script can read a file (sed r, awk getline), write one (sed w) or run
# a command (GNU sed e). Nor is less, which runs LESSOPEN preprocessors and
# opens another file on +:e. An option is listed by what its value is: "flag"
# takes none, "pattern" fills the pattern slot, "dirs" is grep's -d whose
# action other than read or skip walks the tree, "value" is a number, a glob
# filter or a mode, and "glued" takes a value only as --opt=value. No option
# whose value names a file is listed (grep -f/--file/--exclude-from, rg
# -f/--file/--ignore-file): judging the file as a path still left the pattern
# slot and the tree-read check to a parser that got them wrong twice, so the
# operands are positional only. An unlisted option is refused, not guessed at:
# grep is GNU, BSD or ugrep depending on the machine, and the unlisted ones
# include options that read a file (ugrep --config, --include-from) or run a
# program (ugrep --filter, rg --pre).
_READ_VERB_OPTIONS = {
    "cat": _options(flags="-A -b -e -E -n -s -t -T -u -v -l --show-all --number-nonblank "
                          "--number --squeeze-blank --show-ends --show-tabs --show-nonprinting"),
    "head": _options(flags="-q -v -z --quiet --silent --verbose --zero-terminated",
                     values="-n -c --lines --bytes"),
    "tail": _options(flags="-f -F -r -q -v -z --retry --quiet --silent --verbose "
                           "--zero-terminated",
                     values="-n -c -b -s --lines --bytes --sleep-interval --pid "
                            "--max-unchanged-stats",
                     glued="--follow"),
    "grep": _options(flags="-a -c -E -F -G -h -H -i -I -l -L -n -o -P -q -r -R -s -v -w -x -y "
                           "--recursive --dereference-recursive --ignore-case --invert-match "
                           "--word-regexp --line-regexp --count --files-with-matches "
                           "--files-without-match --line-number --no-filename --with-filename "
                           "--only-matching --quiet --silent --no-messages --extended-regexp "
                           "--fixed-strings --basic-regexp --perl-regexp --text --null "
                           "--byte-offset --initial-tab",
                     patterns="-e --regexp",
                     values="-m -A -B -C -D --max-count --after-context --before-context "
                            "--devices --include --exclude --exclude-dir --include-dir --label",
                     dirs="-d --directories",
                     glued="--context --color --colour"),
    "rg": _options(flags="-i -s -S -v -w -x -c -l -n -N -o -q -F -P -H -I -a -z -b -p -u -U "
                         "--ignore-case --smart-case --case-sensitive --invert-match "
                         "--word-regexp --line-regexp --count --count-matches "
                         "--files-with-matches --files-without-match --line-number "
                         "--no-line-number --only-matching --quiet --fixed-strings --pcre2 "
                         "--with-filename --no-filename --text --search-zip --byte-offset "
                         "--pretty --heading --no-heading --vimgrep --json --hidden "
                         "--no-ignore --multiline --files",
                   patterns="-e --regexp",
                   values="-A -B -C -m -g -t -T -j -E -M -d -r --after-context "
                          "--before-context --context --max-count --glob --iglob --type "
                          "--type-not --threads --encoding --max-columns --max-depth "
                          "--max-filesize --replace --sort --sortr --color --colors"),
}
_READ_VERBS = frozenset(_READ_VERB_OPTIONS)
# grep and rg take a pattern before their paths, unless -e or --regexp gave it.
# -f and --file are not listed, so they are refused like any unlisted option.
_READ_PATTERN_VERBS = frozenset({"grep", "rg"})
# Round 2 review (C2, whole-branch): `rg` is recursive by default with no
# path operand at all - it does not need a flag to walk the tree, and it
# does not read stdin unless given a literal "-" argument. GNU/BSD `grep`
# only behaves the same way once `-r`, `-R` or a `-d` action other than read
# or skip is given (bundled into a short-option group like "-rn" or "-nr",
# not only standalone). Neither fact has anything to do with whether the
# command is fed by a pipe: a piped `grep -r pattern` still walks "." and
# ignores stdin. So a verb that walks the tree is a tree-read whenever it is
# present, regardless of the pipe flag. A bare `grep`/`head`/`tail` with no
# path operand and no recursion is the only shape that is actually reading
# stdin, and even that is a genuine pipe filter only when a real "|" fed it -
# not merely because the path list happens to be empty.
_GREP_RECURSIVE = frozenset({"-r", "-R", "--recursive", "--dereference-recursive"})


def _read_verb_operands(tokens):
    """(paths, whole, walks_tree, refused) for a read-verb invocation, or None
    when `tokens` is not one. `paths` are the positional operands the verb
    opens, `whole` their token indices, `walks_tree` whether it reads the tree
    from cwd without a path, and `refused` the first option the guard does not
    classify. Short options are read getopt-style: bundled (-in), with a value
    glued (-etotal) or in the next token, and `--` ends the options."""
    if not tokens or tokens[0] not in _READ_VERB_OPTIONS:
        return None
    verb = tokens[0]
    table = _READ_VERB_OPTIONS[verb]
    pattern_due = verb in _READ_PATTERN_VERBS
    recursive = verb == "rg"
    paths, whole = [], set()
    refused = None
    options = True
    i = 1
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if not options or tok == "-" or not tok.startswith("-"):
            if pattern_due:
                pattern_due = False
            else:
                paths.append(tok)
                whole.add(i - 1)
            continue
        if tok == "--":
            options = False
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            kind = table.get(name)
            if kind is None or (kind == "flag" and eq):
                refused = refused or tok
                continue
            if kind in ("flag", "glued"):
                recursive = recursive or (verb == "grep" and name in _GREP_RECURSIVE)
                continue
            if not eq:
                if i >= len(tokens):
                    continue
                value = tokens[i]
                i += 1
        else:
            kind = value = None
            for j in range(1, len(tok)):
                char = tok[j]
                if char.isdigit() and verb in ("grep", "head", "tail"):
                    continue
                kind = table.get("-" + char)
                if kind is None:
                    refused = refused or tok
                    break
                if kind == "flag":
                    recursive = recursive or (verb == "grep" and char in "rR")
                    kind = None
                    continue
                value = tok[j + 1:]
                if not value and i < len(tokens):
                    value = tokens[i]
                    i += 1
                break
            if kind is None:
                continue
        if kind == "pattern":
            pattern_due = False
        elif kind == "dirs" and value not in ("read", "skip"):
            recursive = True
    return paths, whole, recursive, refused


def _read_verb_allowed(tokens, state, cwd, preceded_by_pipe, patterns):
    operands = _read_verb_operands(tokens)
    if operands is None:
        return False
    paths, _, recursive, refused = operands
    if refused:
        return False
    if not paths:
        if recursive:
            # rg with no path, or grep -r/-R with no path operand: this
            # reads the tree from cwd, piped or not - not a pipe filter.
            return False
        return preceded_by_pipe
    return all(_path_is_provably_test_scoped(p, state, cwd, patterns) for p in paths)


def _segment_allowed(segment, state, cwd, preceded_by_pipe, shapes, patterns):
    exported = _ENV_EXPORT_RE.match(segment)
    if exported:
        return exported.group(1) not in _REFUSED_ENV
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
    if _read_segment_problem(segment, tokens, body):
        return False
    if _git_add_allowed(body, state, cwd, patterns):
        return True
    if _ls_allowed(body):
        return True
    if _read_verb_allowed(body, state, cwd, preceded_by_pipe, patterns):
        return True
    return False


# Characters the shell expands when it sees them unquoted. In a pattern, an
# option or its value their expansion is new words the guard never judged:
# from the repo root `grep -r s* tests` is `grep -r scripts src tests`.
_WORD_EXPANSION_CHARS = frozenset("{}()*?[")


def _word_masks(segment, count):
    """Each shell word of `segment` with its quoted characters masked by
    _outside_quotes, aligned with the `count` shlex tokens, or None when the
    two do not line up."""
    masked = _outside_quotes(segment)
    if masked is None:
        return None
    words = masked.split()
    return words if len(words) == count else None


def _word_expansion_problem(token, mask):
    """Why the shell would turn a word that is no path operand into other
    words, or None. Braces, groups and globs count only unquoted (`mask`), a $
    or a backtick counts even quoted, since double quotes still expand it."""
    if "$" in token or "`" in token:
        return "parameter or command expansion"
    if any(char in _WORD_EXPANSION_CHARS for char in mask):
        return "brace, glob or pattern-group expansion"
    if mask[:1] in ("~", "="):
        return "tilde or command-path expansion"
    return None


def _read_segment_problem(segment, tokens, body):
    """(word, reason) for the first word of a read-verb or `git add` segment
    the guard refuses, or None [R-TEST-WORKER-BASH-READS]. Every word after the
    verb is checked: a path operand by _expansion_problem, which keeps a plain
    glob such as tests/*.py, and any other word by _word_expansion_problem,
    since a pattern or an option value the shell expands becomes operands."""
    if body[:2] == ["git", "add"]:
        for token in body[2:]:
            problem = _expansion_problem(token)
            if problem:
                return token, "uses shell expansion the guard cannot resolve (%s)" % problem
        return None
    operands = _read_verb_operands(body)
    if operands is None:
        return None
    _, whole, _, refused = operands
    if refused:
        return refused, ("is an option the guard does not classify, which may read a file "
                         "or run a program")
    masks = _word_masks(segment, len(tokens))
    if masks is None:
        return segment, "uses quoting the guard cannot follow"
    for index in range(1, len(body)):
        token = body[index]
        problem = (_expansion_problem(token) if index in whole
                   else _word_expansion_problem(token, masks[index]))
        if problem:
            return token, "uses shell expansion the guard cannot resolve (%s)" % problem
    return None


def _denied_segment_reason(segment, command):
    tokens = _shlex_tokens(segment) or []
    found = _read_segment_problem(segment, tokens, _strip_trailing_redirect(tokens))
    if found:
        return "ppl-test-worker's Bash word %s %s: %s" % (found[0], found[1], command)
    return "ppl-test-worker may not read main sources via Bash: %s" % command


def bash_denies_main_source_read(command, state, cwd, shapes, patterns):
    """None when every segment of `command`, run by ppl-test-worker, is one
    of the small set of shapes this role is derived to need, the deny reason
    otherwise. `shapes` is the ShapeMatcher built from the run's harness
    snapshot, `patterns` the test globs it froze, `()` when they cannot be
    trusted. See the module comment above for why this must be an allowlist
    rather than a blocklist, and for exactly which shapes are recognised.
    """
    if any(marker in command for marker in _SUBSTITUTION_MARKERS):
        return "ppl-test-worker may not read main sources via Bash: %s" % command
    for segment, preceded_by_pipe in _segments_with_pipe_flag(command):
        if not _segment_allowed(segment, state, cwd, preceded_by_pipe, shapes, patterns):
            return _denied_segment_reason(segment, command)
    return None


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
# applied to writes instead of reads. That fail-closed deny fires only in a
# session bound to a run, a lane session: one in no lane gets the global
# denials alone, and the hooks reach no other session at all.
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
# take a value as the next word, as git.c's handle_options reads them (git 2.46, with
# --super-prefix from before 2.40). `-C <dir>` matters most: every path operand after it
# is relative to <dir>, from whatever cwd the command ran in.
_GIT_GLOBAL_VALUE_FLAGS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                                     "--super-prefix", "--config-env", "--attr-source",
                                     "--shallow-file"})
# The global options that take their value glued with `=` only, or none: a bare
# --exec-path prints the exec path and runs nothing.
_GIT_GLOBAL_GLUED = ("--git-dir=", "--work-tree=", "--namespace=", "--super-prefix=",
                     "--config-env=", "--attr-source=", "--exec-path=", "--list-cmds=")
_GIT_GLOBAL_FLAGS = frozenset({"-p", "--paginate", "-P", "--no-pager", "--no-replace-objects",
                               "--no-lazy-fetch", "--no-optional-locks", "--no-advice", "--bare",
                               "--literal-pathspecs", "--no-literal-pathspecs",
                               "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
                               "--exec-path", "--html-path", "--man-path", "--info-path",
                               "-h", "--help", "-v", "--version"})
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
    beside the repo, is not a shape this parses at all; in a lane every git
    worktree subcommand but list is denied outright, see lane_git_denial). Read-only
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
    # the hyphenated plumbing writes the index, refs, objects or the tree with no path this
    # could name: symbolic-ref with a target or -d, hash-object with -w, the rest always
    if (sub in _GIT_PLUMBING_WRITERS
            or (sub == "symbolic-ref" and (len(_positionals(rest)) >= 2
                                           or any(t in ("-d", "--delete") for t in rest)))
            or (sub == "hash-object" and "-w" in rest)):
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


def _segment_write_targets(tokens, segment, cwd, dirstack, targets, commands=None):
    """Append the write targets of one segment's `tokens` to `targets`, and
    return the shell's cwd after it: `cd` and `pushd` move it, `popd` pops
    `dirstack`. Wrapper verbs (`env`, `command`, `builtin`, `exec`, `nohup`,
    `nice`, `time`) are taken off, nested ones too, before the verb is read.
    `env -C` moves only the command env runs: a redirect on the segment is the
    shell's own and stays on the shell's cwd, the one the segment starts in,
    a cd's own redirect included. Redirects are split off the raw body before
    the tokens reach here, see _bash_write_targets. With `commands`, the
    segment is appended to it as (tokens, index of the command its wrappers
    run, the cwd that command runs in)."""
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
    if commands is not None:
        commands.append((tokens, i, arg_cwd))
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


def _bash_write_targets(command, base_cwd, commands=None):
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
    opaque word: nothing inside it is read. With `commands`, every segment the
    walk reads is appended to it (see _segment_write_targets), one it cannot
    split into words as (None, its raw text, its cwd), and a command holding a
    `$(` or a backtick appends nothing.
    """
    if "$(" in command or "`" in command:
        # read without here-document bodies, which are data: a commit message there names
        # no write (_command_views)
        found = _command_views(command)
        if BASH_WRITE_SHAPES.search("\n".join(found[0] + found[1]) if found else command):
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
            if commands is not None:
                commands.append((None, segment, cwd))
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
            cwd = _segment_write_targets(tokens, segment, cwd, dirstack, targets, commands)
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
    and bash_write_boundary_deny fails closed on it for a session bound to a run.
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


def _has_dotdot(path):
    return ".." in str(path).split("/")


def _dotdot_reason(tool, path):
    """A test-worker path with a `..` segment: the guard collapses `..` before
    a symlink is followed, the kernel after, so a link in a test directory
    would take the path elsewhere [R-TEST-WORKER-READ-TOOLS]
    [R-ROLE-TEST-WORKER-MAIN]."""
    return ("ppl-test-worker's %s path %s has a .. segment, and .. paths are not resolved "
            "for the test worker, name the path without it" % (tool, path))


def bash_write_boundary_deny(command, payload, agent, is_role, task_id, state, patterns):
    """The Bash equivalent of the structured Edit/Write/MultiEdit/
    NotebookEdit write-boundary checks below (R-GUARD-BASH-WRITE-BOUNDARY), for a call
    of a session bound to run `task_id`, every one of which is in that run's scope.
    `patterns` is test_patterns(task_id). Returns a deny dict or None.
    """
    targets = _bash_write_targets(command, payload.get("cwd"))
    if not targets:
        return None
    bound = None
    if is_role:
        bound = store.read_json(paths.run_dir(task_id) / "boundary.json",
                                {"write": [], "forbid": []}, _READ_LIMIT)
    for raw, seg_cwd, _removal in targets:
        if agent == "ppl-test-worker" and raw and _has_dotdot(raw):
            return deny("BOUNDARY_VIOLATION", _dotdot_reason("Bash write", raw))
        resolved = _resolve_path_against_cwd(raw, seg_cwd) if raw else None
        if resolved is None:
            return deny("BOUNDARY_VIOLATION",
                        "a write-shaped Bash command's target could not be "
                        "resolved, denied rather than allowed: %s" % command)
        if not targets_repo(resolved, state):
            continue
        if is_role:
            if patterns is None and boundary.ROLE_RULES[agent]["may_write"]:
                return unknown_patterns_deny(task_id)
            try:
                boundary.check_path(agent, relative_to_repo(resolved, state), bound, patterns)
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


def bound_state(binding):
    """The run this hook's own argv binds it to (lanes.binding), as (task_id, state).

    task_id is None only outside a lane. state is None both outside a lane AND when the
    bound run's state.json could not be read or parsed into a usable record - the caller
    must tell those two apart, because outside a lane every run-scoped check is off and a
    bound run whose state we cannot trust must not be. A binding with a problem raises
    PplError OWNER_DECISION naming it, so escape.decide, which takes this as its reader,
    refuses a flagged call in a lane it cannot confirm [R-LANE-HOOK-BINDING]. _decide
    answers the problem itself and must never call this with one: main() reads an exception
    from _decide as an internal error, which allows an unroled call, so the fail-closed
    refusal would turn into an allow.

    run.load raises PplError when state.json is simply missing, but a state.json
    that exists and is corrupt (a half-written file from a crashed process) makes
    the underlying json.load raise json.JSONDecodeError, and a permission problem
    raises OSError; neither is a PplError. None of those escapes.
    """
    if binding.problem:
        raise PplError("OWNER_DECISION", binding.problem)
    task_id = binding.task
    if not task_id:
        return None, None
    try:
        state = run.load(task_id, _READ_LIMIT)
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


def dispatch_leak_deny(prompt, task_id, state, patterns):
    """Deny a ppl-test-worker dispatch whose prompt carries the implementation.

    R-TEST-WORKER-DISPATCH-LEAK: the test worker writes tests from behaviour,
    and its own reads of main sources are already denied, so the dispatch
    prompt was the one channel left. Two detectors: patch syntax, and lines the
    implementation added since base_sha (committed or not, test sources by the
    run's `patterns` excluded) found verbatim in the prompt. Lines the brief
    itself carries are the agreed interfaces and stay allowed. It fails closed:
    a dispatch that cannot be checked is not started, and with unknown
    `patterns` no added line can be told test from implementation.
    """
    if patterns is None:
        return unknown_patterns_deny(task_id)
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
    brief = store.read_json(paths.run_dir(task_id) / "brief.json", {}, _READ_LIMIT)
    agreed = {_leak_key(line) for text in _strings(brief) for line in text.splitlines()}
    in_prompt = {_leak_key(line) for line in lines}
    hits = {}
    for rel, added_here in sorted(added.items()):
        # a file list, so a product file named like a test directory still counts
        if boundary.is_test_path(rel, patterns):
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


def stateless_lane_write_denial(tool, tool_input, path, payload, agent, task_id, entry):
    """R-UNROLED-REPO-WRITE while the bound run has no readable state.json, as between
    ppl lane and ppl init: the lane's registry entry names its worktree and the project
    repo, and they stand in for the run's repo, so that window opens no repo write. A role
    never gets here, every role call is denied while the state cannot be read. An entry
    that names neither leaves a write nowhere to be placed, so every write is denied."""
    roots = [root for root in (entry.get("worktree"), entry.get("repo"))
             if isinstance(root, str) and root]
    unplaced = deny("OWNER_DECISION",
                    "lane %s's registry entry names no worktree or repo, so a write cannot be "
                    "placed: run ppl lane resume %s" % (task_id, task_id))
    if tool == "Bash":
        command = tool_input.get("command", "")
        if not roots:
            return unplaced if _bash_write_targets(command, payload.get("cwd")) else None
        for root in roots:
            refusal = bash_write_boundary_deny(command, payload, agent, False, task_id,
                                               {"repo": root}, ())
            if refusal:
                return refusal
        return None
    if not path:
        return None
    if not roots:
        return unplaced
    if any(targets_repo(path, {"repo": root}) for root in roots):
        return deny("BOUNDARY_VIOLATION",
                    "%s may not write repo code: %s is inside lane %s's worktree or repo, which "
                    "only ppl-code-worker and ppl-test-worker write, inside their boundary"
                    % (agent or "a session with no role", path, task_id))
    return None


# What an unroled session keeps in a lane whose registry entry cannot be confirmed: it
# reads, it changes nothing [R-LANE-HOOK-BINDING].
UNCONFIRMED_LANE_TOOLS = ("Read", "Glob", "Grep")


def lane_denial(binding, agent, tool):
    """The deny for a call this hook's lane binding refuses, else None.

    A hook knows its run from the `--task` its own command line carries, which ppl lane
    renders into the lane's settings.json, never from shared state. With no --task the
    session is in no lane: a ppl-* role there is refused [R-LANE-ROLE-OUTSIDE], anything
    else meets only the global denials. A --task that is malformed, or names a lane the
    registry does not hold or a registry that cannot be read, fails closed: a role is
    refused, and any other session keeps Read, Glob and Grep [R-LANE-HOOK-BINDING]."""
    if binding.problem:
        if escape.is_role(agent) or tool not in UNCONFIRMED_LANE_TOOLS:
            return deny("OWNER_DECISION", binding.problem)
        return None
    if binding.task is None and escape.is_role(agent):
        return deny("BOUNDARY_VIOLATION",
                    "%s is denied: ppl roles run only inside a lane session; start one with "
                    "ppl lane <link>" % agent)
    return None


def _lane_files(binding, lanes_list):
    """What ppl lane writes for the lanes and no session does: PPL_HOME/lanes, where it makes
    every start directory, the registry and its lock, and each start directory a registry
    entry records, the bound lane's included, whatever the facts say [R-LANE-START-DIR-PROTECTED].
    A start directory holds the session's settings (its hooks and sandbox profile), skill
    plugin, agents, MCP config and guard facts, all under the session's cwd, where acceptEdits
    takes an edit unasked."""
    home = paths.home()
    roots = [str(home / "lanes"), str(lanes.registry_path()), str(home / lanes.LOCK_FILE)]
    for item in list(lanes_list or []) + ([binding.entry] if binding.entry else []):
        start = item.get("start_dir") if isinstance(item, dict) else None
        if isinstance(start, str) and os.path.isabs(start):
            roots.append(start)
    return roots


def _lane_write_forms(tool, tool_input, cwd):
    """(forms, real forms, removal) for every path the call writes, as escape_paths.landing
    places it: each path key of an edit tool, and each target of a write-shaped Bash
    (_bash_write_targets) it can resolve. A Bash target it cannot resolve is left to
    bash_write_boundary_deny, which denies one in a lane, and a relative edit path with no
    absolute cwd, which Claude Code does not take, is not placed."""
    placed = []
    if tool in WRITE_TOOLS:
        raws = [(tool_input.get(key), cwd, False) for key in READ_PATH_KEYS]
    elif tool == "Bash" and isinstance(tool_input.get("command"), str):
        raws = [(raw, seg_cwd, removal)
                for raw, seg_cwd, removal in _bash_write_targets(tool_input["command"], cwd)
                if raw and _resolve_path_against_cwd(raw, seg_cwd)]
    else:
        raws = []
    for raw, at, removal in raws:
        if not isinstance(raw, str) or not raw:
            continue
        forms = escape_paths.landing(raw, at if isinstance(at, str) else None)
        if forms:
            placed.append((forms[0], forms[1], removal))
    return placed


def _takes(real_forms, removal, roots):
    """The root of `roots` a removal of one of `real_forms` takes with it, else None."""
    if not removal:
        return None
    for root in roots:
        if any(escape_paths.inside(os.path.realpath(root), [form]) for form in real_forms):
            return os.path.realpath(root)
    return None


def cross_lane_denial(payload, binding, lanes_list):
    """("BOUNDARY_VIOLATION", reason) when a call writes where no session of this lane may,
    else None. Every path key of an edit tool and every target of a write-shaped Bash is
    placed through every link, in any letter case and by device and inode (escape_paths), and
    a removal counts for what it takes with it:
    - any session, in a lane or not: a lane's files, every start directory and the registry
      (_lane_files) [R-LANE-START-DIR-PROTECTED];
    - a session bound to a lane: another lane's worktree, and a project checkout a lane works
      on, outside the bound lane's own worktree [R-LANE-CROSS-WRITE]. The lanes branch from
      that checkout, and git in a worktree writes only its .git, which ppl's own git reaches.
    `lanes_list` is the registry as read for this call, None when it could not be read: a
    bound session then gets ("OWNER_DECISION", why) for a write, since another lane's work
    cannot be placed."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    placed = _lane_write_forms(tool, tool_input, payload.get("cwd"))
    if not placed:
        return None
    agent = payload.get("agent_type")
    who = agent if isinstance(agent, str) and agent else "a session with no role"
    files = _lane_files(binding, lanes_list)
    for forms, real_forms, removal in placed:
        root = escape_paths.under(forms, files) or _takes(real_forms, removal, files)
        if root:
            return ("BOUNDARY_VIOLATION",
                    "%s may not write %s: it is or lies under %s, where ppl lane keeps the "
                    "lanes' session files (each lane's settings, hooks, skill plugin, agents, MCP "
                    "config and guard facts) and the lane registry, which no session writes"
                    % (who, forms[0], root))
    entry = binding.entry
    if not (binding.task and isinstance(entry, dict)):
        return None
    if lanes_list is None:
        return ("OWNER_DECISION",
                "lane %s's registry cannot be read now, so a write cannot be placed against the "
                "other lanes' work: repair PPL_HOME/lanes.json, then run ppl lane resume %s"
                % (binding.task, binding.task))
    own = entry.get("worktree")
    own = [own] if isinstance(own, str) and os.path.isabs(own) else []
    others = [item for item in lanes_list
              if isinstance(item, dict) and item.get("task") != binding.task]
    for forms, real_forms, removal in placed:
        for form in real_forms:
            # where the write lands decides: in the lane's own worktree it is the run's own
            # boundary's to judge, a link there to elsewhere lands elsewhere
            if own and escape_paths.inside(form, own):
                continue
            for item in others:
                worktree = item.get("worktree")
                if not (isinstance(worktree, str) and os.path.isabs(worktree)):
                    continue
                if escape_paths.under([form], [worktree]) or _takes([form], removal, [worktree]):
                    return ("BOUNDARY_VIOLATION",
                            "%s may not write %s: it lies in lane %s's worktree %s, and lane %s "
                            "writes only its own" % (who, forms[0], item.get("task"), worktree,
                                                     binding.task))
            for item in [entry] + others:
                checkout = item.get("repo")
                if not (isinstance(checkout, str) and os.path.isabs(checkout)):
                    continue
                within = escape_paths.under([form], [checkout])
                root = within or _takes([form], removal, [checkout])
                if root:
                    return ("BOUNDARY_VIOLATION",
                            "%s may not write %s: it %s the project checkout %s outside lane "
                            "%s's worktree %s. The lanes branch from that checkout, and a lane "
                            "writes only its own worktree"
                            % (who, forms[0], "lies in" if within else "would take with it",
                               root, binding.task, own[0] if own else "(none recorded)"))
    return None


# A word that is an assignment, an empty one included: `PPL_TASK= ppl` runs ppl.
_ASSIGNMENT_WORD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# The package or any module of it, as python -m takes it: ppl.__main__ runs cli.main too.
_PPL_MODULE_RE = re.compile(r"ppl(?:\.\w+)*")


def _runs_ppl_module(words):
    """True when an interpreter's `words` name a ppl module for -m: a short-flag word with an
    m in it (-m, -Im, -bBm) whose rest is the module (-mppl, -Imppl.cli), or, with no rest,
    whose next word is. Every m of the cluster is tried, so a cluster python reads otherwise
    is taken as running ppl rather than not."""
    for index, word in enumerate(words):
        if not (word.startswith("-") and not word.startswith("--")):
            continue
        for at in range(1, len(word)):
            # the flags before the m are letters, as python's own short flags are
            if word[at] != "m" or not (at == 1 or word[1:at].isalpha()):
                continue
            module = word[at + 1:] or (words[index + 1] if index + 1 < len(words) else "")
            if _PPL_MODULE_RE.fullmatch(module):
                return True
    return False


def _is_ppl_command(words):
    """True when `words`, a command with its wrappers taken off, runs the ppl CLI: ppl or
    ppl.cli spelled by any path, or python -m ppl[.<module>], past any assignment before
    it."""
    i = 0
    while i < len(words) and _ASSIGNMENT_WORD_RE.match(words[i]):
        i += 1
    if i >= len(words):
        return False
    verb = _verb_name(words[i])
    if verb in ("ppl", "ppl.cli"):
        return True
    return verb.startswith("python") and _runs_ppl_module(words[i + 1:])


def _spells_ppl(command, segments):
    """True when `command` may run ppl under a wrapper the write walk does not unwrap
    (timeout, xargs, sudo, find -exec, stdbuf, an env -S string): ppl spelled in the raw text
    (PPL_INVOCATION_RE), or a word shlex reads as ppl or ppl.cli by any path, or a python with
    -m ppl[.<module>], anywhere in a segment the walk split."""
    if PPL_INVOCATION_RE.search(command):
        return True
    for tokens, _, _ in segments:
        for index, word in enumerate(tokens or ()):
            if _verb_name(word) in ("ppl", "ppl.cli"):
                return True
            if _verb_name(word).startswith("python") and _runs_ppl_module(tokens[index + 1:]):
                return True
    return False


def _whole_word(task, text):
    return re.search(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(task), text)


def _task_named(command, segments, tasks):
    """The task of `tasks` that `command` mentions: a word the write walk split, of its own or
    as the value of an assignment or an option (--task=<id>), or a whole word anywhere in the
    raw text, free text and quoted strings included."""
    for tokens, _, _ in segments:
        for word in tokens or ():
            value = word.rpartition("=")[2]
            if value in tasks:
                return value
    for task in sorted(tasks):
        if _whole_word(task, command):
            return task
    return None


def _task_assigned(command, segments, tasks):
    """The task of `tasks` a PPL_TASK assignment names anywhere in `command`, as a word the
    write walk split (PPL_TASK=<id> before a command, after env, in an export) or in the raw
    text."""
    for tokens, _, _ in segments:
        for word in tokens or ():
            if word.startswith("PPL_TASK=") and word[len("PPL_TASK="):] in tasks:
                return word[len("PPL_TASK="):]
    for task in sorted(tasks):
        if re.search(r"PPL_TASK=[\"']?%s(?![A-Za-z0-9_])" % re.escape(task), command):
            return task
    return None


# What the shell may expand in a directory the walk took as written: a glob, a brace, a ~user.
_UNPLACED_CHARS = frozenset("*?[{")


# What a segment's command position may hold that the cd follower does not see through: a
# cd behind a control keyword, inside a function or in a string the shell runs again is not
# one it follows [R-LANE-CROSS-TASK].
_SHELL_STRUCTURE_WORDS = frozenset({"if", "then", "elif", "else", "while", "until", "for", "do",
                                    "case", "select", "function", "eval", "source", "."})


def _shell_structure(tokens, start):
    """The control keyword, eval, source, `.`, exec with a command or function definition a
    segment's command position holds, else None."""
    word = tokens[start] if start < len(tokens) else ""
    if word in _SHELL_STRUCTURE_WORDS:
        return word
    # `f() {` reaches the walk as the word f(, its ) read as a group closer
    if "()" in word or word.endswith("(") or tokens[start + 1:start + 2] == ["()"]:
        return "a function definition"
    if start < len(tokens) and any(_verb_name(token) == "exec" for token in tokens[:start]):
        return "exec"
    return None


def _unplaced_cwd(segments, cwd):
    """Why the directories a command's segments run in cannot be placed, else None: the call
    names no cwd, the walk was blinded (a $(...) or a backtick, a segment it cannot split
    into words), a segment runs a control keyword, eval, source, `.`, exec with a command or
    a function definition (_shell_structure), behind which a cd is not followed, or a cd,
    pushd, popd or env -C went where it cannot follow (a variable, cd -, pushd +N, a bare cd)
    or to a glob, a brace or a ~user it takes as written."""
    if not isinstance(cwd, str):
        return "a call that names no cwd"
    if not segments or any(tokens is None for tokens, _, _ in segments):
        return "a $(...), a backtick or a word the shell may split otherwise"
    for tokens, start, _ in segments:
        structure = _shell_structure(tokens, start)
        if structure:
            return "%s, behind which a cd is not followed" % structure
    for _, _, where in segments:
        if where is None:
            return "a cd, pushd, popd or env -C to a variable, cd -, pushd +N or a bare cd"
        if any(char in _UNPLACED_CHARS for char in where) or any(
                part.startswith("~") for part in where.split("/")):
            return "a cd to %s, which the shell expands" % where
    return None


def ppl_task_mismatch(command, binding, lanes_list, cwd=None):
    """("BOUNDARY_VIOLATION", reason) when a Bash command of a session bound to a lane may run
    ppl on another lane's run, else None [R-LANE-CROSS-TASK], for the orchestrator and every
    role alike. It fails closed on what the write walk cannot read:
    - a PPL_TASK assignment anywhere in the command naming a task another registry entry
      holds, ppl or no ppl;
    - a command that runs or spells ppl (a segment the walk reads as ppl, ppl spelled in the
      raw text, or a word shlex reads as ppl, behind any wrapper) and mentions such a task
      anywhere, as --task <id>, --task=<id>, a positional <id>, or as a whole word of free
      text;
    - ppl run in a directory inside another lane's start directory or worktree, where a bare
      ppl with no PPL_TASK takes that lane's run (cli.resolve_task reads lanes.for_path), the
      cwd followed through cd, pushd, popd and env -C as the write walk follows it, and the
      session's own cwd where the walk recognises no ppl segment;
    - a command that runs or spells ppl where that cwd cannot be placed (_unplaced_cwd): no
      cwd in the call, the walk blinded by a substitution, a control keyword, eval, source,
      exec or a function definition, or a cd it cannot follow or that the shell expands.
    A task with no lane is no other lane's run: a pending reflection names a destroyed run's
    task. `lanes_list` is the registry as read for this call: None, it could not be read, and
    a command that may run ppl or sets PPL_TASK gets ("OWNER_DECISION", why)."""
    if not (isinstance(command, str) and binding.task and not binding.problem):
        return None
    segments = []
    _bash_write_targets(command, cwd if isinstance(cwd, str) else None, segments)
    places = [where for tokens, start, where in segments
              if tokens is not None and _is_ppl_command(tokens[start:])]
    invoked = bool(places) or _spells_ppl(command, segments)
    assigns = "PPL_TASK" in command or any(
        word.startswith("PPL_TASK=") for tokens, _, _ in segments for word in tokens or ())
    if not (invoked or assigns):
        return None
    if lanes_list is None:
        return ("OWNER_DECISION",
                "lane %s's registry cannot be read now, so a ppl command cannot be placed "
                "against the other lanes' runs: repair PPL_HOME/lanes.json, then run ppl lane "
                "resume %s" % (binding.task, binding.task))
    others = [item for item in lanes_list if isinstance(item, dict)
              and isinstance(item.get("task"), str) and item.get("task") != binding.task]
    tasks = set(item["task"] for item in others)
    assigned = _task_assigned(command, segments, tasks)
    if assigned:
        return ("BOUNDARY_VIOLATION",
                "lane %s may not set PPL_TASK to %s, another lane's run: a lane drives only its "
                "own run, and ppl reads PPL_TASK, which ppl lane set to %s"
                % (binding.task, assigned, binding.task))
    if not invoked:
        return None
    named = _task_named(command, segments, tasks)
    if named:
        return ("BOUNDARY_VIOLATION",
                "lane %s may not run ppl on run %s, which is another lane's: a lane drives only "
                "its own run, and a command that runs ppl may not name %s anywhere, so name %s "
                "or leave the task out (ppl reads PPL_TASK)"
                % (binding.task, named, named, binding.task))
    unplaced = _unplaced_cwd(segments, cwd)
    if unplaced:
        return ("BOUNDARY_VIOLATION",
                "lane %s may not run ppl where the guard cannot tell the directory it runs in "
                "(%s): a ppl run from inside another lane takes that lane's run, so spell the "
                "directory plainly, or leave the cd out" % (binding.task, unplaced))
    for where in places or [cwd if isinstance(cwd, str) else None]:
        # the lane's own directories are its own, whatever else a registry entry names there
        if not where or (binding.entry and lanes.holding([binding.entry], where)):
            continue
        holder = lanes.holding(others, where)
        if holder:
            return ("BOUNDARY_VIOLATION",
                    "lane %s may not run ppl from %s, inside lane %s's start directory or "
                    "worktree, where a bare ppl takes run %s: run it from lane %s's own "
                    "directories" % (binding.task, where, holder["task"], holder["task"],
                                     binding.task))
    return None


# The read forms of git config, the only ones a role runs in a lane [R-LANE-SHARED-GIT-DIR]:
# the first word after config must be one of these, an option of the old syntax or a verb of
# the new one, and no action option may follow anywhere.
_GIT_CONFIG_READS = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch",
                               "--list", "-l", "get", "list"})
_GIT_CONFIG_ACTIONS = frozenset({"--add", "--replace-all", "--unset", "--unset-all",
                                 "--rename-section", "--remove-section", "--edit", "-e"})


# A config key a command line may not set for git in a lane: an alias turns a word into
# another subcommand, and an include brings in a file that can define one. Keys are read
# in any letter case, as git reads a section name.
_GIT_ALIAS_KEY_RE = re.compile(r"(?:alias|include|includeif)\.", re.IGNORECASE)
# An environment variable that hands git config from elsewhere, an alias with it: the
# GIT_CONFIG_* family, and a HOME or XDG_CONFIG_HOME that moves the global config.
_GIT_CONFIG_ENV_RE = re.compile(r"^(?:GIT_CONFIG\w*|HOME|XDG_CONFIG_HOME)=")
_GIT_ALIAS_SPELLED_RE = re.compile(
    r"\bgit\b[\s\S]*(?:-c\s*|--config-env[\s=]*)[\"']?(?:alias|include)", re.IGNORECASE)


def _git_subcommand(words):
    """(subcommand, the words after it, problem) of what follows a git, past git's own
    options, each read as git.c reads it. `problem` is why the subcommand cannot be trusted:
    an option this does not read, which may take a value and shift every word after it, a
    value-taking option with no value, or an alias or include set on the command line (-c,
    --config-env), which can make a word another subcommand. (None, [], None) when no
    subcommand follows."""
    i = 0
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word in _GIT_GLOBAL_VALUE_FLAGS:
            if i + 1 >= len(words):
                return None, [], "%s with no value" % word
            if word in ("-c", "--config-env") and _git_config_key_problem(words[i + 1]):
                return None, [], "%s %s, %s" % (word, words[i + 1],
                                                _git_config_key_problem(words[i + 1]))
            i += 2
        elif word.startswith(_GIT_GLOBAL_GLUED):
            if (word.startswith("--config-env=")
                    and _git_config_key_problem(word[len("--config-env="):])):
                return None, [], "%s, %s" % (word,
                                             _git_config_key_problem(word[len("--config-env="):]))
            i += 1
        elif word in _GIT_GLOBAL_FLAGS:
            i += 1
        else:
            return None, [], "the option %s, which the guard does not read" % word
    if i >= len(words):
        return None, [], None
    if any(char in words[i] for char in _BUILT_WORD_CHARS):
        return None, [], "the subcommand %s, which the shell builds or expands" % words[i]
    return words[i], words[i + 1:], None


# What makes a word one the shell builds or expands before git or the guard sees it: a
# variable, a substitution, a glob, a brace.
_BUILT_WORD_CHARS = frozenset("$`*?[{")


def _git_config_key_problem(value):
    """Why a -c or --config-env value cannot be let through in a lane, else None: an alias or
    an include, or a value the shell builds, which may be either."""
    if _GIT_ALIAS_KEY_RE.match(value):
        return "an alias or include set on the command line"
    if "$" in value or "`" in value:
        return "a config value the shell builds"
    return None


def _git_config_write(rest):
    """True unless the words after git config are a read: the first a read option or verb,
    and no action option among them. Anything else, an implicit get or set included, is a
    write form."""
    if not rest or rest[0] not in _GIT_CONFIG_READS:
        return True
    return any(word in _GIT_CONFIG_ACTIONS or word.split("=", 1)[0] in _GIT_CONFIG_ACTIONS
               for word in rest)


_GIT_CONFIG_SPELLED_RE = re.compile(r"\bgit\b[\s\S]*\bconfig\b")
_GIT_WORKTREE_SPELLED_RE = re.compile(r"\bgit\b[\s\S]*\bworktree\b")


# A here-document operator and its delimiter word, plain or quoted.
# The groups: the - of <<-, a single-quoted, a double-quoted or a backslashed delimiter, and
# a bare one. A quoted delimiter keeps the shell from expanding the body.
_HEREDOC_OP_RE = re.compile(
    r"<<(-?)[ \t]*(?:'([^'\n]*)'|\"([^\"\n]*)\"|\\([^\s;&|<>()]+)|([^\s;&|<>()]+))")


class _Unreadable(Exception):
    """A substitution or a here-document left open."""


# What a here-document is fed to: cat is data (a commit message), a shell or any other
# command runs its body as commands, and an interpreter runs it as code the walk cannot
# read [R-LANE-CROSS-WRITE].
_HEREDOC_WRAPPERS = frozenset({"env", "command", "builtin", "exec", "nohup", "nice", "time",
                               "sudo", "doas", "xargs", "timeout", "stdbuf"})
_HEREDOC_INTERPRETERS = frozenset({"perl", "ruby", "node", "nodejs", "php", "osascript", "lua",
                                   "tclsh", "Rscript", "deno", "bun", "pwsh", "awk", "gawk",
                                   "irb", "julia", "jshell", "groovy"})


def _heredoc_consumer(prefix):
    """What the here-document that follows `prefix` is fed to, as "data" (cat), "interpreter"
    or "commands", read off the last simple command of `prefix` past assignments, options and
    the usual wrappers. Python and the other interpreters run code, and anything else, a shell
    (sh, bash, zsh, dash, ksh, behind env or xargs too) among them, runs commands. cat's body
    is data only when _heredoc_kind also finds its delimiter quoted and no pipe onward."""
    part = re.split(r"\|\||&&|[;|\n(){}]", prefix)[-1]
    for token in _shlex_tokens(part) or []:
        if (_ASSIGNMENT_WORD_RE.match(token) or token.startswith("-") or token.isdigit()
                or token in (">", ">>", "<") or _verb_name(token) in _HEREDOC_WRAPPERS):
            continue
        name = _verb_name(token)
        if name == "cat":
            return "data"
        if name.startswith("python") or name.startswith("pypy") or name in _HEREDOC_INTERPRETERS:
            return "interpreter"
        return "commands"
    return "commands"


def _heredoc_kind(text, match, prefix):
    """How the here-document whose operator `match` found in `text` after `prefix` is judged
    [R-LANE-CROSS-WRITE]: "data" only when it is fed to cat, its delimiter is quoted (the
    shell then expands nothing in the body) and nothing on its line pipes it onward (cat <<'EOF'
    | bash runs it); "interpreter" when it is fed to one; else "commands"."""
    consumer = _heredoc_consumer(prefix)
    if consumer != "data":
        return consumer
    quoted = match.group(2) is not None or match.group(3) is not None or (
        match.group(4) is not None)
    end = text.find("\n", match.end())
    onward = text[match.end():len(text) if end < 0 else end].replace("||", "")
    return "data" if quoted and "|" not in onward else "commands"


def _mask_text(text, i, closer, inner, fed):
    """(text[i:] up to `closer` outside quotes, with each $(...) and backtick replaced by the
    word $SUBST and the text inside each appended to `inner`, the index after `closer`). A
    here-document body is taken out: dropped when it is data (_heredoc_kind), appended to `fed`
    when fed to an interpreter, and appended to `inner` as commands, masked the same way, when
    fed to a shell or any other command. Raises _Unreadable on a substitution or a
    here-document left open."""
    out, pending = [], []
    quote, depth, n = None, 0, len(text)
    while i < n:
        char = text[i]
        if quote == "'":
            out.append(char)
            quote = None if char == "'" else quote
            i += 1
            continue
        if char == "\\":
            out.append(text[i:i + 2])
            i += 2
            continue
        if quote is None and closer is not None and char == closer and (
                closer == "`" or depth == 0):
            return "".join(out), i + 1
        if text.startswith("$(", i):
            body, i = _mask_text(text, i + 2, ")", inner, fed)
            inner.append(body)
            out.append("$SUBST")
            continue
        if char == "`":
            body, i = _mask_text(text, i + 1, "`", inner, fed)
            inner.append(body)
            out.append("$SUBST")
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
        elif quote is None and char == "'":
            quote = "'"
        elif quote is None and text.startswith("<<", i) and not text.startswith("<<<", i):
            match = _HEREDOC_OP_RE.match(text, i)
            if match:
                word = next(group for group in match.groups()[1:] if group is not None)
                pending.append((match.group(1) == "-", word,
                                _heredoc_kind(text, match, "".join(out))))
                out.append(match.group())
                i = match.end()
                continue
        elif quote is None and char == "\n" and pending:
            out.append(char)
            i += 1
            for strip_tabs, word, consumer in pending:
                body = []
                while True:
                    if i >= n:
                        raise _Unreadable("here-document %s left open" % word)
                    end = text.find("\n", i)
                    end = n if end < 0 else end
                    line = text[i:end]
                    i = end + 1
                    if (line.lstrip("\t") if strip_tabs else line) == word:
                        break
                    body.append(line)
                if consumer == "interpreter":
                    fed.append("\n".join(body))
                elif consumer == "commands":
                    inner.append(_mask_text("\n".join(body), 0, None, inner, fed)[0])
            pending = []
            continue
        elif quote is None and char == "(":
            depth += 1
        elif quote is None and char == ")" and depth:
            depth -= 1
        out.append(char)
        i += 1
    if closer is not None:
        raise _Unreadable("%s left open" % closer)
    return "".join(out), n


def _command_views(command):
    """([the command with every $(...) and backtick masked to the word $SUBST and every
    here-document body taken out, then the same of the text inside each substitution and of
    each here-document body fed to a shell or another command], [each here-document body fed
    to an interpreter]), or None when one is left open (_mask_text). A commit message in a
    here-document fed to cat is data, not commands."""
    inner, fed = [], []
    try:
        outer, _ = _mask_text(command, 0, None, inner, fed)
    except _Unreadable:
        return None
    return [outer] + inner, fed


def _lane_git_segments(command, cwd):
    """(every segment the write walk reads over each view of `command` (_command_views), the
    texts it could not read as words, the here-document bodies fed to an interpreter): the
    whole command is unread when a substitution or a here-document is left open, else each
    segment the walk cannot split."""
    found = _command_views(command)
    if found is None:
        return [], [command], []
    views, fed = found
    segments = []
    for view in views:
        _bash_write_targets(view, cwd if isinstance(cwd, str) else None, segments)
    return segments, [text for tokens, text, _ in segments if tokens is None], fed


# What an interpreter's here-document may not spell in a lane: ppl, or a git worktree or
# config call, which the walk cannot read inside code.
_INTERPRETED_SPELLING_RE = re.compile(
    r"(?<![\w.-])ppl\b|\bgit\b[\s\S]*?\b(?:worktree|config)\b")


def lane_git_denial(command, agent, binding, cwd=None):
    """("BOUNDARY_VIOLATION", reason) for a git command a session bound to a lane may not run,
    else None. Any word read as git counts, by any path and behind any wrapper, and a command
    the write walk cannot split fails closed on the spelling:
    - git worktree in any form but list, for every session [R-LANE-CROSS-WRITE]: git takes a
      worktree by a unique tail of its path from anywhere in the repository, so remove,
      move, prune, lock, unlock and repair can reach another lane's worktree whatever path
      the guard reads, and ppl lane makes and ppl destroy removes a lane's own;
    - git config in any form but a read, for a ppl-* role [R-LANE-SHARED-GIT-DIR]: git in a
      worktree writes its config into the project checkout's shared .git/config, which every
      lane on that checkout and the operator's own git read;
    - a git whose subcommand cannot be trusted, for every session [R-LANE-CROSS-WRITE]: an
      alias or include set on its command line (-c, --config-env, keys in any case), config
      handed in from elsewhere by GIT_CONFIG_*, HOME or XDG_CONFIG_HOME, or a global option
      the guard does not read, which may take a value and shift the words (_git_subcommand).
    Aliases in the operator's own global config are the operator's, and the checkout's
    .git/config is kept from the lanes (R-LANE-SHARED-GIT-DIR)."""
    if not (isinstance(command, str) and binding.task and not binding.problem):
        return None
    role = escape.is_role(agent)
    segments, unread, fed = _lane_git_segments(command, cwd)
    for body in fed:
        spelled = _INTERPRETED_SPELLING_RE.search(body)
        if spelled:
            return ("BOUNDARY_VIOLATION",
                    "lane %s may not feed an interpreter a here-document that spells %r: the "
                    "guard cannot read ppl, git worktree or git config inside code"
                    % (binding.task, spelled.group()[:60]))
    # only what the walk could not read as words is judged by its spelling: a git whose
    # subcommand it reads, a commit whose message is a substitution among them, is judged by
    # that subcommand
    worktree = any(_GIT_WORKTREE_SPELLED_RE.search(text) for text in unread)
    config = role and any(_GIT_CONFIG_SPELLED_RE.search(text) for text in unread)
    untrusted = (any(_GIT_ALIAS_SPELLED_RE.search(text) for text in unread)
                 and "an alias or include set on the command line")
    runs_git = any(re.search(r"\bgit\b", text) for text in unread)
    for tokens, start, _ in segments:
        if tokens is None:
            continue
        verb = tokens[start] if start < len(tokens) else ""
        if (("$" in verb or "`" in verb)
                and ({"worktree", "config"} & set(tokens) or _runs_ppl_module(tokens)
                     or any(_verb_name(word) in ("ppl", "ppl.cli") for word in tokens))):
            untrusted = untrusted or ("the command word %s, which the shell builds, before "
                                      "a worktree, config or ppl word" % verb)
        for index, word in enumerate(tokens):
            if _verb_name(word) != "git":
                continue
            runs_git = True
            sub, rest, problem = _git_subcommand(tokens[index + 1:])
            # an option it cannot read shifts every word after it: in command position the
            # git is refused, elsewhere (a wrapper's operand, a search word) when a
            # subcommand it guards follows anywhere
            if problem and ("alias or include" in problem or index == start
                            or {"worktree", "config"} & set(tokens[index + 1:])):
                untrusted = untrusted or problem
            if sub == "worktree" and rest[:1] != ["list"]:
                worktree = True
            if sub == "config" and role and _git_config_write(rest):
                config = True
    if runs_git:
        moved = [word for tokens, _, _ in segments for word in tokens or ()
                 if _GIT_CONFIG_ENV_RE.match(word)]
        if moved or any(re.search(r"(?:GIT_CONFIG\w*|HOME|XDG_CONFIG_HOME)=", text)
                        for text in unread):
            untrusted = untrusted or ("%s, which hands git config from elsewhere"
                                      % (moved[0].split("=", 1)[0] + "=" if moved
                                         else "an environment assignment"))
    if untrusted:
        return ("BOUNDARY_VIOLATION",
                "lane %s may not run a command with %s: the guard reads git's subcommand to "
                "keep git worktree and a role's git config in bounds, and that could make it "
                "another" % (binding.task, untrusted))
    if worktree:
        return ("BOUNDARY_VIOLATION",
                "lane %s may not run git worktree but git worktree list: git takes a worktree "
                "by a tail of its path, which can name another lane's, and ppl lane and ppl "
                "destroy make and remove a lane's own" % binding.task)
    if config:
        return ("BOUNDARY_VIOLATION",
                "%s may not write git config in lane %s: the project checkout's .git/config is "
                "shared by every lane on it and by the operator's own git, so only a read is a "
                "role's, its first word one of %s and no %s after it"
                % (agent, binding.task, ", ".join(sorted(_GIT_CONFIG_READS)),
                   ", ".join(sorted(_GIT_CONFIG_ACTIONS))))
    return None


def _decide(payload, argv=()):
    """The decision on one tool call, for the hook whose command line is `argv`."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    agent = payload.get("agent_type")
    # The lane binding, read once and only when a check needs it: the role checks just
    # below decide from the call alone.
    bound = []

    def binding():
        if not bound:
            bound.append(lanes.binding(argv, _READ_LIMIT))
        return bound[0]

    # R-OS-SANDBOX-ROLE-NO-ESCAPE, R-OS-SANDBOX-ORCHESTRATOR-SHAPE: a call that asks to run
    # outside the OS sandbox is decided first, before any file read, so a ppl-* role is
    # refused from the call alone. One the escape check lets through still meets every
    # check below, the tracker gate and the forge denials included.
    if escape.flagged(payload):
        refusal = escape.decide(payload, facts_file(argv), lambda: bound_state(binding()))
        if refusal:
            return deny(*refusal)

    # R-FORGE-GH-MCP-NO-ROLE, R-WEBFETCH-GITHUB-NO-ROLE: an MCP server and WebFetch run
    # outside the OS sandbox, so no role reaches GitHub through either, run or no run. The
    # orchestrator's GitHub MCP reads stay open. Decided before any run file is read, so a run
    # file a role made unreadable or huge cannot hold these back. The server name the run's
    # snapshot gives is matched further down.
    if escape.is_role(agent):
        # a tracker write keeps the tracker's reason (R-JIRA-NO-ROLE-TOOL below)
        if agent in boundary.ROLE_RULES and default_tracker_write_tool(tool):
            return deny("BOUNDARY_VIOLATION", _ROLE_TRACKER_WRITE % agent)
        if (isinstance(tool, str) and tool.startswith("mcp__")
                and escape.github_mcp_tool(tool, ())):
            return _github_mcp_denial(agent, tool)
        if tool in FETCH_TOOLS and escape.webfetch_github(tool_input):
            return deny("BOUNDARY_VIOLATION",
                        "%s may not WebFetch %s: no role reaches GitHub, and WebFetch runs "
                        "outside the OS sandbox" % (agent, str(tool_input.get("url"))[:200]))

    # R-OS-SANDBOX-PROTECTED-PATHS: an edit tool runs outside the OS sandbox too, so while the
    # guard facts exist it writes only where sandboxed Bash may, and never what an unsandboxed
    # gh, git or ppl reads, for roles and the orchestrator alike, before any run file is read.
    # In a lane with no facts the protected paths every session has still hold. Every path
    # key of the call is checked, not only the one target_path takes.
    if tool in WRITE_TOOLS:
        refusal = protected_edit_denial(payload, argv)
        if refusal:
            return deny(*refusal)

    # The bound task's snapshot only adds to the global denials below, so it is asked even
    # for a lane the registry cannot confirm. A malformed --task names no snapshot.
    active = binding().task

    denied = denied_tools(active)
    if isinstance(tool, str) and tool in denied:
        return deny("BOUNDARY_VIOLATION", denied[tool])

    # R-JIRA-NO-ROLE-TOOL: no role calls a tracker MCP write tool of any known
    # tracker, run or no run, merged or not. The CLI twin is R-TRACKER-CLI-NO-ROLE.
    if (isinstance(agent, str) and agent in boundary.ROLE_RULES
            and any_tracker_write_tool(tool, active)):
        return deny("BOUNDARY_VIOLATION", _ROLE_TRACKER_WRITE % agent)

    # R-FORGE-GH-MCP-NO-ROLE under the GitHub MCP server name the run's snapshot gives.
    if (escape.is_role(agent) and isinstance(tool, str) and tool.startswith("mcp__")
            and escape.github_mcp_tool(tool, github_mcp_prefixes(active))):
        return _github_mcp_denial(agent, tool)

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
        # R-TRACKER-CLI-NO-ROLE: every role, run or no run, merged or not. The tracker
        # is the orchestrator's, and a role's issue reads stay open.
        if agent in boundary.ROLE_RULES:
            why = tracker_cli_write(command)
            if why:
                return deny("BOUNDARY_VIOLATION",
                            _with_reading(_ROLE_TRACKER_WRITE % agent, why))

    path = target_path(tool_input) if tool in WRITE_TOOLS else None
    if path and structured_write_lands_in_runs(path, payload.get("cwd")):
        return deny("BOUNDARY_VIOLATION",
                    "run state is written only by ppl, use the ppl CLI")

    # R-LANE-ROLE-OUTSIDE, R-LANE-HOOK-BINDING: past the global denials every check is the
    # bound run's, so a session in no lane stops here, and one in a lane that cannot be
    # confirmed fails closed.
    refusal = lane_denial(binding(), agent, tool)
    if refusal:
        return refusal
    if binding().problem:
        return None

    # R-LANE-START-DIR-PROTECTED, R-LANE-CROSS-WRITE, R-LANE-CROSS-TASK and
    # R-LANE-SHARED-GIT-DIR: before the bound run's state is read, so a lane before ppl init
    # is held to them as after it, and the start directories for a session in no lane too.
    # The registry is read for the other lanes, the binding having read only this lane's
    # entry.
    if tool in WRITE_TOOLS or tool == "Bash":
        try:
            listed = lanes.read(_READ_LIMIT)
        except Exception:
            listed = None
        refusal = cross_lane_denial(payload, binding(), listed)
        if refusal is None and tool == "Bash":
            refusal = (ppl_task_mismatch(tool_input.get("command"), binding(), listed,
                                         payload.get("cwd"))
                       or lane_git_denial(tool_input.get("command"), agent, binding(),
                                          payload.get("cwd")))
        if refusal:
            return deny(*refusal)

    task_id, state = bound_state(binding())
    if state is None:
        # state.json for the bound run is missing, corrupt, or malformed. The
        # write boundary cannot be checked without a readable repo path, and a
        # ppl-* agent_type is pipeline-internal, so a tool call carrying one is
        # denied outright rather than silently falling through every scoped check
        # because state could not be read.
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
        if task_id:
            # R-UNROLED-REPO-WRITE before ppl init, or over an unreadable state.json: the
            # lane's own worktree and repo place the write instead
            return stateless_lane_write_denial(tool, tool_input, path, payload, agent,
                                               task_id, binding().entry)
        return None

    is_role = agent in boundary.ROLE_RULES
    # Read once for the whole decision. None (the snapshot cannot be trusted)
    # denies a writing role's write and the test-worker dispatch, and reads
    # get () below [R-TEST-PATTERNS-FROM-SNAPSHOT].
    patterns = test_patterns(task_id)

    # R-TEST-WORKER-DISPATCH-LEAK. ppl-test-worker is a pipeline role name, so a
    # dispatch of it is this run's business wherever the dispatching session sits.
    if dispatches_test_worker(tool, tool_input):
        try:
            leak = dispatch_leak_deny(tool_input.get("prompt"), task_id, state, patterns)
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
        # targeting a path inside the bound run's repo gets the same
        # per-role decision a structured Write does, from any cwd, for a
        # role or for an unroled session. See the function's own docstring
        # for exactly what this does and does not cover.
        bash_command = tool_input.get("command", "")
        bash_result = bash_write_boundary_deny(bash_command, payload, agent,
                                               is_role, task_id, state, patterns)
        if bash_result:
            return bash_result

    hits_repo = targets_repo(path, state)
    # Every call from here on is the bound run's, whatever the session's cwd: the hook
    # fires only in its own lane's session, which ppl lane started with this run's
    # settings, so neither cwd nor the target path has to place the call. An unroled
    # session's write outside the run's repo is still untouched, see the tail below.

    # R-TRACKER-CLI-GATE and R-JIRA-GATE: one stage check for both transports. The
    # CLI gate runs first, as it needs no snapshot, and asks the stage before reading
    # the command, the cheap half first.
    if tool == "Bash" and tracker_gate_denial(state):
        why = tracker_cli_write(tool_input.get("command", ""))
        if why:
            return tracker_gate_denial(state, why)
    if tracker_mcp_write(tool, tracker_from_snapshot(task_id)):
        denial = tracker_gate_denial(state)
        if denial:
            return denial

    record = control.read(task_id, _READ_LIMIT)
    if record and record.get("invalid"):
        # control.read's own invalid marker carries command=None, so testing only
        # `record["command"] in HALTING` would miss it and permit every tool call
        # against a run whose halt state cannot be trusted. Fail closed instead,
        # matching control.enforce's own treatment of an unreadable control file.
        return deny("OWNER_DECISION", record["reason"])
    if record and record["command"] in control.HALTING:
        return deny("OWNER_DECISION",
                    "run %s is %sd by the operator" % (task_id, record["command"]))

    # R-TEST-WORKER-READ-TOOLS / R-TEST-WORKER-BASH-READS: the test worker
    # writes tests from behaviour, never from the implementation (5.3/5.4).
    # Placed before the write-only checks below because these are read
    # checks, on tools (Read, Grep, Glob, Bash) the write-boundary logic
    # never looks at.
    if agent == "ppl-test-worker":
        # unknown patterns leave no path test-scoped, so every repo read denies
        read_patterns = () if patterns is None else patterns
        if tool == "Bash":
            command = tool_input.get("command", "")
            reason = bash_denies_main_source_read(command, state, payload.get("cwd"),
                                                  harness_shapes(task_id), read_patterns)
            if reason:
                # an untrusted snapshot is the cause, not the path (decision 2 code)
                return deny("BOUNDARY_VIOLATION", boundary.unknown_patterns_reason(task_id)
                            if patterns is None else reason)
        elif tool in READ_TOOLS:
            raw = read_target_path(tool_input, payload)
            glob = tool_input.get("pattern") if tool == "Glob" else None
            for named in (raw, glob):
                if isinstance(named, str) and _has_dotdot(named):
                    return deny("BOUNDARY_VIOLATION", _dotdot_reason(tool, named))
            # A relative path is the session's, so it resolves against the
            # payload cwd, never the hook process's own.
            target = _resolve_path_against_cwd(raw, payload.get("cwd"))
            if not target:
                return deny("BOUNDARY_VIOLATION",
                            "ppl-test-worker's %s call names no resolvable path" % tool)
            if targets_repo(target, state):
                rel = relative_to_repo(target, state)
                if not boundary.path_is_test_scoped(rel, read_patterns):
                    if patterns is None:
                        return deny("BOUNDARY_VIOLATION",
                                    boundary.unknown_patterns_reason(task_id))
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
        if agent == "ppl-test-worker" and _has_dotdot(path):
            return deny("BOUNDARY_VIOLATION", _dotdot_reason(tool, path))
        if not hits_repo:
            # check_path alone would not close this: its test-worker branch
            # returns as soon as a path looks like a test source, so a
            # test-shaped path outside the tree would pass. A pipeline role has
            # no business writing anywhere but the run's own repo.
            return deny("BOUNDARY_VIOLATION",
                        "%s may write only inside run %s's repo, not %s"
                        % (agent, task_id, path))
        if patterns is None and boundary.ROLE_RULES[agent]["may_write"]:
            return unknown_patterns_deny(task_id)
        try:
            bound = store.read_json(paths.run_dir(task_id) / "boundary.json",
                                    {"write": [], "forbid": []}, _READ_LIMIT)
            boundary.check_path(agent, relative_to_repo(path, state), bound, patterns)
        except PplError as err:
            return deny(err.code, err.message)
        return None

    if hits_repo:
        # DESIGN 5.3: the orchestrator writes state through ppl and nothing else.
        # It carries no agent_type, and neither does any other session, so the
        # discriminator is the target path: inside the bound run's repo it is
        # repo code that only a worker role may touch, and outside it this
        # branch is never reached at all.
        return deny("BOUNDARY_VIOLATION",
                    "%s may not write repo code: %s is inside run %s's repo, which only "
                    "ppl-code-worker and ppl-test-worker write, inside their boundary"
                    % (agent or "a session with no role", path, task_id))
    return None


# The session_id `ppl doctor` puts on its hook_runs probe, an unroled Read of
# /dev/null (ppl/doctor.py:_run_problem).
DOCTOR_PROBE_SESSION_ID = "ppl-doctor-hook-probe"


def _is_doctor_probe(payload):
    return (payload.get("session_id") == DOCTOR_PROBE_SESSION_ID
            and payload.get("agent_type") is None and payload.get("tool_name") == "Read"
            and payload.get("tool_input") == {"file_path": "/dev/null"})


def log_denial(payload, result, argv=()):
    """Append one deny event to the log of the run this hook's `argv` binds it to,
    nothing outside a lane or in one the registry cannot confirm. Best effort:
    whatever goes wrong here, the decision already made stands [R-GUARD-DENIAL-LOGGED].
    The doctor's probe is not logged: it is not a call any session made. A log.jsonl
    that is there but not a regular file is skipped, since opening a FIFO for
    append blocks until a reader appears."""
    try:
        if _is_doctor_probe(payload):
            return
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        bound = lanes.binding(argv, _READ_LIMIT)
        if bound.problem or not bound.task:
            return
        task_id = bound.task
        log = paths.run_dir(task_id) / "log.jsonl"
        if os.path.lexists(log) and not os.path.isfile(log):
            return
        # runs/ is sandbox-writable: no append through a run directory a role made a link
        store.checked_run_path(log)
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


def decide(payload, argv=()):
    """The decision on `payload` for the hook whose command line is `argv` (the arguments
    after the script, as main() passes sys.argv[1:]), with a deny logged to the bound run."""
    result = _decide(payload, argv)
    if result and isinstance(payload, dict):
        log_denial(payload, result, argv)
    return result


def main():
    # The lane binding is the hook's own command line [R-LANE-HOOK-BINDING]. The python
    # floor ran before any of it was read.
    argv = sys.argv[1:]
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        result = _decide(payload, argv)
    except Exception as err:
        # Claude Code reads exit 0 with no output as allow. A ppl-* role is
        # refused instead, an unroled session keeps the allow so a hook defect
        # cannot lock the operator out of unrelated sessions, unless the call
        # asks to leave the OS sandbox [R-GUARD-INTERNAL-ERROR-DENIES-ROLES]. The deny goes out before the
        # diagnostic, and a broken stderr is swallowed, since a nonzero exit
        # would not block the call either.
        agent = payload.get("agent_type") if isinstance(payload, dict) else None
        # A call that asks to leave the OS sandbox is refused whatever its agent
        # [R-OS-SANDBOX-FAIL-CLOSED]. A check that fails reads as flagged.
        try:
            unsandboxed = escape.flagged(payload)
        except Exception:
            unsandboxed = True
        if unsandboxed or (isinstance(agent, str) and agent in boundary.ROLE_RULES):
            refusal = deny(
                "BOUNDARY_VIOLATION",
                "guard internal error, %s call refused: %s"
                % ("unsandboxed" if unsandboxed else "role", type(err).__name__))
            sys.stdout.write(json.dumps(refusal))
            sys.stdout.flush()
            log_denial(payload, refusal, argv)
        try:
            sys.stderr.write("ppl_guard internal error: %s\n" % err)
        except Exception:
            pass
        return 0
    if result:
        # The decision goes out before the log is touched, as in the branch above.
        sys.stdout.write(json.dumps(result))
        sys.stdout.flush()
        if isinstance(payload, dict):
            log_denial(payload, result, argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())

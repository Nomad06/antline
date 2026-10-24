"""Who may run a Bash command outside Claude Code's OS sandbox, the one way a pipeline
command reaches GitHub. The guard asks this first for a Bash call that sets
dangerouslyDisableSandbox: a ppl-* role never may, and while the guard facts exist or a run
is active no other subagent may and the unroled orchestrator may only for one plain command
on a pinned gh, git or ppl in the shapes below and in escape_git. It also closes the routes
that run outside the sandbox anyway: an edit tool writing outside the sandbox's write scope
or what those commands read (escape_paths), and a role's GitHub MCP call or WebFetch of
GitHub. The guard hook imports this module, so it
parses on Python 3.9, carries no annotations and reads JSON only."""
import json
import os
import re
import shlex
import stat
import stringprep
import unicodedata
import urllib.parse

from . import boundary, escape_git, escape_paths, paths, store
# protected_paths is ppl.escape's as well, the name the brief and the rule rows give it.
from .escape_paths import EDIT_TOOLS, FACTS_FILE, PIN_NAMES, protected_paths  # noqa: F401

FLAG = "dangerouslyDisableSandbox"
APPLY = "run `ppl harness apply`"

# What the shell reads as more than one plain word, outside single quotes.
_REFUSED = frozenset(";&|<>()`{}*?[$")
# What zsh or bash may still expand outside any quotes: EXTENDED_GLOB's ^ ~ #, a tilde,
# history or negation. A comment is one of them.
_UNQUOTED_REFUSED = frozenset("~^!#")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_FIELDS = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:,[A-Za-z][A-Za-z0-9_]*)*")
# A jq path only: a filter such as `env` or `$ENV` would print the unsandboxed environment.
_JQ_PATH = re.compile(r"(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_REPLY_ROUTE = re.compile(r"repos/([^/]+/[^/]+)/pulls/[1-9][0-9]{0,9}/comments/[1-9][0-9]{0,19}"
                          r"/replies")

# The orchestrator's gh table: (first verb, second verb) -> the flags taking a value, the
# value-less flags, the required flags and how many <n> operands. Any other flag or word
# denies. `api` is read on its own below.
_GH_TABLE = {
    ("issue", "view"): ({"-R": "tracker", "--json": "fields"}, ("--comments",), ("-R",), 1),
    ("issue", "comment"): ({"-R": "tracker", "--body-file": "body"}, (),
                           ("-R", "--body-file"), 1),
    ("pr", "create"): ({"-R": "forge", "--head": "head", "--base": "base", "--title": "text",
                        "--body-file": "body"}, ("--draft",),
                       ("-R", "--head", "--base", "--title", "--body-file"), 0),
    ("pr", "view"): ({"-R": "forge", "--json": "fields"}, (), ("-R",), 1),
    ("pr", "checks"): ({"-R": "forge"}, (), ("-R",), 1),
    ("pr", "close"): ({"-R": "forge"}, (), ("-R",), 1),
    ("auth", "status"): ({}, (), (), 0),
}


def flagged(payload):
    """True for a Bash call that asks to run outside the OS sandbox. Any value but an absent,
    null or false flag counts, so a string "true" is not a way around the check."""
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return False
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return False
    value = tool_input.get(FLAG)
    return value is not None and value is not False


def _absolute(value):
    return isinstance(value, str) and os.path.isabs(value)


def _facts_problem(doc):
    """What makes `doc` unusable as the guard's facts, or None. Only the keys this module
    reads are checked, extra keys are kept."""
    if not isinstance(doc, dict):
        return "is not a JSON object"
    if doc.get("version") != 1:
        return "has version %r, not 1" % (doc.get("version"),)
    for key in ("workspace", "checkout", "scratch"):
        if not _absolute(doc.get(key)):
            return "has no absolute %s" % key
    if not (isinstance(doc.get("default_branch"), str) and doc["default_branch"]):
        return "has no default_branch"
    for key in ("forge_repo", "tracker_repo"):
        if doc.get(key) is not None and not isinstance(doc.get(key), str):
            return "has a %s that is not a string" % key
    scope = doc.get("write_scope")
    if not (isinstance(scope, list) and all(_absolute(entry) for entry in scope)):
        return "has a write_scope that is not a list of absolute paths"
    # absent in facts written before apply recorded it, then computed (escape_paths)
    denied = doc.get("deny_write")
    if denied is not None and not (isinstance(denied, list)
                                   and all(_absolute(entry) for entry in denied)):
        return "has a deny_write that is not a list of absolute paths"
    pins = doc.get("pins")
    if not isinstance(pins, dict):
        return "has no pins"
    for name in PIN_NAMES:
        pin = pins.get(name)
        if not (isinstance(pin, dict) and _absolute(pin.get("path"))
                and _absolute(pin.get("realpath"))):
            return "has no absolute path and realpath for the %s pin" % name
    return None


def _read_json(path):
    """The JSON document at `path`. FileNotFoundError when it is absent, ValueError for
    anything else, a file that is not a regular one or is over store.HOOK_READ_LIMIT bytes
    included, which is never waited on or read."""
    try:
        text = store.read_regular_text(path, store.HOOK_READ_LIMIT)
    except FileNotFoundError:
        raise
    except Exception as err:
        raise ValueError("%s cannot be read: %s" % (path, getattr(err, "message", None)
                                                       or type(err).__name__))
    try:
        return json.loads(text)
    except Exception as err:
        raise ValueError("%s cannot be read: %s" % (path, type(err).__name__))


def load_facts(home):
    """The parsed `home`/os_sandbox.json, None when it is absent. Raises ValueError when it
    cannot be read or is of the wrong shape."""
    path = os.path.join(str(home), FACTS_FILE)
    try:
        doc = _read_json(path)
    except FileNotFoundError:
        return None
    problem = _facts_problem(doc)
    if problem:
        raise ValueError("%s %s" % (path, problem))
    return doc


def _inside(path, root):
    real_root = os.path.realpath(root).rstrip("/") or "/"
    real_path = os.path.realpath(path)
    return (real_path == real_root or real_root == "/"
            or real_path.startswith(real_root + "/"))


def _is_role(agent):
    return agent in boundary.ROLE_RULES or agent.startswith("ppl-")


def is_role(agent):
    """True for a ppl-* role, one the boundary table names or any other ppl- name."""
    return isinstance(agent, str) and bool(agent) and _is_role(agent)


def edit_denial(payload, home, facts):
    """Why a pipeline session's Edit, Write, MultiEdit or NotebookEdit may not write its
    target, or None. Edit tools run outside the OS sandbox: escape_paths.target_denial says
    why about each path key of the call, for roles and the orchestrator alike, and only while
    `facts` is there: without an OS sandbox profile it stands aside, run or no run. Facts that
    cannot be read are the guard's to refuse before this."""
    if not isinstance(payload, dict) or payload.get("tool_name") not in EDIT_TOOLS:
        return None
    if facts is None:
        return None
    agent = payload.get("agent_type")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    tool = payload["tool_name"]
    who = agent if isinstance(agent, str) and agent else "a session with no role"
    try:
        for key in escape_paths.PATH_KEYS:
            if tool_input.get(key):
                reason = escape_paths.target_denial(tool_input[key], payload.get("cwd"), home,
                                                    facts, who, tool)
                if reason:
                    return reason
    except (OSError, ValueError, TypeError, KeyError) as err:
        return ("%s's %s target cannot be checked against where an edit tool may write: %s"
                % (who, tool, type(err).__name__))
    return None


# The GitHub MCP server under its usual name, in any case, or a numbered second
# registration of it.
_GITHUB_SERVER = re.compile(r"mcp__github(?:_[0-9]+)?__", re.IGNORECASE)


def forge_github_prefixes(forge):
    """The mcp__<server>__ prefix a github [forge] table names in mcp_server, as a list,
    empty for another forge, no server name or a table that is not one."""
    if not isinstance(forge, dict) or forge.get("kind") != "github":
        return []
    name = forge.get("mcp_server")
    return ["mcp__%s__" % name] if isinstance(name, str) and name else []


def github_mcp_tool(tool_name, snapshot_prefixes):
    """True for a tool of a GitHub MCP server: under mcp__github__ or a numbered duplicate,
    under any server whose name holds "github" in any case (the claude.ai connector, a
    plugin's server), and under each mcp__<server>__ prefix in `snapshot_prefixes`."""
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return False
    if _GITHUB_SERVER.match(tool_name):
        return True
    if "github" in tool_name[len("mcp__"):].rpartition("__")[0].casefold():
        return True
    folded = tool_name.casefold()
    for prefix in snapshot_prefixes or ():
        if (isinstance(prefix, str) and len(prefix) > len("mcp____")
                and prefix.startswith("mcp__") and prefix.endswith("__")
                and folded.startswith(prefix.casefold())):
            return True
    return False


# Schemes a URL parser reads an authority after with or without the slashes.
_SPECIAL_SCHEMES = ("http", "https", "ws", "wss", "ftp")
_URL_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*):")
# What a URL parser drops from a URL: leading and trailing C0 controls and spaces, and a
# tab or newline anywhere.
_URL_EDGE = "".join(chr(code) for code in range(0x21))
_URL_DROPPED = re.compile("[\t\n\r]")
# Full stops IDNA reads as the label separator.
_IDN_DOTS = ("\u3002", "\uff0e", "\uff61")


def _url_hosts(url):
    """Every host a URL parser may read `url` to name: as written, with a backslash read as
    a slash, with a special scheme's missing slashes put back, and as a URL with no scheme.
    Raises ValueError for a URL urlsplit cannot read."""
    text = _URL_DROPPED.sub("", url.strip(_URL_EDGE))
    slashed = text.replace("\\", "/")
    readings = [text, slashed, "//" + slashed]
    match = _URL_SCHEME.match(slashed)
    if match and match.group(1).lower() in _SPECIAL_SCHEMES:
        readings.append("//" + slashed[match.end():].lstrip("/"))
    return [urllib.parse.urlsplit(reading).hostname for reading in readings]


def _mapped_host(text):
    """`text` as IDNA maps a host: compatibility forms (fullwidth letters and dots) composed,
    the characters mapped to nothing (a soft hyphen, a zero-width or format character, a
    variation selector) dropped, every full stop a dot, and case folded."""
    text = unicodedata.normalize("NFKC", text)
    text = "".join(char for char in text
                   if not (stringprep.in_table_b1(char) or unicodedata.category(char) == "Cf"))
    for dot in _IDN_DOTS:
        text = text.replace(dot, ".")
    return unicodedata.normalize("NFKC", text.casefold())


def _is_github_host(host):
    """True for github.com or a host under it, after percent-decoding, the IDNA mapping,
    Punycode labels and trailing dots."""
    if not host:
        return False
    labels = []
    for label in _mapped_host(urllib.parse.unquote(host)).split("."):
        if label.startswith("xn--"):
            try:
                label = label[len("xn--"):].encode("ascii").decode("punycode")
            except (UnicodeError, ValueError):
                pass
        labels.append(label)
    host = _mapped_host(".".join(labels)).rstrip(".")
    return host == "github.com" or host.endswith(".github.com")


def webfetch_github(tool_input):
    """True when a WebFetch call's URL host is github.com or ends in .github.com, under any
    reading a URL parser may give it. A URL urlsplit cannot read counts as GitHub."""
    url = tool_input.get("url") if isinstance(tool_input, dict) else None
    if not isinstance(url, str):
        return False
    try:
        hosts = _url_hosts(url)
    except ValueError:
        return True
    return any(_is_github_host(host) for host in hosts)


def decide(payload, home, state):
    """None to let a flagged call go on to the guard's other checks, else (code, reason).
    `state` is the guard's active_state, a callable answering (task_id, run state or None),
    called only past the role checks so a ppl-* role is refused before any file is read. A
    run state already read, or None, is taken as well.

    While the guard facts exist or a run is active, every flagged call is the pipeline's
    whatever its cwd, since a cwd and the run's recorded repo are both in a role's reach: any
    other subagent is denied and the unroled session meets the shape check. With neither, the
    call goes on."""
    agent = payload.get("agent_type")
    if agent is not None and not (isinstance(agent, str) and agent):
        return ("BOUNDARY_VIOLATION",
                "a call whose agent_type is %s names no session the guard can place, so it "
                "may not run outside the OS sandbox" % _short(agent))
    if agent is not None and _is_role(agent):
        return ("BOUNDARY_VIOLATION",
                "%s may not run a command outside the OS sandbox: GitHub is reached only by "
                "the orchestrator's plain gh, git and ppl commands" % agent)
    if callable(state):
        task_id, run_state = state()
    else:
        run_state = state if isinstance(state, dict) else None
        task_id = (run_state or {}).get("task_id")
    try:
        facts = load_facts(home)
    except ValueError as err:
        return ("OWNER_DECISION",
                "the guard's OS sandbox facts are unusable (%s), so a command outside the OS "
                "sandbox cannot be checked: %s" % (err, APPLY))
    if facts is None and not task_id:
        return None
    if facts is None:
        return ("OWNER_DECISION",
                "%s is missing while run %s is active, so no gh, git or ppl is pinned for a "
                "command outside the OS sandbox: %s" % (FACTS_FILE, task_id, APPLY))
    agent_id = payload.get("agent_id")
    if agent is not None or agent_id is not None:
        return ("BOUNDARY_VIOLATION",
                "subagent %s may not run a command outside the OS sandbox while the pipeline "
                "is set up or running: GitHub is reached only by the orchestrator"
                % (agent or "with agent_id %s" % _short(agent_id)))
    if task_id and (run_state is None or run_state.get("task_id") != task_id):
        return ("OWNER_DECISION",
                "run %s state is unreadable, so a command outside the OS sandbox cannot be "
                "checked. Repair or restore state.json to continue." % task_id)
    command = (payload.get("tool_input") or {}).get("command")
    argv, problem = _plain(command)
    if problem:
        return ("BOUNDARY_VIOLATION",
                "the orchestrator leaves the OS sandbox only for one plain command, and this "
                "one has %s" % problem)
    reason = orchestrator_shape(argv, payload.get("cwd"), facts, run_state)
    if reason:
        return ("BOUNDARY_VIOLATION", "outside the OS sandbox: " + reason)
    return None


def _short(value):
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def _plain_problem(command):
    """What makes `command` more than one plain command, or None."""
    if not isinstance(command, str) or not command.strip():
        return "no command"
    quote = None
    index = 0
    while index < len(command):
        char = command[index]
        if (ord(char) < 0x20 and char != "\t") or ord(char) == 0x7f:
            return "a newline or a control character"
        if quote == "'":
            if char == "'":
                quote = None
                # zsh's RC_QUOTES reads '' inside single quotes as one quote
                if command[index + 1:index + 2] == "'":
                    return "two single-quoted strings side by side"
        elif char == "\\":
            following = command[index + 1:index + 2]
            if not following:
                return "a trailing backslash"
            if following in _REFUSED or ord(following) < 0x20:
                return "an escaped %r, which only single quotes may hold" % following
            index += 2
            continue
        elif quote == '"':
            if char == '"':
                quote = None
            elif char in _REFUSED:
                return "%r inside double quotes, which the shell still reads" % char
        elif char == "'":
            quote = "'"
        elif char == '"':
            quote = '"'
        elif char in _REFUSED:
            return "%r outside single quotes" % char
        elif char in _UNQUOTED_REFUSED:
            return "%r outside quotes, which zsh may expand" % char
        elif char == "=" and (index == 0 or command[index - 1] in " \t"):
            return "a word that starts with '=', which zsh expands to a command path"
        index += 1
    if quote:
        return "an unclosed quote"
    return None


def _plain(command):
    """(argv, None) for one plain command, else (None, what failed)."""
    problem = _plain_problem(command)
    if problem:
        return None, problem
    try:
        argv = shlex.split(command, comments=False, posix=True)
    except ValueError:
        return None, "words the shell cannot split"
    if not argv:
        return None, "no command"
    if _ASSIGNMENT.match(argv[0]):
        return None, "a leading assignment %s" % _short(argv[0])
    return argv, None


def plain_argv(command):
    """The argv of one plain command, or None: no separator, redirect, subshell, group, glob,
    expansion, comment, newline or leading assignment outside single quotes, and nothing
    zsh reads differently from a POSIX shell."""
    return _plain(command)[0]


def _task_id(run_state):
    task_id = (run_state or {}).get("task_id")
    if isinstance(task_id, str) and paths.TASK_ID_RE.match(task_id):
        return task_id
    return None


def orchestrator_shape(argv, cwd, facts, state):
    """Why the orchestrator may not run `argv` outside the OS sandbox, or None. `state` is
    the active run's state, or None with no run."""
    if facts is None:
        return "%s is missing, so no gh, git or ppl is pinned: %s" % (FACTS_FILE, APPLY)
    if not argv:
        return "the command is empty"
    for name in PIN_NAMES:
        pin = facts["pins"][name]
        if argv[0] != pin["path"]:
            continue
        if os.path.realpath(pin["path"]) != pin["realpath"]:
            return ("the pinned %s %s no longer resolves to %s, the binary the profile protects: "
                    "%s" % (name, pin["path"], pin["realpath"], APPLY))
        if name == "git":
            # -C names the repository, so the cwd plays no part
            return escape_git.git_shape(argv, facts, state)
        problem = _cwd_problem(name, cwd, facts)
        if problem:
            return problem
        if name == "gh":
            return _gh_problem(argv[1:], facts, state)
        return _ppl_problem(argv[1:], state)
    return ("%s is not a pinned binary: only %s run outside the OS sandbox, spelled exactly"
            % (_short(argv[0]), ", ".join(facts["pins"][name]["path"] for name in PIN_NAMES)))


def _parse(args, values, bools):
    """(operands, {flag: value}) for gh's `--flag value` and `--flag=value` forms, or
    (None, why) for an unknown or repeated flag. A value-taking flag consumes the next word
    whatever it is, as gh does."""
    operands, flags = [], {}
    index = 0
    while index < len(args):
        word = args[index]
        if word.startswith("-") and word != "-":
            name, equals, value = word.partition("=")
            if name in values:
                if not equals:
                    if index + 1 >= len(args):
                        return None, "%s takes a value" % name
                    index += 1
                    value = args[index]
            elif name in bools and not equals:
                value = True
            else:
                return None, "%s is not an allowed flag" % _short(word)
            if name in flags:
                return None, "%s is given twice" % name
            flags[name] = value
        else:
            operands.append(word)
        index += 1
    return operands, flags


def _body_file_problem(value, facts, state):
    """None for a plain file directly in the run's scratch directory, reached through no link.
    A role can write that directory, so a link there could hand gh a secret to post."""
    task_id = _task_id(state)
    if not task_id:
        return "a body file needs an active run, whose scratch directory holds it"
    expected_dir = os.path.join(facts["scratch"], task_id)
    name = os.path.basename(value)
    if (not os.path.isabs(value) or not name
            or os.path.realpath(value) != os.path.join(expected_dir, name)):
        return ("the body file %s is not a file directly in the run's scratch directory %s, "
                "reached through no link" % (_short(value), expected_dir))
    try:
        info = os.lstat(os.path.join(expected_dir, name))
    except OSError:
        return "the body file %s does not exist" % _short(value)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return "the body file %s is not a plain file with a single link" % _short(value)
    return None


def _value_problem(kind, flag, value, facts, state):
    if kind in ("tracker", "forge"):
        repo = facts["tracker_repo" if kind == "tracker" else "forge_repo"]
        if not repo:
            return "the harness names no GitHub %s repo for %s" % (kind, flag)
        if value != repo:
            return "%s %s is not the harness's %s repo %s" % (flag, _short(value), kind, repo)
    elif kind == "fields" and not _FIELDS.fullmatch(value):
        return "%s %s is not a list of field names" % (flag, _short(value))
    elif kind == "body":
        return _body_file_problem(value, facts, state)
    elif kind == "head":
        branch = (state or {}).get("branch")
        if not (isinstance(branch, str) and branch) or branch == facts["default_branch"]:
            return "the run has no branch of its own recorded for --head"
        if value != branch:
            return "--head %s is not the run's branch %s" % (_short(value), branch)
    elif kind == "base" and value != facts["default_branch"]:
        return "--base %s is not the default branch %s" % (_short(value), facts["default_branch"])
    return None


def _cwd_problem(name, cwd, facts):
    """gh, and ppl whose own gh and git calls inherit the cwd, never load a repo a role can
    write, so they run only from the workspace or a directory under it, which the profile
    write-denies, and never from inside a write scope entry. Being outside the write scope is
    not enough: a temp directory is sandbox-writable and in no entry. The reason names the
    workspace, the directory the skill runs them from."""
    if not (isinstance(cwd, str) and os.path.isabs(cwd)):
        return ("%s runs only from a known directory, and this call names no cwd: run it from "
                "the workspace %s" % (name, facts["workspace"]))
    if not _inside(cwd, facts["workspace"]):
        return ("%s runs only from the workspace %s or a directory under it, which no "
                "sandboxed command can write, and %s is not one: cd to the workspace and run "
                "it from there" % (name, facts["workspace"], cwd))
    for entry in facts["write_scope"]:
        if _inside(cwd, entry):
            return ("%s runs only from a directory no sandboxed command can write, and %s is "
                    "inside %s: cd to the workspace %s and run it from there"
                    % (name, cwd, entry, facts["workspace"]))
    return None


def _gh_problem(args, facts, state):
    if args[:1] == ["api"]:
        return _gh_api_problem(args[1:], facts, state)
    shape = _GH_TABLE.get(tuple(args[:2]))
    if shape is None:
        return ("gh %s is not in the table: issue view|comment, pr create|view|checks|close, "
                "api user or a review reply, auth status" % " ".join(_short(a) for a in args[:2]))
    values, bools, required, count = shape
    operands, flags = _parse(args[2:], values, bools)
    if operands is None:
        return "gh %s %s: %s" % (args[0], args[1], flags)
    if len(operands) != count or not all(_NUMBER.fullmatch(word) for word in operands):
        return "gh %s %s takes %s" % (args[0], args[1],
                                      "one number" if count else "no operand")
    for flag in required:
        if flag not in flags:
            return "gh %s %s needs %s" % (args[0], args[1], flag)
    for flag, value in flags.items():
        if flag in values:
            problem = _value_problem(values[flag], flag, value, facts, state)
            if problem:
                return problem
    return None


def _gh_api_problem(args, facts, state):
    operands, flags = _parse(args, {"--jq": "jq", "-F": "field"}, ())
    if operands is None:
        return "gh api: %s" % flags
    if len(operands) != 1:
        return "gh api takes one route"
    route = operands[0]
    if route == "user":
        if "-F" in flags:
            return "gh api user takes only --jq"
        if "--jq" in flags and not _JQ_PATH.fullmatch(flags["--jq"]):
            return "gh api user --jq %s is not a field path" % _short(flags["--jq"])
        return None
    match = _REPLY_ROUTE.fullmatch(route)
    if not match:
        return "gh api %s is not the user route or a review reply route" % _short(route)
    if match.group(1) != facts["forge_repo"]:
        return "gh api %s is not on the harness's forge repo %s" % (_short(route),
                                                                    facts["forge_repo"])
    if set(flags) != {"-F"} or not flags["-F"].startswith("body=@"):
        return "a review reply takes exactly -F body=@<file in the run's scratch directory>"
    return _body_file_problem(flags["-F"][len("body=@"):], facts, state)


def _ppl_problem(args, state):
    task_id = _task_id(state)
    if args[:2] == ["tracker", "fetch"]:
        if task_id and args[2:] in (["--task", task_id], ["--task=" + task_id]):
            return None
        return "ppl tracker fetch takes only --task <the active run's id>"
    if args[:2] == ["review", "fetch"]:
        rest = args[2:]
        number = rest[1] if len(rest) == 2 and rest[0] == "--pr" else (
            rest[0][len("--pr="):] if len(rest) == 1 and rest[0].startswith("--pr=") else "")
        if _NUMBER.fullmatch(number):
            return None
        return "ppl review fetch takes only --pr <number>"
    if args == ["doctor", "--forge"]:
        return None
    return ("ppl %s is not one of tracker fetch --task <id>, review fetch --pr <n> or "
            "doctor --forge" % " ".join(_short(a) for a in args[:2]))

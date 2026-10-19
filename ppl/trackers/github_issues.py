"""GitHub Issues through the gh CLI. The briefer reads no tracker tool, the
orchestrator writes the issue with gh after the merge, and write_denials holds the
check the guard runs on a whole Bash command to tell an issue write from a read.

The check reads the command's words, so a renamed gh binary, an API host held in a
variable, an interpreter or a script file, and a gh config the session changed
outside Bash are not seen. The boundary that would close them is a GitHub
credential the role sessions never hold."""
import re
from urllib.parse import unquote

from ..errors import PplError

KIND = "github-issues"
TRANSPORT = "cli"
KEYS = ("kind", "repo", "task_prefix")
DEFAULTS = {"task_prefix": "GH"}

_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PREFIX = re.compile(r"[A-Z][A-Z0-9]*")
_TASK_ID = re.compile(r"[A-Z][A-Z0-9]*-([1-9][0-9]*)")

# The GitHub MCP server's issue write tools, under any server name.
MCP_WRITE_OPERATIONS = ("create_issue", "update_issue", "add_issue_comment", "issue_write",
                        "sub_issue_write", "add_sub_issue", "remove_sub_issue",
                        "reprioritize_sub_issue", "assign_copilot_to_issue")
_MCP_WRITE = re.compile(r"mcp__[A-Za-z0-9_-]+__(?:%s)\Z" % "|".join(MCP_WRITE_OPERATIONS))

# The commands of gh 2.92.0 (2026-04-28): those `gh help` lists, their aliases, the
# hidden ones and the help topics. Any other name is a user alias or an extension,
# which can run any gh command under a name read here as nothing.
GH_VERSION = "2.92.0"
BUILTINS = frozenset((
    "auth", "browse", "codespace", "cs", "gist", "issue", "org", "pr", "project", "release",
    "repo", "skill", "skills", "cache", "run", "workflow", "agent-task", "agent-tasks",
    "agent", "agents", "alias", "api", "attestation", "at", "completion", "config", "copilot",
    "extension", "extensions", "ext", "gpg-key", "label", "licenses", "preview", "ruleset",
    "rs", "search", "secret", "ssh-key", "status", "variable", "help", "version", "credits",
    "accessibility", "actions", "environment", "exit-codes", "formatting", "mintty",
    "reference", "telemetry"))
WRITE_VERBS = frozenset(("comment", "create", "new", "edit", "close", "reopen", "delete",
                         "transfer", "pin", "unpin", "lock", "unlock", "develop"))
# Every mutation behind a gh issue write verb, plus the sub-issue and duplicate ones.
MUTATIONS = ("addComment", "updateIssueComment", "deleteIssueComment", "createIssue",
             "updateIssue", "closeIssue", "reopenIssue", "deleteIssue", "transferIssue",
             "pinIssue", "unpinIssue", "lockLockable", "unlockLockable",
             "addLabelsToLabelable", "removeLabelsFromLabelable", "clearLabelsFromLabelable",
             "addAssigneesToAssignable", "removeAssigneesFromAssignable",
             "updateIssueIssueType", "createLinkedBranch", "deleteLinkedBranch", "addSubIssue",
             "removeSubIssue", "reprioritizeSubIssue", "markIssueAsDuplicate",
             "unmarkIssueAsDuplicate")
_MUTATION = re.compile(r"(?<![A-Za-z0-9_])(?:%s)(?![A-Za-z0-9_])" % "|".join(MUTATIONS),
                       re.IGNORECASE)
# An issues route or the GraphQL endpoint of github.com or of a GitHub Enterprise
# host, named by any client.
_API_URL = re.compile(
    r"(?:api\.github\.com\.?(?::[0-9]+)?|/api/v3)/+(?:(?:repos/+[^/\s?#]+/+[^/\s?#]+"
    r"|repositories/+[0-9]+)/+(?:[^\s?#]*?/+)?issues(?![A-Za-z0-9_-])|graphql(?![A-Za-z0-9_-]))"
    r"|/api/graphql(?![A-Za-z0-9_-])", re.IGNORECASE)
_GH_NAME = re.compile(r"(?<![A-Za-z0-9_.-])gh(?![A-Za-z0-9_.-])")
_SHELL_TEXT = re.compile(r"[\s;&|()<>`$'\"\\]")
# A word after an unreadable program word that makes it worth reading as gh.
_SENSITIVE = frozenset(("issue", "api", "alias", "extension", "extensions", "ext", "auth",
                        "copilot", "config"))
# Programs that run a word of theirs as a script, so that word is read as a command:
# always, or when given one of the listed flags.
_SHELLS = frozenset(("bash", "sh", "zsh", "dash", "ksh", "mksh", "fish", "eval", "watch",
                     "ssh"))
_SCRIPT_FLAGS = {"env": ("-S", "--split-string"), "script": ("-c", "--command"),
                 "flock": ("-c", "--command"), "su": ("-c", "--command"),
                 "runuser": ("-c", "--command"), "sudo": ("-s", "--shell", "-i", "--login")}
# The gh binary found by a lookup, as a substitution spells it.
_WHICH_GH = re.compile(r"\b(?:which|command\s+-[vV]|type\s+-[aptP]+|whence|whereis)\s+[\"']?"
                       r"gh(?![A-Za-z0-9_.-])")
# Programs that copy, link or install a file, so the gh binary given to one may come
# back under a name the check does not read as gh.
_COPIERS = frozenset(("cp", "ln", "install", "rsync", "mv", "tee", "cat", "ditto", "dd", "scp",
                      "gcp", "gln", "gmv", "ginstall"))
# gh's config directory under the names the shell gives it, the programs that write,
# move or enter what a later word names, and the in-place editors.
# A macOS file system ignores case, so ~/.CONFIG/GH is the same directory.
_GH_CONFIG = re.compile(r"(?:^|/)\.config/+gh(?:/|$)|GH_CONFIG_DIR|XDG_CONFIG_HOME\}?/+gh(?:/|$)",
                        re.IGNORECASE)
_CONFIG_WRITERS = frozenset(("tee", "cp", "mv", "install", "ln", "rsync", "dd", "touch",
                             "truncate", "ditto", "rm", "unlink", "chmod", "mkdir", "cd", "pushd",
                             "patch", "gcp", "gmv", "sed", "gsed", "perl"))
_IN_PLACE = frozenset(("sed", "gsed", "perl"))
_WRITE_REDIRECTS = (">", ">>", ">|", "&>", "&>>", "<>")
_BOOLS = ("--help", "-h", "--version")
# The jq filter flag glued to its value, the one word a pathname glob may stay in.
_JQ_FLAGS = ("--jq=", "-q=")
# Nesting deeper than this, or more gh programs in one command, is no command the
# pipeline runs, and such a command is refused rather than read at length.
_MAX_DEPTH = 16
_MAX_GH = 8


class _Unreadable(Exception):
    """The text cannot be split into words the way the shell splits it."""


class _Command(list):
    """A simple command's words, with its redirections as (operator, target word)."""

    def __init__(self, words=()):
        list.__init__(self, words)
        self.redirects = []


class _Word(object):
    """One shell word after quote removal, an expansion kept as written. `expanded`
    is set by any expansion, `loose` by one outside double quotes (it can split
    into several words), `globbed` by an unquoted glob character or a brace
    expansion, and `plain` while the word is unquoted text only. A pathname glob in
    a word that opens with a literal, unquoted --jq= or -q= leaves it unglobbed:
    whatever the shell makes of it keeps that prefix, so it stays the jq flag and
    never becomes a verb, a subcommand, a method or a route. `script` is the
    text another shell would read if handed the word, each expansion of this shell
    standing as `${_}` there (braced, so a letter after it stays its own), since its
    own commands are read already."""
    __slots__ = ("parts", "script_parts", "expanded", "loose", "globbed", "plain", "braces",
                 "wild", "jq")

    def __init__(self):
        self.parts = []
        self.script_parts = []
        self.expanded = False
        self.loose = False
        self.globbed = False
        self.plain = True
        self.braces = []
        self.wild = False
        self.jq = False

    @property
    def text(self):
        return "".join(self.parts)

    @property
    def script(self):
        return "".join(self.script_parts)

    def quoted(self, text):
        self.parts.append(text)
        self.script_parts.append(text)
        self.plain = False

    def bare(self, text):
        if not self.parts and text.startswith(_JQ_FLAGS):
            self.jq = True
        self.parts.append(text)
        self.script_parts.append(text)
        for match in re.finditer(r"[*?\[{},]|\.\.", text):
            token = match.group(0)
            if token in ("*", "?", "["):
                self.wild = True
            else:
                self.braces.append(token)

    def expansion(self, raw, loose):
        self.parts.append(raw)
        # a gh lookup stays as written, so a copy of the binary in a script is seen
        self.script_parts.append(raw if _WHICH_GH.search(raw) else "${_}")
        self.expanded = True
        self.loose = self.loose or loose
        self.plain = False

    def close(self):
        if self.wild and not self.jq:
            self.globbed = True
        # bash expands {a,b} and {1..3}, a lone {x} stays as it is, in any word
        stack = []
        for token in self.braces:
            if token == "{":
                stack.append(False)
            elif token == "}":
                if stack and stack.pop():
                    self.globbed = True
            elif stack:
                stack[-1] = True
        return self


class _Lexer(object):
    """Splits shell text into simple commands of words as bash does: quotes, escapes
    and line continuations removed, separators, redirections and comments taken
    out. A command or process substitution adds its own commands, and a
    here-document or here-string body is kept as text to read again."""

    # bash splits words on space, tab and newline only, so \f, \v and NBSP are word text
    _RUN = re.compile(r"[^ \t\r\n;&|()<>'\"\\$`]+")
    _PLAIN_DOUBLE = re.compile(r"[^\"\\$`]+")
    _NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-]")
    _REDIRECT = re.compile(r"&>>|&>|<<<|<<-|<<|<>|<&|<|>>|>&|>\||>")

    def __init__(self, text, depth):
        self.s = text
        self.n = len(text)
        self.i = 0
        self.depth = depth
        self.commands = []
        self.texts = []
        self.heredocs = []

    def run(self):
        self._list(False)
        return self

    def _list(self, closing):
        s = self.s
        words = _Command()
        parens = 0
        while True:
            if self.i >= self.n:
                if closing:
                    raise _Unreadable()
                self._emit(words)
                return
            c = s[self.i]
            if c in " \t\r":
                self.i += 1
            elif c == "\\" and s.startswith("\n", self.i + 1):
                self.i += 2
            elif c == "\n":
                words = self._emit(words)
                self.i += 1
                self._read_heredocs()
            elif c == "#":
                end = s.find("\n", self.i)
                self.i = self.n if end < 0 else end
            elif c == ")":
                words = self._emit(words)
                self.i += 1
                if parens:
                    parens -= 1
                elif closing:
                    return
                else:
                    raise _Unreadable()
            elif c == "(":
                words = self._emit(words)
                parens += 1
                self.i += 1
            elif (c in "<>" and not s.startswith("(", self.i + 1)) or s.startswith("&>", self.i):
                self._redirect(words)
            elif c in ";&|":
                words = self._emit(words)
                self.i += 1
            else:
                word = self._word()
                if (word.plain and word.text.isdigit() and self.i < self.n
                        and s[self.i] in "<>" and not s.startswith("(", self.i + 1)):
                    self._redirect(words)
                else:
                    words.append(word.close())

    def _emit(self, words):
        if words or words.redirects:
            self.commands.append(words)
        return _Command()

    def _redirect(self, words):
        op = self._REDIRECT.match(self.s, self.i).group(0)
        self.i += len(op)
        while self.i < self.n and self.s[self.i] in " \t":
            self.i += 1
        if self.i >= self.n or self.s[self.i] in "\n;&|()<>":
            if op in ("<<", "<<-", "<<<"):
                raise _Unreadable()
            return
        target = self._word().close()
        words.redirects.append((op, target))
        if op in ("<<", "<<-"):
            self.heredocs.append((target.text, op == "<<-"))
        elif op == "<<<":
            self.texts.append(target.text)

    def _read_heredocs(self):
        for delimiter, strip in self.heredocs:
            body = []
            while self.i < self.n:
                end = self.s.find("\n", self.i)
                line = self.s[self.i:] if end < 0 else self.s[self.i:end]
                self.i = self.n if end < 0 else end + 1
                if (line.lstrip("\t") if strip else line) == delimiter:
                    break
                body.append(line)
            self.texts.append("\n".join(body))
        self.heredocs = []

    def _word(self):
        s = self.s
        word = _Word()
        while self.i < self.n:
            c = s[self.i]
            if c in " \t\r\n;&|()":
                break
            if c in "<>":
                if not s.startswith("(", self.i + 1):
                    break
                start = self.i
                self.i += 2
                self._nested()
                word.expansion(s[start:self.i], True)
            elif c == "\\":
                if s.startswith("\n", self.i + 1):
                    self.i += 2
                else:
                    word.quoted(s[self.i + 1:self.i + 2] or "\\")
                    self.i += 2
            elif c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise _Unreadable()
                word.quoted(s[self.i + 1:end])
                self.i = end + 1
            elif c == '"':
                self._double(word)
            elif c == "$":
                self._dollar(word, False)
            elif c == "`":
                self._backtick(word, False)
            else:
                run = self._RUN.match(s, self.i).group(0)
                word.bare(run)
                self.i += len(run)
        return word

    def _double(self, word):
        s = self.s
        self.i += 1
        word.plain = False
        while True:
            if self.i >= self.n:
                raise _Unreadable()
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\":
                if self.i + 1 >= self.n:
                    raise _Unreadable()
                following = s[self.i + 1]
                if following != "\n":
                    word.quoted(following if following in '$`"\\' else "\\" + following)
                self.i += 2
            elif c == "$":
                self._dollar(word, True)
            elif c == "`":
                self._backtick(word, True)
            else:
                run = self._PLAIN_DOUBLE.match(s, self.i).group(0)
                word.quoted(run)
                self.i += len(run)

    def _dollar(self, word, quoted):
        s = self.s
        start = self.i
        following = s[self.i + 1:self.i + 2]
        if following == "(":
            self.i += 2
            self._nested()
        elif following == "{":
            self.i += 2
            self._parameter()
        elif following == "'" and not quoted:
            # ANSI-C quoting turns escapes into any character, read as an expansion
            end = self.i + 2
            while end < self.n and s[end] != "'":
                end += 2 if s[end] == "\\" else 1
            if end >= self.n:
                raise _Unreadable()
            self.i = end + 1
        elif following == '"' and not quoted:
            self.i += 1
            self._double(word)
            return
        else:
            name = self._NAME.match(s, self.i + 1)
            self.i += 1
            if not name:
                if quoted:
                    word.quoted("$")
                else:
                    word.bare("$")
                return
            self.i += len(name.group(0))
        word.expansion(s[start:self.i], not quoted)

    def _nested(self):
        if self.depth >= _MAX_DEPTH:
            raise _Unreadable()
        self.depth += 1
        self._list(True)
        self.depth -= 1

    def _parameter(self):
        s = self.s
        level = 1
        while level:
            if self.i >= self.n:
                raise _Unreadable()
            c = s[self.i]
            if c == "\\":
                self.i += 2
            elif c == "'":
                end = s.find("'", self.i + 1)
                if end < 0:
                    raise _Unreadable()
                self.i = end + 1
            elif c == '"':
                self._double(_Word())
            elif c == "$":
                self._dollar(_Word(), True)
            elif c == "`":
                self._backtick(_Word(), True)
            else:
                level += 1 if c == "{" else -1 if c == "}" else 0
                self.i += 1

    def _backtick(self, word, quoted):
        s = self.s
        start = self.i
        end = self.i + 1
        body = []
        while end < self.n and s[end] != "`":
            if s[end] == "\\" and end + 1 < self.n:
                body.append(s[end + 1] if s[end + 1] in "$`\\" else s[end:end + 2])
                end += 2
            else:
                body.append(s[end])
                end += 1
        if end >= self.n or self.depth >= _MAX_DEPTH:
            raise _Unreadable()
        self.i = end + 1
        inner = _Lexer("".join(body), self.depth + 1).run()
        self.commands.extend(inner.commands)
        self.texts.extend(inner.texts)
        word.expansion(s[start:self.i], not quoted)


def _variants(text):
    """The text and its percent-decoded forms, as a server may decode a path."""
    seen = [text]
    for _ in range(3):
        decoded = unquote(seen[-1])
        if decoded == seen[-1]:
            break
        seen.append(decoded)
    return seen


def _segments(text):
    """(as written, dot segments resolved) path segments of an API endpoint,
    lower-cased, without its scheme, host, /api/v3 prefix, query and fragment."""
    path = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*", "", text)
    path = re.sub(r"^api\.github\.com\.?(?::[0-9]+)?(?=/|$)", "", path, flags=re.IGNORECASE)
    path = re.split(r"[?#]", path, maxsplit=1)[0].lower()
    written = [part for part in path.split("/") if part]
    if written[:2] == ["api", "v3"]:
        written = written[2:]
    resolved = []
    for part in written:
        if part == "..":
            if resolved:
                resolved.pop()
        elif part != ".":
            resolved.append(part)
    return written, resolved


def _is_issues_route(text):
    for variant in _variants(text):
        for parts in _segments(variant):
            if parts[:1] == ["repos"] and "issues" in parts[3:]:
                return True
            if parts[:1] == ["repositories"] and "issues" in parts[2:]:
                return True
    return False


def _is_graphql(text):
    return any(parts and parts[-1] == "graphql"
               for variant in _variants(text) for parts in _segments(variant))


def _is_literal_pulls_route(word):
    """The review loop's reply route written out: no expansion, no glob, no dot
    segment, also once decoded."""
    if word.expanded or word.globbed:
        return False
    for variant in _variants(word.text):
        if any(part in (".", "..") for part in _segments(variant)[0]):
            return False
    written = _segments(word.text)[0]
    return written[:1] == ["repos"] and written[3:4] == ["pulls"] and "issues" not in written


def _names_api(text):
    return any(_API_URL.search(variant) for variant in _variants(text))


def _has_mutation(texts):
    """A mutation name in a GraphQL document, read past escapes, comments, case, and
    the whitespace or quotes that may split a name."""
    flat = re.sub(r"\\u([0-9a-fA-F]{4})", lambda match: chr(int(match.group(1), 16)),
                  "\n".join(texts))
    flat = re.sub(r"[\s,\\\"']+", "", re.sub(r"#[^\n\r]*", "", flat))
    return bool(_MUTATION.search(flat))


def _first_word(words, consume, bools=_BOOLS):
    """The index of the first word that is no flag, or None. With `consume`, a flag
    written without = as -x or --name takes the next word as its value, as cobra
    reads it while it looks for a subcommand, unless it is one of `bools`. A lone -
    or -- is read past."""
    index = 0
    while index < len(words):
        text = words[index].text
        if text in ("-", "--"):
            index += 1
        elif not text.startswith("-"):
            return index
        elif (consume and "=" not in text and (len(text) == 2 or text.startswith("--"))
              and text not in bools):
            index += 2
        else:
            index += 1
    return None


def _candidates(words, bools=_BOOLS):
    """The subcommand as cobra finds it, and the first word that is no flag."""
    found = []
    for consume in (True, False):
        index = _first_word(words, consume, bools)
        if index is not None and index not in found:
            found.append(index)
    return found


_API_LONG = {"method": "method", "field": "field", "raw-field": "field", "input": "input",
             "header": "value", "hostname": "value", "jq": "value", "template": "value",
             "preview": "value", "cache": "value", "include": "bool", "paginate": "bool",
             "silent": "bool", "slurp": "bool", "verbose": "bool", "help": "bool"}
_API_SHORT = {"X": "method", "f": "field", "F": "field", "H": "value", "p": "value",
              "q": "value", "t": "value"}


def _api_long_kind(name):
    if name in _API_LONG:
        return _API_LONG[name]
    # gh refuses an abbreviation, read here as the flag it abbreviates all the same
    for full in ("method", "input", "field", "raw-field"):
        if len(name) >= 2 and full.startswith(name):
            return _API_LONG[full]
    return "value"


def _api_flags(words):
    """(positional words, methods as (value, expanded), field values, input given) of
    a gh api call, read as gh's flag parser reads them. A short cluster holding f, F
    or X before its first non-letter also counts as that flag."""
    positionals = []
    methods = []
    fields = []
    has_input = False
    index = 0
    after_dashes = False
    while index < len(words):
        word = words[index]
        text = word.text
        following = words[index + 1] if index + 1 < len(words) else None
        if after_dashes or text == "-" or not text.startswith("-"):
            positionals.append(word)
        elif text == "--":
            after_dashes = True
        elif text.startswith("--"):
            name, equals, value = text[2:].partition("=")
            kind = _api_long_kind(name)
            expanded = word.expanded
            if kind != "bool" and not equals:
                value = following.text if following else ""
                expanded = expanded or bool(following and following.expanded)
                index += 1
            if kind == "method":
                methods.append((value, expanded))
            elif kind == "field":
                fields.append(value)
            elif kind == "input":
                has_input = True
        else:
            letters = text[1:]
            kind = None
            for position, letter in enumerate(letters):
                kind = _API_SHORT.get(letter)
                if kind is None:
                    continue
                value = letters[position + 1:]
                value = value[1:] if value.startswith("=") else value
                expanded = word.expanded
                if not value:
                    value = following.text if following else ""
                    expanded = expanded or bool(following and following.expanded)
                    index += 1
                if kind == "method":
                    methods.append((value, expanded))
                elif kind == "field":
                    fields.append(value)
                break
            head = re.match(r"[A-Za-z]*", letters).group(0)
            if kind != "field" and ("f" in head or "F" in head):
                fields.append(letters)
            if kind != "method" and "X" in head:
                methods.append(("", True))
        index += 1
    return positionals, methods, fields, has_input


def _check_api(words):
    positionals, methods, fields, has_input = _api_flags(words)
    reads = [word for word in words if not word.text.startswith("-")]
    route = any(_is_issues_route(word.text) for word in reads)
    graphql = any(_is_graphql(word.text) for word in reads)
    gets = [value for value, expanded in methods
            if not expanded and value.strip().upper() in ("GET", "HEAD")]
    if route and (len(gets) < len(methods) or ((fields or has_input) and not gets)):
        return "gh api writes an issues route with a method other than GET or a body"
    if graphql and _has_mutation([word.text for word in words]):
        return "gh api sends a GraphQL issue mutation"
    if graphql and (has_input or any("@" in value for value in fields)):
        return "gh api sends a GraphQL document from a file or stdin, which cannot be read"
    if any(word.expanded for word in words):
        endpoint = positionals[0] if positionals else None
        if (endpoint is None or not _is_literal_pulls_route(endpoint)
                or any(word.loose for word in words)):
            return ("an expansion in a gh api call can hide the endpoint or a flag, write the "
                    "pull request route and every flag out")
    return None


def _check_issue(words):
    if any(word.expanded for word in words):
        return "an expansion in a gh issue call can become any verb"
    for index in _candidates(words, ("--help", "-h")):
        # gh refuses `CLOSE`, `close5` and `'close 5'`, read as the verb all the same
        verb = re.match(r"[a-z-]*", words[index].text.strip().lower()).group(0)
        if verb in WRITE_VERBS:
            return "gh issue %s writes the issue" % verb
    return None


def _check_gh(argv, run, appended=False):
    """Why a gh call with arguments `argv` may write the tracker, or None. `run` is
    False for text that may be prose, where an unknown subcommand is no evidence.
    `appended` is True when xargs, parallel or find -exec may add arguments."""
    globbed = any(word.globbed for word in argv)
    if run and globbed:
        return "a glob or brace expansion in a gh call can become any word"
    cobra = _first_word(argv, True)
    if appended:
        sub = argv[cobra].text if cobra is not None else ""
        verb = _first_word(argv[cobra + 1:], True, ("--help", "-h")) if cobra is not None else None
        if sub in _SENSITIVE and not (sub == "issue" and verb is not None and argv[
                cobra + 1 + verb].text in ("view", "list", "ls", "status")):
            return "xargs, parallel or find -exec can add the words that make gh %s write" % (
                sub)
        if not sub:
            return "xargs, parallel or find -exec can add any gh command"
    for index in _candidates(argv):
        sub = argv[index]
        name = sub.text
        if sub.expanded or (run and sub.globbed):
            return "the gh subcommand is an expansion or a glob, which cannot be read"
        if globbed and name in _SENSITIVE:
            return "a glob or brace expansion in a gh call can become any word"
        rest = argv[index + 1:]
        first = _first_word(rest, False)
        verb = rest[first].text if first is not None else ""
        reason = None
        if run and index == cobra and name not in BUILTINS:
            reason = "gh %s is no gh command, an alias or an extension can run any" % name
        elif name == "issue":
            reason = _check_issue(rest)
        elif name == "api":
            reason = _check_api(argv[:index] + rest)
        elif name == "copilot":
            reason = "gh copilot runs an agent that can write the tracker"
        elif name == "config" and verb == "set":
            reason = "gh config set can turn a read into a write"
        elif name == "alias" and verb in ("set", "import"):
            reason = "gh alias %s makes a name gh runs as any command" % verb
        elif name in ("extension", "extensions", "ext") and verb in ("install", "exec"):
            reason = "gh extension %s runs code that can write the tracker" % verb
        elif name == "auth" and (verb == "token" or (verb == "status" and any(
                word.text.startswith("--show-token") or re.match(r"-[A-Za-z]*t", word.text)
                for word in rest))):
            reason = "the gh token would let any client write the tracker"
        if reason:
            return reason
    return None


def _is_gh_program(words, index):
    word = words[index]
    if not word.expanded and not word.globbed:
        # a case-insensitive file system runs GH as gh
        name = word.text.lower()
        return name == "gh" or name.endswith("/gh")
    if word.expanded and _WHICH_GH.search(word.text):
        return True
    following = words[index + 1].text if index + 1 < len(words) else ""
    return following in _SENSITIVE


# An expansion the shell splits on whitespace, so `gh${IFS}issue` is two words.
_SPLITTING = re.compile(r"\$\{[^}]*\}|\$\([^)]*\)|`[^`]*`|\$[A-Za-z_][A-Za-z0-9_]*"
                        r"|\$[0-9@*#?$!-]")
_APPENDERS = frozenset(("xargs", "parallel", "-exec", "-execdir", "-ok", "-okdir"))
# Reserved words and assignments before the program, and programs that run another.
_RESERVED = frozenset(("if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"))
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_RUNNERS = frozenset(("sudo", "doas", "env", "command", "exec", "builtin", "nohup", "nice",
                      "ionice", "timeout", "gtimeout", "stdbuf", "xargs", "parallel", "watch",
                      "time", "caffeinate", "setsid", "flock", "chronic", "unbuffer", "strace",
                      "script", "find", "ssh", "su", "runuser"))


def _head(words):
    """The index of the command's program word, past assignments and reserved words."""
    for index, word in enumerate(words):
        if word.text in _RESERVED or (_ASSIGNMENT.match(word.text) and not word.globbed):
            continue
        return index
    return None


def _program(words, names):
    """The index of the word among `names` that the command runs: its program, or a
    later word when the program is a runner such as sudo. None if it runs none."""
    head = _head(words)
    if head is None:
        return None
    for index in range(head, len(words)):
        if words[index].text.rsplit("/", 1)[-1] in names:
            return index
        if index == head and words[head].text.rsplit("/", 1)[-1] not in _RUNNERS:
            return None
    return None


def _names_gh_binary(word):
    if word.expanded and _WHICH_GH.search(word.text):
        return True
    name = word.text.rsplit("=", 1)[-1].lower()
    return name == "gh" or name.endswith("/gh")


def _check_files(words):
    """Why a command copies the gh binary or writes gh's config, or None."""
    copier = _program(words, _COPIERS)
    if copier is not None and (any(_names_gh_binary(word) for word in words[copier + 1:]) or any(
            op in ("<", "<>") and _names_gh_binary(target) for op, target in words.redirects)):
        return "copying the gh binary hides it from the tracker gate"
    if any(op in _WRITE_REDIRECTS and _GH_CONFIG.search(target.text)
           for op, target in words.redirects):
        return "a write to gh's config can turn a read into a write"
    writer = _program(words, _CONFIG_WRITERS)
    if writer is not None:
        name = words[writer].text.rsplit("/", 1)[-1]
        rest = words[writer + 1:]
        # every operand of a copy may be its target (a trailing flag, -t DIR, -tDIR), so a
        # copy out of the config is denied too
        if name in _IN_PLACE and not any(
                re.match(r"-[A-Za-z]*i|--in-place", word.text) for word in rest):
            targets = []
        else:
            targets = rest
        if any(_GH_CONFIG.search(word.text) for word in targets):
            return "a write to gh's config can turn a read into a write"
    return None


def _script_runner(words):
    """True when the command hands a word of its own to a shell as a script."""
    for word in words:
        name = word.text.rsplit("/", 1)[-1]
        if name in _SHELLS:
            return True
        for flag in _SCRIPT_FLAGS.get(name, ()):
            if any(other.text == flag or other.text.startswith(flag + "=") or (
                    len(flag) == 2 and other.text.startswith(flag)) for other in words):
                return True
    return False


def _split_words(text):
    words = []
    for piece in text.split():
        word = _Word()
        word.bare(piece)
        words.append(word.close())
    return words


def _check_command(words, run, depth, respace=True):
    programs = [index for index in range(len(words)) if _is_gh_program(words, index)]
    if len(programs) > _MAX_GH:
        return "a command naming gh more than %d times cannot be read" % _MAX_GH
    head = _head(words)
    runner = head is not None and words[head].text.rsplit("/", 1)[-1] in _RUNNERS
    in_gh = set()
    for index in programs:
        # gh is the program the shell runs, or the first gh after a runner such as
        # sudo or xargs, else a word like `grep gh .` names it
        program = run and (index == head or (runner and index == programs[0]))
        appended = any(word.text.rsplit("/", 1)[-1] in _APPENDERS for word in words[:index])
        reason = _check_gh(words[index + 1:], program, appended)
        if reason:
            return reason
        in_gh.update(id(word) for word in words[index:])
    for index, word in enumerate(words):
        if respace and word.loose:
            respaced = _SPLITTING.sub(" ", word.text)
            if respaced != word.text and _GH_NAME.search(respaced.lower()):
                # the first such word only, so the words after it are read once more
                respace = False
                reason = _check_command(words[:index] + _split_words(respaced)
                                        + words[index + 1:], run, depth, False)
                if reason:
                    return reason
    if run:
        reason = _check_files(_command_of(words))
        if reason:
            return reason
    script = run and _script_runner(words)
    for word in words:
        if id(word) not in in_gh and _names_api(word.text):
            return "a client other than gh names a GitHub issues or GraphQL endpoint"
        texts = [word.script]
        glued = re.match(r"(?:-[A-Za-z]|--[A-Za-z-]+=)(.+)", word.script, re.DOTALL)
        if script and glued:
            # the script glued to its flag, `env -S'...'` or `--split-string=...`
            texts.append(glued.group(1))
        for text in texts:
            if _GH_NAME.search(text) and _SHELL_TEXT.search(text):
                reason = _check_text(text, script, depth + 1)
                if reason:
                    return reason
    return None


def _command_of(words):
    if isinstance(words, _Command):
        return words
    command = _Command(words)
    return command


# The texts read during one search with their answers, keyed by depth too, since a
# reading past _MAX_DEPTH falls back to a looser one. A text met again while it is
# being read (a word whose script is the word itself) answers None: the reading in
# progress, at a shallower depth, gives the real answer.
_SEEN = {}
_READING = "reading"


def _check_text(text, run, depth):
    """Why `text` may write the tracker, or None. `run` is True for a command the
    shell runs and False for a word or a here-document body, prose or a script."""
    key = (text, run, depth)
    if key in _SEEN:
        return _SEEN[key]
    if _SEEN.get((text, run, _READING)):
        return None
    _SEEN[(text, run, _READING)] = True
    try:
        reason = _read_text(text, run, depth)
    finally:
        del _SEEN[(text, run, _READING)]
    _SEEN[key] = reason
    return reason


def _read_text(text, run, depth):
    try:
        if depth > _MAX_DEPTH:
            raise _Unreadable()
        lexer = _Lexer(text, depth).run()
    except _Unreadable:
        if run:
            flat = re.sub(r"[\"'\\`]", "", text)
            if _GH_NAME.search(flat) or _names_api(flat):
                return "a command naming gh that the shell cannot split is refused"
            return None
        # prose or a script whose quotes do not pair: read it again without them
        loosened = re.sub(r"[\"'`]", " ", text)
        if loosened != text and depth <= _MAX_DEPTH:
            return _check_text(loosened, False, depth + 1)
        return _loose_gh_write(text)
    for words in lexer.commands:
        reason = _check_command(words, run, depth)
        if reason:
            return reason
    for body in lexer.texts:
        reason = _check_text(body, False, depth + 1)
        if reason:
            return reason
    return None


def _loose_gh_write(text):
    """For text no split reads: a gh word followed by an issue write verb or a
    sensitive subcommand."""
    tokens = re.findall(r"[^\s;&|()<>]+", text)
    for index, token in enumerate(tokens):
        if token == "gh" or token.endswith("/gh"):
            later = tokens[index + 1:]
            if any(item in _SENSITIVE - {"issue"} for item in later) or (
                    "issue" in later and any(item in WRITE_VERBS for item in later)):
                return "a gh write in text the shell cannot split is refused"
    return None


class _Hit(object):
    def __init__(self, reason):
        self.reason = reason

    def group(self, index=0):
        return self.reason


class _WriteCheck(object):
    """The tracker write check, with the search(command) method of a compiled
    pattern so the guard runs it as it runs the forge's patterns."""
    pattern = "github-issues writes"

    def search(self, command):
        if not isinstance(command, str):
            return None
        _SEEN.clear()
        try:
            reason = _check_text(command, True, 0)
        except Exception:
            # a defect here must not let the call through: read the command as one the
            # shell cannot split
            flat = re.sub(r"[\"'\\`]", "", command)
            reason = ("the tracker write check failed on a command naming gh"
                      if _GH_NAME.search(flat) or _names_api(flat) else None)
        finally:
            _SEEN.clear()
        return _Hit(reason) if reason else None


_WRITES = _WriteCheck()


def write_reason(command):
    """Why the Bash `command` may write GitHub Issues, or None."""
    hit = _WRITES.search(command)
    return hit.reason if hit else None


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
    """The GitHub MCP server's issue write tools under any mcp__<server>__ prefix."""
    return _MCP_WRITE


def mcp_read_tools(table):
    return []


def write_denials(table):
    """The Bash write checks, each with search(command) like a compiled pattern. They
    match a write to any repo, not only the one the table names."""
    return [_WRITES]


def issue_number(task_id):
    """The issue number of a task id shaped <PREFIX>-<number>, such as 123 for GH-123."""
    match = _TASK_ID.fullmatch(task_id) if isinstance(task_id, str) else None
    if not match:
        raise PplError("SCHEMA_INVALID", "task id %r is not <PREFIX>-<issue number>, such as "
                       "GH-123" % (task_id,))
    return int(match.group(1))

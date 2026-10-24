"""Where an edit tool may write in a pipeline session. Claude Code's Edit, Write, MultiEdit
and NotebookEdit run outside the OS sandbox, so while the guard facts exist they write only
where sandboxed Bash may (the facts' write_scope) and in Claude Code's own projects and plans
directories, and never, even there, a .git directory or file or a file an unsandboxed gh, git
or ppl reads or runs. Without the facts nothing here applies. escape.edit_denial
decides whether a call is the pipeline's and asks this module about each path it names. The
guard hook imports it, so it parses on Python 3.9, carries no annotations and reads JSON
only."""
import os
import stat
import unicodedata

from . import paths

try:
    import pwd
except ImportError:
    # no passwd database off POSIX, where HOME is the only home there is
    pwd = None

# Its own copy of os_sandbox.FACTS_FILE, so the hook never imports that module.
FACTS_FILE = "os_sandbox.json"
PIN_NAMES = ("gh", "git", "ppl")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
PATH_KEYS = ("file_path", "path", "notebook_path")

# What an unsandboxed git, ssh or gh reads under a home directory, gh's extensions included.
_HOME_CONFIG = (".gitconfig", ".config/git", ".ssh", ".config/gh", ".local/share/gh")
# Where git and gh read the same instead when the environment moves it: the variable, and
# the part under its value.
_MOVED_CONFIG = (("XDG_CONFIG_HOME", "git"), ("XDG_CONFIG_HOME", "gh"), ("XDG_DATA_HOME", "gh"),
                 ("GH_CONFIG_DIR", ""), ("GIT_CONFIG_GLOBAL", ""))
# Claude Code's settings, which hold the hooks, the permissions and the sandbox, in its
# config directory.
_CLAUDE_SETTINGS = ("settings.json", "settings.local.json")
# The part of Claude Code's config directory an edit tool writes in every session: memory
# and transcripts under projects, plans under plans.
_CLAUDE_WRITABLE = ("projects", "plans")
# What a workspace session loads from the workspace: the sandbox profile and hooks under
# .claude, and its MCP servers.
_WORKSPACE_FILES = (".claude", ".mcp.json")
# What an unsandboxed ppl reads under PPL_HOME, and the guard's own facts.
_PPL_FILES = ("config.toml", "harnesses", FACTS_FILE)
# What JavaScript's trim removes besides the Zs spaces: ECMAScript's other WhiteSpace and
# its LineTerminators. Claude Code trims a path with it before it resolves it.
_JS_TRIMMED = frozenset(chr(code) for code in (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x2028, 0x2029,
                                               0xFEFF))

PROTECTED_WHY = "which a command outside the OS sandbox reads or runs"
OUTSIDE_SCOPE = "writes only where sandboxed Bash may"
GIT_DIR = "a .git directory or the .git file of a worktree"


def _homes():
    """The home directory HOME names and the one the passwd database records, which ssh
    reads whatever HOME says."""
    homes = [os.path.expanduser("~")]
    if pwd is not None:
        try:
            homes.append(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError):
            pass
    return homes


def _claude_dirs():
    """Claude Code's config directories: .claude under each home, and an absolute
    PPL_CLAUDE_HOME or CLAUDE_CONFIG_DIR."""
    dirs = [os.path.join(user_home, ".claude") for user_home in _homes()]
    for variable in ("PPL_CLAUDE_HOME", "CLAUDE_CONFIG_DIR"):
        value = os.environ.get(variable)
        if value and os.path.isabs(value):
            dirs.append(value)
    return dirs


def _claude_config():
    """Claude Code's settings files in each config directory, and its .claude.json: beside
    each home's .claude, beside PPL_CLAUDE_HOME as ppl doctor reads it, and inside
    CLAUDE_CONFIG_DIR as Claude Code reads it."""
    entries = [os.path.join(directory, name) for directory in _claude_dirs()
               for name in _CLAUDE_SETTINGS]
    entries.extend(os.path.join(user_home, ".claude.json") for user_home in _homes())
    for variable, beside in (("PPL_CLAUDE_HOME", True), ("CLAUDE_CONFIG_DIR", False)):
        value = os.environ.get(variable)
        if value and os.path.isabs(value):
            entries.append(os.path.join(os.path.dirname(value.rstrip("/")) if beside else value,
                                        ".claude.json"))
    return entries


def _ppl_code():
    """ppl's own code: the package this module is part of, the one the guard imports, and
    the assets directory, which holds the hook scripts. A PPL_ROOT that paths.root() refuses
    is still listed."""
    entries = [os.path.dirname(os.path.abspath(__file__))]
    try:
        entries.append(str(paths.root()))
    except Exception:
        configured = os.environ.get("PPL_ROOT")
        if configured:
            entries.append(configured)
    return entries


def _unique_real(entries):
    found = []
    for entry in entries:
        real = os.path.realpath(entry)
        if real not in found:
            found.append(real)
    return found


def protected_paths(home, facts):
    """Every path an edit tool may not write in a pipeline session, `realpath`-ed: the git,
    ssh and gh config under the home directory and wherever an absolute XDG_CONFIG_HOME,
    XDG_DATA_HOME, GH_CONFIG_DIR or GIT_CONFIG_GLOBAL in this environment moves it, Claude
    Code's settings and .claude.json (_claude_config), the workspace's .claude directory and
    .mcp.json, ppl's config, harnesses and guard facts under `home`, ppl's own code
    (_ppl_code), and each pin's path and realpath. With `facts` None, the workspace and the
    pins are not known and not listed."""
    entries = [os.path.join(user_home, rel) for user_home in _homes() for rel in _HOME_CONFIG]
    for variable, rel in _MOVED_CONFIG:
        value = os.environ.get(variable)
        if value and os.path.isabs(value):
            entries.append(os.path.join(value, rel) if rel else value)
    entries.extend(_claude_config())
    if facts:
        entries.extend(os.path.join(facts["workspace"], name) for name in _WORKSPACE_FILES)
    entries.extend(os.path.join(str(home), name) for name in _PPL_FILES)
    entries.extend(_ppl_code())
    if facts:
        for name in PIN_NAMES:
            pin = facts["pins"][name]
            entries.extend((pin["path"], pin["realpath"]))
    return _unique_real(entries)


def allowed_roots(facts):
    """Where an edit tool may write while the guard facts exist, `realpath`-ed: each entry of
    the facts' write_scope, where sandboxed Bash may write too, and the projects and plans
    directories of each Claude Code config directory."""
    entries = list(facts["write_scope"])
    entries.extend(os.path.join(directory, name) for directory in _claude_dirs()
                   for name in _CLAUDE_WRITABLE)
    return _unique_real(entries)


def _fold(path):
    """`path` as a case-insensitive file system compares it, so a variant in letter case or
    Unicode form is the same path."""
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def _within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _unclear(raw):
    """True when `raw` starts or ends with what JavaScript's trim removes, which Claude Code
    drops before it resolves a path, or holds a control character anywhere."""
    for char in (raw[:1], raw[-1:]):
        if char and (char in _JS_TRIMMED or unicodedata.category(char) == "Zs"):
            return True
    return any(unicodedata.category(char) == "Cc" for char in raw)


def _target_forms(raw, cwd):
    """(every form, the real forms) of the absolute paths an edit of `raw` may land on: `~`
    expanded or taken as written, joined to the session cwd, `..` collapsed in the text or by
    the kernel after a link, and through every link, a missing tail kept as spelled. The real
    forms are the ones through every link, where the write lands. None when `raw` is relative
    and the call names no absolute cwd."""
    forms, real = [], []
    for spelling in (raw, os.path.expanduser(raw)):
        if not os.path.isabs(spelling):
            if not (isinstance(cwd, str) and os.path.isabs(cwd)):
                return None
            spelling = os.path.join(cwd, spelling)
        for form, is_real in ((os.path.normpath(spelling), False),
                              (os.path.realpath(spelling), True),
                              (os.path.realpath(os.path.normpath(spelling)), True)):
            if form not in forms:
                forms.append(form)
            if is_real and form not in real:
                real.append(form)
    return forms, real


def _identity(path):
    try:
        info = os.stat(path)
    except (OSError, ValueError):
        return None
    return (info.st_dev, info.st_ino)


def _anchored(path, fold):
    """Each (device and inode, parts below it) for `path`, from the nearest existing entry at
    or above it up to the root: the path as the file system knows it whatever name reached
    it, with the part that does not exist yet kept as spelled, case folded with `fold`."""
    below = []
    current = path
    while True:
        identity = _identity(current)
        if identity is not None:
            yield identity, tuple(reversed(below))
        parent, name = os.path.split(current)
        if parent == current:
            return
        below.append(_fold(name) if fold else name)
        current = parent


def _anchors(entries, fold):
    found = []
    for entry in entries:
        nearest = next(_anchored(entry, fold), None)
        if nearest:
            found.append((entry,) + nearest)
    return found


def _lies_under(form, anchors, fold):
    """The entry of `anchors` that `form` is or lies under by device and inode: an entry above
    the form is the anchor's nearest existing entry, and the parts below match."""
    for identity, below in _anchored(form, fold):
        for entry, entry_identity, entry_below in anchors:
            if identity == entry_identity and below[:len(entry_below)] == entry_below:
                return entry
    return None


def _protected_hit(forms, protected):
    """The protected path one of `forms` is or lies under, or None. Compared as text after
    case folding, then by device and inode, which finds a hard link of a protected file, a
    case variant and another name of a directory (a macOS firmlink) even when the protected
    path does not exist yet. A hard link of a file inside a protected directory is refused by
    the link count instead (_multi_linked)."""
    folded = [(entry, _fold(entry)) for entry in protected]
    anchors = _anchors(protected, True)
    for form in forms:
        key = _fold(form)
        for entry, entry_key in folded:
            if _within(key, entry_key):
                return entry
        hit = _lies_under(form, anchors, True)
        if hit:
            return hit
    return None


def _git_part(forms):
    """The first of `forms` with a .git component in any letter case, or None."""
    for form in forms:
        if any(_fold(part) == ".git" for part in form.split("/")):
            return form
    return None


def _outside(real_forms, roots):
    """The first of `real_forms` that lies under none of `roots`, or None. Compared exactly as
    text, then by device and inode with the parts below kept in their case, so a case variant
    counts as inside only where the file system says it is the same entry."""
    anchors = _anchors(roots, False)
    for form in real_forms:
        if any(_within(form, root) for root in roots):
            continue
        if _lies_under(form, anchors, False):
            continue
        return form
    return None


def _multi_linked(real_forms):
    """The first of `real_forms` that is a regular file with more than one link, or None."""
    for form in real_forms:
        try:
            info = os.stat(form)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return form
    return None


def _shown(value):
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def target_denial(raw, cwd, home, facts, who, tool):
    """Why `who`'s `tool` call may not write `raw`, or None. The check holds only under an OS
    sandbox profile, while `facts` is there: a harness without one keeps today's behaviour.
    A path is refused when it starts or ends with whitespace or holds a control character,
    when it cannot be placed, when it is or lies under a protected path, outside the allowed
    roots, in a .git directory or file, and as a file with more than one link, which may be
    another name of a file outside them."""
    if facts is None:
        return None
    if not isinstance(raw, str):
        return ("%s's %s names a path that is not a string, %s, so the guard cannot tell where "
                "it writes" % (who, tool, _shown(raw)))
    if _unclear(raw):
        return ("%s's %s path %s starts or ends with whitespace or holds a control character, "
                "which Claude Code may trim or read otherwise than the guard: spell the path "
                "plainly" % (who, tool, _shown(raw)))
    resolved = _target_forms(raw, cwd)
    if resolved is None:
        return ("%s's %s names the relative path %s and no absolute cwd, so the guard cannot "
                "tell where it writes" % (who, tool, _shown(raw)))
    forms, real_forms = resolved
    hit = _protected_hit(forms, protected_paths(home, facts))
    if hit:
        return ("%s may not %s %s while the pipeline is set up or running: it is or lies under "
                "%s, %s" % (who, tool, _shown(raw), hit, PROTECTED_WHY))
    roots = allowed_roots(facts)
    outside = _outside(real_forms, roots)
    if outside:
        return ("%s may not %s %s while the pipeline is set up or running: an edit tool runs "
                "outside the OS sandbox, so it %s and in Claude Code's projects and plans "
                "directories (%s), and %s lies in none of them"
                % (who, tool, _shown(raw), OUTSIDE_SCOPE, ", ".join(roots), outside))
    git = _git_part(forms)
    if git:
        return ("%s may not %s %s while the pipeline is set up or running: %s is %s or lies in "
                "one, whose config, hooks and gitdir pointer steer the orchestrator's git"
                % (who, tool, _shown(raw), git, GIT_DIR))
    linked = _multi_linked(real_forms)
    if linked:
        return ("%s may not %s %s while the pipeline is set up or running: %s is a file with "
                "more than one link, which may be another name of a file outside the write "
                "scope" % (who, tool, _shown(raw), linked))
    return None

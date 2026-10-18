import os
import re
import stat
import subprocess
import time

from .errors import PplError


def _git(repo, *args):
    # E1/E4, cycle 2 wave E: a repo path that does not exist as a directory
    # at all is not "GitLab, Jira or CI is unavailable" - the closed
    # vocabulary's EXTERNAL_BLOCKED remedy (references/escalation.md: "park,
    # retry on the next tick") never creates the missing worktree, so an
    # orchestrator that follows the documented remedy loops forever on a
    # permanent setup defect. This is the same shape as `ppl init` on a task
    # nobody ran `ppl init` for: a precondition was never satisfied, and the
    # fix is to satisfy it (run Stage 1 and create the worktree), not to
    # wait. Raised as SCHEMA_INVALID instead, whose documented remedy ("fix
    # the artifact or the invocation and retry once") is the right action.
    # A repo directory that DOES exist but where git itself fails (a lock
    # file, a corrupt object, a real permissions problem) is left as
    # EXTERNAL_BLOCKED below - that is a genuinely external condition
    # retrying can plausibly clear.
    if not os.path.isdir(str(repo)):
        raise PplError("SCHEMA_INVALID",
                       "repo %s does not exist. If this run's worktree has not been "
                       "created yet, create it now (see Stage 1, the skill's "
                       "references/stage-1-baseline.md) and retry - "
                       "this is not something to wait out." % repo)
    result = subprocess.run(["git", "-C", str(repo)] + list(args),
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise PplError("EXTERNAL_BLOCKED",
                       "git %s failed in %s: %s" % (" ".join(args), repo, result.stderr.strip()))
    return result.stdout.strip()


def is_repo(repo):
    try:
        return _git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    except PplError:
        return False


def head_sha(repo):
    return _git(repo, "rev-parse", "HEAD")


def branch(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def changed_paths(repo, base, head):
    # A falsy base or head (an unrecorded base_sha, say) would otherwise diff
    # to an empty list, indistinguishable from "no changes" and letting the
    # done-gate's boundary check pass vacuously.
    # A missing base or head is a bad input document (e.g. a run whose
    # base_sha was never recorded), not a broken external system - the same
    # reasoning is_ancestor gives below for why its own two-revisions check
    # is SCHEMA_INVALID rather than EXTERNAL_BLOCKED. E1, cycle 2 wave E:
    # this used to be EXTERNAL_BLOCKED, whose documented remedy ("park,
    # retry on the next tick") does nothing for a base_sha that was simply
    # never written.
    if not base or not head:
        raise PplError("SCHEMA_INVALID",
                       "changed_paths needs both a base and a head revision, got base=%r head=%r"
                       % (base, head))
    out = _git(repo, "diff", "--name-only", "%s..%s" % (base, head))
    return [line for line in out.splitlines() if line]


def is_ancestor(repo, maybe_ancestor, head):
    """True when maybe_ancestor is reachable from head. A commit is its own ancestor.

    git merge-base --is-ancestor answers with its exit code: 0 yes, 1 no, and
    anything else is an error - in practice a revision that is not a valid
    object. That is a bad input document rather than a broken repository, since
    a repository this process cannot use has already failed in head_sha, so it
    is raised as SCHEMA_INVALID and not as EXTERNAL_BLOCKED.
    """
    if not maybe_ancestor or not head:
        raise PplError("SCHEMA_INVALID",
                       "is_ancestor needs two revisions, got %r and %r"
                       % (maybe_ancestor, head))
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor",
         str(maybe_ancestor), str(head)],
        capture_output=True, text=True)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise PplError("SCHEMA_INVALID",
                   "cannot compare %s with %s in %s: %s"
                   % (maybe_ancestor, head, repo, result.stderr.strip()))


UNTRACKED_FILE_LIMIT = 1024 * 1024
_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_C_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _header_path(text):
    """The path in a +++ header as git prints it: the tab git appends when the
    path holds a space dropped, and a C-quoted path (a double quote, a
    backslash, a control character) decoded to its bytes."""
    text = text.rstrip("\t")
    if len(text) < 2 or text[0] != '"' or text[-1] != '"':
        return text
    body, raw, i = text[1:-1], bytearray(), 0
    while i < len(body):
        if body[i] != "\\":
            raw.extend(body[i].encode("utf-8"))
            i += 1
        elif body[i + 1:i + 2] in _C_ESCAPES:
            raw.append(_C_ESCAPES[body[i + 1]])
            i += 2
        elif body[i + 1:i + 2].isdigit():
            raw.append(int(body[i + 1:i + 4], 8) & 0xFF)
            i += 4
        else:
            raw.extend(body[i + 1:i + 2].encode("utf-8"))
            i += 2
    return raw.decode("utf-8", errors="replace")


def _added_in_diff(patch):
    """{path: added lines} from a -U0 patch. Inside a hunk every line is content,
    whatever it starts with, until the hunk's own old and new line counts run
    out: an added line "++ x" prints as "+++ x" and must not be read as the
    next file's header."""
    added, current, old_left, new_left = {}, None, 0, 0
    for line in patch.splitlines():
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                if current is not None:
                    added.setdefault(current, []).append(line[1:])
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            elif line.startswith(" "):
                old_left -= 1
                new_left -= 1
            elif not line.startswith("\\"):
                old_left = new_left = 0
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
        elif line.startswith("+++ "):
            name = _header_path(line[4:])
            current = name[2:] if name.startswith("b/") else None
    return added


def added_lines(repo, base, timeout=2.0):
    """The lines the working tree adds since base, as {repo-relative path: [lines]}.

    Committed, staged, unstaged and untracked changes all count: a new file the
    code worker has not added yet is as much the implementation as a committed
    one. One deadline covers every git call and the untracked reads, so a caller
    on a hook budget gets an answer or an error in bounded time. Only regular
    files are read, never through a symlink, and at most UNTRACKED_FILE_LIMIT
    bytes of each. A missing base is a bad input document (SCHEMA_INVALID, as in
    changed_paths), and a git that fails, a file that cannot be read, or a run
    past the deadline is EXTERNAL_BLOCKED.
    """
    if not base:
        raise PplError("SCHEMA_INVALID", "added_lines needs a base revision, got %r" % (base,))
    deadline = time.monotonic() + timeout

    def left():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PplError("EXTERNAL_BLOCKED",
                           "reading the changes in %s took longer than %.1f s" % (repo, timeout))
        return remaining

    def run(*args):
        try:
            result = subprocess.run(["git", "-C", str(repo), "-c", "core.quotePath=false"]
                                    + list(args), capture_output=True, text=True,
                                    errors="replace", timeout=left())
        except subprocess.TimeoutExpired:
            raise PplError("EXTERNAL_BLOCKED",
                           "git %s in %s took longer than %.1f s" % (args[0], repo, timeout))
        except OSError as err:
            raise PplError("EXTERNAL_BLOCKED", "git %s in %s: %s" % (args[0], repo, err))
        if result.returncode != 0:
            raise PplError("EXTERNAL_BLOCKED", "git %s failed in %s: %s"
                           % (args[0], repo, result.stderr.strip()))
        return result.stdout

    added = _added_in_diff(run("diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames",
                               str(base), "--"))
    for rel in run("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if not rel:
            continue
        left()
        target = os.path.join(str(repo), rel)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            continue
        except OSError as err:
            raise PplError("EXTERNAL_BLOCKED", "cannot stat %s: %s" % (rel, err))
        if not stat.S_ISREG(info.st_mode):
            continue
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            with os.fdopen(os.open(target, flags), "rb") as handle:
                data = handle.read(UNTRACKED_FILE_LIMIT)
        except OSError as err:
            raise PplError("EXTERNAL_BLOCKED", "cannot read %s: %s" % (rel, err))
        added.setdefault(rel, []).extend(data.decode("utf-8", errors="replace").splitlines())
    return added


def apply_patch(repo, diff_file):
    check = subprocess.run(["git", "-C", str(repo), "apply", "--check", str(diff_file)],
                           capture_output=True, text=True)
    if check.returncode != 0:
        raise PplError("IMPLEMENTATION_FAILURE",
                       "patch does not apply cleanly: %s" % check.stderr.strip())
    _git(repo, "apply", str(diff_file))

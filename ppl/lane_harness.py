"""The harness a run renders from: the committed state of the installed harness
repository, never its working copy. `commit` names the commit and `export` writes
that tree out, so what a lane runs is what the harness repository has committed."""
import io
import os
import posixpath
import subprocess
import tarfile
from pathlib import Path

from . import escape_git
from .errors import PplError

FETCH_TIMEOUT_SECONDS = 60
GIT_TIMEOUT_SECONDS = 30
_LISTED_PATHS = 10
# The import commit is the operator's own, signed when their config signs, so its gpg program
# stays theirs. The repository is one `git init` made a moment before, with no config of anyone
# else's to override.
_IMPORT_PINS = [word for pair in zip(escape_git.OPERATOR_PINNED_CONFIG[0::2],
                                     escape_git.OPERATOR_PINNED_CONFIG[1::2])
                if not pair[1].startswith("gpg.program=") for word in pair]


def git(directory, args, timeout=GIT_TIMEOUT_SECONDS, text=True, pins=None):
    """`git -C <directory> <args>` run outside any sandbox: the -c words of
    escape_git.OPERATOR_PINNED_CONFIG (or `pins`) first, no prompt, no replace refs
    (GIT_NO_REPLACE_OBJECTS, which git's child processes inherit as well), captured output and
    a timeout. EXTERNAL_BLOCKED when git cannot start or runs out of time."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_NO_REPLACE_OBJECTS="1")
    pinned = escape_git.OPERATOR_PINNED_CONFIG if pins is None else pins
    argv = ["git", *pinned, "-C", str(directory), *args]
    try:
        return subprocess.run(argv, capture_output=True, text=text, stdin=subprocess.DEVNULL,
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise PplError("EXTERNAL_BLOCKED", "git %s timed out after %d seconds in %s"
                       % (args[0], timeout, directory))
    except OSError as exc:
        raise PplError("EXTERNAL_BLOCKED", "git could not start: %s" % exc)


def stderr_text(result):
    err = result.stderr
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return err.strip()


def _not_a_repository(directory):
    name = Path(directory).name
    return PplError(
        "SETUP_INCOMPLETE",
        "harness %s at %s is not a git repository, so a run cannot render from a commit. "
        "Re-import it with `ppl harness add --source %s --name %s` (under another name if "
        "%s is taken), or commit it in place: `git -C %s init -b main && git -C %s add -A "
        "&& git -C %s commit -m \"Import %s\"`."
        % (name, directory, directory, name, name, directory, directory, directory, name))


def _is_repository_root(directory):
    result = git(directory, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        return False
    return os.path.realpath(result.stdout.strip()) == os.path.realpath(str(directory))


def import_directory(directory, name):
    """Turn a copied plain directory into a git repository with one commit,
    `Import <name>`, under the operator's own git identity. Does nothing when the
    directory already is a repository. The add runs with no core.excludesFile, so the
    operator's global ignore patterns leave no harness file out of the commit."""
    if _is_repository_root(directory):
        return
    for key in ("user.name", "user.email"):
        got = git(directory, ["config", "--get", key])
        if got.returncode != 0 or not got.stdout.strip():
            raise PplError("SETUP_INCOMPLETE",
                           "importing harness %s needs a git identity to commit it: run "
                           "`git config --global user.name \"<you>\"` and `git config --global "
                           "user.email \"<you@example.com>\"`" % name)
    pins = {"add": escape_git.OPERATOR_PINNED_CONFIG + ["-c", "core.excludesFile="],
            "commit": _IMPORT_PINS}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["commit", "-q", "-m", "Import %s" % name]):
        result = git(directory, args, pins=pins.get(args[0]))
        if result.returncode != 0:
            raise PplError("SETUP_INCOMPLETE", "git %s failed importing harness %s: %s"
                           % (args[0], name, stderr_text(result)))


def _rev(directory, ref):
    result = git(directory, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def commit(harness_dir):
    """The sha a run of this harness renders from: the fetched default head of
    `origin` when the harness repository has one, else its local HEAD. Refuses a
    directory that is no repository, a working copy with uncommitted paths, and a
    failed fetch."""
    if not _is_repository_root(harness_dir):
        raise _not_a_repository(harness_dir)
    status = git(harness_dir, ["status", "--porcelain"])
    if status.returncode != 0:
        raise PplError("SETUP_INCOMPLETE", "git status failed in %s: %s"
                       % (harness_dir, stderr_text(status)))
    dirty = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty:
        shown = "\n".join("  " + line for line in dirty[:_LISTED_PATHS])
        more = "\n  ... and %d more" % (len(dirty) - _LISTED_PATHS) if len(dirty) > _LISTED_PATHS else ""
        raise PplError("OWNER_DECISION",
                       "harness %s has uncommitted changes; commit them in the harness repository "
                       "(and push them when it has an origin) so the run can use them:\n%s%s"
                       % (harness_dir, shown, more))
    remotes = git(harness_dir, ["remote"]).stdout.split()
    if "origin" not in remotes:
        sha = _rev(harness_dir, "HEAD")
        if sha is None:
            raise PplError("SETUP_INCOMPLETE", "harness %s has no commit yet: commit it in the "
                           "harness repository" % harness_dir)
        return sha
    fetch = git(harness_dir, ["fetch", "--quiet", escape_git.FETCH_FLAG, "origin"],
                timeout=FETCH_TIMEOUT_SECONDS)
    if fetch.returncode != 0:
        raise PplError("EXTERNAL_BLOCKED", "git fetch of origin failed in %s: %s"
                       % (harness_dir, stderr_text(fetch)))
    head = git(harness_dir, ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"])
    candidates = [head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else []
    candidates += ["refs/remotes/origin/main", "refs/remotes/origin/master"]
    for ref in candidates:
        sha = _rev(harness_dir, ref)
        if sha:
            return sha
    raise PplError("SETUP_INCOMPLETE",
                   "harness %s has an origin but no default branch is known: run "
                   "`git -C %s remote set-head origin --auto`" % (harness_dir, harness_dir))


def _archive(harness_dir, sha):
    result = git(harness_dir, ["archive", "--format=tar", sha], text=False)
    if result.returncode != 0:
        raise PplError("SETUP_INCOMPLETE", "git archive %s failed in %s: %s"
                       % (sha, harness_dir, stderr_text(result)))
    return result.stdout


def _escapes(name):
    return posixpath.isabs(name) or ".." in posixpath.normpath(name).split("/")


def export(harness_dir, sha, dest_root, name):
    """Write the tree at `sha` into dest_root/name and return that directory.
    Refuses an archive member with an absolute or `..` path, or a link that
    points outside the tree."""
    dest = Path(dest_root) / name
    if dest.exists():
        raise PplError("SETUP_INCOMPLETE", "%s already exists" % dest)
    data = _archive(harness_dir, sha)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
            for member in members:
                bad = _escapes(member.name)
                if member.issym():
                    target = posixpath.join(posixpath.dirname(member.name), member.linkname)
                    bad = bad or _escapes(member.linkname) or _escapes(target)
                elif member.islnk():
                    bad = bad or _escapes(member.linkname)
                if bad:
                    raise PplError("SETUP_INCOMPLETE", "the harness archive holds a member that "
                                   "escapes its directory: %s" % member.name)
            dest.mkdir(parents=True)
            archive.extractall(dest, members=members, **(
                {"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    except (tarfile.TarError, OSError) as exc:
        raise PplError("SETUP_INCOMPLETE", "could not export harness %s at %s: %s"
                       % (name, sha, exc))
    return dest

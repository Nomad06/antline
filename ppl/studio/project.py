"""What the wizard learns about the user's repository before it drafts a
harness: the git facts, the stack, and the CLAUDE.md / AGENTS.md files that
describe it. Nothing here reads outside the repository: the md files are
found by walking it, and an @import that resolves elsewhere is skipped."""
import os
import re
import stat
import subprocess
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

from ..errors import PplError
from . import stacks

MD_NAMES = ("CLAUDE.md", "AGENTS.md")
MAX_DEPTH = 3
MAX_FILES = 20
MAX_BYTES = 200_000
MAX_SKIPPED = 50
SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".worktrees", "node_modules", "target", "build",
                       "dist", "vendor", ".venv", "venv", "__pycache__", ".tox", ".gradle", ".idea"})
_GIT_TIMEOUT_SECONDS = 20
# An @import in CLAUDE.md: @ and a path, not the tail of a word (an e-mail
# address) or of a path. An absolute or ~/ path matches too, to be refused.
_IMPORT_RE = re.compile(r"(?<![\w@/`.])@((?:~/|/)?[A-Za-z0-9_.][A-Za-z0-9_./-]*[A-Za-z0-9_])")
_URL_USERINFO_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@")


def _git(path, *args):
    try:
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _skipped_name(name):
    return name in SKIP_DIRS or (name.startswith(".") and name != ".claude")


def _in_skipped_dir(parts):
    return any(_skipped_name(part) for part in parts)


def _public_remote(remote):
    """The remote without the user[:password]@ part of a scheme:// URL, where
    a token is often carried."""
    return _URL_USERINFO_RE.sub(r"\1", remote) if remote else remote


def safe_name(text):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    return name or "project"


def forge_hint(remote):
    if not remote:
        return None
    if "://" in remote:
        try:
            host = urlparse(remote).hostname or ""
        except ValueError:
            return None
    elif "@" in remote and ":" in remote:
        host = remote.split("@", 1)[1].split(":", 1)[0]
    else:
        return None
    host = host.lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


def inspect(raw_path):
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise PplError("SCHEMA_INVALID", "give the path of your project's git checkout")
    try:
        expanded = Path(raw_path.strip()).expanduser()
    except (RuntimeError, OSError):
        raise PplError("SCHEMA_INVALID", "cannot expand ~ in %s" % raw_path.strip()) from None
    path = Path(os.path.abspath(expanded))
    if not path.is_dir():
        raise PplError("SCHEMA_INVALID", "%s is not a directory" % path)
    top = _git(path, "rev-parse", "--show-toplevel")
    if not top:
        raise PplError("SCHEMA_INVALID", "%s is not inside a git work tree: give the path of a "
                       "git checkout, or run git init there first" % path)
    root = Path(os.path.realpath(top))
    head = _git(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if head and "/" in head:
        branch = head.split("/", 1)[1]
    else:
        branch = _git(root, "branch", "--show-current") or "main"
    remote = _public_remote(_git(root, "remote", "get-url", "origin"))
    return {"path": str(root), "default_branch": branch, "remote_url": remote,
            "forge_hint": forge_hint(remote), "name_hint": safe_name(root.name),
            "stack": stacks.detect(root), "md_files": find_md(root)}


def _listed_size(real_root, directory, name):
    """The size of a CLAUDE.md or AGENTS.md that is a regular file inside the
    repository once its symlinks are followed, else None."""
    target = Path(os.path.realpath(Path(directory) / name))
    if real_root not in target.parents or _in_skipped_dir(target.relative_to(real_root).parts):
        return None
    try:
        info = os.stat(target)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


def find_md(root):
    root = Path(root)
    real_root = Path(os.path.realpath(root))
    found = []
    for directory, dirnames, filenames in os.walk(root):
        relative = Path(directory).relative_to(root)
        depth = len(relative.parts)
        dirnames[:] = sorted(name for name in dirnames
                             if depth < MAX_DEPTH and not _skipped_name(name))
        for name in sorted(filenames):
            if name in MD_NAMES:
                size = _listed_size(real_root, directory, name)
                if size is not None:
                    found.append({"path": (relative / name).as_posix(), "bytes": size})
    found.sort(key=lambda item: (item["path"].count("/"), item["path"]))
    return found[:MAX_FILES]


def collect(root, selected):
    """The files the extraction reads: the `selected` md files (paths find_md
    listed) and what they @import inside the repository, each file once, at
    most MAX_FILES files, MAX_BYTES bytes and MAX_SKIPPED skipped entries."""
    root = Path(os.path.realpath(root))
    listed = {item["path"] for item in find_md(root)}
    queue = deque()
    skipped = []

    def skip(path, reason):
        if len(skipped) < MAX_SKIPPED:
            skipped.append({"path": path, "reason": reason})

    for rel in selected or []:
        if isinstance(rel, str) and rel in listed:
            queue.append(rel)
        else:
            skip(str(rel), "not one of the files the project inspection found")
    files = []
    seen = set()
    refused = set()
    total = 0
    while queue:
        rel = queue.popleft()
        target = Path(os.path.realpath(root / rel))
        if str(target) in seen:
            continue
        seen.add(str(target))
        if root not in target.parents:
            skip(rel, "outside the repository")
            continue
        if _in_skipped_dir(target.relative_to(root).parts):
            skip(rel, "in a skipped directory")
            continue
        try:
            mode = os.stat(target).st_mode
        except OSError:
            skip(rel, "no such file")
            continue
        if not stat.S_ISREG(mode):
            skip(rel, "not a regular file")
            continue
        if len(files) >= MAX_FILES:
            skip(rel, "more than %d files, %d more not read" % (MAX_FILES, len(queue)))
            break
        try:
            with open(target, "rb") as handle:
                data = handle.read(MAX_BYTES - total + 1)
        except OSError:
            skip(rel, "cannot be read")
            continue
        if total + len(data) > MAX_BYTES:
            skip(rel, "over the %d byte budget" % MAX_BYTES)
            continue
        if b"\x00" in data:
            skip(rel, "binary file")
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skip(rel, "not UTF-8 text")
            continue
        total += len(data)
        files.append({"path": target.relative_to(root).as_posix(), "text": text})
        for match in _IMPORT_RE.finditer(text):
            imported = match.group(1)
            if imported.startswith(("/", "~")):
                if imported not in refused:
                    refused.add(imported)
                    skip(imported, "outside the repository")
                continue
            relative = os.path.relpath(os.path.realpath(target.parent / imported), root)
            queue.append(Path(relative).as_posix())
    return files, skipped

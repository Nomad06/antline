"""What the wizard learns about the user's repository before it drafts a
harness: the git facts, the stack, and the CLAUDE.md / AGENTS.md files that
describe it. Nothing here reads outside the repository: the md files are
found by walking it, and an @import that resolves elsewhere is skipped."""
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from ..errors import PplError
from . import stacks

MD_NAMES = ("CLAUDE.md", "AGENTS.md")
MAX_DEPTH = 3
MAX_FILES = 20
MAX_BYTES = 200_000
SKIP_DIRS = frozenset({".git", ".hg", ".svn", ".worktrees", "node_modules", "target", "build",
                       "dist", "vendor", ".venv", "venv", "__pycache__", ".tox", ".gradle", ".idea"})
_GIT_TIMEOUT_SECONDS = 20
# An @import in CLAUDE.md: @ and a relative path, not the tail of a word (an
# e-mail address) or of a path.
_IMPORT_RE = re.compile(r"(?<![\w@/`.])@([A-Za-z0-9_.][A-Za-z0-9_./-]*[A-Za-z0-9_])")


def _git(path, *args):
    try:
        result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


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
    path = Path(os.path.abspath(Path(raw_path.strip()).expanduser()))
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
    remote = _git(root, "remote", "get-url", "origin")
    return {"path": str(root), "default_branch": branch, "remote_url": remote,
            "forge_hint": forge_hint(remote), "name_hint": safe_name(root.name),
            "stack": stacks.detect(root), "md_files": find_md(root)}


def find_md(root):
    root = Path(root)
    found = []
    for directory, dirnames, filenames in os.walk(root):
        relative = Path(directory).relative_to(root)
        depth = len(relative.parts)
        dirnames[:] = sorted(name for name in dirnames
                             if depth < MAX_DEPTH and name not in SKIP_DIRS
                             and (name == ".claude" or not name.startswith(".")))
        for name in sorted(filenames):
            if name in MD_NAMES:
                try:
                    size = (Path(directory) / name).stat().st_size
                except OSError:
                    continue
                found.append({"path": (relative / name).as_posix(), "bytes": size})
    found.sort(key=lambda item: (item["path"].count("/"), item["path"]))
    return found[:MAX_FILES]


def collect(root, selected):
    """The files the extraction reads: the `selected` md files (paths find_md
    listed) and what they @import inside the repository, each file once, at
    most MAX_FILES files and MAX_BYTES bytes."""
    root = Path(os.path.realpath(root))
    listed = {item["path"] for item in find_md(root)}
    queue = []
    skipped = []
    for rel in selected or []:
        if isinstance(rel, str) and rel in listed:
            queue.append(rel)
        else:
            skipped.append({"path": str(rel),
                            "reason": "not one of the files the project inspection found"})
    files = []
    seen = set()
    total = 0
    while queue:
        rel = queue.pop(0)
        target = Path(os.path.realpath(root / rel))
        if str(target) in seen:
            continue
        seen.add(str(target))
        if root not in target.parents:
            skipped.append({"path": rel, "reason": "outside the repository"})
            continue
        if not target.is_file():
            skipped.append({"path": rel, "reason": "no such file"})
            continue
        if len(files) >= MAX_FILES:
            skipped.append({"path": rel, "reason": "more than %d files, %d more not read"
                            % (MAX_FILES, len(queue))})
            break
        try:
            with open(target, "rb") as handle:
                data = handle.read(MAX_BYTES - total + 1)
        except OSError:
            skipped.append({"path": rel, "reason": "cannot be read"})
            continue
        if total + len(data) > MAX_BYTES:
            skipped.append({"path": rel, "reason": "over the %d byte budget" % MAX_BYTES})
            continue
        if b"\x00" in data:
            skipped.append({"path": rel, "reason": "binary file"})
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": rel, "reason": "not UTF-8 text"})
            continue
        total += len(data)
        files.append({"path": target.relative_to(root).as_posix(), "text": text})
        for match in _IMPORT_RE.finditer(text):
            imported = os.path.relpath(os.path.realpath(target.parent / match.group(1)), root)
            queue.append(Path(imported).as_posix())
    return files, skipped

"""The studio's saves. A file is named by a key, never by a path from the
browser: "routing", or "harness:<installed name>:<one of HARNESS_FILES>". A
save names the sha256 of the content the page loaded and is refused when
the file changed since, and the content it replaces is kept in
~/.ppl/studio/history/<key>/ for a restore."""
import contextlib
import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import harness, paths, routing
from ..errors import PplError
from . import drafts

KEEP = 50
HARNESS_FILES = ("harness.toml",) + drafts.FRAGMENTS
_VERSION_RE = re.compile(r"[0-9]{8}T[0-9]{12}Z")
_STAMP = "%Y%m%dT%H%M%S%fZ"
_LOCK = threading.Lock()


def _split(key):
    parts = key.split(":", 2) if isinstance(key, str) else []
    if len(parts) != 3 or parts[0] != "harness" or parts[2] not in HARNESS_FILES:
        raise PplError("SCHEMA_INVALID", "unknown file key %r: the studio saves routing, or "
                       "harness:<name>:<file> with the file one of %s"
                       % (key, ", ".join(HARNESS_FILES)))
    drafts.check_name(parts[1])
    return parts[1], parts[2]


def _inside(folder, rel):
    """folder/rel, refused when a symlink in it leads out of the folder."""
    path = folder / rel
    try:
        inside = path.resolve().is_relative_to(folder.resolve())
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        raise PplError("SCHEMA_INVALID", "%s leaves the harness directory" % rel)
    return path


def _installed(name):
    drafts.check_name(name)
    if name not in harness.list_names():
        raise PplError("SCHEMA_INVALID", "no installed harness %r" % name)
    return paths.harness_dir(name)


def resolve(key):
    if key == "routing":
        return paths.routing_file()
    name, rel = _split(key)
    return _inside(_installed(name), rel)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _bytes(path):
    return path.read_bytes() if path.is_file() else b""


def read(key):
    data = _bytes(resolve(key))
    return {"key": key, "text": data.decode("utf-8", errors="replace"), "hash": digest(data)}


def harness_fragments(name):
    """The fragments an installed harness has on disk, by relative path."""
    folder = _installed(name)
    found = {}
    for rel in drafts.FRAGMENTS:
        path = _inside(folder, rel)
        if path.is_file():
            found[rel] = path.read_text(encoding="utf-8", errors="replace")
    return found


def _history_dir(key):
    return paths.home() / "studio" / "history" / key.replace(":", "__").replace("/", "__")


def _stage(path, data, mode=None):
    """A sibling of path holding data, for os.replace to move into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".studio-tmp")
    temporary.unlink(missing_ok=True)
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(temporary, mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _write(path, data):
    temporary = _stage(path, data)
    try:
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _next_stamp(folder):
    """The name of the next version: now, or one microsecond after the newest
    kept one when the clock stands behind it, so a version never sorts below
    an older one."""
    moment = datetime.now(timezone.utc)
    for item in folder.iterdir():
        if _VERSION_RE.fullmatch(item.name):
            try:
                taken = datetime.strptime(item.name, _STAMP).replace(tzinfo=timezone.utc)
                moment = max(moment, taken + timedelta(microseconds=1))
            except (ValueError, OverflowError):
                continue
    return moment.strftime(_STAMP)


def _keep(key, data):
    folder = _history_dir(key)
    folder.mkdir(parents=True, exist_ok=True)
    _write(folder / _next_stamp(folder), data)


def _prune(key):
    folder = _history_dir(key)
    names = sorted(item.name for item in folder.iterdir() if _VERSION_RE.fullmatch(item.name))
    for old in names[:max(0, len(names) - KEEP)]:
        with contextlib.suppress(OSError):
            (folder / old).unlink()


def _encode(label, text):
    if not isinstance(text, str):
        raise PplError("SCHEMA_INVALID", "the content of %s must be text" % label)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise PplError("SCHEMA_INVALID", "the content of %s is not valid UTF-8 text" % label)


def _target(path):
    """Where a save to path lands: the file a link at path points to, so the
    link stays a link, and one name for a file however it is reached. A
    harness key was already proven to stay inside its directory by resolve."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        raise PplError("SCHEMA_INVALID", "%s is a link ppl cannot follow" % path.name)


def _plan(key, text, expected):
    data = _encode(key, text)
    if not isinstance(expected, str):
        raise PplError("SCHEMA_INVALID", "the save of %s must name the hash of the content "
                       "the page loaded" % key)
    target = _target(resolve(key))
    current = _bytes(target)
    if digest(current) != expected:
        raise PplError("CHANGED_ON_DISK", "%s changed since the page loaded it: reload to "
                       "see the new content, your edit is still in the page" % key)
    mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else None
    return key, target, current, data, mode


def save_many(changes):
    """[(key, text, expected_hash)], all or nothing: every hash is checked under
    one lock before the first write and every changed file is staged before
    the first one is replaced, so one stale file or a full disk refuses the
    whole save. A history version is pruned only after every file of the batch
    was replaced."""
    with _LOCK:
        planned = [_plan(*change) for change in changes]
        if len({target for _, target, _, _, _ in planned}) != len(planned):
            raise PplError("SCHEMA_INVALID", "one save names each file once, a link and the "
                           "file it points to included")
        staged = []
        kept = []
        try:
            for key, target, current, data, mode in planned:
                if data != current:
                    staged.append((key, target, current, _stage(target, data, mode)))
            for key, _, current, _ in staged:
                if current:
                    _keep(key, current)
                    kept.append(key)
            for _, target, _, temporary in staged:
                os.replace(temporary, target)
        except BaseException:
            for _, _, _, temporary in staged:
                temporary.unlink(missing_ok=True)
            raise
        for key in kept:
            _prune(key)
        return {key: digest(data) for key, _, _, data, _ in planned}


def save(key, text, expected_hash):
    return save_many([(key, text, expected_hash)])[key]


def versions(key):
    resolve(key)
    folder = _history_dir(key)
    if not folder.is_dir():
        return []
    names = sorted((item.name for item in folder.iterdir() if _VERSION_RE.fullmatch(item.name)),
                   reverse=True)
    return [{"version": name, "bytes": (folder / name).stat().st_size} for name in names]


def version_text(key, version):
    resolve(key)
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise PplError("SCHEMA_INVALID", "version %r is not a history timestamp" % (version,))
    path = _history_dir(key) / version
    if not path.is_file():
        raise PplError("NOT_FOUND", "no version %s of %s" % (version, key))
    return path.read_bytes().decode("utf-8", errors="replace")


def _toml(text, label, code):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PplError(code, "%s is not valid TOML: %s" % (label, exc))


def _renamed(text, scratch_names, installed):
    for scratch in scratch_names:
        text = text.replace(scratch, installed)
    return text


def check_harness(name, changes=()):
    """drafts.verify of a private copy of an installed harness with each
    (rel, text) of changes written over its file: the exact bytes a save
    writes, so what is checked is what lands. A change through a link lands
    on the file the link points to, as a save does. The copy is removed after
    and drafts/<name>/ is never touched."""
    folder = _installed(name)
    targets = {rel: _inside(folder, rel).resolve() for rel in HARNESS_FILES}
    contents = {rel: (folder / rel).read_bytes() for rel in HARNESS_FILES
                if (folder / rel).is_file()}
    for rel, text in changes:
        if rel not in HARNESS_FILES:
            raise PplError("SCHEMA_INVALID", "unknown harness file %r (files: %s)"
                           % (rel, ", ".join(HARNESS_FILES)))
        data = _encode(rel, text)
        for other in HARNESS_FILES:
            if targets[other] == targets[rel]:
                contents[other] = data
    drafts.drafts_dir().mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(dir=drafts.drafts_dir(), prefix=".check-"))
    try:
        copy = scratch / name
        copy.mkdir()
        for rel, data in contents.items():
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            (copy / rel).write_bytes(data)
        verdict = drafts.verify(copy, name)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    named = (str(copy), str(copy.resolve()))
    for field in ("errors", "warnings"):
        verdict[field] = [_renamed(text, named, str(folder)) for text in verdict[field]]
    return verdict


def validate(key, text):
    """Refuse content ppl would refuse: a routing.toml that does not route, or a
    harness file that leaves its harness invalid."""
    if not isinstance(text, str):
        raise PplError("SCHEMA_INVALID", "the content of %s must be text" % key)
    if key == "routing":
        doc = _toml(text, "routing.toml", "SCHEMA_INVALID")
        try:
            routing.validate_config(doc)
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PplError("SCHEMA_INVALID", "routing.toml is not a routing document: %s" % exc)
        return
    resolve(key)
    name, rel = _split(key)
    result = check_harness(name, [(rel, text)])
    if not result["ok"]:
        raise PplError("HARNESS_INVALID", "; ".join(result["errors"]))


def restore(key, version, expected_hash):
    text = version_text(key, version)
    validate(key, text)
    return save(key, text, expected_hash)

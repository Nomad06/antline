"""The studio's saves. A file is named by a key, never by a path from the
browser: "routing", or "harness:<installed name>:<one of HARNESS_FILES>". A
save names the sha256 of the content the page loaded and is refused when
the file changed since, and the content it replaces is kept in
~/.ppl/studio/history/<key>/ for a restore."""
import hashlib
import os
import re
import threading
import tomllib
from datetime import datetime, timedelta, timezone

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


def resolve(key):
    if key == "routing":
        return paths.routing_file()
    name, rel = _split(key)
    if name not in harness.list_names():
        raise PplError("SCHEMA_INVALID", "no installed harness %r" % name)
    return _inside(paths.harness_dir(name), rel)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _bytes(path):
    return path.read_bytes() if path.is_file() else b""


def read(key):
    data = _bytes(resolve(key))
    return {"key": key, "text": data.decode("utf-8", errors="replace"), "hash": digest(data)}


def harness_fragments(name):
    """The fragments an installed harness has on disk, by relative path."""
    drafts.check_name(name)
    folder = paths.harness_dir(name)
    found = {}
    for rel in drafts.FRAGMENTS:
        path = _inside(folder, rel)
        if path.is_file():
            found[rel] = path.read_text(encoding="utf-8", errors="replace")
    return found


def _history_dir(key):
    return paths.home() / "studio" / "history" / key.replace(":", "__").replace("/", "__")


def _stage(path, data):
    """A sibling of path holding data, for os.replace to move into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".studio-tmp")
    temporary.unlink(missing_ok=True)
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
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


def _keep(key, data):
    folder = _history_dir(key)
    folder.mkdir(parents=True, exist_ok=True)
    moment = datetime.now(timezone.utc)
    while (folder / moment.strftime(_STAMP)).exists():
        moment += timedelta(microseconds=1)
    _write(folder / moment.strftime(_STAMP), data)
    names = sorted(item.name for item in folder.iterdir() if _VERSION_RE.fullmatch(item.name))
    for old in names[:max(0, len(names) - KEEP)]:
        (folder / old).unlink()


def _plan(key, text, expected):
    if not isinstance(text, str):
        raise PplError("SCHEMA_INVALID", "the content of %s must be text" % key)
    try:
        data = text.encode("utf-8")
    except UnicodeEncodeError:
        raise PplError("SCHEMA_INVALID", "the content of %s is not valid UTF-8 text" % key)
    if not isinstance(expected, str):
        raise PplError("SCHEMA_INVALID", "the save of %s must name the hash of the content "
                       "the page loaded" % key)
    path = resolve(key)
    current = _bytes(path)
    if digest(current) != expected:
        raise PplError("CHANGED_ON_DISK", "%s changed since the page loaded it: reload to "
                       "see the new content, your edit is still in the page" % key)
    return key, path, current, data


def save_many(changes):
    """[(key, text, expected_hash)], all or nothing: every hash is checked under
    one lock before the first write and every changed file is staged before
    the first one is replaced, so one stale file or a full disk refuses the
    whole save."""
    with _LOCK:
        keys = [change[0] for change in changes]
        if len(set(keys)) != len(keys):
            raise PplError("SCHEMA_INVALID", "one save names each file once")
        planned = [_plan(*change) for change in changes]
        staged = []
        try:
            for key, path, current, data in planned:
                if data != current:
                    staged.append((key, path, current, _stage(path, data)))
            for key, _, current, _ in staged:
                if current:
                    _keep(key, current)
            for _, path, _, temporary in staged:
                os.replace(temporary, path)
        except BaseException:
            for _, _, _, temporary in staged:
                temporary.unlink(missing_ok=True)
            raise
        return {key: digest(data) for key, _, _, data in planned}


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
    fragments = harness_fragments(name)
    if rel == "harness.toml":
        doc = _toml(text, "harness.toml", "HARNESS_INVALID")
    else:
        installed = resolve("harness:%s:harness.toml" % name).read_text(encoding="utf-8",
                                                                         errors="replace")
        doc = _toml(installed, "harness.toml", "HARNESS_INVALID")
        fragments[rel] = text
    result = drafts.check(name, doc, fragments)
    if not result["ok"]:
        raise PplError("HARNESS_INVALID", "; ".join(result["errors"]))


def restore(key, version, expected_hash):
    text = version_text(key, version)
    validate(key, text)
    return save(key, text, expected_hash)

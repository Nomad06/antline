import json
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import paths
from .errors import PplError


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deadline_in(seconds):
    """A future timestamp in the same shape `now()` produces.

    Both are fixed-width UTC `isoformat(timespec="seconds")` strings, so
    comparing a deadline to `now()` with plain string comparison agrees with
    comparing the parsed datetimes - no parsing needed at the call site.
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds")


# The most the guard hook and escape read of one run file. runs/ is sandbox-writable, and a
# sparse file of any size a role leaves there would take the hook past its timeout to read,
# and a hook that times out denies nothing. A CLI read and the log readers keep no cap.
HOOK_READ_LIMIT = 1024 * 1024


def _over_limit(path, size, limit):
    return PplError("SCHEMA_INVALID", "%s holds %d bytes or more, over the %d a run file may "
                    "hold, so it is not read" % (path, size, limit))


def read_regular_text(path, limit=None):
    """The text of the regular file at `path`. It is opened without blocking and read only
    when it is a regular file: runs/ is sandbox-writable, and a FIFO left where a run file
    belongs would hold every reader, the guard hook past its timeout among them, and a hook
    that times out denies nothing. With `limit`, a file of more than `limit` bytes is refused
    unread, and no more than that is read of one that grows meanwhile. FileNotFoundError when
    absent, SCHEMA_INVALID when not a regular file or over the limit."""
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PplError("SCHEMA_INVALID", "%s is not a regular file" % path)
        if limit is None:
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = None
                return handle.read()
        if info.st_size > limit:
            raise _over_limit(path, info.st_size, limit)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise _over_limit(path, len(data), limit)
        return data.decode("utf-8")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_json(path, default=None, limit=None):
    """Every JSON reader in this codebase goes through here, so a document a
    session left half-written (or one mangled by hand) is a routable error
    everywhere at once, not a raw json.JSONDecodeError - exit 1, past the
    documented 0/2/1 contract - at whichever call site happens to read it
    first [R-STORE-CORRUPT-JSON]. It never blocks on a file that is not a
    regular one, and with `limit` reads none over that many bytes (read_regular_text).
    """
    try:
        text = read_regular_text(path, limit)
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PplError("SCHEMA_INVALID", "%s is not valid JSON: %s" % (path, exc))


def checked_run_path(path):
    """`path` when it lies under the real runs directory and no part of it below runs/ is a
    link or resolves anywhere else, else EXTERNAL_BLOCKED naming the part. A role can write
    runs/, so a ppl command that runs outside the OS sandbox calls this right before it
    writes there, and writes nothing through a link a role planted."""
    path = Path(path)
    runs = paths.runs_dir()
    real_runs = os.path.realpath(str(runs))
    try:
        parts = path.relative_to(runs).parts
    except ValueError:
        parts = None
    if not parts or any(part in (".", "..") for part in parts):
        raise PplError("EXTERNAL_BLOCKED", "%s is not a path under %s, so ppl will not write "
                       "it" % (path, runs))
    current, expected = runs, real_runs
    for part in parts:
        current, expected = current / part, os.path.join(expected, part)
        if os.path.islink(str(current)) or os.path.realpath(str(current)) != expected:
            raise PplError("EXTERNAL_BLOCKED", "%s is a link or resolves outside %s, so ppl "
                           "will not write through it: remove it and run the command again"
                           % (current, real_runs))
    return path


def write_json(path, obj):
    # Coerced once here so every caller (present and future) may pass a str
    # or a Path interchangeably instead of each call site needing to know.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_text(path, text):
    """write_json's atomic replace for a text file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".%s." % path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_log(task_id, event):
    """Append one event. The log is opened through no link and without blocking, and only a
    regular file with a single link is appended to: runs/ is sandbox-writable, so a link there
    could point an unsandboxed ppl command's append anywhere."""
    record = dict(event)
    record["at"] = now()
    target = paths.run_dir(task_id) / "log.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    flags = (os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        descriptor = os.open(str(target), flags, 0o644)
    except OSError as exc:
        if os.path.islink(str(target)) or os.path.lexists(str(target)):
            raise PplError("EXTERNAL_BLOCKED", "%s is a link or not a regular file, so ppl "
                           "will not append to it: %s" % (target, exc))
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise PplError("EXTERNAL_BLOCKED", "%s is not a regular file, so ppl will not "
                           "append to it" % target)
        if info.st_nlink != 1:
            raise PplError("EXTERNAL_BLOCKED", "%s has %d links, and a second link may be "
                           "another name of a file outside the run, so ppl will not append "
                           "to it" % (target, info.st_nlink))
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            descriptor = None
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_log_at(directory):
    """Every event logged under `directory`, in the order they were appended.

    Takes a directory directly rather than a task_id so a caller can read a
    run's history whether it still lives under `runs/<task_id>` or has been
    moved to `runs/_archive/...` by `ppl destroy --scope local` - the log
    file itself is untouched by that move, only its parent directory's name
    changes.
    """
    path = Path(directory) / "log.jsonl"
    events = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except FileNotFoundError:
        pass
    return events


def read_log(task_id):
    """The log of the run currently live at `runs/<task_id>` - not usable
    once that run has been archived. See `read_log_at` for a path that
    survives archival."""
    return read_log_at(paths.run_dir(task_id))

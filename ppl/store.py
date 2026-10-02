import json
import os
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


def read_json(path, default=None):
    """Every JSON reader in this codebase goes through here, so a document a
    session left half-written (or one mangled by hand) is a routable error
    everywhere at once, not a raw json.JSONDecodeError - exit 1, past the
    documented 0/2/1 contract - at whichever call site happens to read it
    first [R-STORE-CORRUPT-JSON].
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise PplError("SCHEMA_INVALID", "%s is not valid JSON: %s" % (path, exc))


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


def append_log(task_id, event):
    record = dict(event)
    record["at"] = now()
    target = paths.run_dir(task_id) / "log.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


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

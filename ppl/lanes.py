"""The lane registry: which task windows run now. `PPL_HOME/lanes.json` holds one entry per
running task. Writers take an exclusive flock on `lanes.lock`, re-read, change and replace the
file atomically; readers (the hooks) take no lock, so a hook never blocks on one and never
sees a torn file. Parses on Python 3.9: the hooks import it."""
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from . import paths, store
from .errors import PplError

REGISTRY_FILE = "lanes.json"
LOCK_FILE = "lanes.lock"
READ_LIMIT = 1 << 20


def registry_path():
    return paths.home() / REGISTRY_FILE


def start_dir(task_id):
    return paths.home() / "lanes" / task_id


def _invalid(why):
    return PplError("SCHEMA_INVALID", "%s %s" % (registry_path(), why))


def read(limit=READ_LIMIT):
    """The entries of lanes.json, [] when it is absent."""
    try:
        text = store.read_regular_text(registry_path(), limit)
    except FileNotFoundError:
        return []
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise _invalid("is not valid JSON: %s" % exc)
    lanes = doc.get("lanes") if isinstance(doc, dict) else None
    if (not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(lanes, list)
            or not all(isinstance(item, dict) for item in lanes)):
        raise _invalid('is not {"version": 1, "lanes": [...]}')
    return lanes


def entry(task_id, limit=READ_LIMIT):
    for item in read(limit):
        if item.get("task") == task_id:
            return item
    return None


def _contains(directory, target):
    if not directory:
        return False
    root = os.path.realpath(str(directory))
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def for_path(path, limit=READ_LIMIT):
    """The entry whose start directory or worktree holds `path`, compared by realpath so a
    symlinked parent (/tmp, /private/tmp) does not hide it."""
    target = os.path.realpath(str(path))
    for item in read(limit):
        if _contains(item.get("start_dir"), target) or _contains(item.get("worktree"), target):
            return item
    return None


@contextmanager
def _locked():
    home = paths.home()
    home.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(home / LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write(lanes):
    store.write_json(registry_path(), {"version": 1, "lanes": lanes})


def _listing(items):
    return ", ".join("lane %s %s %s" % (item.get("lane"), item.get("task"), item.get("harness"))
                     for item in items)


def claim(task_id, harness, harness_sha, repo, worktree, session_id, harness_max, machine_max):
    """Reserve the lowest free lane for `task_id`. ILLEGAL_TRANSITION when the task already has
    one, BUSY when the machine or the harness is at its limit."""
    with _locked():
        lanes = read()
        if any(item.get("task") == task_id for item in lanes):
            raise PplError("ILLEGAL_TRANSITION", "task %s already has a lane" % task_id)
        if len(lanes) >= machine_max:
            raise PplError("BUSY", "the machine runs %d of %d lanes: %s"
                           % (len(lanes), machine_max, _listing(lanes)))
        same = [item for item in lanes if item.get("harness") == harness]
        if len(same) >= harness_max:
            raise PplError("BUSY", "harness %s runs %d of %d lanes: %s"
                           % (harness, len(same), harness_max, _listing(same)))
        taken = set(item.get("lane") for item in lanes)
        lane = next(number for number in range(1, machine_max + 1) if number not in taken)
        record = {"lane": lane, "task": task_id, "harness": harness,
                  "harness_sha": harness_sha, "repo": str(repo), "worktree": str(worktree),
                  "start_dir": str(start_dir(task_id)), "session_id": session_id,
                  "pid": None, "started": store.now()}
        _write(lanes + [record])
        return record


def set_pid(task_id, pid):
    with _locked():
        lanes = read()
        for item in lanes:
            if item.get("task") == task_id:
                item["pid"] = pid
        _write(lanes)


def release(task_id):
    """Free the lane of `task_id`. True when it had one."""
    with _locked():
        lanes = read()
        kept = [item for item in lanes if item.get("task") != task_id]
        if len(kept) == len(lanes):
            return False
        _write(kept)
        return True

"""The lane registry: which task windows run now. `PPL_HOME/lanes.json` holds one entry per
running task. Writers take an exclusive flock on `lanes.lock`, re-read, change and replace the
file atomically; readers (the hooks) take no lock, so a hook never blocks on one and never
sees a torn file. Parses on Python 3.9: the hooks import it."""
import collections
import json
import os
from contextlib import contextmanager
from pathlib import Path

from . import paths, store
from .errors import PplError

REGISTRY_FILE = "lanes.json"
LOCK_FILE = "lanes.lock"
RECORD_FILE = "lane.json"
READ_LIMIT = 1 << 20


def registry_path():
    return paths.home() / REGISTRY_FILE


def start_dir(task_id):
    return paths.home() / "lanes" / task_id


def record_path(task_id):
    """The lane's record in its start directory, {"task", "worktree", "checkout", "branch"} as
    ppl lane made them. It outlives the registry entry, which ppl lane close removes, until
    ppl destroy removes the start directory, and no lane's sandbox can write it."""
    return start_dir(task_id) / RECORD_FILE


def _invalid(why):
    return PplError("SCHEMA_INVALID", "%s %s" % (registry_path(), why))


def read(limit=READ_LIMIT):
    """The entries of lanes.json, [] when it is absent. SCHEMA_INVALID when it is not a
    regular file, over `limit`, not UTF-8, not JSON or not the registry's shape."""
    try:
        text = store.read_regular_text(registry_path(), limit)
    except FileNotFoundError:
        return []
    except UnicodeDecodeError as exc:
        raise _invalid("is not UTF-8: %s" % exc)
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError) as exc:
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


Binding = collections.namedtuple("Binding", "task entry problem")


def hook_task(argv):
    """The task id a hook command names as `--task <id>`, None when it names none. A hook's
    argv comes from the lane's write-protected settings.json, where ppl renders exactly that
    pair, so anything else is refused: ValueError when --task is given twice, has no value or
    is not a task id, and for any other argument."""
    task = None
    items = list(argv)
    index = 0
    while index < len(items):
        item = items[index]
        if item != "--task":
            raise ValueError("unexpected argument %r" % (item,))
        if task is not None:
            raise ValueError("--task is given twice")
        if index + 1 >= len(items):
            raise ValueError("--task has no value")
        value = items[index + 1]
        if not isinstance(value, str):
            raise ValueError("--task %r is not a string" % (value,))
        try:
            paths.check_task_id(value)
        except PplError as err:
            raise ValueError(err.message)
        # check_task_id's `$` lets a trailing newline through, a run directory name it never makes
        if not paths.TASK_ID_RE.fullmatch(value):
            raise ValueError("task id must match %s, got: %r" % (paths.TASK_ID_RE.pattern, value))
        task = value
        index += 2
    return task


def binding(argv, limit=READ_LIMIT):
    """The lane a hook's argv binds it to, as Binding(task, entry, problem). Never raises.
    - no --task: Binding(None, None, None), a hook outside any lane;
    - a registered lane: Binding(task, its registry entry, None);
    - otherwise `problem` says why the lane cannot be confirmed (a malformed argv, a registry
      that cannot be read, a task with no entry), and the caller fails closed.
    The registry is read with no lock (a hook never waits on one) and through `limit`."""
    try:
        task = hook_task(argv)
    except Exception as err:
        return Binding(None, None, "the hook's command line binds no lane (%s): its settings.json "
                                   "is not the one ppl lane renders" % err)
    if task is None:
        return Binding(None, None, None)
    try:
        found = entry(task, limit)
    except Exception as err:
        return Binding(task, None, "lane %s cannot be confirmed, the lane registry cannot be read "
                                   "(%s): repair PPL_HOME/lanes.json, then run ppl lane resume %s"
                       % (task, getattr(err, "message", None) or type(err).__name__, task))
    if found is None:
        return Binding(task, None, "lane %s is not registered: run ppl lane resume %s"
                       % (task, task))
    return Binding(task, found, None)


def _contains(directory, target):
    if not directory or not isinstance(directory, str):
        return False
    root = os.path.realpath(directory)
    return target == root or target.startswith(root.rstrip(os.sep) + os.sep)


def holding(entries, path):
    """The entry of `entries` whose start directory or worktree holds `path`, compared by
    realpath so a symlinked parent (/tmp, /private/tmp) does not hide it, else None."""
    target = os.path.realpath(str(path))
    for item in entries:
        if _contains(item.get("start_dir"), target) or _contains(item.get("worktree"), target):
            return item
    return None


def holding_worktree(entries, path):
    """The entry of `entries` whose worktree is or holds `path`, compared by realpath, else
    None."""
    target = os.path.realpath(str(path))
    for item in entries:
        if _contains(item.get("worktree"), target):
            return item
    return None


def for_path(path, limit=READ_LIMIT):
    """The registered entry whose start directory or worktree holds `path` (holding)."""
    return holding(read(limit), path)


def task_for_call(given=None, cwd=None, environ=None):
    """The run a ppl call is about: `given` (its --task), else PPL_TASK of `environ` (the
    process environment by default), else the task of the lane whose start directory or
    worktree holds `cwd` (the process cwd by default), else None. Never a pointer file.
    SCHEMA_INVALID when --task and PPL_TASK name different runs: a lane's session works on its
    own run, and from outside one PPL_TASK is unset."""
    env = (os.environ if environ is None else environ).get("PPL_TASK") or None
    if given and env and given != env:
        raise PplError("SCHEMA_INVALID", "--task %s and PPL_TASK=%s name different runs: a lane's "
                       "session works on its own run; unset PPL_TASK to name another from "
                       "outside a lane" % (given, env))
    if given or env:
        return given or env
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:
            return None
    found = for_path(cwd)
    return found.get("task") if found else None


@contextmanager
def _locked():
    # imported here: only the registry's writers lock, and the hooks, which read it with no
    # lock (D1), must import this module where fcntl does not exist
    import fcntl

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

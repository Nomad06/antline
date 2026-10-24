import uuid

from . import paths, run, store, watchdog
from .errors import PplError

COMMANDS = ("pause", "resume", "stop", "destroy")
SCOPES = ("local", "full")
HALTING = ("pause", "stop", "destroy")


def _path(task_id):
    return paths.run_dir(task_id) / "control.json"


def set_command(task_id, command, scope=None, by="panel"):
    if command not in COMMANDS:
        raise PplError("SCHEMA_INVALID", "unknown control command %r" % command)
    if command == "destroy" and scope not in SCOPES:
        raise PplError("SCHEMA_INVALID",
                       "destroy needs an explicit scope: local or full")
    record = {"command": command, "scope": scope, "requested_by": by,
              "requested_at": store.now(), "nonce": uuid.uuid4().hex}
    store.write_json(_path(task_id), record)
    store.append_log(task_id, {"event": "control", "command": command, "scope": scope, "by": by})
    if command == "stop":
        # a stop is an escalation with a record, not a silent halt
        run.park(task_id, "OWNER_DECISION",
                 "operator stopped the run from the %s, resume when they say so" % by, [])
    return record


def _invalid(path, reason):
    return {"command": None, "invalid": True,
            "reason": "control file %s %s" % (path, reason)}


def read(task_id, limit=None):
    """The operator's control record, None when there is none, an invalid marker when it
    cannot be used. `limit` caps the read as store.read_regular_text does."""
    path = _path(task_id)
    if not path.exists():
        return None
    try:
        record = store.read_json(path, limit=limit)
    except PplError as exc:
        return _invalid(path, "is not valid JSON: %s" % exc.message)
    except (OSError, ValueError) as exc:
        return _invalid(path, "is not valid JSON: %s" % exc)
    if not isinstance(record, dict) or not record.get("command"):
        return _invalid(path, "has no usable command")
    command = record["command"]
    if command not in COMMANDS:
        return _invalid(path, "has an unknown command %r" % command)
    if not record.get("requested_at"):
        return _invalid(path, "is missing requested_at")
    if command == "destroy" and record.get("scope") not in SCOPES:
        return _invalid(path, "is a destroy command with no usable scope")
    return record


def clear(task_id):
    path = _path(task_id)
    if path.exists():
        path.unlink()


def enforce(task_id, refresh=True):
    # The watchdog is checked first: a run with no recorded activity past its
    # inactivity timeout is discovered and parked here, before the panel's own
    # control record is even read, because a run this call is about to let
    # write is the one place TIMED_OUT actually gets caught for every stage
    # before "mr" (see DESIGN 6.9) - nothing else calls this on its own
    # during a hang.
    result = watchdog.check(task_id)
    if result["timed_out"]:
        raise PplError("TIMED_OUT",
                       "run %s has had no ppl activity since its deadline and has been "
                       "parked (lease generation is now %d). Run `ppl recover` before doing "
                       "anything else." % (task_id, result["lease_generation"]))
    # control.json is the one carve-out to "only ppl writes under runs/": an
    # external panel writes it, so it can be malformed by a partial write, a
    # schema drift, or a panel bug. A halt control that fails open on bad
    # input is worse than no control at all, so any record we cannot trust
    # halts the run until a human looks, same as an explicit pause.
    record = read(task_id)
    if record is not None:
        if record.get("invalid"):
            raise PplError("OWNER_DECISION",
                           "run %s control file is unreadable: %s. Fix or remove it to "
                           "continue." % (task_id, record["reason"]))
        if record["command"] in HALTING:
            raise PplError("OWNER_DECISION",
                           "run %s is %sd by the operator since %s. Resume from the panel."
                           % (task_id, record["command"], record["requested_at"]))
    # Reached only when nothing above raised: this call is real control-plane
    # activity (the command is about to proceed), so the inactivity clock
    # restarts from here. See R-WATCHDOG-DEADLINE. `refresh=False` is for a
    # caller with a SECOND gate still ahead of it (require_current_generation,
    # for `ppl review answered`) that can still refuse the very command this
    # enforce() call just cleared - refreshing here would extend the deadline
    # for a write that has not actually succeeded yet, which is the same
    # "refresh before the refusing gate ran" bug this function's own docstring
    # forbids for the generation check (W2, cycle 2 wave B). That caller
    # refreshes itself, once, only after its own check_generation passes.
    if refresh:
        run.refresh_deadline(task_id)

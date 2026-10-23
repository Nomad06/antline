"""SubagentStop usage recorder. It records tokens and never decides anything."""
import json
import os
import sys
import time
from pathlib import Path

# The floor module sits beside this file, and the script's own directory is not
# on sys.path under PYTHONSAFEPATH, -P or -I. Appended, so it shadows nothing.
_HOOKS_DIR = str(Path(__file__).resolve().parent)
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)

import ppl_python_floor

HOOK = "ppl_usage"
EVENT = "SubagentStop"

# Before any ppl import, which needs 3.11 [R-GUARD-PYTHON-FLOOR]. Everything in
# this file parses on 3.9.
if __name__ == "__main__" and ppl_python_floor.refuse_old_python(
        sys.version_info, HOOK, EVENT, sys.stdin, sys.stdout, sys.stderr):
    sys.exit(0)

# parents[3] holds the ppl package this hook ships in, see ppl_dispatch.py for
# why it goes first on sys.path for the import of ppl alone and comes off again.
_PACKAGE_PARENT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PACKAGE_PARENT))
try:
    import ppl
finally:
    sys.path.remove(str(_PACKAGE_PARENT))
_PPL_FILE = getattr(ppl, "__file__", None)
if not _PPL_FILE or not Path(_PPL_FILE).resolve().is_relative_to(_PACKAGE_PARENT):
    sys.stderr.write("ppl_usage: imported ppl from %s, not the one beside this hook in %s\n"
                     % (_PPL_FILE, _PACKAGE_PARENT))

from ppl import boundary, run, store, usage

DEADLINE_SECONDS = 2.0
# the run files the hook reads or appends besides state.json
_RUN_FILES = ("log.jsonl", "usage.json")


def _effort(value):
    """As given when a string, its `level` when a dict, else None."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("level"), str):
        return value["level"]
    return None


def _code(err):
    return err.code if isinstance(err, usage.TranscriptError) else "internal"


def _record_failure(task_id, role, agent_id, err):
    """An error event, and one stderr line too when the failure is not a
    TranscriptError. Only stderr when the log cannot be written."""
    code = _code(err)
    if code == "internal":
        sys.stderr.write("ppl_usage: %s for %s: %s\n" % (type(err).__name__, agent_id, err))
    try:
        store.append_log(task_id, {"event": "usage", "role": role, "agent_id": agent_id,
                                   "error": code})
    except Exception as again:
        sys.stderr.write("ppl_usage: could not log %s for %s: %s\n" % (code, agent_id, again))


def _record_role(task_id, role, payload, deadline):
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        agent_id = None
    try:
        if agent_id is None:
            raise usage.TranscriptError("schema_unknown", "the payload carries no agent_id")
        path = payload.get("agent_transcript_path")
        if not isinstance(path, str) or not path:
            raise usage.TranscriptError("transcript_missing", "the payload names no transcript")
        models = usage.sum_transcript(path, deadline=deadline)
        store.append_log(task_id, {"event": "usage", "role": role, "agent_id": agent_id,
                                   "effort": _effort(payload.get("effort")), "models": models})
    except Exception as err:
        _record_failure(task_id, role, agent_id, err)


def _record_orchestrator(task_id, payload, deadline):
    session = payload.get("session_id")
    try:
        usage.note_orchestrator(task_id, payload.get("transcript_path"), session)
        usage.refresh_orchestrator(task_id, deadline=deadline)
    except Exception as err:
        _record_failure(task_id, "orchestrator", "orchestrator:%s" % session, err)


def on_stop(payload):
    """Record the stopped role's cumulative usage, then the orchestrator's.

    SubagentStop is informational here: the hook only appends `usage` events to
    the active run's log and never returns or prints a decision
    [R-USAGE-HOOK-CANNOT-BLOCK]. Scoped by agent_type like ppl_dispatch.py, a
    role stopping anywhere this hook fires belongs to the one active run. The
    whole call shares one 2 s deadline: a role read past it is an error event,
    an orchestrator read stops early, keeps its offset and logs what it
    counted.
    """
    if not isinstance(payload, dict):
        return None
    role = payload.get("agent_type") or ""
    if role not in boundary.ROLE_RULES:
        return None
    task_id = run.active()
    if not task_id:
        return None
    try:
        state = run.state_path(task_id)
        if not state.is_file():
            return None
        # A FIFO, or anything else that is not a regular file, blocks the open
        # that reads or appends it until Claude Code's timeout, so nothing is
        # recorded then (ppl_guard.log_denial skips such a log the same way).
        for name in _RUN_FILES:
            target = state.parent / name
            if os.path.lexists(target) and not os.path.isfile(target):
                return None
    except Exception:
        return None
    deadline = time.monotonic() + DEADLINE_SECONDS
    _record_role(task_id, role, payload, deadline)
    _record_orchestrator(task_id, payload, deadline)
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        on_stop(payload)
    except Exception as err:
        sys.stderr.write("ppl_usage internal error: %s\n" % err)
    return 0


if __name__ == "__main__":
    sys.exit(main())

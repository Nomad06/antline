"""Stop, SessionEnd and Notification recorder. It records why the orchestrator's session
stopped, as a kind and counts, and never decides anything."""
import json
import os
import re
import sys
from pathlib import Path

# The floor module sits beside this file, and the script's own directory is not
# on sys.path under PYTHONSAFEPATH, -P or -I. Appended, so it shadows nothing.
_HOOKS_DIR = str(Path(__file__).resolve().parent)
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)

import ppl_python_floor

HOOK = "ppl_session"

# Before any ppl import, which needs 3.11. Everything in this file parses on 3.9.
# ppl_python_floor.refuse_old_python is not called: in a lane it writes a
# permissionDecision document for any call that is not a read tool, and a Stop hook
# prints nothing [R-SESSION-HOOK-CANNOT-BLOCK]. One stderr line, exit 0, nothing recorded.
if __name__ == "__main__" and tuple(sys.version_info[:2]) < tuple(ppl_python_floor.MINIMUM):
    try:
        sys.stderr.write("%s: Python %s is older than the %s antline needs, so the session "
                         "is not recorded: start the lane with `ppl lane` under Python %s or "
                         "newer\n" % (HOOK, ".".join(str(part) for part in sys.version_info[:3]),
                                      "%d.%d" % tuple(ppl_python_floor.MINIMUM),
                                      "%d.%d" % tuple(ppl_python_floor.MINIMUM)))
    except Exception:
        pass
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
    sys.stderr.write("ppl_session: imported ppl from %s, not the one beside this hook in %s\n"
                     % (_PPL_FILE, _PACKAGE_PARENT))

from ppl import lanes, run, store

# hook_event_name -> the kind a session event records
KINDS = {"Stop": "stop", "SessionEnd": "end", "Notification": "notification"}

# A reason or a notification type is kept only in this shape: a plain word of Claude
# Code's own vocabulary, never a sentence that could carry text [R-SESSION-EVENT-NO-TEXT].
_PLAIN_WORD = re.compile(r"[a-z_]{1,40}")


def _plain(value):
    """`value` when it is a plain word (_PLAIN_WORD, the whole string), else None."""
    if isinstance(value, str) and _PLAIN_WORD.fullmatch(value):
        return value
    return None


def _running(tasks):
    """How many entries of a background_tasks list are objects whose status is "running",
    None when it is not a list."""
    if not isinstance(tasks, list):
        return None
    return sum(1 for task in tasks if isinstance(task, dict) and task.get("status") == "running")


def _event(kind, payload):
    """The session event for `kind`: the kind and counts or one plain word, nothing else of
    the payload, so neither the last assistant message, a notification's message nor a
    background task's description reaches the log [R-SESSION-EVENT-NO-TEXT]."""
    if kind == "stop":
        crons = payload.get("session_crons")
        return {"event": "session", "kind": kind,
                "background": _running(payload.get("background_tasks")),
                "crons": len(crons) if isinstance(crons, list) else None}
    if kind == "end":
        return {"event": "session", "kind": kind, "reason": _plain(payload.get("reason"))}
    return {"event": "session", "kind": kind, "type": _plain(payload.get("notification_type"))}


def on_event(payload, argv=()):
    """Append one `session` event to the log of the run the hook's command line `argv`
    binds it to (`--task <id>`, lanes.binding).

    Stop, SessionEnd and Notification are recorded so a later reader can tell a run whose
    orchestrator stopped from one still working. Nothing is recorded outside a lane, in a
    lane the registry cannot confirm, for a run whose state.json is gone, or for an unknown
    hook_event_name, and the hook never returns or prints a decision
    [R-SESSION-HOOK-CANNOT-BLOCK].
    """
    if not isinstance(payload, dict):
        return None
    kind = KINDS.get(payload.get("hook_event_name"))
    if kind is None:
        return None
    bound = lanes.binding(argv)
    if bound.problem or not bound.task:
        return None
    task_id = bound.task
    try:
        state = run.state_path(task_id)
        if not state.is_file():
            return None
        # A FIFO, or anything else that is not a regular file, blocks the open that
        # appends it until Claude Code's timeout, so nothing is recorded then
        # (ppl_usage.py skips such a log the same way).
        target = state.parent / "log.jsonl"
        if os.path.lexists(target) and not os.path.isfile(target):
            return None
    except Exception:
        return None
    try:
        store.append_log(task_id, _event(kind, payload))
    except Exception as err:
        sys.stderr.write("ppl_session: could not log the %s event: %s\n" % (kind, err))
    return None


def main():
    # the lane binding is this hook's own command line, read after the python floor
    argv = sys.argv[1:]
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        on_event(payload, argv)
    except Exception as err:
        sys.stderr.write("ppl_session internal error: %s\n" % err)
    return 0


if __name__ == "__main__":
    sys.exit(main())

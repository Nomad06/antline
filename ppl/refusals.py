"""A role's output that `ppl` refuses SCHEMA_INVALID goes back to the role once. The
refusals of the output itself (errors.role_output) are counted per command, role and
lease generation in the run's log.jsonl: the second in a row parks the run with
FORMAT_RETRY_EXHAUSTED, and a later call for the same key is refused before its input is
read [R-REFUSAL-RETRY-CAP]. A refusal of how the command was called is logged, not
counted."""
from . import routing, run, store
from .errors import PplError

# The commands that record a role's output, as "<command> <action>".
COMMANDS = ("brief set", "brief challenge", "verdict record", "findings add", "evidence add")
ROLES = routing.REQUIRED_ROLES + ("orchestrator",)
# The --from value a refusal for a missing one names.
EXPECTED_ROLE = {"brief set": "ppl-briefer", "brief challenge": "ppl-brief-challenger",
                 "verdict record": "ppl-verifier", "findings add": "ppl-verifier",
                 "evidence add": "orchestrator"}
CAP = 2


def _counted(task_id, command, role, generation):
    """The counted refusals for this key since the run last resumed from parking or
    accepted a call for the key: a resume clears the count like any park, an accepted
    call ends a run of refusals, and a new lease generation is a new key."""
    found = []
    for event in store.read_log(task_id):
        name = event.get("event")
        if name == "stage_enter" and event.get("from") == "parking":
            found = []
        elif (name in ("refusal", "role_output_accepted") and event.get("command") == command
              and event.get("role") == role and event.get("lease_generation") == generation):
            if name == "role_output_accepted":
                found = []
            elif event.get("counted"):
                found.append(event)
    return found


def _generation(task_id):
    return run.load(task_id).get("lease_generation", 1)


class CountUnreadable(Exception):
    """The log the count is read from could not be read, so the cap cannot be applied."""


def _park(task_id, counted):
    run.park(task_id, "FORMAT_RETRY_EXHAUSTED",
             "inspect both refusals in parking.json, fix the role prompt or the schema, then "
             "resume - never just redispatch a third time", [],
             refusals=[{key: event.get(key) for key in ("command", "role", "message", "at")}
                       for event in counted])


def require_retry_left(task_id, command, role):
    """Refuse a spent key, parking the run first when the park that spent it never
    happened. Raises CountUnreadable when the log cannot be read."""
    state = run.load(task_id)
    generation = state.get("lease_generation", 1)
    try:
        counted = _counted(task_id, command, role, generation)
    except OSError as exc:
        raise CountUnreadable(str(exc) or type(exc).__name__) from exc
    if len(counted) < CAP:
        return
    if state.get("stage") != "parking":
        _park(task_id, counted)
    raise PplError("FORMAT_RETRY_EXHAUSTED",
                   "`ppl %s` from %s was refused SCHEMA_INVALID twice in lease generation "
                   "%d and the run is parked, see parking.json. Fix the role prompt or the "
                   "schema, then resume: a third attempt is refused [R-REFUSAL-RETRY-CAP]"
                   % (command, role, generation))


def record_refusal(task_id, command, role, message, of_role_output):
    """Log one refusal. The second refusal of the role's output in a row for the same key
    parks the run with both messages, and the returned sentence says so to the caller.
    `role` is None when the call named none, which is never counted."""
    generation = _generation(task_id)
    store.append_log(task_id, {"event": "refusal", "command": command, "code": "SCHEMA_INVALID",
                               "message": message, "role": role,
                               "lease_generation": generation, "counted": bool(of_role_output)})
    if not of_role_output:
        return None
    counted = _counted(task_id, command, role, generation)
    if len(counted) < CAP:
        return None
    _park(task_id, counted)
    return ("the run is now parked with FORMAT_RETRY_EXHAUSTED: this is refusal %d in a row of "
            "`ppl %s` from %s in lease generation %d, see parking.json"
            % (len(counted), command, role, generation))


def record_acceptance(task_id, command, role):
    """Mark an accepted call, only when it ends a run of counted refusals, so the log
    does not grow on every ordinary call."""
    generation = _generation(task_id)
    if _counted(task_id, command, role, generation):
        store.append_log(task_id, {"event": "role_output_accepted", "command": command,
                                   "role": role, "lease_generation": generation})

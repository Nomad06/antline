"""The interpreter floor every hook checks before anything from ppl is imported.

The hooks and ppl need Python 3.11 (tomllib, possessive and atomic regex
syntax). Under an older interpreter a hook used to die on import, and Claude
Code reads a non-zero exit other than 2 as a non-blocking error: every tool
call allowed, the cage disarmed with nothing saying so. This module is kept to
syntax and calls Python 3.9 runs, since running there is its whole job.
"""
import json

MINIMUM = (3, 11)


def refuse_old_python(version_info, hook, event, stdin, stdout, stderr):
    """False, touching nothing, when `version_info` meets MINIMUM. Otherwise the
    payload is read, a call whose agent_type starts with ppl- is denied on
    `stdout`, one line naming the found version goes to `stderr`, and True tells
    the caller to exit 0.

    boundary.ROLE_RULES cannot be imported here, so a role is told apart by the
    ppl- prefix, a superset of the generated roles: the cage fails closed. An
    unroled session is allowed, so an old interpreter cannot lock the operator
    out of unrelated sessions [R-GUARD-PYTHON-FLOOR].
    """
    if tuple(version_info[:2]) >= MINIMUM:
        return False
    found = ".".join(str(part) for part in tuple(version_info[:3]))
    floor = "%d.%d" % MINIMUM
    try:
        payload = json.load(stdin)
    except Exception:
        payload = None
    agent = payload.get("agent_type") if isinstance(payload, dict) else None
    if isinstance(agent, str) and agent.startswith("ppl-"):
        stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "BOUNDARY_VIOLATION %s runs on Python %s and needs %s or newer, so the role "
                "call is refused. Run `ppl setup claude` under Python %s or newer to pin "
                "that interpreter in the hook entries." % (hook, found, floor, floor)}}))
    try:
        stderr.write("%s: Python %s is older than the %s antline needs, run `ppl setup claude` "
                     "under Python %s or newer\n" % (hook, found, floor, floor))
    except Exception:
        pass
    return True

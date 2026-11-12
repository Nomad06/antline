"""The interpreter floor every hook checks before anything from ppl is imported.

The hooks and ppl need Python 3.11 (tomllib, possessive and atomic regex
syntax). Under an older interpreter a hook used to die on import, and Claude
Code reads a non-zero exit other than 2 as a non-blocking error: every tool
call allowed, the cage disarmed with nothing saying so. This module is kept to
syntax and calls Python 3.9 runs, since running there is its whole job.
"""
import json

MINIMUM = (3, 11)

# What an unroled session of a lane keeps when the guard cannot decide, under an old
# interpreter or on an internal error, the tools a lane that cannot be confirmed keeps
# (ppl_guard.UNCONFIRMED_LANE_TOOLS, which this module cannot import) [R-GUARD-PYTHON-FLOOR].
LANE_READ_TOOLS = ("Read", "Glob", "Grep")


def in_lane(argv):
    """True when a hook's command line carries --task, as ppl lane renders it into every hook
    entry of a lane: hooks exist only in lane sessions, so there an old interpreter or a
    guard defect can lock out no unrelated session. Read by its words alone, so a malformed
    one counts as a lane and the guard fails closed."""
    return any(isinstance(arg, str) and (arg == "--task" or arg.startswith("--task="))
               for arg in argv)


def refuse_old_python(version_info, hook, event, stdin, stdout, stderr, argv=()):
    """False, touching nothing, when `version_info` meets MINIMUM. Otherwise the
    payload is read, a call whose agent_type starts with ppl- is denied on
    `stdout`, and in a lane (`argv`, the hook's command line, carries --task) so is
    every unroled call but Read, Glob and Grep, one line naming the found version
    goes to `stderr`, and True tells the caller to exit 0.

    boundary.ROLE_RULES cannot be imported here, so a role is told apart by the
    ppl- prefix, a superset of the generated roles: the cage fails closed. Outside a
    lane an unroled session is allowed, so an old interpreter cannot lock the
    operator out of unrelated sessions [R-GUARD-PYTHON-FLOOR].
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
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    role = isinstance(agent, str) and agent.startswith("ppl-")
    if role or (in_lane(argv) and tool not in LANE_READ_TOOLS):
        refused = ("the role call is refused" if role else
                   "in a lane only Read, Glob and Grep are allowed")
        stdout.write(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason":
                "BOUNDARY_VIOLATION %s runs on Python %s and needs %s or newer, so %s. The "
                "lane's hook entries pin the interpreter that ran `ppl lane`: the owner starts "
                "the lane under Python %s or newer." % (hook, found, floor, refused, floor)}}))
    try:
        stderr.write("%s: Python %s is older than the %s antline needs: start the lane with "
                     "`ppl lane` under Python %s or newer, which pins that interpreter in its "
                     "hook entries\n" % (hook, found, floor, floor))
    except Exception:
        pass
    return True

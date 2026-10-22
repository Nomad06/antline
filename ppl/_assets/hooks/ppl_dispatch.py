"""SubagentStart counter. The ppl refusal is the guarantee, this is the early cut."""
import json
import sys
from pathlib import Path

# The floor module sits beside this file, and the script's own directory is not
# on sys.path under PYTHONSAFEPATH, -P or -I. Appended, so it shadows nothing.
_HOOKS_DIR = str(Path(__file__).resolve().parent)
if _HOOKS_DIR not in sys.path:
    sys.path.append(_HOOKS_DIR)

import ppl_python_floor

# Before any ppl import, which needs 3.11 [R-GUARD-PYTHON-FLOOR]. Everything in
# this file parses on 3.9.
if __name__ == "__main__" and ppl_python_floor.refuse_old_python(
        sys.version_info, "ppl_dispatch", "SubagentStart", sys.stdin, sys.stdout, sys.stderr):
    sys.exit(0)

# parents[3] holds the ppl package this hook ships in. It goes first on sys.path
# for the import of ppl alone, so that copy wins over any other installed ppl,
# and comes off again before anything else is imported. Installed, parents[3]
# is site-packages, where a top-level module named like a stdlib one (an old
# backport) would shadow the stdlib for every later import and crash the hook,
# and Claude Code allows the call when a hook crashes. The modules of ppl
# resolve through ppl.__path__, not sys.path.
_PACKAGE_PARENT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_PACKAGE_PARENT))
try:
    import ppl
finally:
    sys.path.remove(str(_PACKAGE_PARENT))
_PPL_FILE = getattr(ppl, "__file__", None)
if not _PPL_FILE or not Path(_PPL_FILE).resolve().is_relative_to(_PACKAGE_PARENT):
    sys.stderr.write("ppl_dispatch: imported ppl from %s, not the one beside this hook in %s\n"
                     % (_PPL_FILE, _PACKAGE_PARENT))

from ppl import boundary, budget, run
from ppl.errors import PplError


def on_start(payload):
    """Count every ppl-* role dispatch against the active run's dispatch budget.

    Returns a deny document if the dispatch exceeds the hard budget cap -
    informational only, SubagentStart cannot block (DESIGN 12.1,
    R-DISPATCH-HOOK-CANNOT-BLOCK). Returns None for all other cases - no
    active run, an agent_type that is not a pipeline role, or errors reading
    the state file.

    Scoped by agent_type, not by cwd (post-branch review, finding 2). This
    used to count only when the payload's cwd sat inside the run's repo, and
    the documented layout puts the orchestrator's session - whose cwd every
    subagent inherits - BESIDE the worktree, not under it (DESIGN 5.4, README
    "The isolated launch directory"). So in exactly the prescribed arrangement
    the counter never moved, and `ppl stage enter`'s hard-cap check
    (ppl/cli.py:check_dispatch_budget, self-described as "the whole of" the
    enforcement) compared against zero for the life of the run - the same bug
    class cycle 1 fixed for the write boundary. A ppl-* agent_type is
    pipeline-internal wherever its session sits (ppl/_assets/hooks/ppl_guard.py:decide
    already scopes on that), and ppl runs one active run at a time, so a role
    dispatch anywhere this hook fires is this run's dispatch.
    """
    if not isinstance(payload, dict):
        return None

    agent = payload.get("agent_type") or ""
    if agent not in boundary.ROLE_RULES:
        return None

    task_id = run.active()
    if not task_id:
        return None

    try:
        state = run.load(task_id)
    except (PplError, OSError, ValueError):
        # Fail open if state is unreadable: we cannot spend a budget we cannot read,
        # and ppl budget refusing the next transition is the real guarantee. The early
        # cut here is best-effort only.
        return None

    if not isinstance(state, dict):
        return None

    # Dispatch budget is incremented on SubagentStart, not completion - a subagent that
    # crashes immediately still consumes budget, which is intended for surfacing runaway
    # loops. Note: the increment is not atomic against concurrent SubagentStart events,
    # but this is accepted while ppl runs a single sequential lane. ppl budget refusing
    # the next transition is the real guarantee.
    try:
        budget.spend(task_id, "dispatch", 1)
    except PplError as err:
        return {"hookSpecificOutput": {
            "hookEventName": "SubagentStart",
            "permissionDecision": "deny",
            "permissionDecisionReason": "%s %s" % (err.code, err.message)}}
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    try:
        result = on_start(payload)
    except Exception as err:
        sys.stderr.write("ppl_dispatch internal error: %s\n" % err)
        return 0
    if result:
        sys.stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

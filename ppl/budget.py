from . import run, store
from .errors import PplError

KINDS = {"dispatch": ("dispatch_used", "dispatch_hard"),
         "retry": ("retry_used", "retry_max"),
         "usd": ("usd_used", "usd_max")}


def spend(task_id, kind, amount=1):
    if kind not in KINDS:
        raise PplError("SCHEMA_INVALID", "unknown budget %r" % kind)
    used_key, cap_key = KINDS[kind]
    state = run.load(task_id)
    budgets = state["budgets"]
    proposed = budgets[used_key] + amount
    cap = budgets[cap_key]
    unlimited = kind == "usd" and cap == 0
    if not unlimited and proposed > cap:
        raise PplError("BUDGET_EXHAUSTED",
                       "%s budget exceeded: %s of %s. Park the task with a handoff instead "
                       "of continuing." % (kind, proposed, cap))
    budgets[used_key] = proposed
    run.save(state)
    store.append_log(task_id, {"event": "budget", "kind": kind, "used": proposed, "cap": cap})
    return state


def remaining(state, kind):
    used_key, cap_key = KINDS[kind]
    return state["budgets"][cap_key] - state["budgets"][used_key]

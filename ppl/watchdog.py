"""The watchdog: the only timer this pipeline has, and it runs in the
control process, not in the stuck agent.

It measures inactivity, not stage duration - total work is already capped by
the dispatch/retry budgets in `ppl/run.py`, so wall-clock-per-stage was never
this module's job. `ppl/run.py:stage_enter` (and `_new_state`, for admission)
stamps a `deadline` on the state every time a stage is entered;
`run.refresh_deadline` restamps it on ordinary control-plane activity short
of a full stage transition (`control.enforce`, `require_current_generation`,
and a not-already-timed-out `recover.reconcile` - see each of their own
comments for exactly where in their sequence, and DESIGN 6.9 for why those
three and not others). `check` compares the deadline to the current time and,
past it with nothing having refreshed it, parks the run with blocker
`TIMED_OUT` and bumps the lease generation - the same fence a returning stale
attempt trips over (`R-LEASE-STALE-WRITE`), for the same reason: if the hung
attempt wakes up and writes anyway, it is still holding the generation this
call left behind.

This module has no loop of its own. See DESIGN 6.9 for the honest limit
that follows from that: it only fires when something calls it.
"""
from . import run, store


def check(task_id):
    """Park `task_id` with TIMED_OUT if no activity has refreshed its
    deadline in time; otherwise a no-op.

    Idempotent by construction, not by a guard added on top: once a timed-out
    run is parked its stage becomes "parking", and a deadline is only ever
    read (never re-checked) for a stage that owns one - so a second call
    (control.enforce and require_current_generation both call this, and
    recover.reconcile calls it too, all on the same command) finds nothing
    left to do and returns `timed_out: False` without touching the run again.
    """
    state = run.load(task_id)
    deadline = state.get("deadline")
    # parking: already the terminal-ish state a timeout itself produces -
    # nothing to overrun a second time. escalation: a distinct legal stage
    # (fsm.STAGES, reachable directly with `ppl stage enter escalation`,
    # never via `ppl escalate` - that command's run.park always lands on
    # "parking" instead, see DESIGN 6.9's note on that mismatch) that already
    # means "waiting on the owner" - ppl/panel/server.py's needs-you check
    # treats it the same way parking is treated, so parking it a second time
    # over it would relabel one owner-facing wait as another, not surface
    # anything new. merge_watch: the optional post-done observational tail
    # (DESIGN 6.9, R-MERGE-WATCH-TERMINAL) - the lifecycle is already
    # terminal and parking a tail that only watches for a human merge would
    # serve nothing.
    if not deadline or state["stage"] in ("parking", "escalation", "merge_watch"):
        return {"timed_out": False}
    if store.now() < deadline:
        return {"timed_out": False}
    run.park(task_id, "TIMED_OUT",
             "there has been no ppl activity on this run since %s. An owner should inspect "
             "the stuck attempt - what it last wrote, whether it is still running - before "
             "anything resumes; do not just re-dispatch into the same stage."
             % state.get("last_activity_at", "an unknown time"),
             [])
    generation = run.bump_generation(task_id)
    return {"timed_out": True, "lease_generation": generation}

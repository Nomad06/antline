from . import control, run

TEMPLATE = """Task:     {task_id}  {tracker_url}
Outcome:  {outcome}
State:    {stage} ({status}){paused}
Progress: {proven}/{total} acceptance criteria proven
Proof:    {last_fact}
Risk:     {risk}
Models:   {models}
Budget:   dispatch {d_used}/{d_hard}, retry {r_used}/{r_max}, {usd}
MR:       {mr}
Needs you: {needs}
Next:     {next_action}
"""


def card(task_id):
    state = run.load(task_id)
    from . import verdict
    acceptance = state.get("acceptance", [])
    # The done-gate's own proven set, not a receipt count: the two are different
    # numbers and the card used to print totals like 3/1 out of the difference.
    proven = len(verdict.proven_acceptance(task_id, state.get("candidate_sha") or "")
                 & set(acceptance))
    command = control.read(task_id)
    paused = ""
    needs = "no"
    if command and command.get("invalid"):
        paused = "  CONTROL FILE INVALID"
        needs = "yes - %s" % command["reason"]
    elif command and command["command"] in control.HALTING:
        paused = "  PAUSED by operator"
        needs = "yes - run is %sd from the panel" % command["command"]
    # E3, cycle 2 wave E: this used to read state["status"] == "blocked"
    # only. `ppl park` demotes status to "blocked" solely from "in_progress"
    # (wave B's W3 fix, kept deliberately narrow so a run parked before or
    # after in_progress does not fabricate activity that never happened -
    # that reasoning is about the LIFECYCLE STATUS and stays right). "Needs
    # you" is a different signal and must not inherit that narrowness: a run
    # parked at "planned" or at "done" still has an unanswered blocker
    # (OWNER_DECISION included, since `ppl escalate` is `run.park` under the
    # other name) sitting invisible on the one surface built so a human
    # notices it. `state["stage"]` is the fact that is always true whenever
    # a run is waiting on someone, regardless of lifecycle status:
    # `ppl/panel/server.py:_needs_you` already checks stage this way - this
    # brings `ppl show`'s own card into agreement with it instead of leaving
    # the two surfaces answering "needs you" differently for the same run.
    if state["stage"] == "escalation":
        needs = "yes - task is escalated, awaiting the owner's answer"
    elif state["stage"] == "parking":
        needs = "yes - task is parked, see parking.json"
    elif state["status"] == "blocked":
        needs = "yes - task is blocked, see parking.json"
    budgets = state["budgets"]
    usd_max = budgets["usd_max"]
    # usd_max of 0 means unlimited by design (a subscription provider has no
    # dollar meter, see DESIGN.md 12.1 and budget.spend's own unlimited
    # check), so say "no cap" rather than a bare number that reads as a cap.
    if usd_max:
        usd = "usd %s/%s" % (budgets["usd_used"], usd_max)
    else:
        usd = "usd %s (no cap)" % budgets["usd_used"]
    return TEMPLATE.format(
        task_id=state["task_id"], tracker_url=state.get("tracker_url") or "",
        outcome=state.get("last_durable_fact") or "not started",
        stage=state["stage"], status=state["status"], paused=paused,
        proven=proven, total=len(acceptance),
        last_fact=state.get("last_durable_fact") or "none",
        risk=state.get("risk", "normal"),
        models=state.get("model_check", "verified"),
        d_used=budgets["dispatch_used"], d_hard=budgets["dispatch_hard"],
        r_used=budgets["retry_used"], r_max=budgets["retry_max"],
        usd=usd,
        mr=(state.get("mr") or {}).get("url") or "not opened",
        needs=needs, next_action=state.get("next_safe_action") or "unknown",
    )

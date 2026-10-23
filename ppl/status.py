import math

from . import control, paths, run, usage
from .errors import PplError

TEMPLATE = """Task:     {task_id}  {tracker_url}
Outcome:  {outcome}
State:    {stage} ({status}){paused}
Progress: {proven}/{total} acceptance criteria proven
Proof:    {last_fact}
Risk:     {risk}
Models:   {models}
Tokens:   {tokens}
Budget:   dispatch {d_used}/{d_hard}, retry {r_used}/{r_max}, {usd}
MR:       {mr}
Needs you: {needs}
Next:     {next_action}
"""


def _short(count):
    if count < 1000:
        return str(count)
    thousands = round(count / 1000, 1 if count < 10000 else 0)
    if thousands < 1000:
        return "%sk" % (("%.1f" % thousands) if thousands < 10 else int(thousands))
    millions = round(count / 1000000, 1 if count < 10000000 else 0)
    return "%sM" % (("%.1f" % millions) if millions < 10 else int(millions))


def tokens_line(task_id):
    roles = usage.run_usage(paths.run_dir(task_id))
    if not roles:
        return "none recorded"
    names = sorted((role for role in roles if role != "orchestrator"),
                   key=lambda role: role[4:] if role.startswith("ppl-") else role)
    if "orchestrator" in roles:
        names.append("orchestrator")
    parts = []
    output = 0
    for role in names:
        counts = {}
        for model_counts in roles[role].values():
            for name in usage.COUNTS:
                counts[name] = counts.get(name, 0) + model_counts.get(name, 0)
        output += counts.get("output", 0)
        label = role[4:] if role.startswith("ppl-") else role
        parts.append("%s %s" % (label, _short(usage.total(counts))))
    return "%s (out %s)" % (", ".join(parts), _short(output))


def _live_spent(task_id, used):
    """spent_usd priced with the live config.toml prices instead of the frozen ones."""
    if not usage.valid_usd_used(used):
        return math.inf, []
    priced, unpriced = usage.run_dollars(paths.run_dir(task_id))
    if unpriced:
        return None, unpriced
    return used + (priced or 0.0), []


def usd_text(task_id):
    """A capped run reads what the cap reads, the prices frozen at ppl init only. An
    uncapped run reads what the trail reads, the frozen prices, or the live config.toml
    prices when ppl init froze none."""
    budgets = run.load(task_id)["budgets"]
    # usd_max of 0 means unlimited by design (a subscription provider has no
    # dollar meter), so say "no cap" rather than a bare number that reads as a cap
    limit = "/%s" % budgets["usd_max"] if budgets["usd_max"] else " (no cap)"
    frozen = budgets.get("prices")
    if budgets["usd_max"] or (isinstance(frozen, dict) and frozen):
        spent, unpriced = usage.spent_usd(task_id)
        where = "not in the prices frozen at ppl init"
    else:
        try:
            spent, unpriced = _live_spent(task_id, budgets.get("usd_used"))
        except PplError as exc:
            return "usd unknown%s, %s" % (limit, exc.message)
        where = "not in config.toml [prices]"
    if unpriced:
        text = "usd %s%s + unpriced %s, %s" % (budgets["usd_used"], limit,
                                               ", ".join(unpriced), where)
    elif spent == math.inf:
        text = "usd unknown (usd_used is not a number >= 0)"
    else:
        text = "usd %s%s" % (round(spent, 4), limit)
    if not usage.run_usage(paths.run_dir(task_id)):
        text += ", no usage recorded"
    return text


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
        usd=usd_text(task_id), tokens=tokens_line(task_id),
        mr=(state.get("mr") or {}).get("url") or "not opened",
        needs=needs, next_action=state.get("next_safe_action") or "unknown",
    )

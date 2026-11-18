"""Where a run's time went, computed from its event log alone.

The log is the only source: nothing here reads the clock or the run's other files, so the same
log always gives the same report. A figure the log cannot support is None with an entry in
`reasons`, never 0, so a reader can tell "took no time" from "not measured".
"""

import json
from datetime import datetime

IDLE_GAP_SECONDS = 45 * 60

# Events that are not part of the run's own timeline. A stall note is written in the middle of
# an idle gap, and counting it would split the gap in two and book the first part as work.
SKIPPED_EVENTS = frozenset({"stall"})


def _parse(events):
    """(name, datetime, event) for each usable item, in log order."""
    parsed = []
    for item in events:
        if not isinstance(item, dict):
            continue
        name = item.get("event")
        at = item.get("at")
        if not isinstance(name, str) or name in SKIPPED_EVENTS or not isinstance(at, str):
            continue
        try:
            when = datetime.fromisoformat(at)
        except ValueError:
            continue
        if when.tzinfo is None:
            # The log stamps UTC offsets, and a naive time cannot be subtracted from them.
            continue
        if name == "session":
            name = "session:%s" % item.get("kind")
        parsed.append((name, when, item))
    return parsed


def report(events, idle_gap_seconds=IDLE_GAP_SECONDS):
    parsed = _parse(events)
    reasons = {}
    stages = {}
    idle_gaps = []
    gates = {}
    starts = {}
    dispatch_count = {}
    dispatch_times = {}
    saw_start = False
    saw_budget_dispatch = False
    init_at = None
    mr_at = None
    stage = "admission"
    stages_open = False

    def stage_row(name):
        return stages.setdefault(name, {"entries": 0, "seconds": 0.0, "idle_seconds": 0.0})

    prev_name = prev_at = None
    for name, when, item in parsed:
        if name == "init" and init_at is None:
            init_at = when
        if name == "mr" and mr_at is None:
            mr_at = when
        if not stages_open:
            stage_row(stage)["entries"] += 1
            stages_open = True
        gap = 0.0
        idle = False
        if prev_at is not None:
            gap = (when - prev_at).total_seconds()
            idle = gap > idle_gap_seconds
            row = stage_row(stage)
            if idle:
                row["idle_seconds"] += gap
                idle_gaps.append({"from": prev_at.isoformat(), "to": when.isoformat(),
                                  "seconds": gap, "stage": stage, "after": prev_name,
                                  "ended_by": name})
            else:
                row["seconds"] += gap
        if name == "stage_enter" and isinstance(item.get("to"), str):
            stage = item["to"]
            stage_row(stage)["entries"] += 1
        elif name == "evidence" and isinstance(item.get("check"), str):
            gate = gates.setdefault(item["check"], {"runs": 0, "wait_seconds": 0.0})
            gate["runs"] += 1
            gate["wait_seconds"] += 0.0 if idle else gap
        elif name == "dispatch_start":
            saw_start = True
            role = item.get("role") or "unknown"
            dispatch_count[role] = dispatch_count.get(role, 0) + 1
            agent = item.get("agent_id")
            if agent:
                starts.setdefault(agent, []).append((role, when))
        elif name == "usage":
            agent = item.get("agent_id")
            if agent and starts.get(agent):
                role, began = starts[agent].pop(0)
                dispatch_times.setdefault(role, []).append((when - began).total_seconds())
        elif name == "budget" and item.get("kind") == "dispatch":
            saw_budget_dispatch = True
        prev_name, prev_at = name, when

    dispatches = {}
    for role, count in dispatch_count.items():
        times = dispatch_times.get(role)
        dispatches[role] = {"count": count, "seconds": sum(times) if times else None,
                            "longest_seconds": max(times) if times else None}
    if saw_budget_dispatch and not saw_start:
        reasons["dispatches"] = "the log predates the dispatch_start event"

    wall = init_to_mr = None
    if init_at is None:
        reasons["wall_clock_seconds"] = "the log has no init event"
        reasons["init_to_mr_seconds"] = "the log has no init event"
    else:
        wall = (prev_at - init_at).total_seconds()
        if mr_at is None:
            reasons["init_to_mr_seconds"] = "the run has no mr event yet"
        else:
            init_to_mr = (mr_at - init_at).total_seconds()
    return {"wall_clock_seconds": wall, "init_to_mr_seconds": init_to_mr, "stages": stages,
            "dispatches": dispatches, "gates": gates, "idle_gaps": idle_gaps, "reasons": reasons}


def _fmt(seconds):
    if seconds is None:
        return "-"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh%02dm%02ds" % (hours, minutes, secs)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs


def text(rep):
    lines = ["wall clock (init to last event): %s" % _fmt(rep["wall_clock_seconds"]),
             "init to merge request: %s" % _fmt(rep["init_to_mr_seconds"]), "",
             "stage            entries  working     idle"]
    for name, row in rep["stages"].items():
        lines.append("%-16s %7d  %-10s  %s" % (name, row["entries"], _fmt(row["seconds"]),
                                               _fmt(row["idle_seconds"])))
    lines += ["", "dispatch role       count  total       longest"]
    for role, row in rep["dispatches"].items():
        lines.append("%-19s %5d  %-10s  %s" % (role, row["count"], _fmt(row["seconds"]),
                                               _fmt(row["longest_seconds"])))
    lines += ["", "gate: wait before receipt (includes the orchestrator's own time)",
              "gate                 runs  wait"]
    for check, row in rep["gates"].items():
        lines.append("%-20s %5d  %s" % (check, row["runs"], _fmt(row["wait_seconds"])))
    lines += ["", "idle gaps"]
    for gap in rep["idle_gaps"]:
        lines.append("%s  %s in %s, after %s, ended by %s" % (
            gap["from"], _fmt(gap["seconds"]), gap["stage"], gap["after"], gap["ended_by"]))
    if rep["reasons"]:
        lines.append("")
    for key, reason in rep["reasons"].items():
        lines.append("%s: - (%s)" % (key, reason))
    return "\n".join(lines)


def as_json(rep):
    return json.dumps(rep, indent=2, sort_keys=True)

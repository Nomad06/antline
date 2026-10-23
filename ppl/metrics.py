"""Fleet metrics and human labels.

`ppl metrics` reads every run's `log.jsonl` and `verdicts/` (the latest verdict per
SHA, and for the incomplete check rate every recorded one) and computes
nine numbers, sliced by `producer`: defect escape rate, outcompeted rate,
false FAIL rate, incomplete check rate, cost per accepted task, tokens per
accepted task, tokens by role, time to acceptance, human minutes.

The one rule this module exists to hold: **a metric with no data prints that
it is undefined, and never prints zero.** Zero is a claim ("we shipped no
defects") - undefined is the truth when nobody has told the pipeline whether
it did. Every metric below returns an envelope - `{"value": ..., "defined":
bool, "reason": ...}` - and `ppl metrics` prints that whole envelope as JSON,
so `value` is `null` and `reason` explains why whenever `defined` is false,
never a bare 0. This is deliberately the same shape whether the absence is
temporary (no labels filed yet) or a gap (nothing records it at all):

- **defect escape rate**, **outcompeted rate** and **false FAIL rate** need a
  human label (`ppl label`) on top of a verdict - undefined until at least one exists
  [R-METRIC-LABEL-DEPENDENT-UNDEFINED].
- **incomplete check rate** and **time to acceptance** need at least one
  verdict, respectively one completed (`done`) task - undefined on an empty
  fleet or a fleet with nothing accepted yet [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED].
- **cost per accepted task** is the mean dollars over the accepted (`done`)
  runs that have recorded usage, priced with the live `[prices]` table of
  `config.toml`. It is undefined while any model in those runs has no price,
  and the reason names the model [R-METRIC-COST-NEEDS-PRICES].
- **tokens per accepted task** and **tokens by role** are the mean of the four
  token counts per accepted run, and the counts summed per role over those
  runs. They need no prices but do need the usage hook (`ppl setup claude`),
  so they are undefined until an accepted run has recorded usage
  [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED].
- **human minutes** is undefined always: it IS buildable (a `--minutes` flag on `ppl label`, or a
  dedicated command) and simply has not been - no event or field anywhere in
  this pipeline records a human's working minutes today (a human's *wait*
  time is inferable from timestamps - effort minutes are a different claim
  and are simply absent) [R-METRIC-HUMAN-MINUTES-UNDEFINED].

`ppl label --sha X --actual defective|correct` records the one fact this
system cannot observe on its own: whether a given candidate SHA, in
hindsight, was actually correct or actually defective. It deliberately does
NOT go through `control.enforce` or `require_current_generation`
[R-LABEL-NO-LIVENESS-REFRESH] [R-LABEL-NO-LEASE-FENCE] - see this module's
`record_label` for both rulings.

**Every reader here scans `runs/_archive/` as well as the live runs under
`runs/`, and labels live outside the run directory entirely**
[R-METRIC-SCANS-ARCHIVE]. Defect escape and false FAIL only become knowable
after a task is actually merged, and the normal flow is `done -> merge_watch
-> ppl destroy --scope local`, which moves a run's whole directory into
`runs/_archive/<mangled-name>` - after that point the ground truth this
metric exists to hold arrives, and it must still be recordable and still be
visible. A label store nested inside `runs/<task_id>/` would be moved along
with everything else on destroy, and `record_label`'s own existence check
(`run.load`) would then refuse to find a task it had just archived - the
metric would report "undefined, run `ppl label`" forever, over advice nobody
could act on. Labels are therefore kept at `PPL_HOME/labels/<task_id>/<sha>.json`,
untouched by `ppl destroy`, and every scan below walks both `runs/*` and
`runs/_archive/*` rather than only the former.
"""
import re
from datetime import datetime

from . import paths, schema, store, usage
from .errors import PplError

# "outcompeted": the accepted candidate was correct but a parallel
# implementation of the same task was judged better (leaner, more complete)
# and replaced it. A design-quality loss, not a defect, and invisible to
# defect escape [R-METRIC-OUTCOMPETED].
LABEL_VALUES = ("defective", "correct", "outcompeted")

_SHA_RE = re.compile(r"^[0-9a-fA-F]+$")


def _validate_sha(sha):
    if not sha:
        raise PplError("SCHEMA_INVALID", "sha must not be empty")
    if not _SHA_RE.match(sha):
        raise PplError("SCHEMA_INVALID", "sha %r is invalid: must be hexadecimal" % sha)
    if "/" in sha or ".." in sha or sha.startswith("."):
        raise PplError("SCHEMA_INVALID", "sha %r is invalid: no `/`, `..`, or leading `.`" % sha)


def _label_dir(task_id):
    return paths.home() / "labels" / task_id


def _live_dir(task_id):
    """The live run directory for `task_id`, or None if it is not (or no
    longer) live - archived, or never created."""
    directory = paths.run_dir(task_id)
    return directory if (directory / "state.json").is_file() else None


def _archived_dirs():
    """Every directory `ppl destroy --scope local` has ever produced, in the
    shape `shutil.move` leaves them: the full run directory, state.json and
    all, just parented under `_archive` with a mangled name instead of the
    task id."""
    root = paths.runs_dir() / "_archive"
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and (d / "state.json").is_file())


def _task_exists(task_id):
    """True when `task_id` names a run this pipeline still has a record of,
    live or archived. The archived side reads each archive directory's own
    state.json rather than trying to parse the task id back out of the
    mangled archive directory name, which is not reversible in general (a
    task id itself may contain a hyphen)."""
    if _live_dir(task_id) is not None:
        return True
    for directory in _archived_dirs():
        state = store.read_json(directory / "state.json")
        if isinstance(state, dict) and state.get("task_id") == task_id:
            return True
    return False


def record_label(task_id, sha, actual, by=None):
    """Record human ground truth for a candidate SHA.

    Deliberately bypasses both of `ppl`'s shared write gates, for reasons
    specific to what this fact is:

    `control.enforce` and its watchdog refresh are skipped
    [R-LABEL-NO-LIVENESS-REFRESH]: a human labelling a SHA from days or weeks
    ago says nothing about whether *this run* is alive right now, and
    refreshing the inactivity deadline on it would mask a real stall - the
    exact failure the watchdog (DESIGN 6.9) exists to catch.

    `require_current_generation` (the lease fence) is skipped
    [R-LABEL-NO-LEASE-FENCE]: a label is a human-entered fact about a SHA
    that is already in the past, never a write produced by a dispatched
    attempt, so there is no "stale attempt" for a lease generation to fence
    out. Requiring `--generation` here would be theatre - a human filling in
    ground truth is not competing with anything for the run's current state.

    Must also work AFTER `ppl destroy --scope local` [R-METRIC-SCANS-ARCHIVE]:
    defect escape and false FAIL are only knowable once a task has actually
    merged, which in the normal flow (`done -> merge_watch -> destroy`) is
    after the run has already been archived. `_task_exists` accepts a live
    OR an archived task id, and the label itself is written to a store
    outside the run directory entirely, so it is never moved, mangled or
    lost by a destroy that happens later - see this module's docstring.
    """
    _validate_sha(sha)
    if actual not in LABEL_VALUES:
        raise PplError("SCHEMA_INVALID",
                       "actual must be one of %s" % ", ".join(LABEL_VALUES))
    paths.check_task_id(task_id)
    if not _task_exists(task_id):
        raise PplError("SCHEMA_INVALID",
                       "no run for %s, live or archived. Run ppl init first." % task_id)
    record = {"task_id": task_id, "sha": sha, "actual": actual,
             "by": by or "unknown", "at": store.now()}
    store.write_json(_label_dir(task_id) / ("%s.json" % sha), record)
    # Only a still-live run has a log.jsonl worth appending to - an archived
    # run's directory is left exactly as destroy produced it rather than
    # written into again after the fact. The label file itself is the
    # durable record either way - it carries its own "at" and "by".
    live = _live_dir(task_id)
    if live is not None:
        store.append_log(task_id, {"event": "label", "sha": sha, "actual": actual})
    return record


def _metric(value, reason=None):
    return {"value": value, "defined": value is not None, "reason": reason}


def _run_records():
    """(task_id, directory) for every run this pipeline can still read from -
    live under `runs/<task_id>`, or archived under `runs/_archive/...`
    [R-METRIC-SCANS-ARCHIVE]. The task id for an archived directory comes
    from its own state.json, not from the (mangled) directory name."""
    records = []
    root = paths.runs_dir()
    if root.is_dir():
        for entry in sorted(root.iterdir()):
            if entry.name in ("ACTIVE", "_archive"):
                continue
            if entry.is_dir() and (entry / "state.json").is_file():
                records.append((entry.name, entry))
    for directory in _archived_dirs():
        state = store.read_json(directory / "state.json")
        task_id = (state or {}).get("task_id")
        if task_id:
            records.append((task_id, directory))
    return records


def _verdicts(directory):
    folder = directory / "verdicts"
    if not folder.is_dir():
        return []
    docs = []
    for path in sorted(folder.glob("*.json")):
        doc = store.read_json(path)
        if isinstance(doc, dict):
            docs.append(doc)
    return docs


def _verdict_history(directory):
    """Every recorded verdict, earlier ones on a re-recorded SHA included.

    A SHA with no `verdicts/<sha>/` folder (a run from before the history)
    contributes its single latest file.
    """
    docs = []
    for doc in _verdicts(directory):
        folder = directory / "verdicts" / str(doc.get("candidate_sha"))
        numbered = sorted((p for p in folder.glob("*.json") if p.stem.isdigit()),
                          key=lambda p: int(p.stem)) if folder.is_dir() else []
        entries = [store.read_json(p) for p in numbered]
        entries = [d for d in entries if isinstance(d, dict)]
        docs.extend(entries or [doc])
    return docs


def _labels(task_id):
    folder = _label_dir(task_id)
    if not folder.is_dir():
        return {}
    out = {}
    for path in sorted(folder.glob("*.json")):
        doc = store.read_json(path)
        if isinstance(doc, dict) and doc.get("sha"):
            out[doc["sha"]] = doc.get("actual")
    return out


def _producer_key(producer):
    producer = producer or {}
    provider = producer.get("provider") or "unknown"
    model = producer.get("model") or "unknown"
    return "%s:%s" % (provider, model)


def _gather(run_records, history=False):
    """One record per verdict, across every run live or archived, with its
    labelled ground truth (if any) attached. The latest verdict per SHA by
    default, every recorded one with `history`."""
    records = []
    for task_id, directory in run_records:
        labels = _labels(task_id)
        for doc in (_verdict_history(directory) if history else _verdicts(directory)):
            sha = doc.get("candidate_sha")
            records.append({
                "task_id": task_id,
                "sha": sha,
                "verdict": doc.get("verdict"),
                "producer": _producer_key(doc.get("producer")),
                "actual": labels.get(sha),
            })
    return records


def _labeled_rate(records, outcome_filter, matches, kind):
    """The shared shape behind defect-escape and false-FAIL: a rate over only
    the subset of verdicts that both matches `outcome_filter` and carries a
    human label [R-METRIC-LABEL-DEPENDENT-UNDEFINED].

    An empty result has two genuinely different causes, and review round 2
    caught the two of them collapsed into one message: "no ppl label
    recorded yet" printed even when two labels HAD been recorded, because
    zero verdicts of this kind existed to attach them to - the exact false-
    reason bug this metric exists to avoid, reproduced one call deeper.
    Distinguished here:

    - no verdict of `kind` exists at all - there is nothing to label, so the
      message must not suggest `ppl label`, which would be advice about a
      sha that carries no such verdict.
    - a verdict of `kind` exists but none carries a label yet - the message
      names a real, copyable sha from among the unlabelled ones, not a
      `<sha>` placeholder nobody can act on.
    """
    candidates = [r for r in records if r["verdict"] in outcome_filter]
    if not candidates:
        return _metric(None, "no %s verdict has been recorded yet - there is nothing to "
                             "label" % kind)
    labelled = [r for r in candidates if r["actual"]]
    if not labelled:
        example = candidates[0]
        return _metric(None,
                       "%d %s verdict(s) recorded and none labelled yet - run "
                       "`ppl label --sha %s --actual defective|correct --task %s`"
                       % (len(candidates), kind, example["sha"], example["task_id"]))
    hits = sum(1 for r in labelled if matches(r["actual"]))
    return _metric(hits / len(labelled))


def _defect_escape_rate(records):
    return _labeled_rate(records, ("PASS",), lambda actual: actual == "defective",
                         "accepted (PASS)")


def _outcompeted_rate(records):
    return _labeled_rate(records, ("PASS",), lambda actual: actual == "outcompeted",
                         "accepted (PASS)")


def _false_fail_rate(records):
    return _labeled_rate(records, schema.REJECTED_OUTCOMES, lambda actual: actual == "correct",
                         "rejected (REWORK/BLOCKED)")


def _incomplete_check_rate(records):
    """Undefined on zero verdicts, never a bare 0/0
    [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED]."""
    if not records:
        return _metric(None, "no verdicts recorded")
    incomplete = sum(1 for r in records if r["verdict"] in schema.INCOMPLETE_OUTCOMES)
    return _metric(incomplete / len(records))


def _counts_of(models):
    out = dict.fromkeys(usage.COUNTS, 0)
    for counts in models.values():
        for name in usage.COUNTS:
            value = counts.get(name, 0)
            if usage._is_count(value):
                out[name] += value
    return out


def _usage_metrics(run_records, producer_key):
    """cost, tokens per task and tokens by role over the accepted runs that
    have recorded usage. Undefined is never zero: each reason says what is
    missing [R-METRIC-COST-NEEDS-PRICES] [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED]."""
    accepted = _accepted_runs(run_records, producer_key)
    if not accepted:
        why = "no task in scope has reached done yet"
        if producer_key is not None and _accepted_runs(run_records, None):
            why = "no done task was accepted by producer %s yet" % producer_key
        return tuple(_metric(None, why) for _ in range(3))
    usages = [u for u in (usage.run_usage(directory) for _, directory in accepted) if u]
    if not usages:
        why = ("%d accepted task(s) but no usage recorded - install the usage hook "
               "with `ppl setup claude`" % len(accepted))
        return tuple(_metric(None, why) for _ in range(3))
    table = usage.prices()
    by_role = {}
    per_task = []
    costs = []
    unpriced = set()
    for roles in usages:
        task_total = dict.fromkeys(usage.COUNTS, 0)
        models = {}
        for role, role_models in roles.items():
            counts = _counts_of(role_models)
            bucket = by_role.setdefault(role, dict.fromkeys(usage.COUNTS, 0))
            for name in usage.COUNTS:
                bucket[name] += counts[name]
                task_total[name] += counts[name]
            models = usage.add(models, role_models)
        per_task.append(task_total)
        usd, missing = usage.dollars(models, table)
        unpriced.update(missing)
        costs.append(usd)
    tokens = _metric({name: sum(t[name] for t in per_task) / len(per_task)
                      for name in usage.COUNTS})
    roles_metric = _metric(by_role)
    if unpriced:
        cost = _metric(None, "no price for model(s) %s - add a [prices.\"<model>\"] table "
                             "to config.toml" % ", ".join(sorted(unpriced)))
    else:
        cost = _metric(sum(costs) / len(costs))
    return cost, tokens, roles_metric


def preset_costs(limit=10):
    """{preset: {"runs", "tokens_mean", "usd_mean", "unpriced", "usd_reason"}} for every
    routing preset, over the `limit` newest done runs with recorded usage, newest `init`
    first, priced with the live config.toml prices. A preset with no such run has runs 0
    and nulls. usd_mean is None when a model is unpriced (unpriced names them) or the
    price table is invalid (usd_reason carries the message). A run that cannot be read
    is skipped and a run without a stamp counts for no preset."""
    from . import presets
    found = {}
    for task_id, directory in _accepted_runs(_run_records(), None):
        try:
            state = store.read_json(directory / "state.json")
            preset = state.get("routing_preset") if isinstance(state, dict) else None
            roles = usage.run_usage(directory)
            started = _event(store.read_log_at(directory), "init")
        except (PplError, OSError, ValueError, AttributeError, TypeError):
            continue
        if isinstance(preset, str) and roles:
            found.setdefault(preset, []).append(
                (((started or {}).get("at") or ""), task_id, roles))
    try:
        table, table_error = usage.prices(), None
    except (PplError, OSError) as exc:
        table, table_error = None, getattr(exc, "message", None) or str(exc)
    out = {}
    for preset in list(presets.PRESETS) + sorted(set(found) - set(presets.PRESETS)):
        runs = sorted(found.get(preset, []), key=lambda r: r[:2], reverse=True)[:limit]
        entry = {"runs": len(runs), "tokens_mean": None, "usd_mean": None,
                 "unpriced": [], "usd_reason": None}
        out[preset] = entry
        if not runs:
            continue
        tokens, costs, unpriced = [], [], set()
        for _, _, roles in runs:
            models = {}
            for role_models in roles.values():
                models = usage.add(models, role_models)
            tokens.append(usage.total(_counts_of(models)))
            if table is not None:
                usd, missing = usage.dollars(models, table)
                costs.append(usd)
                unpriced.update(missing)
        entry["tokens_mean"] = sum(tokens) / len(tokens)
        entry["unpriced"] = sorted(unpriced)
        if table is None:
            entry["usd_reason"] = table_error
        elif unpriced:
            entry["usd_reason"] = "no price for %s" % ", ".join(sorted(unpriced))
        else:
            entry["usd_mean"] = sum(costs) / len(costs)
    return out


def _human_minutes():
    """Always undefined today, but unlike cost this is a real gap, not a
    property of the architecture: nothing anywhere records a human's working
    minutes yet, though a data source for it is buildable
    [R-METRIC-HUMAN-MINUTES-UNDEFINED]."""
    return _metric(None, "no event or field in this pipeline records human working "
                         "minutes - timestamps can show how long a human was asked to "
                         "wait, never how many minutes they actually spent")


def _parse(ts):
    return datetime.fromisoformat(ts)


def _event(events, name, **match):
    for ev in events:
        if ev.get("event") != name:
            continue
        if all(ev.get(key) == value for key, value in match.items()):
            return ev
    return None


def _accepted_runs(run_records, producer_key):
    """(task_id, directory) of every run that reached `done`, narrowed to
    those whose accepted PASS verdict (the one at the run's `candidate_sha`)
    has `producer_key`, or all of them when it is None. Reads state.json
    directly so an archived run counts too [R-METRIC-SCANS-ARCHIVE]."""
    accepted = []
    for task_id, directory in run_records:
        events = store.read_log_at(directory)
        if not _event(events, "init") or not _event(events, "lifecycle", to="done"):
            continue
        if producer_key is not None:
            state = store.read_json(directory / "state.json")
            sha = (state or {}).get("candidate_sha")
            doc = store.read_json(directory / "verdicts" / ("%s.json" % sha)) if sha else None
            if _producer_key((doc or {}).get("producer")) != producer_key:
                continue
        accepted.append((task_id, directory))
    return accepted


def _time_to_acceptance(run_records, producer_key):
    """Elapsed wall-clock time from `ppl init` to the `done` transition,
    attributed to the producer of the PASS verdict that unlocked it.
    Reads state.json directly rather than through `run.load` so an archived
    run counts too [R-METRIC-SCANS-ARCHIVE] - `run.load` only ever finds a
    live run.

    Undefined has two different causes when sliced by producer, and
    conflating them is the same false-reason shape review round 2 found in
    `_labeled_rate`: "no task in scope has reached done yet" would be a lie
    for a producer's own slice if OTHER producers' tasks had reached done
    and only this one's had not [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED].
    Distinguished here:

    - nothing anywhere has reached `done` yet - true regardless of slice.
    - something has reached `done`, but not under this producer - true only
      for a producer slice, and says how many tasks did finish, so the
      reader is not left thinking the fleet is idle when it is not.
    """
    done = _accepted_runs(run_records, None)
    accepted_here = _accepted_runs(run_records, producer_key)
    all_durations = []
    matching_durations = []
    for task_id, directory in done:
        events = store.read_log_at(directory)
        duration = (_parse(_event(events, "lifecycle", to="done")["at"])
                    - _parse(_event(events, "init")["at"])).total_seconds()
        all_durations.append(duration)
        if producer_key is None or (task_id, directory) in accepted_here:
            matching_durations.append(duration)
    if not all_durations:
        return _metric(None, "no task in scope has reached done yet")
    if not matching_durations:
        return _metric(None,
                       "%d task(s) have reached done, but none were accepted by "
                       "producer %s yet" % (len(all_durations), producer_key))
    return _metric(sum(matching_durations) / len(matching_durations))


def _entry(records, run_records, producer_key, history=None):
    cost, tokens, by_role = _usage_metrics(run_records, producer_key)
    return {
        "defect_escape_rate": _defect_escape_rate(records),
        "outcompeted_rate": _outcompeted_rate(records),
        "false_fail_rate": _false_fail_rate(records),
        "incomplete_check_rate": _incomplete_check_rate(records if history is None else history),
        "cost_per_accepted_task": cost,
        "tokens_per_accepted_task": tokens,
        "tokens_by_role": by_role,
        "time_to_acceptance_seconds": _time_to_acceptance(run_records, producer_key),
        "human_minutes": _human_minutes(),
    }


def compute():
    run_records = _run_records()
    records = _gather(run_records)
    history = _gather(run_records, history=True)
    producers = sorted(set(r["producer"] for r in history))
    result = {"overall": _entry(records, run_records, None, history), "producers": {}}
    for key in producers:
        sliced = [r for r in records if r["producer"] == key]
        sliced_history = [r for r in history if r["producer"] == key]
        result["producers"][key] = _entry(sliced, run_records, key, sliced_history)
    return result

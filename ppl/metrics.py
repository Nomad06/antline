"""Fleet metrics and human labels.

`ppl metrics` reads every run's `log.jsonl` and `verdicts/*.json` and computes
seven numbers, sliced by `producer`: defect escape rate, outcompeted rate,
false FAIL rate, incomplete check rate, cost per accepted task, time to
acceptance, human minutes.

The one rule this module exists to hold: **a metric with no data prints that
it is undefined, and never prints zero.** Zero is a claim ("we shipped no
defects") - undefined is the truth when nobody has told the pipeline whether
it did. Every metric below returns an envelope - `{"value": ..., "defined":
bool, "reason": ...}` - and `ppl metrics` prints that whole envelope as JSON,
so `value` is `null` and `reason` explains why whenever `defined` is false,
never a bare 0. This is deliberately the same shape whether the absence is
temporary (no labels filed yet) or structural (this pipeline has no data
source for it at all):

- **defect escape rate**, **outcompeted rate** and **false FAIL rate** need a
  human label (`ppl label`) on top of a verdict - undefined until at least one exists
  [R-METRIC-LABEL-DEPENDENT-UNDEFINED].
- **incomplete check rate** and **time to acceptance** need at least one
  verdict, respectively one completed (`done`) task - undefined on an empty
  fleet or a fleet with nothing accepted yet [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED].
- **cost per accepted task** is undefined always, for a structural reason
  that will not change until routing does: v1 routing is entirely
  subscription-billed Claude subagents (DESIGN 10.2), so `usd_used` is a
  manual budget cap that nothing in the dispatch path spends automatically
  and there is no dollar meter anywhere to read from - there is nothing to
  build here yet, not merely nothing built [R-METRIC-STRUCTURALLY-UNDEFINED].
- **human minutes** is undefined always too, but for the other reason: unlike
  cost, this one IS buildable (a `--minutes` flag on `ppl label`, or a
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

from . import paths, schema, store
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


def _gather(run_records):
    """One record per verdict, across every run live or archived, with its
    labelled ground truth (if any) attached."""
    records = []
    for task_id, directory in run_records:
        labels = _labels(task_id)
        for doc in _verdicts(directory):
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


def _cost_per_accepted_task():
    """Always undefined: v1 has no billed adapter, so no task carries a real
    dollar figure to average [R-METRIC-STRUCTURALLY-UNDEFINED]. See DESIGN
    10.3 - cost tracking becomes meaningful only once an API-billed provider
    exists."""
    return _metric(None, "v1 routing is entirely subscription-billed (DESIGN 10.2) - "
                         "usd_used is a manual budget cap nothing in the dispatch path "
                         "spends automatically, so no accepted task carries a real "
                         "dollar figure to average")


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
    all_durations = []
    matching_durations = []
    for task_id, directory in run_records:
        events = store.read_log_at(directory)
        init_ev = _event(events, "init")
        done_ev = _event(events, "lifecycle", to="done")
        if not init_ev or not done_ev:
            continue
        state = store.read_json(directory / "state.json")
        sha = (state or {}).get("candidate_sha")
        doc = store.read_json(directory / "verdicts" / ("%s.json" % sha)) if sha else None
        accepted_producer = _producer_key((doc or {}).get("producer"))
        duration = (_parse(done_ev["at"]) - _parse(init_ev["at"])).total_seconds()
        all_durations.append(duration)
        if producer_key is None or accepted_producer == producer_key:
            matching_durations.append(duration)
    if not all_durations:
        return _metric(None, "no task in scope has reached done yet")
    if not matching_durations:
        return _metric(None,
                       "%d task(s) have reached done, but none were accepted by "
                       "producer %s yet" % (len(all_durations), producer_key))
    return _metric(sum(matching_durations) / len(matching_durations))


def _entry(records, run_records, producer_key):
    return {
        "defect_escape_rate": _defect_escape_rate(records),
        "outcompeted_rate": _outcompeted_rate(records),
        "false_fail_rate": _false_fail_rate(records),
        "incomplete_check_rate": _incomplete_check_rate(records),
        "cost_per_accepted_task": _cost_per_accepted_task(),
        "time_to_acceptance_seconds": _time_to_acceptance(run_records, producer_key),
        "human_minutes": _human_minutes(),
    }


def compute():
    run_records = _run_records()
    records = _gather(run_records)
    producers = sorted(set(r["producer"] for r in records))
    result = {"overall": _entry(records, run_records, None), "producers": {}}
    for key in producers:
        sliced = [r for r in records if r["producer"] == key]
        result["producers"][key] = _entry(sliced, run_records, key)
    return result

"""Lessons from a finished run: the evidence bundle the read-only ppl-reflector reads.

After the merge, `ppl lessons bundle` writes `runs/<task>/lessons/bundle/`, the run's
evidence as files. The reflector is pointed at that directory: nothing of it is pasted into
a prompt, and what a reviewer wrote in a remark stays data. The bundle is built in a temporary
directory beside it and renamed onto it, so a build that fails leaves the earlier bundle as it
was and no partial one.
"""
import os
import re
import shutil
import tempfile
from pathlib import Path

from . import config, paths, run, store, trail
from .errors import PplError

BUNDLE_FILES = ("outcome.json", "brief.json", "challenge.json", "findings.json",
                "verdicts.json", "gates.json", "timeline.json", "threads.json",
                "memory-in-effect.json")

# The log events the timeline keeps, each with its index in the log as `n`.
TIMELINE_EVENTS = ("init", "stage_enter", "lifecycle", "park", "budget", "refusal", "deny",
                   "usage", "verdict", "evidence", "finding_add", "finding_status",
                   "review_reply", "merge_observed", "recover", "lease_bump")

# What a proposal's evidence reference may point at inside the bundle.
KINDS = ("finding", "remark", "verdict", "gate", "park", "deny", "timeline")

REFLECTOR_AGENT = "ppl-reflector.md"

# A thread id names its file under remarks/, and review-threads.json lies in runs/, where a
# role's sandboxed Bash can write, so only a plain name becomes a file name.
_PLAIN_ID = re.compile(r"[A-Za-z0-9_-]+")
_RECEIPT = re.compile(r"evidence/([^/]+)/([^/]+)\.json")


def bundle_dir(directory):
    """Where a run's bundle lives: `<run directory>/lessons/bundle`."""
    return Path(directory) / "lessons" / "bundle"


def _findings(docs):
    """Every finding document, any status, oldest first."""
    found = [doc for rel, doc in docs.items()
             if rel.startswith("findings/") and isinstance(doc, dict)]
    return sorted(found, key=lambda doc: str(doc.get("at") or ""))


def _verdicts(docs):
    """Every verdict event of the log in order, with `n`, its index in the log, and its
    verdict document, None when that document is not on disk."""
    paired, _ = trail.verdict_events(docs)
    return [dict(event, n=index, document=doc) for index, event, doc in paired]


def _gates(docs):
    """One summary per receipt, by SHA and then check. The build log stays out."""
    gates = []
    for rel, doc in docs.items():
        match = _RECEIPT.fullmatch(rel)
        if match and isinstance(doc, dict):
            gates.append({"check": match.group(2), "result": doc.get("result"),
                          "sha": match.group(1), "producer": doc.get("producer"),
                          "at": doc.get("at")})
    return gates


def _timeline(events):
    """The TIMELINE_EVENTS of the log, each with `n`, its index in the log. A usage event
    loses its per-model token counts."""
    timeline = []
    for index, event in enumerate(events):
        if event.get("event") not in TIMELINE_EVENTS:
            continue
        entry = dict(event, n=index)
        if entry["event"] == "usage":
            entry.pop("models", None)
        timeline.append(entry)
    return timeline


def _memory_in_effect(docs):
    """The lessons the run's snapshot froze at ppl init, under the harness's name. A snapshot
    from before memory existed names the harness state.json records and no entries."""
    snapshot = docs.get("harness.json")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    state = docs.get("state.json")
    recorded = state.get("harness") if isinstance(state, dict) else None
    name = snapshot.get("name") or (recorded.get("name") if isinstance(recorded, dict) else None)
    entries = snapshot.get("memory")
    return {"harness": name, "entries": entries if isinstance(entries, list) else []}


def _remarks(docs):
    """{"remarks/<thread id>.json": {"id", "notes"}} for every thread of review-threads.json."""
    fetched = docs.get("review-threads.json")
    threads = fetched.get("threads") if isinstance(fetched, dict) else None
    remarks = {}
    for thread in threads if isinstance(threads, list) else []:
        if not isinstance(thread, dict):
            continue
        ident = thread.get("id")
        if not (isinstance(ident, str) and _PLAIN_ID.fullmatch(ident)):
            raise PplError("SCHEMA_INVALID",
                           "review-threads.json holds the thread id %s, which is not a plain "
                           "name of letters, digits, _ and -, and a thread id names its file "
                           "under remarks/, so no bundle was written: fetch the threads again "
                           "with ppl review fetch" % repr(ident)[:80])
        notes = thread.get("notes")
        remarks["remarks/%s.json" % ident] = {"id": ident,
                                              "notes": notes if isinstance(notes, list) else []}
    return remarks


def _documents(directory, outcome):
    """{path in the bundle: document}. A brief or challenge the run never wrote is null."""
    docs, _ = trail.collect(directory)
    ledger = docs.get("threads.json")
    documents = {
        "outcome.json": outcome,
        "brief.json": docs.get("brief.json"),
        "challenge.json": docs.get("challenge.json"),
        "findings.json": _findings(docs),
        "verdicts.json": _verdicts(docs),
        "gates.json": _gates(docs),
        "timeline.json": _timeline(docs.get("log.jsonl") or []),
        "threads.json": ledger if ledger is not None else {},
        "memory-in-effect.json": _memory_in_effect(docs),
    }
    documents.update(_remarks(docs))
    return documents


def build_bundle(task_id, outcome):
    """Writes the live run's bundle with `outcome` as outcome.json and returns its path. It
    checks no stage and calls no control.enforce: `bundle` gates the command, and a destroy
    with reflection builds the bundle after its own destroy command is recorded. Every path
    under the run is checked right before it is written, as a role may have linked one
    elsewhere, and an earlier bundle is removed only once the new one is complete."""
    directory = paths.run_dir(task_id)
    store.checked_run_path(directory)
    run.load(task_id)
    documents = _documents(directory, outcome)
    lessons = directory / "lessons"
    target = bundle_dir(directory)
    store.checked_run_path(lessons)
    try:
        lessons.mkdir(exist_ok=True)
        building = tempfile.mkdtemp(dir=str(lessons), prefix=".bundle-")
    except OSError as exc:
        raise PplError("SCHEMA_INVALID", "the lessons bundle could not be built under %s: %s"
                       % (lessons, exc))
    try:
        for rel in sorted(documents):
            store.write_json(Path(building, rel), documents[rel])
        trail._umask_modes(building)
        store.checked_run_path(lessons)
        store.checked_run_path(target)
        if os.path.isdir(target):
            shutil.rmtree(target)
        elif os.path.lexists(target):
            os.unlink(target)
        os.rename(building, target)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return target


def _harness_name(state):
    recorded = state.get("harness")
    name = recorded.get("name") if isinstance(recorded, dict) else None
    return name or config.active_harness() or "<name>"


def bundle(task_id):
    """`ppl lessons bundle`: the merged run's bundle for ppl-reflector and its path, or None
    when config.toml [lessons] reflect is false. Refused ILLEGAL_TRANSITION unless the run is
    in merge_watch and `ppl merge observed` recorded the merge [R-LESSONS-BUNDLE-AFTER-MERGE],
    and SETUP_INCOMPLETE while the reflector's agent file is missing."""
    if not config.lessons_reflect():
        return None
    state = run.load(task_id)
    mr = state.get("mr")
    merged = mr.get("merged_sha") if isinstance(mr, dict) else None
    if state.get("stage") != "merge_watch":
        raise PplError("ILLEGAL_TRANSITION",
                       "ppl lessons bundle is valid only in stage merge_watch once `ppl merge "
                       "observed` recorded the merge, and the current stage is %s"
                       % state.get("stage"))
    if not merged:
        raise PplError("ILLEGAL_TRANSITION",
                       "ppl lessons bundle is valid only once `ppl merge observed` recorded the "
                       "merge, and none is recorded for %s yet" % task_id)
    agent = config.agents_dir() / REFLECTOR_AGENT
    if not agent.is_file():
        raise PplError("SETUP_INCOMPLETE",
                       "the reflector's agent file %s is missing, so ppl-reflector cannot be "
                       "dispatched: run `ppl harness apply %s`" % (agent, _harness_name(state)))
    return build_bundle(task_id, {"ended": "merged", "merged_sha": merged})

"""Lessons from a finished run: the evidence bundle the read-only ppl-reflector reads.

After the merge, `ppl lessons bundle` writes `runs/<task>/lessons/bundle/`, the run's
evidence as files. The reflector is pointed at that directory: nothing of it is pasted into
a prompt, and what a reviewer wrote in a remark stays data. The bundle is built in a temporary
directory beside it and swapped in by two renames, so a build that fails leaves the earlier
bundle as it was and no partial one.
"""
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from . import config, harness, memory, paths, routing, run, store, trail
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

# What an assessment may say of a lesson that was in effect, and how many lessons one record
# may propose.
VERDICTS = ("helped", "hurt", "no observed effect", "confounded")
MAX_PROPOSALS = 5

# The most of one bundle file `record` reads. A run's timeline is the largest of them.
BUNDLE_READ_LIMIT = 16 * 1024 * 1024

# The name start of a build directory and of an earlier bundle set aside, both in lessons/.
TEMP_PREFIX = ".bundle-"

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


def _remove(path):
    """Best effort: what it leaves behind, the next build's _clear_leftovers removes."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


def _clear_leftovers(lessons):
    """Removes the build and set-aside directories an interrupted build left in lessons/.
    Each is removed only as a real directory, a link of that name is left alone."""
    if not os.path.isdir(lessons):
        return
    for name in sorted(os.listdir(lessons)):
        path = lessons / name
        if (name.startswith(TEMP_PREFIX) and os.path.isdir(path)
                and not os.path.islink(path)):
            shutil.rmtree(path, ignore_errors=True)


def _write_all(building, documents):
    """Writes every document under `building`. remarks/ is made here, never reused, and each
    file's directory and the file itself are checked right before the write, the order
    cmd_review_fetch uses, so nothing is written through a link a role planted."""
    store.checked_run_path(building)
    store.checked_run_path(building / "remarks")
    try:
        os.mkdir(building / "remarks")
    except FileExistsError:
        raise PplError("EXTERNAL_BLOCKED", "%s appeared while the lessons bundle was being "
                       "built, so ppl will not write into it" % (building / "remarks"))
    for rel in sorted(documents):
        path = building / rel
        store.checked_run_path(path.parent)
        store.checked_run_path(path)
        store.write_json(path, documents[rel])
    trail._umask_modes(building)


def _swap(lessons, building, target):
    """Puts the new bundle in place: the earlier one is renamed aside, the new one renamed onto
    `target`, and the earlier one removed only then. A failed rename puts the earlier one
    back. Between the two renames no bundle is at `target`."""
    aside = None
    store.checked_run_path(lessons)
    store.checked_run_path(target)
    if os.path.lexists(target):
        aside = lessons / ("%sold-%s" % (TEMP_PREFIX, uuid.uuid4().hex[:12]))
        store.checked_run_path(aside)
        os.rename(target, aside)
    try:
        store.checked_run_path(lessons)
        store.checked_run_path(target)
        os.rename(building, target)
    except BaseException:
        if aside is not None:
            try:
                os.rename(aside, target)
            except OSError as exc:
                raise PplError("SCHEMA_INVALID",
                               "the lessons bundle could not be moved onto %s, and the earlier "
                               "bundle could not be put back, it is at %s: %s"
                               % (target, aside, exc))
        raise
    if aside is not None:
        _remove(aside)


def build_bundle(task_id, outcome):
    """Writes the live run's bundle with `outcome` as outcome.json and returns its path. It
    checks no stage and calls no control.enforce: `bundle` gates the command, and a destroy
    with reflection builds the bundle after its own destroy command is recorded. Every path
    under the run is checked right before it is written or renamed, as a role may have linked
    one elsewhere, and a build or a swap that fails leaves the earlier bundle in place."""
    directory = paths.run_dir(task_id)
    store.checked_run_path(directory)
    run.load(task_id)
    documents = _documents(directory, outcome)
    lessons = directory / "lessons"
    target = bundle_dir(directory)
    store.checked_run_path(lessons)
    _clear_leftovers(lessons)
    try:
        lessons.mkdir(exist_ok=True)
        building = Path(tempfile.mkdtemp(dir=str(lessons), prefix=TEMP_PREFIX))
    except OSError as exc:
        raise PplError("SCHEMA_INVALID", "the lessons bundle could not be built under %s: %s"
                       % (lessons, exc))
    try:
        try:
            _write_all(building, documents)
        except OSError as exc:
            raise PplError("SCHEMA_INVALID", "the lessons bundle could not be written under %s, "
                           "the earlier bundle is kept: %s" % (lessons, exc))
        try:
            _swap(lessons, building, target)
        except OSError as exc:
            raise PplError("SCHEMA_INVALID", "the lessons bundle could not be moved onto %s, "
                           "the earlier bundle is kept: %s" % (target, exc))
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return target


def lessons_home():
    """Where the owner's lessons live, outside runs/: `PPL_HOME/lessons`."""
    return paths.home() / "lessons"


def proposals_path(harness_name, task_id):
    """The proposals `ppl lessons record` stored for a task, per harness."""
    return lessons_home() / harness_name / "proposals" / ("%s.json" % task_id)


def pending_path(task_id):
    """The marker a destroyed run leaves while its reflection is not recorded."""
    return lessons_home() / "pending" / ("%s.json" % task_id)


def _refuse(message):
    raise PplError("SCHEMA_INVALID", message)


def _checked_bundle(candidate, source, tail=()):
    """`candidate` when it is a real directory under runs/, live or archived, with no link
    on the way below runs/, else SCHEMA_INVALID naming `source`."""
    candidate = Path(candidate)
    runs = paths.runs_dir()
    try:
        parts = candidate.relative_to(runs).parts
    except ValueError:
        parts = None
    if not parts or any(part in (".", "..") for part in parts):
        _refuse("%s names %s, which is not a path under %s" % (source, candidate, runs))
    current = runs
    for part in parts:
        current = current / part
        if os.path.islink(str(current)):
            _refuse("%s names %s, and %s is a link" % (source, candidate, current))
    if tail and parts[-len(tail):] != tail:
        _refuse("%s names %s, which does not end in %s" % (source, candidate, "/".join(tail)))
    if not candidate.is_dir():
        _refuse("%s names %s, which is not a directory" % (source, candidate))
    return candidate


def find_bundle(task_id):
    """The bundle `record` reads: the one a pending marker names, else the run's own, live
    or archived. A marker wins, since a destroyed run's bundle sits in the archive. A marker
    that names no real directory inside runs/ is SCHEMA_INVALID, and so is no bundle at
    all."""
    paths.check_task_id(task_id)
    marker = pending_path(task_id)
    named = store.read_json(marker, None, BUNDLE_READ_LIMIT)
    if named is not None:
        where = "the pending marker %s" % marker
        if not isinstance(named, dict) or named.get("task") != task_id \
                or not isinstance(named.get("bundle"), str):
            _refuse("%s is not {\"task\": %s, \"bundle\": <path>}" % (where, task_id))
        return _checked_bundle(named["bundle"], where, ("lessons", "bundle"))
    target = bundle_dir(trail.find_run(task_id))
    if not os.path.lexists(str(target)):
        _refuse("%s has no bundle at %s: run ppl lessons bundle first" % (task_id, target))
    return _checked_bundle(target, "the run's bundle")


def _read(bundle, name, cache):
    """A bundle file's JSON. A link, a file that is not a regular one or a missing one is
    SCHEMA_INVALID, and nothing is read through a link."""
    if name not in cache:
        path = Path(bundle) / name
        if os.path.islink(str(path)):
            _refuse("%s is a link, so it is not read" % path)
        if not os.path.lexists(str(path)):
            _refuse("%s is missing: run ppl lessons bundle first" % path)
        try:
            cache[name] = store.read_json(path, None, BUNDLE_READ_LIMIT)
        except (UnicodeDecodeError, RecursionError):
            _refuse("%s is not valid UTF-8 JSON" % path)
    return cache[name]


def _entries(bundle, name, cache):
    """The dict entries of a bundle file that holds a list."""
    data = _read(bundle, name, cache)
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


_HEX_PREFIX = re.compile(r"[0-9a-f]{7,40}")


def resolve(bundle, kind, ref, cache=None):
    """True when `ref` names something in the bundle's own files [R-LESSONS-EVIDENCE-RESOLVES]:
    a finding id of findings.json, a thread id with a remarks/ file or a key of threads.json,
    a sha prefix (7 hex digits or more, exactly one sha) of verdicts.json, `<sha prefix>/<check>`
    of one gates.json entry, the `n` of a timeline.json event named park or deny, or the `n` of
    any timeline event. `n` is the log index the bundle recorded, never the live log's."""
    cache = {} if cache is None else cache
    if not isinstance(ref, str) or not ref:
        return False
    if kind == "finding":
        return any(e.get("finding_id") == ref for e in _entries(bundle, "findings.json", cache))
    if kind == "remark":
        if _PLAIN_ID.fullmatch(ref):
            path = Path(bundle) / "remarks" / ("%s.json" % ref)
            if os.path.isfile(str(path)) and not os.path.islink(str(path)) \
                    and not os.path.islink(str(path.parent)):
                return True
        ledger = _read(bundle, "threads.json", cache)
        return isinstance(ledger, dict) and ref in ledger
    if kind == "verdict":
        if not _HEX_PREFIX.fullmatch(ref):
            return False
        shas = {e.get("sha") for e in _entries(bundle, "verdicts.json", cache)
                if isinstance(e.get("sha"), str) and e["sha"].startswith(ref)}
        return len(shas) == 1
    if kind == "gate":
        prefix, slash, check = ref.partition("/")
        if not slash or not check or not _HEX_PREFIX.fullmatch(prefix):
            return False
        found = {e["sha"] for e in _entries(bundle, "gates.json", cache)
                 if isinstance(e.get("sha"), str) and e["sha"].startswith(prefix)
                 and e.get("check") == check}
        return len(found) == 1
    if kind in ("park", "deny", "timeline"):
        if not re.fullmatch(r"(0|[1-9][0-9]*)", ref):
            return False
        return any(e.get("n") == int(ref) and (kind == "timeline" or e.get("event") == kind)
                   for e in _entries(bundle, "timeline.json", cache))
    return False


def _check_evidence(bundle, where, items, cache, required):
    """The evidence list as stored, [{"kind", "ref"}], each reference resolved."""
    if not isinstance(items, list) or (required and not items):
        _refuse("%s evidence must be a %slist of {kind, ref}"
                % (where, "non-empty " if required else ""))
    stored = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"kind", "ref"}:
            _refuse("%s evidence entry %r must be {kind, ref}" % (where, item))
        kind, ref = item["kind"], item["ref"]
        if kind not in KINDS:
            _refuse("%s evidence kind %r must be one of %s" % (where, kind, ", ".join(KINDS)))
        if isinstance(ref, int) and not isinstance(ref, bool):
            ref = str(ref)
        if not resolve(bundle, kind, ref, cache):
            _refuse("evidence %s:%s does not resolve in the bundle" % (kind, ref))
        stored.append({"kind": kind, "ref": ref})
    return stored


def _check_text(where, key, value, cleaner):
    if not isinstance(value, str):
        _refuse("%s %s must be a string" % (where, key))
    cleaned = cleaner(value)
    if not cleaned:
        _refuse("%s %s is empty" % (where, key))
    if len(cleaned) > memory.MAX_TEXT:
        _refuse("%s %s is %d characters, at most %d"
                % (where, key, len(cleaned), memory.MAX_TEXT))
    return cleaned


def _check_proposal(bundle, number, item, cache):
    where = "proposal %d" % number
    if not isinstance(item, dict) or set(item) != {"role", "cue", "lesson", "evidence"}:
        _refuse("%s must be {role, cue, lesson, evidence}" % where)
    if item["role"] not in routing.LESSON_ROLES:
        _refuse("%s role %r must be one of %s" % (where, item["role"],
                                                   ", ".join(routing.LESSON_ROLES)))
    return {"role": item["role"],
            "cue": _check_text(where, "cue", item["cue"], memory.clean_cue),
            "lesson": _check_text(where, "lesson", item["lesson"], memory.clean),
            "evidence": _check_evidence(bundle, where, item["evidence"], cache, True)}


def _check_assessment(bundle, number, item, cache, in_effect):
    where = "assessment %d" % number
    if not isinstance(item, dict) or not {"memory_id", "verdict"} <= set(item) \
            or set(item) - {"memory_id", "verdict", "evidence"}:
        _refuse("%s must be {memory_id, verdict, evidence}" % where)
    if not isinstance(item["memory_id"], str) or item["memory_id"] not in in_effect:
        _refuse("%s names memory %r, which was not in effect for the run"
                % (where, item["memory_id"]))
    if item["verdict"] not in VERDICTS:
        _refuse("%s verdict %r must be one of %s" % (where, item["verdict"],
                                                      ", ".join(VERDICTS)))
    return {"memory_id": item["memory_id"], "verdict": item["verdict"],
            "evidence": _check_evidence(bundle, where, item.get("evidence", []), cache,
                                        False)}


def record(task_id, doc):
    """`ppl lessons record`: check the reflector's answer against the run's bundle and store
    its proposals, pending, for the owner's review. Every reference must resolve in the bundle,
    or the whole record is refused SCHEMA_INVALID and nothing is written
    [R-LESSONS-EVIDENCE-RESOLVES]. A second record for the task is ILLEGAL_TRANSITION. Memory is
    never written here: the owner's accept does that. The pending marker of a destroyed run is
    removed by a record that succeeds, and left by one that is refused."""
    bundle = find_bundle(task_id)
    cache = {}
    in_effect = _read(bundle, "memory-in-effect.json", cache)
    name = in_effect.get("harness") if isinstance(in_effect, dict) else None
    try:
        harness.check_name(name)
    except PplError as exc:
        _refuse("memory-in-effect.json names a harness that is not a plain name: %s"
                % exc.message)
    entries = in_effect.get("entries")
    if not isinstance(entries, list) or not all(
            isinstance(e, dict) and isinstance(e.get("id"), str) for e in entries):
        _refuse("memory-in-effect.json entries must be a list of entries with a string id")
    ids = {e["id"] for e in entries}
    target = proposals_path(name, task_id)
    if target.exists():
        raise PplError("ILLEGAL_TRANSITION", "proposals for %s are already recorded at %s"
                       % (task_id, target))
    if not isinstance(doc, dict):
        _refuse("the reflector's answer must be a JSON object")
    unknown = sorted(set(doc) - {"task", "no_entry", "proposals", "assessments"})
    if unknown:
        _refuse("the reflector's answer has the key %r, which is not one of task, no_entry, "
                "proposals, assessments" % unknown[0])
    if doc.get("task") != task_id:
        _refuse("the answer's task %r is not %s" % (doc.get("task"), task_id))
    no_entry = doc.get("no_entry", False)
    proposals = doc.get("proposals", [])
    assessments = doc.get("assessments", [])
    if not isinstance(no_entry, bool):
        _refuse("no_entry must be true or false")
    if not isinstance(proposals, list) or not isinstance(assessments, list):
        _refuse("proposals and assessments must be lists")
    if no_entry and proposals:
        _refuse("no_entry is true and the answer still carries proposals")
    if not no_entry and not proposals:
        _refuse("an answer with no proposals sets no_entry true")
    if len(proposals) > MAX_PROPOSALS:
        _refuse("%d proposals, at most %d" % (len(proposals), MAX_PROPOSALS))
    stored = []
    for number, item in enumerate(proposals, 1):
        checked = _check_proposal(bundle, number, item, cache)
        stored.append(dict(checked, id="%s.%d" % (task_id, number), status="pending"))
    judged = [_check_assessment(bundle, number, item, cache, ids)
              for number, item in enumerate(assessments, 1)]
    result = {"task": task_id, "harness": name, "recorded_at": store.now(),
              "no_entry": no_entry, "proposals": stored, "assessments": judged}
    target.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(target, result)
    try:
        pending_path(task_id).unlink()
    except FileNotFoundError:
        pass
    return result


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

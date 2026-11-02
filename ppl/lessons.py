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
from .studio import files

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
        # at most 18 digits: int() of a longer one raises past Python's digit limit, and no
        # log holds so many events
        if not re.fullmatch(r"(0|[1-9][0-9]{0,17})", ref):
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
    removed by a record that succeeds, and left by one that is refused, except the refusal of
    a task that is already recorded, which removes it: nothing is left to record, and a marker
    left there would send the next session to the reflector again."""
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
        _clear_marker(task_id)
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
    _clear_marker(task_id)
    return result


def _clear_marker(task_id):
    try:
        pending_path(task_id).unlink()
    except FileNotFoundError:
        pass


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


# --- the owner's review: list, accept, reject, retire, edit -------------------------------

_PROPOSAL_ID = re.compile(r"(.+)\.([0-9]+)")


def harness_name(given=None):
    """`given`, else the active harness, as a plain name of an installed harness, else
    SCHEMA_INVALID naming it. The proposals file it selects stays inside lessons_home()."""
    name = given or config.active_harness()
    if not name:
        _refuse("no harness is active: name one with --harness <name>")
    harness.check_name(name)
    if name not in harness.list_names():
        _refuse("no installed harness %r" % name)
    return name


def _key(name):
    return "harness:%s:memory.toml" % name


def _load_memory(name):
    """(expected hash, entries) from one read of memory.toml. The hash goes to the save, so a
    change in between is CHANGED_ON_DISK and nothing is written."""
    read = files.read(_key(name))
    return read["hash"], memory.parse(read["text"], _key(name), routing.LESSON_ROLES)


def _save_memory(name, entries, expected_hash):
    """The only write of memory.toml: the harness check on a private copy, then the studio's
    versioned save, which keeps the old file in history [R-LESSONS-OWNER-ACCEPTS]."""
    key = _key(name)
    text = memory.dumps(entries)
    files.validate(key, text)
    files.save(key, text, expected_hash)


def _load_proposal(name, proposal_id):
    """(path, document, proposal) of a pending proposal, else SCHEMA_INVALID for one that is
    not there and ILLEGAL_TRANSITION for one already decided."""
    match = _PROPOSAL_ID.fullmatch(proposal_id) if isinstance(proposal_id, str) else None
    if not match:
        _refuse("%r is not a proposal id, which reads <task>.<n>" % (proposal_id,))
    paths.check_task_id(match.group(1))
    path = proposals_path(name, match.group(1))
    doc = store.read_json(path, None, BUNDLE_READ_LIMIT)
    if not isinstance(doc, dict) or not isinstance(doc.get("proposals"), list):
        _refuse("no proposals are recorded for %s in harness %s" % (match.group(1), name))
    for proposal in doc["proposals"]:
        if isinstance(proposal, dict) and proposal.get("id") == proposal_id:
            if proposal.get("status") != "pending":
                raise PplError("ILLEGAL_TRANSITION", "proposal %s is already %s"
                               % (proposal_id, proposal.get("status")))
            return path, doc, proposal
    _refuse("proposal %s is not in %s" % (proposal_id, path))


def _mark(path, doc, proposal, **fields):
    proposal.update(fields)
    store.write_json(path, doc)


def _sources(proposal):
    task = proposal["id"].rsplit(".", 1)[0]
    evidence = proposal.get("evidence")
    if not isinstance(evidence, list) or not evidence or not all(
            isinstance(e, dict) and isinstance(e.get("kind"), str)
            and isinstance(e.get("ref"), str) for e in evidence):
        _refuse("proposal %s has no evidence of {kind, ref}" % proposal["id"])
    return ["%s:%s:%s" % (task, e["kind"], e["ref"]) for e in evidence]


def listing(harness_name_=None):
    """{"harness", "proposals", "memory", "skipped"}: the pending proposals of every recorded
    task, each with its "task", every entry of memory.toml, retired ones included, and
    `{"path", "reason"}` for each proposals file that could not be read as one, which is
    left out of the list instead of failing it."""
    name = harness_name(harness_name_)
    pending, skipped = [], []
    folder = lessons_home() / name / "proposals"
    names = sorted(os.listdir(str(folder))) if folder.is_dir() else []
    for file_name in names:
        if not file_name.endswith(".json") or file_name.startswith("."):
            continue
        try:
            doc = store.read_json(folder / file_name, None, BUNDLE_READ_LIMIT)
        except PplError as exc:
            skipped.append({"path": file_name, "reason": exc.message})
            continue
        except (OSError, UnicodeDecodeError, RecursionError) as exc:
            skipped.append({"path": file_name, "reason": "cannot be read: %s" % exc})
            continue
        if not isinstance(doc, dict) or not isinstance(doc.get("proposals"), list):
            skipped.append({"path": file_name,
                            "reason": "is not an object with a proposals list"})
            continue
        for proposal in doc["proposals"]:
            if isinstance(proposal, dict) and proposal.get("status") == "pending":
                pending.append(dict(proposal, task=doc.get("task")))
    return {"harness": name, "proposals": pending, "memory": _load_memory(name)[1],
            "skipped": skipped}


def accept(harness_name_, proposal_id, cue=None, lesson=None):
    """The owner accepts a pending proposal [R-LESSONS-OWNER-ACCEPTS]. A cue that normalizes
    like an active entry's of the same role merges into it (`seen` + 1, sources unioned in
    order), else a new entry M-<max + 1> is appended, which the cap of memory.MAX_ACTIVE active
    entries per role refuses. `cue` and `lesson` are the owner's edit, cleaned and checked.
    memory.toml is saved first, with the hash read at the start, and the proposal is marked
    after that: a mark that fails leaves the lesson saved and the proposal pending, and
    accepting it again marks it without adding it twice, as its sources are already in the
    entry. Returns the entry written."""
    name = harness_name(harness_name_)
    expected, entries = _load_memory(name)
    path, doc, proposal = _load_proposal(name, proposal_id)
    role = proposal.get("role")
    if role not in routing.LESSON_ROLES:
        _refuse("proposal %s has the role %r, which is not one of %s"
                % (proposal_id, role, ", ".join(routing.LESSON_ROLES)))
    cue = _check_text("the owner's cue" if cue is not None else "proposal %s" % proposal_id,
                      "cue", proposal.get("cue") if cue is None else cue, memory.clean_cue)
    new_lesson = None if lesson is None else _check_text(
        "the owner's lesson", "lesson", lesson, memory.clean)
    text = new_lesson or _check_text("proposal %s" % proposal_id, "lesson",
                                     proposal.get("lesson"), memory.clean)
    sources = _sources(proposal)
    wanted = memory.normalize_cue(cue)
    target = next((e for e in memory.active(entries, role)
                   if memory.normalize_cue(e["cue"]) == wanted), None)
    if target is not None:
        fresh = [source for source in sources if source not in target["sources"]]
        if fresh or new_lesson:
            if fresh:
                target["seen"] += 1
                target["sources"] = target["sources"] + fresh
            if new_lesson:
                target["lesson"] = new_lesson
            _save_memory(name, entries, expected)
        entry = target
    else:
        if len(memory.active(entries, role)) >= memory.MAX_ACTIVE:
            _refuse("%s already has %d active lessons: retire one first: ppl lessons retire <id>"
                    % (role, memory.MAX_ACTIVE))
        entry = {"id": memory.next_id(entries), "role": role, "cue": cue, "lesson": text,
                 "sources": sources, "accepted_at": store.now(), "seen": 1,
                 "status": "active"}
        _save_memory(name, entries + [entry], expected)
    try:
        _mark(path, doc, proposal, status="accepted", memory_id=entry["id"])
    except OSError as exc:
        _refuse("memory.toml now holds the lesson as %s, but %s could not be updated (%s): "
                "the proposal stays pending, accept it again to mark it, nothing is added twice"
                % (entry["id"], path, exc))
    return entry


def reject(harness_name_, proposal_id, reason):
    """The owner rejects a pending proposal with a reason. memory.toml is not touched."""
    name = harness_name(harness_name_)
    reason = memory.clean(reason) if isinstance(reason, str) else ""
    if not reason:
        _refuse("rejecting %s needs a reason: --reason <text>" % proposal_id)
    path, doc, proposal = _load_proposal(name, proposal_id)
    _mark(path, doc, proposal, status="rejected", reason=reason)


def _find_active(entries, memory_id):
    for entry in entries:
        if entry["id"] == memory_id:
            if entry["status"] != "active":
                _refuse("%s is already retired" % memory_id)
            return entry
    _refuse("no lesson %s in memory.toml" % memory_id)


def retire(harness_name_, memory_id):
    """The owner retires an active entry: it stays in memory.toml, out of the render."""
    name = harness_name(harness_name_)
    expected, entries = _load_memory(name)
    _find_active(entries, memory_id)["status"] = "retired"
    _save_memory(name, entries, expected)


def edit(harness_name_, memory_id, cue=None, lesson=None):
    """The owner rewrites the cue or the lesson of an active entry, cleaned and checked.
    Returns the entry."""
    name = harness_name(harness_name_)
    if cue is None and lesson is None:
        _refuse("editing %s needs --cue or --lesson" % memory_id)
    expected, entries = _load_memory(name)
    entry = _find_active(entries, memory_id)
    if cue is not None:
        entry["cue"] = _check_text("the owner's cue", "cue", cue, memory.clean_cue)
    if lesson is not None:
        entry["lesson"] = _check_text("the owner's lesson", "lesson", lesson, memory.clean)
    _save_memory(name, entries, expected)
    return entry


def _recorded(state, task_id):
    """True when proposals for the task are already recorded under the harness state.json
    names. state.json lies in runs/, where a role's sandboxed Bash can write, so a name that
    is not a plain one counts as not recorded."""
    recorded = state.get("harness")
    name = recorded.get("name") if isinstance(recorded, dict) else None
    try:
        harness.check_name(name)
    except PplError:
        return False
    return proposals_path(name, task_id).is_file()


def _ended(state, task_id, why):
    """outcome.json for a destroy with reflection. A merge that `ppl merge observed` recorded
    is `{"ended": "merged", "merged_sha"}`, as `ppl lessons bundle` writes it. Any other run
    is `{"ended": "destroyed", "last_stage", "blocker", "why"}`: its stage, or the stage a
    parked or escalated run resumes to, and the blocker of parking.json only while the run is
    parked or escalated, since a resume leaves parking.json on disk."""
    mr = state.get("mr")
    merged = mr.get("merged_sha") if isinstance(mr, dict) else None
    if merged:
        return {"ended": "merged", "merged_sha": merged}
    stage = state.get("stage")
    blocker = None
    if stage in ("parking", "escalation"):
        parking = store.read_json(paths.run_dir(task_id) / "parking.json", None)
        parking = parking if isinstance(parking, dict) else {}
        blocker = parking.get("blocker")
        if stage == "parking":
            stage = parking.get("resume_stage")
        elif state.get("escalation_resume_stage"):
            stage = state["escalation_resume_stage"]
    return {"ended": "destroyed", "last_stage": stage, "blocker": blocker, "why": why}


def destroy_with_reflection(task_id, why):
    """Archives a run at the owner's say-so and leaves a pending marker for its reflection.
    `why` is the owner's one line on why it ended, in outcome.json for a run that was not
    merged and in the result for one that was. The bundle is built first, so a failure
    refuses before anything is archived. With `[lessons] reflect = false` it only destroys,
    and so it does, as `"reflection": "recorded"`, when the task's proposals are already
    recorded: the bundle they were drawn from stays as it is in the archive, and no marker
    sends a later session to the reflector again. A marker that cannot be written after the
    archive is an error naming the archived bundle, so the owner can reflect by hand."""
    if not isinstance(why, str) or not why.strip():
        raise PplError("SCHEMA_INVALID", "a destroy with reflection needs a non-empty why")
    why = why.strip()
    if not config.lessons_reflect():
        result = run.destroy(task_id, "local")
        result["reflection"] = "off"
        return result
    state = run.load(task_id)
    if _recorded(state, task_id):
        result = run.destroy(task_id, "local")
        result["reflection"] = "recorded"
        return result
    outcome = _ended(state, task_id, why)
    build_bundle(task_id, outcome)
    result = run.destroy(task_id, "local")
    if outcome["ended"] == "merged":
        result["why"] = why
    bundle_path = result["archive"] + "/lessons/bundle"
    try:
        store.write_json(pending_path(task_id), {"task": task_id, "bundle": bundle_path})
    except (OSError, PplError) as exc:
        raise PplError("EXTERNAL_BLOCKED",
                       "%s is archived at %s, but its pending reflection marker could not be "
                       "written (%s). The bundle is at %s: reflect on it by hand"
                       % (task_id, result["archive"], exc, bundle_path))
    result["reflection"] = "pending"
    return result


def pending():
    """The pending markers, oldest first by modification time, then by name, as
    `{"task", "bundle", "warning"}`: a marker that is unreadable or malformed comes back with
    a `warning` and no bundle, for the caller to report and skip."""
    folder = pending_path("X").parent
    try:
        names = sorted(name for name in os.listdir(str(folder)) if name.endswith(".json"))
    except OSError:
        return []
    found = []
    for name in names:
        marker = folder / name
        try:
            stamp = os.lstat(str(marker)).st_mtime
        except OSError:
            continue
        task = name[:-len(".json")]
        try:
            doc = store.read_json(marker, None, BUNDLE_READ_LIMIT)
            ok = isinstance(doc, dict) and doc.get("task") == task \
                and isinstance(doc.get("bundle"), str)
            entry = {"task": task, "bundle": doc["bundle"]} if ok else \
                {"task": task, "warning": "is not {\"task\": %s, \"bundle\": <path>}" % task}
        except (PplError, OSError, UnicodeDecodeError, RecursionError) as exc:
            entry = {"task": task, "warning": "cannot be read: %s" % exc}
        found.append((stamp, name, entry))
    return [entry for _, _, entry in sorted(found, key=lambda item: item[:2])]

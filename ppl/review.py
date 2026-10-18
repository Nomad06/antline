import re

from . import paths, run, store
from .errors import PplError

CLASSIFICATIONS = ("NEEDS_CODE_CHANGE", "NEEDS_REPLY_ONLY", "DISAGREE", "NEEDS_OWNER")

# Named so a caller can pass it explicitly, but pending() no longer defaults
# to it silently. A hardcoded username never matches a real GitLab account,
# and a filter that never fires while looking like it does is worse than no
# filter: see _resolve_author below, which prefers the identity recorded by
# `ppl mr set --author` and otherwise disables the author filter visibly
# rather than pretending to apply one.
PIPELINE_AUTHOR = "ppl-pipeline"

# Sentinel distinguishing "caller did not pass author at all" from "caller
# explicitly passed author=None to mean: do not filter by author". A plain
# None default cannot make that distinction.
_AUTHOR_NOT_GIVEN = object()


def _ledger_path(task_id):
    return paths.run_dir(task_id) / "threads.json"


def _ledger(task_id):
    return store.read_json(_ledger_path(task_id), {})


def set_mr(task_id, iid, url, author=None):
    state = run.load(task_id)
    state["mr"]["iid"] = iid
    state["mr"]["url"] = url
    # author is optional and sticky: a later `mr set` call that omits it
    # (e.g. recording a new iid after a force-push) must not erase an
    # identity recorded earlier.
    if author:
        state["mr"]["author"] = author
    # E2, cycle 2 wave E: same restamp as `ppl baseline record` and `ppl
    # brief set` - see ppl/cli.py:cmd_baseline for why this was missing.
    # skill/SKILL.md.skeleton Stage 7 calls `ppl mr set` once, after `ppl stage
    # enter review`, right before the review loop's first tick.
    state["next_safe_action"] = "run the review loop (references/review-loop.md)"
    run.save(state)
    store.append_log(task_id, {"event": "mr", "iid": iid, "url": url, "author": author})
    return state


def set_approval(task_id, sha):
    state = run.load(task_id)
    state["mr"]["approved_sha"] = sha
    run.save(state)
    store.append_log(task_id, {"event": "approval", "sha": sha})
    return state


_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def record_merge(task_id, sha, url=None):
    """Record the merge the orchestrator observed on the forge. Valid only in merge_watch and
    only once [R-MERGE-OBSERVED-ONCE]. The tracker gate opens on merged_sha [R-JIRA-GATE]."""
    if not sha or not _HEX_RE.match(sha):
        raise PplError("SCHEMA_INVALID", "--sha must be a hexadecimal commit sha, got %r" % sha)
    state = run.load(task_id)
    if state["stage"] != "merge_watch":
        raise PplError("ILLEGAL_TRANSITION",
                       "ppl merge observed is valid only in stage merge_watch, current stage is %s"
                       % state["stage"])
    mr = state.get("mr") or {}
    if mr.get("merged_sha"):
        raise PplError("ILLEGAL_TRANSITION",
                       "the merge is already recorded on %s, it is recorded once"
                       % mr["merged_sha"])
    mr["merged_sha"] = sha
    mr["merged_url"] = url
    state["mr"] = mr
    run.save(state)
    store.append_log(task_id, {"event": "merge_observed", "sha": sha, "url": url})
    return state


def approval_state(task_id, head_sha):
    approved = (run.load(task_id).get("mr") or {}).get("approved_sha")
    if not approved:
        return "none"
    return "current" if approved == head_sha else "stale"


def _normalize_note_id(note_id):
    """The single place a note id is coerced, so recording and comparing can
    never drift apart. GitLab's API hands note ids back as JSON integers;
    `ppl review answered --last-note-id` hands one back as a CLI string. An
    un-normalized `==` between the two would mismatch on every tick, making
    every thread look newer than its ledger record forever - the exact
    repeated-reply failure the ledger exists to prevent, wearing a type
    mismatch as a disguise.
    """
    return None if note_id is None else str(note_id)


def record_reply(task_id, thread_id, sha, classification, last_note_id=None):
    if classification not in CLASSIFICATIONS:
        raise PplError("SCHEMA_INVALID",
                       "classification must be one of %s" % ", ".join(CLASSIFICATIONS))
    # A missing run must not silently materialise a ledger file for a task
    # that was never initialised, the same guarantee run.load already gives
    # every other writer in this module.
    run.load(task_id)
    normalized = _normalize_note_id(last_note_id)
    ledger = _ledger(task_id)
    ledger.setdefault(thread_id, {})[sha] = {
        "classification": classification, "at": store.now(),
        "last_note_id": normalized}
    store.write_json(_ledger_path(task_id), ledger)
    store.append_log(task_id, {"event": "review_reply", "thread": thread_id,
                               "sha": sha, "classification": classification,
                               "last_note_id": normalized})
    return ledger[thread_id][sha]


def already_answered(task_id, thread_id, sha, last_note_id=None):
    """True once the ledger holds a reply for this thread on this SHA.

    When a note id is known on both sides (the one passed in and the one
    recorded when the pipeline last answered), a mismatch means a reviewer
    posted a further note on the thread after that reply, so this returns
    False even though the thread was "answered" in the old, coarser sense -
    the ledger's job is to stop the pipeline repeating itself, not to stop a
    human being heard. When either side has no note id, today's behaviour
    (recorded == answered) is kept so callers that never pass one see no
    change in meaning. Both sides are normalized through the same function
    record_reply uses, so an int from GitLab and a str from the CLI compare
    equal.
    """
    record = _ledger(task_id).get(thread_id, {}).get(sha)
    if record is None:
        return False
    recorded_note_id = _normalize_note_id(record.get("last_note_id"))
    given_note_id = _normalize_note_id(last_note_id)
    if given_note_id is None or recorded_note_id is None:
        return True
    return recorded_note_id == given_note_id


def _resolve_author(state, author):
    if author is not _AUTHOR_NOT_GIVEN:
        return author
    stored = (state.get("mr") or {}).get("author")
    if stored:
        return stored
    # Nothing stored and nothing passed: filtering by PIPELINE_AUTHOR here
    # would silently do nothing in production (it matches no real GitLab
    # username) while looking like protection against the pipeline replying
    # to itself. Disable the filter instead of faking it, and log the fact
    # so the gap is visible rather than discovered by a human being ignored.
    # Logged once per run (a flag on state, checked and set here) rather
    # than on every call: a multi-day loop calls pending() every tick, and
    # an unbounded repeat of the same line would bury it in log.jsonl noise
    # instead of standing out as the one thing worth fixing.
    if not state.get("review_unfiltered_logged"):
        store.append_log(state["task_id"], {
            "event": "review_pending_unfiltered",
            "note": "no pipeline author configured (run ppl mr set --author); "
                     "threads are not being filtered by author"})
        state["review_unfiltered_logged"] = True
        run.save(state)
    return None


def _is_own_reply(state, thread, identity):
    """A thread whose NEWEST note is a reply the pipeline identity posted is
    the pipeline's own work and stays out of the dispatch set. A thread the
    identity merely OPENED is not: when the orchestrator runs as the human
    MR author (the GitLab MCP acts as that account, so `ppl mr set --author`
    records the human), the author's own review remarks would otherwise be
    hidden forever. Without this rule the author's own remarks would never
    reach the responder, and the loop would report "no threads" while a remark
    waited.

    New thread shape: `last_author` (author of the newest note) and
    `note_count` (non-system notes). Skip only a multi-note thread whose
    newest note is by the identity - a reply. A single-note thread is an
    opening remark whoever wrote it. The legacy shape without `last_author`
    keeps the old first-author rule, logged once per run so the gap is
    visible rather than silent.
    """
    if identity is None:
        return False
    if "last_author" in thread:
        return thread.get("last_author") == identity and (thread.get("note_count") or 1) > 1
    if not state.get("review_legacy_threads_logged"):
        store.append_log(state["task_id"], {
            "event": "review_pending_legacy_thread_shape",
            "note": "threads carry no last_author/note_count; a thread the pipeline "
                     "identity opened is filtered out as if it were a reply"})
        state["review_legacy_threads_logged"] = True
        run.save(state)
    return thread.get("author") == identity


def _check_thread(index, thread):
    """threads[] is external input from GitLab by way of a file the CLI never
    validated - a shape it does not expect must raise SCHEMA_INVALID, not
    crash with a raw KeyError/TypeError past main()'s documented contract.
    The index is named in every message: "one of your forty threads is
    malformed" is not something an operator or an LLM orchestrator can act on.
    """
    if not isinstance(thread, dict):
        raise PplError("SCHEMA_INVALID",
                       "threads[%d] must be an object, got %s"
                       % (index, type(thread).__name__))
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise PplError("SCHEMA_INVALID", "threads[%d] is missing a non-empty 'id'" % index)
    resolved = thread.get("resolved")
    if resolved is not None and not isinstance(resolved, bool):
        raise PplError("SCHEMA_INVALID",
                       "threads[%d].resolved must be a boolean, got %s"
                       % (index, type(resolved).__name__))
    last_note_id = thread.get("last_note_id")
    if last_note_id is not None and (isinstance(last_note_id, bool)
                                     or not isinstance(last_note_id, (str, int))):
        raise PplError("SCHEMA_INVALID",
                       "threads[%d].last_note_id must be a string or integer, got %s"
                       % (index, type(last_note_id).__name__))
    last_author = thread.get("last_author")
    if last_author is not None and not isinstance(last_author, str):
        raise PplError("SCHEMA_INVALID",
                       "threads[%d].last_author must be a string, got %s"
                       % (index, type(last_author).__name__))
    note_count = thread.get("note_count")
    if note_count is not None and (isinstance(note_count, bool)
                                   or not isinstance(note_count, int) or note_count < 1):
        raise PplError("SCHEMA_INVALID",
                       "threads[%d].note_count must be a positive integer, got %r"
                       % (index, note_count))


def pending(task_id, threads, sha, author=_AUTHOR_NOT_GIVEN):
    # Unconditional and first: every path through this function - including
    # an explicit --author, which used to return before ever touching
    # run.load - must require an initialised run. "the documented loop never
    # passes --author here" was exactly the reasoning that left pending()
    # itself unreachable in production before it got a CLI wrapper - the same
    # reasoning does not get to excuse an unchecked branch of it now.
    state = run.load(task_id)
    for index, thread in enumerate(threads):
        _check_thread(index, thread)
    resolved_author = _resolve_author(state, author)
    out = []
    for thread in threads:
        if thread.get("resolved"):
            continue
        if _is_own_reply(state, thread, resolved_author):
            continue
        if already_answered(task_id, thread["id"], sha, thread.get("last_note_id")):
            continue
        out.append(thread)
    return out

"""The findings registry.

`ppl/verdict.py`'s `findings` field used to be free-form prose the verifier
wrote inline into the same document `done_blockers` reads back - the
self-attestation shape this whole project exists to close (see cycle 1 Task 6:
`done_blockers` once computed "proven" from the verdict's own claimed keys).
This module gives a finding a durable, structured home of its own
(`runs/<task_id>/findings/<finding_id>.json`), independent of any single
verdict document, and replaces `verdict.py`'s old `doc["findings"]` check.

Two-track routing, and there is no third box: a defect in the candidate's own
diff is fixed now (`TRACK_OWN_DIFF`); a defect in old or foreign code is
batch-escalated to its owner (`TRACK_FOREIGN`). The track is never an argument
to `add()` - it is derived, every time, from `gitfacts.changed_paths(repo,
base_sha, affected_sha)`: the finding's `affected_path` is either in that diff
or it is not, and those are the only two answers there are
[R-FINDINGS-TRACK-DERIVED]. A caller cannot hand in a third option because
there is no parameter to hand it through - a `track` keyword is rejected by
Python itself as an unexpected argument, not by a runtime check that a future
edit could quietly loosen.

`affected_sha` is the schema-listed field the design names; it is the candidate SHA
this finding is bound to, the same SHA-binding discipline `ppl verdict record`
already uses (R-DONE-SHA-BINDING). Track derivation, though, is a question
about a *path*, not a SHA - "is this file part of the candidate's own diff" -
so an entry also carries `affected_path`. Without it, "derive the track from
gitfacts.changed_paths" would have nothing to compare against; this field is
necessary to make that derivation possible, not an invented alternative to it.

Deleting a finding without a closing reason is refused - `delete()` requires a
non-empty `reason` and refuses to run without it [R-FINDINGS-DELETE-REASON].
The finding is still genuinely removed (this is a delete, not a soft mark),
but the reason is written to the durable log first, so a finding does not
just vanish without a trace of why.
"""
import re
import uuid

from . import gitfacts, paths, run, store
from .errors import PplError, role_output

SEVERITIES = ("blocking", "important", "minor")

STATUSES = ("open", "escalated", "fixed")

TRACK_OWN_DIFF = "own_diff"
TRACK_FOREIGN = "foreign"

_SHA_RE = re.compile(r"^[0-9a-fA-F]+$")


def _dir(task_id):
    return paths.run_dir(task_id) / "findings"


def _path(task_id, finding_id):
    return _dir(task_id) / ("%s.json" % finding_id)


def _validate_severity(severity):
    if severity not in SEVERITIES:
        raise PplError("SCHEMA_INVALID",
                       "severity must be one of %s, got %r" % (", ".join(SEVERITIES), severity))


def _validate_evidence(evidence):
    if not evidence or not isinstance(evidence, str) or not evidence.strip():
        raise PplError("SCHEMA_INVALID", "evidence must be a non-empty string")


def _validate_sha(sha):
    if not sha:
        raise PplError("SCHEMA_INVALID", "affected_sha must not be empty")
    if not _SHA_RE.match(sha):
        raise PplError("SCHEMA_INVALID", "affected_sha %r is invalid: must be hexadecimal" % sha)
    if "/" in sha or ".." in sha or sha.startswith("."):
        raise PplError("SCHEMA_INVALID",
                       "affected_sha %r is invalid: no `/`, `..`, or leading `.`" % sha)


def _validate_path(path):
    if not path or not isinstance(path, str):
        raise PplError("SCHEMA_INVALID", "affected_path must be a non-empty string")
    if path.startswith("/"):
        raise PplError("SCHEMA_INVALID", "affected_path must not be absolute: %s" % path)
    if ".." in path.split("/"):
        raise PplError("SCHEMA_INVALID",
                       "affected_path contains a traversal sequence (..): %s" % path)


def _derive_track(repo, base_sha, affected_sha, affected_path):
    """The only two answers there are: `affected_path` is in the diff from
    `base_sha` to `affected_sha`, or it is not [R-FINDINGS-TRACK-DERIVED].

    Never accepts a caller-supplied track - see this module's docstring for
    why that would reopen exactly the hole two-track routing exists to close.
    """
    changed = gitfacts.changed_paths(repo, base_sha, affected_sha)
    return TRACK_OWN_DIFF if affected_path in changed else TRACK_FOREIGN


def add(task_id, severity, evidence, affected_sha, affected_path, owner=None, fix_link=None):
    """Record a finding. `severity`, `evidence`, `affected_sha` and
    `affected_path` are required; `owner` is required only when the derived
    track is `foreign` (an own-diff finding is owned by the task fixing it
    now, by default); `fix_link` is optional at creation and normally set
    later, once a fix exists, via `set_status`.
    """
    with role_output():
        _validate_severity(severity)
        _validate_evidence(evidence)
        _validate_sha(affected_sha)
        _validate_path(affected_path)
    state = run.load(task_id)
    if not state.get("base_sha"):
        raise PplError("SCHEMA_INVALID",
                       "task %s has no base_sha recorded yet - run ppl baseline record first, "
                       "so a finding's track can be derived against something" % task_id)
    track = _derive_track(state["repo"], state["base_sha"], affected_sha, affected_path)
    if track == TRACK_FOREIGN:
        with role_output():
            if not owner or not str(owner).strip():
                raise PplError("SCHEMA_INVALID",
                               "a finding outside the candidate's own diff (track=foreign) "
                               "must record an owner to batch-escalate to - affected_path %r "
                               "was not touched between base_sha and affected_sha"
                               % affected_path)
        status = "escalated"
    else:
        owner = owner or task_id
        status = "open"
    finding_id = uuid.uuid4().hex
    record = {
        "finding_id": finding_id, "task_id": task_id, "severity": severity,
        "evidence": evidence, "affected_sha": affected_sha, "affected_path": affected_path,
        "track": track, "owner": owner, "status": status, "fix_link": fix_link,
        "at": store.now(),
    }
    store.write_json(_path(task_id, finding_id), record)
    store.append_log(task_id, {"event": "finding_add", "finding_id": finding_id,
                               "severity": severity, "track": track, "status": status,
                               "affected_sha": affected_sha})
    return record


def _load_all(task_id):
    """Every finding on disk for `task_id` - or a refusal, never a silent drop.

    Round 1 review (C2/C3, cycle 2 wave B): a document that decoded to
    something other than a finding object (most concretely, a blocking
    finding's file rewritten to `[]` - still valid JSON) used to be silently
    skipped, same shape as `store.read_json` decoding invalid JSON outright
    (both now routed as PplError - see the previous commit). Either way, a
    finding this registry cannot make sense of used to just vanish, which
    OPENS `open_blocking_on_own_diff` and the done gate it gates rather than
    refusing them - "an unpopulated registry gated nothing" walking back in
    through a different door. The guard hook's `active_state()` was
    hardened to fail CLOSED on exactly this class in cycle 1
    (`ppl/_assets/hooks/ppl_guard.py:active_state`); this registry gets the same
    treatment now: a finding file this function cannot trust makes every
    caller - `list_all`, `list_open`, `open_blocking_on_own_diff` - refuse
    with SCHEMA_INVALID rather than silently proceed as if the file did not
    exist [R-FINDINGS-FAILS-CLOSED-ON-MALFORMED].
    """
    folder = _dir(task_id)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("*.json")):
        doc = store.read_json(path)
        if not isinstance(doc, dict):
            raise PplError("SCHEMA_INVALID",
                           "findings file %s does not contain a finding object (got %s) - "
                           "the registry cannot be trusted, refusing rather than treating a "
                           "malformed or wrong-shaped finding as absent" % (path, type(doc).__name__))
        out.append(doc)
    return out


def list_all(task_id):
    return _load_all(task_id)


def list_open(task_id):
    return [f for f in _load_all(task_id) if f.get("status") != "fixed"]


def get(task_id, finding_id):
    doc = store.read_json(_path(task_id, finding_id))
    if doc is None:
        raise PplError("SCHEMA_INVALID", "no finding %s recorded for %s" % (finding_id, task_id))
    return doc


def set_status(task_id, finding_id, status, fix_link=None, by=None):
    """Round 1 review (C2): `fixed` used to require nothing at all - no
    fix_link, no evidence, no head movement - which is the exact
    self-attestation shape cycle 1 already closed once for acceptance
    evidence, arriving here under a different noun. `fixed` must now be
    earned: a `fix_link` for the human trail, AND a real commit that
    touched the finding's own `affected_path` between `affected_sha` and
    the current head - the same derive-do-not-accept discipline the track
    already uses, not "the head moved" (any unrelated commit would satisfy
    that, which is exactly the C1 hole walking back in through closure
    instead of gating) [R-FINDINGS-FIXED-REQUIRES-PROOF].
    """
    if status not in STATUSES:
        raise PplError("SCHEMA_INVALID",
                       "status must be one of %s, got %r" % (", ".join(STATUSES), status))
    record = get(task_id, finding_id)
    fixed_at_sha = None
    if status == "fixed":
        if not fix_link or not str(fix_link).strip():
            raise PplError("SCHEMA_INVALID",
                           "closing a finding as fixed requires --fix-link, for the human trail")
        state = run.load(task_id)
        fixed_at_sha = gitfacts.head_sha(state["repo"])
        changed = gitfacts.changed_paths(state["repo"], record["affected_sha"], fixed_at_sha)
        if record["affected_path"] not in changed:
            raise PplError("SCHEMA_INVALID",
                           "closing %s as fixed requires a commit between %s and head %s that "
                           "touches %s - the file the defect lives in has not been touched, so "
                           "there is nothing here that could be the fix"
                           % (finding_id, record["affected_sha"], fixed_at_sha,
                              record["affected_path"]))
        record["fixed_at_sha"] = fixed_at_sha
    record["status"] = status
    if fix_link is not None:
        record["fix_link"] = fix_link
    record["updated_at"] = store.now()
    store.write_json(_path(task_id, finding_id), record)
    store.append_log(task_id, {"event": "finding_status", "finding_id": finding_id,
                               "status": status, "by": by, "fixed_at_sha": fixed_at_sha})
    return record


def delete(task_id, finding_id, reason, by=None):
    """Remove a finding. Refuses to run without a non-empty closing `reason`
    [R-FINDINGS-DELETE-REASON] - the reason is written to the durable log
    before the file is removed, so what closed it survives the finding
    itself.
    """
    reason = (reason or "").strip()
    if not reason:
        raise PplError("SCHEMA_INVALID",
                       "a finding may not be deleted without a closing reason")
    record = get(task_id, finding_id)
    store.append_log(task_id, {"event": "finding_delete", "finding_id": finding_id,
                               "reason": reason, "by": by, "severity": record["severity"]})
    _path(task_id, finding_id).unlink()
    return {"finding_id": finding_id, "deleted": True, "reason": reason}


def open_blocking_on_own_diff(task_id):
    """Findings that gate `ppl transition done` (R-DONE-OPEN-FINDING):
    `blocking` severity, not `fixed`, on the `own_diff` track.

    Round 1 review (C1): this used to also require `affected_sha ==
    head_sha`, applying the same-SHA rule that governs ATTESTATIONS
    (verdicts, approvals, CI results - each says "as of this tree" and
    rightly expires when the tree moves) to a DEFECT, which is not an
    attestation and does not stop existing because an unrelated commit
    landed. Any commit satisfied that filter, so it cleared a real,
    unfixed, blocking finding off the done gate for free. Dropped: a
    blocking own_diff finding blocks until it is closed by proof
    (`set_status(..., "fixed")`, which itself now requires that proof - see
    its docstring), whatever the head does in the meantime.

    A `foreign`-track finding never blocks this task's own done gate:
    escalating it to its owner, not stalling this task on someone else's
    code, is the entire point of the second track.
    """
    return [f for f in _load_all(task_id)
            if f.get("track") == TRACK_OWN_DIFF and f.get("severity") == "blocking"
            and f.get("status") != "fixed"]

import hashlib
import json
import re

from .errors import PplError

ACCEPTANCE_EVIDENCE_RE = re.compile(r"^evidence:[A-Za-z0-9._-]+$")

BRIEF_FIELDS = {
    "task_id": str, "purpose": str, "success_outcome": str,
    "governing_requirements": list, "acceptance": list,
    "allowed_write_paths": list, "forbidden_paths": list,
    "verification": list, "open_decisions": list,
    "name_contract": dict,
}

VERDICT_FIELDS = {
    "task_id": str, "candidate_sha": str, "spec_revision": str,
    "verdict": str, "acceptance_evidence": dict, "findings": list,
    "producer": dict,
}

# An open decision the brief may carry as an object instead of a bare string.
# `decision` says what the briefer did with it: pulled it into scope, left it
# for a later ticket, or handed it to the owner. `cost_to_close_files` is the
# briefer's estimate of the files a close-now would touch, and it is what
# makes "parked" checkable: a gap that small is not parked, it is decided or
# escalated [R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP]. A three-file gap that a
# parallel implementation simply closed is the size of hole this makes visible.
OPEN_DECISION_KINDS = ("in_scope", "parked", "owner")
SMALL_DECISION_FILES = 5

# The brief challenge: a second reading of the brief against the spec text,
# recorded before implementation may start. Three fixed questions, answered
# mechanically enough that ppl can refuse a self-contradicting report: a new
# class that duplicates an existing owner, a wire shape that does not match
# the spec's own wording, and a cheap gap left parked cannot sit in an
# ACCEPTED report [R-BRIEF-CHALLENGE-SELF-CONSISTENT].
CHALLENGE_FIELDS = {
    "task_id": str, "spec_revision": str, "verdict": str,
    "new_classes": list, "wire_shapes": list, "open_decisions": list,
    "producer": dict,
}
CHALLENGE_VERDICTS = ("ACCEPTED", "REVISE")


def require(obj, spec, what):
    if not isinstance(obj, dict):
        raise PplError("SCHEMA_INVALID", "%s must be an object" % what)
    for field, kind in spec.items():
        if field not in obj:
            raise PplError("SCHEMA_INVALID", "%s is missing field %r" % (what, field))
        if not isinstance(obj[field], kind):
            raise PplError("SCHEMA_INVALID",
                           "%s field %r must be %s" % (what, field, kind.__name__))


def validate_brief(brief):
    require(brief, BRIEF_FIELDS, "brief")
    if not brief["acceptance"]:
        raise PplError("SCHEMA_INVALID", "brief must carry at least one acceptance id")
    # Presence (a non-empty list) was the only thing ever checked here - not
    # what the list actually contains. `[{"id": "AC1", "text": "..."}]` is the
    # obvious reading of "acceptance criteria" when nothing documents the
    # shape, and it used to be accepted at `ppl brief set` only to blow up as
    # a raw `TypeError: cannot use 'dict' as a set element` five stages later,
    # inside done_blockers's `acceptance_id not in proven` set membership
    # test - exit 1, not a routable exit 2, at the single most load-bearing
    # step in the system (E1 CRITICAL, cycle 2 wave B). Refused here instead,
    # where the brief is still being accepted and a clear message is possible.
    if not all(isinstance(a, str) and a.strip() for a in brief["acceptance"]):
        raise PplError("SCHEMA_INVALID",
                       "every acceptance id must be a non-empty string, not an object, a "
                       "list, or blank text - got %r" % (brief["acceptance"],))
    if not brief["allowed_write_paths"]:
        raise PplError("SCHEMA_INVALID", "brief write boundary must not be empty")
    overlap = set(brief["allowed_write_paths"]) & set(brief["forbidden_paths"])
    if overlap:
        raise PplError("SCHEMA_INVALID",
                       "write and forbid lists overlap: %s" % ", ".join(sorted(overlap)))
    if not brief["name_contract"]:
        raise PplError("SCHEMA_INVALID",
                       "brief name_contract must not be empty - it is the frozen method "
                       "signatures, DTO field names, error codes, units and boundary rules "
                       "the code worker and the test worker are both dispatched against")
    validate_open_decisions(brief["open_decisions"], "brief")


def validate_open_decisions(entries, what):
    """Bare strings stay accepted (the legacy shape). An object entry must
    carry question, cost_to_close_files and decision, and a parked decision
    with a small cost is refused: close it or escalate it, do not park it
    [R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP]."""
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            continue
        if not isinstance(entry, dict):
            raise PplError("SCHEMA_INVALID",
                           "%s open_decisions[%d] must be a string or an object" % (what, index))
        question = entry.get("question")
        if not isinstance(question, str) or not question.strip():
            raise PplError("SCHEMA_INVALID",
                           "%s open_decisions[%d] needs a non-empty 'question'" % (what, index))
        cost = entry.get("cost_to_close_files")
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise PplError("SCHEMA_INVALID",
                           "%s open_decisions[%d].cost_to_close_files must be a non-negative "
                           "integer (files a close-now would touch)" % (what, index))
        decision = entry.get("decision")
        if decision not in OPEN_DECISION_KINDS:
            raise PplError("SCHEMA_INVALID",
                           "%s open_decisions[%d].decision must be one of %s"
                           % (what, index, ", ".join(OPEN_DECISION_KINDS)))
        if decision == "parked" and cost <= SMALL_DECISION_FILES:
            raise PplError("SCHEMA_INVALID",
                           "%s open_decisions[%d] %r is parked at a cost of %d files: a gap of "
                           "%d files or fewer is closed (decision in_scope) or handed to the "
                           "owner (decision owner, then ppl escalate --code OWNER_DECISION), "
                           "never parked [R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP]"
                           % (what, index, question, cost, SMALL_DECISION_FILES))


def validate_challenge(doc):
    require(doc, CHALLENGE_FIELDS, "challenge")
    if doc["verdict"] not in CHALLENGE_VERDICTS:
        raise PplError("SCHEMA_INVALID",
                       "challenge verdict must be one of: %s" % ", ".join(CHALLENGE_VERDICTS))
    problems = []
    for index, entry in enumerate(doc["new_classes"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise PplError("SCHEMA_INVALID",
                           "challenge new_classes[%d] must be an object with a 'name'" % index)
        if not isinstance(entry.get("justified"), bool):
            raise PplError("SCHEMA_INVALID",
                           "challenge new_classes[%d].justified must be a boolean" % index)
        if not entry["justified"]:
            problems.append("new class %s is not justified (existing owner: %s)"
                            % (entry["name"], entry.get("existing_owner")))
    for index, entry in enumerate(doc["wire_shapes"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("shape"), str):
            raise PplError("SCHEMA_INVALID",
                           "challenge wire_shapes[%d] must be an object with a 'shape'" % index)
        if not isinstance(entry.get("matches_spec"), bool):
            raise PplError("SCHEMA_INVALID",
                           "challenge wire_shapes[%d].matches_spec must be a boolean" % index)
        if not entry["matches_spec"]:
            problems.append("wire shape %s does not match the spec text" % entry["shape"])
    validate_open_decisions(doc["open_decisions"], "challenge")
    if doc["verdict"] == "ACCEPTED" and problems:
        raise PplError("SCHEMA_INVALID",
                       "a challenge cannot be ACCEPTED while it reports: %s - set this "
                       "report's verdict to REVISE and keep those findings as they are "
                       "[R-BRIEF-CHALLENGE-SELF-CONSISTENT]"
                       % "; ".join(problems))


def spec_revision(name_contract, acceptance, allowed_write_paths, forbidden_paths):
    """A deterministic fingerprint of every brief field the done gate reads.

    Cycle 2 wave F: this used to hash `name_contract` alone. `done_blockers`
    (ppl/verdict.py) also reads `state["acceptance"]` (condition 8's non-empty
    check and condition 3's per-criterion loop) and `boundary.json`'s `write`/
    `forbid` (condition 4) - both are brief fields, both are rewritten by a
    later `ppl brief set` the same way `name_contract` is, and neither was
    covered. A re-brief that dropped an unproven acceptance id, or widened the
    write boundary to cover a path the diff had already escaped to, left every
    outstanding verdict looking current: the done gate proves the criteria
    that are CURRENTLY on record, and both of those were editable by the same
    session being gated [R-BRIEF-SPEC-REVISION-STALE]. Found while examining a
    different, narrower question (E5, cycle 2 wave E) and fixed here rather
    than filed as a residual, because the shrink is provably exploitable, not
    merely under-tested.

    Deliberately NOT hashed: `purpose`, `success_outcome`,
    `governing_requirements`, `verification`, `open_decisions`. None of them
    is read anywhere in `done_blockers` - the fingerprint's scope is derived
    from what the gate actually reads, not from "the whole brief" - so a
    re-brief that only corrects prose or fills in `governing_requirements`
    (the CONTEXT_INSUFFICIENT retry SKILL.md allows once) still leaves this
    value byte-identical and does not force a re-verification nothing
    downstream needed.

    `ppl boundary set` can also rewrite `write`/`forbid` directly, without a
    brief in the loop at all, so `cmd_boundary` recomputes and rebinds this
    same value (reusing the current `name_contract` and `acceptance`) whenever
    a spec_revision is already bound - see cli.py:cmd_boundary and the
    reopened N5 ruling it closes.
    """
    canonical = json.dumps({
        "name_contract": name_contract,
        "acceptance": list(acceptance),
        "allowed_write_paths": list(allowed_write_paths),
        "forbidden_paths": list(forbidden_paths),
    }, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()



# The closed set of verdict outcomes. PASS is the only one that can pass the
# done gate (R-DONE-VERDICT-PASS) - every other outcome is refused as not-PASS,
# and that refusal is one check, not eight - see ppl/verdict.py:done_blockers.
# INCONCLUSIVE, ERROR, CANCELLED and TIMEOUT all name a check that did not
# happen (crashed, was cancelled, ran out of time, or produced no clear
# result): that is not evidence the code is bad, and it is not evidence the
# code is good either, so none of them is spelled as a kind of PASS or folded
# into REWORK/BLOCKED, which do mean "a real check found a real problem"
# [R-VERDICT-OUTCOME-VOCAB].
VERDICT_OUTCOMES = ("PASS", "REWORK", "BLOCKED", "OWNER_DECISION",
                   "INCONCLUSIVE", "ERROR", "CANCELLED", "TIMEOUT")

# The four outcomes where a check did not happen. Shared by ppl/verdict.py
# (which parks the run rather than let the same non-answer repeat forever -
# see R-VERIFICATION-UNAVAILABLE-PARK) and ppl/metrics.py (which excludes
# them from defect-escape and false-FAIL - see R-METRIC-LABEL-DEPENDENT-UNDEFINED).
# Defined once here so the two never drift onto different sets.
INCOMPLETE_OUTCOMES = ("INCONCLUSIVE", "ERROR", "CANCELLED", "TIMEOUT")

# The two outcomes where a real check ran to completion and came out against
# the candidate. Shared by ppl/metrics.py's false-FAIL rate.
REJECTED_OUTCOMES = ("REWORK", "BLOCKED")


def validate_verdict(verdict):
    require(verdict, VERDICT_FIELDS, "verdict")
    if verdict["verdict"] not in VERDICT_OUTCOMES:
        raise PplError("SCHEMA_INVALID",
                       "verdict must be one of: %s" % ", ".join(VERDICT_OUTCOMES))
    for acceptance_id, ref in verdict["acceptance_evidence"].items():
        if not isinstance(ref, str) or not ACCEPTANCE_EVIDENCE_RE.match(ref):
            raise PplError("SCHEMA_INVALID",
                           "acceptance_evidence for %r must be a string of the form "
                           "'evidence:<check>', got %r" % (acceptance_id, ref))
    # findings[] used to be the enforcement surface (done_blockers read it
    # straight off this same document the verifier writes) - the ppl/findings.py
    # registry replaces it, and a verdict now carries findings only by
    # reference (bound separately via `ppl findings add`, keyed on candidate
    # SHA). A document still carrying the old inline shape is refused rather
    # than silently ignored or auto-migrated: auto-ingesting it here would
    # invent a fresh finding_id on every re-record of the same verdict (a
    # verdict may legitimately be recorded more than once on a SHA, e.g. a
    # correction), silently duplicating open findings with no way to tell
    # they came from the same report [R-FINDINGS-VERDICT-FIELD-RETIRED].
    if verdict["findings"]:
        raise PplError("SCHEMA_INVALID",
                       "verdict.findings must be empty - findings are recorded separately via "
                       "'ppl findings add', bound to this same candidate_sha, not embedded in "
                       "the verdict document")

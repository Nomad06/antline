from . import boundary, evidence, findings, paths, run, schema, store
from .errors import PplError, role_output

REQUIRED_CHECKS = ("focused-tests", "secret-scan")

# Checks the done gate requires a receipt for, but which may honestly be
# skipped: a simplification pass on a docs-only change has nothing to do,
# and `skip` says so on the record instead of the pass never happening
# [R-DONE-SIMPLIFICATION-RECEIPT].
ADVISORY_CHECKS = ("simplification",)

# E2, cycle 2 wave E: `ppl verdict record` used to leave next_safe_action
# naming itself ("dispatch ppl-verifier and record its verdict") even after
# it had just succeeded - repeating it, the only thing the stale text names,
# errors ILLEGAL_TRANSITION. Restamped here by outcome, mirroring
# skill/SKILL.md.skeleton Stage 5's own routing table so the two never drift
# apart - see schema.VERDICT_OUTCOMES for the full closed set this keys.
VERDICT_NEXT_ACTION = {
    "PASS": "verdict is PASS - proceed to Stage 6 (ppl stage enter mr)",
    "REWORK": "ppl budget spend retry, then dispatch a fresh worker with the finding",
    "BLOCKED": "map the finding to an error_code and ppl park --blocker <code> "
              "--safe-next \"...\"",
    "OWNER_DECISION": "ppl escalate --code OWNER_DECISION --question \"<the verifier's "
                      "question>\"",
    "INCONCLUSIVE": "ppl budget spend dispatch, then redispatch verification at the same sha",
    "ERROR": "ppl budget spend dispatch, then redispatch verification at the same sha",
    "TIMEOUT": "ppl budget spend dispatch, then redispatch verification at the same sha",
    "CANCELLED": "ppl escalate --code OWNER_DECISION --question \"verification was "
                "cancelled on <sha> - resume, redispatch, or park?\"",
}


def _path(task_id, sha):
    return paths.run_dir(task_id) / "verdicts" / ("%s.json" % sha)


def _history_dir(task_id, sha):
    return paths.run_dir(task_id) / "verdicts" / sha


def _history_numbers(folder):
    if not folder.is_dir():
        return []
    return sorted(int(p.stem) for p in folder.glob("*.json") if p.stem.isdigit())


def history(task_id, sha):
    """Every verdict recorded for `sha`, oldest first.

    A run created before the history existed has only `verdicts/<sha>.json`,
    which then is the whole history.
    """
    folder = _history_dir(task_id, sha)
    docs = [store.read_json(folder / ("%d.json" % n)) for n in _history_numbers(folder)]
    docs = [d for d in docs if d is not None]
    if docs:
        return docs
    doc = latest(task_id, sha)
    return [doc] if doc is not None else []


def _previous_verdict_outcome(task_id, sha):
    """The outcome of the verdict recorded for `sha` just before this one.

    `verdicts/<sha>.json` only ever holds the latest verdict for that SHA, so
    "the previous outcome" is read from `log.jsonl`, which is append-only and
    keeps every "verdict" event this run has ever recorded, including the one
    this call is about to append below, so it is read here BEFORE that
    append happens.
    """
    events = [e for e in store.read_log(task_id)
             if e.get("event") == "verdict" and e.get("sha") == sha]
    return events[-1].get("verdict") if events else None


def record(task_id, doc, head_sha):
    with role_output():
        schema.validate_verdict(doc)
    # A stale verdict is logged, not counted: it was dispatched against an old head or
    # brief, and a new dispatch fixes it, not a format retry.
    if doc["candidate_sha"] != head_sha:
        raise PplError("SCHEMA_INVALID",
                       "verdict is stale: it was produced for %s but head is %s"
                       % (doc["candidate_sha"], head_sha))
    # The same discipline as the SHA check just above, applied to the brief's
    # gate-relevant fields instead of the git head: `state["spec_revision"]`
    # is only ever rebound by `ppl brief set` or `ppl boundary set`
    # (schema.spec_revision, hashing name_contract, acceptance and the write/
    # forbid boundary together - cycle 2 wave F widened this from
    # name_contract alone), so a verdict produced before either one and
    # recorded after it is stale for the same reason a verdict produced for a
    # different SHA is - the thing it was dispatched against no longer
    # matches the thing that governs the run.
    # Gated on a binding actually existing: with no `ppl brief set` yet,
    # state["spec_revision"] is None and there is nothing to compare the
    # verdict's own value against, so this check accepts it as-is. That is
    # not the floor - it only means `record` cannot be where an unbriefed run
    # is stopped. `done_blockers` refuses one outright (R-DONE-REQUIRES-BRIEF),
    # at the one place a run actually needs to be stopped, regardless of what
    # got recorded here along the way [R-BRIEF-SPEC-REVISION-STALE].
    bound_revision = run.load(task_id).get("spec_revision")
    if bound_revision is not None and doc["spec_revision"] != bound_revision:
        raise PplError("SCHEMA_INVALID",
                       "verdict is stale: it was produced against spec_revision %s but the "
                       "run is now bound to %s - the brief's name_contract, acceptance or "
                       "write boundary changed (ppl brief set or ppl boundary set was called "
                       "again) after this verdict was dispatched against it"
                       % (doc["spec_revision"], bound_revision))
    # Read before the append below records this very call - see
    # _previous_verdict_outcome's own docstring for why.
    previous = _previous_verdict_outcome(task_id, head_sha)
    folder = _history_dir(task_id, head_sha)
    numbers = _history_numbers(folder)
    if not numbers and _path(task_id, head_sha).exists():
        # a run from before the history: keep its one verdict as entry 1
        store.write_json(folder / "1.json", latest(task_id, head_sha))
        numbers = [1]
    store.write_json(folder / ("%d.json" % ((numbers[-1] if numbers else 0) + 1)), doc)
    store.write_json(_path(task_id, head_sha), doc)
    store.append_log(task_id, {"event": "verdict", "sha": head_sha, "verdict": doc["verdict"]})
    # Recorded here, not left to the CLI caller, so it happens whether or
    # not the incomplete-outcome park below raises afterwards - the verdict
    # really was durably recorded against this head either way.
    state = run.load(task_id)
    state["candidate_sha"] = head_sha
    state["next_safe_action"] = VERDICT_NEXT_ACTION.get(
        doc["verdict"], "route the verdict per references/escalation.md")
    run.save(state)
    # A check that did not happen is not the code's fault, so a single
    # incomplete outcome is routed like REWORK (dispatch a fresh verification
    # attempt at the SAME sha - nothing about the code changed, only the
    # check needs retrying). But redispatching a check that keeps failing to
    # produce an answer is not what retrying is for, and doing it forever
    # would let a broken verifier masquerade as an undecided candidate
    # indefinitely. The SECOND consecutive incomplete outcome for this exact
    # sha parks the run here, in code, rather than trusting the orchestrator
    # prose to notice the pattern and stop on its own
    # [R-VERIFICATION-UNAVAILABLE-PARK].
    if (doc["verdict"] in schema.INCOMPLETE_OUTCOMES
            and previous in schema.INCOMPLETE_OUTCOMES):
        run.park(task_id, "VERIFICATION_UNAVAILABLE",
                 "two consecutive incomplete verifications on %s (%s then %s) - inspect "
                 "why the verifier keeps failing to reach a result before dispatching it "
                 "again; do not just redispatch into the same check a third time."
                 % (head_sha, previous, doc["verdict"]),
                 [])
        raise PplError("VERIFICATION_UNAVAILABLE",
                       "verdict recorded, but %s has now failed to reach a result twice "
                       "in a row on %s, and the run has been parked - see parking.json"
                       % (doc["verdict"], head_sha))
    return doc


def latest(task_id, sha):
    return store.read_json(_path(task_id, sha))


def _proven(doc, receipts):
    """Acceptance ids the verdict maps to a receipt that actually passed."""
    proven = set()
    for acceptance_id, ref in ((doc or {}).get("acceptance_evidence", {}) or {}).items():
        if not isinstance(ref, str) or not ref.startswith("evidence:"):
            continue
        check = ref.split(":", 1)[1]
        if receipts.get(check, {}).get("result") == "pass":
            proven.add(acceptance_id)
    return proven


def proven_acceptance(task_id, sha):
    """The same proven set the done-gate computes, for anything that reports it.

    A passing receipt is not an acceptance criterion: one criterion can rest on
    several receipts and a receipt can belong to no criterion at all. Counting
    receipts instead reported progress like 3/1.
    """
    if not sha:
        return set()
    return _proven(latest(task_id, sha), evidence.for_sha(task_id, sha))


def done_blockers(task_id, head_sha, changed_paths):
    state = run.load(task_id)
    reasons = []
    # A run that never called `ppl brief set` at all - `ppl boundary set` is
    # its own command and needs no brief - reaches every check below with
    # nothing recorded: acceptance=[] makes the acceptance loop iterate zero
    # times and pass vacuously, and spec_revision=None used to make the
    # staleness elif below simply never fire, accepting whatever the verdict
    # claimed. Zero acceptance criteria, all "proven", is not a pass - it is a
    # task nobody specified. These are blockers in their own right, not
    # preconditions gating some other check, so a fabricated spec_revision on
    # an unbriefed run is refused here rather than silently let through
    # [R-DONE-REQUIRES-BRIEF].
    if state.get("spec_revision") is None:
        reasons.append("no spec_revision bound for %s - ppl brief set was never called"
                       % task_id)
    if not state.get("acceptance"):
        reasons.append("no acceptance criteria recorded for %s - ppl brief set was never "
                       "called" % task_id)
    doc = latest(task_id, head_sha)
    if doc is None:
        reasons.append("no verdict recorded for candidate %s" % head_sha)
    elif doc["verdict"] != "PASS":
        # Every non-PASS outcome is refused here by the same equality check,
        # deliberately: REWORK, BLOCKED and OWNER_DECISION mean a real check
        # found a real problem, while INCONCLUSIVE, ERROR, CANCELLED and
        # TIMEOUT mean a check did not happen at all - neither is evidence
        # the candidate is good, so neither may pass the gate
        # [R-DONE-VERDICT-PASS]. See schema.VERDICT_OUTCOMES for the full set.
        reasons.append("verdict for %s is %s, not PASS" % (head_sha, doc["verdict"]))
    elif (state.get("spec_revision") is not None
          and doc.get("spec_revision") != state["spec_revision"]):
        # `record`'s check only catches a verdict that arrives AFTER a rebrief
        # already moved state["spec_revision"] - a verdict recorded PASS before
        # the rebrief, on this same head, would otherwise sail through here
        # untouched. The `is not None` guard is not a carve-out for an
        # unbriefed run - that run is already refused above, unconditionally,
        # by R-DONE-REQUIRES-BRIEF - it only stops this elif from firing a
        # second, redundant reason on top of that one
        # [R-BRIEF-SPEC-REVISION-STALE].
        reasons.append("verdict for %s was recorded against spec_revision %s, but the run "
                       "is now bound to %s - the brief's name_contract, acceptance or write "
                       "boundary changed since this verdict was produced, a fresh verdict is "
                       "required"
                       % (head_sha, doc.get("spec_revision"), state["spec_revision"]))

    receipts = evidence.for_sha(task_id, head_sha)
    for check in REQUIRED_CHECKS:
        if receipts.get(check, {}).get("result") != "pass":
            reasons.append("required check %s has no passing receipt on %s" % (check, head_sha))
    for check in ADVISORY_CHECKS:
        if receipts.get(check, {}).get("result") not in ("pass", "skip"):
            reasons.append("advisory check %s has no pass or skip receipt on %s - run the "
                           "simplification pass or file skip with the reason in its log "
                           "[R-DONE-SIMPLIFICATION-RECEIPT]" % (check, head_sha))
    if not run.challenge_current(state):
        reasons.append("no ACCEPTED brief challenge is bound to spec_revision %s - the brief "
                       "was never challenged, or it changed after the challenge "
                       "[R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION]" % state.get("spec_revision"))

    proven = _proven(doc, receipts)
    for acceptance_id in state.get("acceptance", []):
        if acceptance_id not in proven:
            reasons.append("acceptance %s has no evidence" % acceptance_id)

    bound = store.read_json(paths.run_dir(task_id) / "boundary.json", {"write": [], "forbid": []})
    # This loop only ever tested any_match(path, write) or is_test_path(path) -
    # boundary.forbid was never consulted here at all, so a changed path the
    # brief explicitly forbade sailed through the terminal gate as long as it
    # also matched boundary.write or looked like a test source. The guard
    # hook's ppl/boundary.py:check_path checks forbid FIRST, ahead of the
    # test-path exemption, and wins over it - mirrored here in the same order
    # so the done gate proves the same thing the write-time check enforces,
    # rather than only checking presence of a write match (W1, cycle 2 wave B).
    for path in changed_paths:
        if boundary.any_match(path, bound.get("forbid")):
            reasons.append("changed path %s is in the forbidden-path list" % path)
        elif not boundary.any_match(path, bound.get("write")) and not boundary.is_test_path(path):
            reasons.append("changed path %s is outside the write boundary" % path)

    # The registry, not doc["findings"] (retired - see
    # schema.validate_verdict's R-FINDINGS-VERDICT-FIELD-RETIRED check): an
    # own-diff blocking finding not yet marked fixed blocks done, REGARDLESS
    # of head - a defect is not an attestation and does not go stale just
    # because an unrelated commit moved the head (round 1 review, C1 -
    # dropped the affected_sha filter, see
    # ppl/findings.py:open_blocking_on_own_diff). A foreign-track finding
    # never blocks here - it was batch-escalated to its own owner precisely
    # so it does not stall this task.
    open_blocking = findings.open_blocking_on_own_diff(task_id)
    if open_blocking:
        reasons.append("verdict carries an unresolved blocking finding: %s"
                       % ", ".join(f["finding_id"] for f in open_blocking))

    # budget.spend refuses any proposed spend that would exceed the cap, so
    # retry_used can only ever reach exactly retry_max through a real path,
    # never exceed it - a strict `>` here was unreachable in practice (W6,
    # cycle 2 wave B). retry_used == retry_max already means the budget is
    # exhausted (no rework attempts remain), so the comparison is `>=`.
    if state["budgets"]["retry_used"] >= state["budgets"]["retry_max"]:
        reasons.append("retry budget exhausted")
    return reasons

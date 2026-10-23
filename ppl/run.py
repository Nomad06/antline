import os
import shutil
import subprocess
import uuid

from . import fsm, paths, store
from .errors import PplError, role_output

DEFAULT_BUDGETS = {
    "dispatch_used": 0, "dispatch_norm": 21, "dispatch_hard": 34,
    "retry_used": 0, "retry_max": 3,
    "usd_used": 0.0, "usd_max": 0.0,
}

# The only time budget this pipeline has, and it measures inactivity, not
# stage duration: total work is already capped by the dispatch/retry budgets
# above, so wall-clock-per-stage was never this field's job. `stage_enter`
# (and `_new_state`, for admission) stamps `deadline` this far out from the
# moment the stage is entered. `refresh_deadline` restamps it, this far out
# from now, on control-plane activity short of a full stage transition. Past
# it with nothing having refreshed it, `ppl/watchdog.py:check` treats the run
# as abandoned. See DESIGN 6.9 for the three cases that do and do not count
# as activity, and for why this is a fence, not a live timer.
INACTIVITY_TIMEOUT_SECONDS = 4 * 60 * 60


def process_started_at(pid):
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def state_path(task_id):
    return paths.run_dir(task_id) / "state.json"


def active():
    try:
        return paths.active_file().read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def set_active(task_id):
    paths.runs_dir().mkdir(parents=True, exist_ok=True)
    paths.active_file().write_text(paths.check_task_id(task_id) + "\n", encoding="utf-8")


def load(task_id):
    state = store.read_json(state_path(task_id))
    if state is None:
        raise PplError("SCHEMA_INVALID", "no run for %s. Run ppl init first." % task_id)
    return state


def save(state):
    store.write_json(state_path(state["task_id"]), state)


RISK_TIERS = ("low", "normal", "high")


def _new_state(task_id, tracker_url, repo, isolated, risk="normal", model_check="verified",
               harness=None, usd_max=0.0, prices=None, routing_preset=None):
    pid = os.getpid()
    budgets = dict(DEFAULT_BUDGETS)
    budgets["usd_max"] = usd_max
    budgets["prices"] = prices or {}
    return {
        "task_id": task_id,
        "tracker_url": tracker_url,
        "status": "planned",
        "stage": "admission",
        "risk": risk,
        "model_check": model_check,
        "harness": harness,
        "routing_preset": routing_preset,
        "repo": repo,
        "isolated": isolated,
        "branch": None,
        "base_sha": None,
        "candidate_sha": None,
        "spec_revision": None,
        "acceptance": [],
        "attempt_id": None,
        "lease_generation": 1,
        "deadline": store.deadline_in(INACTIVITY_TIMEOUT_SECONDS),
        "last_activity_at": store.now(),
        "budgets": budgets,
        "orchestrator": {"pid": pid, "started_at": process_started_at(pid)},
        "mr": {"iid": None, "url": None, "approved_sha": None,
               "merged_sha": None, "merged_url": None},
        "last_durable_fact": "run initialised",
        "next_safe_action": "complete admission checklist",
        "unsafe_to_repeat": [],
    }


def _routing_preset(presets, routing):
    """The preset routing.toml equals, "custom" when none does, None when it cannot be read."""
    try:
        return presets.matching(routing.load()) or "custom"
    except (PplError, OSError):
        return None


def init(task_id, tracker_url, repo, isolated=True, risk="normal", model_check="verified",
         harness=None):
    paths.check_task_id(task_id)
    if risk not in RISK_TIERS:
        raise PplError("SCHEMA_INVALID", "risk must be one of %s" % ", ".join(RISK_TIERS))
    if state_path(task_id).exists():
        raise PplError("ILLEGAL_TRANSITION",
                       "run %s already exists. Use ppl show, or destroy it first." % task_id)
    # imported here so the hooks, which import this module on every call, never load tomllib
    from . import config, presets, routing, usage
    # The cap and the prices it is computed with are frozen into state.json, which
    # the guard write-protects (R-RUNS-WRITE-PROTECTION), since config.toml stays
    # editable mid-run.
    state = _new_state(task_id, tracker_url, repo, isolated, risk, model_check, harness,
                       usd_max=config.budgets_usd_max(), prices=usage.prices(),
                       routing_preset=_routing_preset(presets, routing))
    save(state)
    set_active(task_id)
    store.append_log(task_id, {"event": "init", "tracker_url": tracker_url, "repo": repo,
                               "model_check": model_check,
                               "harness": (harness or {}).get("name")})
    return state


def _resume_target(origin, resume_stage, target):
    """Refuse anything but the recorded resume point when leaving `origin`
    ("parking" or "escalation").

    D1, cycle 2 wave D: `_FORWARD["parking"]` and `_FORWARD["escalation"]`
    are `set(STAGES) - {"merge_watch"}` in fsm.py, by design, so that
    entering either stage is legal from anywhere (`fsm.check_stage` special-
    cases the target and never even consults the graph for that half). That
    same wide set governs the OTHER direction too - leaving parking or
    escalation for a real stage - and nothing before this function ever
    narrowed it back down. A parked run could legally re-enter any stage
    at all: a run parked while briefing could resume straight into gates,
    silently skipping implementation, tests and verification, still exit 0.
    "ppl decides transitions, not your judgement" was false for every parked
    run since fsm.py's first commit; this function is what makes it true.

    `resume_stage` is untrusted input (a value read off disk, or state a
    caller does not otherwise control) - it must itself be a real, resumable
    stage, not "parking" or "escalation" again (a run parked while already
    parked or escalating, or a hand-edited record, must not chain into a
    second unresolved wait with no further resume point) [R-PARK-RESUME-EXACT].
    A record failing that check is refused the same way
    `ppl/findings.py:_load_all` refuses a registry it cannot trust, rather
    than either silently allowing the old "any stage" behaviour back in or
    silently narrowing to nothing without saying why.

    "admission" is the one legitimate exception, not a hole: it is the value
    `_new_state` sets directly and no code path ever calls `stage_enter` with
    "admission" as a target - re-entering it literally is not an operation
    this system has. A run parked before Stage 1 ever ran (still "admission")
    resumes through admission's own ordinary forward edges (`baseline` or
    `escalation`, per `fsm._FORWARD["admission"]`) instead of being asked to
    do something no legal call has ever done - the same narrow set that
    would have applied had it never been parked at all, not "any" of the
    thirteen stages.
    """
    if resume_stage not in fsm.STAGES or resume_stage in ("parking", "escalation"):
        raise PplError("SCHEMA_INVALID",
                       "the %s record for this run does not name a usable resume stage "
                       "(got %r) - this registry cannot be trusted enough to resume from, "
                       "and guessing would recreate the exact hole this check exists to "
                       "close. An owner has to inspect the run and repair or park it fresh."
                       % (origin, resume_stage))
    if resume_stage == "admission":
        fsm.check_stage("admission", target)
        return
    if target != resume_stage:
        raise PplError("ILLEGAL_TRANSITION",
                       "a run resuming from %s may only enter %s - the stage it was "
                       "actually in when it %s - not %s [R-PARK-RESUME-EXACT]. If %s is "
                       "wrong, that is an owner decision: escalate, do not force a "
                       "different stage." % (origin, resume_stage,
                                             "escalated" if origin == "escalation" else "parked",
                                             target, resume_stage))


def _parking_resume_stage(task_id):
    record = store.read_json(paths.run_dir(task_id) / "parking.json")
    if not isinstance(record, dict):
        # store.read_json already raises SCHEMA_INVALID for malformed JSON -
        # this branch is the "file is simply absent" case, e.g. parking.json
        # deleted by hand, or "parking" reached some other way than
        # `run.park` (see the stage_enter guard below). Missing is not
        # "unrestricted" - it fails the same way a present-but-untrustworthy
        # record does, through _resume_target's own None/unknown check.
        return None
    return record.get("resume_stage")


def stage_enter(task_id, stage):
    state = load(task_id)
    previous = state["stage"]
    # `ppl park` is the only writer of parking.json (the record
    # `_resume_target` fences the eventual resume against). Reaching
    # "parking" any other way would set stage="parking" with no record
    # behind it at all - not a wider hole (the fail-closed check above still
    # refuses the resume), but a confusing one, since the failure would
    # surface later, on a different command, than the mistake that caused
    # it. Refused here, at the point of the mistake, with `ppl park` named
    # as the one legal door.
    if stage == "parking":
        raise PplError("SCHEMA_INVALID",
                       "use `ppl park` to enter parking, not `ppl stage enter parking` - "
                       "park is what records where to resume from; entering the stage "
                       "directly would leave no such record")
    if previous in ("parking", "escalation"):
        resume_stage = (_parking_resume_stage(task_id) if previous == "parking"
                        else state.get("escalation_resume_stage"))
        _resume_target(previous, resume_stage, stage)
    else:
        fsm.check_stage(previous, stage)
        # E1, cycle 2 wave E: fsm.check_stage only knows the stage graph, not
        # whether baseline's own required artifact was ever written. Nothing
        # before this refused leaving "baseline" without a recorded
        # base_sha, so a run could walk brief, implementation, tests, gates,
        # verdict, mr, review, approval and closure - every call exit 0 -
        # and fail only at `ppl transition done`, far past the point where
        # the missing baseline could still be fixed cheaply. Refused here,
        # at the point of the mistake, the same way `ppl baseline record`
        # itself is now gated by stage (D2, cycle 2 wave D) instead of by
        # comparing base_sha to HEAD. Escalation is deliberately exempt:
        # `_resume_target` above already forces a run that escalates out of
        # baseline back into baseline on resume, so nothing is skipped by
        # letting it through here [R-BASELINE-RECORDED-BEFORE-LEAVING].
        if previous == "baseline" and stage != "escalation" and not state.get("base_sha"):
            raise PplError("ILLEGAL_TRANSITION",
                           "cannot leave the baseline stage without a recorded baseline - "
                           "run `ppl baseline record` first [R-BASELINE-RECORDED-BEFORE-LEAVING]")
        # Same shape for the brief: the challenge is briefing's own required
        # artifact, and leaving briefing for implementation without an
        # ACCEPTED challenge bound to the current spec_revision is refused at
        # the point of the mistake. A rebrief moves spec_revision and stales
        # the stored challenge by construction, so a re-brief means a
        # re-challenge [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION].
        if previous == "briefing" and stage == "implementation" and not challenge_current(state):
            raise PplError("ILLEGAL_TRANSITION",
                           "cannot leave briefing for implementation without an ACCEPTED brief "
                           "challenge bound to the current spec_revision - dispatch "
                           "ppl-brief-challenger and run `ppl brief challenge` first "
                           "[R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION]")
    # closure leads only to `ppl transition done` and has no edge back to rework, so a run
    # whose done gate refused there had no legal way on. It enters closure only when the
    # gate would pass at that moment, a resume included, and otherwise stays where it is
    # [R-CLOSURE-ONLY-WHEN-DONE-PASSES].
    if stage == "closure":
        from . import verdict
        blockers = verdict.current_blockers(task_id)
        if blockers:
            where = ("review, where rework (ppl stage enter implementation or tests) and "
                     "evidence repair on the current head are still possible"
                     if previous == "review" else
                     "%s: repair the evidence on the current head (receipt, verdict, finding "
                     "status) before resuming, or escalate OWNER_DECISION" % previous)
            raise PplError("ILLEGAL_TRANSITION",
                           "cannot enter closure while `ppl transition done` would refuse: "
                           "%s. The run stays in %s [R-CLOSURE-ONLY-WHEN-DONE-PASSES]"
                           % ("; ".join(blockers), where))
    # closure -> merge_watch is a legal edge, but merge_watch has no way back, so a run that
    # took it before `ppl transition done` could never be closed, and the tracker gate would
    # sit one `ppl merge observed` away. A resume into merge_watch is held to the same
    # [R-MERGE-WATCH-AFTER-DONE].
    if stage == "merge_watch" and state["status"] != "done":
        raise PplError("ILLEGAL_TRANSITION",
                       "cannot enter merge_watch while the run's status is %s - run "
                       "`ppl transition done` in closure first [R-MERGE-WATCH-AFTER-DONE]"
                       % state["status"])
    if stage == "escalation":
        # The one record escalation needs, kept on state itself rather than
        # a second file next to parking.json: unlike parking, there is
        # nothing else escalation ever writes, and the two real entry edges
        # (admission and briefing, per `fsm._FORWARD`) make one field enough
        # - see `_resume_target` for why re-entering it is refused the same
        # way an untrustworthy parking.json is.
        state["escalation_resume_stage"] = previous
    state["stage"] = stage
    if stage in STAGE_NEXT_ACTION:
        state["next_safe_action"] = STAGE_NEXT_ACTION[stage]
    elif stage == "escalation":
        state["next_safe_action"] = "await the owner's answer to the escalation question"
    if stage == "baseline" and state["status"] == "planned":
        fsm.check_lifecycle("planned", "ready")
        fsm.check_lifecycle("ready", "in_progress")
        state["status"] = "in_progress"
    # A run resuming from "parking" whose lifecycle status is "blocked" would
    # otherwise be stuck there forever: _LIFECYCLE_EDGES["blocked"] is only
    # {"in_progress", "failed"}, so `ppl transition done` (blocked -> done)
    # is illegal, and nothing else in the documented flow ever moves status
    # back to in_progress - the string "in_progress" appears nowhere in
    # skill/, so an orchestrator following only the skill has no
    # documented way out either (W3, cycle 2 wave B). `_resume_target` above
    # has already narrowed which stage this resume is allowed to land on -
    # restoring the lifecycle here, on that same resume, closes the deadlock
    # without inventing a new command the operator has to remember. A run
    # parked before it ever reached "in_progress" (still "planned") is
    # untouched - nothing here fabricates activity that never happened.
    if previous == "parking" and state["status"] == "blocked":
        fsm.check_lifecycle("blocked", "in_progress")
        state["status"] = "in_progress"
    # A fresh attempt id is minted on every stage entry - it names which
    # worker this is, for the log, but it is never what a write gate
    # compares. Only lease_generation is: an attempt captures it here (or
    # from `ppl show --json`) and must hand it back on every write below.
    # `ppl recover` and the watchdog (`ppl/watchdog.py:check`, on a timeout)
    # are the only things that change it (see bump_generation), so a worker
    # that captured a generation before either one ran and only writes after
    # is refused by check_generation, whatever attempt id it carries.
    state["attempt_id"] = uuid.uuid4().hex
    # A fresh deadline too, on every stage entry, not just the first one:
    # entering a stage is control-plane activity, the same as refresh_deadline
    # below - it just also happens to be the moment that mints a fresh
    # attempt_id. See INACTIVITY_TIMEOUT_SECONDS and ppl/watchdog.py:check.
    state["deadline"] = store.deadline_in(INACTIVITY_TIMEOUT_SECONDS)
    state["last_activity_at"] = store.now()
    save(state)
    store.append_log(task_id, {"event": "stage_enter", "from": previous, "to": stage,
                               "status": state["status"], "attempt_id": state["attempt_id"],
                               "lease_generation": state["lease_generation"],
                               "deadline": state["deadline"]})
    return state


def refresh_deadline(task_id):
    """Restamp the inactivity deadline without a stage transition.

    Called from exactly three places, each proving real control-plane
    activity happened: `control.enforce`, after its control-record check
    passes (never before - a run halted or timed out must not look active
    for having merely been asked to act); `require_current_generation`,
    after `check_generation` passes (never before - a stale attempt's
    refused write must not extend the deadline the current attempt is
    racing); and `recover.reconcile`, only on the branch where
    `watchdog.check` did not itself just time the run out (never before
    that check - a refresh placed ahead of it would mask the very hang it
    exists to catch). See DESIGN 6.9 for why a quiet `ppl recover` tick
    counts as activity here even though nothing else happened on it.
    """
    state = load(task_id)
    state["deadline"] = store.deadline_in(INACTIVITY_TIMEOUT_SECONDS)
    state["last_activity_at"] = store.now()
    save(state)
    return state["deadline"]


def check_generation(task_id, generation):
    """Refuse a write from an attempt whose captured lease generation is not
    the run's current one.

    This is a counter compared on write, not a lock file - the same reasoning
    5.4 gives for declining a real lock applies here: a stale lock surviving a
    crashed hook is worse than an imprecise counter. `generation` is the value
    the caller captured earlier (from `ppl stage enter` or `ppl show --json`),
    not something re-read from disk here - a state-read generation would
    always equal itself and fence nothing. See R-LEASE-STALE-WRITE.
    """
    state = load(task_id)
    current = state.get("lease_generation", 1)
    if generation != current:
        raise PplError("ILLEGAL_TRANSITION",
                       "a stale attempt may not write: it holds generation %s, but the run "
                       "is now at generation %s. Re-read the current generation and confirm "
                       "this attempt is still the live one before retrying."
                       % (generation, current))
    return state


def bump_generation(task_id):
    """Fence out every attempt still holding the old generation.

    Called by `ppl recover` unconditionally, and by `ppl/watchdog.py:check`
    when it parks a run for TIMED_OUT - never on its own by an ordinary stage
    transition or write. `ppl recover` itself runs at session start and, in
    the review loop, once per tick, so a well-behaved caller re-captures the
    generation every time it calls recover (or sees a fresh one reported back
    by a command it ran) and passes that value on its next write; only a
    value held across one of those calls it did not itself make - the
    timed-out-and-replaced case this rule exists for - goes stale.
    """
    state = load(task_id)
    state["lease_generation"] = state.get("lease_generation", 1) + 1
    save(state)
    store.append_log(task_id, {"event": "lease_bump", "lease_generation": state["lease_generation"]})
    return state["lease_generation"]


# D6, cycle 2 wave D. `next_safe_action` used to be set once, in `_new_state`,
# to the admission checklist, and nowhere else except `park` (a real,
# reasoned override) and the panel's own default for a run it cannot find.
# Every ordinary stage transition left it exactly as admission wrote it -
# `ppl show --json` at `done` still read "complete admission checklist", a
# stale field in the one JSON contract callers are told to trust
# (status.card, ppl/panel/server.py, ppl/panel/index.html all read it
# verbatim). `stage_enter` now restamps it from this map on every real
# transition, so it always names the next thing to actually do rather than
# the first thing this run ever did [R-NEXT-SAFE-ACTION-CURRENT].
STAGE_NEXT_ACTION = {
    "admission": "complete admission checklist",
    "baseline": "set up the worktree and record the baseline (ppl baseline record)",
    "briefing": "dispatch ppl-briefer and record its brief (ppl brief set)",
    "implementation": "dispatch ppl-code-worker inside the write boundary",
    "tests": "dispatch ppl-test-worker to write tests only",
    "gates": "run the deterministic gates and record a receipt for each required check",
    "verification": "dispatch ppl-verifier and record its verdict (ppl verdict record)",
    "mr": "push the branch and open the merge request",
    "review": "run the review loop (references/review-loop.md)",
    "closure": "transition the run to done (ppl transition done)",
    "merge_watch": ("watch for the merge, record it (ppl merge observed), then write the "
                    "post-merge tracker update"),
    # "parking" and "escalation" are not keyed here: `run.park` sets
    # `next_safe_action` from the caller's own `safe_next` (a real, specific
    # answer to "what do I do about this blocker"), and stage_enter never
    # overwrites it for those two targets - see the "parking" refusal above
    # and the escalation branch below.
}

# The skill file holding each stage's procedure, which `ppl stage enter` names
# as its last line. Parking and escalation have none: references/escalation.md
# is read by error code, not by stage.
STAGE_REFERENCES = {
    "admission": "references/stage-0-admission.md",
    "baseline": "references/stage-1-baseline.md",
    "briefing": "references/stage-2-briefing.md",
    "implementation": "references/stage-3-implementation.md",
    "tests": "references/stage-3-tests.md",
    "gates": "references/stage-4-gates.md",
    "verification": "references/stage-5-verification.md",
    "mr": "references/stage-6-mr.md",
    "review": "references/stage-7-review.md",
    "closure": "references/stage-8-after-merge.md",
    "merge_watch": "references/stage-8-after-merge.md",
}


BLOCKER_CODES = ("SPEC_GAP", "CONTEXT_INSUFFICIENT", "IMPLEMENTATION_FAILURE",
                 "TEST_FAILURE", "BASELINE_FAILURE", "BOUNDARY_VIOLATION",
                 "BUDGET_EXHAUSTED", "OWNER_DECISION", "EXTERNAL_BLOCKED", "TIMED_OUT",
                 "VERIFICATION_UNAVAILABLE", "FORMAT_RETRY_EXHAUSTED")


def park(task_id, blocker, safe_next, unsafe_to_repeat, refusals=None):
    if blocker not in BLOCKER_CODES:
        raise PplError("SCHEMA_INVALID",
                       "blocker must be one of: %s" % ", ".join(BLOCKER_CODES))
    state = load(task_id)
    # A second park (e.g. `ppl park` followed by an operator stop, which
    # calls run.park again through control.set_command) must not overwrite
    # the real resume point with "parking" itself - reading state["stage"]
    # unconditionally used to do exactly that, once the run was already
    # parked (N1, cycle 2 wave B). The FIRST park's resume_stage, preserved
    # from the existing parking.json when one is already on disk, is what a
    # later resume actually needs.
    existing = store.read_json(paths.run_dir(task_id) / "parking.json")
    resume_stage = (existing or {}).get("resume_stage") if state["stage"] == "parking" \
        else state["stage"]
    record = {
        "task_id": task_id,
        "resume_stage": resume_stage,
        "candidate_sha": state.get("candidate_sha"),
        "last_durable_fact": state.get("last_durable_fact"),
        "blocker": blocker,
        "owner": "repository-owner",
        "safe_next_action": safe_next,
        "unsafe_to_repeat": list(unsafe_to_repeat or []),
        "at": store.now(),
    }
    if refusals:
        record["refusals"] = list(refusals)
    store.write_json(paths.run_dir(task_id) / "parking.json", record)
    state["stage"] = "parking"
    if state["status"] == "in_progress":
        state["status"] = "blocked"
    state["next_safe_action"] = safe_next
    save(state)
    store.append_log(task_id, {"event": "park", "blocker": blocker})
    return state


def destroy(task_id, scope):
    """Local scope is file work and happens here. Remote scope is an outward mutation and
    is queued for the orchestrator, which is the only component holding GitLab credentials.
    """
    if scope not in ("local", "full"):
        raise PplError("SCHEMA_INVALID", "destroy scope must be local or full")
    state = load(task_id)
    if scope == "full":
        store.write_json(paths.run_dir(task_id) / "remote_cleanup.json", {
            "task_id": task_id, "scope": "full",
            "mr": state.get("mr"), "branch": state.get("branch"),
            "requested_at": store.now(),
        })
        store.append_log(task_id, {"event": "destroy", "scope": "full",
                                   "status": "queued_remote_cleanup"})
        return {"status": "queued_remote_cleanup",
                "next": "orchestrator closes the MR and deletes the remote branch, then "
                        "runs ppl destroy --scope local"}

    repo = state.get("repo")
    worktree = "absent"
    worktree_error = None
    if repo and os.path.isdir(repo):
        # -C <repo> matters: without it git runs against ppl's own cwd, which is
        # not the target repository, so the removal silently did nothing and the
        # discarded exit code made the archive below report success anyway,
        # leaving an orphan worktree with nothing pointing at it.
        removal = subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(repo)],
            capture_output=True, text=True)
        if removal.returncode == 0:
            worktree = "removed"
        else:
            worktree = "left_behind"
            worktree_error = (removal.stderr.strip() or removal.stdout.strip()
                              or "git worktree remove exited %d" % removal.returncode)
    # Logged before the move: append_log would otherwise recreate the run
    # directory it has just archived.
    store.append_log(task_id, {"event": "destroy", "scope": "local",
                               "worktree": worktree, "worktree_error": worktree_error})
    # store.now() has second resolution, so appending a random suffix keeps two
    # destroys of the same task id (even inside the same second) from colliding:
    # a name collision would make shutil.move nest the second archive inside the
    # first instead of placing it beside it.
    archive = paths.runs_dir() / "_archive" / ("%s-%s-%s" % (
        task_id, store.now().replace(":", ""), uuid.uuid4().hex[:8]))
    archive.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(paths.run_dir(task_id)), str(archive))
    if active() == task_id:
        paths.active_file().unlink(missing_ok=True)
    result = {"status": "archived", "archive": str(archive), "worktree": worktree}
    if worktree_error:
        # The run is archived either way, but a destroy that could not remove
        # the worktree has to say so rather than report a clean success.
        result["worktree_error"] = worktree_error
    return result


def record_tracker_fetch(task_id, revision):
    """Bind the revision of the issue ppl tracker fetch read to the run
    [R-TRACKER-FETCH-PROVENANCE]."""
    state = load(task_id)
    state["tracker_revision"] = revision
    save(state)
    store.append_log(task_id, {"event": "tracker_fetch", "revision": revision})
    return state


def challenge_current(state):
    """True when an ACCEPTED brief challenge is bound to the run's current
    spec_revision. Read by stage_enter (briefing -> implementation) and by the
    done gate, so a rebrief after the challenge cannot slip through either
    [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION]."""
    record = state.get("brief_challenge") or {}
    return (record.get("verdict") == "ACCEPTED"
            and state.get("spec_revision") is not None
            and record.get("spec_revision") == state.get("spec_revision"))


def record_challenge(task_id, doc):
    """Bind a brief-challenge report to the run. The document is refused when
    it contradicts itself (schema.validate_challenge) or when it was written
    against a spec_revision the run no longer carries."""
    from . import schema
    state = load(task_id)
    with role_output():
        schema.validate_challenge(doc)
    if state.get("spec_revision") is None:
        raise PplError("SCHEMA_INVALID",
                       "no spec_revision bound for %s - run ppl brief set before ppl brief "
                       "challenge" % task_id)
    # stale, not malformed: logged, not counted toward the retry cap
    if doc["spec_revision"] != state["spec_revision"]:
        raise PplError("SCHEMA_INVALID",
                       "challenge was written against spec_revision %s but the run is bound to "
                       "%s - the brief changed since the challenger read it, dispatch it again"
                       % (doc["spec_revision"], state["spec_revision"]))
    store.write_json(paths.run_dir(task_id) / "challenge.json", doc)
    state["brief_challenge"] = {"spec_revision": doc["spec_revision"],
                                "verdict": doc["verdict"], "at": store.now()}
    if doc["verdict"] == "ACCEPTED":
        state["next_safe_action"] = "ppl stage enter implementation"
    else:
        state["next_safe_action"] = ("re-brief with the challenger's revisions (ppl brief set), "
                                     "then dispatch ppl-brief-challenger again")
    save(state)
    store.append_log(task_id, {"event": "brief_challenge", "verdict": doc["verdict"],
                               "spec_revision": doc["spec_revision"]})
    return state

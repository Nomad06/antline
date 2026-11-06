import argparse
import json
import os
import re
import stat
import sys

from . import (__version__, boundary, budget, config, control, doctor, evidence, findings, fsm, gitfacts,
               harness, lane_launch, lanes, lessons, metrics, models, os_sandbox, paths, recover,
               refusals, review, routing, run, schema, setup, status, store, trackers, trail,
               usage, verdict, watchdog)
from .errors import PplError, role_output
from .forges import github_fetch


def resolve_task(args):
    """The run a command is about: --task, else PPL_TASK, else the lane whose start directory or
    worktree holds the cwd (lanes.task_for_call), else SCHEMA_INVALID naming ppl lane."""
    task_id = lanes.task_for_call(getattr(args, "task", None))
    if not task_id:
        raise PplError("SCHEMA_INVALID",
                       "no --task given and not inside a lane; start one with ppl lane <link>")
    return task_id


def require_current_generation(task_id, generation):
    """The shared gate behind evidence add, verdict record, review answered
    and apply-patch: refuse a write from a stale attempt.

    A missing `--generation` is refused rather than defaulted to the run's
    current value - a default read here would always match, because a stale
    attempt calling this exact code takes this exact path, so a state-read
    default would fence nothing. The caller must hand back the value it
    captured earlier from `ppl stage enter` or `ppl show --json`
    [R-LEASE-STALE-WRITE].
    """
    # Checked before the missing-generation case below, not after: evidence,
    # verdict and apply-patch never call control.enforce (see its own
    # comment), so this is the only place any of the three learns their run
    # timed out - a caller that also forgot --generation should still be told
    # the more consequential fact.
    result = watchdog.check(task_id)
    if result["timed_out"]:
        raise PplError("TIMED_OUT",
                       "run %s has had no ppl activity since its deadline and has been "
                       "parked (lease generation is now %d). Run `ppl recover` before doing "
                       "anything else." % (task_id, result["lease_generation"]))
    if generation is None:
        raise PplError("SCHEMA_INVALID",
                       "--generation is required: capture it from `ppl stage enter` or "
                       "`ppl show --json` and pass it back unchanged")
    run.check_generation(task_id, generation)
    # Refreshed only after check_generation passes, never before: a stale
    # attempt's refused write must not extend the deadline the current
    # attempt is racing (R-WATCHDOG-DEADLINE).
    run.refresh_deadline(task_id)


def role_output_command(args):
    """`<command> <action>` when this call records a role's output, else None."""
    name = "%s %s" % (args.command, getattr(args, "action", None))
    return name if name in refusals.COMMANDS else None


def require_retry_left(task_id, args):
    """Checked after the run's own gates and before the input is read: the call has to
    name whose output it records, and a spent key is refused whatever the input
    [R-REFUSAL-RETRY-CAP]. A log that cannot be read is a bookkeeping failure like the
    ones count_refusal reports, and the input is validated as usual."""
    command = role_output_command(args)
    if args.from_role not in refusals.ROLES:
        given = "needs" if args.from_role is None else "got --from %s, it needs" % args.from_role
        raise PplError("SCHEMA_INVALID",
                       "ppl %s %s --from <role>, the role whose output it records: pass "
                       "--from %s for that role's output, or another of %s"
                       % (command, given, refusals.EXPECTED_ROLE[command],
                          ", ".join(refusals.ROLES)))
    try:
        refusals.require_retry_left(task_id, command, args.from_role)
    except refusals.CountUnreadable as exc:
        bookkeeping_failed(task_id, command, args.from_role, exc)


def load_json_arg(path, what, kind=dict):
    """A missing or malformed input file is a routable error, not a traceback.

    Valid JSON of the wrong shape is malformed too: a JSON array where a
    document is expected used to reach the command body and die there on a bare
    AttributeError, exit 1, outside the documented 0/1/2 contract - and after
    the command had already written part of the run.
    """
    return parse_json_doc(read_arg(path, what), path, what, kind)


def load_role_output(path, what):
    """load_json_arg for a role's output: a file that cannot be read is how the command
    was called, a body that is not a JSON object is what the role returned."""
    text = read_arg(path, what)
    with role_output():
        return parse_json_doc(text, path, what, dict)


def read_arg(path, what):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError as exc:
        raise PplError("SCHEMA_INVALID", "%s file %s could not be read: %s" % (what, path, exc))


def parse_json_doc(text, path, what, kind):
    doc = parse_json_arg(text, "%s file %s" % (what, path))
    if kind is not None and not isinstance(doc, kind):
        raise PplError("SCHEMA_INVALID",
                       "%s file %s must contain a JSON %s"
                       % (what, path, "array" if kind is list else
                          "array or object" if isinstance(kind, tuple) else "object"))
    return doc


def parse_json_arg(text, what):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PplError("SCHEMA_INVALID", "%s is not valid JSON: %s" % (what, exc))


def os_sandbox_problem(inspected, cwd=None):
    """None when this session's OS sandbox denies GitHub and the guard is registered in the
    lane's settings.json alone, else a message naming the first failing fact. `inspected` is
    doctor.inspect of the lane, `cwd` the lane's start directory (the lane this call is in when
    None)."""
    facts = doctor.os_sandbox_facts(cwd)
    settings = doctor.local_settings_path(cwd)
    if facts["state"] != "on":
        return ("this harness reaches GitHub and the OS sandbox is %s for this session (%s): "
                "start the task's session with ppl lane <link>, or pass --no-os-sandbox to "
                "record a run without it" % (facts["state"], _why_off(facts, settings)))
    if inspected["registration"] != "local":
        other = (inspected.get("start_local") or {}).get("path") or \
            "its start directory's .claude/settings.local.json"
        return ("the guard hook is registered %s, not in the lane's %s alone: ppl lane renders "
                "it there, and the lane session loads %s as well, so a guard there would run "
                "beside it. Fix that, or pass --no-os-sandbox to record a run without the OS "
                "sandbox" % (inspected["registration"], settings, other))
    return None


def _why_off(facts, settings):
    if facts["state"] == "unavailable":
        return "native Windows has no OS sandbox"
    if not facts["env_marker"]:
        return "this session is not running inside it"
    if not facts["profile"]:
        return ("ppl's profile in %s is missing or is not the one the lane's os_sandbox.json "
                "records" % settings)
    if facts["undoing"]:
        return "a setting undoes it"
    return "GitHub is reachable from here"


def _init_lane(args):
    """The registry entry of the lane `ppl init` runs in: the one PPL_TASK, else the cwd, binds,
    which must be this task's (R-LANE-INIT-INSIDE)."""
    bound = lanes.task_for_call(None)
    if bound is None:
        raise PplError("SETUP_INCOMPLETE",
                       "ppl init runs inside its task's lane, and this call is in none (no "
                       "PPL_TASK, and no lane's start directory or worktree holds the cwd): start "
                       "one with ppl lane <link>")
    if bound != args.task_id:
        raise PplError("SCHEMA_INVALID",
                       "this session is lane %s's, so it initialises %s, not %s: start %s's own "
                       "lane with ppl lane <link>" % (bound, bound, args.task_id, args.task_id))
    entry = lanes.entry(bound)
    if entry is None:
        raise PplError("SETUP_INCOMPLETE",
                       "PPL_TASK names %s, which holds no lane: start one with ppl lane <link>"
                       % bound)
    return entry


def _lane_harness(args, entry):
    """The harness the lane exported into its start directory, loaded: never the installed one,
    which may have moved on since the lane started [R-INIT-HARNESS-APPLIED]."""
    name = entry.get("harness")
    if args.harness and args.harness != name:
        raise PplError("SCHEMA_INVALID",
                       "--harness %s is not lane %s's harness %s: leave --harness out, ppl init "
                       "runs the harness its lane exported" % (args.harness, args.task_id, name))
    snapshot = os.path.join(str(entry.get("start_dir")), "harness", str(name))
    if not os.path.isfile(os.path.join(snapshot, "harness.toml")):
        raise PplError("SETUP_INCOMPLETE",
                       "lane %s has no harness snapshot at %s: destroy the lane with ppl destroy "
                       "--scope local --task %s and start it again with ppl lane <link>"
                       % (args.task_id, snapshot, args.task_id))
    return harness.load(snapshot)


def _lane_repo(args, entry):
    """The lane's worktree, which --repo may name only by another spelling of the same path."""
    worktree = entry.get("worktree")
    if args.repo and os.path.realpath(args.repo) != os.path.realpath(str(worktree)):
        raise PplError("SCHEMA_INVALID",
                       "--repo %s is not lane %s's worktree %s: leave --repo out, ppl init takes "
                       "the lane's" % (args.repo, args.task_id, worktree))
    return worktree


def cmd_init(args):
    # A run belongs to its lane: ppl init runs in the session ppl lane started for the task, and
    # takes the lane's worktree and the harness it exported [R-LANE-INIT-INSIDE].
    entry = _init_lane(args)
    repo = _lane_repo(args, entry)
    where = str(entry.get("start_dir"))
    # The run starts only under its lane's guard: the settings.json ppl lane rendered registers
    # it with this task on its command line. A registered matcher that never passes some guarded
    # tool name to the hook is refused (doctor.matcher_problem): a denial the hook cannot see is
    # a denial that cannot fire, and the run would start believing otherwise. So is a guard
    # entry of the wrong shape.
    inspected = doctor.inspect(where)
    problem = (doctor.lane_guard_problem(entry) or doctor.matcher_problem(inspected)
               or doctor.guard_shape_problem(where))
    if problem:
        raise PplError("SCHEMA_INVALID", problem)
    # Setup is checked before the model gate so a missing models step is told as
    # "run ppl setup models", and --skip-model-check waives only that one step.
    state = doctor.setup_state()
    problem = doctor.setup_problem(state, args.skip_model_check)
    if problem:
        raise PplError("SETUP_INCOMPLETE", problem)
    if args.skip_model_check:
        model_check = "skipped"
    else:
        models.require_verified(args.risk)
        model_check = "verified"
    doc = _lane_harness(args, entry)
    os_sandboxed = False
    if os_sandbox.needed(doc) and args.os_sandbox:
        problem = os_sandbox_problem(inspected, where)
        if problem:
            raise PplError("SCHEMA_INVALID", problem)
        os_sandboxed = True
    pattern = doc["task"]["key_pattern"]
    if not re.match(pattern, args.task_id):
        raise PplError("SCHEMA_INVALID",
                       "task id %s must match the harness key pattern %s" % (args.task_id, pattern))
    harness_record = {"name": harness.installed_name(doc), "path": doc["_dir"],
                      "format": harness.FORMAT}
    snapshot = harness.snapshot(doc)
    lane = {"number": entry.get("lane"), "start_dir": entry.get("start_dir"),
            "session_id": entry.get("session_id")}
    state = run.init(args.task_id, args.tracker_url, repo, risk=args.risk,
                     model_check=model_check, harness=harness_record,
                     os_sandboxed=os_sandboxed, lane=lane)
    store.write_json(paths.run_dir(args.task_id) / "harness.json", snapshot)
    # after the run exists, so a refusal's stderr stays the one JSON line it is
    for warning in (doctor.dispatch_warning(where), doctor.usage_warning(where),
                    doctor.hook_location_warning(where), doctor.matcher_warning(where),
                    doctor.mcp_servers_warning(where),
                    doctor.github_token_warning() if os_sandbox.needed(doc) else None,
                    doctor.foreign_bash_hooks_warning(where) if os_sandbox.needed(doc) else None):
        if warning:
            sys.stderr.write("warning: %s\n" % warning)
    print(status.card(state["task_id"]))
    return 0


def cmd_doctor(args):
    if getattr(args, "forge", False):
        print(json.dumps(doctor.forge_report(), sort_keys=True))
        return 0
    # a --task naming another run than PPL_TASK is refused as every command refuses it, and a
    # registry that cannot be read is reported, not refused
    given = getattr(args, "task", None)
    if given and os.environ.get("PPL_TASK"):
        lanes.task_for_call(given)
    result = doctor.report(given)
    print(json.dumps(result, sort_keys=True))
    lane = result.get("lane")
    if not lane:
        # outside a lane only the machine is checked, and a lane named but not confirmed says why
        if result.get("lane_problem"):
            print(result["lane_problem"])
        warning = doctor.github_token_warning()
        if warning:
            print("warning: %s" % warning)
        return 0
    where = result["start_dir"]
    for line in (result.get("lane_guard_problem"), doctor.matcher_problem(result),
                 doctor.guard_shape_problem(where), doctor.dispatch_warning(where),
                 doctor.usage_warning(where), doctor.hook_location_warning(where)):
        if line:
            print(line)
    for warning in (doctor.matcher_warning(where), doctor.mcp_servers_warning(where),
                    doctor.github_token_warning(), _foreign_hooks_warning(lane, where)):
        if warning:
            print("warning: %s" % warning)
    if result.get("hook_runs_problem"):
        print(result["hook_runs_problem"])
    return 0


def _foreign_hooks_warning(lane, where):
    """The foreign Bash hook warning while the lane has an OS sandbox profile, else None."""
    try:
        applied = os_sandbox.load_facts(lane) is not None
    except PplError:
        applied = True
    return doctor.foreign_bash_hooks_warning(where) if applied else None


def cmd_show(args):
    state = run.load(resolve_task(args))
    print(json.dumps(state, indent=2, sort_keys=True) if args.json else status.card(state["task_id"]))
    return 0


def cmd_status(args):
    print(status.card(resolve_task(args)))
    return 0


def check_dispatch_budget(task_id):
    """The dispatch hard cap has no other enforcement anywhere.

    SubagentStart cannot block a dispatch (DESIGN 12.1), so this refusal at
    stage boundaries and lifecycle transitions is the whole of it. ppl park
    stays available, which is the designed way out of an exhausted budget.
    """
    budgets = run.load(task_id)["budgets"]
    if budgets["dispatch_used"] >= budgets["dispatch_hard"]:
        raise PplError("BUDGET_EXHAUSTED",
                       "dispatch budget is spent: %s of %s. Park the task with a handoff "
                       "(ppl park) instead of continuing."
                       % (budgets["dispatch_used"], budgets["dispatch_hard"]))


def check_usd_budget(task_id):
    """The dollar cap is checked after the fact, like the dispatch cap.

    What a dispatch cost is known only once its role stops and the usage hook
    logs it, so the dispatch that crosses usd_max has already run and this
    refusal at stage boundaries and lifecycle transitions is the whole of the
    enforcement [R-BUDGET-USD-CAP]. Spent is usd_used, the manual `ppl budget
    spend usd`, plus the usage priced with the table ppl init froze into the
    run, so a price edited in config.toml mid-run moves nothing. A usd_used that
    is not a finite number >= 0 counts as spent. A recorded model without a
    frozen price leaves the cap uncomputable, and a cap that cannot be computed
    is refused, never read as zero [R-BUDGET-USD-UNPRICED]. A zero cap is no cap,
    and ppl park stays available. The machine-wide day cap is checked first, whatever
    the run's own cap [R-BUDGET-USD-PER-DAY].
    """
    problem = usage.day_cap_problem(task_id)
    if problem:
        raise PplError(problem[0], "%s Park the task with a handoff (ppl park) instead "
                       "of continuing." % problem[1])
    budgets = run.load(task_id)["budgets"]
    cap = budgets["usd_max"]
    if not cap > 0:
        return
    if not usage.valid_usd_used(budgets.get("usd_used")):
        raise PplError("BUDGET_EXHAUSTED",
                       "usd_used is %r, not a finite number >= 0, so the dollar budget counts as "
                       "spent. Park the task with a handoff (ppl park) instead of continuing."
                       % (budgets.get("usd_used"),))
    spent, unpriced = usage.spent_usd(task_id)
    if unpriced:
        raise PplError("OWNER_DECISION",
                       "the dollar cap (usd_max %s) cannot be checked: the prices this run took "
                       "from config.toml at ppl init have none for the recorded model%s %s. Add "
                       "%s with input, cache_write, cache_read and output in USD per million "
                       "tokens to %s and start a new run, or park this one (ppl park)."
                       % (cap, "s" if len(unpriced) > 1 else "", ", ".join(unpriced),
                          ", ".join('[prices."%s"]' % model for model in unpriced),
                          paths.config_file()))
    if spent >= cap:
        raise PplError("BUDGET_EXHAUSTED",
                       "dollar budget is spent: %s of %s USD (usd_used plus the priced usage). "
                       "Park the task with a handoff (ppl park) instead of continuing."
                       % (round(spent, 4), cap))


def refresh_orchestrator_usage(task_id):
    """Counts the orchestrator's turns since the last role stopped. A refresh never
    changes the command's outcome: a failure is a warning."""
    try:
        usage.refresh_orchestrator(task_id)
    except Exception as exc:
        sys.stderr.write("warning: orchestrator usage not refreshed: %s\n" % exc)


def cmd_stage(args):
    task_id = resolve_task(args)
    control.enforce(task_id)
    # before the cap check, so the orchestrator's turns up to this call count
    refresh_orchestrator_usage(task_id)
    check_dispatch_budget(task_id)
    check_usd_budget(task_id)
    state = run.stage_enter(task_id, args.stage)
    print("stage: %s" % state["stage"])
    # The value to capture and pass back as --generation on every write below
    # (evidence add, verdict record, review answered, apply-patch) until the
    # next `ppl recover` call, or until the watchdog bumps it out from under a
    # deadline this attempt overran [R-LEASE-STALE-WRITE].
    print("generation: %d" % state["lease_generation"])
    reference = run.STAGE_REFERENCES.get(state["stage"])
    if reference:
        print("Before anything in this stage, read %s" % reference)
    return 0


def _run_tracker(task_id):
    """(adapter, [tracker] table) the run froze: the Jira defaults when it has no snapshot or
    one from before trackers, OWNER_DECISION when the snapshot cannot be trusted."""
    snapshot = paths.run_dir(task_id) / "harness.json"
    try:
        mode = os.lstat(snapshot).st_mode
    except FileNotFoundError:
        mode = None
    unreadable = PplError("OWNER_DECISION",
                          "the tracker of %s cannot be read from its harness snapshot - restore "
                          "%s or re-init the run" % (task_id, snapshot))
    if mode is None:
        return trackers.get("jira"), dict(trackers.get("jira").DEFAULTS, kind="jira")
    if not stat.S_ISREG(mode):
        raise unreadable
    try:
        doc = store.read_json(snapshot)
    except PplError:
        raise unreadable
    if not isinstance(doc, dict):
        raise unreadable
    if "tracker" not in doc:
        return trackers.get("jira"), dict(trackers.get("jira").DEFAULTS, kind="jira")
    table = doc["tracker"]
    if not trackers.usable(table):
        raise unreadable
    kind = table.get("kind", trackers.DEFAULT_KIND)
    return trackers.get(kind), table


def require_tracker_fetched(task_id):
    """A CLI tracker's briefer reads the files ppl tracker fetch wrote, so ppl brief set
    waits for the fetch. Refused before the retry cap, which is not charged
    [R-TRACKER-FETCH-PROVENANCE]."""
    adapter, _ = _run_tracker(task_id)
    if adapter.TRANSPORT == "cli" and not run.load(task_id).get("tracker_revision"):
        raise PplError("ILLEGAL_TRANSITION",
                       "the issue of %s was not fetched: run `ppl tracker fetch` first, then "
                       "dispatch ppl-briefer" % task_id)


def cmd_tracker(args):
    # It runs outside the OS sandbox with gh's credentials, so it trusts no file a role can
    # write for where it reaches or where it writes: the run directory is checked before
    # anything is written there, the repository comes from the guard facts.
    task_id = resolve_task(args)
    dest = store.checked_run_path(paths.run_dir(task_id) / "tracker")
    control.enforce(task_id)
    adapter, table = _run_tracker(task_id)
    if adapter.TRANSPORT != "cli":
        raise PplError("SCHEMA_INVALID",
                       "the %s tracker of %s is read through MCP tools, ppl tracker fetch is "
                       "for a CLI tracker" % (adapter.KIND, task_id))
    if adapter.KIND == "github-issues":
        table = dict(table, repo=os_sandbox.pinned_repo("tracker_repo", table.get("repo"),
                                                        task_id))
    result = adapter.fetch(table, task_id, dest)
    store.checked_run_path(paths.run_dir(task_id))
    run.record_tracker_fetch(task_id, result["revision"])
    print("issue: %s" % (dest / "issue.md"))
    print("revision: %s" % result["revision"])
    return 0


def cmd_brief(args):
    # E5, cycle 2 wave E: `ppl brief set`, unlike `ppl baseline record`
    # (D2), carries no stage gate, and that asymmetry was examined rather
    # than closed for symmetry's sake. Baseline's gate exists because a
    # wrongly-timed record can reach the done gate silently - base_sha feeds
    # changed_paths directly and nothing downstream re-checks WHEN it was
    # written. A wrongly-timed `ppl brief set` cannot do the analogous
    # damage: it does not advance state["stage"] itself, so the FSM graph
    # still refuses whatever real stage transition would need to follow it
    # until `ppl stage enter briefing` legitimately happens (a call this
    # command does not skip) - and any brief change that matters - a
    # name_contract, acceptance or boundary edit - already invalidates every
    # verdict recorded against the old one via the spec_revision staleness
    # check in ppl/verdict.py:record and ppl/verdict.py:done_blockers
    # (R-BRIEF-SPEC-REVISION-STALE), regardless of which stage the run was
    # in when `ppl brief set` ran. A brief is also legitimately repeatable
    # (fsm._FORWARD allows "implementation"/"verification" back to
    # "briefing" for exactly this), unlike baseline's write-once field, so a
    # symmetric once-only stage gate would not even fit its shape. Left
    # ungated on purpose, not left unexamined.
    #
    # Cycle 2 wave F closed the residual E5 left open: spec_revision used to
    # hash name_contract only, so a re-brief that shrank state["acceptance"]
    # without touching name_contract sailed past the staleness check - see
    # schema.spec_revision's docstring for the full fix.
    task_id = resolve_task(args)
    control.enforce(task_id)
    if args.action == "challenge":
        require_current_generation(task_id, args.generation)
        require_retry_left(task_id, args)
        doc = load_role_output(args.file, "challenge")
        state = run.record_challenge(task_id, doc)
        print("challenge %s bound to spec_revision %s"
              % (doc["verdict"], state["brief_challenge"]["spec_revision"]))
        return 0
    require_tracker_fetched(task_id)
    require_retry_left(task_id, args)
    doc = load_role_output(args.file, "brief")
    with role_output():
        schema.validate_brief(doc)
    store.write_json(paths.run_dir(task_id) / "brief.json", doc)
    bound = {"write": doc["allowed_write_paths"], "forbid": doc["forbidden_paths"]}
    store.write_json(paths.run_dir(task_id) / "boundary.json", bound)
    state = run.load(task_id)
    state["acceptance"] = doc["acceptance"]
    # Binds a fingerprint of every gate-relevant brief field to the run - see
    # schema.spec_revision's docstring for exactly which fields and why. A
    # later `ppl brief set` that changes any of them rebinds this to a new
    # value, which is what makes a verdict recorded against the old one stale
    # (R-BRIEF-SPEC-REVISION-STALE in ppl/verdict.py:record and
    # ppl/verdict.py:done_blockers).
    revision = schema.spec_revision(doc["name_contract"], doc["acceptance"],
                                    doc["allowed_write_paths"], doc["forbidden_paths"])
    state["spec_revision"] = revision
    # A brief that changed is a brief that was not challenged: the stored
    # challenge stays but no longer matches, which is exactly what
    # challenge_current reads [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION].
    state["next_safe_action"] = ("dispatch ppl-brief-challenger, then ppl brief challenge, "
                                 "then ppl stage enter implementation")
    run.save(state)
    print("brief accepted, %d acceptance ids, boundary set, spec_revision %s"
          % (len(doc["acceptance"]), revision))
    return 0


def cmd_boundary(args):
    task_id = resolve_task(args)
    control.enforce(task_id)
    bound = load_json_arg(args.file, "boundary")
    if not bound.get("write"):
        raise PplError("SCHEMA_INVALID", "write boundary must not be empty")
    # "**" matches every path in the tree, which makes the done gate's
    # diff-boundary check (condition 4, R-DONE-DIFF-BOUNDARY) unconditionally
    # true - and this command can be called AFTER a verdict already passed,
    # the same shape as the C3 baseline attack: rewrite the criterion the
    # gate reads, then run the gate (N5, cycle 2 wave B). Refused at the
    # narrowest point that closes it outright, rather than attempting to
    # judge every merely-too-wide-but-not-"**" glob.
    if "**" in bound["write"]:
        raise PplError("SCHEMA_INVALID",
                       "write boundary must not include '**' - that matches every path in "
                       "the tree and makes the done gate's diff-boundary check "
                       "unconditionally true")
    store.write_json(paths.run_dir(task_id) / "boundary.json", bound)
    # Reopened N5 ruling, cycle 2 wave F. Wave B decided a boundary change
    # should NOT stale an already-recorded PASS, reasoning that
    # done_blockers re-reads boundary.json fresh on every `ppl transition
    # done` call - so refusing the one degenerate value ("**") that made
    # the check vacuous was enough. That freshness is exactly what makes
    # the acceptance-shrink defect this same wave fixes work: a criterion
    # the gate re-reads live is a criterion the same session can rewrite to
    # fit a diff that already exists, whether the criterion is "which
    # acceptance ids remain" or "which paths this diff may touch". Widening
    # `write` to newly cover a path an already-produced, already-blocked
    # diff escaped to is that same move with a different noun. Re-decided:
    # rebind spec_revision here too, using the run's current name_contract
    # and acceptance, so ANY boundary edit - widen, narrow or an honest
    # correction alike - stales an outstanding verdict the same way a
    # name_contract or acceptance edit already does. This trades away wave
    # B's "a narrowing is low-cost and should not force re-verification"
    # convenience for closing the loophole with one mechanism instead of
    # judging widen-vs-narrow case by case (which needs a superset check on
    # arbitrary globs this codebase has no general way to compute).
    # Skipped when no spec_revision has ever been bound (no `ppl brief set`
    # yet, or a boundary-only run): there is no name_contract or acceptance
    # to combine it with, and R-DONE-REQUIRES-BRIEF already refuses such a
    # run at `ppl transition done` regardless of what its boundary says
    # [R-BRIEF-SPEC-REVISION-STALE].
    state = run.load(task_id)
    if state.get("spec_revision") is not None:
        brief = store.read_json(paths.run_dir(task_id) / "brief.json", {})
        state["spec_revision"] = schema.spec_revision(
            brief.get("name_contract", {}), state.get("acceptance", []),
            bound.get("write", []), bound.get("forbid", []))
        run.save(state)
    print("boundary set")
    return 0


def cmd_baseline(args):
    task_id = resolve_task(args)
    control.enforce(task_id)
    doc = load_json_arg(args.file, "baseline")
    state = run.load(task_id)
    # D2, cycle 2 wave D. Wave B's fix for C3 refused base_sha == HEAD
    # outright, on the reasoning that recording a baseline at the tip of the
    # work it is meant to precede always empties the done-gate's diff. That
    # reasoning is right about the DANGER and wrong about WHERE to catch it:
    # on a freshly cut branch with no commits of its own yet - exactly where
    # the skill's Stage 1 tells the orchestrator to record the baseline - HEAD
    # IS the correct base_sha, and there is no other commit to name. The
    # walker driving this skill had to invent HEAD~1 to get past the old
    # refusal, and a branch cut from a fresh tip would have had no way out
    # at all. The real hazard is recording a baseline AFTER work has already
    # landed, not the value coinciding with HEAD - so it is caught by WHEN
    # this call happens, not by comparing two commits that read identically
    # in git for "fresh branch" and "recorded late" alike.
    #
    # the skill's Stage 1 already sequences `ppl stage enter baseline` before
    # this command - a run whose stage has moved past "baseline" (implementation,
    # gates, or later) recording one now is exactly the late-repair shape C3
    # named, whatever commit it names. Refused here instead of at the
    # base_sha/HEAD comparison [R-BASELINE-NOT-AT-HEAD].
    #
    # Residual, stated rather than hidden: a commit made DURING the baseline
    # stage (before this call, after entering it) still passes, because
    # nothing here inspects what has already landed on the branch by the
    # time this call runs - it only checks when the call happens, not what
    # else happened in between. Closing that would need comparing against
    # the target branch's own history (a merge-base check), which the run's
    # state does not currently carry enough to do - left as a known gap
    # rather than solved partially and reported as closed.
    if state["stage"] != "baseline":
        raise PplError("ILLEGAL_TRANSITION",
                       "baseline must be recorded while the run is in the baseline stage "
                       "(currently %s) - recording one after the run has moved on is exactly "
                       "the late-repair shape this refusal exists to catch, whatever commit "
                       "it names [R-BASELINE-NOT-AT-HEAD]. Run `ppl stage enter baseline` "
                       "first if this is a genuinely fresh run; anything else is an owner "
                       "decision, not something to force." % state["stage"])
    # base_sha is what the done-gate diffs against. Re-recording it after the
    # work commit would make changed_paths empty and pass the gate's boundary
    # rule vacuously, so it is written once and only to an ancestor of HEAD.
    if state.get("base_sha"):
        raise PplError("ILLEGAL_TRANSITION",
                       "baseline for %s is already recorded at %s. It is written once, "
                       "because the done-gate diffs against it." % (task_id, state["base_sha"]))
    base_sha = doc.get("base_sha")
    if not base_sha or not isinstance(base_sha, str):
        raise PplError("SCHEMA_INVALID", "baseline must carry a base_sha string")
    # Every git fact is resolved before anything is written: a bad repo path or
    # a base_sha git rejects must not leave baseline.json on disk with the state
    # left unsaved.
    head = gitfacts.head_sha(state["repo"])
    # A commit is its own ancestor, so base_sha == head is accepted here on
    # purpose now - see the stage check above for why that is safe.
    if not gitfacts.is_ancestor(state["repo"], base_sha, head):
        raise PplError("SCHEMA_INVALID",
                       "base_sha %s is not an ancestor of HEAD %s. A baseline ahead of "
                       "the work it is supposed to precede would empty the done-gate's diff."
                       % (base_sha, head))
    branch = gitfacts.branch(state["repo"])
    store.write_json(paths.run_dir(task_id) / "baseline.json", doc)
    state["base_sha"] = base_sha
    state["branch"] = branch
    # E2, cycle 2 wave E: next_safe_action is restamped by stage_enter,
    # transition, park, escalate and recover (D6, cycle 2 wave D), but not by
    # this or the other three one-shot artifact commands - each left it
    # naming the action that had just succeeded, which then errors
    # ILLEGAL_TRANSITION if repeated. See run.STAGE_NEXT_ACTION for the
    # analogous per-stage table this mirrors.
    state["next_safe_action"] = "ppl stage enter briefing"
    run.save(state)
    print("baseline recorded at %s on %s" % (base_sha, branch))
    return 0


def cmd_evidence(args):
    task_id = resolve_task(args)
    require_current_generation(task_id, args.generation)
    require_retry_left(task_id, args)
    producer = parse_json_arg(args.producer, "--producer") if args.producer else {
        "provider": "claude-code-subagent", "model": "unknown", "params_hash": ""}
    # The receipt's binding SHA comes from git rather than from the caller by
    # default. The rest of a receipt is model-authored (DESIGN 15), so the one
    # field that decides which candidate it counts for is read independently.
    sha = args.sha or gitfacts.head_sha(run.load(task_id)["repo"])
    receipt = evidence.add(task_id, args.check, args.result, sha,
                           args.log, args.rc, producer)
    print("receipt %s=%s on %s" % (receipt["check"], receipt["result"], receipt["candidate_sha"]))
    return 0


def cmd_verdict(args):
    task_id = resolve_task(args)
    require_current_generation(task_id, args.generation)
    require_retry_left(task_id, args)
    state = run.load(task_id)
    head = gitfacts.head_sha(state["repo"])
    # verdict.record itself sets state["candidate_sha"] once the verdict is
    # durably written, so it happens even when the second-consecutive-
    # incomplete park below raises after that write (R-VERIFICATION-UNAVAILABLE-PARK).
    doc = verdict.record(task_id, load_role_output(args.file, "verdict"), head)
    print("verdict %s recorded on %s" % (doc["verdict"], head))
    return 0


def cmd_findings(args):
    task_id = resolve_task(args)
    if args.action == "add":
        require_current_generation(task_id, args.generation)
        require_retry_left(task_id, args)
        record = findings.add(task_id, args.severity, args.evidence, args.affected_sha,
                              args.affected_path, owner=args.owner, fix_link=args.fix_link)
        print("finding %s recorded: track=%s severity=%s status=%s"
             % (record["finding_id"], record["track"], record["severity"], record["status"]))
    elif args.action == "status":
        require_current_generation(task_id, args.generation)
        if not args.finding_id or not args.status:
            raise PplError("SCHEMA_INVALID", "findings status requires --finding-id and --status")
        record = findings.set_status(task_id, args.finding_id, args.status,
                                     fix_link=args.fix_link, by=args.by)
        print("finding %s status: %s" % (record["finding_id"], record["status"]))
    elif args.action == "delete":
        require_current_generation(task_id, args.generation)
        if not args.finding_id:
            raise PplError("SCHEMA_INVALID", "findings delete requires --finding-id")
        result = findings.delete(task_id, args.finding_id, args.reason, by=args.by)
        print("finding %s deleted: %s" % (result["finding_id"], result["reason"]))
    else:
        print(json.dumps(findings.list_all(task_id), sort_keys=True))
    return 0


def _closure_hint(stage):
    """The next step toward closure that is actually legal from `stage`."""
    if stage == "review":
        return "run `ppl stage enter closure` first"
    if stage == "merge_watch":
        return ("the run already left closure and merge_watch has no way back, "
                "so escalate to the owner with `ppl escalate`")
    return "closure follows review in the stage graph"


def cmd_transition(args):
    task_id = resolve_task(args)
    control.enforce(task_id)
    # before the cap check, so the orchestrator's turns up to this call count
    refresh_orchestrator_usage(task_id)
    check_dispatch_budget(task_id)
    check_usd_budget(task_id)
    state = run.load(task_id)
    # Checked BEFORE the done gate below (N4, cycle 2 wave B): a lifecycle
    # transition that is illegal on its face - still "planned", say - used
    # to be reported as whatever confusing error done_blockers happened to
    # raise first (EXTERNAL_BLOCKED for a missing base_sha, concretely, on a
    # run that never recorded one), rather than the much more direct
    # ILLEGAL_TRANSITION. fsm.check_lifecycle is a pure, cheap, always-
    # available check - it costs nothing to run it first, and doing so makes
    # a genuinely illegal transition (including W3's parked-forever
    # deadlock, before its own fix) easy to diagnose instead of masked by
    # whatever the done gate tripped over on the way.
    fsm.check_lifecycle(state["status"], args.target)
    if args.target == "done":
        if state["stage"] != "closure":
            raise PplError("ILLEGAL_TRANSITION",
                           "ppl transition done runs only in stage closure, current stage is %s - %s"
                           % (state["stage"], _closure_hint(state["stage"])))
        blockers = verdict.current_blockers(task_id)
        if blockers:
            raise PplError("IMPLEMENTATION_FAILURE",
                           "cannot close task: " + "; ".join(blockers))
    state["status"] = args.target
    if args.target == "done":
        # D6, cycle 2 wave D: `next_safe_action` otherwise still reads
        # whatever the current stage last stamped ("transition the run to
        # done", from "closure") for as long as it takes the orchestrator to
        # also call `ppl stage enter merge_watch` - stale, on the one
        # boundary where a caller reading it is most likely to be checking
        # whether the run is actually over [R-NEXT-SAFE-ACTION-CURRENT].
        state["next_safe_action"] = "none - task is done"
    run.save(state)
    store.append_log(task_id, {"event": "lifecycle", "to": args.target})
    print("status: %s" % args.target)
    return 0


def cmd_park(args):
    task_id = resolve_task(args)
    refresh_orchestrator_usage(task_id)
    run.park(task_id, args.blocker, args.safe_next, args.unsafe_to_repeat or [])
    print(status.card(task_id))
    return 0


def cmd_escalate(args):
    task_id = resolve_task(args)
    run.park(task_id, args.code, "await owner answer: %s" % args.question, [])
    print("escalated: %s" % args.question)
    return 0


def cmd_budget(args):
    task_id = resolve_task(args)
    control.enforce(task_id)
    state = budget.spend(task_id, args.kind, args.amount)
    print("%s used: %s" % (args.kind, state["budgets"][budget.KINDS[args.kind][0]]))
    return 0


def cmd_approval(args):
    task_id = resolve_task(args)
    state = run.load(task_id)
    head = gitfacts.head_sha(state["repo"])
    approved = (state.get("mr") or {}).get("approved_sha")
    if approved is None:
        outcome = "none"
    elif approved == head:
        outcome = "current"
    else:
        outcome = "stale"
    print("approval is %s: approved %s, head %s" % (outcome, approved, head))
    # stale approval is not an error: it stays exit 0, but the orchestrator's
    # contract says it never branches on prose, so the state is also given as
    # a single parseable JSON line (the last line of stdout), not just the
    # human-readable summary above.
    print(json.dumps({"approval": outcome, "approved_sha": approved, "head_sha": head},
                     sort_keys=True))
    return 0


def cmd_mr(args):
    task_id = resolve_task(args)
    if args.action == "set":
        control.enforce(task_id)
        if args.iid is None or not args.url:
            raise PplError("SCHEMA_INVALID", "mr set requires --iid and --url")
        review.set_mr(task_id, args.iid, args.url, author=args.author)
        print("mr %s recorded" % args.iid)
    else:
        state = run.load(task_id)
        head = gitfacts.head_sha(state["repo"])
        print(review.approval_state(task_id, head))
    return 0


def cmd_merge(args):
    task_id = resolve_task(args)
    control.enforce(task_id)
    review.record_merge(task_id, args.sha, args.url)
    print("merge observed: %s" % args.sha)
    return 0


def _review_repo(task_id):
    """The GitHub repository `ppl review fetch` reaches: the forge_repo of the guard facts of
    the run's lane, which a github-issues repo the run's snapshot names must agree with. No git
    runs and the snapshot alone is never taken (os_sandbox.pinned_repo): the run's repo and
    snapshot are role-writable, and this command runs outside the OS sandbox."""
    snapshot = paths.run_dir(task_id) / "harness.json"
    try:
        doc = store.read_json(snapshot) if stat.S_ISREG(os.lstat(snapshot).st_mode) else None
    except (OSError, PplError):
        doc = None
    forge = doc.get("forge") if isinstance(doc, dict) else None
    if not (isinstance(forge, dict) and forge.get("kind") == "github"):
        raise PplError("SCHEMA_INVALID",
                       "ppl review fetch reads GitHub pull requests, and the harness snapshot "
                       "of %s does not name the github forge" % task_id)
    tracker = doc.get("tracker")
    repo = tracker.get("repo") if isinstance(tracker, dict) and \
        tracker.get("kind") == "github-issues" else None
    facts = os_sandbox.load_facts(task_id)
    if facts is not None:
        forge_repo = facts.get("forge_repo")
        if not (isinstance(forge_repo, str) and github_fetch.is_repo(forge_repo)):
            raise PplError("SCHEMA_INVALID",
                           "%s names no GitHub forge repository (forge_repo is %r), so ppl "
                           "review fetch reaches none: %s"
                           % (os_sandbox.FACTS_FILE, forge_repo,
                              os_sandbox.rendered_by_lane(task_id)))
        repo = repo or forge_repo
    return os_sandbox.pinned_repo("forge_repo", repo, task_id)


def cmd_review_fetch(args):
    # Like cmd_tracker it runs outside the OS sandbox with gh's credentials: the repository
    # comes from the guard facts and the run directory is checked, before the network calls
    # and again right before the write, as a role may swap a directory for a link meanwhile.
    task_id = resolve_task(args)
    target = paths.run_dir(task_id) / "review-threads.json"
    store.checked_run_path(paths.run_dir(task_id))
    store.checked_run_path(target.parent)
    control.enforce(task_id)
    if args.pr is None or args.pr < 1:
        raise PplError("SCHEMA_INVALID", "review fetch requires --pr <number>")
    repo = _review_repo(task_id)
    # gh from the same lane's facts as the repository, whatever lane the cwd is in
    doc = github_fetch.fetch(repo, args.pr, gh=os_sandbox.gh_path(task_id))
    store.checked_run_path(paths.run_dir(task_id))
    store.checked_run_path(target.parent)
    store.checked_run_path(target)
    store.write_json(target, doc)
    print("threads: %s" % target)
    print("%d threads" % len(doc["threads"]))
    return 0


def cmd_review(args):
    if args.action == "fetch":
        return cmd_review_fetch(args)
    if not args.sha:
        raise PplError("SCHEMA_INVALID", "review %s requires --sha" % args.action)
    task_id = resolve_task(args)
    if args.action == "answered":
        # refresh=False: require_current_generation below is a second gate
        # that can still refuse this exact write, and its own refresh (after
        # check_generation passes) is the one that must count - see
        # control.enforce's comment on `refresh` [R-WATCHDOG-DEADLINE].
        control.enforce(task_id, refresh=False)
        require_current_generation(task_id, args.generation)
        if not args.thread:
            raise PplError("SCHEMA_INVALID", "review answered requires --thread")
        review.record_reply(task_id, args.thread, args.sha, args.classification,
                            args.last_note_id)
        print("thread %s recorded as %s" % (args.thread, args.classification))
    elif args.action == "pending":
        if not args.threads:
            raise PplError("SCHEMA_INVALID", "review pending requires --threads")
        threads = load_json_arg(args.threads, "threads", kind=(list, dict))
        if isinstance(threads, dict):
            threads = threads.get("threads")
            if not isinstance(threads, list):
                raise PplError("SCHEMA_INVALID", "threads file %s has no threads list"
                               % args.threads)
        # Three states, not two: omitted (args.author is None) falls through
        # to review.pending's own stored-identity/nothing-configured
        # resolution. An explicit empty string is a deliberate "no filter,
        # and do not warn about it either" request, distinct from omitting
        # the flag. Anything else is an explicit identity override.
        if args.author is None:
            result = review.pending(task_id, threads, args.sha)
        elif args.author == "":
            result = review.pending(task_id, threads, args.sha, author=None)
        else:
            result = review.pending(task_id, threads, args.sha, author=args.author)
        print("%d of %d threads pending" % (len(result), len(threads)))
        # Last stdout line only, same convention as `ppl approval check`: the
        # orchestrator is an LLM following references/review-loop.md, not
        # Python - it branches on this JSON array, never on the prose above.
        print(json.dumps(result, sort_keys=True))
    else:
        control.enforce(task_id)
        review.set_approval(task_id, args.sha)
        print("approval recorded on %s" % args.sha)
    return 0


def cmd_recover(args):
    task_id = resolve_task(args)
    result = recover.reconcile(task_id)
    for line in result["actions"]:
        print("- %s" % line)
    # control.enforce is the hard gate: reconcile only reports facts and never
    # decides the lifecycle on its own, so a broken control file or an
    # operator halt must still turn into a routable exit code 2 here, not an
    # advisory line the caller can read past.
    control.enforce(task_id)
    if result["blocked"]:
        # E4, cycle 2 wave E: a repo path that does not exist at all is a run
        # parked (or otherwise left) before Stage 1 ever created its
        # worktree - SCHEMA_INVALID, whose documented remedy ("fix the
        # artifact or the invocation and retry once") is "create the
        # worktree now, see references/stage-1-baseline.md", the same fix
        # Stage 1 already names. EXTERNAL_BLOCKED, whose remedy is "park,
        # retry on the next tick", is kept for the other shape: a path that
        # DOES exist but is not a usable git repository (corrupted, or
        # something else parked on top of it) - that one really can
        # plausibly clear on its own.
        repo = result["facts"].get("repo")
        if not repo or not os.path.isdir(repo):
            raise PplError("SCHEMA_INVALID",
                           "recovery cannot verify the run: repo %s does not exist. The run "
                           "exists - do not run `ppl init` again. If its worktree has not "
                           "been created yet, create it now (see Stage 1, the skill's "
                           "references/stage-1-baseline.md) and retry "
                           "`ppl recover`; a worktree that existed and was removed is the "
                           "same fix." % repo)
        raise PplError("EXTERNAL_BLOCKED",
                       "recovery cannot verify the run: repo %s exists but is not a usable "
                       "git worktree, see the actions above" % repo)
    return 0


def cmd_watchdog(args):
    task_id = resolve_task(args)
    result = watchdog.check(task_id)
    if result["timed_out"]:
        print("timed out: run parked with TIMED_OUT, lease generation is now %d"
             % result["lease_generation"])
        raise PplError("TIMED_OUT",
                       "run %s has had no ppl activity since its deadline and has been "
                       "parked (lease generation is now %d). Run `ppl recover` before doing "
                       "anything else." % (task_id, result["lease_generation"]))
    print("not timed out")
    return 0


def cmd_serve(args):
    from .panel import server
    server.serve(args.port, open_browser=args.open)
    return 0


def cmd_demo(args):
    from . import demo
    demo.run(keep=args.keep)
    return 0


def cmd_control(args):
    task_id = resolve_task(args)
    if args.action == "set":
        record = control.set_command(task_id, args.control_command, args.scope, by="cli")
        print(json.dumps(record, sort_keys=True))
    else:
        print(json.dumps(control.read(task_id), sort_keys=True))
    return 0


def cmd_routing(args):
    if args.preset and args.action != "preset":
        raise PplError("SCHEMA_INVALID",
                       "ppl routing %s takes --role <name>, not a positional name" % args.action)
    if args.action == "preset":
        if not args.preset:
            raise PplError("SCHEMA_INVALID", "name a preset: ppl routing preset economy|balanced|max")
        routing.apply_preset(args.preset)
        print(json.dumps({"preset": args.preset, "roles": routing.summary()}, sort_keys=True))
        return 0
    if args.role:
        risk = args.risk
        if risk is None and args.task:
            risk = run.load(args.task).get("risk")
        entry = routing.resolve(args.role, risk)
        print(json.dumps({"role": args.role, "risk": risk, "model": entry["model"],
                          "params": entry.get("params", {})}, sort_keys=True))
    else:
        print(json.dumps(routing.load(), indent=2, sort_keys=True))
    return 0


def cmd_models(args):
    if args.action == "verify":
        report = models.verify(models_to_check=args.model or None, force=args.force,
                               dry_run=args.dry_run)
        for line in report["commands"]:
            print(line)
        print(json.dumps({k: v for k, v in report.items() if k != "commands"}, sort_keys=True))
        if report["failed"]:
            raise PplError("MODELS_UNVERIFIED", "probe failed for %s: %s" % (
                ", ".join(report["failed"]),
                "; ".join("%s: %s" % item for item in (report.get("details") or {}).items())))
        return 0
    rows = models.status(args.risk)
    for row in rows:
        print("%-8s %-14s %-32s %s" % (row["tier"] or "-", row["model"],
                                      row["resolved_id"] or "-", row["state"]))
    print(json.dumps(rows, sort_keys=True))
    return 0


def cmd_harness(args):
    if args.action == "list":
        for name in harness.list_names():
            print(name)
        return 0
    if args.action == "add":
        if not args.source:
            raise PplError("SCHEMA_INVALID", "harness add needs --source <directory or git url>")
        target = harness.add(args.source, name=args.name)
        print("added %s" % target)
        return 0
    if not args.name:
        raise PplError("SCHEMA_INVALID",
                       "harness %s needs a harness name or a directory path" % args.action)
    if args.action == "apply":
        result = harness.apply(args.name)
        print("default harness: %s at %s" % (result["active"], result["commit"]))
        for warning in result["warnings"]:
            print("warning: %s" % warning)
        unverified = [row for row in models.status() if row["state"] != "verified"]
        if unverified:
            print("warning: models not verified: %s (run ppl models verify)"
                  % ", ".join("%s (%s)" % (row["model"], row["state"]) for row in unverified))
        return 0
    if args.action == "validate":
        # the studio's check, so a harness passes or fails the same way in both
        result = harness.verify(args.name, args.name)
        if args.json:
            print(json.dumps(result, sort_keys=True))
        else:
            for warning in result["warnings"]:
                print("warning: %s" % warning)
        if result["errors"]:
            raise PplError("HARNESS_INVALID", result["errors"][0])
        if not args.json:
            print("harness %s is valid" % harness.load(args.name)["harness"]["name"])
        return 0
    print(json.dumps(harness.load(args.name), indent=2, sort_keys=True, default=str))
    return 0


def _key_values(items, flag):
    pairs = []
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise PplError("SCHEMA_INVALID", "%s takes key=value, got %r" % (flag, item))
        pairs.append((key.strip(), value))
    return pairs


def _terminal_only(args):
    """The setup options the browser wizard has no use for, by the name the user typed."""
    given = []
    if args.step:
        given.append("step names")
    for flag, value in (("--answer", args.answer), ("--non-interactive", args.non_interactive),
                        ("--tier", args.tier), ("--skip", args.skip),
                        ("--no-verify", args.no_verify)):
        if value:
            given.append(flag)
    return given


def _offer_browser(args):
    """An interactive setup in a terminal offers the browser wizard while no step is
    done, so the offer repeats until one is. Any option the browser wizard would
    drop, or a step done already, keeps the terminal wizard as it was."""
    if _terminal_only(args) or not sys.stdin.isatty():
        return False
    if config.steps_state()["done"]:
        return False
    try:
        reply = input("Open the setup wizard in your browser? [Y/n] ").strip().lower()
    except EOFError:
        return False
    return reply in ("", "y", "yes")


def cmd_setup(args):
    if args.web:
        ignored = _terminal_only(args)
        if ignored:
            raise PplError("SCHEMA_INVALID", "--web runs the wizard in the browser: it takes only "
                           "--port, not %s" % ", ".join(ignored))
    elif args.port is not None:
        raise PplError("SCHEMA_INVALID", "--port is for --web")
    if args.web or _offer_browser(args):
        from .panel import server
        server.serve(8787 if args.port is None else args.port, open_browser=True,
                     fragment="#/setup")
        return 0
    answers = {}
    for key, value in _key_values(args.answer, "--answer"):
        if key not in setup.ANSWER_KEYS and not key.startswith(setup.TIER_PREFIX):
            raise PplError("SCHEMA_INVALID", "unknown answer key %r (keys: %s, %s<name>)"
                           % (key, ", ".join(setup.ANSWER_KEYS), setup.TIER_PREFIX))
        answers[key] = value
    for key, value in _key_values(args.tier, "--tier"):
        answers[setup.TIER_PREFIX + key] = value
    if args.no_verify:
        answers["verify"] = "no"
    result = setup.run(steps=args.step or None, answers=answers,
                       non_interactive=args.non_interactive, skip=args.skip or ())
    print(json.dumps(result, sort_keys=True))
    if result["failed"]:
        reasons = "; ".join("%s: %s" % (step, result["reasons"].get(step) or "failed")
                            for step in result["failed"])
        raise PplError("SETUP_INCOMPLETE", "steps did not complete: %s. Run ppl setup %s again "
                       "once that is fixed" % (reasons, " ".join(result["failed"])))
    return 0


def cmd_dispatch(args):
    payload = load_json_arg(args.input, "dispatch input")
    result = routing.dispatch(args.role, payload)
    store.write_json(args.output, result)
    print("role %s finished with status %s" % (args.role, result["status"]))
    return 0


def cmd_destroy(args):
    if args.why is not None and not args.reflect:
        raise PplError("SCHEMA_INVALID", "--why goes with --reflect")
    if args.reflect and args.scope != "local":
        raise PplError("SCHEMA_INVALID", "reflect at the local destroy that archives the run")
    if args.reflect and (args.why is None or not args.why.strip()):
        raise PplError("SCHEMA_INVALID", "--reflect needs --why \"<one line on why the run "
                                         "ended>\"")
    task_id = resolve_task(args)
    if args.reflect:
        result = lessons.destroy_with_reflection(task_id, args.why)
    else:
        result = run.destroy(task_id, args.scope)
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_metrics(args):
    report = metrics.compute()
    if args.text:
        print(metrics.text(report))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_trail(args):
    # A reference run is normally archived by the time it is published, so the
    # task is named explicitly and nothing here touches the run.
    redact = tuple(part.strip() for part in args.redact.split(",") if part.strip())
    manifest = trail.export(args.task, args.out, redact=redact)
    print("trail written to %s: %d files" % (args.out, len(manifest["files"]) + 1))
    return 0


def cmd_label(args):
    # Deliberately no control.enforce and no require_current_generation - see
    # ppl/metrics.py:record_label for both rulings (R-LABEL-NO-LIVENESS-REFRESH,
    # R-LABEL-NO-LEASE-FENCE). record_label's own existence check accepts a
    # live OR an archived task (R-METRIC-SCANS-ARCHIVE), since ground truth
    # normally arrives after ppl destroy has already archived the run -
    # --task must be given explicitly then, since no lane holds that run any
    # more for resolve_task to find.
    task_id = resolve_task(args)
    metrics.record_label(task_id, args.sha, args.actual, by=args.by)
    print("labeled %s on %s as %s" % (task_id, args.sha, args.actual))
    return 0


def _lessons_record(args):
    # --task is required: a pending run reflected on is archived, so no lane holds it, and a
    # lane session's PPL_TASK names its own run, so --task is not checked against it. In a lane
    # the task is the lane's own run or a marker it claimed (R57). Not a role-output command of
    # refusals.COMMANDS and no control.enforce: it reads the bundle and writes only under
    # PPL_HOME/lessons.
    if not args.task or not args.file:
        raise PplError("SCHEMA_INVALID", "ppl lessons record needs --task <id> and --file <json>")
    lane = lanes.task_for_call(None)
    try:
        doc = store.read_json(args.file, None, lessons.BUNDLE_READ_LIMIT)
    except (OSError, UnicodeDecodeError, RecursionError) as exc:
        raise PplError("SCHEMA_INVALID", "cannot read %s: %s" % (args.file, exc))
    if doc is None:
        raise PplError("SCHEMA_INVALID", "%s is not there" % args.file)
    result = lessons.record(args.task, doc, by_task=lane)
    if result["no_entry"]:
        print("recorded no entry for %s" % args.task)
    else:
        print("recorded %d proposals for %s" % (len(result["proposals"]), args.task))
    return 0


OWNER_ACTIONS = ("list", "accept", "reject", "retire", "edit")


def _lessons_table(data):
    lines = ["harness %s" % data["harness"], "", "pending proposals:"]
    for item in data["proposals"]:
        lines.append("  %s  %s  when %s: %s" % (item["id"], item["role"], item["cue"],
                                                  item["lesson"]))
    if not data["proposals"]:
        lines.append("  none")
    lines += ["", "active lessons:"]
    active = [e for e in data["memory"] if e["status"] == "active"]
    for entry in active:
        lines.append("  %s  %s  seen %d  when %s: %s" % (entry["id"], entry["role"],
                                                           entry["seen"], entry["cue"],
                                                           entry["lesson"]))
    if not active:
        lines.append("  none")
    return "\n".join(lines)


def _lessons_review(args):
    # The owner's review: the only writer of memory.toml, through the studio's versioned save.
    # Not a role-output command, and a role's Bash call of it is denied by the guard
    # [R-LESSONS-OWNER-ACCEPTS]. No control.enforce: it touches no run.
    name = lessons.harness_name(args.harness)
    if args.action == "list":
        data = lessons.listing(name)
        for item in data["skipped"]:
            print("warning: the proposals file %s was skipped: %s" % (item["path"],
                                                                     item["reason"]),
                  file=sys.stderr)
        print(json.dumps(data, indent=2, sort_keys=True) if args.json else _lessons_table(data))
        return 0
    if not args.target:
        raise PplError("SCHEMA_INVALID", "ppl lessons %s needs a %s" % (
            args.action, "proposal id" if args.action in ("accept", "reject") else "lesson id"))
    if args.action == "accept":
        entry = lessons.accept(name, args.target, args.cue, args.lesson)
        print("accepted %s as %s (seen %d)" % (args.target, entry["id"], entry["seen"]))
    elif args.action == "reject":
        lessons.reject(name, args.target, args.reason)
        print("rejected %s" % args.target)
    elif args.action == "retire":
        lessons.retire(name, args.target)
        print("retired %s" % args.target)
    else:
        lessons.edit(name, args.target, args.cue, args.lesson)
        print("edited %s" % args.target)
    print(lessons.lane_hint(name))
    return 0


def _lessons_pending():
    """Every pending marker outside a lane. In a lane, the markers of runs of the lane's harness,
    each claimed for this lane first (D20), and the ones it claimed before and has not recorded,
    so a resumed session finishes them."""
    lane = lanes.task_for_call(None)
    if lane is None:
        markers = lessons.pending()
    else:
        for marker in lessons.pending(lessons.lane_harness(lane)):
            lessons.claim_pending(marker["task"], lane)
        markers = lessons.claimed(lane)
    shown = 0
    for marker in markers:
        if "warning" in marker:
            print("warning: the pending marker for %s %s, skipped" % (marker["task"],
                                                                    marker["warning"]),
                  file=sys.stderr)
        else:
            print("%s %s" % (marker["task"], marker["bundle"]))
            shown += 1
    if not shown:
        print("no pending reflections")
    return 0


def cmd_lessons(args):
    # No control.enforce, by design: the bundle changes no run state, and a destroy with
    # reflection builds it after control.set_command recorded that destroy, so an enforce
    # would refuse its own caller. lessons.bundle holds the stage gate
    # [R-LESSONS-BUNDLE-AFTER-MERGE].
    if args.action == "record":
        return _lessons_record(args)
    if args.action in OWNER_ACTIONS:
        return _lessons_review(args)
    if args.action == "pending":
        return _lessons_pending()
    task_id = resolve_task(args)
    target = lessons.bundle(task_id)
    if target is None:
        print("reflection is off ([lessons] reflect = false)")
    else:
        print("bundle: %s" % target)
    return 0


def _lane_close_task(args):
    """The task `ppl lane close` frees: the one it names as <id> or --task, else as every bare
    command finds it (resolve_task)."""
    if args.rest and args.task and args.rest != args.task:
        raise PplError("SCHEMA_INVALID", "ppl lane close names %s and --task %s: name one task"
                       % (args.rest, args.task))
    return resolve_task(argparse.Namespace(task=args.rest or args.task))


def cmd_lane(args):
    # The first word is read by hand: list, resume and close are verbs, anything else is a
    # task link or id (task ids cannot be these words).
    if args.target == "list":
        rows = lane_launch.listing()
        print(json.dumps(rows, indent=2, sort_keys=True) if args.json else lane_launch.text(rows))
        return 0
    if args.target == "resume":
        if not args.rest:
            raise PplError("SCHEMA_INVALID", "ppl lane resume needs the task: ppl lane resume <id>")
        result = lane_launch.resume(args.rest, exec_=not args.no_exec)
    elif args.target == "close":
        result = lane_launch.close(_lane_close_task(args))
    else:
        if args.rest:
            raise PplError("SCHEMA_INVALID", "ppl lane takes one task link or id, and %r is a "
                           "second" % args.rest)
        result = lane_launch.start(args.target, harness_name=args.harness, slug=args.slug,
                                   exec_=not args.no_exec)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def cmd_apply_patch(args):
    task_id = resolve_task(args)
    require_current_generation(task_id, args.generation)
    patterns = boundary.run_test_patterns(task_id)
    if patterns is None:
        raise PplError("OWNER_DECISION",
                       "%s - restore the harness snapshot or re-init the run"
                       % boundary.unknown_patterns_reason(task_id))
    state = run.load(task_id)
    bound = store.read_json(paths.run_dir(task_id) / "boundary.json", {"write": [], "forbid": []})
    with open(args.diff, "r", encoding="utf-8") as handle:
        diff_text = handle.read()
    boundary.check_diff(args.role, diff_text, bound, patterns)
    gitfacts.apply_patch(state["repo"], args.diff)
    print("patch applied within boundary")
    return 0


COMMANDS = {
    "init": cmd_init, "show": cmd_show, "status": cmd_status, "stage": cmd_stage,
    "brief": cmd_brief, "boundary": cmd_boundary, "baseline": cmd_baseline,
    "evidence": cmd_evidence, "verdict": cmd_verdict, "findings": cmd_findings,
    "transition": cmd_transition,
    "park": cmd_park, "escalate": cmd_escalate, "budget": cmd_budget,
    "approval": cmd_approval, "recover": cmd_recover, "serve": cmd_serve, "demo": cmd_demo,
    "control": cmd_control, "routing": cmd_routing, "models": cmd_models,
    "dispatch": cmd_dispatch, "harness": cmd_harness,
    "apply-patch": cmd_apply_patch, "destroy": cmd_destroy,
    "mr": cmd_mr, "merge": cmd_merge, "review": cmd_review, "doctor": cmd_doctor,
    "watchdog": cmd_watchdog, "metrics": cmd_metrics, "label": cmd_label,
    "setup": cmd_setup, "tracker": cmd_tracker, "trail": cmd_trail, "lessons": cmd_lessons,
    "lane": cmd_lane,
}


class JsonArgumentParser(argparse.ArgumentParser):
    """argparse's own usage errors (bad subcommand, invalid choice, missing
    required argument) exit 2 like a PplError does, but by default they print
    plain usage text, not JSON. An orchestrator that follows the documented
    contract (exit 2 -> json.loads(stderr)["error_code"]) would crash on its
    own error handling for exactly the inputs most likely to be malformed.
    """

    def error(self, message):
        sys.stderr.write(json.dumps({"error_code": "SCHEMA_INVALID", "message": message},
                                    sort_keys=True) + "\n")
        raise SystemExit(2)


def add_from(parser, default=None):
    # The role whose output the call records, so a refusal is counted against it
    # [R-REFUSAL-RETRY-CAP]. Checked in require_retry_left, not by argparse choices, so
    # a missing or unknown one is a logged refusal rather than an argparse error.
    parser.add_argument("--from", dest="from_role", default=default)


def build_parser():
    parser = JsonArgumentParser(prog="ppl")
    parser.add_argument("--version", action="version", version="antline %s" % __version__)
    # --task belongs to each subparser only. A parent-level copy would be silently
    # overwritten by the subparser default, so `ppl --task X status` would lose X.
    # parser_class is passed explicitly so every subparser (a bad `stage`, a bad
    # `control` command, ...) raises through the same JSON error path, not just
    # the top-level command choice.
    sub = parser.add_subparsers(dest="command", required=True, parser_class=JsonArgumentParser)

    p = sub.add_parser("init")
    p.add_argument("task_id")
    p.add_argument("--tracker-url", required=True)
    # the lane's worktree when left out, and any other directory is refused
    p.add_argument("--repo")
    # A harness that reaches GitHub needs the OS sandbox that denies it. --no-os-sandbox is the
    # operator's per-run opt-out, and the run records it.
    p.add_argument("--os-sandbox", action=argparse.BooleanOptionalAction, default=True)
    # The risk tier routes the code worker to a stronger model on high-risk
    # tasks (routing.toml by_risk) - see routing.resolve.
    p.add_argument("--risk", choices=list(run.RISK_TIERS), default="normal")
    p.add_argument("--skip-model-check", action="store_true")
    p.add_argument("--harness")

    p = sub.add_parser("show")
    p.add_argument("--task")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status")
    p.add_argument("--task")

    p = sub.add_parser("stage")
    p.add_argument("action", choices=["enter"])
    p.add_argument("stage", choices=fsm.STAGES)
    p.add_argument("--task")

    p = sub.add_parser("brief")
    p.add_argument("action", choices=["set", "challenge"])
    p.add_argument("file")
    p.add_argument("--task")
    # challenge is a lease-fenced write like verdict record, set is not
    p.add_argument("--generation", type=int)
    add_from(p)

    p = sub.add_parser("tracker")
    p.add_argument("action", choices=["fetch"])
    p.add_argument("--task")

    p = sub.add_parser("boundary")
    p.add_argument("action", choices=["set"])
    p.add_argument("file")
    p.add_argument("--task")

    p = sub.add_parser("baseline")
    p.add_argument("action", choices=["record"])
    p.add_argument("file")
    p.add_argument("--task")

    p = sub.add_parser("evidence")
    p.add_argument("action", choices=["add"])
    p.add_argument("--check", required=True)
    p.add_argument("--result", required=True)
    # --sha defaults to git rev-parse HEAD in the run's repo, see cmd_evidence
    p.add_argument("--sha")
    p.add_argument("--log", required=True)
    p.add_argument("--rc", required=True)
    p.add_argument("--producer")
    # Required in effect (see require_current_generation), not via argparse
    # `required=True`. cmd_evidence has no control.enforce call to preserve
    # ordering against - the reason not to use `required=True` here is
    # simpler: a stale attempt should be refused for holding a stale
    # generation, not for a shape argparse can already report on its own, and
    # the two error codes (SCHEMA_INVALID for missing, ILLEGAL_TRANSITION for
    # stale) stay distinguishable that way. type=int so the comparison is
    # never a string/int mismatch. (Contrast `review answered` below, where
    # control.enforce genuinely does run first and argparse-required would
    # misreport a paused run.)
    p.add_argument("--generation", type=int)
    # the orchestrator runs the gates itself
    add_from(p, default="orchestrator")
    p.add_argument("--task")

    p = sub.add_parser("verdict")
    p.add_argument("action", choices=["record"])
    p.add_argument("file")
    # Same reasoning as evidence's --generation above: cmd_verdict has no
    # control.enforce call either, so this is not about preserving ordering
    # against one.
    p.add_argument("--generation", type=int)
    add_from(p)
    p.add_argument("--task")

    p = sub.add_parser("findings")
    p.add_argument("action", choices=["add", "status", "delete", "list"])
    # add
    # findings.add checks it, so a wrong one counts as the role's output
    p.add_argument("--severity")
    p.add_argument("--evidence")
    p.add_argument("--affected-sha", dest="affected_sha")
    p.add_argument("--affected-path", dest="affected_path")
    p.add_argument("--owner")
    p.add_argument("--fix-link", dest="fix_link")
    add_from(p)
    # status / delete
    p.add_argument("--finding-id", dest="finding_id")
    p.add_argument("--status", choices=list(findings.STATUSES))
    p.add_argument("--reason")
    p.add_argument("--by")
    # Same reasoning as evidence's --generation above: findings add/status/
    # delete have no control.enforce call either, so this is not about
    # preserving ordering against one - see require_current_generation.
    p.add_argument("--generation", type=int)
    p.add_argument("--task")

    p = sub.add_parser("transition")
    p.add_argument("target", choices=["done", "blocked", "failed", "in_progress", "ready"])
    p.add_argument("--task")

    p = sub.add_parser("park")
    p.add_argument("--blocker", required=True)
    p.add_argument("--safe-next", required=True, dest="safe_next")
    p.add_argument("--unsafe-to-repeat", nargs="*", dest="unsafe_to_repeat")
    p.add_argument("--task")

    p = sub.add_parser("escalate")
    p.add_argument("--code", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--task")

    p = sub.add_parser("budget")
    p.add_argument("action", choices=["spend"])
    p.add_argument("kind", choices=["dispatch", "retry", "usd"])
    p.add_argument("amount", type=float, nargs="?", default=1)
    p.add_argument("--task")

    p = sub.add_parser("approval")
    p.add_argument("action", choices=["check"])
    p.add_argument("--task")

    p = sub.add_parser("mr")
    p.add_argument("action", choices=["set", "state"])
    p.add_argument("--iid", type=int)
    p.add_argument("--url")
    p.add_argument("--author")
    p.add_argument("--task")

    p = sub.add_parser("merge")
    p.add_argument("action", choices=["observed"])
    p.add_argument("--sha", required=True)
    p.add_argument("--url")
    p.add_argument("--task")

    p = sub.add_parser("review")
    p.add_argument("action", choices=["answered", "approved", "pending", "fetch"])
    p.add_argument("--thread")
    p.add_argument("--threads")
    p.add_argument("--sha")
    p.add_argument("--pr", type=int)
    p.add_argument("--classification", choices=list(review.CLASSIFICATIONS))
    p.add_argument("--last-note-id", dest="last_note_id")
    p.add_argument("--author")
    # Optional at argparse for the same reason as evidence's --generation,
    # but this is the one write path where the ordering concern is real:
    # cmd_review's "answered" branch calls control.enforce() before
    # require_current_generation, so `required=True` here would fail before
    # that gate ever ran and misreport a paused run as SCHEMA_INVALID instead
    # of OWNER_DECISION.
    p.add_argument("--generation", type=int)
    p.add_argument("--task")

    p = sub.add_parser("recover")
    p.add_argument("--task")

    p = sub.add_parser("watchdog")
    p.add_argument("action", choices=["check"])
    p.add_argument("--task")

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--open", action="store_true", help="open the studio in the default browser")

    p = sub.add_parser("demo", help="play one issue through the pipeline offline, in a sandbox")
    p.add_argument("--keep", action="store_true")

    p = sub.add_parser("control")
    p.add_argument("action", choices=["set", "get"])
    # dest is renamed away from "command": the top-level subparsers dest is
    # also "command" (it picks the handler out of COMMANDS), and a positional
    # here named the same would silently overwrite it after parsing, making
    # `ppl control set pause` dispatch on "pause" instead of "control".
    p.add_argument("control_command", nargs="?", choices=list(control.COMMANDS))
    p.add_argument("--scope", choices=list(control.SCOPES))
    p.add_argument("--task")

    p = sub.add_parser("routing")
    p.add_argument("action", choices=["show", "preset"])
    p.add_argument("preset", nargs="?", help="preset name for `ppl routing preset`: economy, balanced, max")
    p.add_argument("--role")
    p.add_argument("--risk", choices=list(run.RISK_TIERS))
    p.add_argument("--task")

    p = sub.add_parser("models")
    p.add_argument("action", choices=["verify", "status"])
    p.add_argument("--model", action="append")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--risk", choices=list(run.RISK_TIERS))

    p = sub.add_parser("harness")
    p.add_argument("action", choices=["validate", "list", "show", "add", "apply"])
    p.add_argument("name", nargs="?")
    p.add_argument("--source")
    p.add_argument("--json", action="store_true",
                   help="validate: print {errors, warnings} as the studio shows them")

    p = sub.add_parser("setup")
    # No choices= here: argparse before 3.12 checks the empty list against
    # them and refuses a bare `ppl setup`, so setup.run checks the names.
    p.add_argument("step", nargs="*", help="steps to run: %s (default: every step not done)"
                   % ", ".join(setup.STEPS))
    p.add_argument("--non-interactive", action="store_true")
    p.add_argument("--answer", action="append",
                   help="key=value, keys: %s, %s<name>" % (", ".join(setup.ANSWER_KEYS),
                                                           setup.TIER_PREFIX))
    p.add_argument("--tier", action="append", help="tier=model")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--skip", action="append", choices=list(setup.STEPS))
    p.add_argument("--web", action="store_true",
                   help="run the wizard in the browser (ppl serve), it takes only --port")
    p.add_argument("--port", type=int, help="the studio port for --web (default 8787)")

    p = sub.add_parser("dispatch")
    p.add_argument("--role", required=True)
    p.add_argument("--in", dest="input", required=True)
    p.add_argument("--out", dest="output", required=True)

    p = sub.add_parser("destroy")
    p.add_argument("--scope", choices=["local", "full"], required=True)
    p.add_argument("--task")
    p.add_argument("--reflect", action="store_true")
    p.add_argument("--why")

    p = sub.add_parser("apply-patch")
    p.add_argument("--role", required=True)
    p.add_argument("--diff", required=True)
    # Same reasoning as evidence's --generation above: cmd_apply_patch has no
    # control.enforce call either.
    p.add_argument("--generation", type=int)
    p.add_argument("--task")

    p = sub.add_parser("doctor")
    p.add_argument("--task")
    p.add_argument("--forge", action="store_true")

    p = sub.add_parser("metrics")
    p.add_argument("--text", action="store_true")

    p = sub.add_parser("trail")
    p.add_argument("action", choices=["export"])
    p.add_argument("--task", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--redact", default="paths,people",
                   help="comma list of %s, paths always included" % ", ".join(trail.REDACTIONS))

    p = sub.add_parser("label")
    p.add_argument("--sha", required=True)
    p.add_argument("--actual", required=True, choices=list(metrics.LABEL_VALUES))
    p.add_argument("--by")
    p.add_argument("--task")

    p = sub.add_parser("lane", help="start a task window, or list, resume or close one")
    p.add_argument("target", help="a task link or id, or one of %s" % ", ".join(lane_launch.VERBS))
    p.add_argument("rest", nargs="?", help="the task id after resume or close")
    p.add_argument("--harness", help="the installed harness (default: the one whose [repo] path "
                                     "holds the cwd, else the default harness)")
    p.add_argument("--slug", default="work", help="the branch is <TASK-ID>-<slug>")
    p.add_argument("--no-exec", action="store_true",
                   help="print {argv, env, cwd} as JSON instead of starting claude")
    p.add_argument("--task", help="close: the task whose lane to free (default: PPL_TASK, else "
                                  "the lane holding the cwd)")
    p.add_argument("--json", action="store_true", help="list: print JSON")

    p = sub.add_parser("lessons")
    p.add_argument("action", choices=["bundle", "record", "list", "accept", "reject", "retire",
                                      "edit", "pending"])
    p.add_argument("target", nargs="?")
    p.add_argument("--task")
    p.add_argument("--file")
    p.add_argument("--harness")
    p.add_argument("--reason")
    p.add_argument("--cue")
    p.add_argument("--lesson")
    p.add_argument("--json", action="store_true")
    return parser


def main(argv=None):
    parser = build_parser()
    # main()'s documented interface is "-> int": argparse's own error() and
    # its -h/--help both raise SystemExit rather than returning, which would
    # otherwise blow straight through this function's return contract for
    # any caller that invokes main() in-process rather than as a subprocess.
    try:
        args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    if args.command == "budget":
        args.amount = int(args.amount) if args.kind != "usd" else float(args.amount)
    try:
        code = COMMANDS[args.command](args)
    except PplError as err:
        error = err.as_dict()
        if err.code == "SCHEMA_INVALID":
            note = count_refusal(args, err)
            if note:
                error["message"] = "%s - %s" % (err.message, note)
        sys.stderr.write(json.dumps(error, sort_keys=True) + "\n")
        return 2
    if code == 0:
        note_acceptance(args)
    return code


def _role_output_run(args):
    """(task_id, command) when this call records a role's output in a run that exists."""
    command = role_output_command(args)
    try:
        # the run resolve_task finds, without its refusal when there is none
        task_id = lanes.task_for_call(getattr(args, "task", None))
        if command is None or not task_id or not run.state_path(task_id).exists():
            return None, None
    except (OSError, PplError):
        # a task id, a PPL_TASK or a registry the command itself already failed on
        return None, None
    return task_id, command


# The two helpers below never change the command's outcome: exit 2 keeps its JSON as the
# last stderr line and exit 0 stays 0. A count they cannot read or write is reported as a
# warning line and, when the log takes it, a refusal_bookkeeping_error event.
def count_refusal(args, err):
    """Log a refused role output in its run and park the run on the second refusal of
    its content in a row (refusals.record_refusal). Returns the sentence to add to the
    message."""
    task_id, command = _role_output_run(args)
    if task_id is None:
        return None
    try:
        return refusals.record_refusal(task_id, command, args.from_role, err.message,
                                       err.of_role_output)
    except Exception as exc:
        bookkeeping_failed(task_id, command, args.from_role, exc)
        return None


def note_acceptance(args):
    """An accepted call ends the run of refusals for its command and role."""
    task_id, command = _role_output_run(args)
    if task_id is None:
        return
    try:
        refusals.record_acceptance(task_id, command, args.from_role)
    except Exception as exc:
        bookkeeping_failed(task_id, command, args.from_role, exc)


def bookkeeping_failed(task_id, command, role, exc):
    reason = str(exc) or type(exc).__name__
    sys.stderr.write("warning: refusal count not updated: %s\n" % reason)
    try:
        store.append_log(task_id, {"event": "refusal_bookkeeping_error", "command": command,
                                   "role": role, "reason": reason})
    except Exception:
        pass

from . import control, gitfacts, paths, run, store, watchdog


def reconcile(task_id):
    # Checked, and acted on, before anything else: if this run has had no
    # activity past its inactivity deadline, it is parked with TIMED_OUT and
    # the generation is bumped right here - that bump is this call's
    # unconditional bump below, not a second one on top of it (see the branch
    # after `state` is reloaded). `ppl recover` runs at session start and, in
    # the review loop, once per tick, so it is one of the few callers that
    # can ever discover a timeout that fired between commands.
    watchdog_result = watchdog.check(task_id)
    state = run.load(task_id)
    actions = []
    facts = {"repo": state.get("repo")}
    repo = state.get("repo")

    if watchdog_result["timed_out"]:
        actions.append("no activity since the deadline: run parked with blocker TIMED_OUT")
        generation = watchdog_result["lease_generation"]
    else:
        # Bumped first and unconditionally, even on the blocked-repo path below:
        # `ppl review answered` never touches the repo, so a stale attempt could
        # still write through a "blocked" recovery if the fence waited on facts
        # this path cannot gather (R-LEASE-STALE-WRITE).
        generation = run.bump_generation(task_id)
        # This call itself is the activity: a quiet review-loop tick whose
        # only `ppl` call is `ppl recover` still counts as the run being
        # watched, not abandoned, so the inactivity clock restarts here -
        # only on this branch, never on the one above (a refresh placed
        # ahead of `watchdog.check` would mask the very hang it exists to
        # catch). See DESIGN 6.9 and R-WATCHDOG-DEADLINE.
        run.refresh_deadline(task_id)
    facts["lease_generation"] = generation

    if not repo or not gitfacts.is_repo(repo):
        actions.append("repo %s is not a git worktree, cannot verify any fact" % repo)
        actions.append("lease generation is now %d: pass --generation %d on every write "
                       "from here" % (generation, generation))
        store.append_log(task_id, {"event": "recover", "facts": facts, "actions": actions,
                                   "blocked": True})
        return {"facts": facts, "actions": actions, "blocked": True}

    facts["branch"] = gitfacts.branch(repo)
    facts["head_sha"] = gitfacts.head_sha(repo)

    if state.get("candidate_sha") and state["candidate_sha"] != facts["head_sha"]:
        actions.append(
            "head moved: checkpoint says %s, git says %s. Every approval, CI result and "
            "verdict bound to the old SHA is stale."
            % (state["candidate_sha"], facts["head_sha"]))

    if state["stage"] == "parking":
        parking = store.read_json(paths.run_dir(task_id) / "parking.json", {})
        actions.append("run is parked on %s, resume stage is %s, safe next action: %s"
                       % (parking.get("blocker"), parking.get("resume_stage"),
                          parking.get("safe_next_action")))

    command = control.read(task_id)
    if command is not None:
        if command.get("invalid"):
            actions.append(
                "control file is unusable: %s. Do not resume until it is fixed or removed."
                % command["reason"])
        elif command["command"] in control.HALTING:
            actions.append("operator %s is in force, do not resume until cleared"
                           % command["command"])

    if state.get("unsafe_to_repeat"):
        actions.append("do not replay: %s" % ", ".join(state["unsafe_to_repeat"]))

    if not actions:
        actions.append("state matches facts, safe to continue at stage %s" % state["stage"])

    # Reported last, after the fallback above, so a healthy run still reads
    # "safe to continue" and also learns the value it must pass as
    # --generation on every write from here.
    actions.append("lease generation is now %d: pass --generation %d on every write "
                   "from here" % (generation, generation))

    store.append_log(task_id, {"event": "recover", "facts": facts, "actions": actions})
    return {"facts": facts, "actions": actions, "blocked": False}

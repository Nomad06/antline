# Reading `ppl doctor`

The guard hook is a `PreToolUse` hook registration in Claude Code settings: `ppl lane`
registers it in the settings of the session it starts for a run, and a session resumed by
hand without the lane flags has none [R-LANE-BARE-RESUME n/a].
`ppl doctor` prints a JSON line whose `registration` field is one of:

- **`none`** - the hook is not registered anywhere. Every sentence in this skill about a hook
  stopping something is false for this session, right now: the write boundary, the Bash
  landmines, the tracker gate, the operator pause - none of it fires. `git add -A` in the
  worktree returns exit 0 with no warning at all, because there is no hook there to see it.
  You are unprotected, and the only checks that actually run are the ones `ppl` itself
  performs (the state machine and the done gate) - a role staying inside its lane because
  you dispatched it correctly, not because anything physically stopped it if you did not.
  Tell the operator, and proceed on that basis, or pause and ask them to start this session
  again with `ppl lane resume <TASK-ID>` in a terminal outside the lane, which loads the
  lane's settings. The dispatch counter is a hook too (`SubagentStart`): with
  `none`, run `ppl budget spend dispatch` yourself before every `ppl-*` dispatch, or the
  hard cap at `ppl stage enter` compares against zero for the whole run.
- **`local`** - registered in the lane's own `settings.json`, which `ppl lane` rendered for
  this run with `--task <TASK-ID>` on every hook's command line, so each hook binds this run
  whatever the cwd of the call [R-LANE-HOOK-BINDING]. This is the expected state; the
  present-tense claims in this skill hold for this run.
- **`start_local`** or **`both`** - a guard entry also sits in `.claude/settings.local.json`
  of the lane's start directory, a file `ppl lane` does not write and the session loads as
  its local settings. With `both` the lane's own entry still binds this run
  [R-LANE-HOOK-BINDING]; tell the operator the extra entry is there. With `start_local` alone
  the lane's own entry is gone: read it as `none`, and tell the operator to start the lane
  again in a terminal outside it.
- **`unknown`** - a settings file exists but could not be read or parsed. Read this the same
  way as `none`: this check has no confirmed answer from a file it is unable to read, so
  nothing in this skill should be taken as armed until that is fixed.

The same JSON line carries a `matcher` field - `ok` when every registered guard entry's matcher
passes all the tool names the hook acts on, `incomplete` when some are left out, with the
`uncovered` list under `local`/`start_local` naming them. An uncovered tool is a denial that can never
fire, so read `incomplete` as `none` for exactly those tools, tell the operator, and expect
`ppl init` to refuse a NEW run until the matcher is widened to the one in README.md "Hooks"
[R-GUARD-MATCHER].

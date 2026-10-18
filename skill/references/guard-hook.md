# Reading `ppl doctor`

The guard hook is a `PreToolUse` hook registration in Claude Code settings, and registering
it is a decision the owner makes separately from anything `ppl` does - it is not armed by
default, and nothing about starting a run arms it on its own [R-GUARD-NOT-ARMED n/a].
`ppl doctor` prints a JSON line whose `registration` field is one of:

- **`none`** - the hook is not registered anywhere. Every sentence in this skill about a hook
  stopping something is false for this session, right now: the write boundary, the Bash
  landmines, the Jira gate, the operator pause - none of it fires. `git add -A` in the
  worktree returns exit 0 with no warning at all, because there is no hook there to see it.
  You are unprotected, and the only checks that actually run are the ones `ppl` itself
  performs (the state machine and the done gate) - a role staying inside its lane because
  you dispatched it correctly, not because anything physically stopped it if you did not.
  Tell the operator, and proceed on that basis, or pause and ask them to arm it first
  (README.md "Hooks"). The dispatch counter is a hook too (`SubagentStart`): with
  `none`, run `ppl budget spend dispatch` yourself before every `ppl-*` dispatch, or the
  hard cap at `ppl stage enter` compares against zero for the whole run.
- **`local`** - registered in this directory's `.claude/settings.local.json` only. This is
  the isolated, recommended state; the present-tense claims in this skill hold for this run.
- **`global`** or **`both`** - registered in `~/.claude/settings.json`, which fires in every
  Claude Code session on the machine, not just this run - the opposite of what `isolated`
  asserts. `ppl init --isolated` (the default) already refuses `SCHEMA_INVALID` on a NEW run
  in this configuration [R-INIT-ISOLATION-CHECK]; if you are resuming an existing run that
  predates this state, treat the isolation guarantee specifically as an owner decision, the
  same as `none`, even though the hook itself is armed and every other cage rule does fire.
- **`unknown`** - a settings file exists but could not be read or parsed. Read this the same
  way as `none`: this check has no confirmed answer about isolation from a file it is unable
  to read, so nothing in this skill should be taken as armed until that is fixed.

The same JSON line carries a `matcher` field - `ok` when every registered guard entry's matcher
passes all the tool names the hook acts on, `incomplete` when some are left out, with the
`uncovered` list under `local`/`global` naming them. An uncovered tool is a denial that can never
fire, so read `incomplete` as `none` for exactly those tools, tell the operator, and expect
`ppl init` to refuse a NEW run until the matcher is widened to the one in README.md "Hooks"
[R-GUARD-MATCHER].

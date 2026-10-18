# Stage 5 - verification

`ppl stage enter verification`. Before dispatch, run `ppl findings list --task <task_id>`
yourself and pass the result to
`ppl-verifier` along with the brief, the challenge report (`runs/<task>/challenge.json`),
the diff, the tests, the gate receipts with their exit codes, and the `spec_revision` string
`ppl brief set` printed in Stage 2 - so it does
not re-report a defect someone already recorded (see the dedupe note below) and so its
verdict carries the current binding rather than one it invented (`ppl show --json` also
carries the current value, if a rebrief happened since Stage 2 and you need to refetch it).
Then dispatch `ppl-verifier`. It has no
shell and cannot run `ppl` or re-run anything itself, by design: a role with a shell and no
write boundary could write anywhere, and every generated role is denied every state-changing
`ppl` subcommand even where it does hold Bash (`ppl-code-worker`, `ppl-test-worker`) -
`ppl show --task <id>` and `ppl findings list` stay allowed, everything else does not
[R-ROLE-TOOLS-READONLY] [R-GUARD-PPL-CLI-DENIED]. It verifies only what the receipts and
the diff already prove. The exact field names and types `ppl verdict record` checks are in
`references/verdict.md` - read it before composing the verdict, not after `ppl verdict
record` rejects the first attempt: SCHEMA_INVALID, and no schema reference to read, is
exactly the shape D3 (cycle 2 wave D) found taking six attempts to get past. Its verdict
maps each acceptance ID to a string of the exact form
`evidence:<check-name>` (letters, digits, dot, underscore, hyphen only); `ppl verdict
record` rejects anything else, and `ppl transition done` later refuses an acceptance ID
whose reference does not resolve to a receipt that actually passed on the current head SHA
[R-ACCEPTANCE-EVIDENCE-FORMAT] [R-DONE-ACCEPTANCE-EVIDENCE].

Findings are not part of the verdict document any more - they live in their own registry,
and only YOU, the orchestrator, write to it; the verifier only reports. Its report carries
a separate `findings` array (severity, evidence, affected_sha, affected_path, owner when it
can name one) alongside the empty-by-contract verdict JSON. For each entry, before
recording the verdict, run `ppl findings add --severity <blocking|important|minor>
--evidence "<...>" --affected-sha <candidate_sha> --affected-path <path> [--owner <name>]
[--fix-link <url>] --generation $GENERATION --from ppl-verifier` - your own generation, not
anything the verifier claims, since it holds no lease. `ppl findings add` decides the routing itself,
from the real base-to-candidate diff, never from what is asked of it: a finding whose
`--affected-path` is inside the candidate's own diff is `own_diff` (fixed now, as REWORK
below); a finding whose path the candidate never touched is `foreign` (batch-escalated to
`--owner` and does not block this task) [R-FINDINGS-TRACK-DERIVED]. A verdict document
whose `findings` array is not empty is refused when recorded
[R-FINDINGS-VERDICT-FIELD-RETIRED] - record the findings first.

Findings no longer expire when the head moves (a real defect does not stop existing
because an unrelated commit landed), so re-adding the same still-open defect on every round
would pile up duplicate blockers instead of the one real finding closing once it is
actually fixed. That is why you list open findings before dispatch and hand them to the
verifier: only add what it reports as genuinely new.

Record the verdict with `ppl verdict record <file> --generation $GENERATION --from
ppl-verifier`. It refuses a verdict produced for a different SHA than the current head
[R-DONE-SHA-BINDING].

The verdict's own `verdict` field (`PASS | REWORK | BLOCKED | OWNER_DECISION | INCONCLUSIVE
| ERROR | CANCELLED | TIMEOUT`, written by the verifier) is a different, smaller vocabulary
than `error_code` (the closed list in `references/escalation.md`, printed by `ppl` on
stderr). They happen to share the exact spelling `OWNER_DECISION` and nothing else lines up -
`BLOCKED` here is not itself a valid `--blocker` value for `ppl park`, it is the verifier
telling you to pick one from its stated finding. Route all eight:

- **PASS** - forward to Stage 6.
- **REWORK** - `ppl budget spend retry` first. This is the only place the retry budget is
  ever spent, so skipping it makes the three-cycle limit below unenforced prose, not a real
  limit. Then dispatch a fresh worker with the finding, which produces a new SHA and
  restarts Stage 4 in full. If `ppl budget spend retry` itself exits 2 with
  `BUDGET_EXHAUSTED`, stop rework immediately and park; do not dispatch the worker it was
  gating. Three rework cycles is the limit either way; past that the problem has changed
  class and needs a re-brief, a re-plan, or the owner.
- **BLOCKED** - the verifier judges the candidate unfit to proceed, for a reason stated in
  the findings you just recorded on its behalf (`ppl findings list --task <task_id>` to see
  them again). Map that reason to the closest matching `error_code` in
  `references/escalation.md` (typically `BASELINE_FAILURE`, `EXTERNAL_BLOCKED`, or
  `IMPLEMENTATION_FAILURE` depending on what the finding actually says) and run `ppl park
  --blocker <that-code> --safe-next "<action>"`. `ppl park` moves the lifecycle status to
  `blocked` itself as part of parking - do not also run `ppl transition blocked`, the state
  machine now refuses that as `ILLEGAL_TRANSITION` [R-FSM-LIFECYCLE].
- **OWNER_DECISION** - the specification does not determine the answer even after reading
  the code, which is the verifier's own rule for choosing this verdict. Run
  `ppl escalate --code OWNER_DECISION --question "<the verifier's question, from its
  report>"`.
- **INCONCLUSIVE, ERROR, TIMEOUT** - a check did not actually happen: it crashed, ran past
  its own time budget, or produced no clear result. None of these is a kind of PASS and none
  is a kind of REWORK/BLOCKED either - a check that did not run is not the code's fault, so
  retry budget is not spent for one - only `ppl budget spend retry` above ever spends it
  [R-BUDGET-RETRY]. Spend `ppl budget spend dispatch` instead (dispatch
  already caps a runaway loop) and dispatch a fresh verification attempt at the SAME sha -
  nothing about the code needs to change, only the check needs retrying. Do not decide
  whether this is the second such outcome in a row yourself: `ppl verdict record` already
  refuses it for you if it is, exiting 2 with `VERIFICATION_UNAVAILABLE` and a parking record
  already written [R-VERIFICATION-UNAVAILABLE-PARK] - read that exit code, do not
  re-dispatch into the same check a third time regardless of what this bullet says. Never
  let a verifier report PASS to mean "I ran out of time to check properly" - that is exactly
  the self-attestation the done gate exists to refuse [R-DONE-VERDICT-PASS].
- **CANCELLED** - a human or the operator cancelled this check, not the verifier itself. Do
  not redispatch it automatically; that would be ignoring the person who stopped it. Run
  `ppl escalate --code OWNER_DECISION --question "verification was cancelled on <sha> - "
  "resume, redispatch, or park?"` and wait.

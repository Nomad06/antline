# Review monitor loop

Started with `/loop` in dynamic mode. The loop schedules its own next wake-up: 1200 to 1800
seconds while idle, tighter while a CI pipeline is running. Every tick is stateless and
reads durable state plus live GitLab, not chat history.

Before the first tick: call the GitLab MCP's `whoami` once and run
`ppl mr set --iid <iid> --url <url> --author <username>` with the result. The authorship
filter in step 4 below has nothing to compare against until this has run at least once -
without it, every thread looks unauthored-by-the-pipeline and nothing is filtered, which
`ppl` will say plainly in its log rather than pretending to filter. `--author` is sticky
across later `mr set` calls (a re-push that changes `--iid` does not need to repeat it).

Each tick, in this order:

1. `ppl recover` - facts first. Capture the lease generation it reports; it is the value
   this tick's step 6 must pass as `--generation` (see SKILL.md "The lease") - a generation
   captured on a previous tick is stale the moment a new `ppl recover` call reports a
   different one. This same call is also what keeps a healthy, still-open review from ever
   expiring: it refreshes the run's inactivity deadline on any tick where nothing needed a
   reply, precisely because it is the only `ppl` activity that tick produced. If
   its actions instead report no activity since the deadline (blocker `TIMED_OUT`, see
   `references/escalation.md`), the run is already parked: stop the tick here and follow that
   blocker instead of the steps below. This tick is, structurally, the only thing that ever
   discovers that kind of timeout on its own.
2. Fetch from GitLab: `mr_discussions`, `get_merge_request_approval_state`,
   `list_merge_request_pipelines`. Build a JSON array, one object per thread, each with
   `id`, `author` (author of the first note), `resolved`, `last_note_id` (id of the newest
   note), `last_author` (author of the newest note) and `note_count` (notes that are not
   `system: true`), and write it to a file. The MCP returns `{"items": [...], "pagination":
   ...}` - an object, not an array. A JMESPath that starts with `[?...]` evaluates to `null`
   on it and the tool prints `null`, which reads exactly like "no threads". On the first
   tick run the call once without JMESPath and look at the shape; afterwards start every
   filter with `items[...]`, and treat any `null` from a list tool as a filter bug to fix
   before the tick continues.
3. `ppl approval check` - the last line of stdout is a single JSON object,
   `{"approval": "current"|"stale"|"none", "approved_sha": ..., "head_sha": ...}`; parse that
   line and branch on `approval`, not on the human-readable line above it. If it says
   `stale`, previous approvals do not count - say so in the tick summary rather than quietly
   proceeding.
4. `ppl review pending --threads <file> --sha <sha>`. This is the dispatch filter, not an
   input to one - do not re-derive "which threads need a reply" by reading the discussions
   yourself. Parse the JSON array on the command's last stdout line (the human-readable
   count above it is not the contract, same rule as `ppl approval check`) and dispatch every
   thread it names to `ppl-review-responder`. The array already excludes resolved threads,
   the pipeline's own notes, and threads whose newest note matches what was last recorded
   for this SHA - which is what lets a reviewer's follow-up on an already-answered thread -
   "no, that is not what I meant" - reach the responder again on the same SHA, instead of
   waiting for the SHA to change. Own-reply detection needs `last_author` and
   `note_count`: `whoami` is normally the human MR author, so a single-note thread that
   identity opened is a remark to answer, while a multi-note thread whose newest note is
   by the identity is the pipeline's own reply and is dropped even if the ledger missed it.
5. Act per classification:
   - `NEEDS_CODE_CHANGE`: re-enter stage 3 with the remark as an added constraint. The full
     loop runs again: worker, test worker, gates, verifier. A new SHA invalidates approvals.
   - `NEEDS_REPLY_ONLY`: post the draft as a note on that thread.
   - `DISAGREE`: do not argue in the thread. Surface the counter-argument to the owner.
   - `NEEDS_OWNER`: surface it and park if it blocks progress.
6. After posting, `ppl review answered --thread <id> --sha <sha> --classification <c>
   --last-note-id <note-id> --generation <n>` with the newest note id observed in step 2
   and the generation captured in step 1, so the next tick's
   `ppl review pending` does not name this thread again - unless and until a newer note
   lands on it.
7. **Do not resolve the thread** - only the person who wrote the remark may resolve it
   [R-MCP-RESOLVE-THREAD].
8. Red CI on the candidate: classify against the baseline first. A pre-existing failure is
   `BASELINE_FAILURE`, a new one is rework. Do not just run it again.
9. Terminal: approval bound to the current head SHA. Close out in this order:
   <!-- include: sequence/close-out -->, then tell the owner. `ppl approval check` must
   read `current` before you go on.

Wake-up discipline: do not poll for work the harness will notify you about. Use a long
fallback interval and let notifications do the fast path.

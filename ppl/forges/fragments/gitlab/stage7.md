`ppl stage enter review`, then run the review loop with `/loop`. The full loop, including
its wake-up discipline and the CI classification rule, is `references/review-loop.md` - read
it before the first tick. The commands below are the ledger half of that loop, and nothing
else records approval, so skipping them leaves `ppl approval check` printing `none` forever,
and the run does not end.

Once, before the first tick: call the GitLab MCP's `whoami` and run
`ppl mr set --iid <iid> --url <url> --author <username>` with the result. Until that has run,
no thread can be recognised as the pipeline's own and nothing is filtered out.

Each tick:

1. Fetch discussions, approval state and pipelines from GitLab. Build a JSON array, one
   object per thread with `id`, `author` (who opened it), `resolved`, `last_note_id`,
   `last_author` (who wrote the newest note) and `note_count` (non-system notes), and write
   it to `<!-- include: harness/scratch -->/<TASK-ID>/threads.json`. The GitLab MCP wraps list results as `{"items": [...], "pagination": ...}`,
   not a bare array: a JMESPath written for an array (`[?...]`) evaluates to `null` on it and
   the tool prints `null` as if there were no threads. On the first tick call `mr_discussions`
   once with no JMESPath to see the real shape, then start every filter with `items[...]`.
   A `null` from a list tool is a filter bug, not an empty thread list: treat it as one
   before the tick continues. Skip notes with `system: true`.
2. `ppl approval check` - it prints a human line and then, as the LAST line of stdout, a
   single JSON object: `{"approval": "current"|"stale"|"none", "approved_sha": ...,
   "head_sha": ...}`. Parse that last line and branch on `approval`; do not branch on the
   prose line above it. A moved head SHA makes every prior approval stale.
3. `ppl review pending --threads <!-- include: harness/scratch -->/<TASK-ID>/threads.json --sha <sha>` is the dispatch filter, not an input to
   one. Parse the JSON array on its last stdout line and dispatch exactly the threads it
   names to `ppl-review-responder`. Do not re-derive that set by reading the discussions
   yourself. The identity from `whoami` is usually the human MR author (the MCP acts as
   that account), so a thread that identity merely opened is a review remark and stays
   pending; only a multi-note thread whose newest note is by the identity counts as the
   pipeline's own reply - which is why `last_author` and `note_count` are required above.
4. `NEEDS_CODE_CHANGE` re-enters Stage 3 with the remark as an added constraint:
   `ppl stage enter implementation` (it names `references/stage-3-implementation.md`), or
   `ppl stage enter tests` (`references/stage-3-tests.md`) when the remark is about the tests,
   before the fresh worker.
   `NEEDS_REPLY_ONLY` gets its draft posted. `DISAGREE` and `NEEDS_OWNER` go to the owner -
   do not argue in the thread yourself.
5. Post replies through the GitLab MCP. **You do not resolve a thread** - only the person
   who wrote the remark may [R-MCP-RESOLVE-THREAD]. After posting, record it:
   `ppl review answered --thread <id> --sha <sha> --classification <c> --last-note-id <id> --generation $GENERATION`,
   so the next tick does not name the same thread again unless a newer note lands on it.
6. Terminal condition: the MR is approved on the current head SHA. Then, in this order:
   <!-- include: sequence/close-out -->. `ppl review approved` is the only writer of the
   approved SHA - without it `ppl approval check` prints `none` forever, and the run does
   not end - and `ppl approval check` must now read `current`. Do not skip `merge_watch` even if
   `ppl doctor` reported the guard hook is not registered - **if it is armed**, it will not
   let you write to the tracker until the run's stage is exactly `merge_watch` and the merge is
   recorded (see `references/stage-8-after-merge.md`); if it is not armed, nothing stops the write
   either way [R-GUARD-NOT-ARMED n/a],
   so entering `merge_watch` first is the only thing that still holds the line.

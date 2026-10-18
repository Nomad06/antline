# Review monitor loop

Started with `/loop` in dynamic mode. The loop schedules its own next wake-up: 1200 to 1800
seconds while idle, tighter while a CI pipeline is running. Every tick is stateless and
reads durable state plus live GitHub, not chat history.

Before the first tick: run `gh api user --jq .login` once and run
`ppl mr set --iid <number> --url <url> --author <login>` with the result. The authorship
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
2. Fetch from GitHub: `gh api repos/<owner>/<repo>/pulls/<number>/comments --paginate` for
   review comments, `gh api repos/<owner>/<repo>/pulls/<number>/reviews` for review states
   and `gh pr checks <number>` for CI. Group the comments by `in_reply_to_id` into threads (a
   comment with no `in_reply_to_id` opens a thread). Build a JSON array, one object per
   thread, each with `id` (the opening comment id, written as a JSON string such as
   "123456": the REST API returns an integer and `ppl review pending` rejects one),
   `author` (`user.login` of the opening comment), `resolved`, `last_note_id` (id of the
   newest comment), `last_author` (`user.login` of the newest comment) and `note_count`
   (number of comments in the thread), and write it to a file. The REST API does not expose
   resolution, so every tick also runs `gh api graphql` for
   `reviewThreads { isResolved comments(first: 1) { nodes { databaseId } } }` and takes
   `resolved` from it, matching a thread by the `databaseId` of its opening comment. A
   thread the lookup does not return counts as unresolved.
3. `ppl approval check` - the last line of stdout is a single JSON object,
   `{"approval": "current"|"stale"|"none", "approved_sha": ..., "head_sha": ...}`; parse that
   line and branch on `approval`, not on the human-readable line above it. If it says
   `stale`, previous approvals do not count - say so in the tick summary rather than quietly
   proceeding. Approval on GitHub is judged by the latest review of each reviewer: a later
   `CHANGES_REQUESTED`, or a dismissal, supersedes an earlier `APPROVED` from the same
   reviewer, and a plain `COMMENTED` review changes nothing. Read it with
   `gh pr view <number> --json=reviews,headRefOid` and accept an `APPROVED` review only
   when its `commit.oid` equals `headRefOid`. The `=` form is what the responder's cage
   accepts and is safe for the orchestrator too.
4. `ppl review pending --threads <file> --sha <sha>`. This is the dispatch filter, not an
   input to one - do not re-derive "which threads need a reply" by reading the discussions
   yourself. Parse the JSON array on the command's last stdout line (the human-readable
   count above it is not the contract, same rule as `ppl approval check`) and dispatch every
   thread it names to `ppl-review-responder`. The array already excludes resolved threads,
   the pipeline's own notes, and threads whose newest note matches what was last recorded
   for this SHA - which is what lets a reviewer's follow-up on an already-answered thread -
   "no, that is not what I meant" - reach the responder again on the same SHA, instead of
   waiting for the SHA to change. Own-reply detection needs `last_author` and
   `note_count`: the login from `gh api user` is normally the human PR author, so a
   single-note thread that identity opened is a remark to answer, while a multi-note thread
   whose newest note is by the identity is the pipeline's own reply and is dropped even if
   the ledger missed it.
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
   [R-FORGE-GH-NO-APPROVE-MERGE].
8. Red CI on the candidate: classify against the baseline first. A pre-existing failure is
   `BASELINE_FAILURE`, a new one is rework. Do not just run it again.
9. Terminal: approval bound to the current head SHA. Close out in this order:
   <!-- include: sequence/close-out -->, then tell the owner. `ppl approval check` must
   read `current` before you go on.

Wake-up discipline: do not poll for work the harness will notify you about. Use a long
fallback interval and let notifications do the fast path.

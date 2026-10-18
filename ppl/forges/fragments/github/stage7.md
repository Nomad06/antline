`ppl stage enter review`, then run the review loop with `/loop`. The full loop is
`references/review-loop.md` - read it before the first tick. The commands below are the
ledger half of that loop, and nothing else records approval.

Once, before the first tick: `gh api user --jq .login`, then
`ppl mr set --iid <number> --url <url> --author <login>`.

Each tick:

1. Fetch review threads: `gh api repos/<owner>/<repo>/pulls/<number>/comments --paginate` for
   review comments and `gh api repos/<owner>/<repo>/pulls/<number>/reviews` for review
   states. Group comments by `in_reply_to_id` into threads (a comment with no
   `in_reply_to_id` opens a thread). Build a JSON array, one object per thread with `id`
   (the opening comment id, written as a JSON string such as "123456": the REST API returns
   an integer and `ppl review pending` rejects one), `author` (`user.login` of the opening
   comment), `resolved`, `last_note_id` (id of the newest comment), `last_author`
   (`user.login` of the newest comment) and `note_count` (number of comments in the
   thread), and write it to a file. The REST API does not expose resolution, so every tick
   also runs
   `gh api graphql -f query='query { repository(owner: "<owner>", name: "<repo>") { pullRequest(number: <number>) { reviewThreads(first: 100) { nodes { isResolved comments(first: 1) { nodes { databaseId } } } } } } }'`
   and takes `resolved` from it, matching a thread by the `databaseId` of its opening
   comment. A thread the lookup does not return counts as unresolved.
2. `ppl approval check` - parse the JSON object on its LAST stdout line, branch on
   `approval`. Approval on GitHub is judged by the latest review of each reviewer: a later
   `CHANGES_REQUESTED`, or a dismissal, supersedes an earlier `APPROVED` from the same
   reviewer, and a plain `COMMENTED` review changes nothing. Read it with
   `gh pr view <number> --json=reviews,headRefOid` and accept an `APPROVED` review only
   when its `commit.oid` equals `headRefOid`. The `=` form is what the responder's cage
   accepts and is safe for the orchestrator too.
3. `ppl review pending --threads <file> --sha <sha>` is the dispatch filter. Dispatch exactly
   the threads it names to `ppl-review-responder`.
4. `NEEDS_CODE_CHANGE` re-enters Stage 3 with the remark as an added constraint.
   `NEEDS_REPLY_ONLY` gets its draft posted. `DISAGREE` and `NEEDS_OWNER` go to the owner.
5. Post replies with `gh api repos/<owner>/<repo>/pulls/<number>/comments/<id>/replies -f body=<text>`.
   **You do not resolve a thread**, you do not `gh pr review --approve`, you do not
   `gh pr merge` - when the hook is armed those three are denied outright
   [R-FORGE-GH-NO-APPROVE-MERGE]. After posting, record it:
   `ppl review answered --thread <id> --sha <sha> --classification <c> --last-note-id <id> --generation $GENERATION`.
6. Terminal condition: the pull request is approved on the current head SHA (step 2). Then,
   in this order: <!-- include: sequence/close-out -->. `ppl approval check` must read
   `current`.

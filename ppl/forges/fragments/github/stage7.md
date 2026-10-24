`ppl stage enter review`, then run the review loop with `/loop`. The full loop is
`references/review-loop.md` - read it before the first tick. The commands below are the
ledger half of that loop, and nothing else records approval.

Once, before the first tick: `<!-- include: os/gh --> api user --jq .login`, then
`ppl mr set --iid <number> --url <url> --author <login>`. <!-- include: os/flag-note -->

Each tick:

1. Fetch the review threads with `<!-- include: os/ppl --> review fetch --pr <number>`: it
   reads the pull request's review comments, reviews and thread resolution, writes the
   threads to a file of the run and prints its path. <!-- include: os/flag-note -->
2. `ppl approval check` - parse the JSON object on its LAST stdout line, branch on
   `approval`. Approval on GitHub is judged by the latest review of each reviewer: a later
   `CHANGES_REQUESTED`, or a dismissal, supersedes an earlier `APPROVED` from the same
   reviewer, and a plain `COMMENTED` review changes nothing. Read it with
   `<!-- include: os/gh --> pr view <number> -R <!-- include: os/forge-repo --> --json=reviews,headRefOid`
   and accept an `APPROVED` review only when its `commit.oid` equals `headRefOid`.
   <!-- include: os/flag-note -->
3. `ppl review pending --threads <the path step 1 printed> --sha <sha>` is the dispatch
   filter. Dispatch exactly the threads it names to `ppl-review-responder`.
4. `NEEDS_CODE_CHANGE` re-enters Stage 3 with the remark as an added constraint:
   `ppl stage enter implementation` (it names `references/stage-3-implementation.md`), or
   `ppl stage enter tests` (`references/stage-3-tests.md`) when the remark is about the tests,
   before the fresh worker.
   `NEEDS_REPLY_ONLY` gets its draft posted. `DISAGREE` and `NEEDS_OWNER` go to the owner.
5. Post a reply: write it to `<!-- include: harness/scratch -->/<TASK-ID>/reply-<id>.md`, then run
   `<!-- include: os/gh --> api repos/<!-- include: os/forge-repo -->/pulls/<number>/comments/<id>/replies -F body=@<!-- include: harness/scratch -->/<TASK-ID>/reply-<id>.md`.
   <!-- include: os/flag-note --> **You do not resolve a thread, approve or merge** - when
   the hook is armed those three are denied outright [R-FORGE-GH-NO-APPROVE-MERGE]. After
   posting, record it:
   `ppl review answered --thread <id> --sha <sha> --classification <c> --last-note-id <id> --generation $GENERATION`.
6. Terminal condition: the pull request is approved on the current head SHA (step 2). Then,
   in this order: <!-- include: sequence/close-out -->. `ppl approval check` must read
   `current`.

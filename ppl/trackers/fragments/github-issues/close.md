write the note to `/tmp/ppl-<TASK-ID>/note.md`, then run
`gh issue comment <number> -R <owner/name> --body-file /tmp/ppl-<TASK-ID>/note.md`, taking
`<number>` and `<owner/name>` from the `URL:` line of the fetched `issue.md` and typing them
as literals, with no shell variable in the command

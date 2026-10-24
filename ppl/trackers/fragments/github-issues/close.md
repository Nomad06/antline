write the note to `<!-- include: harness/scratch -->/<TASK-ID>/note.md`, then run
`<!-- include: os/gh --> issue comment <number> -R <!-- include: os/tracker-repo --> --body-file <!-- include: harness/scratch -->/<TASK-ID>/note.md`.
<!-- include: os/flag-note --> Take `<number>` from the `URL:` line of the fetched `issue.md`
and type it as a literal, with no shell variable in the command

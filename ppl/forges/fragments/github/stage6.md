`ppl stage enter mr`. Push the branch with
`<!-- include: os/push -->` [R-OS-SANDBOX-GIT-CONFIG].
Write the pull request body to `<!-- include: harness/scratch -->/<TASK-ID>/pr-body.md`, describing each acceptance
criterion and the receipt that proves it, and open the pull request with
`<!-- include: os/gh --> pr create -R <!-- include: os/forge-repo --> --head <branch> --base <!-- include: harness/default-branch --> --title "<TASK-ID> <summary>" --body-file <!-- include: harness/scratch -->/<TASK-ID>/pr-body.md`.
<!-- include: os/flag-note --> `<worktree>` is the run's worktree and `<branch>` its branch
from Stage 1. Record the pull request with `ppl mr set --iid <number> --url <url>`. Do not
write to the tracker here.

# CLAUDE.md

Rules for anyone, agent or human, who commits to this repository.

## Commit identity

Every commit in this repository, on every branch, is authored and committed as
`Nomad06 <fortune06@yandex.ru>`. That includes commits made by agents and subagents. A
`Co-Authored-By:` trailer is allowed and does not change the author.

- Before the first commit in a clone or worktree, check `git config user.name` and
  `git config user.email`. If they differ, set them for this repository only, never globally:

  ```bash
  git config user.name "Nomad06"
  git config user.email "fortune06@yandex.ru"
  ```

- Never commit under another identity through `--author`, `-c user.name=...`,
  `-c user.email=...` or the `GIT_AUTHOR_*` / `GIT_COMMITTER_*` environment variables.
- Before every push, list the identities of the commits about to leave:

  ```bash
  git log --format='%an <%ae> | %cn <%ce>' origin/main..HEAD
  ```

  Every line must read `Nomad06 <fortune06@yandex.ru> | Nomad06 <fortune06@yandex.ru>`. A
  local commit with any other identity is fixed before the push (for the last commit,
  `git commit --amend --reset-author --no-edit`), never pushed as is.
- Work reaches `main` through a pull request that the owner merges on GitHub. Agents do not
  merge pull requests or push to `main` unless the owner asks for it.

# antline

Drive an issue through briefing, isolated implementation, independent tests,
deterministic gates, independent verification and review, to an approved merge
request or pull request. Claude Code subagents do the work inside a cage. A
PreToolUse hook denies every write outside a role's boundary
[R-ROLE-CODE-WORKER-BOUNDARY], every read the test worker must not make
[R-TEST-WORKER-READ-TOOLS], and the forge actions a human must perform
[R-MCP-RESOLVE-THREAD].

`docs/DESIGN.md` is the specification. This file is how to install and use it.

## Requirements

- Python 3.11 or newer, no packages.
- Claude Code (`claude` on PATH, logged in).
- git.
- A git forge: GitLab through a GitLab MCP server, or GitHub through `gh`.
- Jira through the Atlassian MCP server for the tracker.

## Install

```bash
git clone https://github.com/Nomad06/antline ~/antline
export PATH="$HOME/antline/bin:$PATH"     # add this line to your shell profile
ppl setup
```

The supported install is the clone with `bin/` on `PATH`: `bin/ppl` points ppl at
the checkout, where `defaults/`, `templates/` and `skill/` live. An editable dev
install (`pip install -e .`) also works from the clone.

`ppl setup` walks six steps - paths, claude, models, harness, forge, apply -
each skippable with `s` and re-runnable with `ppl setup <step>`: the data root
(`~/.ppl`), the hooks in an isolated workspace (`~/ppl-workspace` by default),
a model per tier verified by a cheap probe, your harness from an example, the
forge check, and the agent files and skill. `ppl init` refuses a run until
`ppl setup` has recorded models, harness and apply as done [R-INIT-SETUP-COMPLETE],
`--skip-model-check` waives models, and a harness only added and applied by hand
gets `SETUP_INCOMPLETE` naming the `ppl setup` step to run.

## Studio

`ppl serve --open` starts the studio on `http://127.0.0.1:8787` and opens it in your browser,
`ppl setup --web` opens it straight on the setup wizard, and a first `ppl setup` in a terminal
offers it. The wizard runs the same six steps as `ppl setup`, records them in the same
`config.toml` and writes the same files, so you can start in one and finish in the other.

On the Project and harness screen the studio looks at your checkout. When it has a
`CLAUDE.md` or `AGENTS.md`, one short Claude call on your lightest model tier reads them and
drafts the gates, the commands the test writer may run and the build environment (a few
cents). Otherwise, or when that call fails, the draft starts from a preset for the stack your
build files name: npm, Python, Maven, Gradle, Go, Cargo or Make. You review every field in a
form before anything is installed, with the source of each field shown beside it.

The Models screen offers three presets, a tier and an effort per role: Economy, Balanced
and Max quality. The same presets are `ppl routing preset economy|balanced|max`.

The studio listens on loopback only. Every request needs the session token in the link
`ppl serve` prints [R-STUDIO-SESSION-TOKEN].

After setup the studio has three more pages. **Agents** sets the model tier and effort per
role (or a preset for all of them), shows each role's base prompt beside its project notes
and the agent file apply would write, and lists what the cage enforces for that role
whatever its prompt says [R-GUARD-NOT-ARMED n/a]. **Harness** edits, adds and creates harnesses, and applies one, which also makes it the
active harness, as `ppl harness apply` does.
In Expert mode a raw editor sits beside every form, every save keeps the content it replaced
in `~/.ppl/studio/history/` with a restore, and **Playground** runs one tool call through the
real guard hook against a throwaway run. A save that would overwrite a file changed in
another tab or a terminal since the page loaded it is refused [R-STUDIO-CHANGED-ON-DISK].

## Hooks

`ppl setup claude` writes these into the workspace's `.claude/settings.local.json`
(answer `global` or `manual` and it prints them for you to paste):

```json
{"hooks": {
  "PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command",
    "command": "/path/to/python3.11 /path/to/antline/hooks/ppl_guard.py", "timeout": 5}]}],
  "SubagentStart": [{"matcher": "", "hooks": [{"type": "command",
    "command": "/path/to/python3.11 /path/to/antline/hooks/ppl_dispatch.py", "timeout": 5}]}]}}
```

The interpreter in those commands has to be Python 3.11 or newer, and `ppl setup
claude` writes the one that ran it, so the hooks do not depend on which `python3`
the `PATH` Claude Code spawns them with finds first. Under an older interpreter the
guard denies every role call [R-GUARD-PYTHON-FLOOR], and `ppl doctor` reports
`hook_runs: false` with the remedy.

The recommended matcher is `.*`, so the guard sees every call. `ppl init` refuses
a run under a matcher that leaves a guarded tool name uncovered [R-GUARD-MATCHER].
Those names carry the default tracker prefixes and forge server names only, so an
explicit matcher plus a custom `tracker.mcp_prefixes` or `forge.mcp_server` needs
those names covered by hand. `ppl init` also refuses an isolated run while the
guard is in `~/.claude/settings.json` [R-INIT-ISOLATION-CHECK], so if you answered
`global` in `ppl setup claude`, start runs with `ppl init --no-isolated`. Until the
hooks are registered, nothing in the cage is enforced [R-GUARD-NOT-ARMED n/a].

## Harness

A harness is a directory under `~/.ppl/harnesses/<name>/` holding everything
that belongs to one project: `harness.toml` (repo path, task key pattern,
tracker and forge settings, build gates, the command shapes the test worker
may run), `skill/` fragments and `templates/` overlays. Two examples ship in
`defaults/harnesses/`: `example-maven` (GitLab, Maven) and `example-generic`
(GitHub, make). Keep yours in its own git repository and install it with
`ppl harness add <name> --source <directory or git url>`.

```bash
ppl harness validate <name>
ppl harness apply <name>      # renders the agents and the skill, makes it active
```

The `skill/` fragments a harness can carry are `gates.md` (notes in front of the
gate recipes), `extra-gates.md` (project-specific gate steps), `baseline.md` (what
to record before any change) and `tracker-close.md` (the note written to the
tracker once the merge is observed, by default a short comment saying what was
done and how to check it). In a fragment, `<default-branch>` is replaced with
`repo.default_branch` when the skill is rendered, while `<worktree>`, `<TASK-ID>`,
`<SHA>` and `<GENERATION>` are left for the orchestrator to fill in at run time.

`ppl harness apply <name>` re-renders the agent files and the skill, so run it
after any harness or routing change. A harness's `tracker.mcp_prefixes`
replaces the default Atlassian prefixes rather than extending them: list every
prefix your tracker server is registered under. `ppl harness validate` warns when
the list keeps none of the defaults.

## Models

`~/.ppl/routing.toml` names a model per tier (`heavy`, `good`, `average`,
`light`) and a tier per role, see `defaults/routing.example.toml`. `ppl models
status` shows what is verified. `ppl init` refuses to start a run while a model
it would route to is unverified or stale, `ppl models verify` fixes that, and
`--skip-model-check` records that you chose not to [R-INIT-MODELS-VERIFIED].
After changing a tier, run `ppl models verify` and `ppl harness apply <name>`.
`ppl routing apply` re-renders the agent files alone, for `--harness <name>` or
the active harness. A role can carry `by_risk = { high = "good" }`, used for
runs started with `ppl init --risk high` [R-ROUTING-BY-RISK].

## Run a task

```bash
cd ~/ppl-workspace
claude
> /pipeline https://tracker.example/browse/KEY-123
```

The orchestrator skill takes it from there: `ppl init --harness <name>`, a
worktree under the harness's `repo.worktrees`, the stages of `docs/DESIGN.md`
section 7, and a review loop until the MR is approved. A halt is a parked run
with a code and a question, see the skill's `references/escalation.md`.

## Watch and control it

```bash
ppl status
ppl serve --open     # the studio on http://127.0.0.1:8787, the run list at /runs
ppl recover          # always safe, always first after a restart
ppl doctor           # hooks, setup, models, harness, forge
ppl control set pause|resume|stop
```

Run `ppl doctor` and `ppl init` from the workspace: both read the hook
registration from `.claude/settings.local.json` in the current directory, and from
anywhere else doctor reports `registration: none`.

## Sandbox

Four variables keep an experiment out of your installation:

- `PPL_HOME` - the data root (default `~/.ppl`): config, routing, models, harnesses, runs.
- `PPL_CLAUDE_HOME` - where `ppl doctor` looks for Claude settings (default `~/.claude`).
- `PPL_AGENTS_DIR` - where `ppl harness apply` writes the agent files.
- `PPL_SKILLS_DIR` - where `ppl harness apply` writes the skill.

```bash
export PPL_HOME=$(mktemp -d) PPL_CLAUDE_HOME=$(mktemp -d)
export PPL_AGENTS_DIR=$PPL_HOME/workspace/.claude/agents
export PPL_SKILLS_DIR=$PPL_HOME/workspace/.claude/skills
ppl setup --answer workspace=$PPL_HOME/workspace
```

A `claude` session started in that workspace sees the sandbox agents and skill.
Export `PPL_HOME` in the shell that launches `claude` too, or the hooks read a
different data root than the CLI. Check that `data_root` in `ppl doctor`'s JSON
line is your temp dir.

## Tests

```bash
python3 -m unittest discover -s tests -q
python3 tools/cage_check.py
```

The rule matrix (`rules.toml`, `tests/test_rule_matrix.py`) ties every
enforcement claim in the documents to a row and every blocking row to a test
that asserts a refusal. Keep it green. `tools/cage_check.py` drives the guard
hook as a subprocess, the way Claude Code does.

## License

MIT, see `LICENSE`.

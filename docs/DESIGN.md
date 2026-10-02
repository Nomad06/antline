# antline - Design

Status: v1 built and unit-tested. The cage is proven at hook level by `tools/cage_check.py`
(section 14), an end-to-end run against a live tracker, forge and reviewer is the operator's
to make.
Source doctrine: a twelve-chapter text on engineering reliable frameworks around AI agents,
cited below by chapter.

---

## 1. Purpose

Take a tracker task link and drive it through the full development lifecycle to a merge
request (a pull request on GitHub, MR below means either), then monitor reviewer remarks and
answer or fix them until the MR is approved.

The pipeline is the control plane. Models are replaceable components inside it. Every
stage transition is decided by deterministic code against artifacts, not by a model's
account of how well it did [R-FSM-STAGE-GRAPH] [R-DONE-GATE].

Target maturity: L2 (verified pipeline) plus L3 recovery. Not L4.

## 2. Scope

In scope for v1 (maps to book P0 + P1 + the review loop):

- one sequential lane, one task at a time
- spec/task admission, brief compilation, isolated implementation, independent tests,
  deterministic gates, independent verification, MR creation
- durable state, recovery after kill/restart, budgets, parking
- reviewer-remark monitoring loop until approval
- local admin panel: observe every run, pause, resume, stop, destroy (section 9)
- `routing.toml` as the single source of model routing, with agent files generated from it,
  and the provider-agnostic adapter contract frozen (section 10)

Out of scope for v1 (book P2/P3, deferred):

- parallel waves and multi-lane scheduling
- dependency-matrix-driven planning across many tasks
- mutation testing (Stryker-equivalent) and the critical-tier adversarial verifier
- model eval flywheel and automated routing updates
- adapter *implementations* for non-Claude-Code providers. The contract, the routing config
  and the patch-based enforcement path are built in v1; the API and CLI adapters are written
  when a second provider actually exists.
- a packaged installer: v1 runs from a git clone and `ppl setup` configures one machine

## 3. Authoritative decisions

These were decided by the owner and override any generic doctrine below.

| Decision | Value |
|---|---|
| Autonomous: create branch, commit, push | yes |
| Autonomous: create and update MR | yes |
| Autonomous: reply to reviewer threads | yes |
| Autonomous: resolve reviewer threads | NO. Only the remark author may resolve [R-MCP-RESOLVE-THREAD]. Globally denied by hook [R-MCP-RESOLVE-THREAD]. |
| Autonomous: tracker writes | only after merge is observed, in plain English. No pre-merge tracker writes. |
| Install location | code in a git checkout, data under `~/.ppl`, the generated agents and skill in an isolated workspace's `.claude/`. Nothing is committed to the project repository. |
| Model routing | frontier on top, rails at the bottom (section 12) |
| Panel `destroy` | always prompts. Owner picks local-only or full cleanup (close MR, delete remote branch) per run, with typed confirmation for the remote half. |
| Panel spawning sessions | NO. The panel queues a resume request; it never starts a Claude session [R-PANEL-NO-SPAWN n/a]. |
| Source egress to third-party providers | default-deny. `allow_source_upload` is an explicit per-provider opt-in. |
| v1 scope | P0 + P1 + review loop + panel + routing config (section 2) |

Consequence: the pipeline terminates on approval, not on merge. Merge is performed by a
human maintainer. The tracker update fires only if and when the pipeline observes the merge.

## 4. Non-goals

- Not a universal framework. The workflow is fixed, and everything project-specific (build
  gates, guard shapes, task key, tracker and forge) lives in a harness (5.6).
- Not autonomous product decisions. Material decisions return to the owner.
- Not a second task registry. The tracker is the business truth; `state.json` is a recoverable
  execution snapshot and nothing more.
- Not measured by unattended run length. Measured by safe accepted changes.

## 5. Architecture

### 5.1 Layout

```
<checkout>/                      the code root, a git clone (bin/ppl sets PPL_ROOT to it)
  bin/ppl                        shim on PATH
  ppl/                           the state machine package (the real controller)
    __main__.py cli.py           CLI entry point
    fsm.py store.py boundary.py budget.py control.py gitfacts.py routing.py run.py
    config.py                    config.toml in the data root: setup state, workspace, active harness
    setup.py                     `ppl setup`, the six-step onboarding wizard
    models.py                    tier models: `ppl models verify` probes and their record
    harness.py                   harness load, validate, add, apply, and the run snapshot (5.6)
    tomlout.py                   the TOML writer for the files setup rewrites
    schema.py                    brief and verdict field/shape checks (Python, not JSON Schema files)
    forges/                      gitlab.py github.py: denied tools, role tools, skill fragments
    trackers/                    jira.py: tracker read and write tool names per MCP prefix
    panel/server.py              stdlib HTTP server, `ppl serve`
    panel/index.html             single-page dashboard
    (no adapters/ package)       out of scope for v1 (section 2): a non-subagent role has no
                                  code path at all, `routing.dispatch` raises `SCHEMA_INVALID`
  hooks/ppl_guard.py             PreToolUse cage
  hooks/ppl_dispatch.py          SubagentStart dispatch counter (telemetry only, cannot block - see 12.1)
  skill/                         SKILL.md and references/ skeletons, rendered per harness
  templates/ppl-*.md.body        the fixed body each generated agent file is rendered around
  defaults/                      config.example.toml, routing.example.toml, harnesses/example-*
  tests/                         stdlib unittest suite
  tools/cage_check.py            subprocess-level proof of the guard hook
  rules.toml                     the rule matrix (5.5)
  docs/DESIGN.md                 this document
~/.ppl/                          the data root (PPL_HOME)
  config.toml                    per-machine settings, written by ppl setup
  routing.toml                   single source of truth: tier -> provider/model, role -> tier/params
  models.json                    which models `ppl models verify` proved, and when
  harnesses/<name>/              one installed harness: harness.toml, skill/, templates/
  runs/<TASK-ID>/                durable state for one task
    state.json                   written only by ppl
    harness.json                 the harness snapshot `ppl init --harness` froze (5.6)
    control.json                 inbox: written by panel or `ppl control set`
    brief.json
    boundary.json
    baseline.json
    threads.json                 review-remark ledger: thread id -> sha -> {classification, last_note_id} (8, 6.6)
    remote_cleanup.json          (only when a full destroy is queued, 9.3)
    evidence/<sha>/<check>.json
    verdicts/<sha>.json
    findings/<finding_id>.json   the findings registry (6.6a)
    parking.json                 (only when parked)
    log.jsonl                    append-only transition log
<workspace>/.claude/             the isolated launch directory (default ~/ppl-workspace)
  settings.local.json            the two hook registrations ppl setup writes
  agents/ppl-*.md                generated by ppl harness apply
  skills/pipeline/SKILL.md       orchestrator playbook, rendered by ppl harness apply, invoked as /pipeline <task>
  skills/pipeline/references/    gate recipes, escalation matrix, prompts
```

### 5.2 Components

**Orchestrator** - the main Claude Code session running the `pipeline` skill. Selects the
stage, compiles context, dispatches roles, runs deterministic gates, requests transitions
from `ppl`. It is the only *agent* allowed to perform external mutations (git push, MR
create, MR note, tracker write). It cannot edit code [R-UNROLED-REPO-WRITE] and cannot write `state.json` directly
[R-RUNS-WRITE-PROTECTION].
The one non-agent path to an external mutation is the panel's full-cleanup destroy, which
is an explicit operator action requiring typed confirmation and is executed by `ppl`, not by
a model (section 9.3).

**Role adapters** - `<workspace>/.claude/agents/ppl-*.md`, generated by `ppl harness apply`
(or `ppl routing apply`) from `routing.toml` and the active harness. Each declares `name`,
`description`, `tools`, `model`, `effort`, `maxTurns` and `permissionMode`. There is no
`disallowedTools` key and no role-scoped `hooks` key in a generated file - a role's rights
are the allow-list in `tools` plus the global `PreToolUse` guard, nothing else.
`routing.toml` supplies only the model and its params. The base tool list and description
come from `routing.BASE_TOOLS` in Python, and `routing.role_tools` adds only what the
harness names - the tracker's read tools and the knowledge tools for the briefer, the forge
adapter's review tools - so widening a role past that is a code edit, not a config edit.
No generated role holds a tracker write tool, and a harness that would grant one is refused
at validate and at render [R-JIRA-NO-ROLE-TOOL]. Subagents
cannot spawn subagents in Claude Code, so the "no recursive
delegation" rule from chapter 5 is enforced by the harness itself
[R-NO-RECURSIVE-DELEGATION n/a]. Section 10 defines the
provider-agnostic contract that lets a role move to a non-Claude model without touching the
workflow.

**Admin panel** - a loopback-only web app (`ppl serve`) that observes every run and issues
pause, resume, stop and destroy through a control file. It reads state and writes only
`control.json`. Section 9.

**State machine (`ppl/`)** - owns lifecycle, leases, budgets, evidence binding, parking
and recovery. Refuses illegal transitions [R-FSM-STAGE-GRAPH]. This is the component that makes the pipeline
engineering rather than prayer.

**Hook engine** - `ppl_guard.py` on `PreToolUse`, which can deny; `ppl_dispatch.py` on
`SubagentStart`, which cannot (12.1) and only counts [R-DISPATCH-HOOK-CANNOT-BLOCK]. Returns stable codes, not prose.

**Evidence store** - `runs/<TASK-ID>/evidence/<candidate_sha>/`. Every receipt is bound to
the SHA it was produced on.

**Delivery adapters** - the forge adapter the harness names (`ppl/forges/`: GitLab through
the GitLab MCP server the harness names, or GitHub through `gh`) and the tracker adapter
(`ppl/trackers/`: Jira through the Atlassian MCP server).

### 5.3 Roles, routing and boundaries

| Role | Tier | Effort | Writes | Denied |
|---|---|---|---|---|
| orchestrator (main session) | the operator's session | - | state only via `ppl` CLI | Edit/Write on repo code; direct write to `runs/` |
| ppl-briefer | good | high | nothing - it returns the brief as JSON and the controller writes it | every path, it has no write tool |
| ppl-brief-challenger | good | xhigh | nothing - it returns its challenge as JSON, recorded with `ppl brief challenge` | every path, it has no write tool |
| ppl-code-worker | average | high | paths in `boundary.write`, main sources | test sources (`**/src/test/**`, `**/*Test.java`, `**/*IT.java`, `**/*Tests.java`), git mutations |
| ppl-test-worker | average | high | test paths only [R-ROLE-TEST-WORKER-MAIN] | main sources for writes [R-ROLE-TEST-WORKER-MAIN]; its own Read/Grep/Glob and Bash reads of them are denied by the guard hook [R-TEST-WORKER-READ-TOOLS] [R-TEST-WORKER-BASH-READS] |
| ppl-verifier | good | xhigh | nothing | Edit, Write, MultiEdit, Bash, all git mutations |
| ppl-review-responder | average | high | nothing | posting directly; it drafts, orchestrator posts |

Rationale for routing: chapter 8. The stronger tier is used where the solution space is wide
(briefing, verification). The `average` tier executes on rails. The subscription window is
the real budget - workers are the token hogs, so they stay on `average`. Which model a tier
names is the operator's choice in `routing.toml`, see `defaults/routing.example.toml` and
12.2.

### 5.4 The cage

Prompts explain intent. Hooks decide what is physically possible.

`ppl_guard.py` fires on `PreToolUse`. The registered matcher (README.md "Hooks") is `.*`:
every tool call reaches the hook, and the hook itself decides which names it acts on,
returning nothing for the rest. An explicit tool list is the fragile alternative: a tracker
server registered under a prefix the list does not name, or a read tool it leaves out, is a
denial the hook never sees, and a denial the hook never sees cannot fire, however correct the
code behind it reads [R-GUARD-MATCHER]. So `ppl doctor` judges whatever matcher is registered:
every tool name the cage acts on (`hooks/ppl_guard.py:GUARDED_TOOL_NAMES` - the four editing
tools, the three read tools, Bash, the forge adapters' denied tools, the tracker write tools
under every default prefix) must be a full regex match of some registered guard entry's
matcher, the names left uncovered are reported per settings file as `uncovered` under a
top-level `matcher` verdict of `ok` or `incomplete`, and `ppl init` refuses `SCHEMA_INVALID` to
create a run while a confirmed registration leaves any of them uncovered, `--no-isolated` or
not [R-GUARD-MATCHER]. That list holds the default tracker prefixes and the default forge server names only, so an
operator who keeps an explicit matcher and names a custom tracker prefix or `forge.mcp_server`
in the harness covers those names by hand.

The tracker gate reads its MCP prefixes and write operations from the run's harness snapshot
(5.6). The defaults are the Atlassian server's two registration names, `mcp__atlassian__`
(added by hand) and `mcp__claude_ai_Atlassian_Rovo__` (added through claude.ai), each with its
numbered duplicates (`_2__`, `_3__`), and a harness's `tracker.mcp_prefixes` replaces them
rather than extending them, with a `ppl harness validate` warning when none of the defaults is
kept. The write operations come from the same snapshot, and the adapter's defaults stand in only
when the snapshot is missing or malformed: comment, edit, transition, worklog, create,
`executeWrite`, `executeDestructive`, Confluence page create and update and page comments, graph
context add, covering the names the server has used across builds, so an older or newer
registration is gated. The gate matches the write operations by name under any of those
prefixes (`is_tracker_write_tool`), never an exact tool name under one of them, and denies them
until the run reaches `merge_watch` [R-JIRA-GATE]. The hook reads `agent_type` from the stdin
payload (present when the hook fires inside a subagent), loads `runs/<active>/boundary.json`,
and returns:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "BOUNDARY_VIOLATION source/ledger/X.java is outside the write boundary for ppl-code-worker"
  }
}
```

The reason string is always `"<CODE> <message>"` - a stable code, a space, then a free-text
message. `<CODE>` is one of the codes in section 13 (`BOUNDARY_VIOLATION` for every cage
denial in this section, `OWNER_DECISION` for a pause/stop/destroy or a pre-`merge_watch`
tracker write).

**What the hook is scoped by.** Four denials are global and are decided before any run is
looked up (`ppl_guard.py`'s `decide()`, before `active_state()` is called): the denied forge
tools and forge Bash commands, the repo landmines, the `runs/` write protection, and a `ppl-*`
role invoking a state-changing `ppl` CLI subcommand of its own - all in the table below
[R-GUARD-GLOBAL-DENIALS]. They fire in every session and every directory, run or no run.
Everything else belongs to the active run, and a tool call belongs to that run when any of
three things is true: its `agent_type` is a `ppl-*` role, the path it is writing is inside
`state["repo"]`, or the session's cwd is inside that repo (or, for the tracker gate and the
operator pause specifically, the run is `isolated` and the session carries no `agent_type` at
all - see below). The path test is the load-bearing one and the cwd test alone is not enough:
a session's cwd is fixed when it launches, and stage 1 creates the worktree beside the
orchestrator's session rather than under it, so a cage scoped only by cwd is inert for exactly
the layout this design prescribes. A consequence, and an intended one (5.3): a write landing
inside the run's repo from a session carrying no `agent_type` - the orchestrator itself - is
denied `BOUNDARY_VIOLATION`, because only the two worker roles write the tree
[R-UNROLED-REPO-WRITE]. A session writing paths outside that repo, with no `ppl-*` role, is
not touched by any of this.

Denies, in addition to per-role boundaries, each row naming the rule-matrix id that carries
its deny test [R-DOC-RULE-IDS]. Not everything below is global - the "Scope" column says,
for each row, exactly what `ppl_guard.py` requires before it fires:

| Rule | Reason | Scope |
|---|---|---|
| `git add -A` / `git add .` [R-GIT-ADD-ALL] | git add -A can sweep a live credential into a commit, add explicit paths | global, no run required |
| bare `git stash` / `git stash pop` / `clear` / a `drop` that names no `stash@{N}` [R-GIT-STASH] | stash stack is shared across worktrees and other sessions. The recipe is `push -m <tag>`, `apply <sha>`, then `drop stash@{N}` at the index the entry is re-found at, so an explicit drop is allowed and a bare one, which takes the top entry, likely another session's, is not | global, no run required |
| `git push --force` / `--force-with-lease` / `-f` in any short cluster / a `+` refspec (`+main`, `+HEAD:main`) on shared refs [R-GIT-FORCE-PUSH] | irreversible | global, no run required |
| `git reset --hard` [R-GIT-RESET-HARD] | it discards worker output that has no receipt | global, no run required |
| the forge adapter's denied tools (GitLab and GitHub): resolving a thread [R-MCP-RESOLVE-THREAD], merging [R-MCP-MERGE-MR], approving or submitting a review [R-MCP-APPROVE-MR], taken as the union of both adapters' defaults plus the run's own server name [R-FORGE-DENIALS-FROM-ADAPTER] | owner rules: only the remark author resolves, the pipeline ends at approval and merge is a human action, the pipeline never approves its own merge request | global, no run required |
| `gh pr merge`, `gh pr review --approve` and a `gh api graphql` call carrying `resolveReviewThread`, as Bash [R-FORGE-GH-NO-APPROVE-MERGE] | the same three owner rules, through the GitHub CLI | global, no run required, whatever forge the run names |
| a write under the data root's `runs/` through Edit/Write/MultiEdit/NotebookEdit [R-RUNS-WRITE-PROTECTION], or through a Bash write-shaped construct whose resolved target lands under the runs directory - absolute, `~`, after a `cd`, `pushd` or `popd`, through a `(` subshell or `{` group, behind `env -C` or a wrapper verb (`command`, `builtin`, `exec`, `nohup`, `nice`, `time`, each read by its basename when spelled by a path), with the redirect glued or spaced, relative to the session cwd or through a symlinked alias - or a removal (`rm`, an `rm`-source `mv`/`git mv`, `git rm`, `rsync --delete`) that holds it (`rm -rf ~/.ppl`), or a copy into a directory that places a source's own name on it (`cp -r x/runs ~/.ppl`; a copy into `~` under another name writes beside runs/), or that names the runs directory literally [R-RUNS-BASH-WRITE] | state integrity. Sole carve-out: `control.json`, the panel's inbox (section 9.2), and in the Bash check for the unroled operator session only - a role that rewrote it could lift the operator's pause. An interpreter invocation (`python3 -c "open(path,'w').write(...)"`) or a quoted `sh -c` string models neither shape and is not caught, and a target spelled through a variable (`$HOME/...`) is not resolved, so only a pipeline-scoped session is denied it [R-GUARD-BASH-WRITE-BOUNDARY] - see R-RUNS-BASH-WRITE's residual | global, no run required |
| direct writes to the repo tree by any role whose provider is not a Claude Code subagent [R-APPLY-PATCH-BOUNDARY] | such a provider has no hooks at all to deny anything with; the guarantee is architectural (no direct write path exists for it), backed by `ppl apply-patch`'s separate diff validation (section 10.1) - `ppl_guard.py` makes no check of its own here | not a hook check |
| tracker write tools until merge is observed on the forge [R-JIRA-GATE] | owner rule: tracker writes only after merge | requires an active run; scoped as described above (the `isolated` broadening included) - **not** global, and not enforced at all when no run is active |
| every state-changing `ppl` CLI subcommand, when `agent_type` is one of the roles in `boundary.ROLE_RULES` [R-GUARD-PPL-CLI-DENIED] | `ppl-code-worker` and `ppl-test-worker` both hold `Bash`, and none of the write checks above sees a `ppl <command>` invocation - it writes through ppl's own API, not a shell redirect or a tracked path, so a worker driving its own CLI could mark its own blocking finding fixed or open its own done gate. Read-only lookups (`ppl show`, with `--task <id>` and not a positional id, `ppl status`, `ppl doctor`, `ppl metrics`, `ppl findings list`, `ppl control get`, `ppl routing show`, `ppl approval check`, `ppl mr state`, `ppl review pending`) stay allowed | global, no run required - fires by `agent_type` alone, before `active_state()` is even looked up |
| a write-shaped Bash construct (a redirect - `>`, `>>`, `&>`, `>|`, glued to a word like `echo x>f` or spaced, a `cd`'s own redirect included - `tee`, `sed -i`, `cp`, `mv`, `dd`, `rm`, `touch`, `truncate`, `ln`, `install`, `rsync`, `git rm`/`git mv`/`git checkout <ref> -- <path>`/`git restore`; `patch`, `git apply` and the whole-tree git rewriters fail closed as unnameable) whose target resolves inside the active run's repo [R-GUARD-BASH-WRITE-BOUNDARY] | the structured write boundary (`is_role` / `hits_repo` / `boundary.check_path`) covers Edit/Write/MultiEdit/NotebookEdit only, and both workers hold Bash, so `hooks/ppl_guard.py:bash_write_boundary_deny` extracts the target out of every such construct and runs it through the same per-role decision `check_path` makes | active run, any writing role or an unroled session - fires whenever the resolved target lands inside the repo, whatever the session's cwd, mirroring R-UNROLED-REPO-WRITE's own "the target path is the honest question" |

**How the `ppl`-CLI row reads a command.** Every occurrence of the word `ppl` in the command
is scanned, not only the first, and the command denies if ANY of them does not parse into a
known read-only pair - an unparseable occurrence denies rather than defaults to allow, because
a false allow here is a role opening its own gate [R-GUARD-PPL-CLI-DENIED]. A variable (`P=ppl;
$P transition done`), a `bash -c` wrapper or a pipe into `sh` keeps the literal word `ppl`, so
the same scan denies it [R-GUARD-PPL-CLI-DENIED]. Invoking the module directly (`python3 -m
ppl` / `python3 -m ppl.cli` / a bare `ppl.cli` token) is matched exactly like the word `ppl` -
same allowlist, same fail-closed default - because it is a realistic accident (a worker whose
`PATH` lacks `ppl` reaches for `python3 -m` with no intent to evade anything), not because it
closes the class. Quote-splitting (`p''pl`, and by the same reasoning base64 or any other
shell-quoting or encoding trick) is left uncaught on purpose: a lexical matcher cannot win that
arms race, and chasing one instance would buy a longer regex and a false sense of completeness,
not a closed class [R-GUARD-PPL-CLI-DENIED].

**Bash write boundary.** `hooks/ppl_guard.py:bash_write_boundary_deny` extracts the target out
of every write-shaped Bash construct `BASH_WRITE_SHAPES` knows how to spot - a redirect (`>`,
`>>`, `>|`, `&>`, `&>>`, the csh-style `>&`, found outside quotes on the raw segment text so a
word-glued form like `echo x>f` is a target and a quoted `>` or a `>(` process substitution is
not, fd dup forms like `2>&1` excluded), `tee`,
`sed -i`, `cp`, `mv`, `dd`'s `of=`, `rm`, `touch`, `truncate`, `ln`, `install`, `rsync`, and
`git rm`/`git mv`/`git checkout <ref> -- <path>`/`git restore` - and runs it through the same
per-role decision the structured tools get, for a role and for an unroled session alike. It
tracks a `cd` across `;`/`&&`/`||`/newline-separated segments, a bare `&` included, whose list
runs in a subshell and gives the cwd back, and across a pipe too, although a pipe stage's cd
does not move the shell (see R-RUNS-BASH-WRITE's residual), so a relative target resolves
against the command's own effective directory, not only the session's launch cwd. Every
occurrence is scanned, not the
first [R-GUARD-BASH-WRITE-BOUNDARY]. `ln [-s] [-f] target linkname`, `install ... source dest`
and `rsync ... source dest` write their LAST positional argument, exactly like `cp`/`mv` -
checking the first (the symlink's target, or the source being copied) would be a false deny
that reads as coverage while denying the wrong path [R-GUARD-BASH-WRITE-BOUNDARY]. The write
shapes are proven against the code worker specifically: the test worker's read allowlist
rejects any shape it does not recognise, so a missing write shape would stay hidden behind it
for that role and show only for the role without that extra layer [R-TEST-WORKER-BASH-READS]
[R-GUARD-BASH-WRITE-BOUNDARY].

Unlike the structured-path branch above, landing outside the repo is not itself a violation
here: a role's Bash legitimately writes build logs and rc files outside the repo (the gates
recipe chains `<build> > /tmp/x.log 2>&1; echo $? > /tmp/x.rc`), so this only ever fires for a
target that resolves inside the active run's repo - the same "the target path is the honest
question, not the cwd" principle this section already states for R-UNROLED-REPO-WRITE
[R-GUARD-BASH-WRITE-BOUNDARY]. Fail closed on anything this cannot pin down to a concrete path:
a command substitution anywhere in the command, an ambiguous `sed -i` (GNU's optional attached
suffix and BSD's mandatory separate one cannot be told apart lexically), a
`tee`/`cp`/`mv`/`rm`/`touch`/`truncate`/`ln`/`install`/redirect missing its argument, a
single-argument `ln` (linkname implied by cwd and basename rather than named), a relative path
after a `cd` this cannot resolve (`cd`, `pushd` and `popd` are tracked - `popd` on an empty stack
keeps the cwd - a `(` subshell's cd ends at its `)` read outside quotes, and `env -C` moves only
the command env runs, never the shell's redirect, which the shell opens in the pre-cd cwd, and
`env`, `command`, `builtin`, `exec`, `nohup`, `nice` and `time` in front of a verb are taken
off, nested ones too, each read by its basename when spelled by a path, while `sudo` and `xargs`
are not), and `patch`, `git apply` and the whole-tree rewriters
(`merge`, `rebase`, `cherry-pick`, `pull`, `revert`, `clean`), whose paths come from a diff or
a ref rather than the command line [R-GUARD-BASH-WRITE-BOUNDARY]. That fail-closed deny only
fires when the command is otherwise pipeline-scoped - a `ppl-*` role, or a session whose cwd
sits inside the run's repo - so an unrelated session running an ambiguous `sed -i` somewhere
else on the machine is not denied merely because some other run happens to be active
[R-GUARD-BASH-WRITE-BOUNDARY]. The unnameable shapes matter more than a redirect variant
usually would: the done gate exempts test paths from its boundary check because the test
worker legitimately writes them, so nothing after the hook catches a code worker that reached a
test source through a shape the hook did not know.

What this still does not close, stated plainly rather than left for the worker to assume from
"a hook denies anything else": an interpreter invocation - `python3 -c
"open('<path>','w').write(...)"`, and the same in any other language a role's Bash can reach -
writes the same path with no redirect, `tee`, `cp`, or any other lexical shape this module
looks for, and is not caught [R-GUARD-BASH-WRITE-BOUNDARY]. This is the same class of gap as
`p''pl` quote-splitting and the test worker's execution channel: a lexical scan of a shell
string cannot close an arbitrary-code channel, and chasing one shape (`python3 -c`) would buy a
longer regex and a false sense of completeness, not a closed class, when `perl -e`, `ruby -e`,
`node -e` and an unbounded number of others reach the identical result
[R-GUARD-BASH-WRITE-BOUNDARY]. Both worker templates and R-RUNS-WRITE-PROTECTION say this rather
than claim a coverage this module does not have [R-GUARD-BASH-WRITE-BOUNDARY]
[R-RUNS-WRITE-PROTECTION].

**What this actually buys**, stated once rather than left to be inferred: this cage catches
shortcuts, not a determined role. Every lexical check in `ppl_guard.py` is a speed bump against
an agent taking the easy path, not a proof no path exists. The lease is a second fence, and a
narrower one: `require_current_generation` (6.9) refuses a write that cannot produce the run's
current generation [R-LEASE-STALE-WRITE], and it guards exactly `ppl brief challenge`, `ppl
evidence add`, `ppl verdict`, `ppl findings add|status|delete`, `ppl review answered` and `ppl
apply-patch`. The other state-changing commands, `ppl transition done` and `ppl stage enter`
among them, take no generation, so against a role driving them the `ppl`-CLI row above is the
only fence, and it is lexical [R-GUARD-PPL-CLI-DENIED].

Post-check as second layer: `ppl transition done` runs `git diff --name-only
<base_sha>..HEAD` and refuses if the diff escapes the boundary (6.4 rule 4, with the test-
path exemption stated there) [R-DONE-DIFF-BOUNDARY]. That is the only place the post-check runs in v1 - the earlier
stage transitions do not diff - so during a stage the hook is what catches the modelled
write shapes (the lexical checks above, with the same residual "what this actually buys"
already discloses: an interpreter invocation is not one of them) [R-GUARD-BASH-WRITE-BOUNDARY],
and before the run is closed the terminal gate is what checks the diff against the boundary
on disk [R-DONE-DIFF-BOUNDARY] - itself one of the "six of the seven done conditions read
documents the same session wrote" R-DONE-GATE's own residual admits, so this too is a check
against recorded state, not an independent proof.

**Test-worker read isolation.** The test worker holds the same tools as the code worker, Bash
included, and nothing above constrains a *read*. Two denials close the deliberate and casual
half of that gap, and they are built to two different standards, stated once here rather than
left for a reader to assume:

`Read`, `Grep` and `Glob` arrive as structured tool calls carrying a path argument (or, for
Grep/Glob, none at all - the tool then searches the session's cwd). That half is exact: a
field is read off the payload, not guessed at from text, and an omitted path resolves to the
cwd rather than being treated as "unscoped, therefore allowed" - a search with no resolvable
root is unclassifiable, and unclassifiable denies here exactly as it does everywhere else in
this section [R-TEST-WORKER-READ-TOOLS].

`Bash` is one opaque string, so this half is lexical - and it is an ALLOWLIST, not a
blocklist, because the requirement is that any command shape that cannot be classified is
denied [R-TEST-WORKER-BASH-READS]. A blocklist cannot satisfy that requirement, because an unrecognised command is by
construction unclassified and therefore allowed under a blocklist [R-TEST-WORKER-BASH-READS] -
and naming more verbs would not fix it, since `python3 -c "print(open('x').read())"` alone
subsumes every reader anyone could name, and so does `perl -e`. A blocklist misses every reader
nobody told it about (`nl`, `tac`, `cut`, `od`, `xxd`, `strings`, `more`, `diff`, `git log -p`,
`git blame`, `git cat-file`, a `while read ... < file` redirect) without any new trick.

The allowlist: the whole command is split into segments (`;`, `&&`, `||`, `|`, newline), and
every segment must match one of a small set of shapes this role is derived to need, or the
whole command denies [R-TEST-WORKER-BASH-READS]. The shapes: the build shapes the run's harness
snapshot declares, matched token by token with flags and goals in whichever order the recipe
puts them [R-GUARD-SHAPES-FROM-SNAPSHOT]; the `echo` the recipe's own `; echo $? > <file>`
exit-code idiom depends on (echo never reads a file itself, so any invocation of it is
allowed, not only that exact shape); `git status`, `git add` of test paths only, and `git
commit`; `ls`; and reading a test source with `cat`/`head`/`tail`/`less`/`grep`/`rg`/`awk`/
`sed` [R-TEST-WORKER-BASH-READS].

A read verb with no path operand is not assumed to be filtering a pipe. `rg` is recursive by
default with no path operand at all, and `grep -r`/`-R`/`--recursive` (bundled or not - `-rn`
counts) defaults to the cwd, and neither reads stdin, piped or not. So the classifier first
asks whether the verb is a tree-read regardless of an operand - `rg` always is, `grep` is
whenever a recursive flag is present - and only answers "yes, it is filtering a pipe" for the
remaining verbs when a real `|` fed the segment, never merely because the path list came up
empty [R-TEST-WORKER-BASH-READS].

`git diff` and
`git show` are not in the allowlist at all, not even restricted to a test pathspec - the role
has no documented need for either, and keeping a restricted form was still more surface than
derived. A command substitution (`$(`, a backtick) anywhere in the raw string denies the
whole command before any segment is even classified: it is the one construct that could
smuggle an unrelated read inside an otherwise-allowed shape's own argument (a module name, a
commit message, a file argument) [R-TEST-WORKER-BASH-READS].

Conservative deny is the deliberate direction throughout: a false deny costs one clarifying
round, a false allow silently hands the test worker the one thing its independence depends on
not seeing. What this still does not chase, named rather than quietly patched over (the same
call as for `p''pl` quote-splitting): `bash -c '...'` denies via a leftover
unbalanced quote making the tokenizer raise on the wrapped segment, not by recognising the
wrapper - a false deny in the accepted direction, since it also denies a `bash -c` wrapping an
otherwise-allowed read of a real test source [R-TEST-WORKER-BASH-READS]. A shell input
redirect (`< file`) has no
dedicated handling and happens to deny in every shape checked, which is incidental to the
classifier rather than a designed protection [R-TEST-WORKER-BASH-READS].

Two channels stay open on purpose. First: `ppl`
assembles no dispatch payload on the v1 subagent path - the orchestrator composes the prompt
itself - so nothing stops the orchestrator pasting the implementation diff into that prompt's
literal text; the two denials above reach only the worker's own tool calls, not what it is
handed [R-TEST-WORKER-NO-DIFF n/a]. Second, and this is the one that cannot be closed while
the worker still has to run its own tests, a compiler error names the offending method, a
stack trace names classes, a failing assertion prints the actual value, and the build's own
output carries all of that back through the same Bash call the worker used to invoke it - no
hook filters a tool result after the fact [R-TEST-WORKER-EXECUTION-CHANNEL n/a]. What this
delivers is narrower than sealed isolation: it removes deliberate and casual lookup, and
leaves that execution-shaped channel open, because closing it is not possible without
stopping the worker from verifying its own tests at all [R-TEST-WORKER-EXECUTION-CHANNEL n/a].

**The code-worker mirror, decided.** `ppl-code-worker` is not given the matching read
restriction. Doctrine's "the implementation worker does not touch tests" is about *writes* -
one agent writing both the code and the test that checks it is the profanation the two-worker
split exists to prevent. A worker reading the tests it must satisfy while it implements is
ordinary TDD, not that failure mode: the property under protection is the test's independence
from the implementation, not the reverse, and the code worker already sees the tests' content
regardless the moment it runs them, so denying its reads would buy nothing structural
[R-CODE-WORKER-READS-UNRESTRICTED n/a].

Worktree isolation is used for change isolation, not as a security sandbox. Stated
explicitly so nobody mistakes convenience for protection.

**Per-lane resources (precondition for waves).** A worktree separates files and nothing
else. It gives each lane its own working tree and its own `git status`, and that is the
whole of what it gives: not a test database, not a port, not a temp directory, not a queue,
not object storage. A typical project's local development environment makes the shared
surface concrete: one database, one object store, one message broker, a fixed set of ports,
all shared across every worktree on the machine, and integration tests that use
Testcontainers bind host Docker resources the same way, against the same shared daemon. Two
lanes of this pipeline running integration tests at the same time, in two worktrees, would
collide on all of the above: the same schema, the same bucket names, the same broker, the
same bound ports. No code in v1 addresses this, because v1 is one sequential lane (section 2)
and there is nothing for it to address yet. It is written here as a precondition, not a
feature: parallel waves (section 2, out of scope for v1) must not be turned on until per-lane
resource separation exists for whichever of these a gate recipe actually touches, and nothing
in this codebase enforces that precondition today [R-LANE-RESOURCE-ISOLATION n/a].

**The `isolated` flag.** Two of the checks above - the tracker-write gate (9.2) and the
operator pause (9.2) - additionally fire for the unroled main session (the orchestrator,
which carries no `agent_type`) whenever the active run's `isolated` field is true, regardless
of cwd. `ppl init` writes this field; it defaults to true and can be turned off with
`--no-isolated`. This is not something the hook can verify against reality - it is the
operator's assertion that the guard hook is registered only in this run's own launch
directory, so it does not fire in an unrelated session at all. Under that assumption,
broadening these two checks to "this run is active" is safe: the documented workflow (6.3,
Stage 1) creates the worktree beside the orchestrator's session rather than under it, so a
cwd-only check does not catch that session at all, which is why both checks would be inert
for it without this flag. `--no-isolated` restores the plain cwd check instead, for an
operator who registered the hook more broadly (e.g. in global settings) and knows scoping by
"run active" would reach their other projects.

**Checked, not just trusted.** `ppl` cannot see how the guard hook is registered
from inside `decide()` - that lives in a `.claude/settings*.json` file, not in run state -
but it can be checked separately, on disk, before a run even starts
[R-INIT-ISOLATION-CHECK]. `ppl doctor` and `ppl
init --isolated` (the default) both call `doctor.inspect()`, which reads this directory's
`.claude/settings.local.json` and `~/.claude/settings.json` for the guard hook's command and
reports `"local"`, `"global"`, `"both"`, `"none"`, or `"unknown"` (a settings file exists but
could not be read or parsed). The two files answer different halves of what
`isolated=true` asserts - "the hook fires here" (needs a confirmed local yes) and "the hook
does not fire elsewhere" (needs a confirmed global no) - so a confirmed local yes does not
paper over an unreadable global file: that combination is `"unknown"`, not `"local"`,
because the half isolation actually depends on was not confirmed. `ppl init --isolated`
refuses with `SCHEMA_INVALID` on three combinations that are silently wrong
[R-INIT-ISOLATION-CHECK]:

- `"global"` - registered only globally. The tracker gate and the pause would reach every
  session on the machine while `isolated=true` asserts the opposite.
- `"both"` - registered locally *and* globally. The local registration is real, but the
  global one still fires everywhere else, which is the same violation as `"global"` alone; a
  deliberate migration in progress (global first, local added afterward) looks identical to
  this on disk, and is refused the same way [R-INIT-ISOLATION-CHECK].
- `"unknown"` - the registration could not be confirmed at all, most often because the global
  side could not be read or parsed even though the local side is a confirmed yes. Treating
  that as "fine" would be the exact bug this check exists to remove.

`--no-isolated` is not refused by any of these, and neither is `"local"` or `"none"` - "none"
is the openly documented unregistered state (README.md "Hooks"), not a mismatch between a flag
and reality, and refusing it would block using `ppl` before the hook exists at all
[R-INIT-ISOLATION-CHECK] [R-GUARD-NOT-ARMED n/a].

**Residual, still not fixed.** The check is a file inspection, not a proof that the *session
running this command* was itself launched from a directory whose `.claude/settings.local.json`
carries the hook - an operator could register it locally in `~/ppl-workspace` and then run
`ppl init --isolated` from a shell in that same directory while a *different* Claude Code
session (launched elsewhere) is the one actually driving the pipeline; `ppl doctor` would
report `"local"` and see no mismatch, and the isolated assumption would still be false for
that other session. It is also a point-in-time check: nothing re-verifies registration for
the lifetime of the run if the operator edits `~/.claude/settings.json` afterward.

Two more blind spots: `PPL_CLAUDE_HOME` fully substitutes for `~/.claude` when set in the
environment, which is a deliberate test seam (the suite uses it to stay out of the real
settings) - but it means a stray value of that variable in a real invocation makes `ppl
doctor` and `ppl init --isolated` inspect an unrelated, likely-empty directory and report
`"none"`, seeing no mismatch that may in fact exist. And `doctor._has_guard` matches the
literal substring `ppl_guard.py` in a hook's `command`, so a different checkout's guard hook,
at a different path on the same machine, reads as this one's.

All four gaps above are known limitations, each recorded as a matrix row
[R-ISOLATION-SESSION-IDENTITY n/a] [R-ISOLATION-POINT-IN-TIME n/a]
[R-ISOLATION-CLAUDE-HOME n/a] [R-ISOLATION-GUARD-BASENAME n/a]. What is checked, not merely
trusted: the hook being registered only globally (`"global"`), registered both locally and
globally (`"both"`), and the registration being unreadable or unparseable at all (`"unknown"`,
including a confirmed local yes next to an unreadable global file - see above) - all three
refuse `ppl init --isolated` with `SCHEMA_INVALID`, and all are reported by `ppl doctor`
together with the paths and, for `"unknown"`, the parse or read error [R-INIT-ISOLATION-CHECK].
The write-boundary checks earlier in this section do not participate in the `isolated`
broadening at all, checked or not: an unroled session's ordinary file write outside any run's
repo is untouched whether or not the run is isolated.

### 5.5 The rule matrix

A document that claims enforcement drifts from the code silently: prose and code move apart
and nothing mechanical notices, so a claim that the dispatch cap holds at stage boundaries, or
that `ppl apply-patch` validates every worker patch, reads true long after it stopped being
true. `rules.toml` and `tests/test_rule_matrix.py` exist to make that drift fail the suite. The
matrix is deliberately not a document - a document would drift the same way.

`rules.toml` carries one row per rule: its `statement`, what `scope` it applies to, the
`event` that fires it, whether `enforcement` is `blocking`, `soft` or `n/a`, the
`hook_or_check` that performs it, a `deny_test` proving the refusal, an `allow_test` proving
the legitimate case still passes, an `owner`, and the `residual`. `ppl/rules.py` loads and
validates it. The test asserts, and the assertions are the point: a `blocking` row must name
a deny test that exists, whose body carries the rule id, and whose body actually asserts a
refusal - a rule id in a comment proves nothing; an `n/a` row must carry a residual; every
claim sentence in the scanned documents must name a row; every marker, in a claim sentence or
not, must resolve to a row; a citation of an `n/a` row must disclose it in the marker; a
`blocking` or `soft` row must name a check that is really in the tree; and every entry in the
guard's three deny tables must map to a `blocking` row - the single `BASH_WRITE_SHAPES` regex
is checked to exist, not swept [R-DOC-RULE-IDS]
[R-MATRIX-GUARD-SELF-DESCRIBING]. A row pointing at a test that does not exist, or at a test
that does not know which rule it proves, is the same family of lie as a document claiming a
check nothing performs. An `allow_test` is only checked to exist - it is not required to name
its rule.

Three documents are scanned: this one, `SKILL.md` (rendered from the `skill/` skeletons for
the `example-maven` harness, forge and harness fragments included, and the one that matters
most, since the orchestrator reads it at runtime), and `README.md`.

A claim sentence is one containing any of these words, matched case-insensitively:

```
enforced  enforces  denies  denied  refuses  refused  rejected  cannot  impossible  must not
may not  may only  only ... may  never  prevents  stops  binds  ensures  guarantee
```

Such a sentence names its row with a trailing marker placed inside the sentence, before its
final period, and a citation of a row that enforces nothing says so in the marker itself
[R-DOC-RULE-IDS]:

```
... is denied `BOUNDARY_VIOLATION` [R-UNROLED-REPO-WRITE].
... the orchestrator's dispatch prompt is not checked [R-TEST-WORKER-NO-DIFF n/a].
```

The marker was chosen over matching the row's `statement` text, which would break on any
rewording, and over a separate index section, which is a second document free to drift from
the first. The ` n/a` disclosure exists because a marker alone made a claim look covered
while citing a row that admits the thing was not built, and because it puts a later
downgrade in the diff of the document, not only in the diff of the table. A table row is one
unit and carries its id in the first column; a table header names columns and claims nothing.

Three enforcement values, and the distance between them is the whole point. `blocking` means
code refuses the action [R-DOC-RULE-IDS]. `soft` means code notices, warns or filters and the
action still goes through. `n/a` means nothing here enforces it - and an `n/a` row must say
which kind it is, because "there is nothing to build here" (`PROPERTY:`) and "this is needed
and was never built" (`GAP:`) read identically otherwise, which is exactly how a real hole
passes for a normal exception [R-DOC-RULE-IDS].

What the matrix does not catch, stated plainly so nobody reads more into a green suite than
is in it:

- The claim-word list is finite. A claim phrased without any of those words - "`ppl
  apply-patch` validates every patch", say - is seen by nothing: the sentence scan only
  looks for those words, and the check-exists assertion iterates rows, not sentences, so it
  covers that class only for a claim someone already wrote a row for
  [R-DOC-KEYWORD-FREE-CLAIM n/a].
- A marker proves a row exists, not that it is the right row. A claim citing a plausible but
  wrong row passes.
- The table is seeded from the scanned documents and the design's rule families, not
  from a sweep of the code, so a new refusal added to `ppl` with no row is noticed by nothing
  [R-MATRIX-SCOPE n/a].
- The opposite motion - relabelling a rule the code still enforces as `n/a`, inventing a
  residual for it and correcting the counts below - is caught for the guard's three deny
  tables and nowhere else [R-MATRIX-DISOWNED-RULE n/a].
- A deny test is checked for a refusal *shape*, and that is a heuristic over the text of a
  test, not a proof that the test exercises its rule. The rule id may be named in a comment,
  because saying which rule a test enforces is prose; the refusal is looked for in the code
  only, with comments and docstrings stripped, because prose cannot prove one
  [R-DOC-RULE-IDS]. That split is
  load-bearing - without it, a body whose comment line happens to contain "unknown" passes
  the check while asserting nothing - but even with it, this

  ```
  # rule: R-GIT-ADD-ALL
  self.assertIsNotNone(decide(payload(None, "Bash", {"command": "ls"})))
  ```

  asserts a refusal of something, and nothing checks it is the refusal the row describes
  [R-DOC-RULE-IDS].

As of this commit the table carries 153 rows: 111 `blocking`, 16 `soft` and 26 `n/a`. Of the
`n/a` rows, 20 are `GAP:` - a rule this design names and no code performs - and 6 are
`PROPERTY:`. The gaps are the honest output of the matrix: the guard hook not being
registered at all (README.md "Hooks"), the orchestrator's dispatch prompt for the test worker
being unchecked (the hook closes the worker's own tool-call reads, not what it is handed), the
stage 6 preconditions being unchecked before the push, the four isolation-check blind spots of
5.4, the harness gate recipes of section 11, the three blind spots the matrix has as GAP rows
of its own, a foreign finding's owner not actually being notified (6.6a), per-lane resources
(test databases, ports, temp directories, queues, object storage) having no isolation at all
beyond the worktree's own files (5.4), and seven rules only prose or a convention holds: the
content of a tracker write, the panel's own state writes, write/forbid disjointness at `ppl
boundary set`, the USD spend coming before a dispatch, a per-role count of format violations,
the briefer returning `SPEC_GAP` instead of inventing, and the keys inside a provider's
`params`. These counts are read back out of `rules.toml` by the suite, so this
paragraph cannot drift from the table it describes [R-DOC-RULE-IDS].

### 5.6 Harness

A harness is everything project-specific, kept outside the project repository in an installed
directory `~/.ppl/harnesses/<name>/`: `harness.toml` (repo path and default branch, task key
pattern and URL template, `[tracker]`, `[forge]`, `[knowledge]`, `[env]`, the `[[gate]]` build
recipes and the `[[guard.shape]]` command shapes), `skill/` fragments and `templates/` role
overlays. Two examples ship in `defaults/harnesses/`: `example-maven` (GitLab, Maven gates) and
`example-generic` (GitHub, make gates). `ppl harness add` installs one from a directory or a
git URL, `ppl harness validate` checks it.

`ppl harness apply <name>` renders the agent files and the orchestrator skill for that harness
and records it as the active one. The skill is rendered from the `skill/` skeletons plus the
forge and harness fragments, carries a generated notice, and the render refuses while any
include marker is unresolved [R-SKILL-GENERATED]. The harness fragments are `skill/gates.md`,
`skill/extra-gates.md`, `skill/baseline.md` and `skill/tracker-close.md`, the last one the
note the orchestrator writes to the tracker after the merge (default: a short comment saying
what was done and how to check it). A fragment's `<default-branch>` is replaced with
`repo.default_branch` at render time, and `<worktree>`, `<TASK-ID>`, `<SHA>` and
`<GENERATION>` stay as written for the orchestrator to fill in at run time. `ppl init` refuses `SETUP_INCOMPLETE` while
`ppl setup` has not recorded the models step (waived by `--skip-model-check`), the harness step
and the apply step as done, or when no `--harness` is passed [R-INIT-SETUP-COMPLETE]. A
harness added and applied by hand does not count as done, and the refusal names the `ppl
setup` step to run. It refuses `HARNESS_NOT_APPLIED` unless the harness it loads is
the active one the agents and skill were rendered for [R-INIT-HARNESS-APPLIED], and refuses a
task id that does not match the harness's `task.key_pattern` [R-INIT-TASK-KEY-PATTERN].

`ppl init --harness` freezes the slice the cage enforces - `[task]`, `[tracker]`, `[forge]` and
the `[[guard.shape]]` rows - into `runs/<task>/harness.json`, and the hook reads that snapshot,
never the live harness, so an edit to the harness mid-run cannot widen what the cage allows
[R-GUARD-SHAPES-FROM-SNAPSHOT]. A test worker's build command must match one of the snapshot's
shapes token by token - argv0 exactly, every required flag with its value regex, exactly one
declared goal when `one_goal_of` is non-empty, any other token against `free_args` - or it is denied
[R-GUARD-SHAPES-FROM-SNAPSHOT]. A missing or unreadable snapshot yields zero shapes, so every
build command is denied rather than allowed by default [R-GUARD-SNAPSHOT-FAIL-CLOSED]. The
denied forge tools are the union of both adapters' defaults plus the snapshot adapter's names
under its configured server name, so a renamed server never un-denies a default name
[R-FORGE-DENIALS-FROM-ADAPTER]. `gh pr merge`, `gh pr review --approve` and a `gh api graphql`
call resolving a review thread are denied as Bash for every session, whatever forge the run
names [R-FORGE-GH-NO-APPROVE-MERGE]. `ppl-review-responder` holds Bash only under the GitHub
forge, as an allowlist of the adapter's read shapes, and under GitLab every Bash call it makes
is denied [R-REVIEW-RESPONDER-BASH-SHAPES].

The hook's own failure modes are decided the same way. When `decide()` raises, the call is
denied for any role in `boundary.ROLE_RULES` (all six generated roles) and allowed for an
unroled session, so a hook defect cannot lock
the operator out of unrelated work [R-GUARD-INTERNAL-ERROR-DENIES-ROLES]. A role's Bash command
longer than 4096 characters is denied before any pattern runs, so a padded command cannot
outlast the hook's timeout [R-GUARD-ROLE-COMMAND-LENGTH]. Both hooks need Python 3.11, and
`ppl setup claude` writes the interpreter that ran it into both hook entries. Under an older
interpreter each hook reads the payload before importing anything from `ppl`, denies a call
whose `agent_type` starts with `ppl-`, writes one stderr line naming the version it found and
exits 0, where it used to die on import and leave every call allowed [R-GUARD-PYTHON-FLOOR].
`ppl doctor` runs every registered guard command once on an unroled Read of `/dev/null` and
reports `hook_runs: false`, with the first stderr line and the remedy `ppl setup claude`, when
one does not exit 0 with an empty stderr.

## 6. State machine

### 6.1 Vocabulary

```
task lifecycle:  planned -> ready -> in_progress -> done
                                  \-> blocked
                                  \-> failed

run stages:      admission -> baseline -> briefing -> implementation -> tests
                 -> gates -> verification -> mr -> review -> closure
                 closure -> merge_watch (optional tail, see 8)
                 any -> parking
                 any -> escalation
```

The task reaches `done` when the MR is approved on the current head SHA - that is the
owner-defined end of the pipeline. `merge_watch` is an optional low-frequency tail that
exists only to observe the human merge and fire the post-merge tracker update. It cannot
change the lifecycle [R-MERGE-WATCH-TERMINAL].

Task lifecycle is the only business truth. The run stage explains where the orchestrator
stopped and does not become a second registry.

### 6.2 Transition ownership

- orchestrator selects the task and takes the lease
- roles produce artifacts; no role may declare its own work done
- the verifier produces a verdict but does not close the task
- `ppl` binds the verdict to the candidate SHA [R-DONE-SHA-BINDING] and performs the terminal transition
- recovery reconciles facts but makes no lifecycle decision on its own
- resuming from parking DOES restore the lifecycle status it needs, though: `ppl stage
  enter <resume_stage>` moves `status` from `blocked` back to `in_progress` when the run
  is leaving the "parking" stage, so a parked-then-resumed run can still reach `done`
  [R-PARK-RESUME-RESTORES-LIFECYCLE]. Without it, `_LIFECYCLE_EDGES["blocked"]` (6.1) has
  no edge to `done` at all, and nothing else in the documented flow ever moves status back
  to `in_progress` - a run parked for any reason (a BLOCKED verdict, `ppl escalate`, a
  watchdog `TIMED_OUT`, an operator stop) stayed stuck at `blocked` forever. A run parked
  before it ever reached `in_progress` (still `planned`) is untouched by this - it takes
  the ordinary planned -> ready -> in_progress path baseline entry already handles.
- `_FORWARD["parking"]` and `_FORWARD["escalation"]` (6.1) are `set(STAGES) - {merge_watch}`
  for entering either stage from anywhere, on purpose - but a run LEAVING parking or
  escalation may only enter the one stage it was actually in when it parked or escalated,
  never any other real stage [R-PARK-RESUME-EXACT]. `ppl/fsm.py` stays a pure stage graph
  unaware of any one run's history and still accepts every real stage as a target from
  either one - `ppl/run.py:stage_enter` is the layer that narrows it, reading `resume_stage`
  from `parking.json` (or, for escalation, the `escalation_resume_stage` field `stage_enter`
  itself stamps on the way in) the same way `control.enforce` layers the pause gate on top
  of `fsm.check_stage` rather than folding it into the graph. A missing or untrustworthy
  record refuses every target rather than silently allowing one [R-PARK-RESUME-EXACT] -
  a parked run does not get to skip implementation, tests and
  verification just because something wrote to disk incorrectly.

The lease is a counter, not a lock file, for the same reason 5.4 declines a real lock on the
guard-hook registration check: a stale lock surviving a crashed hook is worse than an
imprecise counter. `ppl stage enter` mints a fresh `attempt_id` and reports the run's current
`lease_generation`; a worker captures that generation and must hand it back, unchanged, as
`--generation` on every `ppl evidence add`, `ppl verdict record`, `ppl review answered` and
`ppl apply-patch` call. `ppl recover` bumps the generation, and so does the watchdog (6.9)
the moment it parks a run for `TIMED_OUT` - nothing else does - so a worker that timed out,
was replaced, and only then comes back to write is refused with `ILLEGAL_TRANSITION`
[R-LEASE-STALE-WRITE]: it is still holding the generation `ppl recover` or the watchdog
left behind. A worker that instead re-reads its generation from `ppl show --json` after that
call would read the new value and pass - the fence only catches an attempt that
writes with the value it captured earlier, which is the design's premise (a returning
timed-out worker still carries what it had), not a gap in it.

### 6.3 `ppl` CLI surface

```
ppl setup [<step> ...]           # the onboarding wizard: paths, claude, models, harness, forge,
                                 # apply - every step skippable and re-runnable
ppl harness validate|list|show|add|apply [<name>] [--source <directory or git url>]
                                 # apply renders the agent files and the skill, sets the active one
ppl models verify|status         # probe the tier models, show what is verified and when
ppl init <TASK_ID> --repo <worktree-path> --tracker-url <url> --harness <name> [--isolated | --no-isolated]
         [--risk low|normal|high] [--skip-model-check]
                                 # isolated defaults true - see 5.4 for what it gates. Refuses
                                 # SETUP_INCOMPLETE until ppl setup finished models, harness and
                                 # apply, and without --harness [R-INIT-SETUP-COMPLETE], and
                                 # MODELS_UNVERIFIED while a routed model is unverified or stale,
                                 # unless --skip-model-check [R-INIT-MODELS-VERIFIED]
ppl status                       # founder status card, human language
ppl show --json                  # full state, machine readable
ppl stage enter <stage>          # validates legal transition and budget
ppl brief set <file>             # schema-validated, binds spec_revision
ppl boundary set <file>          # write/forbid lists; write must be non-empty and must not
                                 # contain "**" [R-BOUNDARY-SET-NO-WILDCARD]. Disjointness
                                 # with forbid is NOT enforced here - see R-BOUNDARY-SET-DISJOINT
ppl baseline record <file>       # base_sha + known failures
ppl evidence add --check <name> --result pass|fail [--sha <sha>] --log <path> --rc <file>
                                  --generation <n>
                                 # --sha defaults to git rev-parse HEAD in the run's repo;
                                 # --generation is the lease generation the caller captured
                                 # from `ppl stage enter` or `ppl recover` (6.2)
ppl verdict record <file> --generation <n>  # must carry candidate_sha and acceptance map
ppl transition <done|blocked|failed>
ppl park --blocker <CODE> --safe-next "<action>" [--unsafe-to-repeat "<action>"]
ppl escalate --code <CODE> --question "<text>"
ppl budget spend <dispatch|retry|usd>
ppl approval check               # compares approved SHA against current head
ppl recover                      # recovery-first reconciliation
ppl metrics                      # fleet-wide rates and durations, sliced by producer (6.11)
ppl label --sha <sha> --actual defective|correct
                                 # records human ground truth for a past candidate SHA (6.11);
                                 # takes no --generation and does not refresh the deadline

ppl mr set --iid <n> --url <url> [--author <username>]   # records the MR, sticky author
ppl mr state                     # none|current|stale, compares approved SHA against head
ppl review pending --threads <file> --sha <sha> [--author <name>]
                                  # the review loop's dispatch filter: drops resolved
                                  # threads, threads by the pipeline's own author, and
                                  # threads the ledger already answered on this exact
                                  # last_note_id (6.6, section 8)
ppl review answered --thread <id> --sha <sha> --classification <c> --generation <n>
                                  [--last-note-id <id>]
                                  # records a reply in the ledger (threads.json)
ppl review approved --sha <sha>  # records approval on a SHA

ppl serve [--port 8787]          # admin panel, loopback only
ppl control set <pause|resume|stop|destroy> [--scope local|full]
ppl control get                  # what the panel has queued, if anything
ppl destroy --scope <local|full> # local: worktree removed and run archived immediately.
                                  # full: remote_cleanup.json queued for the orchestrator,
                                  # local archive happens only after that half completes (9.3)

ppl routing apply [--harness <name>]
                                 # regenerate the agent files from routing.toml and the harness,
                                 # the active one by default
ppl routing show                 # effective role -> provider/model/params
ppl dispatch --role <r> --in <f> --out <f>    # run a role through its provider adapter
ppl apply-patch --role <r> --diff <f> --generation <n>  # boundary-validated patch application

ppl doctor [--task <TASK-ID>]    # reports where the guard hook is registered - local
                                  # (.claude/settings.local.json), global (~/.claude/
                                  # settings.json), both, none, or unknown (a settings file
                                  # exists but could not be read or parsed - including a
                                  # confirmed local yes next to an unreadable global file) -
                                  # with the inspected paths, and whether that matches the
                                  # given or active run's isolated flag (5.4). ppl init
                                  # --isolated runs the same check and refuses SCHEMA_INVALID
                                  # on three silent mismatches: registered only globally,
                                  # registered both locally and globally (the global copy
                                  # still fires everywhere), or registration unknown.
```

### 6.4 Terminal transition rule

`ppl transition done` refuses unless ALL hold [R-DONE-GATE]:

1. a verdict exists with `verdict == PASS` - REWORK, BLOCKED, OWNER_DECISION, INCONCLUSIVE,
   ERROR, CANCELLED and TIMEOUT are all refused by the same equality check as not-PASS
   (6.10) [R-DONE-VERDICT-PASS]
2. `verdict.candidate_sha == git rev-parse HEAD`
3. every acceptance ID in `brief.acceptance` has an evidence receipt on that same SHA
4. `git diff --name-only <base_sha>..HEAD` is a subset of `boundary.write`, except for
   test sources (`**/src/test/**`, `**/*Test.java`, `**/*IT.java`, `**/*Tests.java`),
   which are allowed anywhere in the tree, AND none of them matches `boundary.forbid` -
   checked first, ahead of the write-boundary and test-path exemption, and winning over
   both, the same order `ppl/boundary.py:check_path` already uses at write time. In a
   multi-module repository a test lives beside the module it covers rather than under the module being
   edited, so a write boundary tight enough to be useful for main sources would reject
   the test worker's own output. The role rules keep that exemption honest for every write
   channel this cage models: `ppl-code-worker` may not write a test source through a
   structured edit tool or a write-shaped Bash construct [R-ROLE-CODE-WORKER-TESTS], an
   unroled session may not write inside the repo at all [R-UNROLED-REPO-WRITE], and every
   other role either may not write anywhere (`ppl-briefer`, `ppl-verifier`,
   `ppl-review-responder` [R-ROLE-READONLY-NO-WRITE]) or may write test paths only
   (`ppl-test-worker` [R-ROLE-TEST-WORKER-MAIN]) - modulo the interpreter-invocation
   residual both R-ROLE-CODE-WORKER-TESTS and R-GUARD-BASH-WRITE-BOUNDARY already disclose:
   a `python3 -c` (or perl/ruby/node) write reaches the same path uncaught, so "only that
   role may write those paths" is a claim about the lexical checks this module makes, not
   a proof that no channel exists.
5. no unresolved `blocking`-severity finding is open on the `own_diff` track for this
   candidate SHA, read from the findings registry (6.6a), not from the verdict document
   itself [R-DONE-OPEN-FINDING]
6. retry budget not exhausted - `retry_used < retry_max`, i.e. blocked once `retry_used`
   reaches `retry_max`, not only once it passes it (`budget.spend` refuses any proposed
   spend past the cap, so `retry_used` can reach `retry_max` exactly through a real path
   but never exceed it) [R-DONE-RETRY-BUDGET]
7. a passing receipt for every required check on that SHA: `secret-scan` and
   `focused-tests`
8. the run was actually briefed: `spec_revision` is bound and `brief.acceptance` is
   non-empty. `ppl boundary set` is its own command and needs no brief, so a run can reach
   every other check above with nothing recorded at all - an empty acceptance list makes
   rule 3 iterate zero times and pass vacuously, and an unbound `spec_revision` used to leave
   `R-BRIEF-SPEC-REVISION-STALE`'s check unexercised entirely, accepting a verdict carrying
   any string whatsoever. Zero acceptance criteria, all proven, is not a pass; it is a task
   nobody specified, and it must not reach done [R-DONE-REQUIRES-BRIEF].

Any failure returns a stable code and the owner of the next action. The model cannot
override it by argument [R-DONE-GATE].

`ppl transition` (of which `done` is one target among several) checks
`fsm.check_lifecycle(status, target)` BEFORE running any of the above - a cheap,
always-available check ordered ahead of the done gate, which needs a real repo diff and can
fail for reasons that have nothing to do with the target being illegal. Without this
ordering, a lifecycle transition that is illegal on its face - still `planned`, say - would
surface as whatever the done gate happened to trip over first (`EXTERNAL_BLOCKED` for a
missing `base_sha`, on a run that has not recorded one) rather than the much more direct
`ILLEGAL_TRANSITION`.

Condition 4 reads `boundary.write` and `boundary.forbid` fresh
from disk on every `ppl transition done` call, and `ppl boundary set` can rewrite that same
file at any time - including after a verdict already recorded PASS. A write boundary of
`{"write": ["**"]}` matches every path in the tree and makes condition 4 unconditionally
true, the same shape as a baseline recorded late (Stage 1): rewrite the criterion the gate reads, then
run the gate. `ppl boundary set` refuses `"**"` outright now
[R-BOUNDARY-SET-NO-WILDCARD].

Refusing `"**"` alone is not enough, because condition 4's freshness cuts both ways: a
criterion the gate re-reads live can be rewritten by the same session being gated to fit a
diff that already exists, and widening `write` to newly cover a path an already-produced,
already-blocked diff escaped to is exactly that move. So `ppl boundary set` also rebinds
`spec_revision` whenever one is already bound (using the run's current `name_contract` and
`acceptance` combined with the new boundary), and any edit - widening, narrowing, or an
honest correction alike - stales an outstanding verdict the same way a name-contract or
acceptance edit does [R-BOUNDARY-SPEC-REVISION-STALE]. The convenience of a cheap narrowing is
traded away deliberately, because distinguishing a widening from a narrowing in general would
need a superset check on arbitrary globs this codebase has no way to compute, and a false
deny here (an unnecessary re-verification) is recoverable while a false allow is not. A
merely-too-wide-but-not-`"**"` glob is not judged for content here - that is
`R-BOUNDARY-SET-DISJOINT`'s residual, disclosed separately - and R-DONE-GATE's own residual
already discloses that six of the seven done conditions read documents the same session
wrote.

### 6.5 Same-SHA rule

Every receipt, CI result, verdict and approval is stored under the candidate SHA that
produced it. After a rebase, a fix commit or a conflict resolution:

- previous CI results stop counting
- previous review approvals go stale
- the acceptance-to-evidence mapping is recomputed
- a new verdict is required

This closes the classic hole of reviewing one diff and merging another.

The same discipline binds `spec_revision`, a fingerprint of every brief field the done gate
actually reads - `name_contract`, `acceptance`, and the write/forbid boundary
(`schema.spec_revision`, `ppl brief set`) [R-BRIEF-SPEC-REVISION-STALE]. The name contract is what the test worker and the
code worker are dispatched against instead of each other's output (5.3), so it is frozen
before dispatch the same way a candidate SHA is frozen once evidence starts accumulating
against it. When a re-brief changes any of the three - the `CONTEXT_INSUFFICIENT` retry
Stage 2 allows once, or an owner-directed correction - `spec_revision` is rebound to a new
value, and a verdict recorded against the old one is refused as stale, both at `ppl verdict
record` (a verdict arriving after the rebrief) and at `ppl transition done` (a verdict
already recorded PASS before the rebrief, which nothing else in the done gate would
otherwise notice) [R-BRIEF-SPEC-REVISION-STALE]. `ppl boundary set` rebinds the same value
when a spec_revision is already bound, so a boundary edit made outside a re-brief - through
that command directly - stales the same way (6.4, `R-BOUNDARY-SPEC-REVISION-STALE`). This
staleness check is a comparison
against the last binding, so it has nothing to compare an unbriefed run's verdict against
and cannot itself refuse one - that is not a carve-out, because 6.4 rule 8 refuses an
unbriefed run outright, at the only place that matters: it can never reach done regardless
of what `ppl verdict record` was willing to accept along the way
[R-DONE-REQUIRES-BRIEF].

The hash covers the acceptance list and the boundary, not `name_contract` alone, because the
done gate only ever iterates over the acceptance ids currently on record: with `acceptance =
[AC1, AC2]` and only AC1 proven, a re-brief that drops AC2 would otherwise leave
`spec_revision` untouched and the gate open. The gate proves the criteria that are currently
listed, and the list is editable by the same session being gated - a check redefined to fit
the evidence that happens to exist, the same family as an empty acceptance list, an
unpopulated findings registry and an empty baseline diff.
A re-brief that only fills in
`governing_requirements`, `purpose`, `success_outcome`, `verification` or `open_decisions`
still leaves `spec_revision` untouched, so no verdict already recorded
goes stale over a field it did not depend on - those fields are not read anywhere in
`verdict.done_blockers`. Only the verdict goes stale, not the evidence
receipts under it: `focused-tests` and `secret-scan` are bound to the candidate SHA (6.4,
6.5 above), and a rebrief changes neither the code nor the SHA it ran against, so those
receipts still stand - it is the verifier's judgment against the (now old) contract that
must be redone, and `ppl verdict record` needs a fresh verdict, not a fresh gate run.

### 6.6 Durable records

Task state (`state.json`):

```json
{
  "task_id": "DEMO-12345",
  "tracker_url": "https://tracker.example/browse/DEMO-12345",
  "status": "in_progress",
  "stage": "verification",
  "risk": "normal",
  "model_check": "verified",
  "harness": {"name": "example-maven", "path": "/home/dev/.ppl/harnesses/example-maven", "format": 1},
  "repo": "/home/dev/src/project/.worktrees/project-DEMO-12345",
  "isolated": true,
  "branch": "DEMO-12345-short-slug",
  "base_sha": "base123",
  "candidate_sha": "abc123",
  "spec_revision": "sha256:...",
  "acceptance": ["AC-1", "AC-2"],
  "attempt": 1,
  "budgets": {"dispatch_used": 9, "dispatch_norm": 21, "dispatch_hard": 34,
              "retry_used": 0, "retry_max": 3,
              "usd_used": 0.0, "usd_max": 0.0},
  "orchestrator": {"pid": 41233, "started_at": "2026-08-31T11:58:02Z"},
  "mr": {"iid": null, "url": null, "approved_sha": null},
  "last_durable_fact": "focused tests passed on abc123",
  "next_safe_action": "run module suite",
  "unsafe_to_repeat": []
}
```

`next_safe_action` is restamped from a stage-to-action table on every `ppl stage enter`
call and by `ppl transition done`, not written once at admission and left alone - a field
in a JSON contract callers are told to trust (`ppl status`, the panel, `ppl show --json`)
is a trap once it drifts out of date with the run it describes.

Brief (`brief.json`) - the compiled, one-way projection of the spec. Not a new source of
truth. Carries `governing_requirements`, `direct_specs`, `architecture_constraints`,
`allowed_write_paths`, `forbidden_paths`, `verification`, `open_decisions`, `name_contract`.
Links point at the tracker and spec page revisions, so a spec change makes the brief stale
automatically.

`name_contract` is the frozen method signatures, DTO field names, error codes, units and
boundary rules the briefer compiles before dispatch (`templates/ppl-briefer.md.body`). It
exists for one specific risk: the test worker's own reads of the implementation are
denied [R-TEST-WORKER-READ-TOOLS] [R-TEST-WORKER-BASH-READS] (5.3, modulo the execution-shaped
channel R-TEST-WORKER-EXECUTION-CHANNEL discloses), so
without a name fixed in advance, the test worker and the code worker can each choose a
different one and both be internally consistent while the two halves fail to compose.
`ppl brief set` (`schema.validate_brief`) can only prove `name_contract` is present and
structurally complete - a non-empty mapping - which proves somebody wrote something, not
that the names are the right ones or that the eventual code matches them; no static check
in this pipeline can prove that without executing both sides against each other
[R-NAME-CONTRACT-STRUCTURAL-ONLY n/a]. It reduces the drift the split risks; it does not
close it.

Verdict (`verdicts/<sha>.json`) - `task_id`, `candidate_sha`, `spec_revision`, per-check
results, acceptance-to-evidence map, `verdict`, `findings` (must be an empty list - see
6.6a; `ppl verdict record` refuses a non-empty one [R-FINDINGS-VERDICT-FIELD-RETIRED]).

Parking record (`parking.json`) - `resume_stage`, `last_durable_fact`, `blocker` code,
`owner`, `safe_next_action`, `unsafe_to_repeat`. A second `ppl park` on an already-parked
run (an operator stop landing on top of an existing park, concretely) preserves the FIRST
park's `resume_stage` rather than overwriting it with `"parking"` itself, which is what
naively re-reading the run's current stage would produce once it is already parked
[R-PARK-PRESERVES-RESUME-STAGE].

Review ledger (`threads.json`) - `{thread_id: {sha: {classification, at, last_note_id}}}`.
This is what makes `ppl review pending` (section 8) safe to call every tick without ever
posting the same reply twice: a thread counts as already answered on a given SHA once the
ledger holds a record for that `(thread_id, sha)` pair, unless the reviewer posted a further
note after the recorded `last_note_id`, in which case it is surfaced again even though the
pipeline "answered" it once already - the ledger's job is to stop the pipeline repeating
itself, not to stop a human being heard. `record_reply` (`ppl review answered`) is the only
writer.

### 6.6a Findings registry

A finding inline in the verdict document would be self-attested by the same session that
ran the checks - the same shape as acceptance evidence attested by the verdict itself rather
than by a receipt. `ppl/findings.py` gives a finding
a durable, independent home instead: one file per finding at
`runs/<task_id>/findings/<finding_id>.json`, carrying `finding_id`, `task_id`, `severity`
(`blocking` | `important` | `minor`), `evidence`, `affected_sha`, `affected_path`, `track`,
`owner`, `status` (`open` | `escalated` | `fixed`), `fix_link`, and, once closed,
`fixed_at_sha`. Written only through `ppl findings add/status/delete` (lease-fenced like
`ppl evidence add` and `ppl verdict record` [R-LEASE-STALE-WRITE]), and only the
orchestrator ever calls it: `ppl-verifier` has no shell to call it with, and every role in
`boundary.ROLE_RULES` that does hold Bash is denied every state-changing `ppl` subcommand by the guard hook
regardless (see the cage row below) - the verifier EMITS findings as data in its report,
the orchestrator records each one with its own generation (section 7, Stage 5).

**Two-track routing, and there is no third box.** A finding is either fixed now, because it
is in the candidate's own diff (`own_diff`), or batch-escalated to an owner, because it is
in code the candidate never touched (`foreign`) [R-FINDINGS-TRACK-DERIVED]. The track is never a caller-supplied
argument - `ppl findings add` has no such parameter to accept one through - it is derived
every time from `gitfacts.changed_paths(repo, base_sha, affected_sha)`: `affected_path`
either appears in that diff or it does not, and those are the only two answers there are
[R-FINDINGS-TRACK-DERIVED]. A `foreign` finding must record an `owner` to escalate to, or
`ppl findings add` refuses it [R-FINDINGS-FOREIGN-OWNER]; an `own_diff` finding defaults its
owner to the task itself. A `foreign` finding is born `escalated` at creation, not left
`open` for a follow-up step someone forgets - but nothing beyond that recorded owner string
actually notifies anyone; no email, ticket or message is sent
[R-FINDINGS-FOREIGN-NO-NOTIFICATION n/a].

Only an open `blocking` finding on the `own_diff` track gates this task's own done gate
(R-DONE-OPEN-FINDING) - a `foreign` finding is deliberately not a blocker for this task:
routing it to its owner instead of stalling this task on someone else's code is the entire
point of the second track. This check does **not** filter by `affected_sha == head`: under
that filter any commit at all - including one that does not touch the defective file -
would clear a real, unfixed, blocking finding off the gate. The same-SHA rule that governs *attestations* (a verdict, an approval, a CI
result each say "as of this tree" and rightly expire when the tree moves) does not apply to
a *defect*: a defect does not stop existing because somebody committed a whitespace change,
so a blocking own_diff finding blocks until it is closed by proof, whatever the head does.

Deleting a finding without a closing reason is refused - `ppl findings delete` requires
`--reason` [R-FINDINGS-DELETE-REASON]. The deletion is real (the file is removed), but the
reason is written to `log.jsonl` first, so what closed it outlives the finding record
itself.

**Closing a finding as `fixed` is earned, not asserted.**
`ppl findings status ... --status fixed` requires a `--fix-link` (the human trail) AND a
real commit that touched `affected_path` between `affected_sha` and the current head -
`affected_path in gitfacts.changed_paths(repo, affected_sha, head)` - the same
derive-do-not-accept discipline the track already uses, applied to closure instead of "the
head moved" (which any unrelated commit satisfies, the same hole as the SHA filter above) [R-FINDINGS-FIXED-REQUIRES-PROOF]. The commit checked is
`fixed_at_sha`, recorded on the finding once it closes.

Without the SHA filter a defect can, in principle, be reported again on a
later candidate before it is fixed. Decided: the protocol is to check first, not to make
`add` refuse a duplicate outright - a code-level duplicate refusal would be coarse (a
second, genuinely different blocking defect that happens to live in the same file would be
refused too) where the real distinguishing fact - "is this the same defect as an already-
open finding" - is a judgement call, not something `gitfacts.changed_paths` alone can
settle [R-FINDINGS-DEDUPE-PROTOCOL]. `ppl-verifier` is handed the open-findings list before dispatch and told to report
only what is genuinely new; this is prose, enforced by the verifier prompt and the skill,
not by code, and is recorded here as a soft rule rather than implied to be a hard one
[R-FINDINGS-DEDUPE-PROTOCOL].

A verdict document may no longer carry findings inline: `ppl verdict record` refuses a
document whose `findings` array is non-empty, pointing the caller at `ppl findings add`
instead [R-FINDINGS-VERDICT-FIELD-RETIRED]. This is a refusal, not a silent migration -
auto-ingesting a legacy inline list at record time would mint a fresh `finding_id` on every
re-record of the same verdict (recording a verdict twice on one SHA is legitimate, e.g. a
correction), silently duplicating open findings with no way to tell they came from the same
report.

The panel surfaces open findings per run (`ppl/panel/server.py:run_detail`) and a fleet-
wide open-findings count per row (`list_runs`), so an operator sees them without opening
each run's registry by hand.

**A finding file this registry cannot trust fails the read closed, not open**
[R-FINDINGS-FAILS-CLOSED-ON-MALFORMED]. Skipping a document that is not a dict would let a
blocking finding's file rewritten to `[]` - still valid JSON, just the wrong shape - vanish
from `open_blocking_on_own_diff` and open the done gate for free, and invalid JSON would crash
the process. Both are the same refusal: `list_all`, `list_open` and
`open_blocking_on_own_diff` all raise `SCHEMA_INVALID` when any finding file on disk cannot
be read as a finding object, the same fail-closed treatment `hooks/ppl_guard.py:active_state`
already gives an unreadable `state.json` [R-FINDINGS-FAILS-CLOSED-ON-MALFORMED].

### 6.7 Authority order

Checked top-down on every restart and every contested fact:

1. safety policy and owner decisions
2. external facts: git, the forge, CI, the tracker
3. authoritative spec/task revision
4. machine evidence receipts
5. independent verdict
6. role handoff
7. scheduler snapshot (`state.json`)
8. chat context

### 6.8 Recovery

`ppl recover` runs first on every session start, before the model forms an opinion:

1. read trusted policy from the code checkout and the data root (not from the candidate branch)
2. read real facts: `git status`, branch, head SHA, worktree list, MR state, pipeline state
3. find open leases and `in_progress` tasks
4. reconcile checkpoint, parking record and verdicts against those facts
5. resume or block honestly
6. only then consider new work

A non-idempotent side effect does not repeat just because a log was lost. Proof of
safe replay comes before the replay.

### 6.9 Watchdog

Without a time budget a worker that hangs takes no dispatch, spends no retry, and simply
does not return - and nothing notices. This is a heartbeat, not a stage clock:
it measures inactivity, because total work is already capped by the dispatch and retry
budgets of section 12, so wall-clock-per-stage was not this field's job to measure, and a
stage-duration reading of it would have parked `review` - this pipeline's steady state,
which waits on a human reviewer for as long as reviewers take - within hours of every MR
opening, on pure false-positive noise.

`ppl stage enter` (and `ppl init`, for admission) stamps a `deadline` on the state,
`INACTIVITY_TIMEOUT_SECONDS` out from the moment of entry. `ppl watchdog check` compares it
to the current time and, past it with nothing having refreshed it since, parks the run with
blocker `TIMED_OUT` and bumps the lease generation - the run is refused a second time by
exactly the fence a returning stale attempt already trips (6.2, `R-LEASE-STALE-WRITE`), for
the same reason: if the hung attempt wakes up and writes anyway, it is still holding the
generation this call left behind [R-WATCHDOG-DEADLINE]. Every state-changing `ppl` command
refuses once a run's deadline has passed with nothing refreshing it, through its two existing
shared gates - `control.enforce` and `require_current_generation` - and `ppl recover` runs the
same check before it reports anything else [R-WATCHDOG-DEADLINE]. `ppl park`, `ppl escalate`,
`ppl destroy` and the `merge_watch` tail stay exempt: parking is how a timed-out run is
supposed to be recorded, so the one command that records it must not itself be refused for the
run already being late (the same carve-out 9.2 gives the operator halt); `merge_watch` runs
after `done` with the lifecycle already terminal (`R-MERGE-WATCH-TERMINAL`), where parking a
purely observational tail would serve nothing; and `ppl destroy` is how an owner ends a run,
including one this very mechanism has already parked or that the operator has separately
halted - `cmd_destroy` calls neither `control.enforce` nor `watchdog.check`, deliberately,
because a teardown gated on the same lease or halt it exists to end could never run against
the run it is most needed for [R-WATCHDOG-DEADLINE].

`run.refresh_deadline` is what makes this a heartbeat rather than a clock: it restamps the
deadline on ordinary control-plane activity, short of a full stage transition (which already
restamps it as part of entering the new stage). Three cases define what counts, precisely so
the mechanism only ever measures "has this run been touched, at all, recently":

- **A quiet review-loop tick MUST refresh.** Section 8's tick calls `ppl recover` every
  cycle regardless of whether any thread needs a reply; on a cycle where nothing does, that
  `ppl recover` call is the only `ppl` activity on the run, and it must count. `recover.
  reconcile` refreshes on exactly the branch where `watchdog.check` did *not* just time the
  run out - never ahead of that check, which would mask the very hang it exists to catch
  [R-WATCHDOG-DEADLINE].
  The direct consequence, stated plainly rather than left for a reader to discover: a human
  running `ppl recover` by hand shortly before expiry also extends detection by one more
  inactivity window. That is acceptable, not a loophole, because `ppl recover` is itself a
  control-plane act (it already bumps the lease generation unconditionally) - the passive
  case, "no live signal, ever", is `ppl show`, and `ppl show` does not refresh anything.
- **A hang with zero `ppl` calls MUST still expire, exactly as before this heartbeat
  refinement.** Nothing refreshes a run nobody is touching; the next command against it,
  whenever that arrives, finds the deadline already passed.
- **A stale attempt's refused write MUST NOT refresh the deadline the live attempt is
  racing** [R-WATCHDOG-DEADLINE]. `require_current_generation` refreshes only after
  `check_generation` passes, and `control.enforce` only after its control-record check
  passes - both after the point where a refusal would have raised, never before it
  [R-WATCHDOG-DEADLINE]. A stale
  write that is about to be fenced out by `R-LEASE-STALE-WRITE` must not also extend the
  window the run it no longer owns is judged against [R-WATCHDOG-DEADLINE].

This is bookkeeping wired into every write and every quiet recover tick, not a live timer, and
the textbook's own constraint about the control process applies to what it catches:
**nothing here runs on its own.** A watchdog that only fires when called catches a hang
exactly when something calls it, and the honest answer to "who calls it during a hang" is
stage-dependent:

- Before an MR exists (admission through verification), the only things that ever call
  `ppl` are the orchestrator and the dispatched roles - and a hung agent making no tool calls
  is, by construction, not calling anything. Nobody and nothing fires the check on its own.
  The hang is caught the next time a human or a freshly dispatched attempt runs any `ppl`
  command against that run, or runs `ppl recover` - not before.
- Once stage 6 (MR) is reached, the review monitor (section 8) schedules its own wake-ups
  independently of whatever the orchestrator is doing and calls `ppl recover` every tick,
  which is now also what keeps a healthy, still-open review from expiring. This is the one
  caller that fires *between* dispatches, while the orchestrator is idle waiting on a human
  reviewer or CI - not a caller that can interrupt a hang mid-dispatch. A worker the tick
  itself dispatches (e.g. `ppl-review-responder`) and that then hangs blocks that tick exactly
  as it would block any other command sequence; the next wake-up simply queues behind it. So
  from `mr` onward the tick catches a *stage overrun with no activity at all*, not an agent
  hang in progress.

So this watchdog closes the fencing gap - a timed-out attempt cannot write past the point
something notices [R-WATCHDOG-DEADLINE] - without being a deadline guarantee. A hang before
`mr` is discovered, not prevented in real time; this is a residual, not a hidden hole, because
nothing in a hook-driven, session-scoped harness can run code while no session is dispatching
a tool call [R-WATCHDOG-NO-LIVE-TIMER n/a].

### 6.10 Verdict outcomes

`schema.VERDICT_OUTCOMES` is the closed set a verdict's `verdict` field may hold: `PASS`,
`REWORK`, `BLOCKED`, `OWNER_DECISION`, `INCONCLUSIVE`, `ERROR`, `CANCELLED`, `TIMEOUT`. Any
other value is refused when the verdict is recorded [R-VERDICT-OUTCOME-VOCAB].

The first four mean a real check ran and found (or affirmed) a real result. The last four
share one principle: a check that did not happen
is not evidence the candidate is bad, and it is not evidence the candidate is good either.
`INCONCLUSIVE` (the verifier could not reach a clear result from what it was given),
`ERROR` (a gate or the verifier itself crashed), `CANCELLED` (the run was stopped or
superseded mid-check) and `TIMEOUT` (a check ran past its own time budget) are therefore
never spelled as a kind of `PASS`, and never folded into `REWORK`/`BLOCKED` either
[R-DONE-VERDICT-PASS] - those
two mean a check ran to completion and came out against the candidate, which is a
different claim about the world than "the check never spoke".

`ppl transition done` refuses on every one of the eight outcomes except `PASS`, through the
same single equality check that already refused `REWORK`, `BLOCKED` and `OWNER_DECISION`
(6.4) [R-DONE-VERDICT-PASS]. This is deliberate, not incidental: a verdict
that could attest to its own acceptance - "proven" recomputed from the verdict document's own
claims rather than from a receipt that actually passed (`R-DONE-ACCEPTANCE-EVIDENCE`) - is the
class of hole that would reopen here if any "the check did not run" outcome were treated as
anything but a hard block.

**Routing a single incomplete outcome is not the same question as routing two in a
row.** Routing `INCONCLUSIVE`/`ERROR`/`CANCELLED`/`TIMEOUT` exactly like `REWORK` would spend the retry
budget for the verifier's own failure - three verifier crashes exhaust `retry_max` and park
the run as though the code were bad, with a parking record that blames the wrong thing.
Retry exists to cap how many times the CODE gets another try; a check that did not happen is
not the code's fault, which is the whole reason these outcomes exist. The routing:

- `INCONCLUSIVE`, `ERROR` and `TIMEOUT` spend `dispatch`, not `retry` - dispatch already
  caps a runaway loop, and a fresh verification attempt re-checks the SAME `candidate_sha`,
  since nothing about the code changed, only the check needs retrying.
- `CANCELLED` gets no automatic redispatch at all: a human or the operator cancelled that
  check, and auto-redispatching it would be ignoring them. It routes straight to
  `ppl escalate --code OWNER_DECISION`.
- The SECOND consecutive incomplete outcome recorded for the same `candidate_sha` - of any
  of the four kinds, not only a repeat of the exact same one - parks the run rather than let
  a broken verifier masquerade as an undecided candidate forever. This is enforced inside
  `ppl verdict record` itself, not left to orchestrator prose to notice the pattern and stop
  on its own [R-VERIFICATION-UNAVAILABLE-PARK]: the recurrence is derivable at record time
  from `log.jsonl`'s append-only trail of past "verdict" events for this sha - `verdicts/
  <sha>.json` itself is overwritten on every call and cannot answer "what was the previous
  outcome for this sha", but the log line `record` appends on every call, before this check
  runs, never has been. `ppl verdict record` still writes the verdict durably before parking
  - the verifier's report is not discarded, only the run is stopped from repeating the same
  non-answer a third time. The blocker is `VERIFICATION_UNAVAILABLE` (13), a new code rather
  than a reuse of `EXTERNAL_BLOCKED` or `BASELINE_FAILURE` (which mean other things) or
  `TIMED_OUT` (the inactivity watchdog measuring a different fact - a verifier's own
  self-reported `TIMEOUT` outcome is not the run going quiet, and overloading the one code
  for both would make both unreadable). The orchestrator's job on seeing it is to read the
  exit code, not to re-derive the recurrence itself - `ppl` already parked the run and wrote
  `parking.json` by the time it sees exit 2.

### 6.11 Metrics and labels

`ppl metrics` reads every run's `log.jsonl` and `verdicts/*.json` under `PPL_HOME` and
reports six numbers, sliced by `producer` (`provider:model`) and once more as `overall`.
**Every reader scans `runs/_archive/` as well as the live runs under `runs/`**
[R-METRIC-SCANS-ARCHIVE], because defect escape and false FAIL are only knowable after a
task has actually merged - which in the normal flow (`done -> merge_watch ->
ppl destroy --scope local`, 6.1) is after the run has already been archived.
`ppl destroy` moves a run's whole directory to
`runs/_archive/<mangled-name>`, and a reader that only looked under `runs/` would report
these metrics as permanently undefined for exactly the history they exist to measure, with
a printed reason ("run `ppl label`") that could not be followed because the label itself
could not be recorded against an archived task either. Labels are stored at
`PPL_HOME/labels/<task_id>/<sha>.json` - outside the run directory entirely, so `ppl destroy`
does not touch them - and every metric reader below walks archived run directories by reading
each one's own `state.json` for its `task_id` (the archive directory name is a mangled
`<task_id>-<timestamp>-<random>` string, and is not parsed back apart, since a task id may
itself contain a hyphen).

- **defect escape rate** - of the accepted (`PASS`) verdicts a human has since labelled,
  the fraction labelled `defective`
- **false FAIL rate** - of the declined (`REWORK`/`BLOCKED`) verdicts a human has since
  labelled, the fraction labelled `correct`
- **incomplete check rate** - of every verdict recorded, the fraction whose outcome is one
  of `INCONCLUSIVE`/`ERROR`/`CANCELLED`/`TIMEOUT` (6.10)
- **cost per accepted task**
- **time to acceptance** - elapsed wall-clock time from `ppl init` to the `lifecycle: done`
  transition, attributed to the producer of the `PASS` verdict that unlocked it
- **human minutes**

**A metric with no data prints that it is undefined, and never prints zero**
[R-METRIC-LABEL-DEPENDENT-UNDEFINED] [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED]
[R-METRIC-STRUCTURALLY-UNDEFINED] [R-METRIC-HUMAN-MINUTES-UNDEFINED]. Zero is a claim - "we
shipped no defects" - and undefined is the honest report when nobody has told the pipeline
whether that is true. Four different reasons produce "undefined" here, and the report says
which one applies - deliberately kept as four and not folded together, because "nothing to
build" and "needed and not built" are different states that should not read as the same
thing, the same principle the rule matrix itself holds `n/a` rows to (5.5):

- Defect escape rate and false FAIL rate need a human label on top of a verdict
  (`ppl label`, below); undefined until at least one exists for a verdict of the matching
  kind [R-METRIC-LABEL-DEPENDENT-UNDEFINED]. Two causes are told apart, because
  conflating them prints a false reason with advice nobody can follow ("run `ppl label`"
  with nothing there to label): no verdict of that kind exists at all (nothing to label, so the message
  says that and does not mention `ppl label`), versus a verdict exists but carries no label
  yet (the message names a real, copyable sha and task id from among the unlabelled ones,
  never a `<sha>` placeholder) [R-METRIC-LABEL-DEPENDENT-UNDEFINED].
- Incomplete check rate needs at least one verdict recorded anywhere - it has one cause only,
  since the records a producer slice sees are already scoped to that producer by `compute`,
  so an empty list unambiguously means nothing of that producer's has a verdict yet. Time to
  acceptance needs at least one task in scope to have reached `done`, and here a slice by
  producer DOES have two causes worth telling apart: nothing anywhere has reached `done`
  (true for every slice alike), versus something has reached `done` under a different
  producer (the message says how many tasks did finish, rather than implying the whole fleet
  is idle when one producer's simply is not) [R-METRIC-SAMPLE-DEPENDENT-UNDEFINED].
- Cost per accepted task is undefined always, for a reason that will not change until
  routing does: v1 routing is entirely subscription-billed Claude subagents (12), so
  `usd_used` is a manual budget cap nothing in the dispatch path spends automatically, and
  there is no dollar meter anywhere for this to read from. There is nothing to build here
  yet, not merely nothing built [R-METRIC-STRUCTURALLY-UNDEFINED]. It becomes computable the
  day an API-billed adapter exists (12).
- Human minutes is undefined always too, but for the other reason: this one IS buildable
  today (a `--minutes` flag on `ppl label`, or a dedicated command) and simply has not been
  - no event or field anywhere in this pipeline records a human's working minutes, though a
  human's *wait* time is inferable from timestamps and effort minutes are a different claim
  [R-METRIC-HUMAN-MINUTES-UNDEFINED]. Unlike cost, this is a real gap, not a property of the
  architecture.

`ppl label --sha <sha> --actual defective|correct` records the one fact this system has no
other way to observe: whether a given candidate SHA, in hindsight, was actually correct or
actually defective. Two rulings, both made explicit rather than left to drift, because
the watchdog (6.9) changed what "wiring a command through a shared gate" means:

1. **`ppl label` is not liveness.** It does not call `control.enforce` and does not refresh
   the run's inactivity deadline [R-LABEL-NO-LIVENESS-REFRESH]. A human labelling a SHA from
   days or weeks ago says nothing about whether the run is alive right now, and refreshing
   the deadline on it would mask a real stall - exactly the failure the watchdog (6.9)
   exists to catch. `ppl label` still confirms the task exists, live OR archived
   (R-METRIC-SCANS-ARCHIVE), so a label against a task id that was never created does not
   succeed [R-LABEL-VALUE-VOCAB], but a label against a run whose deadline has already
   passed - or that has already been archived by `ppl destroy` - succeeds exactly as it
   would before either happened.
2. **`ppl label` is not fenced by the lease generation.** It takes no `--generation` argument
   and does not call `require_current_generation` [R-LABEL-NO-LEASE-FENCE]. A label is a
   human-entered fact about a SHA that is already in the past, never a write produced by a
   dispatched attempt [R-LABEL-NO-LEASE-FENCE] - there is no "stale attempt" here for a lease generation to fence out,
   and requiring one would be theatre.

`--sha` and `--actual` are validated the same way every other write in this design validates
untrusted input: hexadecimal only, no path traversal, and `--actual` restricted to
`defective`/`correct` [R-LABEL-VALUE-VOCAB].

## 7. Stage flow: task link to MR

Entry: `/pipeline https://tracker.example/browse/DEMO-12345`

### Stage 0 - admission

Orchestrator reads the tracker issue plus parent/epic and linked issues through the
tracker's MCP read tools, plus any linked spec page. `ppl init` creates the run.

Admission checklist (chapter 12.2), all must pass:

- exactly one user or architectural outcome
- governing revision fixed (issue revision + spec page version)
- acceptance criteria stable and verifiable, each assigned a stable ID
- material decisions closed
- dependencies satisfied
- risk tier assigned (see 12.3)
- write boundary non-empty and enforceable
- forbidden paths enumerated
- proof path defined before implementation
- budget and terminal route set

Fails admission -> `ppl escalate --code SPEC_GAP`. The task is not "clarified along the
way" by a worker.

### Stage 1 - baseline

Record base SHA and baseline check results before touching anything: the harness's gates
(build, lint, unit and integration tests) and known failures. Without this, a pre-existing red test is
indistinguishable from a regression, and the model will pick the convenient reading.

Branch is created from the harness's `repo.default_branch`, in a dedicated worktree under
`repo.worktrees`.

`base_sha` is write-once and must be an ancestor of HEAD. `ppl baseline record` refuses a
second call (`ILLEGAL_TRANSITION`) [R-BASELINE-WRITE-ONCE] and refuses a base that HEAD
does not descend from (`SCHEMA_INVALID`, checked with `git merge-base --is-ancestor`)
[R-BASELINE-ANCESTOR]. Rule 4 of the terminal
transition (6.4) is a diff from `base_sha` to HEAD, so a baseline recorded after the work
commit would make that diff empty and the boundary check pass vacuously - the same commit
value reads identically in git for a legitimate fresh branch (HEAD equals the merge-base,
because nothing has landed yet) and for that late-repair attack, so the ancestor check
alone (or a bare refusal of `base_sha == HEAD`, which would also refuse the
legitimate fresh branch) cannot tell them apart [R-BASELINE-NOT-AT-HEAD]. `ppl baseline record` is refused with
`ILLEGAL_TRANSITION` unless the run is still in the baseline stage - the danger is caught
by WHEN the call happens, not by which commit it names [R-BASELINE-NOT-AT-HEAD]. Accepted
gap: a commit made during the baseline stage, before this call, still passes.

### Stage 2 - briefing

`ppl-briefer` (tier `good`, read-only) compiles the brief from the tracker task, linked
specs and the knowledge skills the harness names (`knowledge.skills`). It reads code to
locate contracts, it does not write code, and it has no write tool at all: the brief comes
back as the JSON body of its final message and the orchestrator saves it. The artifact that
governs every downstream worker is therefore written by the controller, not by the role that
produced it.

Output must validate against the brief schema. `ppl brief set` stores it and derives
`boundary.json` from it. `ppl-brief-challenger` (tier `good`, read-only) then checks the brief
against the spec text - duplicate classes, wire shapes, parked decisions - and `ppl brief
challenge` records its report bound to the brief's `spec_revision`. A run leaves briefing for
implementation only with an `ACCEPTED` challenge bound to the current `spec_revision`
[R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION].

If the brief cannot be derived, the briefer returns `SPEC_GAP` rather than inventing
product truth - a prompt instruction, not a check [R-BRIEFER-SPEC-GAP n/a].

### Stage 3a - implementation

`ppl-code-worker` (tier `average`) receives the brief, the allowed part of the tree, public
signatures, its write boundary and the focused verification commands. It does not write
tests [R-ROLE-CODE-WORKER-TESTS] - nothing here restricts it from reading one if it
chooses to, unlike the read isolation the test worker gets in the other direction (5.3). If the task is not derivable from the brief it returns
`CONTEXT_INSUFFICIENT` instead of expanding scope creatively.

### Stage 3b - tests

`ppl-test-worker` (tier `average`) receives behavior, acceptance criteria, corner cases and
agreed interfaces - NOT the implementation diff. Writes unit tests and integration tests.

This is the single most expensive and most valuable property of the design: tests stop
inheriting the implementation's blind spot. Chapter 6 is explicit that one agent writing
both code and its tests is theatre.

Test quality gate before the tests count as evidence:

- no tautological assertions
- failure paths covered, not only happy path
- order-independent
- no uncontrolled network or wall-clock dependency
- each critical test carries its acceptance ID
- for a bugfix: proven RED on the base SHA before the fix, or a deliberate defect injection

### Stage 4 - deterministic gates

Run by the orchestrator with no model judgement involved. Ordered cheapest first:

1. structural: compile, lint, no forbidden diff, boundary check
2. focused behavioral: tests of the changed module, acceptance examples, negative cases
3. integration evidence: full module suite, delta compared against baseline

The commands are the harness's `[[gate]]` recipes (section 11).

Every gate writes a receipt bound to the candidate SHA. Gate failure inside the boundary
routes to bounded rework. Gate failure outside the boundary is `BASELINE_FAILURE` and gets
parked and escalated - not "greened up".

### Stage 5 - verification

`ppl-verifier` (tier `good`, read-only) compares spec, diff, tests and results. It looks for
acceptance criteria without proof, logic errors and corner cases that break in real
scenarios on this concrete code.

Verifier prompt constraints (chapter 6.5), stated as hard rules because an unconstrained
frontier reviewer becomes an alternative-architecture generator:

- style and taste are not findings and are discarded
- defense-in-depth code is not dead code and is not to be removed
- a blocking finding must state: which scenario breaks, which invariant is violated, where
  the proof is, why the existing test does not catch it, minimal reproduction

The verifier produces a verdict and EMITS its findings as data in the same report -
severity, evidence, affected path, and owner when it can name one - never by running `ppl
findings add` itself [R-ROLE-TOOLS-READONLY]: it has no shell (`routing.BASE_TOOLS` gives `ppl-verifier` no `Bash`), and
even a role that does hold one is denied every state-changing `ppl` subcommand by the guard
hook regardless [R-GUARD-PPL-CLI-DENIED]. The orchestrator records each reported finding with `ppl findings
add`, using its own generation, before recording the verdict (6.6a). Before dispatch, the
orchestrator lists the findings already open on the task and hands that list to the
verifier, so an unfixed defect already on file is not reported - and therefore not
recorded - a second time (R-FINDINGS-DEDUPE-PROTOCOL, 6.6a). It never repairs the
candidate [R-ROLE-READONLY-NO-WRITE]. REWORK routes to a fresh worker with the finding, producing a new SHA and a new
verification round. Three full rework cycles maximum, then the problem changes class:
re-brief, re-plan or escalate.

### Stage 6 - MR

Preconditions before push: secret scan clean, diff within boundary, no temporary artifacts
committed, verdict PASS on head SHA. None of the four is checked at `ppl stage enter mr`, which tests only the operator halt, the
dispatch cap and the stage graph, so they are the orchestrator's to run until the done gate
rechecks three of them after the MR already exists [R-MR-PRECONDITIONS n/a].

Then push the branch and open the MR through the forge adapter (the GitLab MCP server, or
`gh pr create`) with a description that lists the
acceptance criteria and their evidence. Stage moves to `review`.

No tracker write happens here.

## 8. Review monitor

Runs as `/loop` in dynamic mode: the pipeline schedules its own wake-ups
(`ScheduleWakeup`, 1200-1800s when idle, tighter while a CI pipeline is running). Each tick
is stateless and reads `state.json` plus live forge facts. It survives session restarts
because all state is durable.

Per tick:

1. Fetch the MR's discussions, approval state and pipelines through the forge adapter's
   read tools (`mr_discussions`, `get_merge_request_approval_state`,
   `list_merge_request_pipelines` on GitLab, `gh pr view` and `gh api` reads on GitHub).
2. `ppl approval check` (or `ppl mr state`): if head SHA moved since the last approval, mark
   approvals stale.
3. `ppl review pending --threads <fetched threads> --sha <head>` is the dispatch filter: it
   drops resolved threads, threads authored by the pipeline's own recorded MR author
   (`ppl mr set --author`, sticky across MR updates), and threads the review ledger
   (`threads.json`, section 6.6) already answered on this exact `last_note_id`. Only what it
   returns is classified by `ppl-review-responder` (tier `average`), into exactly one class:
   - `NEEDS_CODE_CHANGE` - re-enter stage 3 with the remark as an additional constraint.
     Full loop: worker -> test-worker -> gates -> verifier. New SHA invalidates approvals.
   - `NEEDS_REPLY_ONLY` - a factual answer exists in the code or the evidence. Drafts a
     reply citing file:line or the receipt.
   - `DISAGREE` - the pipeline believes the remark is wrong. It does not argue autonomously -
     it drafts the counter-argument and escalates to the owner.
   - `NEEDS_OWNER` - material decision, scope change, or architectural direction. Escalates.
4. Orchestrator posts replies through the forge adapter, then `ppl review answered --thread <id> --sha
   <sha> --classification <c> --last-note-id <id>` records the reply in the ledger so the
   next tick's `ppl review pending` does not name the thread again unless a newer note
   arrives. It never resolves a thread [R-MCP-RESOLVE-THREAD].
5. CI red on the candidate -> classify as regression vs baseline, then rework or park. Not
   a bare retry in hope.
6. Terminal condition: an approval exists and is bound to the current head SHA. `ppl
   transition done` runs, the lease is released, the owner is notified. This is the end of
   the pipeline as defined by the owner.

Optional tail (`merge_watch`): after `done`, the loop may keep watching the MR at low
frequency. When the merge is observed on the forge, and only then, the orchestrator posts the
tracker update - plain English describing what was done and how to check it - then cleans
the worktree per policy. This tail
records delivery evidence; it cannot reopen or alter the lifecycle
[R-MERGE-WATCH-TERMINAL].

## 9. Admin panel

### 9.1 Purpose and shape

`ppl serve` starts a local web app on `127.0.0.1:8787`. Python standard library only, no
dependencies, works offline. One HTML page, polling `/api/runs`.

The panel is the founder interface from chapter 11: it shows the outcome, not the state
machine. Internal role names, hook codes and stage graphs are available on expand, not by
default.

Per run, the panel shows:

- outcome in human language, plus tracker key and link
- task lifecycle status and current run stage
- budgets consumed (dispatch, retry, cost) against their limits
- base SHA, candidate SHA, branch, worktree path
- MR link, approval state, and whether the approval is stale for the current head SHA
- last durable fact and next safe action
- "needs you" flag with the escalation question when one is open
- open reviewer threads and their classification
- live tail of `log.jsonl`

Multiple runs are listed together; v1 executes one at a time, but a parked run and an
active run coexist, and the panel is the place where that is visible.

### 9.2 Control is cooperative

The panel takes no lifecycle decision of its own: it writes `runs/<TASK-ID>/control.json`
and lets `ppl` and the hook act on it. For `stop` and `destroy` the panel process does write run state - `stop` parks the run
through `run.park` and `destroy` archives the whole run directory - so "the panel only ever
writes control.json" is true of `pause` and `resume` only [R-PANEL-STATE-WRITE n/a].

```json
{"command": "pause", "requested_at": "2026-08-31T12:00:00Z", "requested_by": "panel", "nonce": "..."}
```

Two independent readers act on it:

- `ppl` checks control at the commands that advance a run or mutate state a paused run must
  not be able to move out from under itself: `stage enter`, `brief set`, `boundary set`,
  `baseline record`, `transition`, `budget spend`, `mr set`, `review answered`,
  `review approved`, and `recover` - ten call sites (`ppl/cli.py`: `cmd_stage`, `cmd_brief`,
  `cmd_boundary`, `cmd_baseline`, `cmd_transition`, `cmd_budget`, the `set` branch of
  `cmd_mr`, the `answered` and the `approved`/`else` branches of `cmd_review`, and
  `cmd_recover` - each carries its own `control.enforce` call) [R-OPERATOR-HALT-CLI]. Not
  every subcommand is gated, and deliberately so: `status`, `show` and
  `control get` must keep working while a run is halted, or the operator loses sight of the
  run they just paused; `control set resume` must work while a pause is in force or nothing
  could ever lift it; and `park` and `escalate` stay free too, since an operator halt must
  not block recording why the run stopped [R-OPERATOR-HALT-CLI]. `review answered` is one of the ten: it appends to the review ledger (`threads.json`), which is run state a
  paused run must not be free to keep advancing, the same reasoning as `review approved`
  [R-OPERATOR-HALT-CLI]. Every path that moves a run forward or rewrites ledger/boundary/mr/budget
  state passes through one of the ten, so a halt still lands at the next such call
- the `PreToolUse` guard hook stats the control file on every tool call, so a pause lands
  within one tool call rather than one stage; for the unroled main session specifically, it
  does so under the `isolated` scoping described in 5.4

This is the one carve-out to the "only `ppl` writes under `runs/`" rule: `control.json` is
written by the panel or by `ppl control set`, and read by `ppl` and the hook. It is an
inbox, not authoritative state.

| Command | Effect |
|---|---|
| `pause` | the agent completes the in-flight tool call, then halts. State intact and resumable. A review-monitor tick still fires, reads control, and no-ops. |
| `resume` | clears the pause. `ppl` and the hook stop halting on the next call; no token is queued and nothing is restarted (see 9.4) |
| `stop` | pause, then `ppl park` with a real parking record: blocker `OWNER_DECISION`, owner, last durable fact, safe next action. Parking is escalation, not failure. |
| `destroy` | ends the run (see 9.3) |

### 9.3 Destroy

Destroy always prompts and the owner picks the scope per run:

- **local only** - stop the run, remove the worktree, archive the run directory under
  `runs/_archive/`, and clear the active pointer. No outward mutation. The panel process
  performs this itself, since it is ordinary local file work.
- **full cleanup** - additionally close the MR and delete the remote branch. This is an
  irreversible outward mutation, so it requires typed confirmation of the task ID in the
  dialog. The panel does not perform it: it writes `remote_cleanup.json` into the run and
  the orchestrator carries it out on its next tick, because the orchestrator is the only
  component holding forge credentials. The run directory survives until that half is done,
  after which the local archive step runs.

Destroy does not silently touch remote state. If the dialog is dismissed, nothing happens.

`pause`, `stop` and `destroy` all halt the run at the next `ppl` call or tool call. `stop`
additionally writes a real parking record with blocker `OWNER_DECISION`, so a stopped run
is an escalation with a resume path rather than a silent halt.

### 9.4 Hard stop and resume

**Hard stop.** `state.json` records the orchestrator process PID and its process start
time. Before sending SIGTERM the panel verifies both, so a recycled PID after a crash
cannot be killed by mistake [R-PANEL-SIGTERM-PID]. SIGTERM only; the pipeline is designed to be resumable, so
there is no reason to SIGKILL a session that is holding a lease.

**Resume, stated honestly.** The panel does not spawn Claude sessions. Resume only writes a
`resume` command into `control.json`, which is the absence of a halt rather than a request
for work: there is no `resume_requested` token anywhere in the code, and nothing consumes
one. Either a live session's next `ppl` call simply stops being refused [R-OPERATOR-HALT-CLI],
or the owner starts a session and `ppl recover` reconciles and continues. The reason is a
real constraint, not caution: a headless `claude -p` print session can advance a bounded
stage, but it cannot durably hold the `ScheduleWakeup` review-monitor loop, which requires
an interactive or main-loop session [R-PANEL-NO-SPAWN n/a]. A panel that claimed to resume anything would be
lying about the monitoring half.

### 9.5 Panel security

The panel binds to loopback only, not the network [R-PANEL-LOOPBACK]. It is a control
surface for local runs and holds no credentials of its own: every outward mutation it can
trigger is executed by `ppl` or by an orchestrator session using the credentials those
already have.

Every request must carry the session token `ppl serve` creates at each start: the printed
URL carries it once, the first load turns it into an `HttpOnly`, `SameSite=Strict` cookie,
and a request without it is refused [R-STUDIO-SESSION-TOKEN]. A request whose Host header is
not `127.0.0.1` or `localhost` on the serving port is refused, so a DNS-rebinding page cannot
reach the panel through a hostname it controls [R-STUDIO-HOST-CHECK]. A POST or PUT is refused
unless it carries `X-PPL: 1`, a loopback Origin when it has one, and a JSON content type, so a
cross-site form cannot submit to it [R-STUDIO-JSON-ONLY].

## 10. Role adapters and model substitution

### 10.1 The contract that makes models replaceable

The canonical workflow must not know which provider executed a role
[R-PROVIDER-AGNOSTIC]. Chapter 8 states the
rule and chapter 1 gives the reason: models are components, the control plane is the
system. Two mechanisms make this real rather than aspirational.

**Single source of routing truth.** `~/.ppl/routing.toml` describes every tier and every
role. `ppl harness apply` (or `ppl routing apply`) generates the agent files' frontmatter from
it and the active harness. Agent
files become build artifacts, so there is exactly one place to change a model and no drift
between config and reality. This is built from day one even though v1 is entirely Claude,
because retrofitting it later means rewriting every agent file.

```toml
[providers.claude-code-subagent]
kind = "subagent"
allow_source_upload = true

[providers.vendor-x]
kind = "openai-compatible"
base_url = "https://..."
auth_env = "VENDOR_X_API_KEY"
allow_source_upload = false
max_usd_per_task = 2.00

# A tier names a provider and a model, a role names a tier.
[tiers]
heavy   = { provider = "claude-code-subagent", model = "<model>" }
good    = { provider = "claude-code-subagent", model = "<model>" }
average = { provider = "claude-code-subagent", model = "<model>" }
light   = { provider = "claude-code-subagent", model = "<model>" }

[roles.ppl-briefer]
tier = "good"
params = { effort = "high" }

[roles.ppl-code-worker]
tier = "average"
params = { effort = "high" }
by_risk = { high = "good" }
```

The models the tiers name ship in `defaults/routing.example.toml`. A model name is a claim
until `ppl models verify` proves it: one cheap probe per model through the Claude Code binary,
recorded in `models.json` and stale again after `[models] verify_ttl_days` or a change of the
Claude Code version. `ppl init` refuses `MODELS_UNVERIFIED` while any model a role can route to
for the run's risk is unverified or stale, unless `--skip-model-check` is passed, which the run
records as `model_check = skipped` [R-INIT-MODELS-VERIFIED]. `ppl setup models` probes every
tier it configures, a tier no role routes to included, so a mistyped model is caught when it is
chosen rather than on the first run that routes to it.

`params` is a provider-specific blob, not a universal field. `effort` is a Claude concept
and must not leak into the shared schema [R-PARAMS-PROVIDER-SPECIFIC n/a].

TOML rather than YAML because the whole toolchain is stdlib-only: Python 3.11 and newer ship
`tomllib` for reading, while YAML would add PyYAML as the pipeline's single dependency for
no gain. The file is hand-edited and machine-read, which is exactly `tomllib`'s scope.

**Patch-based worker contract.** This is the load-bearing piece. A non-Claude-Code provider
gets no `PreToolUse` hooks, so the cage from section 5.4 would simply evaporate for it.
Therefore no worker writes the tree directly, whatever the provider: the role returns a
unified diff, and `ppl apply-patch` validates every touched path against `boundary.json`
before applying it. Enforcement moves out of the harness and into `ppl`, where it holds for
every provider equally.

**Unexercised in v1.** `ppl apply-patch` is implemented and unit-tested, but nothing in the
v1 workflow calls it: the only provider is the Claude Code subagent, whose workers write the
tree directly under the `PreToolUse` guard. So in v1 the hook is not "the early cut" beside
a stronger check - it is the only check a worker's write passes. The `apply-patch` guarantee
[R-APPLY-PATCH-BOUNDARY] becomes real the day a non-subagent adapter exists, and the workflow that routes worker
output through it has to be written then. `templates/ppl-code-worker.md.body` does not tell
the worker its patch is validated again after the fact, because the code does not keep that
promise.

Context isolation rides on the same contract: for a role run through `ppl dispatch`, `ppl`
assembles the input payload, and an adapter has no channel through which to fetch more than
it was given. The v1 subagent path has no such payload - the orchestrator composes the
prompt itself. The test worker still holds `Read`, `Grep`, `Glob` and `Bash`, and the guard
hook denies all four of them when aimed at a main source inside the active run's repo
[R-TEST-WORKER-READ-TOOLS] [R-TEST-WORKER-BASH-READS] - what remains an instruction in
`templates/ppl-test-worker.md.body` and nothing structural is narrow: the
orchestrator's own dispatch prompt is not checked, so nothing stops it pasting the diff into
that prompt's text directly [R-TEST-WORKER-NO-DIFF n/a], and running the worker's own tests
still surfaces the implementation through compiler and assertion output, which no hook can
filter out of a tool result [R-TEST-WORKER-EXECUTION-CHANNEL n/a].

### 10.2 Adapter interface

Frozen now, implementations added when a second provider exists.

Input on stdin:

```json
{"role": "ppl-verifier", "task_id": "DEMO-12345", "brief": {...},
 "context_files": [{"path": "...", "content": "..."}],
 "boundary": {"write": ["..."], "forbid": ["..."]},
 "budget": {"max_usd": 1.00, "max_tokens": 200000}}
```

Output on stdout:

```json
{"status": "ok|gap|error", "error_code": "CONTEXT_INSUFFICIENT",
 "diff": "<unified diff or null>", "artifact": {...},
 "usage": {"input_tokens": 0, "output_tokens": 0, "usd": 0.0},
 "producer": {"provider": "vendor-x", "model": "...", "params_hash": "..."}}
```

Same schemas and the same closed failure vocabulary as the subagent path, so a role swap
changes nothing upstream of the adapter.

### 10.3 What comes with API providers

**Cost budgets.** Subscription roles spend a rate-limit window; API roles spend money.
`max_usd` per role and per task sits alongside dispatch and retry in the budget block and
is enforced by `ppl budget` [R-BUDGET-USD]. Exhaustion parks the run like any other budget.

**Egress policy.** Project source is usually proprietary. `allow_source_upload` defaults to
`false` for every non-Anthropic endpoint, and a role whose provider is not permitted to
receive source cannot be assigned a payload containing repository files - `ppl` refuses the
dispatch rather than trimming the payload silently [R-EGRESS-SOURCE-UPLOAD]. Sending source to a third party is an
explicit, per-provider owner decision.

**Attribution.** Every evidence receipt and every verdict carries
`producer: {provider, model, params_hash}`. Without it, a routing change cannot be
evaluated [R-PRODUCER-ATTRIBUTION]. With it, rework rate, hook-hit rate and verifier false-positive rate can each be
attributed to the profile that produced them, which is the input to the chapter 8 eval
flywheel.

**Secrets.** Credentials are referenced by environment variable name in `routing.toml` and
are not stored in it. Adapters receive only the variables their provider declares. Worker
roles receive none.

## 11. Repo-specific gates

Generic doctrine is worthless without the local landmines encoded, and those belong to the
project, not to this pipeline. They are declared in the harness: the `[env]` table (variables
every build command needs, rendered into the worker overlays), the `[[gate]]` table (the named
build recipes rendered into the skill's gates reference, `focused-tests` among them) and the
`[[guard.shape]]` table (the build shapes the test worker's Bash may run, 5.6). See the example
harnesses in `defaults/harnesses/`. What a Maven monorepo harness typically encodes:

| Gate | Rule |
|---|---|
| exit codes | `<build> ; echo $? > rc` and `ppl` reads the file. A piped build returns the exit code of the pipe tail, so a red build reads as green. Do not trust a model's summary of build output [R-EVIDENCE-RC-FILE]. |
| worktree builds | `-Dmaven.gitcommitid.skip=true` where the git-commit-id plugin does not resolve a worktree `.git` file |
| offline | `-o` where the artifact repository is unreachable |
| JDK | declared in the harness `[env]` table (`JAVA_HOME`) |
| module selection | `mvn -pl :artifactId -am`, not a bare `-pl`, so modules resolve through the reactor, not `.m2` |
| stale artifacts | after a branch switch, `mvn install -pl <module> -am -DskipTests` first, or ITs die with `NoSuchMethodError` |
| IT reports | where failsafe reports land (the root `target/` or the module's), stated in the harness's `skill/` fragments |
| lint blind spot | when the lint plugin audits main sources only, a green build does not audit test sources, so the test-worker gate runs its own non-ASCII / tab / CRLF check over the test diff |
| test config poisoning | a test-source `@Configuration` with its own `@ComponentScan` drops Boot's `TypeExcludeFilter` and is scanned into every `@SpringBootTest` context, so the gate flags any new `@Configuration` under test sources |
| secret scan | before every push, over the full diff |

Only the exit-code rule has code behind it, the rest are recipes rendered into the skill
[R-GATE-REPO-RECIPES n/a].

## 12. Budgets, routing, risk

### 12.1 Budgets

- dispatch budget per stage attempt: 21 normal, 34 hard cap. At 21 the orchestrator decides
  between parking with a clean handoff and finishing a safe atomic piece. The hard cap is
  enforced by `ppl`, which refuses `ppl stage enter` and `ppl transition` with
  `BUDGET_EXHAUSTED` once `dispatch_used` reaches 34 [R-BUDGET-DISPATCH-CAP]. `ppl park` stays available, since
  parking with a handoff is the designed way out of an exhausted budget. **Confirmed, not merely designed:** `SubagentStart` is an
  informational Claude Code hook event and cannot block a dispatch - the JSON deny document
  `ppl_dispatch.py` returns past the cap is never acted on by the harness, regardless of its
  shape [R-DISPATCH-HOOK-CANNOT-BLOCK]. The hook
  still runs (it is wired to `SubagentStart`, README.md "Hooks"), but only as a
  best-effort counter and telemetry source, never as a gate [R-DISPATCH-HOOK-CANNOT-BLOCK]. The counter
  counts on `agent_type` alone, the same scoping `ppl_guard.py` uses for a role: the documented
  layout (5.4) puts the orchestrator's session - whose cwd every subagent inherits - beside the
  worktree, so a counter scoped by cwd would stay at zero and the cap at `ppl stage enter`
  would compare against nothing [R-BUDGET-DISPATCH-CAP]. `ppl`'s own refusal at stage
  boundaries and transitions is not merely "the guarantee" alongside an "early cut" - it is
  the *only* enforcement of this budget that exists [R-BUDGET-DISPATCH-CAP].
- retry budget: 3 full rework cycles per task, then escalate. Three failed convergences is
  not bad luck.
- cost budget: `usd_max` per task, and optionally per role, for any provider billed by API
  rather than by subscription window. Enforced by `ppl budget spend usd` [R-BUDGET-USD], which the
  orchestrator calls before a dispatch rather than after - an ordering no code checks
  [R-BUDGET-USD-BEFORE-DISPATCH n/a]. Zero means unlimited only for subscription providers, which have no dollar
  meter.
- format violations: three schema failures from the same role and its output is discarded
  as unusable, with the routing recorded as a signal.
- context: on approaching the limit, the orchestrator parks with an exact handoff rather
  than compacting through a transition.

If a budget is exhausted the pipeline halts explicitly. It does not quietly continue with
a different model to finish the job.

### 12.2 Routing

| Role | Tier | Effort |
|---|---|---|
| ppl-briefer | good | high |
| ppl-brief-challenger | good | xhigh |
| ppl-code-worker | average (`good` for a `--risk high` run) | high |
| ppl-test-worker | average | high |
| ppl-verifier | good | xhigh |
| ppl-review-responder | average | high |

This table is the v1 content of `defaults/routing.example.toml`, which `ppl setup` copies to
`~/.ppl/routing.toml`, the single source of truth. The operator picks the model each tier
names, and the agent files are generated from it by `ppl harness apply` (section 10.1). The
orchestrator is the operator's own session and is not routed. A role can carry a per-risk
override, `by_risk = { high = "good" }`, applied for runs started with `ppl init --risk high`:
the agent file keeps the default and `ppl routing show --role <role> --task <id>` prints the
model the orchestrator passes at dispatch [R-ROUTING-BY-RISK]. `model` and `effort` are Claude
Code subagent frontmatter keys, and `effort` accepts `low|medium|high|xhigh|max`. Routing is a
profile, not part of the canonical workflow - changing a model is not supposed to require
changing the lifecycle or the evidence contracts.

### 12.3 Risk tiers

Two tiers only, per the author's correction to the book's four:

- normal: focused tests plus independent verification (the default)
- critical: data integrity, security, migrations, public contracts, money-adjacent paths.
  Adds an adversarial verification pass and forbids any parallelism. In v1 a critical task
  is flagged and escalated rather than auto-run, since the adversarial verifier is deferred.

## 13. Failure vocabulary and escalation

Free text does not route. Closed dictionary:

| Code | Meaning | Route |
|---|---|---|
| `SPEC_GAP` | governing requirements do not define the behavior | owner decision, then written back into the spec |
| `CONTEXT_INSUFFICIENT` | the answer exists but the brief did not carry it | re-brief once, then `SPEC_GAP` |
| `IMPLEMENTATION_FAILURE` | code does not satisfy the contract | bounded rework, new SHA |
| `TEST_FAILURE` | the test is wrong or incomplete | test-worker rework |
| `BASELINE_FAILURE` | the problem lives on the base revision | park, escalate with provenance, do not mask |
| `BOUNDARY_VIOLATION` | a role exceeded its authority | halt; the hook already blocked the write |
| `BUDGET_EXHAUSTED` | dispatch/retry/context limit reached | park with handoff |
| `OWNER_DECISION` | material decision required | halt that branch of work |
| `EXTERNAL_BLOCKED` | forge/tracker/CI unavailable or a dependency is blocked | park, retry on next tick |
| `SETUP_INCOMPLETE` | `ppl setup` has not recorded a step `ppl init` needs [R-INIT-SETUP-COMPLETE] | run the named `ppl setup <step>`, no run exists yet |
| `MODELS_UNVERIFIED` | a model the run would route to is unverified or stale [R-INIT-MODELS-VERIFIED] | `ppl models verify`, or `--skip-model-check` |
| `HARNESS_NOT_APPLIED` | the harness `ppl init` loads is not the active one [R-INIT-HARNESS-APPLIED] | `ppl harness apply <name>` |
| `HARNESS_INVALID` | a harness file fails validation | fix it, then `ppl harness validate <name>` |
| `TIMED_OUT` | no `ppl` activity was recorded since the run's inactivity deadline (6.9) | `ppl recover`, inspect the stuck attempt, then resume or re-brief |
| `VERIFICATION_UNAVAILABLE` | `ppl verdict record` parks the run [R-VERIFICATION-UNAVAILABLE-PARK]: the second consecutive INCONCLUSIVE/ERROR/CANCELLED/TIMEOUT verdict on the same `candidate_sha` (6.10) | inspect why the verifier keeps failing to reach a result on this sha, never just redispatch a third time |

A gap is not closed silently in code. Closing it in code merely means the next model sees
the same empty space and could statistically pick different behavior. The gap is closed by
writing the decision into the spec, which invalidates dependent briefs and approvals.

Frozen-module rule: if an integration test dies outside the write boundary, it stays red.
The pipeline records the failure with provenance, escalates, and continues on independent
branches of work. Editing a test to produce a green screen is prohibited.

## 14. v1 done criteria

The build is finished when all of these are demonstrably true:

1. After a kill and restart mid-run, the task resumes from durable state with no chat
   history. **Verified**: a run driven to `gates`, its process SIGKILLed,
   and a fresh process's `ppl recover` reported stage `gates` and a head SHA read from `git`
   that matched both the worktree and `log.jsonl`, with no shared memory with the killed
   process.
2. `ppl-code-worker` physically cannot write a test file [R-ROLE-CODE-WORKER-TESTS];
   `ppl-test-worker` physically cannot write a main source file
   [R-ROLE-TEST-WORKER-MAIN]. Verified by an attempt, not by reading the prompt.
   **Verified** at hook level (`tools/cage_check.py`): `hooks/ppl_guard.py` was
   driven as a subprocess, exactly the way Claude Code's `PreToolUse` hook contract invokes
   it - JSON payload on stdin, decision read off stdout - against a real temporary git
   worktree, and denied both attempts with `BOUNDARY_VIOLATION`
   [R-ROLE-CODE-WORKER-TESTS] [R-ROLE-TEST-WORKER-MAIN]. This is a hook-level proof,
   not a live subagent dispatch: `cage_check.py` registers no hook, so the criterion is
   exercised end to end only by a real run with the hooks registered (README.md "Hooks").
3. `ppl-verifier` cannot modify the candidate [R-ROLE-READONLY-NO-WRITE]
   [R-ROLE-TOOLS-READONLY]. **Verified** the same way as criterion 2: the
   role has no `Edit`/`Write`/`Bash` tool in its generated agent file at all
   (`routing.BASE_TOOLS["ppl-verifier"]` grants only `Read, Grep, Glob`, and
   `routing.role_tools` adds none), and `cage_check.py`
   additionally confirmed the guard hook denies a `Write` from `ppl-verifier` even if
   attempted, as a second layer [R-ROLE-READONLY-NO-WRITE].
4. An approval goes stale automatically when the head SHA or the spec revision changes.
5. Every external mutation has a receipt and a replay policy.
6. Exceeding a budget produces an explicit halt with an owner and a resume route.
7. `ppl status` shows one comprehensible card: outcome, stage, proof, risk, budget, whether
   the owner is needed, next action.
8. One real task has gone end to end: task link -> MR -> at least one reviewer remark
   handled -> approval. This needs a real tracker issue and a real human reviewer on a real
   merge request - the operator's to run, not something a coding session can fake without
   invalidating the result - and it is a run under the cage only with the hooks registered
   (README.md "Hooks").
9. The panel lists every run, and pause takes effect within one tool call. Stop produces a
   valid parking record. Destroy prompts for scope and a typed confirmation before it sends
   anything [R-PANEL-TYPED-CONFIRM] - a page-level check a direct POST bypasses, so remote
   state is protected by R-DESTROY-REMOTE-QUEUED underneath it, not by the dialog alone.
10. The panel's SIGTERM path refuses to signal when the recorded PID's start time does not
    match [R-PANEL-SIGTERM-PID].
11. Changing a model is a one-line edit of a tier in `routing.toml`, then `ppl models verify`
    and `ppl harness apply`; no agent file is edited by hand and no workflow step changes.
12. A worker's output is applied through `ppl apply-patch`, and a patch touching a path
    outside the boundary is rejected there, independently of any hook
    [R-APPLY-PATCH-BOUNDARY]. **Unexercised in
    v1** (section 10.1): `apply-patch` is implemented and unit-tested, and rejects an
    out-of-boundary patch when called, but no v1 workflow step calls it - the single
    provider is the Claude Code subagent, whose workers write the tree directly under the
    guard hook. The criterion is met as code and not met as a live path. It becomes
    exercisable when the first non-subagent adapter lands.
13. Every evidence receipt and verdict carries `producer`, so results can be attributed to
    the routing profile that produced them.

## 15. Known limitations

- **Single model family.** The portfolio is all Claude, so the blind spots of the code
  worker and the test worker are correlated in a way that different vendors would not be.
  Compensation is role isolation, disjoint contexts (the test worker is not supposed to see
  the diff, but nothing structural stops the orchestrator pasting it into the dispatch
  prompt [R-TEST-WORKER-NO-DIFF n/a])
  and deterministic gates between roles. This is a stated limitation, not an oversight.
- **Worktree is not a sandbox.** It isolates changes. A process can still read credentials
  or write outside the tree if the OS permits. OS-level isolation is not part of v1.
- **The orchestrator is a model.** Determinism comes from `ppl` refusing illegal
  transitions and hooks refusing illegal writes, not from the orchestrator's discipline.
  Every rule that matters is expected to live in code, not merely in the skill prompt.
- **The done-gate mostly checks documents the orchestrator itself authored.** Section 1
  states the aspiration - transitions decided against artifacts, not against a model's
  account of how well it did - and that aspiration stands. What the gate delivers today is
  narrower, and the difference should be read plainly. `verdict.record` writes whatever
  document it is handed; `evidence.add` trusts an exit-code file the same session wrote;
  the acceptance-to-evidence map inside a verdict names checks whose receipts that session
  also filed. A model that is wrong in a self-consistent way passes. What the gate does
  provide is (a) consistency between those documents, which catches drift, staleness and
  omission - a verdict for the wrong SHA, an acceptance id with no receipt, a receipt
  claiming `pass` against a non-zero rc - and (b) exactly two facts nothing in the run
  authored: `git rev-parse HEAD` in the run's repo, which binds every receipt and verdict
  to a real commit [R-DONE-SHA-BINDING], and `git diff --name-only <base_sha>..HEAD`, which is why `base_sha` is
  write-once and ancestry-checked. `ppl evidence add` defaults its `--sha` to the first of
  those rather than requiring it from the caller, as cheap partial hardening; an explicit
  `--sha` still wins. Genuine
  independence needs a checker that does not share a session with the checked - a CI run,
  or a verifier on separate credentials. That is post-v1.
- **Subscription window is the hard budget.** Long verification passes at xhigh effort can
  stall on rate limits. Parking on budget exhaustion is the designed response.
- **v1 is one lane.** Throughput is one task at a time by design; independence must be
  proven before parallelism is added.
- **The panel cannot start work [R-PANEL-NO-SPAWN n/a].** It observes and it halts a run; it queues a resume. Beginning
  or continuing a run requires a Claude session, because the review-monitor loop needs an
  interactive or main-loop session to hold its wake-ups.
- **Pause is cooperative, not preemptive.** It lands within one tool call, which is fast but
  is not an instant halt. SIGTERM is the preemptive option and costs the in-flight step.
- **Non-Claude adapters are unbuilt in v1.** The contract, the routing config and the
  patch-based enforcement exist; the API and CLI adapters do not. Until one is written, the
  claim that a role can move to another provider is a design property, not a tested one.

## 16. Deferred work (post-v1)

- P2: separate adversarial verifier for critical tier, mutation testing on a schedule,
  per-role credentials
- P3: dependency matrix and waves, multiple isolated lanes, model eval flywheel with
  recorded fixtures, routing updates driven by measured rework rates
- adapter implementations: `claude-api`, `openai-compatible`, `cli`. The contract in
  section 10.2 is frozen so these are additive.
- panel: per-run cost and token charts, side-by-side routing-profile comparison, one-click
  re-run of a stage under a different profile
- health dashboard: `ppl metrics` (6.11) delivers defect escape rate, false FAIL rate,
  incomplete check rate, cost per accepted task, time to acceptance and human minutes,
  sliced by `producer`, reading straight from `log.jsonl` and `verdicts/*.json`. Still
  deferred: a panel visualization of it, accepted-per-attempt rate, hook-hit counts per
  role and code, and parking reasons

## 17. Constitution

1. Facts outrank the agent's account of itself.
2. The specification outranks the brief and the chat history.
3. One agent, one functional role.
4. Rights are minimal and enforced by code, and every such claim in this document names
   its row in `rules.toml` [R-DOC-RULE-IDS].
5. The checker does not repair the checked.
6. Strictness is proportional to the cost of an error.

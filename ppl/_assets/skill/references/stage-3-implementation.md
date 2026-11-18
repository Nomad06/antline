# Stage 3 - implementation

Do not run the run's first `ppl stage enter implementation` until the baseline suite has
finished: every baseline command of Stage 1 has written its `baseline-<gate>.rc` file. If a
baseline command has no `baseline-<gate>.rc` file, run it now, as
`references/stage-1-baseline.md` says. A code worker writing into the tree
under a running baseline suite would contaminate it [R-BASELINE-SUITE-BEFORE-IMPLEMENTATION n/a].

`ppl stage enter implementation`, dispatch `ppl-code-worker` with the brief and its
boundary. Pass the model `ppl routing show --role ppl-code-worker --task <TASK-ID>` prints:
`routing.toml` routes a role to a stronger model for the run's risk tier (`ppl init --risk
high`), and the generated agent file keeps the default, so the override only takes effect
when you pass it at dispatch [R-ROUTING-BY-RISK]. It writes directly inside `allowed_write_paths`; the guard hook denies anything
else [R-ROLE-CODE-WORKER-BOUNDARY] - **if `ppl doctor` reported the hook registered**
(`local`, or an accepted `both`). If it reported `none` or `unknown`, nothing
stops this worker from writing outside `allowed_write_paths` except the brief you dispatched
it with [R-LANE-BARE-RESUME n/a]; review its diff against the boundary yourself before
committing. Commit its work.

The tests half of this stage follows in `references/stage-3-tests.md`, with a different agent.

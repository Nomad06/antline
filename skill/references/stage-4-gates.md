# Stage 4 - gates

`ppl stage enter gates`. Run the gates from `references/gates.md` in order, cheapest first.
Every gate writes its exit code to a file, and every result becomes a receipt recorded under
the current candidate SHA with the check name from that file - use these names exactly,
because two of them are load-bearing:

```bash
ppl evidence add --check focused-tests --result pass --sha $SHA --log <log> --rc <rc file> \
  --generation $GENERATION --from orchestrator
ppl evidence add --check secret-scan --result pass --sha $SHA --log <log> --rc <rc file> \
  --generation $GENERATION --from orchestrator
```

`focused-tests` and `secret-scan` are the two checks `ppl transition done` refuses to close
without, by their exact names - a passing receipt filed under any other name does not
satisfy them [R-DONE-SECRET-SCAN]. Record the rest of the gates the same way under their own check names (see
`references/gates.md`); every acceptance ID also needs its own passing receipt, referenced
later by the verifier as `evidence:<check-name>`.

A gate that fails inside the boundary routes to rework: re-enter Stage 3 with `ppl stage enter
implementation`, which names `references/stage-3-implementation.md`, or `ppl stage enter
tests` and `references/stage-3-tests.md` when a test is at fault, and dispatch a fresh worker
there. A gate that fails outside the boundary is `BASELINE_FAILURE`: park it, escalate with provenance, and do not edit a test to
make it green.

Once the gates are green, run the simplification pass before verification: spend a dispatch
and dispatch the `code-simplifier` agent inside `allowed_write_paths` (main sources only,
tests stay as the test worker left them) with the brief's `name_contract`, asking for the
smallest diff that keeps every contract name. If it changes anything, commit, which is a new
candidate SHA, and run the gates again in full. File the receipt either way: `--check simplification --result pass`
when it ran, `--result skip` with the reason in its log when there was nothing to simplify
(a docs-only change, a diff already at the challenge's shape). `ppl transition done`
refuses a head without that receipt [R-DONE-SIMPLIFICATION-RECEIPT].

A new commit means a new candidate SHA, and every receipt and verdict is filed under the SHA
that produced it - a receipt from the previous SHA proves nothing about this one. After any
rework, gates run again in full and evidence is recorded again under the new SHA; there is
no partial credit across a SHA change.

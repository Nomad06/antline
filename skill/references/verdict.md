# The verdict schema

`ppl verdict record <file> --from ppl-verifier` validates a JSON document against `schema.VERDICT_FIELDS`
before binding it to the current head SHA. Every field below is required - `ppl verdict
record` refuses `SCHEMA_INVALID` on the first one missing or of the wrong type, not on all
of them at once, so composing the verdict without this table costs one round trip per
mistake, and the second mistake in a row parks the run with `FORMAT_RETRY_EXHAUSTED` [R-REFUSAL-RETRY-CAP] (D3, cycle 2 wave D: this is the same defect class the brief schema already had -
`references/brief.md` - on a second artifact the earlier fix did not generalise to).

| Field | Type | Shape |
|---|---|---|
| `task_id` | string | the task id, matching the run |
| `candidate_sha` | string | the commit this verdict is about. `ppl verdict record` refuses a verdict produced for a different SHA than the current head [R-DONE-SHA-BINDING] |
| `spec_revision` | string | copied byte-for-byte from the `spec_revision` string `ppl brief set` printed (or `ppl show --json` reports now) - not computed or guessed. A verdict carrying a different value is refused as stale once a rebrief has moved the binding [R-BRIEF-SPEC-REVISION-STALE] |
| `verdict` | string, one of `PASS \| REWORK \| BLOCKED \| OWNER_DECISION \| INCONCLUSIVE \| ERROR \| CANCELLED \| TIMEOUT` | only `PASS` can pass the done gate; every other value is refused as not-PASS [R-DONE-VERDICT-PASS]. `INCONCLUSIVE`/`ERROR`/`CANCELLED`/`TIMEOUT` name a check that did not happen - not evidence the code is bad, and not evidence it is good either [R-VERDICT-OUTCOME-VOCAB] |
| `acceptance_evidence` | object (dict) | maps every acceptance ID to a string of the exact form `evidence:<check-name>` - letters, digits, dot, underscore and hyphen only, nothing else, not even a space. `evidence:integration tests` is invalid; `evidence:integration-tests` is not [R-ACCEPTANCE-EVIDENCE-FORMAT] |
| `findings` | list, must be empty | findings are recorded separately via `ppl findings add`, bound to this same `candidate_sha`, not embedded in the verdict document. A non-empty list is refused [R-FINDINGS-VERDICT-FIELD-RETIRED] - record every finding first, then the verdict |
| `producer` | object (dict) | who produced this verdict - free-form, no shape check beyond "is an object". `{"role": "verifier"}` is enough |

Everything else about the verdict - what counts as a blocking finding, when to use
`INCONCLUSIVE` over `PASS`, the dedupe rule against already-open findings - is
`templates/ppl-verifier.md.body`'s job, not this file's: this table is the schema `ppl
verdict record` actually checks, not the judgment calls the verifier role makes composing
it. See `references/gates.md` for the receipts a verdict's `acceptance_evidence` entries
point at, and `references/stage-5-verification.md` for the surrounding sequence (findings
first, verdict after).

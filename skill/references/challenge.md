# The brief challenge report

`ppl brief challenge <file> --generation <n> --from ppl-brief-challenger` validates a JSON document against
`schema.CHALLENGE_FIELDS`, checks it against the run, writes `challenge.json` into the run
directory and binds `state["brief_challenge"]` to the report's `spec_revision`
[R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION]. Every field
below is required.

| Field | Type | Shape |
|---|---|---|
| `task_id` | string | the Jira task id, matching the run |
| `spec_revision` | string | copied byte-for-byte from `ppl brief set`; a report written against another value is refused, the brief changed under the challenger [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION] |
| `verdict` | string | `ACCEPTED` or `REVISE` |
| `new_classes` | list | one `{"name": str, "existing_owner": str \| null, "justified": bool}` per class, interface, controller, service, manager or repository the `name_contract` introduces |
| `wire_shapes` | list | one `{"shape": str, "matches_spec": bool}` per request body, response node and endpoint path the contract freezes, compared with the spec text word for word |
| `open_decisions` | list | one `{"question": str, "cost_to_close_files": int, "decision": "in_scope" \| "parked" \| "owner"}` per open question, the brief's own and the spec's parked ones |
| `producer` | object | free-form, `{"role": "brief-challenger"}` is enough |

`ACCEPTED` is refused `SCHEMA_INVALID` when any class is `justified: false`, any shape is
`matches_spec: false`, or any decision is `parked` at five files or fewer
[R-BRIEF-CHALLENGE-SELF-CONSISTENT] [R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP]. `REVISE` may
carry all of those: that is what it is for. After a `REVISE`, re-brief with the report's
findings (`ppl brief set` rebinds `spec_revision` when the contract changes) and dispatch the
challenger again.

`ppl stage enter implementation` from briefing, and `ppl transition done`, both read
`run.challenge_current`: an `ACCEPTED` verdict whose `spec_revision` equals the run's current
one [R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION]. A resume from parking into implementation does
not re-check it, the same way the baseline guard is not re-checked.

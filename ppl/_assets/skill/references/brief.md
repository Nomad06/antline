# The brief schema

`ppl brief set <file> --from ppl-briefer` validates a JSON document against `schema.BRIEF_FIELDS` before
writing `brief.json` and deriving `boundary.json` from it. Every field below is required -
`ppl brief set` refuses `SCHEMA_INVALID` on the first one missing or of the wrong type, not
on all of them at once, so composing the brief without this table costs one round trip per
mistake, and the second mistake in a row parks the run with `FORMAT_RETRY_EXHAUSTED`
[R-BRIEF-SCHEMA] [R-REFUSAL-RETRY-CAP].

| Field | Type | Shape |
|---|---|---|
| `task_id` | string | the task id, matching the run |
| `purpose` | string | why this task exists |
| `success_outcome` | string | what "done" looks like in one sentence |
| `governing_requirements` | list | the requirement sources this brief was built from - free-form entries, no shape check beyond "is a list" |
| `acceptance` | list of non-empty strings | stable acceptance IDs, e.g. `["AC1", "AC2"]`. Each entry must be a string with non-blank content - `[{"id": "AC1", "text": "..."}]`, a nested list, or a blank string is refused `SCHEMA_INVALID` at `ppl brief set`, not five stages later at the done gate (E1) [R-BRIEF-SCHEMA]. The list itself must not be empty [R-BRIEF-SCHEMA]. Feeds `spec_revision` (see `name_contract` below) - dropping an id, even one already proven irrelevant, rebinds `spec_revision` and stales any verdict recorded before the change [R-BRIEF-SPEC-REVISION-STALE]. |
| `allowed_write_paths` | list | glob patterns; becomes `boundary.write`. Must not be empty, and must not overlap `forbidden_paths` - any pattern present in both lists is refused [R-BRIEF-SCHEMA]. Feeds `spec_revision` too, same as `acceptance` - widening or narrowing it after a verdict was recorded stales that verdict [R-BOUNDARY-SPEC-REVISION-STALE]. |
| `forbidden_paths` | list | glob patterns; becomes `boundary.forbid`, checked ahead of the write boundary at every write and at the done gate. Feeds `spec_revision` too, same as `allowed_write_paths`. |
| `verification` | list | what the verifier is meant to check, free-form entries |
| `open_decisions` | list | entries are either bare strings (legacy) or objects `{"question": str, "cost_to_close_files": int, "decision": "in_scope" \| "parked" \| "owner"}`. An object with `decision: parked` and `cost_to_close_files` of five or fewer is refused `SCHEMA_INVALID` [R-BRIEF-OPEN-DECISION-NOT-PARKED-CHEAP]. Not part of `spec_revision` |
| `name_contract` | non-empty object (dict) | the frozen interface: method signatures, DTO field names, error codes, units, boundary rules. `ppl brief set` derives `spec_revision` as a hash of this field together with `acceptance` and the write/forbid boundary (see Stage 2) - an empty object is refused, because there is nothing to freeze [R-BRIEF-SCHEMA]. `purpose`, `success_outcome`, `governing_requirements`, `verification` and `open_decisions` are NOT part of the hash: none of them is read by the done gate, so correcting one in a re-brief does not stale an outstanding verdict. |

Everything else about the brief - what a good acceptance ID looks like, how narrow
`allowed_write_paths` should be, why `name_contract` exists - is `templates/
ppl-briefer.md.body`'s job, not this file's: this table is the schema `ppl brief set`
actually checks, not the judgment calls the briefer role makes composing it.

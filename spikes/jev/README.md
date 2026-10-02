# Spike: TypeSafe Jev as a typed-decision accelerator

Date: 2026-09-22. Status: shape proven offline, model quality unmeasured.

## What this proves, and what it does not

Proven, with tests under `tests/test_spike_jev.py` (fake transport, no network):

- ppl's closed vocabularies (`review.CLASSIFICATIONS`, `run.BLOCKER_CODES`, the two
  DESIGN 10.3 risk tiers) encode losslessly as Jev `choice` questions.
- The fetched response shapes parse, including the noul example that carries no
  `confidence` and the quickstart whose response key differed from its request key.
- Every failure - no key, transport error, non-2xx, garbage body, missing answer,
  low confidence - resolves to `fallback`, which means "do what the pipeline does
  today". Risk tier fails toward `critical`, admission nouls toward "not satisfied".
- A decision lands as a receipt in the field vocabulary `evidence.add` and DESIGN 10.2
  already use (`producer` = provider/model/params_hash, `usage`).

Not proven: whether Jev classifies pipeline inputs correctly. No API key was present
in this session, so zero live calls were made. `python3 -m spikes.jev.spike --live`
runs the two synthetic samples once `TYPESAFE_API_KEY` or `OPENROUTER_API_KEY` is
exported in the operator's own shell. The thresholds in `gate.py` are placeholders
for that reason - TypeSafe's own docs say the right values are domain-specific and
measured.

## The contract, as fetched

| Item | Value | Source |
|---|---|---|
| Endpoint | `POST https://api.typesafe.ai/v1/systemone` | docs.typesafe.ai quickstart |
| Via OpenRouter | `POST https://openrouter.ai/api/v1/systemone`, model `typesafe/jev-1.13` | openrouter.ai typesafe-sdk guide |
| Auth | `Authorization: Bearer <key>`, key from console.typesafe.ai/keys or OpenRouter | quickstart |
| Request | `{model, state, questions: {name: {type, instructions, criteria}}}` | quickstart, primitives/choice |
| Question types | `choice` (up to 255 options), `noul` (yes/no probability), `score` (ordered levels) | primitives |
| Choice response | `{type, choice, probabilities: {option: p}, confidence: 0..1}` | primitives/choice |
| Noul response | `{type: "noul", noul: 0..1}` - no confidence field shown | quickstart |
| Confidence | statistic over the probability spread, 1.0 = all mass on one option | docs/confidence |
| Usage | `{input_tokens, output_tokens}` direct, plus `cost` via OpenRouter | quickstart, sdk guide |
| Pricing | $0.042 per M input tokens, $0 output, 32K context | openrouter.ai/typesafe/jev-1.13 |
| Response model id | `jev-1.13.0` when `jev-latest` was requested | quickstart |
| Python SDK | `pip install typesafe-sdk`, `TypeSafeClient().system_one(...)` | quickstart |
| Latency | "70-500 ms in its tests" - vendor figure, not measured here | jevai.dev |

Cost at that price: a request with all five spike questions is roughly 600 input
tokens, about $0.000025. A thousand review threads is a few cents. Cost is not the
constraint.

Not readable this session (pages returned 404): the HTTP API reference, so the error
body shape, rate limits and maximum `state` size are unknown, and the "Jev 1.13
jaggedness" page, so the vendor's own list of failure modes is unknown. The client
treats every unknown as a `None` and falls back.

## Privacy finding - this is the gate, not the code

docs.typesafe.ai/legal states a "commitment not to train models on user data" and
offers zero data retention for enterprise customers via privacy@typesafe.ai. The
retention duration for the default plan is in the Data Processing Agreement, which
this spike did not read. Via OpenRouter, OpenRouter's own retention applies on top.

DESIGN section 3 makes source egress to a third-party provider default-deny with a
per-provider opt-in, and `routing.check_egress` only inspects `context_files`. Every
input this spike would send - a reviewer remark, a ticket description, a verifier
finding - is company data that is not a context file, so the current check would not
see it. Sending any of it is an owner decision. The spike ships synthetic samples
only for that reason.

## Where Jev fits ppl, and where it must not

Fits (closed vocabulary, today decided by orchestrator judgement or a full dispatch):

1. Review-thread classification into the four `CLASSIFICATIONS`. Per the responder
   template, `NEEDS_CODE_CHANGE` still needs a `change_request` sentence and
   `NEEDS_REPLY_ONLY`/`DISAGREE` still need a `draft_reply`, so a confident Jev answer
   skips the `ppl-review-responder` dispatch only for `NEEDS_OWNER`. For the other
   three it narrows the prompt to one known class instead of four - a smaller,
   cheaper dispatch, not a skipped one.
2. Risk tier at admission. DESIGN 10.3 names two tiers, SKILL Stage 0 asks for one,
   nothing computes it. Uncertain resolves to `critical`.
3. Admission checklist nouls (acceptance stable, decisions closed). Uncertain
   resolves to not satisfied, which is `SPEC_GAP`.
4. `BLOCKED` verdict finding text to a `ppl park --blocker` code.

Must not: the guard hook (lexical, fail-closed, 40-80ms, deterministic by
constitution), the done gate, FSM transitions, `spec_revision`, the verifier's own
PASS/REWORK judgement, the secret scan, the baseline-delta classification.

## What a real integration costs

- Jev is the first non-subagent provider. `routing.dispatch` raises `SCHEMA_INVALID`
  for any kind but `subagent`, so the DESIGN 10.2 adapter contract becomes code.
- `check_egress` has to learn that ticket and remark text is egress too.
- Every decision is a receipt with producer, probabilities and confidence, or it is an
  unlogged model judgement steering the run.
- The USD budget goes live: `R-BUDGET-USD-BEFORE-DISPATCH` is a GAP only because no
  billed provider exists yet.

## Running it

    cd ~/antline                                                           # the checkout
    python3 -m spikes.jev.spike --dry-run                                  # requests only
    python3 -m spikes.jev.spike --replay spikes/jev/fixtures/choice_confident.json
    TYPESAFE_API_KEY=... python3 -m spikes.jev.spike --live                # two synthetic samples
    python3 -m unittest tests.test_spike_jev

"""Shape a Jev decision the way ppl already records model output.

Field vocabulary follows what evidence.add stores and what DESIGN 10.2 names
for an adapter's output, so turning this spike into a real adapter is a
rename, not a redesign: `producer` is {provider, model, params_hash}, and
`usage` carries input_tokens, output_tokens and cost only when the transport
reported them. The model in `producer` is the one the RESPONSE named
(jev-1.13.0), not the alias the request sent (jev-latest).
"""
import hashlib
import json

from . import client


def _params_hash(questions):
    blob = json.dumps(questions, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


def build(question_name, answer, gate_result, raw_response, questions, provider="typesafe"):
    usage = client.usage_of(raw_response)
    model = (raw_response or {}).get("model")
    return {
        "check": "jev-%s" % question_name,
        "producer": {"provider": provider,
                     "model": model if isinstance(model, str) else "unknown",
                     "params_hash": _params_hash(questions)},
        "answer": answer.as_dict() if answer is not None else None,
        "gate": gate_result,
        "usage": usage,
    }

"""Minimal stdlib client for POST /v1/systemone.

Contract as fetched on 2026-09-22 from docs.typesafe.ai and openrouter.ai:

  request  {"model": "jev-latest", "state": "<text>",
            "questions": {"<name>": {"type": "choice|noul|score",
                                     "instructions": "...", "criteria": {...}}}}
  response {"model": "jev-1.13.0",
            "answers": {"<name>": {"type": "choice", "choice": "...",
                                   "probabilities": {...}, "confidence": 0.0-1.0}
                        | {"type": "noul", "noul": 0.0-1.0}},
            "usage": {"input_tokens": n, "output_tokens": n[, "cost": usd]}}

Two transports carry the same wire protocol: TypeSafe direct
(https://api.typesafe.ai, TYPESAFE_API_KEY) and OpenRouter
(https://openrouter.ai/api, OPENROUTER_API_KEY, model id prefixed typesafe/).

Fail closed by design: any transport error, non-2xx, timeout or unparseable
body returns None and the caller falls back to the existing subagent path.
Jev is an accelerator here, never a dependency. Unknowns the fetched docs did
not state (error body shape, rate limits, max state size) are handled by the
same None, not guessed at.
"""
import json
import os
import urllib.error
import urllib.request

DIRECT_BASE = "https://api.typesafe.ai"
OPENROUTER_BASE = "https://openrouter.ai/api"
PATH = "/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT_SECONDS = 10


def credentials():
    """(base_url, api_key, model) from the environment, or None when no key is set.

    TypeSafe direct wins when both keys are present. OpenRouter needs the
    vendor-prefixed model id.
    """
    direct = os.environ.get("TYPESAFE_API_KEY")
    if direct:
        return DIRECT_BASE, direct, os.environ.get("JEV_MODEL", DEFAULT_MODEL)
    via = os.environ.get("OPENROUTER_API_KEY")
    if via:
        model = os.environ.get("JEV_MODEL", "typesafe/" + DEFAULT_MODEL)
        return OPENROUTER_BASE, via, model
    return None


def build_request(state, questions, model=DEFAULT_MODEL):
    """The exact JSON document POSTed. Kept separate so --dry-run and the
    tests see the same bytes the wire would."""
    if not isinstance(state, str) or not state.strip():
        raise ValueError("state must be a non-empty string")
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a non-empty dict")
    return {"model": model, "state": state, "questions": questions}


def http_post(base_url, api_key, body, timeout=DEFAULT_TIMEOUT_SECONDS):
    """Transport. Returns the decoded JSON object or None on any failure."""
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + PATH, data=data, method="POST",
        headers={"Authorization": "Bearer %s" % api_key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return None
            raw = response.read().decode("utf-8")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


class Answer:
    """One parsed answer. `probabilities` is empty for a noul, whose single
    probability sits in `noul`. `confidence` is None when the response did
    not carry one (the fetched noul example did not)."""

    def __init__(self, kind, choice=None, probabilities=None, confidence=None, noul=None):
        self.kind = kind
        self.choice = choice
        self.probabilities = dict(probabilities or {})
        self.confidence = confidence
        self.noul = noul

    def as_dict(self):
        return {"type": self.kind, "choice": self.choice,
                "probabilities": self.probabilities, "confidence": self.confidence,
                "noul": self.noul}


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_answers(doc):
    """{question_name: Answer} from a response document, tolerant of shape.

    Keyed by whatever the RESPONSE carries: the fetched quickstart shows
    `urgency` in the request and `is_urgent` in the response, so the echo of
    request keys is treated as unconfirmed. A malformed answer is dropped,
    not guessed at - a missing answer resolves to fallback downstream.
    """
    out = {}
    answers = (doc or {}).get("answers")
    if not isinstance(answers, dict):
        return out
    for name, raw in answers.items():
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "choice":
            probabilities = raw.get("probabilities")
            if not isinstance(probabilities, dict):
                continue
            clean = {k: _number(v) for k, v in probabilities.items()}
            if any(v is None for v in clean.values()):
                continue
            choice = raw.get("choice")
            if not isinstance(choice, str) or choice not in clean:
                continue
            out[name] = Answer("choice", choice=choice, probabilities=clean,
                               confidence=_number(raw.get("confidence")))
        elif kind == "noul":
            value = _number(raw.get("noul"))
            if value is None:
                continue
            out[name] = Answer("noul", noul=value, confidence=_number(raw.get("confidence")))
        elif kind == "score":
            value = _number(raw.get("score"))
            if value is None:
                continue
            out[name] = Answer("score", choice=value, probabilities=raw.get("probabilities") or {},
                               confidence=_number(raw.get("confidence")))
    return out


def usage_of(doc):
    """input/output tokens and cost when the transport reported them.
    Output is billed at zero per the fetched pricing, so cost is input-only
    when computed by the caller. `cost` appears only via OpenRouter."""
    usage = (doc or {}).get("usage")
    if not isinstance(usage, dict):
        return {}
    out = {}
    for key in ("input_tokens", "output_tokens", "cost"):
        value = _number(usage.get(key))
        if value is not None:
            out[key] = value
    return out


def decide(state, questions, transport=None, creds=None):
    """Full round trip: build, post, parse. Returns
    (answers, raw_response) or (None, None) on any failure or when no key
    is configured. `transport` and `creds` are injection points for tests."""
    creds = creds or credentials()
    if creds is None:
        return None, None
    base_url, api_key, model = creds
    body = build_request(state, questions, model=model)
    post = transport or http_post
    doc = post(base_url, api_key, body)
    if doc is None:
        return None, None
    return _within_vocabulary(parse_answers(doc), questions), doc


def _within_vocabulary(answers, questions):
    """Drop any choice answer whose option set is not exactly the criteria the
    request sent. parse_answers can only check a response against itself. A
    model that answers `medium` to a normal|critical question would otherwise
    parse as a confident, well-formed choice and reach the gate - and a gate
    that maps "not critical" to normal would then fail open. Dropped means
    missing, and missing resolves to fallback (or, for risk, to critical).
    """
    kept = {}
    for name, answer in answers.items():
        question = questions.get(name)
        if answer.kind == "choice":
            if not isinstance(question, dict) or question.get("type") != "choice":
                continue
            if set(answer.probabilities) != set(question.get("criteria") or {}):
                continue
        kept[name] = answer
    return kept

"""Confidence gate: turns a Jev answer into `auto` or `fallback`.

This is what makes Jev an accelerator rather than a dependency. Every path
that is not a confident, well-formed answer resolves to `fallback`, meaning
"do what the pipeline does today" (dispatch the subagent, ask the
orchestrator, escalate). The thresholds are constants a human reviews.

Two decisions carry a safety asymmetry and get a dedicated rule:
  - risk tier: an uncertain answer resolves to `critical`, never `normal`
  - admission nouls: an uncertain answer resolves to "not satisfied"
"""

AUTO = "auto"
FALLBACK = "fallback"

# Act automatically at or above this. Below it, fall back. TypeSafe's own
# guidance: the right value is domain-specific and measured, not assumed.
# 0.85 is a placeholder for a spike with zero live measurements behind it.
CONFIDENCE_FLOOR = 0.85

# A noul is treated as "yes" only at or above this probability.
NOUL_YES_FLOOR = 0.90

# Risk tier is fail-safe in the other direction: below this confidence the
# answer is forced to critical, whatever the model preferred.
RISK_CONFIDENCE_FLOOR = 0.90


def _confident(answer, floor):
    return answer is not None and answer.confidence is not None and answer.confidence >= floor


def gate_choice(answer, floor=CONFIDENCE_FLOOR):
    """{"action": auto|fallback, "choice": str|None, "confidence": float|None}."""
    if answer is None or answer.kind != "choice":
        return {"action": FALLBACK, "choice": None, "confidence": None,
                "reason": "no well-formed choice answer"}
    if not _confident(answer, floor):
        return {"action": FALLBACK, "choice": answer.choice, "confidence": answer.confidence,
                "reason": "confidence below %.2f" % floor}
    return {"action": AUTO, "choice": answer.choice, "confidence": answer.confidence,
            "reason": "confident"}


def gate_risk(answer, floor=RISK_CONFIDENCE_FLOOR):
    """Risk tier, fail-safe: any uncertainty is `critical`, and `critical` is
    always `auto` - a false critical costs one deeper run, a false normal
    skips the verification a critical change needed."""
    if answer is None or answer.kind != "choice":
        return {"action": AUTO, "choice": "critical", "confidence": None,
                "reason": "no answer, forced critical"}
    if answer.choice == "critical":
        return {"action": AUTO, "choice": "critical", "confidence": answer.confidence,
                "reason": "model chose critical"}
    # `normal` is the ONLY answer that can ever come out as normal, and only
    # when confident. Anything else - an option outside the vocabulary, a
    # confidence below the floor - is critical. Never "not critical, so normal".
    if answer.choice == "normal" and _confident(answer, floor):
        return {"action": AUTO, "choice": "normal", "confidence": answer.confidence,
                "reason": "confident normal"}
    return {"action": AUTO, "choice": "critical", "confidence": answer.confidence,
            "reason": "%r below %.2f confidence or outside normal|critical, forced critical"
                      % (answer.choice, floor)}


def gate_noul(answer, yes_floor=NOUL_YES_FLOOR):
    """{"action", "satisfied": bool, "noul": float|None}. Fail-safe toward
    "not satisfied": a missing answer or a middling probability both resolve
    to False, so an admission check never passes on uncertainty."""
    if answer is None or answer.kind != "noul":
        return {"action": FALLBACK, "satisfied": False, "noul": None,
                "reason": "no well-formed noul answer"}
    if answer.noul >= yes_floor:
        return {"action": AUTO, "satisfied": True, "noul": answer.noul, "reason": "yes"}
    if answer.noul <= 1.0 - yes_floor:
        return {"action": AUTO, "satisfied": False, "noul": answer.noul, "reason": "no"}
    return {"action": FALLBACK, "satisfied": False, "noul": answer.noul,
            "reason": "between %.2f and %.2f, undecided" % (1.0 - yes_floor, yes_floor)}

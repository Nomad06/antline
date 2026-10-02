"""The pipeline decisions this spike hands to Jev, as data.

Every option list is built from the vocabulary ppl already owns
(ppl.review.CLASSIFICATIONS, ppl.run.BLOCKER_CODES), so a vocabulary change
breaks this module loudly instead of drifting. The option DESCRIPTIONS are
hand-written here and are the thing a human reviews - TypeSafe's own agent
skill says exactly that about criteria and thresholds.

Only closed-vocabulary decisions that today cost orchestrator judgement or a
full subagent dispatch. Nothing ppl decides deterministically is here, and
nothing a verifier reasons over evidence for is here either.
"""
from ppl import review, run

# --- review-thread classification: the four options ppl.review accepts ---

REVIEW_CRITERIA = {
    "NEEDS_CODE_CHANGE": {
        "what": "The reviewer is right and the code must change to satisfy the remark",
        "not_for": "Questions already answered by the code, or requests for explanation",
        "examples": ["This leaks the connection on the error path",
                     "Null check missing before dereference"],
    },
    "NEEDS_REPLY_ONLY": {
        "what": "The answer already exists in the code or the evidence, a reply suffices",
        "not_for": "Any remark that would require editing a source or test file",
        "examples": ["Why not use the existing helper here?",
                     "Is this covered by a test?"],
    },
    "DISAGREE": {
        "what": "The remark is technically wrong or based on a misreading, needs a reasoned counter",
        "not_for": "Matters of taste, or scope questions for the owner",
        "examples": ["This will deadlock (but the lock order is already fixed elsewhere)"],
    },
    "NEEDS_OWNER": {
        "what": "A material decision, scope change or architectural direction only the owner can take",
        "not_for": "Anything a code change or a factual reply would settle",
        "examples": ["Should this be a separate service?",
                     "Do we want to support the legacy format at all?"],
    },
}

assert tuple(REVIEW_CRITERIA) == review.CLASSIFICATIONS, (
    "REVIEW_CRITERIA must name exactly ppl.review.CLASSIFICATIONS, in order")


def review_classification(remark):
    return {"review_class": {
        "type": "choice",
        "instructions": "How should the pipeline handle this reviewer remark on a merge "
                        "request? Pick the one handling that fits.",
        "criteria": REVIEW_CRITERIA,
    }}


# --- risk tier: DESIGN 10.3, two tiers only ---

RISK_TIERS = ("normal", "critical")

RISK_CRITERIA = {
    "normal": {
        "what": "Ordinary feature or fix. Focused tests plus independent verification suffice",
        "not_for": "Anything touching data integrity, security, migrations, public contracts "
                   "or money-adjacent paths",
    },
    "critical": {
        "what": "Data integrity, security, database migrations, public API contracts, "
                "billing or money-adjacent paths, authentication or permissions",
        "not_for": "Cosmetic, logging, documentation or test-only changes",
    },
}


def risk_tier(ticket_text):
    return {"risk_tier": {
        "type": "choice",
        "instructions": "Which risk tier does this task belong to?",
        "criteria": RISK_CRITERIA,
    }}


# --- admission: one yes/no the checklist asks for ---

def admission_checks(ticket_text):
    return {
        "acceptance_stable": {
            "type": "noul",
            "instructions": "Does this task state acceptance criteria that are stable and "
                            "verifiable, so a test could prove each one?",
        },
        "decisions_closed": {
            "type": "noul",
            "instructions": "Are all material product decisions in this task already made, "
                            "with no open question a developer would have to guess at?",
        },
    }


# --- BLOCKED verdict -> park blocker code (SKILL Stage 5 hands this to judgement) ---

BLOCKER_CRITERIA = {
    "BASELINE_FAILURE": "The problem exists on the base revision, before this candidate",
    "EXTERNAL_BLOCKED": "GitLab, Jira, CI or another external system is unavailable",
    "IMPLEMENTATION_FAILURE": "The candidate's own code does not satisfy the contract",
    "TEST_FAILURE": "The test is wrong or incomplete, the code is not at fault",
    "SPEC_GAP": "The requirements do not define the behaviour in question",
    "OWNER_DECISION": "A material decision is required from the owner",
}

assert set(BLOCKER_CRITERIA) <= set(run.BLOCKER_CODES), (
    "BLOCKER_CRITERIA must be a subset of ppl.run.BLOCKER_CODES")


def blocker_code(finding_text):
    return {"blocker": {
        "type": "choice",
        "instructions": "A verifier judged this candidate BLOCKED for the reason below. "
                        "Which blocker code describes it?",
        "criteria": BLOCKER_CRITERIA,
    }}


def all_for(sample):
    """Every question this spike asks, in one request - the pattern TypeSafe
    recommends (speculative fan-out, one call)."""
    questions = {}
    questions.update(review_classification(sample.get("remark", "")))
    questions.update(risk_tier(sample.get("ticket", "")))
    questions.update(admission_checks(sample.get("ticket", "")))
    questions.update(blocker_code(sample.get("finding", "")))
    return questions

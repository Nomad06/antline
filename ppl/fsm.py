from .errors import PplError

STAGES = ("admission", "baseline", "briefing", "implementation", "tests",
          "gates", "verification", "mr", "review", "closure", "merge_watch",
          "parking", "escalation")

LIFECYCLE = ("planned", "ready", "in_progress", "done", "blocked", "failed")

_FORWARD = {
    "admission": {"baseline", "escalation"},
    "baseline": {"briefing"},
    "briefing": {"implementation", "escalation"},
    "implementation": {"tests", "briefing"},
    "tests": {"gates"},
    "gates": {"verification", "implementation", "tests"},
    "verification": {"mr", "implementation", "tests", "briefing"},
    "mr": {"review"},
    "review": {"closure", "implementation", "tests"},
    "closure": {"merge_watch"},
    "merge_watch": set(),
    "escalation": set(STAGES) - {"merge_watch"},
    "parking": set(STAGES) - {"merge_watch"},
}

_LIFECYCLE_EDGES = {
    "planned": {"ready", "failed"},
    "ready": {"in_progress", "blocked", "failed"},
    "in_progress": {"done", "blocked", "failed"},
    "blocked": {"in_progress", "failed"},
    "done": set(),
    "failed": set(),
}


def _known(stage):
    if stage not in STAGES:
        raise PplError("SCHEMA_INVALID", "unknown stage: %r" % (stage,))


def check_stage(current, target):
    _known(current)
    _known(target)
    if target in ("parking", "escalation"):
        return
    if target not in _FORWARD[current]:
        raise PplError(
            "ILLEGAL_TRANSITION",
            "stage %s cannot move to %s. Legal: %s"
            % (current, target, ", ".join(sorted(_FORWARD[current])) or "none"),
        )


def check_lifecycle(current, target):
    if current not in LIFECYCLE or target not in LIFECYCLE:
        raise PplError("SCHEMA_INVALID", "unknown lifecycle status")
    if target not in _LIFECYCLE_EDGES[current]:
        raise PplError(
            "ILLEGAL_TRANSITION",
            "lifecycle %s cannot move to %s" % (current, target),
        )

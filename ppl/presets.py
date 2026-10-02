"""Model presets for the wizard: a tier and an effort per role. A preset
names tiers, never models, so the models stay in routing.toml [tiers] and a
preset fits whichever models the user put behind each tier."""
from . import routing

PRESETS = {
    "economy": {
        "ppl-briefer": {"tier": "average", "effort": "high"},
        "ppl-brief-challenger": {"tier": "average", "effort": "high"},
        "ppl-code-worker": {"tier": "average", "effort": "medium"},
        "ppl-test-worker": {"tier": "average", "effort": "medium"},
        "ppl-verifier": {"tier": "average", "effort": "high"},
        "ppl-review-responder": {"tier": "light", "effort": "medium"},
    },
    "balanced": {
        "ppl-briefer": {"tier": "good", "effort": "high"},
        "ppl-brief-challenger": {"tier": "good", "effort": "xhigh"},
        "ppl-code-worker": {"tier": "average", "effort": "high", "by_risk": {"high": "good"}},
        "ppl-test-worker": {"tier": "average", "effort": "high"},
        "ppl-verifier": {"tier": "good", "effort": "xhigh"},
        "ppl-review-responder": {"tier": "average", "effort": "high"},
    },
    "max": {
        "ppl-briefer": {"tier": "good", "effort": "xhigh"},
        "ppl-brief-challenger": {"tier": "heavy", "effort": "xhigh"},
        "ppl-code-worker": {"tier": "good", "effort": "xhigh", "by_risk": {"high": "heavy"}},
        "ppl-test-worker": {"tier": "good", "effort": "high"},
        "ppl-verifier": {"tier": "heavy", "effort": "xhigh"},
        "ppl-review-responder": {"tier": "average", "effort": "high"},
    },
}
LABELS = {"economy": "Economy", "balanced": "Balanced", "max": "Max quality"}
COST = {"economy": "low", "balanced": "medium", "max": "high"}


def _same(entry, role, spec):
    return (entry.get("tier") == spec["tier"]
            and routing.role_params(entry, role).get("effort") == spec["effort"]
            and dict(entry.get("by_risk") or {}) == spec.get("by_risk", {}))


def matching(config):
    """The preset routing.toml's roles equal exactly, or None."""
    roles = config.get("roles") or {}
    for name, spec in PRESETS.items():
        if all(_same(roles.get(role) or {}, role, role_spec) for role, role_spec in spec.items()):
            return name
    return None


def listing():
    return [{"name": name, "label": LABELS[name], "cost": COST[name],
             "roles": [dict(spec, role=role) for role, spec in roles.items()]}
            for name, roles in PRESETS.items()]

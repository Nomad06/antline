"""Model presets for the wizard's Models screen."""
from .. import presets, routing


def _view():
    config = routing.load()
    return {"presets": presets.listing(), "current": presets.matching(config),
            "tiers": routing.tiers(config), "roles": routing.summary(config),
            "effort_levels": list(routing.EFFORT_LEVELS)}


def _choose(params, body, query):
    routing.apply_preset(str((body or {}).get("name") or ""))
    return _view()


def register(router):
    router.add("GET", "/api/routing/presets", lambda params, body, query: _view())
    router.add("POST", "/api/routing/preset", _choose)

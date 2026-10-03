"""Model presets for the wizard's Models screen and the Agents page."""
from .. import presets, routing, tomlout
from ..studio import files


def _view():
    current = files.read("routing")
    config = routing.loads(current["text"])
    return {"presets": presets.listing(), "current": presets.matching(config),
            "tiers": routing.tiers(config), "roles": routing.summary(config),
            "effort_levels": list(routing.EFFORT_LEVELS), "routing_hash": current["hash"]}


def _choose(params, body, query):
    """The document `ppl routing preset` writes, saved as every studio save is:
    validated, refused when routing.toml changed since the page loaded it, and
    the replaced file kept as a history version."""
    body = body or {}
    text = tomlout.dumps(routing.preset_document(str(body.get("name") or "")))
    files.validate("routing", text)
    files.save("routing", text, body.get("hash"))
    return _view()


def register(router):
    router.add("GET", "/api/routing/presets", lambda params, body, query: _view())
    router.add("POST", "/api/routing/preset", _choose)

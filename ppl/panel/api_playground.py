"""The cage playground: one synthetic tool call against a harness."""
from .. import config
from ..studio import playground


def _check(params, body, query):
    body = body or {}
    name = body.get("name") or config.active_harness()
    return playground.check(name, body.get("doc"), body.get("fragments") or {},
                            body.get("role"), body.get("tool"), body.get("value"),
                            body.get("boundary"))


def register(router):
    router.add("POST", "/api/playground", _check)

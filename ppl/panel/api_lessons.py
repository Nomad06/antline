"""The Lessons section of the Agents page: the accept queue and the active lessons of the
active harness. Every write is the function `ppl lessons` calls, which saves memory.toml through
the studio's versioned save [R-LESSONS-OWNER-ACCEPTS]."""
from .. import config, lessons
from ..errors import PplError


def _text(body, field):
    value = body.get(field)
    if value is not None and not isinstance(value, str):
        raise PplError("SCHEMA_INVALID", "%s must be text" % field)
    return value


def _body(body):
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise PplError("SCHEMA_INVALID", "the request body must be a JSON object")
    return body


def _answer(name, entry=None):
    answer = {"apply": "run ppl harness apply %s to render it" % name}
    if entry is not None:
        answer["entry"] = entry
    return answer


def overview(params, body, query):
    if not config.active_harness():
        return {"harness": None}
    return lessons.listing(lessons.harness_name())


def accept(params, body, query):
    body = _body(body)
    name = lessons.harness_name()
    entry = lessons.accept(name, params["proposal"], _text(body, "cue"), _text(body, "lesson"))
    return _answer(name, entry)


def reject(params, body, query):
    body = _body(body)
    name = lessons.harness_name()
    lessons.reject(name, params["proposal"], _text(body, "reason"))
    return _answer(name)


def retire(params, body, query):
    _body(body)
    name = lessons.harness_name()
    lessons.retire(name, params["id"])
    return _answer(name)


def edit(params, body, query):
    body = _body(body)
    name = lessons.harness_name()
    entry = lessons.edit(name, params["id"], _text(body, "cue"), _text(body, "lesson"))
    return _answer(name, entry)


def register(router):
    router.add("GET", "/api/lessons", overview)
    router.add("POST", "/api/lessons/<proposal>/accept", accept)
    router.add("POST", "/api/lessons/<proposal>/reject", reject)
    router.add("POST", "/api/memory/<id>/retire", retire)
    router.add("POST", "/api/memory/<id>/edit", edit)

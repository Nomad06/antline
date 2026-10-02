"""The wizard's harness screen: inspect the repository, draft from a stack
preset, draft from Claude reading the md files, check and install a draft."""
from ..errors import PplError
from ..studio import drafts, extract, project, stacks
from .jobs import JOBS


def _inspect(params, body, query):
    return project.inspect((body or {}).get("path"))


def _preset(params, body, query):
    body = body or {}
    info = project.inspect(body.get("path"))
    return stacks.draft(str(body.get("stack") or info["stack"]["stack"]), info)


def _extract(params, body, query):
    body = body or {}
    info = project.inspect(body.get("path"))
    selected = body.get("md")
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise PplError("SCHEMA_INVALID", "md must be a list of the file paths the inspection listed")
    files, skipped = project.collect(info["path"], selected)
    if not files:
        raise PplError("EXTRACTION_FAILED", "none of the chosen files could be read: %s"
                       % "; ".join("%s (%s)" % (item["path"], item["reason"]) for item in skipped))
    preset = stacks.draft(info["stack"]["stack"], info)

    def work(emit):
        result = extract.extract(files, emit)
        merged = extract.merge(preset, result["extracted"])
        merged.update({"dropped": result["dropped"], "skipped": skipped,
                       "model": result["model"], "cost_usd": result["cost_usd"]})
        return merged
    return {"job": JOBS.start("extract", work)}


def _parts(body):
    body = body or {}
    return body.get("name"), body.get("doc"), body.get("fragments") or {}


def _check(params, body, query):
    name, doc, fragments = _parts(body)
    drafts.check_name(name)
    return drafts.check(name, doc, fragments)


def _install(params, body, query):
    name, doc, fragments = _parts(body)
    drafts.check_name(name)
    replace = (body or {}).get("replace") is True
    return {"job": JOBS.start("setup",
                              lambda emit: drafts.install(name, doc, fragments, replace, emit))}


def register(router):
    router.add("POST", "/api/project/inspect", _inspect)
    router.add("POST", "/api/harness/preset", _preset)
    router.add("POST", "/api/harness/extract", _extract)
    router.add("POST", "/api/harness/draft/check", _check)
    router.add("POST", "/api/harness/draft/install", _install)

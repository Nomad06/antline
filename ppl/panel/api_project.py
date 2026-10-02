"""The wizard's harness screen: inspect the repository, draft from a stack
preset, draft from Claude reading the md files, check and install a draft."""
from ..studio import project, stacks


def _inspect(params, body, query):
    return project.inspect((body or {}).get("path"))


def _preset(params, body, query):
    body = body or {}
    info = project.inspect(body.get("path"))
    return stacks.draft(str(body.get("stack") or info["stack"]["stack"]), info)


def register(router):
    router.add("POST", "/api/project/inspect", _inspect)
    router.add("POST", "/api/harness/preset", _preset)

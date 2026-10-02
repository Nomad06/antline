"""The Agents page: per-role routing, the role's project notes, a preview of
the agent file apply would write, and the blocking rules that hold for it."""
import shutil
import tempfile

from .. import config, harness, models, paths, presets, routing, tomlout
from ..errors import PplError
from ..studio import drafts, enforced, files


def _active():
    name = config.active_harness()
    if not name or name not in harness.list_names():
        return None, None
    return name, harness.load(name)


def _check_role(role):
    if role not in routing.REQUIRED_ROLES:
        raise PplError("SCHEMA_INVALID", "unknown role %r (roles: %s)"
                       % (role, ", ".join(routing.REQUIRED_ROLES)))


def _overlay_key(name, role):
    return "harness:%s:templates/%s.part" % (name, role)


def _routing_document(doc):
    if not isinstance(doc, dict):
        raise PplError("SCHEMA_INVALID", "routing must be the routing document")
    try:
        routing.validate_config(doc)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PplError("SCHEMA_INVALID", "routing is not a routing document: %s" % exc)
    return doc


def _verified():
    binary = config.section("claude").get("binary") or "claude"
    try:
        rows = models.status(binary=binary, all_tiers=True)
    except PplError:
        return {}
    return {row["model"]: row["state"] for row in rows}


def overview(params, body, query):
    doc = routing.load()
    name, harness_doc = _active()
    states = _verified()
    roles = []
    for row in routing.summary(doc):
        tools = (routing.role_tools(row["role"], harness_doc) if harness_doc
                 else list(routing.BASE_TOOLS[row["role"]]["tools"]))
        roles.append(dict(row, tools=tools, verified=states.get(row["model"], "unverified")))
    return {"roles": roles, "routing": doc, "routing_hash": files.read("routing")["hash"],
            "tiers": routing.tiers(doc), "effort_levels": list(routing.EFFORT_LEVELS),
            "presets": presets.listing(), "preset": presets.matching(doc), "harness": name}


def detail(params, body, query):
    role = params["role"]
    _check_role(role)
    base = (paths.root() / "templates" / ("%s.md.body" % role)).read_text(encoding="utf-8")
    name, harness_doc = _active()
    return {"role": role, "base": base,
            "overlay": files.read(_overlay_key(name, role)) if name else None,
            "rendered": routing.render(role, harness_doc) if harness_doc else None,
            "enforced": enforced.for_role(role), "harness": name}


def preview(params, body, query):
    role = params["role"]
    _check_role(role)
    name, harness_doc = _active()
    if not harness_doc:
        raise PplError("SCHEMA_INVALID", "no active harness yet: finish the harness and apply "
                       "steps first")
    body = body or {}
    routing_doc = body.get("routing")
    if routing_doc is not None:
        _routing_document(routing_doc)
    overlay = body.get("overlay")
    if overlay is None:
        return {"rendered": routing.render(role, harness_doc, config=routing_doc)}
    if not isinstance(overlay, str):
        raise PplError("SCHEMA_INVALID", "overlay must be text")
    fragments = files.harness_fragments(name)
    fragments["templates/%s.part" % role] = overlay
    # a private scratch root per preview: two previews never share a directory
    drafts.drafts_dir().mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(dir=drafts.drafts_dir(), prefix=".preview-")
    try:
        draft = drafts.write(name, harness_doc, fragments, root=scratch)
        return {"rendered": routing.render(role, harness.load(str(draft)), config=routing_doc)}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def save_routing(params, body, query):
    body = body or {}
    if "text" in body:
        text = body["text"]
        if not isinstance(text, str):
            raise PplError("SCHEMA_INVALID", "text must be the TOML of routing.toml")
    elif isinstance(body.get("config"), dict):
        try:
            text = tomlout.dumps(body["config"])
        except (TypeError, ValueError) as exc:
            raise PplError("SCHEMA_INVALID", "config cannot be written as TOML: %s" % exc)
    else:
        raise PplError("SCHEMA_INVALID", "send the routing document as config, or its TOML as text")
    files.validate("routing", text)
    return {"hash": files.save("routing", text, body.get("hash")), "roles": routing.summary()}


def save_overlay(params, body, query):
    role = params["role"]
    _check_role(role)
    name, _ = _active()
    if not name:
        raise PplError("SCHEMA_INVALID", "no active harness yet: the project notes live in it")
    text = (body or {}).get("text")
    if not isinstance(text, str):
        raise PplError("SCHEMA_INVALID", "text must be the project notes for the role")
    key = _overlay_key(name, role)
    files.validate(key, text)
    return {"hash": files.save(key, text, (body or {}).get("hash"))}


def register(router):
    router.add("GET", "/api/agents", overview)
    router.add("GET", "/api/agents/<role>", detail)
    router.add("POST", "/api/agents/<role>/preview", preview)
    router.add("PUT", "/api/agents/<role>/overlay", save_overlay)
    router.add("PUT", "/api/routing", save_routing)

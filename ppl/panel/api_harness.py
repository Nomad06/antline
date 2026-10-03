"""The Harness page: the installed harnesses, one harness's document and
fragments with their hashes, and check, save, apply, add, create. A harness
becomes the active one only by apply, which validates and renders it first,
as `ppl harness apply` does."""
import json
import shutil
import tempfile
import tomllib
from pathlib import Path

from .. import config, harness, paths
from ..errors import PplError
from ..studio import drafts, files
from .jobs import JOBS


def _installed(name):
    if name not in harness.list_names():
        raise PplError("NOT_FOUND", "no installed harness %r" % name)
    return paths.harness_dir(name)


def _key(name, rel):
    return "harness:%s:%s" % (name, rel)


def _document(text):
    """The parsed harness.toml, or None when it does not parse or holds a value
    JSON cannot carry (a date), so a harness that cannot be loaded is still
    sent as text for repair."""
    try:
        doc = tomllib.loads(text)
        json.dumps(doc)
    except (tomllib.TOMLDecodeError, TypeError, ValueError):
        return None
    return doc


def _absolute(path):
    try:
        return Path(path).expanduser().is_absolute()
    except RuntimeError:
        return False


def _object(body, field):
    value = body.get(field)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PplError("SCHEMA_INVALID", "%s must be an object keyed by file" % field)
    return value


def listing(params, body, query):
    return {"names": harness.list_names(), "active": config.active_harness()}


def one(params, body, query):
    name = params["name"]
    _installed(name)
    loaded = {rel: files.read(_key(name, rel)) for rel in files.HARNESS_FILES}
    toml_text = loaded["harness.toml"]["text"]
    return {"name": name, "doc": _document(toml_text), "text": toml_text,
            "fragments": {rel: loaded[rel]["text"] for rel in files.harness_fragments(name)},
            "hashes": {rel: read["hash"] for rel, read in loaded.items()},
            "active": config.active_harness() == name, "check": files.check_harness(name)}


def check(params, body, query):
    _installed(params["name"])
    body = body or {}
    return drafts.check_private(params["name"], body.get("doc"), body.get("fragments") or {})


def save(params, body, query):
    name = params["name"]
    _installed(name)
    body = body or {}
    hashes = _object(body, "hashes")
    fragments = _object(body, "fragments")
    unknown = sorted(rel for rel in fragments if rel not in drafts.FRAGMENTS)
    if unknown:
        raise PplError("SCHEMA_INVALID", "unknown fragment %s (fragments: %s)"
                       % (", ".join(unknown), ", ".join(drafts.FRAGMENTS)))
    if "text" in body and "doc" in body:
        raise PplError("SCHEMA_INVALID", "send the harness as doc or as text, not both")
    if "text" in body:
        text = body["text"]
        if not isinstance(text, str):
            raise PplError("SCHEMA_INVALID", "text must be the TOML of harness.toml")
    else:
        text = drafts.toml_text(name, body.get("doc"))
    current = files.harness_fragments(name)
    changes = [("harness.toml", text, hashes.get("harness.toml"))]
    for rel, fragment in fragments.items():
        if fragment != current.get(rel, ""):
            changes.append((rel, fragment, hashes.get(rel)))
    # check exactly the bytes about to be written, on a private copy of the harness
    result = files.check_harness(name, [(rel, content) for rel, content, _ in changes])
    if not result["ok"]:
        raise PplError("HARNESS_INVALID", "; ".join(result["errors"]))
    saved = files.save_many([(_key(name, rel), content, expected)
                             for rel, content, expected in changes])
    return {"hashes": {key.split(":", 2)[2]: value for key, value in saved.items()},
            "check": result}


def apply_job(params, body, query):
    name = params["name"]
    _installed(name)
    return {"job": JOBS.start("apply", lambda emit: harness.apply(name))}


def add_job(params, body, query):
    body = body or {}
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        raise PplError("SCHEMA_INVALID", "give a git URL or the directory of a harness")
    source = source.strip()
    if not harness.is_git_source(source) and not _absolute(source):
        raise PplError("SCHEMA_INVALID", "a local source must be an absolute path or start with "
                       "~/: a relative one would resolve against the working directory of "
                       "ppl serve")
    name = body.get("name") or harness.derived_name(source)
    drafts.check_name(name)
    return {"job": JOBS.start("add", lambda emit: {"name": harness.add(source, name).name})}


def create(params, body, query):
    body = body or {}
    name = body.get("name")
    drafts.check_name(name)
    if name in harness.list_names():
        raise PplError("SCHEMA_INVALID", "a harness named %s is installed already" % name)
    # a private copy per request, as install uses, so a check of the same name
    # beside it cannot change what was checked and what is added
    drafts.drafts_dir().mkdir(parents=True, exist_ok=True)
    private = tempfile.mkdtemp(dir=drafts.drafts_dir(), prefix=".create-")
    try:
        target, result = drafts.checked_copy(name, body.get("doc"), body.get("fragments") or {},
                                             private)
        if not result["ok"]:
            raise PplError("HARNESS_INVALID", "; ".join(result["errors"]))
        return {"name": harness.add(str(target), name=name).name, "check": result}
    finally:
        shutil.rmtree(private, ignore_errors=True)


def register(router):
    router.add("GET", "/api/harnesses", listing)
    router.add("POST", "/api/harnesses", add_job)
    router.add("POST", "/api/harnesses/new", create)
    router.add("GET", "/api/harnesses/<name>", one)
    router.add("PUT", "/api/harnesses/<name>", save)
    router.add("POST", "/api/harnesses/<name>/check", check)
    router.add("POST", "/api/harnesses/<name>/apply", apply_job)

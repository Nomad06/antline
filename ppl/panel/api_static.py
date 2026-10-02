"""The studio's own files, served from a fixed manifest: a request names a
manifest entry or gets a 404, never a path on disk."""
from pathlib import Path

from ..errors import PplError
from .router import Raw

STATIC = Path(__file__).with_name("static")
JS = "text/javascript; charset=utf-8"
TEXT = "text/plain; charset=utf-8"
# A module must never outlive a ppl upgrade in the browser cache.
NO_CACHE = {"Cache-Control": "no-cache"}
MANIFEST = {
    "index.html": "text/html; charset=utf-8",
    "app.js": JS,
    "api.js": JS,
    "theme.css": "text/css; charset=utf-8",
    "ui/controls.js": JS,
    "pages/home.js": JS,
    "vendor/htm-preact.mjs": JS,
    "vendor/LICENSE-htm": TEXT,
    "vendor/LICENSE-preact": TEXT,
}


def file(rel):
    if rel not in MANIFEST:
        raise PplError("NOT_FOUND", "%s is not a studio file" % rel)
    return Raw((STATIC / rel).read_bytes(), MANIFEST[rel], NO_CACHE)


def register(router):
    router.add("GET", "/static/<name>", lambda params, body, query: file(params["name"]))
    for folder in ("pages", "ui", "vendor"):
        router.add("GET", "/static/%s/<name>" % folder,
                   lambda params, body, query, folder=folder: file("%s/%s" % (folder, params["name"])))

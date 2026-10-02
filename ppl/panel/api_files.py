"""Keyed file reads and the history of every studio save."""
from ..studio import files


def _key(query):
    return (query.get("key") or [""])[0]


def register(router):
    router.add("GET", "/api/files", lambda params, body, query: files.read(_key(query)))
    router.add("GET", "/api/history", lambda params, body, query:
               {"key": _key(query), "versions": files.versions(_key(query))})
    router.add("GET", "/api/history/version", lambda params, body, query:
               {"text": files.version_text(_key(query), (query.get("version") or [""])[0])})
    router.add("POST", "/api/history/restore", lambda params, body, query:
               {"hash": files.restore((body or {}).get("key"), (body or {}).get("version"),
                                      (body or {}).get("hash"))})

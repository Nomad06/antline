"""The review threads of a GitHub pull request, read with gh and shaped for
`ppl review pending`. It runs outside the OS sandbox, so it only reads and returns a
document: the caller writes it."""

import json
import re
from datetime import datetime, timezone

from ..errors import PplError
from ..trackers import github_issues

GH_TIMEOUT = github_issues.GH_TIMEOUT
COMMAND = "ppl review fetch"
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_QUERY = ("query { repository(owner: \"%s\", name: \"%s\") { pullRequest(number: %d) { "
          "reviewThreads(first: 100) { pageInfo { hasNextPage } nodes { isResolved "
          "comments(first: 1) { nodes { databaseId } } } } } } }")
_PAGE = 100


def _blocked(why):
    return github_issues._blocked(why, COMMAND)


def _gh(runner, argv, what):
    code, out, err = github_issues._gh(runner, argv, what, COMMAND)
    if code != 0:
        raise _blocked("gh %s failed: %s" % (" ".join(argv[:2]),
                                             err.strip()[:300] or "exit %s" % code))
    return out


def _pages(text, what):
    """The items of gh --paginate output: several JSON arrays back to back."""
    decoder = json.JSONDecoder()
    items, index, end = [], 0, len(text)
    while True:
        while index < end and text[index].isspace():
            index += 1
        if index >= end:
            return items
        try:
            page, index = decoder.raw_decode(text, index)
        except ValueError:
            raise _blocked("gh answered %s with text that is not JSON" % what)
        if not isinstance(page, list):
            raise _blocked("gh answered %s with something other than a list" % what)
        items.extend(item for item in page if isinstance(item, dict))


def _login(person):
    login = person.get("login") if isinstance(person, dict) else None
    return login if isinstance(login, str) and login else "unknown"


def _resolved(text):
    """{first comment id: resolved} of the pull request's review threads. A pull request with
    more than one page of them is refused: a thread past the first page would read as
    unresolved, and ppl does not page through them."""
    try:
        threads = json.loads(text)["data"]["repository"]["pullRequest"]["reviewThreads"]
        nodes = threads["nodes"]
        more = threads["pageInfo"]["hasNextPage"]
    except (ValueError, KeyError, TypeError):
        raise _blocked("gh answered the review threads with an unexpected document")
    if more is not False:
        raise _blocked("the pull request has more than %d review threads, more than one page, "
                       "and ppl review fetch reads only the first, so their states cannot all "
                       "be known" % _PAGE)
    found = {}
    for node in nodes if isinstance(nodes, list) else []:
        try:
            first = node["comments"]["nodes"][0]["databaseId"]
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(first, int) and not isinstance(first, bool):
            found[first] = node.get("isResolved") is True
    return found


def _threads(comments, resolved):
    groups = {}
    for comment in comments:
        ident = comment.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool):
            continue
        parent = comment.get("in_reply_to_id")
        root = parent if isinstance(parent, int) and not isinstance(parent, bool) else ident
        groups.setdefault(root, []).append(comment)
    threads = []
    for root, notes in groups.items():
        last = max(notes, key=lambda note: note["id"])
        threads.append({"id": str(root), "author": _login(notes[0].get("user")),
                        "resolved": resolved.get(root, False), "last_note_id": str(last["id"]),
                        "last_author": _login(last.get("user")), "note_count": len(notes)})
    return threads


def _review(review):
    return {"id": str(review.get("id")), "author": _login(review.get("user")),
            "state": review.get("state"), "commit_id": review.get("commit_id"),
            "submitted_at": review.get("submitted_at")}


def fetch(repo, pr, runner=None):
    """{"fetched_at", "pr", "threads", "reviews"} of pull request `pr` of `repo`. Any gh
    failure is EXTERNAL_BLOCKED."""
    import subprocess
    runner = runner or subprocess.run
    if not (isinstance(repo, str) and _REPO.fullmatch(repo)):
        raise PplError("SCHEMA_INVALID", "%r is not an owner/name repository" % (repo,))
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise PplError("SCHEMA_INVALID", "the pull request number must be a positive integer")
    base = "repos/%s/pulls/%d" % (repo, pr)
    comments = _pages(_gh(runner, ["api", base + "/comments", "--paginate"],
                          "the review comments"), "the review comments")
    reviews = _pages(_gh(runner, ["api", base + "/reviews", "--paginate"], "the reviews"),
                     "the reviews")
    owner, name = repo.split("/")
    graph = _gh(runner, ["api", "graphql", "-f", "query=" + _QUERY % (owner, name, pr)],
                "the review thread states")
    return {"fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "pr": pr, "threads": _threads(comments, _resolved(graph)),
            "reviews": [_review(review) for review in reviews]}

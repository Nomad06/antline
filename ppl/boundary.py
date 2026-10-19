import os
import re
import stat

from . import paths, store
from .errors import PplError

TEST_PATTERNS = ("**/src/test/**", "**/*Test.java", "**/*IT.java", "**/*Tests.java")

# Every generated role (routing.REQUIRED_ROLES) has a row: the guard and the
# dispatch counter treat an agent_type as a pipeline role only when it is here.
ROLE_RULES = {
    "ppl-code-worker": {"may_write": True, "tests": "deny"},
    "ppl-test-worker": {"may_write": True, "tests": "only"},
    "ppl-briefer": {"may_write": False, "tests": "deny"},
    "ppl-brief-challenger": {"may_write": False, "tests": "deny"},
    "ppl-verifier": {"may_write": False, "tests": "deny"},
    "ppl-review-responder": {"may_write": False, "tests": "deny"},
}


def glob_to_regex(pattern):
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def glob_match(path, pattern):
    return bool(glob_to_regex(pattern).match(path))


def any_match(path, patterns):
    return any(glob_match(path, p) for p in patterns or ())


# Paths no test glob may match: a glob that does would hand product code to the
# test worker. The list is finite, so a product glob none of them resembles passes.
PRODUCT_PROBES = ("a", "a/b", "a/b/c", "main.py", "src/main.py", "src/main.c",
                  "src/main/java/App.java", "lib/index.js", "src/index.ts",
                  "cmd/app/main.go", "src/lib.rs", "README.md", "Makefile")


def glob_problem(pattern):
    """Why `pattern` cannot name test sources, or None. Refuses what glob_match
    reads literally (braces, classes, escapes, a leading !), what never matches a
    repo-relative path (absolute, ./, .., empty segments, a trailing /) and a glob
    matching any of PRODUCT_PROBES."""
    if not isinstance(pattern, str) or not pattern:
        return "is not a non-empty string"
    if "{" in pattern or "}" in pattern:
        return ("uses braces, which glob_match reads literally: list each alternative as "
                "its own glob (one glob per alternative)")
    for char in ("[", "]", "\\"):
        if char in pattern:
            return "uses %r, which glob_match reads literally" % char
    if pattern.startswith("!"):
        return "starts with !, which glob_match reads literally: negation is not supported"
    if pattern.startswith("/"):
        return "is absolute, but paths are matched repo-relative"
    if pattern.endswith("/"):
        return "ends with /, which no file path matches: end it with /**"
    segments = pattern.split("/")
    if "" in segments:
        return "has an empty path segment, which no repo-relative path matches"
    if "." in segments or ".." in segments:
        return "has a . or .. segment, which no repo-relative path matches"
    for probe in PRODUCT_PROBES:
        if glob_match(probe, pattern):
            return "matches the product path '%s': a test glob must not match product code" % probe
    return None


def patterns_from_snapshot(doc):
    """The test globs a run froze: the Java defaults for a snapshot with no
    `tests` key, the frozen list when it is well formed, None when the document
    or its `tests` value cannot be trusted."""
    if not isinstance(doc, dict):
        return None
    if "tests" not in doc:
        return TEST_PATTERNS
    tests = doc["tests"]
    sources = tests.get("sources") if isinstance(tests, dict) else None
    if not isinstance(sources, list) or not sources:
        return None
    if not all(isinstance(item, str) and item for item in sources):
        return None
    if any(glob_problem(item) for item in sources):
        return None
    return tuple(sources)


_MISSING = object()


def run_test_patterns(task_id):
    """The test globs runs/<task>/harness.json froze [R-TEST-PATTERNS-FROM-SNAPSHOT]:
    the Java defaults when nothing is at that path (ppl init always writes one, so
    that is a fixture or a run older than the snapshot), None when it is anything
    but a regular file (ppl never writes a link there) or cannot be read or
    parsed, otherwise patterns_from_snapshot of it. Never raises."""
    try:
        snapshot = paths.run_dir(task_id) / "harness.json"
        try:
            mode = os.lstat(snapshot).st_mode
        except FileNotFoundError:
            return TEST_PATTERNS
        if not stat.S_ISREG(mode):
            return None
        doc = store.read_json(snapshot, _MISSING)
        # gone between the two looks: not the missing file decision 1 means
        if doc is _MISSING:
            return None
        return patterns_from_snapshot(doc)
    except Exception:
        return None


def unknown_patterns_reason(task_id):
    """The one wording for a run whose test globs cannot be trusted, used by the
    guard, the done gate and ppl apply-patch [R-TEST-PATTERNS-FROM-SNAPSHOT]."""
    return ("run %s: runs/%s/harness.json is unreadable or its tests table is malformed, "
            "test sources cannot be told from product code [R-TEST-PATTERNS-FROM-SNAPSHOT]"
            % (task_id, task_id))


def is_test_path(path, patterns):
    return any_match(path, patterns)


def path_is_test_scoped(path, patterns):
    """True when `path` is a test source or the very directory a `<prefix>/**`
    glob covers: with `tests/**`, `tests` is scoped and `pkg/tests` is not, and
    `**/src/test/**` scopes every `src/test` directory. A Read/Grep/Glob or a
    Bash read aimed at such a directory can only surface what the glob names.

    Deliberately conservative (DESIGN 5.4): an ordinary module root does not
    qualify because a test directory lives under it, and a directory merely
    named `test` or `tests` is no test source unless a glob says so. A false
    deny costs a clarifying round, a false allow hands the test worker the
    implementation it must not see.
    """
    if not path or path == ".":
        return False
    if is_test_path(path, patterns):
        return True
    return any(glob_match(path, pattern[:-3]) for pattern in patterns or ()
               if pattern.endswith("/**") and len(pattern) > 3)


def _unknown_patterns(path):
    # check_path and check_diff are not told which run they serve
    return PplError("OWNER_DECISION", "%s (%s)" % (unknown_patterns_reason("<task>"), path))


def check_path(role, path, bound, patterns):
    """Refuses BOUNDARY_VIOLATION a write `role` may not make. `patterns` is the
    run's run_test_patterns: None refuses OWNER_DECISION once the decision needs
    it, since reading it as "no test sources" would hand the code worker every
    test inside its boundary."""
    if path.startswith("/"):
        raise PplError("BOUNDARY_VIOLATION",
                       "path must not be absolute: %s" % (path,))
    segments = path.split("/")
    if ".." in segments:
        raise PplError("BOUNDARY_VIOLATION",
                       "path contains traversal sequence (..): %s" % (path,))
    rules = ROLE_RULES.get(role)
    if rules is None:
        raise PplError("BOUNDARY_VIOLATION", "unknown role has no write rights: %s" % role)
    if not rules["may_write"]:
        raise PplError("BOUNDARY_VIOLATION", "role %s may not write any path (%s)" % (role, path))
    if any_match(path, bound.get("forbid")):
        raise PplError("BOUNDARY_VIOLATION", "%s is a forbidden path for %s" % (path, role))
    if patterns is None:
        raise _unknown_patterns(path)
    if rules["tests"] == "deny" and is_test_path(path, patterns):
        raise PplError("BOUNDARY_VIOLATION",
                       "%s may not write test sources: %s" % (role, path))
    if rules["tests"] == "only":
        if not is_test_path(path, patterns):
            raise PplError("BOUNDARY_VIOLATION",
                           "%s may only write test sources: %s" % (role, path))
        return
    if not any_match(path, bound.get("write")):
        raise PplError("BOUNDARY_VIOLATION",
                       "%s is outside the write boundary for %s" % (path, role))


def diff_paths(diff_text):
    found = []
    for line in diff_text.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            target = line[4:].strip()
            if target == "/dev/null":
                continue
            if target.startswith("a/") or target.startswith("b/"):
                target = target[2:]
            if target not in found:
                found.append(target)
    return found


def check_diff(role, diff_text, bound, patterns):
    # before the loop, which would fold the refusal into a BOUNDARY_VIOLATION
    if patterns is None:
        raise _unknown_patterns("the whole patch")
    offenders = []
    for path in diff_paths(diff_text):
        try:
            check_path(role, path, bound, patterns)
        except PplError as err:
            offenders.append("%s (%s)" % (path, err.message))
    if offenders:
        raise PplError("BOUNDARY_VIOLATION",
                       "patch touches paths outside the boundary: " + "; ".join(offenders))

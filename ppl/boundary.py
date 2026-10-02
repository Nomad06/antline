import re

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


def is_test_path(path):
    return any_match(path, TEST_PATTERNS)


def path_is_test_scoped(path):
    """True when `path` is a test source (`is_test_path`) or a directory that
    itself sits at or under a `test`/`tests` segment - a Read/Grep/Glob or a
    Bash read aimed at such a directory can only ever surface test sources,
    even though the directory name alone carries no file extension for
    `is_test_path` to match.

    Deliberately conservative, matching Task 8's own doctrine (DESIGN 5.4):
    an ordinary module root or a bare `src` directory does NOT qualify just
    because a `src/test` subdirectory happens to live under it - only a path
    landing AT or INSIDE a `test`/`tests` segment does. A false deny here
    costs a clarifying round; a false allow hands the test worker the
    implementation it must not see.
    """
    if not path or path in (".", ""):
        return False
    if is_test_path(path):
        return True
    segments = [s for s in path.split("/") if s not in ("", ".")]
    return "test" in segments or "tests" in segments


def check_path(role, path, bound):
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
    if rules["tests"] == "deny" and is_test_path(path):
        raise PplError("BOUNDARY_VIOLATION",
                       "%s may not write test sources: %s" % (role, path))
    if rules["tests"] == "only":
        if not is_test_path(path):
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


def check_diff(role, diff_text, bound):
    offenders = []
    for path in diff_paths(diff_text):
        try:
            check_path(role, path, bound)
        except PplError as err:
            offenders.append("%s (%s)" % (path, err.message))
    if offenders:
        raise PplError("BOUNDARY_VIOLATION",
                       "patch touches paths outside the boundary: " + "; ".join(offenders))

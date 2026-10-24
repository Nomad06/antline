"""GitHub through the gh CLI. The review responder gets Bash, caged to local git
read shapes and never reaches GitHub. Merge, approve and thread resolution are denied as Bash patterns and
as the official GitHub MCP server's tool names."""
import re
import subprocess

from .. import os_sandbox
from . import read_fragment

KIND = "github"
VOCABULARY = {"noun": "pull request", "short": "PR", "id_word": "number"}
_DENIED = {
    "mcp__github__merge_pull_request": "the pipeline ends at approval, a human performs the merge",
    "mcp__github__create_and_submit_pull_request_review":
        "the pipeline never approves its own pull request",
    "mcp__github__submit_pending_pull_request_review":
        "the pipeline never approves its own pull request",
}
_NO_MERGE = "the pipeline ends at approval, a human performs the merge"
_NO_APPROVE = "the pipeline never approves its own pull request"
_NO_RESOLVE = "only the remark author may resolve a thread"
_NO_SUBSTITUTION = "command substitution in a gh pr call is refused"
# The orchestrator is not under the responder shapes, so these patterns are its
# only guard. Each one tolerates other tokens before the verb (`gh pr -R o/r
# merge 7`), the short `-a` form of `--approve`, and the REST and GraphQL routes
# to the same effect. The reply route (`gh api .../comments/<id>/replies`) stays open.
# `merge` is matched at verb position, after flags only (a flag value may be a
# quoted string with spaces), so a title or body that says "merge" (`gh pr
# create --title "Fix merge ..."`) is not a merge. The verb may be quoted, since
# the shell unquotes `"merge"` before gh sees it. A verb produced by a command
# substitution cannot be read lexically, so a substitution anywhere in a gh pr
# call is refused.
# The flag grammar is unambiguous and atomic: a value is one shell word (quoted
# pieces, backslash escapes and bare runs glued together, each piece told apart
# by its first character and matched possessively), and a parsed flag is never
# re-split. An ambiguous grammar backtracks exponentially, a 128-character decoy
# took 11 s, and a hook that runs past its timeout denies nothing. A short flag
# may carry its value glued on (`-Ro/r`), as gh accepts. After `=` any word, or
# none, is a value. A spaced value sits after any whitespace, may not be a flag
# itself (`-` and `-1` are values, so `--draft -R o/r` is two flags) and may not
# be the verb itself (so `--x merge` is a value-less flag before the verb).
_WORD_PIECE = r"(?:'[^']*'|\"(?:[^\"\\]|\\[\s\S])*+\"|\\[\s\S]|[^\s'\"\\]+)"
_WORD = _WORD_PIECE + "++"
_FLAG = (r"(?>(?:-[A-Za-z]" + _WORD_PIECE + r"*+|--[A-Za-z-]+)"
         r"(?:=" + _WORD_PIECE + r"*+|\s+(?![\"']?merge\b)(?!-[A-Za-z-])" + _WORD + r")?)")
_DENIED_BASH = (
    (re.compile(r"\bgh\s+pr\s+(?>(?:" + _FLAG + r"\s+)*)[\"']?merge\b"), _NO_MERGE),
    (re.compile(r"\bgh\s+pr\b[^;&|]*(?:\$\(|`)"), _NO_SUBSTITUTION),
    (re.compile(r"\bgh\s+api\b[^;&|]*pulls/\d+/merge\b"), _NO_MERGE),
    (re.compile(r"\bgh\s+api\s+graphql\b[^;&|]*(?:mergePullRequest|enablePullRequestAutoMerge)\b"),
     _NO_MERGE),
    # Commits to the first `review` of the segment: an approve flag after any
    # later one is after the first too, and re-trying every `review` was cubic.
    (re.compile(r"\bgh\s+pr\b(?>[^;&|]*?\breview\b)[^;&|]*?"
                r"(?:--approve\b|(?<=\s)-[A-Za-z]*a[A-Za-z]*\b)"),
     _NO_APPROVE),
    (re.compile(r"\bgh\s+api\b[^;&|]*\bevent=[\"']?APPROVE\b", re.IGNORECASE), _NO_APPROVE),
    (re.compile(r"\bgh\s+api\s+graphql\b[^;&|]*(?:addPullRequestReview|submitPullRequestReview)\b"),
     _NO_APPROVE),
    (re.compile(r"\bgh\s+api\s+graphql\b[^;&|]*resolveReviewThread\b"), _NO_RESOLVE),
)
# The responder never reaches GitHub, it reads the change with local git. The
# shape format cannot bind `-C` to the active run's repo, since a shape row is
# built from the role alone, so `-C` takes any absolute path with no `..`
# segment and every rev rides as a positional word. A rev is a hex SHA of 7 to
# 40 characters or `origin/<name>`, a range is two of them. `git diff` and
# `git show` may run an external diff or textconv driver the repo's config or
# .gitattributes names, so both require `--no-ext-diff --no-textconv`. Any
# other token, `-c` and `--output` among them, matches no regex and denies.
_REPO = r"/(?:[A-Za-z0-9_.@+-]+/?)*"
_REPO_NO_DOTDOT = r"(?!.*(?:^|/)\.\.(?:/|$))" + _REPO
_REV = r"(?:[0-9a-fA-F]{7,40}|origin/[A-Za-z0-9_][A-Za-z0-9_/-]*(?:\.[A-Za-z0-9_/-]+)*)"
_RANGE = r"^" + _REV + r"\.\." + _REV + r"$"
_SINGLE = r"^" + _REV + r"$"
_C_FLAG = ["-C", _REPO_NO_DOTDOT]
_NO_DRIVERS = [["--no-ext-diff", ""], ["--no-textconv", ""]]
_READ_SHAPES = [
    {"argv0": "git", "env_prefix": False, "required_flags": [_C_FLAG] + _NO_DRIVERS,
     "one_goal_of": ["diff"], "free_args": _RANGE},
    {"argv0": "git", "env_prefix": False, "required_flags": [_C_FLAG] + _NO_DRIVERS,
     "one_goal_of": ["show"], "free_args": _SINGLE},
    {"argv0": "git", "env_prefix": False, "required_flags": [_C_FLAG, ["--oneline", ""]],
     "one_goal_of": ["log"], "free_args": _RANGE},
]


def role_tools(role, settings):
    return ["Bash"] if role == "ppl-review-responder" else []


def denied_tools(settings):
    return dict(_DENIED)


def denied_bash():
    return list(_DENIED_BASH)


def bash_shapes(role):
    return [dict(row) for row in _READ_SHAPES] if role == "ppl-review-responder" else []


def fragment(name):
    return read_fragment(KIND, name)


def doctor_check(settings, claude_json_path, workspace, runner=subprocess.run):
    gh = os_sandbox.gh_path()
    try:
        version = runner([gh, "--version"], capture_output=True, text=True, timeout=20)
        if version.returncode != 0:
            return {"ok": False, "detail": "gh --version failed: %s" % (version.stderr or "").strip()}
        auth = runner([gh, "auth", "status"], capture_output=True, text=True, timeout=20)
    except FileNotFoundError:
        return {"ok": False, "detail": "gh is not installed or not on PATH"}
    except OSError as exc:
        return {"ok": False, "detail": "gh could not be run: %s" % exc}
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "gh did not answer within 20s"}
    if auth.returncode != 0:
        return {"ok": False, "detail": "gh auth status failed, run gh auth login"}
    lines = (version.stdout or "").strip().splitlines()
    return {"ok": True, "detail": lines[0] if lines else "gh ok"}

import os
import subprocess

from .errors import PplError


def _git(repo, *args):
    # E1/E4, cycle 2 wave E: a repo path that does not exist as a directory
    # at all is not "GitLab, Jira or CI is unavailable" - the closed
    # vocabulary's EXTERNAL_BLOCKED remedy (references/escalation.md: "park,
    # retry on the next tick") never creates the missing worktree, so an
    # orchestrator that follows the documented remedy loops forever on a
    # permanent setup defect. This is the same shape as `ppl init` on a task
    # nobody ran `ppl init` for: a precondition was never satisfied, and the
    # fix is to satisfy it (run Stage 1 and create the worktree), not to
    # wait. Raised as SCHEMA_INVALID instead, whose documented remedy ("fix
    # the artifact or the invocation and retry once") is the right action.
    # A repo directory that DOES exist but where git itself fails (a lock
    # file, a corrupt object, a real permissions problem) is left as
    # EXTERNAL_BLOCKED below - that is a genuinely external condition
    # retrying can plausibly clear.
    if not os.path.isdir(str(repo)):
        raise PplError("SCHEMA_INVALID",
                       "repo %s does not exist. If this run's worktree has not been "
                       "created yet, create it now (see SKILL.md Stage 1) and retry - "
                       "this is not something to wait out." % repo)
    result = subprocess.run(["git", "-C", str(repo)] + list(args),
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise PplError("EXTERNAL_BLOCKED",
                       "git %s failed in %s: %s" % (" ".join(args), repo, result.stderr.strip()))
    return result.stdout.strip()


def is_repo(repo):
    try:
        return _git(repo, "rev-parse", "--is-inside-work-tree") == "true"
    except PplError:
        return False


def head_sha(repo):
    return _git(repo, "rev-parse", "HEAD")


def branch(repo):
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def changed_paths(repo, base, head):
    # A falsy base or head (an unrecorded base_sha, say) would otherwise diff
    # to an empty list, indistinguishable from "no changes" and letting the
    # done-gate's boundary check pass vacuously.
    # A missing base or head is a bad input document (e.g. a run whose
    # base_sha was never recorded), not a broken external system - the same
    # reasoning is_ancestor gives below for why its own two-revisions check
    # is SCHEMA_INVALID rather than EXTERNAL_BLOCKED. E1, cycle 2 wave E:
    # this used to be EXTERNAL_BLOCKED, whose documented remedy ("park,
    # retry on the next tick") does nothing for a base_sha that was simply
    # never written.
    if not base or not head:
        raise PplError("SCHEMA_INVALID",
                       "changed_paths needs both a base and a head revision, got base=%r head=%r"
                       % (base, head))
    out = _git(repo, "diff", "--name-only", "%s..%s" % (base, head))
    return [line for line in out.splitlines() if line]


def is_ancestor(repo, maybe_ancestor, head):
    """True when maybe_ancestor is reachable from head. A commit is its own ancestor.

    git merge-base --is-ancestor answers with its exit code: 0 yes, 1 no, and
    anything else is an error - in practice a revision that is not a valid
    object. That is a bad input document rather than a broken repository, since
    a repository this process cannot use has already failed in head_sha, so it
    is raised as SCHEMA_INVALID and not as EXTERNAL_BLOCKED.
    """
    if not maybe_ancestor or not head:
        raise PplError("SCHEMA_INVALID",
                       "is_ancestor needs two revisions, got %r and %r"
                       % (maybe_ancestor, head))
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor",
         str(maybe_ancestor), str(head)],
        capture_output=True, text=True)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise PplError("SCHEMA_INVALID",
                   "cannot compare %s with %s in %s: %s"
                   % (maybe_ancestor, head, repo, result.stderr.strip()))


def apply_patch(repo, diff_file):
    check = subprocess.run(["git", "-C", str(repo), "apply", "--check", str(diff_file)],
                           capture_output=True, text=True)
    if check.returncode != 0:
        raise PplError("IMPLEMENTATION_FAILURE",
                       "patch does not apply cleanly: %s" % check.stderr.strip())
    _git(repo, "apply", str(diff_file))

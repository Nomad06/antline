"""The orchestrator's git outside Claude Code's OS sandbox: a fetch of the default branch into
the checkout, and a push or a remote delete of the run's own branch from the run's repo, each
with the fixed -c words of PINNED_CONFIG. Both repositories are ones a role can write, so
before allowing a command the guard asks git itself, never a run file: the repository's local
and worktree config may set only keys that run nothing and point the remote at the forge repo
alone, and the pushed branch is the one HEAD is on. The guard hook imports this module, so it
parses on Python 3.9 and carries no annotations."""
import os
import re
import subprocess

# The -c words every fetch and push outside the OS sandbox carries first, in this order, as its
# only -c words. They turn off a hook, the repository's own ssh command, credential helper and
# askpass program, and the ext:: and local-path transports, since the command line wins over the
# repository's config, which a role can rewrite between the guard's check and the call.
# core.gitProxy and core.alternateRefsCommand are not among them, see the residual of
# R-OS-SANDBOX-GIT-CONFIG. os_sandbox renders the commands and the fetch allow entry from it.
PINNED_CONFIG = ["-c", "core.hooksPath=/dev/null", "-c", "core.sshCommand=ssh",
                 "-c", "credential.helper=", "-c", "core.askPass=",
                 "-c", "protocol.ext.allow=never", "-c", "protocol.file.allow=never"]
FETCH_FLAG = "--no-recurse-submodules"
# A push's first words. The command line wins over a global push.followTags or
# push.recurseSubmodules the guard cannot read, so no tag a role made and no submodule goes too.
PUSH_FLAGS = ["--no-verify", "--no-follow-tags", "--no-recurse-submodules"]
# Every git the guard runs gets this long: a hook that runs past its own timeout denies nothing.
TIMEOUT_SECONDS = 0.2
# Config keys known to run nothing and to send nothing elsewhere. core.hooksPath is here
# because PINNED_CONFIG wins over it.
GIT_SAFE_KEYS = (
    r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase"
    r"|precomposeunicode|symlinks|hookspath)",
    r"remote\.[^.]+\.(url|fetch)",
    r"branch\.[^.]+\.(remote|merge)",
    r"extensions\.(worktreeconfig|objectformat)",
    r"user\.(name|email)",
    r"lfs\.repositoryformatversion",
)
_SAFE_KEY = re.compile("|".join("(?:%s)" % key for key in GIT_SAFE_KEYS), re.IGNORECASE)
# The scopes a role can write: the repository's own config and its worktree's.
_CHECKED_SCOPES = ("local", "worktree")
_HEADS = "refs/heads/"
# The pipeline's <TASK-ID>-<slug>. A slash would let git read the word in another ref
# namespace: with no local main, `heads/main` is the remote's refs/heads/main to a push
# or a delete.
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_PSEUDO_REF = re.compile(r"(?:[A-Z_]*_)?HEAD", re.IGNORECASE)
_UPSTREAM = ("-u", "--set-upstream")
_SHAPE = ("git leaves the OS sandbox only as `git %s -C <dir> fetch --no-recurse-submodules "
          "origin <default branch>` or `... push --no-verify --no-follow-tags "
          "--no-recurse-submodules ...`, those -c words first, in that order, and no other "
          "option, so no hook, helper or local transport runs there" % " ".join(PINNED_CONFIG))
_PUSH_SHAPE = ("git push takes only --no-verify --no-follow-tags --no-recurse-submodules "
               "[-u|--set-upstream] origin refs/heads/<branch>:refs/heads/<branch>, or the same "
               "three words then origin --delete refs/heads/<branch>")


def _short(value):
    text = repr(value)
    return text if len(text) <= 120 else text[:117] + "..."


def git_shape(argv, facts, state, run_git=subprocess.run):
    """Why the orchestrator may not run `argv`, whose argv0 is the pinned git, outside the OS
    sandbox, or None. `state` is the active run's state, or None with no run. The words are
    matched before git runs on anything, so the guard's own git only ever reads the checkout
    or the run's repo."""
    args = list(argv[1:])
    count = len(PINNED_CONFIG)
    if args[:count] != PINNED_CONFIG:
        return "%s: %s comes first" % (_SHAPE, " ".join(_short(word) for word in args[:count]))
    if (args[count:count + 1] != ["-C"] or len(args) < count + 3
            or args[count + 2] not in ("fetch", "push")):
        return "%s: %s" % (_SHAPE, " ".join(_short(word) for word in args[count:count + 3]))
    if not facts.get("forge_repo"):
        return ("the harness names no GitHub forge repo, so git has nothing to reach outside "
                "the OS sandbox")
    directory, verb, rest = args[count + 1], args[count + 2], args[count + 3:]
    if verb == "fetch":
        remote, branch, problem = _fetch_words(directory, rest, facts)
    else:
        remote, branch, problem = _push_words(directory, rest, facts, state)
    if problem:
        return problem
    git = facts["pins"]["git"]["path"]
    problem = _config_problem(run_git, git, directory, remote, facts["forge_repo"])
    if problem or verb == "fetch":
        return problem
    return _head_problem(run_git, git, directory, branch)


def _fetch_words(directory, rest, facts):
    """("origin", None, None) for `fetch --no-recurse-submodules origin <default branch>` in
    the checkout, the words of ppl's allow entry, else (None, None, why). A submodule the
    checkout holds would be fetched with its own config, which the guard never reads."""
    if directory != facts["checkout"]:
        return None, None, ("git fetch runs only in the checkout %s, not %s"
                            % (facts["checkout"], _short(directory)))
    expected = [FETCH_FLAG, "origin", facts["default_branch"]]
    if rest != expected:
        return None, None, ("git fetch takes only %s, the default branch with no submodule"
                            % " ".join(expected))
    return "origin", None, None


def _push_words(directory, rest, facts, state):
    """(remote, branch, None) for an accepted push or delete in the run's repo, else
    (None, None, why)."""
    repo = (state or {}).get("repo")
    if not (isinstance(repo, str) and os.path.isabs(repo)):
        return None, None, "a git push needs an active run, whose repo it runs in"
    if directory != repo:
        return None, None, ("git push runs only in the run's repo %s, not %s"
                            % (repo, _short(directory)))
    words = rest[len(PUSH_FLAGS):] if rest[:len(PUSH_FLAGS)] == PUSH_FLAGS else None
    if words and len(words) == 3 and words[0] in _UPSTREAM:
        remote, refspec, delete = words[1], words[2], False
    elif words and len(words) == 3 and words[1] == "--delete":
        remote, refspec, delete = words[0], words[2], True
    elif words and len(words) == 2:
        remote, refspec, delete = words[0], words[1], False
    else:
        return None, None, _PUSH_SHAPE
    if remote != "origin":
        return None, None, "%s: %s is not origin" % (_PUSH_SHAPE, _short(remote))
    branch, problem = _refspec_branch(refspec, delete)
    if problem:
        return None, None, problem
    return remote, branch, _branch_problem(branch, facts["default_branch"])


def _refspec_branch(refspec, delete):
    """(branch, None) for refs/heads/<branch>:refs/heads/<branch>, or refs/heads/<branch> for a
    delete, else (None, why). A bare or unqualified refspec takes its destination from config a
    role can write: branch.<b>.merge under push.default=upstream moves it onto main."""
    if refspec.startswith("+"):
        return None, "%s is a refspec with a leading '+', a force push" % _short(refspec)
    if delete and refspec.startswith(_HEADS):
        return refspec[len(_HEADS):], None
    source, colon, destination = refspec.partition(":")
    if not delete and colon and source == destination and source.startswith(_HEADS):
        return source[len(_HEADS):], None
    return None, ("%s is not %s: a push names its source and its destination in full, so no "
                  "config can move it onto another ref"
                  % (_short(refspec), "refs/heads/<branch>" if delete
                     else "refs/heads/<branch>:refs/heads/<branch>"))


def _branch_problem(branch, default_branch):
    if ":" in branch:
        return ("%s is a refspec with ':', which could update another ref than the run's "
                "branch" % _short(branch))
    if branch.startswith("+"):
        return "%s is a refspec with a leading '+', a force push" % _short(branch)
    if branch.lower() == default_branch.lower():
        return ("%s is the default branch %s, which the pipeline never pushes or deletes"
                % (_short(branch), default_branch))
    if _PSEUDO_REF.fullmatch(branch):
        return "%s names a git pseudo-ref, not a branch" % _short(branch)
    if not _BRANCH.fullmatch(branch):
        return ("%s is not a plain branch name of letters, digits, '.', '_' and '-' with no "
                "'/', so git could read it as another ref" % _short(branch))
    return None


def _git(run_git, git, directory, args):
    """(exit code, stdout, None), or (None, None, why) when git cannot be run or outlasts
    TIMEOUT_SECONDS. Its output is captured, so none of it reaches the hook's stderr."""
    try:
        done = run_git([git, "-C", directory] + args, stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, None, "git %s did not answer within %s s in %s" % (
            args[0], TIMEOUT_SECONDS, directory)
    except (OSError, ValueError) as err:
        return None, None, "the pinned git %s cannot be run (%s)" % (git, type(err).__name__)
    return done.returncode, done.stdout.decode("utf-8", "replace"), None


def _forge_urls(repo):
    return ("https://github.com/%s" % repo, "https://github.com/%s.git" % repo,
            "git@github.com:%s" % repo, "git@github.com:%s.git" % repo)


def _config_problem(run_git, git, directory, remote, forge_repo):
    """None when the local and worktree config of `directory` set only GIT_SAFE_KEYS and give
    `remote` exactly one url, the forge repo on github.com. With -z an entry is its scope, then
    its key, a newline and its value, so a value holding a newline cannot pass for an entry."""
    code, output, problem = _git(run_git, git, directory,
                                 ["config", "--list", "--show-scope", "-z"])
    if problem:
        return problem
    if code != 0:
        return "git config --list exited %d in %s, so its config cannot be checked" % (
            code, directory)
    fields = output.split("\0")
    if fields[-1:] == [""]:
        fields.pop()
    if len(fields) % 2:
        return "git config --list in %s gave output the guard cannot read" % directory
    url_key = "remote.%s.url" % remote
    urls = []
    for scope, entry in zip(fields[0::2], fields[1::2]):
        if scope not in _CHECKED_SCOPES:
            continue
        key, _, value = entry.partition("\n")
        if not _SAFE_KEY.fullmatch(key):
            return ("the %s git config of %s sets %s, which could run a command or redirect the "
                    "remote: only the keys of escape_git.GIT_SAFE_KEYS may be set there, and no "
                    "include" % (scope, directory, _short(key)))
        if key == url_key:
            urls.append(value)
    allowed = _forge_urls(forge_repo)
    for url in urls:
        if url not in allowed:
            return ("remote %s in %s has url %s, not the forge repo %s on github.com"
                    % (remote, directory, _short(url), forge_repo))
    if len(urls) != 1:
        return ("remote %s in %s has %d urls in its local config, and needs exactly one, the "
                "forge repo %s on github.com" % (remote, directory, len(urls), forge_repo))
    return None


def _head_problem(run_git, git, directory, branch):
    """None when HEAD in `directory` is on refs/heads/<branch>. git is asked, never the run's
    state, which a role can write."""
    code, output, problem = _git(run_git, git, directory, ["symbolic-ref", "--quiet", "HEAD"])
    if problem:
        return problem
    head = output.strip()
    if code != 0:
        return "HEAD in %s is not on a branch (git symbolic-ref exited %d)" % (directory, code)
    if not head.startswith("refs/heads/"):
        return "HEAD in %s is not on a branch but on %s" % (directory, _short(head))
    if head != "refs/heads/" + branch:
        return "%s is not the branch HEAD is on in %s, %s" % (_short(branch), directory, head)
    return None

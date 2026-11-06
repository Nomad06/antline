"""A throwaway HOME, data root, sample repository with a stand-in for its GitHub remote, and a
gh stand-in, all driven through real ppl subprocesses. The run starts as an operator starts
one: `ppl setup`, then `ppl lane`, whose launch the demo takes without exec'ing claude. Every
ppl call after that runs as the lane's session runs it."""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import models, paths, tomlout
from ..errors import PplError
from . import sample

TASK = "GH-1"
TIMEOUT_SECONDS = 60
_STRIPPED_PREFIXES = ("PPL_", "GIT_", "GH_", "GITHUB_")
# These are the throwaway sample repository's commits, so the identity rule in CLAUDE.md,
# which is about this repository, does not apply to them.
_GIT_FLAGS = ("-c", "user.name=antline-demo", "-c", "user.email=demo@example.invalid",
              "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null")


class Sandbox:
    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.ppl_home = self.root / "ppl"
        # the sample project's checkout, the harness's [repo] path
        self.repo = self.root / "shop"
        # the bare repository the sandbox's git reaches in place of sample.REMOTE
        self.remote = self.root / "remote" / "shop.git"
        self.bin = self.root / "bin"
        # TASK's lane start directory and the run's worktree, once build has started the lane
        self.start = None
        self.worktree = None
        self.env = self._env()

    @classmethod
    def create(cls):
        root = Path(tempfile.mkdtemp(prefix="antline-demo-")).resolve()
        box = cls(root)
        for directory in (box.home, box.bin):
            directory.mkdir(parents=True, exist_ok=True)
        return box

    def _env(self):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(_STRIPPED_PREFIXES) and key not in models.SESSION_MARKERS}
        parent = str(paths.package_parent())
        env.update(
            HOME=str(self.home), PPL_HOME=str(self.ppl_home),
            PPL_CLAUDE_HOME=str(self.home / ".claude"), GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL="/dev/null", XDG_CONFIG_HOME=str(self.home / ".config"),
            # an operator's url rewrite to a local mirror, in the command scope of every git
            # in the sandbox, written to no config file: sample.REMOTE is fetched from and
            # pushed to self.remote, and nothing leaves the machine. ppl lane checks only the
            # checkout's local and worktree config, where origin stays the GitHub url.
            GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="url.%s.insteadOf" % self.remote,
            GIT_CONFIG_VALUE_0=sample.REMOTE,
            PATH=os.pathsep.join([str(self.bin), env.get("PATH", "")]),
            PYTHONPATH=os.pathsep.join(filter(None, [parent, env.get("PYTHONPATH")])))
        return env

    def _run(self, argv, **kwargs):
        try:
            return subprocess.run(argv, env=self.env, capture_output=True, text=True,
                                  timeout=TIMEOUT_SECONDS, **kwargs)
        except subprocess.TimeoutExpired as error:
            raise PplError("SCHEMA_INVALID", "demo sandbox: `%s` timed out after %d s"
                           % (" ".join(map(str, argv)), TIMEOUT_SECONDS)) from error

    def ppl(self, *args):
        """ppl as the operator runs it before the lane, and once the lane exists as its session
        runs it: in the start directory, with the environment ppl lane adds (PPL_TASK too)."""
        return self._run([sys.executable, "-m", "ppl", *args], cwd=self.start or self.root)

    def git(self, *args, cwd=None):
        """git in `cwd`, by default in the run's worktree."""
        argv = ["git", *_GIT_FLAGS, "-C", str(cwd or self.worktree), *args]
        done = self._run(argv)
        if done.returncode != 0:
            raise PplError("SCHEMA_INVALID", "demo sandbox: `git %s` failed: %s"
                           % (" ".join(args), (done.stderr or done.stdout).strip()[:500]))
        return done.stdout

    def commit(self, paths_, message, cwd=None):
        self.git("add", "--", *paths_, cwd=cwd)
        self.git("commit", "-q", "-m", message, cwd=cwd)
        return self.git("rev-parse", "HEAD", cwd=cwd).strip()

    def lane_settings(self):
        """The settings.json ppl lane rendered in TASK's start directory."""
        return self.start / "settings.json"

    def guard_argv(self):
        """The guard hook's command line as the lane's settings.json registers it for
        PreToolUse: it ends in --task TASK, which binds the hook to TASK's lane."""
        settings = json.loads(self.lane_settings().read_text(encoding="utf-8"))
        return shlex.split(settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"])

    def guard(self, payload):
        try:
            done = self._run(self.guard_argv(), cwd=self.start, input=json.dumps(payload))
        except PplError as error:
            return {"decision": "error", "code": None, "reason": error.message}
        if done.returncode != 0 or done.stderr.strip():
            return {"decision": "error", "code": None,
                    "reason": "exit %d: %s" % (done.returncode,
                                               (done.stderr or done.stdout).strip())}
        out = done.stdout.strip()
        if not out:
            return {"decision": "allow", "code": None, "reason": ""}
        spec = json.loads(out).get("hookSpecificOutput", {})
        if spec.get("permissionDecision") == "deny":
            code, _, reason = spec.get("permissionDecisionReason", "").partition(" ")
            return {"decision": "deny", "code": code, "reason": reason}
        return {"decision": "allow", "code": None, "reason": spec.get("permissionDecisionReason", "")}

    def _step(self, *args):
        done = self.ppl(*args)
        if done.returncode != 0:
            raise PplError("SCHEMA_INVALID", "demo sandbox: `ppl %s` failed: %s"
                           % (" ".join(args), (done.stderr or done.stdout).strip()[:500]))
        return done

    def build(self):
        self.repo.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main", cwd=self.repo)
        for rel, text in sample.FILES.items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.commit(sorted(sample.FILES), "base", cwd=self.repo)
        # origin is the GitHub repository the harness names as its forge, which ppl lane
        # requires before it fetches the run's base from there
        self.git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.root)
        self.git("remote", "add", "origin", sample.REMOTE, cwd=self.repo)
        self.git("push", "-q", "origin", "main", cwd=self.repo)
        source = self.root / "harness-src"
        doc, fragments = sample.harness(str(self.repo))
        source.mkdir()
        (source / "harness.toml").write_text(tomlout.dumps(doc), encoding="utf-8")
        for rel, text in fragments.items():
            (source / rel).parent.mkdir(parents=True, exist_ok=True)
            (source / rel).write_text(text, encoding="utf-8")
        # a committed repository, so `harness add` keeps it as it is and imports nothing under
        # the sandbox's empty git config, and ppl lane exports its HEAD (it has no origin)
        self.git("init", "-q", "-b", "main", cwd=source)
        self.git("add", "-A", cwd=source)
        self.git("commit", "-q", "-m", "demo harness", cwd=source)
        # first on PATH, so the lane's facts pin these and never a ppl or gh installed elsewhere
        for name, text in (("gh", sample.gh_source()), ("ppl", sample.ppl_source())):
            target = self.bin / name
            target.write_text(text, encoding="utf-8")
            target.chmod(0o755)
        self._step("setup", "--non-interactive", "--skip", "models", "--skip", "forge",
                   "--answer", "harness_from=%s" % source, "--answer", "harness_name=demo")
        launch = json.loads(self._step("lane", TASK, "--harness", "demo", "--no-exec").stdout)
        self.start = Path(launch["cwd"])
        self.env.update(launch["env"])
        self.worktree = Path(json.loads(
            (self.start / "lane.json").read_text(encoding="utf-8"))["worktree"])
        self._step("init", TASK, "--tracker-url", "https://github.com/demo/shop/issues/1",
                   "--harness", "demo", "--skip-model-check", "--no-os-sandbox")

    def remove(self):
        shutil.rmtree(self.root, ignore_errors=True)

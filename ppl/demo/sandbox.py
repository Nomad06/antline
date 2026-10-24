"""A throwaway HOME, data root, Claude workspace, sample repository and gh stand-in, all
driven through real ppl subprocesses."""
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
        self.workspace = self.root / "workspace"
        # beside the workspace, not in it: the OS sandbox profile denies every write under the
        # workspace, to sandboxed Bash and to the guard's edit tools alike
        self.repo = self.root / "shop"
        self.bin = self.root / "bin"
        self.env = self._env()

    @classmethod
    def create(cls):
        root = Path(tempfile.mkdtemp(prefix="antline-demo-")).resolve()
        box = cls(root)
        for directory in (box.home, box.workspace, box.bin):
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
        return self._run([sys.executable, "-m", "ppl", *args], cwd=self.workspace)

    def git(self, *args, cwd=None):
        argv = ["git", *_GIT_FLAGS, *(() if cwd else ("-C", str(self.repo))), *args]
        done = self._run(argv, cwd=cwd)
        if done.returncode != 0:
            raise PplError("SCHEMA_INVALID", "demo sandbox: `git %s` failed: %s"
                           % (" ".join(args), (done.stderr or done.stdout).strip()[:500]))
        return done.stdout

    def commit(self, paths_, message):
        self.git("add", "--", *paths_)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def guard(self, payload):
        settings = json.loads((self.workspace / ".claude" / "settings.local.json").read_text())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        try:
            done = self._run(shlex.split(command), cwd=self.workspace, input=json.dumps(payload))
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
        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.workspace)
        for rel, text in sample.FILES.items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        self.commit(sorted(sample.FILES), "base")
        source = self.root / "harness-src"
        doc, fragments = sample.harness(str(self.repo))
        source.mkdir()
        (source / "harness.toml").write_text(tomlout.dumps(doc), encoding="utf-8")
        for rel, text in fragments.items():
            (source / rel).parent.mkdir(parents=True, exist_ok=True)
            (source / rel).write_text(text, encoding="utf-8")
        gh = self.bin / "gh"
        gh.write_text(sample.gh_source(), encoding="utf-8")
        gh.chmod(0o755)
        self._step("setup", "--non-interactive", "--skip", "models", "--skip", "forge",
                   "--answer", "workspace=%s" % self.workspace, "--answer", "hooks=local",
                   "--answer", "harness_from=%s" % source, "--answer", "harness_name=demo")
        self._step("init", TASK, "--tracker-url", "https://github.com/demo/shop/issues/1",
                   "--repo", str(self.repo), "--harness", "demo", "--skip-model-check", "--no-os-sandbox")

    def remove(self):
        shutil.rmtree(self.root, ignore_errors=True)

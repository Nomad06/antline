import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import __version__

CLONE = Path(__file__).resolve().parents[1]
TIMEOUT = 180
SETUPTOOLS = "setuptools>=77"
STRIPPED_ENV = ("PYTHONPATH", "PPL_ROOT", "PPL_HOME", "PPL_CLAUDE_HOME", "PPL_LOCAL_SETTINGS_DIR")

_built = None


class Unavailable(Exception):
    pass


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in STRIPPED_ENV}
    env.update(extra)
    return env


def _run(cmd, cwd, env=None):
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd), env=env or _clean_env(),
                          capture_output=True, text=True, timeout=TIMEOUT)


def _must(cmd, cwd, what):
    proc = _run(cmd, cwd)
    if proc.returncode != 0:
        raise AssertionError("%s failed (exit %d)\n%s\n%s"
                             % (what, proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:]))
    return proc


def _venv_python(venv):
    return Path(venv) / "bin" / "python"


def _has_setuptools(python):
    check = ("import sys, setuptools; "
             "sys.exit(0 if int(setuptools.__version__.split('.')[0]) >= 77 else 1)")
    try:
        return _run([python, "-c", check], tempfile.gettempdir()).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _tracked_tree(dest):
    if not (CLONE / ".git").exists() or shutil.which("git") is None:
        raise Unavailable("not a git checkout")
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=str(CLONE), capture_output=True,
                             timeout=TIMEOUT)
    if listing.returncode != 0:
        raise Unavailable("git ls-files failed")
    for rel in listing.stdout.decode("utf-8").split("\0"):
        src = CLONE / rel
        if rel and src.exists():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target, follow_symlinks=False)


def _builder_python(work):
    if _has_setuptools(sys.executable):
        return sys.executable
    venv = work / "buildvenv"
    try:
        _must([sys.executable, "-m", "venv", venv], work, "build venv")
        _must([_venv_python(venv), "-m", "pip", "install", "--quiet", SETUPTOOLS], work,
              "pip install " + SETUPTOOLS)
    except (AssertionError, subprocess.SubprocessError, OSError) as exc:
        raise Unavailable("cannot get %s: %s" % (SETUPTOOLS, str(exc)[:300]))
    return _venv_python(venv)


def installed_venv():
    """(python, ppl, package_dir, env) of the wheel installed in a fresh venv, built once."""
    global _built
    if _built is not None:
        return _built
    work = Path(tempfile.mkdtemp(prefix="pplpkg-")).resolve()
    atexit.register(shutil.rmtree, work, ignore_errors=True)
    tree, dist, venv = work / "tree", work / "dist", work / "venv"
    tree.mkdir()
    dist.mkdir()
    _tracked_tree(tree)
    builder = _builder_python(work)
    _must([builder, "-c", "from setuptools import build_meta; build_meta.build_sdist(%r)"
           % str(dist)], tree, "sdist build")
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise AssertionError("expected one sdist, found %s" % sdists)
    _must([builder, "-m", "pip", "wheel", "--quiet", "--no-deps", "--no-build-isolation",
           "-w", dist, sdists[0]], work, "wheel build")
    wheels = sorted(dist.glob("*.whl"))
    if len(wheels) != 1:
        raise AssertionError("expected one wheel, found %s" % wheels)
    _must([sys.executable, "-m", "venv", venv], work, "install venv")
    python = _venv_python(venv)
    _must([python, "-m", "pip", "install", "--quiet", "--no-deps", "--no-index", wheels[0]],
          work, "wheel install")
    found = _run([python, "-I", "-c", "import ppl, pathlib; print(pathlib.Path(ppl.__file__).parent)"],
                 work)
    if found.returncode != 0:
        raise AssertionError("installed ppl does not import\n" + found.stderr)
    _built = (python, venv / "bin" / "ppl", Path(found.stdout.strip()).resolve(), _clean_env())
    return _built


def _files(root):
    out = set()
    for path in Path(root).rglob("*"):
        rel = path.relative_to(root)
        if path.is_file() and "__pycache__" not in rel.parts and path.suffix != ".pyc":
            out.add(rel.as_posix())
    return out


class TestWheel(unittest.TestCase):
    """Builds an sdist from `git ls-files`, a wheel from that sdist, installs it into a
    fresh venv (about 20 s with setuptools at hand, more with a build venv)."""

    @classmethod
    def setUpClass(cls):
        try:
            cls.python, cls.ppl, cls.package_dir, cls.env = installed_venv()
        except Unavailable as exc:
            if os.environ.get("PPL_REQUIRE_PACKAGING") == "1":
                raise AssertionError("packaging test cannot run: %s" % exc)
            raise unittest.SkipTest(str(exc))

    def test_the_installed_package_holds_every_tracked_package_file(self):
        listing = subprocess.run(["git", "ls-files", "ppl"], cwd=str(CLONE), capture_output=True,
                                 text=True, check=True).stdout.split("\n")
        tracked = {line for line in listing
                   if line and "__pycache__" not in line and not line.endswith(".pyc")}
        installed = {"ppl/" + rel for rel in _files(self.package_dir)}
        self.assertEqual(sorted(tracked - installed), [], "tracked but not installed")
        self.assertEqual(sorted(installed - tracked), [], "installed but not tracked")

    def test_the_installed_cli_reports_its_version_and_its_own_code_root(self):
        scratch = Path(tempfile.mkdtemp(prefix="pplrun-")).resolve()
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        env = dict(self.env, PPL_HOME=str(scratch / "pplhome"), HOME=str(scratch / "home"))
        (scratch / "home").mkdir()
        version = _run([self.ppl, "--version"], scratch, env)
        self.assertEqual((version.returncode, version.stdout), (0, "antline %s\n" % __version__))
        doctor = _run([self.ppl, "doctor"], scratch, env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        report = json.loads(doctor.stdout.splitlines()[0])
        self.assertEqual(Path(report["code_root"]).resolve(),
                         (self.package_dir / "_assets").resolve())
        where = _run([self.python, "-I", "-c", "import ppl; print(ppl.__file__)"], scratch, env)
        self.assertEqual(where.returncode, 0, where.stderr)
        venv = self.python.parent.parent.resolve()
        self.assertTrue(Path(where.stdout.strip()).resolve().is_relative_to(venv),
                        where.stdout)

    def test_the_installed_guard_denies_a_force_push(self):
        scratch = Path(tempfile.mkdtemp(prefix="pplrun-")).resolve()
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        env = dict(self.env, PPL_HOME=str(scratch / "pplhome"), HOME=str(scratch))
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "git push --force origin main"}, "cwd": "/tmp"}
        proc = subprocess.run(
            [str(self.python), str(self.package_dir / "_assets" / "hooks" / "ppl_guard.py")],
            input=json.dumps(payload), cwd=str(scratch), env=env, capture_output=True,
            text=True, timeout=TIMEOUT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        decision = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertTrue(decision["permissionDecisionReason"].startswith("BOUNDARY_VIOLATION"),
                        decision["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()

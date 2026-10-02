"""Shared fixtures: install an example harness into the current PPL_HOME."""
import os
import shutil
import subprocess
from pathlib import Path

import tmphome
from ppl import paths

SOURCE = Path(__file__).resolve().parents[1]


def install_harness(name, as_name=None):
    target = paths.harness_dir(as_name or name)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE / "defaults" / "harnesses" / name, target)
    return target


def install_routing():
    paths.home().mkdir(parents=True, exist_ok=True)
    shutil.copy(SOURCE / "defaults" / "routing.example.toml", paths.routing_file())


def render_skill_to(directory, name="example-maven"):
    """Render the orchestrator skill for an example harness into
    <directory>/pipeline and return that directory. The harness is installed
    into a throwaway PPL_HOME: a module that renders at import time must not
    leave a harness behind in the data root the other test modules share."""
    from ppl import harness
    saved = os.environ.get("PPL_HOME")
    os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplrender-")
    try:
        install_harness(name)
        harness.write_skill(harness.load(name), Path(directory))
    finally:
        if saved is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = saved
    return Path(directory) / "pipeline"


# The interpreter the version floor is proven against: the stock macOS python3
# is 3.9. Elsewhere there is usually no older python3 at this path, and the
# tests that need one skip.
SYSTEM_PYTHON = "/usr/bin/python3"


def isolated_env(**extra):
    """os.environ with HOME and PPL_HOME at fresh temp dirs: the stock macOS
    python3 writes a bytecode cache under ~/Library/Caches."""
    return dict(os.environ, HOME=tmphome.mkdtemp(prefix="pplfakehome-"),
                PPL_HOME=tmphome.mkdtemp(prefix="pplhome-"), **extra)


def older_system_python():
    """(path, "X.Y.Z") of SYSTEM_PYTHON when it runs and is older than 3.11,
    else None."""
    if not os.access(SYSTEM_PYTHON, os.X_OK):
        return None
    try:
        result = subprocess.run(
            [SYSTEM_PYTHON, "-c", "import sys; v = sys.version_info; "
             "print('%d.%d.%d %d' % (v[0], v[1], v[2], v[:2] < (3, 11)))"],
            capture_output=True, text=True, timeout=30, env=isolated_env())
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.split()
    if result.returncode != 0 or len(fields) != 2 or fields[1] != "1":
        return None
    return SYSTEM_PYTHON, fields[0]

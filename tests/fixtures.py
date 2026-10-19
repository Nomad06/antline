"""Shared fixtures: install an example harness into the current PPL_HOME."""
import copy
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import tmphome
from ppl import paths, tomlout

SOURCE = Path(__file__).resolve().parents[1]

# The tables that turn an example harness into a GitHub Issues one.
GITHUB_ISSUES = {"tracker": {"kind": "github-issues", "repo": "o/r", "task_prefix": "GH"},
                 "task": {"key_pattern": "^GH-[0-9]+$",
                          "url_template": "https://github.com/o/r/issues/<TASK-ID-number>"}}


def install_harness(name, as_name=None):
    target = paths.harness_dir(as_name or name)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SOURCE / "defaults" / "harnesses" / name, target)
    return target


def install_variant(base, as_name, **tables):
    """Install example harness `base` as `as_name` with each named table replaced
    whole, so the variant keeps no key of the table it replaces."""
    target = install_harness(base, as_name)
    path = target / "harness.toml"
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    for name, table in tables.items():
        doc[name] = copy.deepcopy(table)
    path.write_text(tomlout.dumps(doc), encoding="utf-8")
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


def skill_files(name="example-maven", fragments=None):
    """relative path -> text of every file of the skill rendered for an example harness,
    nothing written. `fragments` maps a forge fragment name to the text rendered in its
    place, so a drift can be seeded where the real text comes from."""
    from unittest import mock
    from ppl import forges, harness
    saved = os.environ.get("PPL_HOME")
    os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplrender-")
    try:
        install_harness(name)
        doc = harness.load(name)
        module = forges.get(doc["forge"]["kind"])
        real = module.fragment
        seeded = dict(fragments or {})
        with mock.patch.object(module, "fragment", lambda part: seeded.get(part) or real(part)):
            return harness.render_skill(doc)
    finally:
        if saved is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = saved


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

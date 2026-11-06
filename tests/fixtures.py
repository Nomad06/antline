"""Shared fixtures: install an example harness into the current PPL_HOME."""
import copy
import os
import shutil
import subprocess
import tomllib
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import tmphome
from ppl import paths, tomlout

SOURCE = Path(__file__).resolve().parents[1] / "ppl" / "_assets"

# The pins a GitHub harness renders its commands outside the OS sandbox on. None is looked up
# on PATH, so a render needs no gh or ppl installed.
FAKE_PINS = {name: {"path": "/opt/pin/bin/" + name, "realpath": "/opt/pin/bin/" + name}
             for name in ("gh", "git", "ppl")}

# What a lane's sandbox can leave in its run's state.json or log that no JSON parse turns into
# a document: an integer of more than 4,300 digits, a plain ValueError on every supported
# Python, and nesting that raises RecursionError on Python 3.11-3.13 (3.14 parses it).
UNPARSEABLE_JSON = {"digits": "1" * 4301, "deep": "[" * 100000 + "]" * 100000}

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
    from unittest import mock
    from ppl import harness, os_sandbox
    saved = os.environ.get("PPL_HOME")
    os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplrender-")
    try:
        install_harness(name)
        with mock.patch.object(os_sandbox, "pins", lambda which=None: FAKE_PINS):
            harness.write_skill(harness.load(name), Path(directory))
    finally:
        if saved is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = saved
    return Path(directory) / "pipeline"


def skill_files(name="example-maven", fragments=None, tables=None, pins=None):
    """relative path -> text of every file of the skill rendered for an example harness,
    nothing written. `fragments` maps a forge fragment name to the text rendered in its
    place, so a drift can be seeded where the real text comes from. `tables` replaces
    whole tables of the harness, installed under `name` itself in the throwaway home.
    `pins` stands in for the binaries found on PATH, FAKE_PINS by default."""
    from unittest import mock
    from ppl import forges, harness, os_sandbox
    saved = os.environ.get("PPL_HOME")
    os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplrender-")
    try:
        if tables:
            install_variant(name, name, **tables)
        else:
            install_harness(name)
        doc = harness.load(name)
        module = forges.get(doc["forge"]["kind"])
        real = module.fragment
        seeded = dict(fragments or {})
        with mock.patch.object(module, "fragment",
                               lambda part: seeded[part] if part in seeded else real(part)), \
                mock.patch.object(os_sandbox, "pins", lambda which=None: pins or FAKE_PINS):
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


def commit_harness(target):
    """Make an installed harness directory a git repository with one commit, as `ppl harness
    add` leaves a plain directory, under a test-only identity (a temporary repository, never
    this project's). Returns `target`."""
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
                  "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "Import")):
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)
    return target


def lane_settings(task_id):
    """The hooks a lane's settings.json carries as `ppl lane` renders them, each command ending
    in --task <task_id>, and no OS sandbox profile."""
    from ppl import setup
    return {"hooks": {event: [entry] for event, entry in
                      setup.hook_entries(paths.root(), task_id).items()}}


def bind_lane(task_id, repo, worktree=None, lane=1, harness="example-maven",
              session_id="00000000-0000-4000-8000-000000000001", branch=None, settings=True):
    """Register a lane for `task_id` and create its start directory, as `ppl lane` leaves them.
    The entry is written directly with the given lane number, and the start directory gets the
    lane's record lane.json (its worktree, checkout and `branch`) and, with `settings`, its
    settings.json (lane_settings). An installed harness is copied to <start>/harness/<harness>,
    what `ppl lane` exports, and the machine's routing.toml to <start>/routing.toml, the copy
    `ppl lane` freezes (D15). Returns the entry."""
    from ppl import lanes, store
    start = lanes.start_dir(task_id)
    start.mkdir(parents=True, exist_ok=True)
    store.write_json(lanes.record_path(task_id), {
        "task": task_id, "worktree": str(worktree or repo), "checkout": str(repo),
        "branch": branch, "harness": harness})
    if settings:
        store.write_json(start / "settings.json", lane_settings(task_id))
    installed = paths.harness_dir(harness)
    if installed.is_dir():
        target = start / "harness" / harness
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(installed, target)
    if paths.machine_routing_file().is_file():
        shutil.copyfile(paths.machine_routing_file(), start / "routing.toml")
    record = {"lane": lane, "task": task_id, "harness": harness, "harness_sha": "0" * 40,
              "repo": str(repo), "worktree": str(worktree or repo), "start_dir": str(start),
              "session_id": session_id, "pid": None, "started": store.now()}
    kept = [item for item in lanes.read() if item.get("task") != task_id]
    store.write_json(lanes.registry_path(), {"version": 1, "lanes": kept + [record]})
    return record


@contextmanager
def git_identity():
    """Give git a test-only identity (and no signing) through the environment, so a
    plain directory can be imported as a harness without the developer's or CI's
    global git config. Never used for this project's own commits."""
    config = Path(tmphome.mkdtemp(prefix="pplgitcfg-")) / "gitconfig"
    config.write_text("[user]\n\tname = t\n\temail = t@t\n[commit]\n\tgpgsign = false\n",
                      encoding="utf-8")
    with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1"}):
        yield

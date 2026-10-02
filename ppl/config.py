"""config.toml: per-machine settings in the data root. It records the active
harness, where the generated agents and skill go, and the `ppl setup`
bookkeeping in [setup]."""
import os
import shutil
import tomllib
from pathlib import Path

from . import paths
from .errors import PplError

STEP_STATES = ("done", "skipped")


def load():
    path = paths.config_file()
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise PplError("SCHEMA_INVALID", "config.toml at %s is not valid TOML: %s" % (path, exc))


def _section(doc, name):
    section = doc.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: [%s] must be a table" % (paths.config_file(), name))
    return section


def section(name):
    """One table of config.toml, empty when the file or the table is absent."""
    return _section(load(), name)


def ensure():
    """config.toml and routing.toml exist in the data root, copied from
    defaults/ when absent. An existing file is never touched."""
    paths.home().mkdir(parents=True, exist_ok=True)
    if not paths.config_file().is_file():
        shutil.copy(paths.defaults_dir() / "config.example.toml", paths.config_file())
    if not paths.routing_file().is_file():
        shutil.copy(paths.defaults_dir() / "routing.example.toml", paths.routing_file())
    return load()


def active_harness():
    active = _section(load(), "harness").get("active")
    if active is not None and not isinstance(active, str):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: harness.active must be a string" % paths.config_file())
    return active or None


def _dir_setting(key, env_var, fallback):
    """The environment wins over config, so a test or a one-off render
    (PPL_AGENTS_DIR=... ppl harness apply) never writes into the real dir."""
    if os.environ.get(env_var):
        return Path(os.environ[env_var])
    value = _section(load(), "claude").get(key)
    if value is not None and not isinstance(value, str):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: claude.%s must be a string" % (paths.config_file(), key))
    return Path(value).expanduser() if value else fallback


def skills_dir():
    """Where `ppl harness apply` writes the skill."""
    return _dir_setting("skills_dir", "PPL_SKILLS_DIR", Path.home() / ".claude" / "skills")


def agents_dir():
    """Where `ppl harness apply` writes the agent files."""
    return _dir_setting("agents_dir", "PPL_AGENTS_DIR", Path.home() / ".claude" / "agents")


def set_active_harness(name):
    doc = load()
    _section(doc, "harness")
    doc.setdefault("harness", {})["active"] = name
    save(doc)


def _step_lists(doc):
    section = _section(doc, "setup")
    lists = {}
    for state in STEP_STATES:
        value = section.get(state, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise PplError("SCHEMA_INVALID", "config.toml at %s: setup.%s must be a list of "
                           "step names" % (paths.config_file(), state))
        lists[state] = set(value)
    return lists


def steps_state():
    return _step_lists(load())


def mark_step(name, state):
    """Record a step as done or skipped, or with state None as neither. Both
    lists stay sorted and free of duplicates, and a step is in at most one
    of them."""
    if state is not None and state not in STEP_STATES:
        raise PplError("SCHEMA_INVALID", "step state must be one of %s, got %r"
                       % (", ".join(STEP_STATES), state))
    doc = ensure()
    lists = _step_lists(doc)
    for members in lists.values():
        members.discard(name)
    if state is not None:
        lists[state].add(name)
    section = doc.setdefault("setup", {})
    section.setdefault("version", 1)
    for key, members in lists.items():
        section[key] = sorted(members)
    save(doc)


def update(section, values):
    """Merge `values` into one table of config.toml, creating the file from
    the example when it is absent."""
    doc = ensure()
    _section(doc, section)
    doc.setdefault(section, {}).update(values)
    save(doc)


def save(doc):
    from . import tomlout
    text = tomlout.dumps(doc)
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text(text, encoding="utf-8")

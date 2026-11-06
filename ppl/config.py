"""config.toml: per-machine settings in the data root. It records the Claude Code binary, the
default harness, the limits and the `ppl setup` bookkeeping in [setup]."""
import math
import shutil
import tomllib

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


BUDGET_KEYS = ("usd_max", "usd_max_per_day")


def _budget_cap(key):
    table = _section(load(), "budgets")
    unknown = sorted(name for name in table if name not in BUDGET_KEYS)
    if unknown:
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: [budgets] has the unknown key %s, the known keys are %s"
                       % (paths.config_file(), ", ".join(unknown), ", ".join(BUDGET_KEYS)))
    value = table.get(key, 0.0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: budgets.%s must be a number >= 0"
                       % (paths.config_file(), key))
    return float(value)


def budgets_usd_max():
    """[budgets] usd_max, the run's dollar cap that ppl init copies. 0.0, no cap,
    when absent. An unknown key is refused: a misspelt cap must not leave the run
    uncapped."""
    return _budget_cap("usd_max")


def budgets_usd_max_per_day():
    """[budgets] usd_max_per_day, the dollar cap on the local day's spend across every run on
    this machine. 0.0, no cap, when absent, and validated like usd_max."""
    return _budget_cap("usd_max_per_day")


LESSON_KEYS = ("reflect",)


def lessons_reflect():
    """[lessons] reflect, whether a merged run's evidence is bundled for the reflector. True
    when absent. An unknown key or a reflect that is not a boolean is refused, the [budgets]
    pattern: a misspelt switch must not leave reflection on while the owner believes it off."""
    table = _section(load(), "lessons")
    unknown = sorted(key for key in table if key not in LESSON_KEYS)
    if unknown:
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: [lessons] has the unknown key %s, the known key is %s"
                       % (paths.config_file(), ", ".join(unknown), ", ".join(LESSON_KEYS)))
    value = table.get("reflect", True)
    if not isinstance(value, bool):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: lessons.reflect must be true or false"
                       % paths.config_file())
    return value


LANE_KEYS = ("max",)


def lanes_max():
    """[lanes] max, how many task windows may run at once on this machine: an integer
    1..9, 3 when absent. An unknown key, a bool or a string is refused, the [budgets]
    pattern: a misspelt limit must not leave the machine on the default unseen."""
    table = _section(load(), "lanes")
    unknown = sorted(key for key in table if key not in LANE_KEYS)
    if unknown:
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: [lanes] has the unknown key %s, the known key is %s"
                       % (paths.config_file(), ", ".join(unknown), ", ".join(LANE_KEYS)))
    value = table.get("max", 3)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 9:
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: lanes.max must be an integer from 1 to 9"
                       % paths.config_file())
    return value


def set_active_harness(name):
    """[harness] active, the harness `ppl lane` runs when it is given none. A memory_sha256
    an older `ppl harness apply` recorded beside it is dropped: nothing reads it."""
    doc = load()
    _section(doc, "harness")
    doc.setdefault("harness", {})["active"] = name
    doc["harness"].pop("memory_sha256", None)
    save(doc)


def remove(section, keys):
    """Drop `keys` from one table of config.toml and return the ones that were there."""
    doc = load()
    table = _section(doc, section)
    present = [key for key in keys if key in table]
    if present:
        for key in present:
            doc[section].pop(key)
        save(doc)
    return present


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

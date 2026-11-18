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
    # the machine's, which a lane only reads a frozen copy of (D15)
    if not paths.machine_routing_file().is_file():
        shutil.copy(paths.defaults_dir() / "routing.example.toml", paths.machine_routing_file())
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


STALL_KEYS = ("notify_after_minutes", "silent_after_minutes", "review_notify_after_minutes",
              "keep_awake")
STALL_DEFAULTS = {"notify_after_minutes": 15, "silent_after_minutes": 60,
                  "review_notify_after_minutes": 45, "keep_awake": False}


def stall_settings():
    """[stall], the thresholds stall detection reads: every key of STALL_KEYS, defaults
    where absent. Each minutes value is an integer from 1 to 1440 and keep_awake a boolean.
    An unknown key, a bool or a string for a number is refused, the [budgets] pattern: a
    misspelt threshold must not leave detection on a default the owner believes changed."""
    table = _section(load(), "stall")
    unknown = sorted(key for key in table if key not in STALL_KEYS)
    if unknown:
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: [stall] has the unknown key %s, the known keys are %s"
                       % (paths.config_file(), ", ".join(unknown), ", ".join(STALL_KEYS)))
    settings = dict(STALL_DEFAULTS)
    settings.update(table)
    for key in STALL_KEYS[:3]:
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1440:
            raise PplError("SCHEMA_INVALID",
                           "config.toml at %s: stall.%s must be an integer from 1 to 1440"
                           % (paths.config_file(), key))
    if not isinstance(settings["keep_awake"], bool):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: stall.keep_awake must be true or false"
                       % paths.config_file())
    return settings


NOTIFY_KEYS = ("command",)


def notify_command():
    """[notify] command, the argv a stall notification also runs, with the task id, the stage,
    the kind and the minutes appended. None when absent: nothing leaves the machine unless the
    owner names a command here. It must be a non-empty list of non-empty strings, and an
    unknown key is refused, so a misspelt table cannot silently send nothing."""
    table = _section(load(), "notify")
    unknown = sorted(key for key in table if key not in NOTIFY_KEYS)
    if unknown:
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: [notify] has the unknown key %s, the known key is %s"
                       % (paths.config_file(), ", ".join(unknown), ", ".join(NOTIFY_KEYS)))
    command = table.get("command")
    if command is None:
        return None
    if (not isinstance(command, list) or not command
            or not all(isinstance(word, str) and word for word in command)):
        raise PplError("SCHEMA_INVALID",
                       "config.toml at %s: notify.command must be a non-empty list of "
                       "non-empty strings" % paths.config_file())
    return list(command)


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

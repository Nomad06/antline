import os
import re
from pathlib import Path

from .errors import PplError

TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")


def root():
    """The code checkout. bin/ppl sets PPL_ROOT, a plain `python3 -m ppl` falls
    back to the package's own parent. A root with no defaults/ is not a checkout,
    and is refused here, naming the fix, rather than step by step later."""
    configured = os.environ.get("PPL_ROOT")
    checkout = Path(configured or Path(__file__).resolve().parents[1])
    if (checkout / "defaults").is_dir():
        return checkout
    if configured:
        raise PplError("SCHEMA_INVALID",
                       "PPL_ROOT=%s has no defaults/ directory, so it is not an antline "
                       "checkout. Point PPL_ROOT at the clone, or unset it and run bin/ppl "
                       "from the clone." % configured)
    raise PplError("SCHEMA_INVALID",
                   "ppl's code root %s has no defaults/ directory: a non-editable install "
                   "(pip install . or pipx install .) copies the ppl package alone, without the "
                   "defaults, templates, skill and hooks it reads. Run ppl from a clone with "
                   "bin/ on PATH, install the clone with pip install -e ., or set PPL_ROOT to "
                   "the clone." % checkout)


def home():
    """The data root: config, routing, verified models, harnesses, runs."""
    return Path(os.environ.get("PPL_HOME", Path.home() / ".ppl"))


def defaults_dir():
    return root() / "defaults"


def runs_dir():
    return home() / "runs"


def config_file():
    return home() / "config.toml"


def routing_file():
    return home() / "routing.toml"


def models_file():
    return home() / "models.json"


def harnesses_dir():
    return home() / "harnesses"


def harness_dir(name):
    return harnesses_dir() / name


def check_task_id(task_id):
    if not TASK_ID_RE.match(task_id or ""):
        raise PplError("SCHEMA_INVALID",
                       "task id must match %s, got: %r" % (TASK_ID_RE.pattern, task_id))
    return task_id


def run_dir(task_id):
    return runs_dir() / check_task_id(task_id)


def active_file():
    return runs_dir() / "ACTIVE"

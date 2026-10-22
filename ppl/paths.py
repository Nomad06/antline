import os
import re
from pathlib import Path

from .errors import PplError

TASK_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")


PACKAGE_ASSETS = Path(__file__).resolve().parent / "_assets"


def package_parent():
    """The directory holding the ppl package, for a child's PYTHONPATH."""
    return Path(__file__).resolve().parents[1]


def root():
    """The assets directory: PPL_ROOT when set, else the package's own. A root
    with no defaults/ is refused here, naming the fix, rather than step by step
    later."""
    configured = os.environ.get("PPL_ROOT")
    assets = Path(configured) if configured else PACKAGE_ASSETS
    if (assets / "defaults").is_dir():
        return assets
    if configured:
        message = ("PPL_ROOT=%s has no defaults/ directory, so it is not an assets directory. "
                   "Point PPL_ROOT at a directory holding defaults/, or unset it." % configured)
        if (assets / "ppl" / "_assets" / "defaults").is_dir():
            message += (" It looks like a clone: the assets moved into the package, so use "
                        "PPL_ROOT=%s or unset it." % (assets / "ppl" / "_assets"))
        raise PplError("SCHEMA_INVALID", message)
    raise PplError("SCHEMA_INVALID",
                   "ppl's assets directory %s has no defaults/ directory: the installation "
                   "is incomplete. Reinstall ppl." % assets)


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

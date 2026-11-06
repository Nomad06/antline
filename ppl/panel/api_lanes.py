"""The Lanes page: one read-only list of the task windows running on this machine. Only the
fields the page shows leave here; the registry's session ids, pids and start directories do
not. `stage` and `outcome` are read from a run's state.json, which a lane's sandbox can write,
so they are data for the page to print as text and nothing more."""
from .. import config, lane_launch, status, usage
from ..errors import PplError

SHOWN = ("lane", "task", "harness", "stage", "outcome", "alive", "resume")


def _usd(task):
    try:
        return status.usd_text(task)
    except (PplError, OSError, ValueError, KeyError, TypeError):
        return ""


def _day():
    """Today's machine spend against [budgets] usd_max_per_day (0, no cap). `issues` counts, per
    kind, the runs spent_today could not count; the runs' names stay out of the response."""
    day = {"usd": None, "cap": None, "issues": {}, "error": None}
    try:
        day["cap"] = config.budgets_usd_max_per_day()
        day["usd"], issues = usage.spent_today()
        day["issues"] = {kind: len(names) for kind, names in issues.items()}
    except (PplError, OSError, ValueError) as exc:
        day["error"] = getattr(exc, "message", None) or str(exc)
    return day


def overview(params, body, query):
    answer = {"lanes": [], "machine_max": None, "day": _day(), "error": None}
    try:
        answer["machine_max"] = config.lanes_max()
    except PplError as exc:
        answer["error"] = exc.message
    try:
        rows = lane_launch.listing()
    except (PplError, OSError) as exc:
        answer["error"] = getattr(exc, "message", None) or str(exc)
        return answer
    for row in rows:
        shown = {key: row.get(key) for key in SHOWN}
        shown["usd"] = _usd(row.get("task"))
        answer["lanes"].append(shown)
    return answer


def register(router):
    router.add("GET", "/api/lanes", overview)

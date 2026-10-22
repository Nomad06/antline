"""Drafts Claude made, kept under the data root so the same input is not paid for
twice. An entry is {created_at, model, cost_usd, raw}: raw is the answer before
extract.check, which runs again on every reuse. The cache is an optimization:
load never raises and save leaves the failure to its caller to log."""
from datetime import datetime, timezone

from .. import paths, store

KEEP = 50


def directory():
    return paths.home() / "studio" / "cache" / "extract"


def _age_days(created_at):
    created = datetime.fromisoformat(created_at)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400


def load(key, max_age_days):
    """The entry, or None when it is missing, unreadable, wrongly shaped or older
    than `max_age_days`."""
    try:
        entry = store.read_json(directory() / (key + ".json"))
        if not isinstance(entry, dict) or "raw" not in entry:
            return None
        if not isinstance(entry.get("created_at"), str) or not isinstance(entry.get("model"), str):
            return None
        cost = entry.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            return None
        if _age_days(entry["created_at"]) > max_age_days:
            return None
        return entry
    except Exception:
        return None


def save(key, entry):
    """Write the entry, then delete all but the KEEP newest by mtime."""
    store.write_json(directory() / (key + ".json"), entry)
    files = []
    for path in directory().glob("*.json"):
        try:
            files.append((path.stat().st_mtime, path.name, path))
        except OSError:
            continue
    files.sort(reverse=True)
    for _, _, path in files[KEEP:]:
        try:
            path.unlink()
        except OSError:
            pass

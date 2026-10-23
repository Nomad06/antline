"""Token usage read from Claude Code transcripts, summed per model.

The transcript format is Claude Code's own, not a public contract. It was
probed on Claude Code 2.1.308: one API response spans several `assistant`
lines sharing a `message.id` and an identical `message.usage`, so lines are
deduplicated by id and the top-level usage only is read, never
`usage.iterations`. Thinking tokens are part of output: never added to a
total and never priced apart.
"""

import json
import math
import os
import stat
import sys
import time
from datetime import datetime

from ppl import config, paths, run, store
from ppl.errors import PplError

COUNTS = ("input", "cache_write", "cache_read", "output")
MAX_TRANSCRIPT_BYTES = 20 * 1024 * 1024
_FIELDS = (("input", "input_tokens"),
           ("cache_write", "cache_creation_input_tokens"),
           ("cache_read", "cache_read_input_tokens"),
           ("output", "output_tokens"))
_CHECK_EVERY = 200


class TranscriptError(Exception):
    """`code` is one of transcript_missing, transcript_not_regular,
    transcript_too_large, deadline, schema_unknown."""

    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def is_count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    # an int too large for a float is no count: pricing it would overflow
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _number(value):
    return int(value) if is_count(value) else 0


def _counts_of(use):
    got = {name: _number(use.get(field)) for name, field in _FIELDS}
    details = use.get("output_tokens_details")
    got["thinking"] = _number(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    return got


def _assistant_usage(event):
    """(message id, model, usage dict) of an assistant line, else None."""
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return None
    message = event.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return None
    use = message["usage"]
    if not any(is_count(use.get(field)) for _, field in _FIELDS):
        return None
    return message.get("id"), message.get("model"), use


def _parse(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _check_file(path):
    try:
        info = os.lstat(path)
    except OSError:
        raise TranscriptError("transcript_missing", "transcript not found: %s" % path)
    if not stat.S_ISREG(info.st_mode):
        raise TranscriptError("transcript_not_regular", "transcript is not a regular file")
    return info


def _expired(deadline):
    return deadline is not None and time.monotonic() >= deadline


def _bucket(models, model):
    return models.setdefault(model, {"input": 0, "cache_write": 0, "cache_read": 0,
                                     "output": 0, "thinking": 0})


def sum_transcript(path, deadline=None):
    info = _check_file(path)
    if info.st_size > MAX_TRANSCRIPT_BYTES:
        raise TranscriptError("transcript_too_large",
                              "transcript is %d bytes, over the %d cap"
                              % (info.st_size, MAX_TRANSCRIPT_BYTES))
    if _expired(deadline):
        raise TranscriptError("deadline")
    last = {}
    seen_usage = False
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle):
            if number % _CHECK_EVERY == 0 and _expired(deadline):
                raise TranscriptError("deadline")
            found = _assistant_usage(_parse(raw))
            if found is None:
                continue
            seen_usage = True
            msg_id, model, use = found
            if (not isinstance(model, str) or model.startswith("<")
                    or not isinstance(msg_id, str)):
                continue
            last[msg_id] = (model, _counts_of(use))
    if not seen_usage:
        raise TranscriptError("schema_unknown", "no assistant line carries a usage dict")
    models = {}
    for model, got in last.values():
        bucket = _bucket(models, model)
        for name, value in got.items():
            bucket[name] += value
    return models


def _moment(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _aware(moment):
    return moment if moment.tzinfo else moment.astimezone()


def read_increment(path, offset, since, seen_ids, deadline=None,
                   max_bytes=MAX_TRANSCRIPT_BYTES):
    """Counts for the complete lines from `offset` on. Returns
    (models, new_offset, seen_ids), the offset being the first unread byte."""
    _check_file(path)
    floor = _aware(_moment(since))
    seen = list(seen_ids)
    known = set(seen)
    last = {}
    position = offset
    with open(path, "rb") as handle:
        handle.seek(offset)
        number = 0
        while True:
            if number % _CHECK_EVERY == 0 and _expired(deadline):
                break
            raw = handle.readline()
            if not raw.endswith(b"\n"):
                break
            if position > offset and position - offset + len(raw) > max_bytes:
                break
            position += len(raw)
            number += 1
            event = _parse(raw)
            found = _assistant_usage(event)
            if found is None:
                continue
            if event.get("isSidechain") is True:
                continue
            try:
                stamp = _aware(_moment(event.get("timestamp")))
            except (ValueError, TypeError):
                continue
            if stamp < floor:
                continue
            msg_id, model, use = found
            if (not isinstance(model, str) or model.startswith("<")
                    or not isinstance(msg_id, str) or msg_id in known):
                continue
            last[msg_id] = (model, _counts_of(use))
    models = {}
    for key, (model, got) in last.items():
        bucket = _bucket(models, model)
        for name, value in got.items():
            bucket[name] += value
        seen.append(key)
    return models, position, seen[-50:]


def _usage_file(task_id):
    return paths.run_dir(task_id) / "usage.json"


def _orchestrator_entries(doc):
    entries = doc.get("orchestrator") if isinstance(doc, dict) else None
    return entries if isinstance(entries, dict) else {}


def note_orchestrator(task_id, transcript_path, session_id):
    """Adds the orchestrator's main transcript to runs/<task>/usage.json the
    first time it is seen. A path that is not a regular file is not noted."""
    if not transcript_path or not isinstance(transcript_path, str) \
            or not session_id or not isinstance(session_id, str):
        return
    try:
        _check_file(transcript_path)
    except TranscriptError:
        return
    target = _usage_file(task_id)
    doc = store.read_json(target, default={})
    if not isinstance(doc, dict):
        doc = {}
    entries = _orchestrator_entries(doc)
    if transcript_path in entries:
        return
    entries[transcript_path] = {"session": session_id, "offset": 0, "seen": [], "models": {}}
    doc["orchestrator"] = entries
    store.write_json(target, doc)


def _run_began(task_id):
    for event in store.read_log(task_id):
        if isinstance(event, dict) and event.get("event") == "init" \
                and isinstance(event.get("at"), str):
            return event["at"]
    return None


def refresh_orchestrator(task_id, deadline=None):
    """Reads the increment of every noted orchestrator transcript, counting the
    lines written since the run's init, saves the offsets and logs the
    cumulative usage of each transcript whose totals changed. A transcript that
    can no longer be read is left as it was and named on stderr, never logged:
    every later refresh would log it again."""
    target = _usage_file(task_id)
    doc = store.read_json(target, default={})
    entries = _orchestrator_entries(doc)
    since = _run_began(task_id)
    if not entries or since is None:
        return
    dirty = False
    for path, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        offset = entry.get("offset")
        offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
        seen = entry.get("seen") if isinstance(entry.get("seen"), list) else []
        before = entry.get("models") if isinstance(entry.get("models"), dict) else {}
        try:
            models, new_offset, new_seen = read_increment(path, offset, since, seen,
                                                          deadline=deadline)
        except TranscriptError as err:
            sys.stderr.write("ppl usage: orchestrator transcript %s skipped: %s\n"
                             % (path, err.code))
            continue
        after = add(before, models)
        if new_offset == offset and after == before:
            continue
        entry.update(offset=new_offset, seen=new_seen, models=after)
        dirty = True
        if after != before:
            store.append_log(task_id, {"event": "usage", "role": "orchestrator",
                                       "agent_id": "orchestrator:%s" % entry.get("session"),
                                       "models": after})
    if dirty:
        store.write_json(target, doc)


def add(a, b):
    out = {model: dict(counts) for model, counts in a.items()}
    for model, counts in b.items():
        bucket = out.setdefault(model, {})
        for name, value in counts.items():
            bucket[name] = bucket.get(name, 0) + value
    return out


def total(counts):
    return sum(counts.get(name, 0) for name in COUNTS)


def run_usage(directory):
    """{role: {model: counts}} from the newest usage event per agent id."""
    newest = {}
    for event in store.read_log_at(directory):
        if not isinstance(event, dict) or event.get("event") != "usage":
            continue
        if "error" in event or not isinstance(event.get("models"), dict):
            continue
        key = event.get("agent_id")
        if not isinstance(key, str):
            continue
        newest[key] = event
    roles = {}
    for event in newest.values():
        role = event.get("role") if isinstance(event.get("role"), str) else "unknown"
        roles[role] = add(roles.get(role, {}), event["models"])
    return roles


def prices():
    """{model: {four COUNTS: float}}, USD per million tokens."""
    table = config.section("prices")
    out = {}
    for model, entry in table.items():
        if not isinstance(entry, dict):
            raise PplError("SCHEMA_INVALID",
                           "config.toml: prices.%s must be a table" % model)
        out[model] = {}
        for key in COUNTS:
            value = entry.get(key)
            if key not in entry:
                raise PplError("SCHEMA_INVALID",
                               "config.toml: prices.%s is missing the key %s" % (model, key))
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise PplError("SCHEMA_INVALID",
                               "config.toml: prices.%s.%s must be a number >= 0" % (model, key))
            out[model][key] = float(value)
    return out


def dollars(models, price_table):
    """(usd, unpriced). usd is None in two cases: no models at all (no data,
    unpriced is empty) and any model without a price (unpriced names them)."""
    if not models:
        return None, []
    unpriced = sorted(model for model in models if model not in price_table)
    if unpriced:
        return None, unpriced
    usd = 0.0
    for model, counts in models.items():
        for key in COUNTS:
            value = counts.get(key, 0)
            usd += (value if is_count(value) else 0) * price_table[model][key] / 1000000
    return usd, []


def run_dollars(directory, price_table=None):
    """(usd, unpriced) over every role. (None, []) when no usage was recorded.
    Priced with `price_table`, or with the live config.toml prices when None."""
    models = {}
    for role_models in run_usage(directory).values():
        models = add(models, role_models)
    return dollars(models, prices() if price_table is None else price_table)


def valid_usd_used(value):
    """True for a usd_used the cap can trust: a finite number >= 0."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value >= 0)


def spent_usd(task_id):
    """(usd, unpriced): the manual usd_used plus the run's usage priced with the
    table ppl init froze into the run, never the live config.toml, which a
    session can edit mid-run. usd is None only when a recorded model is missing
    from that table (unpriced names them). A run with no usage recorded has
    spent its usd_used alone, and a usd_used that is not a finite number >= 0 is
    infinite, so any cap is spent."""
    budgets = run.load(task_id)["budgets"]
    used = budgets.get("usd_used")
    if not valid_usd_used(used):
        return math.inf, []
    table = budgets.get("prices")
    priced, unpriced = run_dollars(paths.run_dir(task_id),
                                   price_table=table if isinstance(table, dict) else {})
    if unpriced:
        return None, unpriced
    return used + (priced or 0.0), []

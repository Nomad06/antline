"""Stall detection: a run in progress whose session stopped working, found from its event log.

In one measured run the orchestrator's session sat idle for hours in three gaps and nothing
noticed until the owner pressed resume. The hooks log why a session stopped (`session`
events) and when a role starts and stops (`dispatch_start`, `usage`), and this module reads
them. `check` decides one run from its state and log and reads no clock of its own, `scan`
runs it over every run, and `announce` logs a stall once and raises a local notification.

It only reports [R-STALL-REPORTS-ONLY]: nothing here parks a run, bumps its lease or writes
its state.json, and it never calls `watchdog.check`, which does park. The only write is the
`stall` event appended to the run's log, which marks the stall as announced.
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

from . import config, control, fsm, paths, store
from .errors import PplError

# Stages where the run waits on the owner or a human merge by design, so a quiet session there
# is expected, not a stall.
WAITING_STAGES = ("parking", "escalation", "closure", "merge_watch")
KINDS = ("stopped", "waiting", "silent")
NOTIFY_TIMEOUT_SECONDS = 10


def _when(item):
    """The timezone-aware time of a log item, None when it cannot be used. The same three
    skips as timing._parse: an item that is not a dict, an `at` that does not parse, and an
    `at` with no timezone, which cannot be subtracted from an aware one."""
    if not isinstance(item, dict) or not isinstance(item.get("at"), str):
        return None
    try:
        when = datetime.fromisoformat(item["at"])
    except ValueError:
        return None
    return when if when.tzinfo is not None else None


def _session(item, kind):
    return item.get("event") == "session" and item.get("kind") == kind


def _pending(item):
    """Whether a stop left background tasks running. Only an integer above 0 counts: a count
    the hook could not read is null, and anything else in a sandbox-writable log reads the
    same, as nothing pending."""
    value = item.get("background")
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _closes_everything(item):
    """A stop with nothing pending or a session end: no role of that session is still
    working. Without this, a window closed in the middle of a dispatch would leave it open
    forever and hide every later stall of the run."""
    return (_session(item, "stop") and not _pending(item)) or _session(item, "end")


def _counts(item):
    """Whether an event can be the newest one. A stall note is ppl's own, and a notification
    other than a permission prompt says nothing the stop before it did not."""
    if item.get("event") == "stall":
        return False
    if _session(item, "notification"):
        return item.get("type") == "permission_prompt"
    return True


def check(state, events, now, settings, halted=False):
    """The stall of one run, or None. `now` is a timezone-aware datetime, `settings` is
    config.stall_settings(). Returns {"task_id", "stage", "kind", "since", "minutes"}, kind one
    of KINDS, since the newest event's `at` and minutes its whole age."""
    if not isinstance(state, dict) or state.get("status") != "in_progress" or halted:
        return None
    stage = state.get("stage")
    if stage in WAITING_STAGES:
        return None
    in_flight = set()
    newest = newest_at = None
    for item in events:
        when = _when(item)
        if when is None:
            continue
        name = item.get("event")
        agent = item.get("agent_id")
        if name == "dispatch_start" and isinstance(agent, str) and agent:
            in_flight.add(agent)
        elif name == "usage" and isinstance(agent, str):
            in_flight.discard(agent)
        elif _closes_everything(item):
            in_flight.clear()
        if _counts(item):
            newest, newest_at = item, when
    if newest is None or in_flight:
        return None
    minutes = int((now - newest_at).total_seconds() // 60)
    if stage == "review":
        kind = "silent" if minutes >= settings["review_notify_after_minutes"] else None
    elif _closes_everything(newest):
        kind = "stopped" if minutes >= settings["notify_after_minutes"] else None
    elif _session(newest, "notification"):
        kind = "waiting" if minutes >= settings["notify_after_minutes"] else None
    else:
        kind = "silent" if minutes >= settings["silent_after_minutes"] else None
    if kind is None:
        return None
    return {"task_id": state.get("task_id"), "stage": stage, "kind": kind,
            "since": newest["at"], "minutes": minutes}


def _read_events(directory):
    """The run's log, read without blocking and only from a regular file: runs/ is writable by
    sandboxed roles, and a FIFO planted as one run's log must not stop the scan of every
    other run. An unreadable line is skipped, as store.read_log_at does."""
    try:
        text = store.read_regular_text(directory / "log.jsonl")
    except FileNotFoundError:
        return []
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except (ValueError, RecursionError):
            continue
    return events


def _halted(task_id):
    """A halting control command, or a control file that cannot be used: control.enforce halts
    the run on either, and the owner already sees both in the panel."""
    record = control.read(task_id, limit=store.HOOK_READ_LIMIT)
    return record is not None and (bool(record.get("invalid"))
                                   or record.get("command") in control.HALTING)


def scan(now=None):
    """check() for every run under runs/, not _archive. A run whose state, control file or log
    cannot be read is skipped, so one broken or planted run does not stop the others."""
    now = now or datetime.now(timezone.utc)
    root = paths.runs_dir()
    if not root.is_dir():
        return []
    settings = config.stall_settings()
    found = []
    for folder in sorted(root.iterdir()):
        task_id = folder.name
        if not paths.TASK_ID_RE.fullmatch(task_id) or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            state = store.read_json(folder / "state.json", limit=store.HOOK_READ_LIMIT)
            if not isinstance(state, dict) or state.get("task_id") != task_id:
                continue
            result = check(state, _read_events(folder), now, settings,
                           halted=_halted(task_id))
        except (OSError, ValueError, KeyError, TypeError, PplError):
            continue
        if result is not None:
            found.append(result)
    return found


def _announced_since(task_id, since):
    """Whether the run's log holds a stall event newer than `since`."""
    start = _when({"at": since})
    for item in _read_events(paths.run_dir(task_id)):
        if isinstance(item, dict) and item.get("event") == "stall":
            when = _when(item)
            if when is not None and start is not None and when > start:
                return True
    return False


def announce(found, notifier=None):
    """Log each stall of `found` (scan's list) once and pass it to `notifier` (default
    notify). A stall already followed by a stall event in its run's log is skipped. The event
    is written before the notifier runs and stays whatever the notifier does, so a notifier
    that fails is not retried on every scan. Returns the stalls it announced."""
    notifier = notifier or notify
    announced = []
    for stall in found:
        task_id = stall["task_id"]
        try:
            if _announced_since(task_id, stall["since"]):
                continue
            store.checked_run_path(paths.run_dir(task_id) / "log.jsonl")
            store.append_log(task_id, {"event": "stall", "kind": stall["kind"],
                                       "stage": stall["stage"], "minutes": stall["minutes"]})
        except (OSError, ValueError, KeyError, TypeError, PplError):
            continue
        announced.append(stall)
        try:
            notifier(stall)
        except Exception:
            # a notification is best effort, and the event above already records the stall
            pass
    return announced


def message(stall):
    """(title, body) of a stall notification: the task id, the minutes, the stage and the
    kind, and no other text."""
    return ("antline", "%s idle %d min at stage %s (%s)"
            % (stall["task_id"], stall["minutes"], stall["stage"], stall["kind"]))


def _sendable(stall):
    """Whether each value that goes into a command line is one ppl itself could have made
    [R-STALL-NOTIFY-NO-TEXT]: a task id, a stage, a kind and an integer."""
    task_id, minutes = stall.get("task_id"), stall.get("minutes")
    return (isinstance(task_id, str) and paths.TASK_ID_RE.fullmatch(task_id) is not None
            and stall.get("stage") in fsm.STAGES and stall.get("kind") in KINDS
            and isinstance(minutes, int) and not isinstance(minutes, bool))


def _run(runner, argv):
    try:
        result = runner(argv, timeout=NOTIFY_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        return False
    return getattr(result, "returncode", 1) == 0


def notify(stall, runner=subprocess.run, platform=sys.platform, which=shutil.which):
    """Raise the stall on this machine and, when [notify] command is set, run it. Returns the
    channels that ran: "desktop", "command". A channel that raises, times out or exits non-zero
    is left out. Nothing is sent when a value is not a task id, a stage, a kind or an integer,
    since those values go into a command line."""
    if not _sendable(stall):
        return []
    title, body = message(stall)
    sent = []
    if platform == "darwin" and which("osascript"):
        literal = body.replace("\\", "\\\\").replace('"', '\\"')
        desktop = ["osascript", "-e",
                   'display notification "%s" with title "%s"' % (literal, title)]
    elif which("notify-send"):
        desktop = ["notify-send", title, body]
    else:
        desktop = None
    if desktop is not None and _run(runner, desktop):
        sent.append("desktop")
    try:
        command = config.notify_command()
    except PplError:
        command = None
    if command is not None and _run(runner, command + [stall["task_id"], stall["stage"],
                                                       stall["kind"], str(stall["minutes"])]):
        sent.append("command")
    return sent

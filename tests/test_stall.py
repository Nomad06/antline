import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import control, paths, run, stall, store

NOW = datetime(2026, 11, 12, 12, 0, 0, tzinfo=timezone.utc)
SETTINGS = {"notify_after_minutes": 15, "silent_after_minutes": 60,
            "review_notify_after_minutes": 45, "keep_awake": False}


def at(minutes_ago, now=NOW):
    return (now - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def ev(minutes_ago, name, now=NOW, **fields):
    record = {"event": name, "at": at(minutes_ago, now)}
    record.update(fields)
    return record


def state(stage, status="in_progress", task_id="DEMO-700"):
    return {"task_id": task_id, "stage": stage, "status": status}


class TestStallCheck(unittest.TestCase):
    settings = SETTINGS

    def check(self, events, stage="gates", **kwargs):
        return stall.check(state(stage), events, NOW, self.settings, **kwargs)

    def test_a_stop_with_nothing_pending_is_a_stall_after_the_threshold(self):
        events = [ev(20, "stage_enter", to="gates"),
                  ev(16, "session", kind="stop", background=0, crons=0)]
        found = stall.check(state("gates"), events, NOW, self.settings)
        self.assertEqual((found["kind"], found["minutes"], found["stage"]),
                         ("stopped", 16, "gates"))
        self.assertEqual(found["task_id"], "DEMO-700")
        self.assertEqual(found["since"], at(16))

    def test_a_stop_younger_than_the_threshold_is_not(self):
        events = [ev(20, "stage_enter", to="gates"),
                  ev(14, "session", kind="stop", background=0, crons=0)]
        self.assertIsNone(self.check(events))

    def test_a_stop_with_a_background_task_waits_for_the_silence_rule(self):
        events = [ev(90, "stage_enter", to="gates"),
                  ev(30, "session", kind="stop", background=1, crons=0)]
        self.assertIsNone(self.check(events))
        events[-1] = ev(61, "session", kind="stop", background=1, crons=0)
        self.assertEqual(self.check(events)["kind"], "silent")

    def test_a_stop_with_unknown_counts_reads_as_nothing_pending(self):
        events = [ev(16, "session", kind="stop", background=None, crons=None)]
        self.assertEqual(self.check(events)["kind"], "stopped")

    def test_a_background_count_that_is_not_an_integer_reads_as_nothing_pending(self):
        for value in ("3", True, 1.5):
            with self.subTest(value=value):
                events = [ev(16, "session", kind="stop", background=value, crons=0)]
                self.assertEqual(self.check(events)["kind"], "stopped")

    def test_a_permission_notification_is_waiting(self):
        events = [ev(30, "session", kind="stop", background=1, crons=0),
                  ev(16, "session", kind="notification", type="permission_prompt")]
        found = self.check(events)
        self.assertEqual((found["kind"], found["minutes"]), ("waiting", 16))
        self.assertEqual(found["since"], at(16))

    def test_an_idle_notification_does_not_hide_the_stop_before_it(self):
        events = [ev(30, "session", kind="stop", background=1, crons=0),
                  ev(29, "session", kind="notification", type="idle_prompt")]
        self.assertIsNone(self.check(events))
        events = [ev(16, "session", kind="stop", background=0, crons=0),
                  ev(15, "session", kind="notification", type="idle_prompt")]
        found = self.check(events)
        self.assertEqual(found["kind"], "stopped")
        self.assertEqual(found["since"], at(16))

    def test_a_stop_with_nothing_pending_closes_an_unpaired_dispatch(self):
        events = [ev(100, "dispatch_start", role="ppl-implementer", agent_id="a1"),
                  ev(16, "session", kind="stop", background=0, crons=0)]
        self.assertEqual(self.check(events)["kind"], "stopped")

    def test_a_session_end_closes_an_unpaired_dispatch(self):
        events = [ev(100, "dispatch_start", role="ppl-implementer", agent_id="a1"),
                  ev(16, "session", kind="end", reason="other")]
        self.assertEqual(self.check(events)["kind"], "stopped")

    def test_a_stop_with_a_background_task_keeps_a_dispatch_in_flight(self):
        events = [ev(300, "dispatch_start", role="ppl-implementer", agent_id="a1"),
                  ev(200, "session", kind="stop", background=1, crons=0)]
        self.assertIsNone(self.check(events))

    def test_a_start_without_an_agent_id_is_never_in_flight(self):
        for agent in (None, ""):
            with self.subTest(agent_id=agent):
                events = [ev(100, "dispatch_start", role="ppl-implementer", agent_id=agent)]
                self.assertEqual(self.check(events)["kind"], "silent")

    def test_a_session_end_is_stopped(self):
        events = [ev(16, "session", kind="end", reason="prompt_input_exit")]
        found = self.check(events)
        self.assertEqual((found["kind"], found["minutes"]), ("stopped", 16))
        self.assertIsNone(self.check([ev(14, "session", kind="end", reason=None)]))

    def test_silence_alone_needs_the_long_threshold(self):
        self.assertIsNone(self.check([ev(59, "evidence", check="unit")]))
        found = self.check([ev(60, "evidence", check="unit")])
        self.assertEqual((found["kind"], found["minutes"]), ("silent", 60))

    def test_a_dispatch_in_flight_is_never_a_stall(self):
        events = [ev(300, "dispatch_start", role="ppl-implementer", agent_id="a1")]
        self.assertIsNone(self.check(events))

    def test_a_finished_dispatch_does_not_hide_a_stall(self):
        events = [ev(100, "dispatch_start", role="ppl-implementer", agent_id="a1"),
                  ev(50, "usage", role="ppl-implementer", agent_id="a1"),
                  ev(16, "session", kind="stop", background=0, crons=0)]
        self.assertEqual(self.check(events)["kind"], "stopped")

    def test_a_usage_for_another_agent_leaves_a_dispatch_in_flight(self):
        events = [ev(300, "dispatch_start", role="ppl-implementer", agent_id="a1"),
                  ev(200, "dispatch_start", role="ppl-tester", agent_id="a2"),
                  ev(100, "usage", role="ppl-tester", agent_id="a2")]
        self.assertIsNone(self.check(events))

    def test_review_is_watched_by_silence_only(self):
        events = [ev(20, "session", kind="stop", background=0, crons=0)]
        self.assertIsNone(self.check(events, stage="review"))
        events = [ev(45, "session", kind="stop", background=0, crons=0)]
        found = self.check(events, stage="review")
        self.assertEqual((found["kind"], found["minutes"], found["stage"]),
                         ("silent", 45, "review"))

    def test_waiting_stages_a_halt_and_a_finished_run_are_not_stalls(self):
        events = [ev(500, "session", kind="stop", background=0, crons=0)]
        for stage in stall.WAITING_STAGES:
            with self.subTest(stage=stage):
                self.assertIsNone(self.check(events, stage=stage))
        self.assertIsNone(self.check(events, halted=True))
        for status in ("planned", "ready", "done", "blocked", "failed"):
            with self.subTest(status=status):
                self.assertIsNone(stall.check(state("gates", status=status), events, NOW,
                                              self.settings))

    def test_a_log_with_no_readable_time_is_not_a_stall(self):
        events = [{"event": "init"}, {"event": "init", "at": "yesterday"},
                  {"event": "init", "at": "2026-11-12T10:00:00"}, "not a dict", None]
        self.assertIsNone(self.check(events))
        self.assertIsNone(self.check([]))

    def test_an_unreadable_item_is_skipped_and_the_rest_is_read(self):
        events = [ev(16, "session", kind="stop", background=0, crons=0),
                  {"event": "evidence", "at": "2026-11-12T11:59:00"}, "x"]
        self.assertEqual(self.check(events)["kind"], "stopped")

    def test_a_log_from_before_session_events_uses_the_silence_rule(self):
        events = [ev(200, "init"), ev(150, "stage_enter", to="gates"),
                  ev(30, "evidence", check="unit")]
        self.assertIsNone(self.check(events))
        events[-1] = ev(61, "evidence", check="unit")
        found = self.check(events)
        self.assertEqual((found["kind"], found["minutes"]), ("silent", 61))

    def test_an_earlier_stall_event_is_not_the_newest_event(self):
        events = [ev(30, "session", kind="stop", background=0, crons=0),
                  ev(10, "stall", kind="stopped", stage="gates", minutes=20)]
        found = self.check(events)
        self.assertEqual((found["kind"], found["minutes"]), ("stopped", 30))
        self.assertEqual(found["since"], at(30))


class FakeNotifier:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def __call__(self, found):
        self.calls.append(found)
        if self.error is not None:
            raise self.error
        return ["desktop"]


def write_events(task_id, events):
    with open(paths.run_dir(task_id) / "log.jsonl", "a", encoding="utf-8") as handle:
        for record in events:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def make_run(task_id, stage="gates", events=()):
    """A run in progress in `stage` whose log holds `events` alone."""
    run.init(task_id, "https://j/%s" % task_id, "/tmp/repo")
    record = run.load(task_id)
    record["stage"] = stage
    record["status"] = "in_progress"
    run.save(record)
    (paths.run_dir(task_id) / "log.jsonl").write_text("", encoding="utf-8")
    write_events(task_id, events)


class FreshHome(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home


def logged(task_id, name):
    return [item for item in store.read_log(task_id) if item.get("event") == name]


class TestAnnounce(FreshHome):
    def stalled_run(self, task_id):
        now = datetime.now(timezone.utc)
        make_run(task_id, events=[ev(16, "session", now=now, kind="stop", background=0,
                                     crons=0)])
        return now

    def test_a_stall_is_logged_and_notified_once(self):
        now = self.stalled_run("DEMO-710")
        notifier = FakeNotifier()
        announced = stall.announce(stall.scan(now), notifier)
        self.assertEqual(len(announced), 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0]["task_id"], "DEMO-710")
        events = logged("DEMO-710", "stall")
        self.assertEqual(len(events), 1)
        self.assertEqual({key: events[0][key] for key in ("kind", "stage", "minutes")},
                         {"kind": "stopped", "stage": "gates", "minutes": 16})
        again = FakeNotifier()
        self.assertEqual(stall.announce(stall.scan(now), again), [])
        self.assertEqual(again.calls, [])
        self.assertEqual(len(logged("DEMO-710", "stall")), 1)

    def test_a_notifier_that_raises_leaves_the_event_and_stays_silent_after(self):
        now = self.stalled_run("DEMO-711")
        failing = FakeNotifier(error=RuntimeError("no display"))
        announced = stall.announce(stall.scan(now), failing)
        self.assertEqual(len(failing.calls), 1)
        self.assertEqual(len(announced), 1)
        self.assertEqual(len(logged("DEMO-711", "stall")), 1)
        again = FakeNotifier()
        self.assertEqual(stall.announce(stall.scan(now), again), [])
        self.assertEqual(again.calls, [])

    def test_a_newer_event_and_a_new_stall_announce_again(self):
        now = self.stalled_run("DEMO-712")
        stall.announce(stall.scan(now), FakeNotifier())
        later = now + timedelta(minutes=40)
        write_events("DEMO-712", [ev(20, "session", now=later, kind="stop", background=0,
                                     crons=0)])
        notifier = FakeNotifier()
        announced = stall.announce(stall.scan(later), notifier)
        self.assertEqual(len(announced), 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0]["minutes"], 20)
        self.assertEqual(len(logged("DEMO-712", "stall")), 2)

    def test_a_log_planted_as_a_link_is_not_appended_and_the_other_run_still_is(self):
        self.stalled_run("DEMO-713")
        now = self.stalled_run("DEMO-714")
        found = stall.scan(now)
        self.assertEqual(len(found), 2)
        log = paths.run_dir("DEMO-713") / "log.jsonl"
        outside = Path(tmphome.mkdtemp()) / "elsewhere.jsonl"
        outside.write_text("", encoding="utf-8")
        log.unlink()
        log.symlink_to(outside)
        notifier = FakeNotifier()
        announced = stall.announce(found, notifier)
        self.assertEqual([item["task_id"] for item in announced], ["DEMO-714"])
        self.assertEqual(outside.read_text(encoding="utf-8"), "")


class FakeRunner:
    def __init__(self, returncode=0, error=None):
        self.calls = []
        self.returncode = returncode
        self.error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode)


def finder(*present):
    return lambda name: "/usr/bin/%s" % name if name in present else None


STALL = {"task_id": "DEMO-720", "stage": "gates", "kind": "stopped",
         "since": "2026-11-12T11:44:00+00:00", "minutes": 16}


class TestNotify(FreshHome):
    def test_the_message_holds_the_four_values(self):
        self.assertEqual(stall.message(STALL),
                         ("antline", "DEMO-720 idle 16 min at stage gates (stopped)"))

    def test_the_desktop_notification_on_darwin(self):
        runner = FakeRunner()
        sent = stall.notify(STALL, runner=runner, platform="darwin",
                            which=finder("osascript", "notify-send"))
        self.assertEqual(sent, ["desktop"])
        self.assertEqual(runner.calls[0][0], [
            "osascript", "-e",
            'display notification "DEMO-720 idle 16 min at stage gates (stopped)" '
            'with title "antline"'])
        self.assertEqual(runner.calls[0][1]["timeout"], 10)
        self.assertEqual(len(runner.calls), 1)

    def test_notify_send_when_only_that_exists(self):
        runner = FakeRunner()
        sent = stall.notify(STALL, runner=runner, platform="linux", which=finder("notify-send"))
        self.assertEqual(sent, ["desktop"])
        self.assertEqual(runner.calls[0][0], [
            "notify-send", "antline", "DEMO-720 idle 16 min at stage gates (stopped)"])

    def test_nothing_runs_without_a_desktop_tool_or_a_command(self):
        runner = FakeRunner()
        self.assertEqual(stall.notify(STALL, runner=runner, platform="darwin",
                                      which=finder()), [])
        self.assertEqual(runner.calls, [])

    def test_the_configured_command_gets_the_four_values(self):
        paths.home().mkdir(parents=True, exist_ok=True)
        paths.config_file().write_text('[notify]\ncommand = ["/opt/hook", "--quiet"]\n',
                                       encoding="utf-8")
        runner = FakeRunner()
        sent = stall.notify(STALL, runner=runner, platform="linux", which=finder())
        self.assertEqual(sent, ["command"])
        self.assertEqual(runner.calls[0][0],
                         ["/opt/hook", "--quiet", "DEMO-720", "gates", "stopped", "16"])
        self.assertEqual(runner.calls[0][1]["timeout"], 10)

    def test_a_runner_that_times_out_gives_no_channel(self):
        runner = FakeRunner(error=subprocess.TimeoutExpired(["osascript"], 10))
        self.assertEqual(stall.notify(STALL, runner=runner, platform="darwin",
                                      which=finder("osascript")), [])

    def test_a_non_zero_exit_gives_no_channel(self):
        runner = FakeRunner(returncode=1)
        self.assertEqual(stall.notify(STALL, runner=runner, platform="darwin",
                                      which=finder("osascript")), [])

    def test_a_value_that_is_not_a_task_id_a_stage_or_a_kind_sends_nothing(self):
        # rule: R-STALL-NOTIFY-NO-TEXT
        paths.home().mkdir(parents=True, exist_ok=True)
        paths.config_file().write_text('[notify]\ncommand = ["/opt/hook"]\n', encoding="utf-8")
        bad = [dict(STALL, task_id='x"; rm -rf ~'), dict(STALL, task_id="DEMO-720\n"),
               dict(STALL, stage='gates" & do shell script "id'), dict(STALL, kind="slept"),
               dict(STALL, minutes="16"), dict(STALL, minutes=True)]
        for item in bad:
            with self.subTest(stall=item):
                runner = FakeRunner()
                self.assertEqual(stall.notify(item, runner=runner, platform="darwin",
                                              which=finder("osascript", "notify-send")), [])
                self.assertFalse(runner.calls)

    def test_the_body_holds_no_text_beyond_the_four_values(self):
        noisy = dict(STALL, note="the last assistant message", since="secret")
        runner = FakeRunner()
        stall.notify(noisy, runner=runner, platform="linux", which=finder("notify-send"))
        body = runner.calls[0][0][2]
        self.assertEqual(body, "DEMO-720 idle 16 min at stage gates (stopped)")
        self.assertNotIn("assistant", body)
        self.assertNotIn("secret", body)


class TestScan(FreshHome):
    def test_a_stalled_run_and_a_halted_run_give_one_row(self):
        now = datetime.now(timezone.utc)
        old = [ev(16, "session", now=now, kind="stop", background=0, crons=0)]
        make_run("DEMO-730", events=old)
        make_run("DEMO-731", events=old)
        control.set_command("DEMO-731", "pause", by="test")
        found = stall.scan(now + timedelta(minutes=1))
        self.assertEqual([item["task_id"] for item in found], ["DEMO-730"])

    def test_an_invalid_control_file_counts_as_halted(self):
        now = datetime.now(timezone.utc)
        make_run("DEMO-732", events=[ev(16, "session", now=now, kind="stop", background=0,
                                        crons=0)])
        (paths.run_dir("DEMO-732") / "control.json").write_text("{", encoding="utf-8")
        self.assertEqual(stall.scan(now), [])

    def test_an_unreadable_state_is_skipped(self):
        now = datetime.now(timezone.utc)
        old = [ev(16, "session", now=now, kind="stop", background=0, crons=0)]
        make_run("DEMO-733", events=old)
        make_run("DEMO-734", events=old)
        make_run("DEMO-735", events=old)
        (paths.run_dir("DEMO-733") / "state.json").write_text("{", encoding="utf-8")
        target = paths.run_dir("DEMO-734") / "state.json"
        target.unlink()
        os.mkfifo(str(target))
        self.assertEqual([item["task_id"] for item in stall.scan(now)], ["DEMO-735"])

    def test_a_fifo_log_is_skipped_without_blocking(self):
        now = datetime.now(timezone.utc)
        old = [ev(16, "session", now=now, kind="stop", background=0, crons=0)]
        make_run("DEMO-736", events=old)
        make_run("DEMO-737", events=old)
        log = paths.run_dir("DEMO-736") / "log.jsonl"
        log.unlink()
        os.mkfifo(str(log))
        self.assertEqual([item["task_id"] for item in stall.scan(now)], ["DEMO-737"])

    def test_a_state_naming_another_task_and_the_archive_are_skipped(self):
        now = datetime.now(timezone.utc)
        old = [ev(16, "session", now=now, kind="stop", background=0, crons=0)]
        make_run("DEMO-738", events=old)
        record = run.load("DEMO-738")
        record["task_id"] = "DEMO-739"
        store.write_json(paths.run_dir("DEMO-738") / "state.json", record)
        archived = paths.runs_dir() / "_archive" / "DEMO-740"
        archived.mkdir(parents=True)
        (archived / "state.json").write_text(json.dumps(state("gates", task_id="DEMO-740")),
                                             encoding="utf-8")
        (paths.runs_dir() / "not-a-task").mkdir()
        self.assertEqual(stall.scan(now), [])

    def test_no_runs_directory_is_an_empty_list(self):
        self.assertEqual(stall.scan(), [])


class TestNeverChangesTheRun(FreshHome):
    def test_scan_and_announce_leave_the_state_byte_identical(self):
        # rule: R-STALL-REPORTS-ONLY
        now = datetime.now(timezone.utc)
        make_run("DEMO-750", events=[ev(500, "session", now=now, kind="stop", background=0,
                                        crons=0)])
        path = run.state_path("DEMO-750")
        before = path.read_bytes()
        found = stall.scan(now)
        self.assertEqual(len(found), 1)
        stall.announce(found, FakeNotifier())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(run.load("DEMO-750")["stage"], "gates")


if __name__ == "__main__":
    unittest.main()

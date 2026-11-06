import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import config, paths, usage
from ppl.errors import PplError

USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 30848,
         "cache_read_input_tokens": 18421, "output_tokens": 474,
         "output_tokens_details": {"thinking_tokens": 90}}
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"


def line(msg_id, model=OPUS, usage_=USAGE, ts="2026-10-22T12:00:00.000Z",
         type_="assistant", sidechain=False):
    message = {"id": msg_id, "model": model}
    if usage_ is not None:
        message["usage"] = usage_
    return {"type": type_, "isSidechain": sidechain, "timestamp": ts, "message": message}


def counts(output, inp=0):
    return {"input_tokens": inp, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": output}


def write_transcript(path, lines):
    with open(path, "w", encoding="utf-8") as handle:
        for item in lines:
            handle.write((item if isinstance(item, str) else json.dumps(item)) + "\n")


class TestUsage(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        self.dir = Path(tmphome.mkdtemp(prefix="usage-"))
        self.path = self.dir / "t.jsonl"

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def test_lines_of_one_message_are_counted_once(self):
        write_transcript(self.path, [line("m1"), line("m1"), line("m1")])
        self.assertEqual(usage.sum_transcript(self.path), {OPUS: {
            "input": 2, "cache_write": 30848, "cache_read": 18421,
            "output": 474, "thinking": 90}})

    def test_two_models_are_summed_apart(self):
        write_transcript(self.path, [
            line("m1", OPUS, counts(10)), line("m2", OPUS, counts(20)),
            line("m3", HAIKU, counts(1)), line("m4", HAIKU, counts(2))])
        got = usage.sum_transcript(self.path)
        self.assertEqual(got[OPUS]["output"], 30)
        self.assertEqual(got[HAIKU]["output"], 3)

    def test_synthetic_and_non_assistant_lines_are_ignored(self):
        write_transcript(self.path, [
            line("m1", OPUS, counts(10)),
            line("m2", "<synthetic>", counts(99)),
            line("m3", OPUS, counts(77), type_="user"),
            "{not json"])
        self.assertEqual(list(usage.sum_transcript(self.path)), [OPUS])
        self.assertEqual(usage.sum_transcript(self.path)[OPUS]["output"], 10)

    def test_iterations_are_never_added(self):
        body = dict(counts(10), iterations=[counts(500), counts(600)])
        write_transcript(self.path, [line("m1", OPUS, body)])
        self.assertEqual(usage.sum_transcript(self.path)[OPUS]["output"], 10)

    def test_a_synthetic_only_transcript_is_schema_unknown(self):
        # an API error before any real response leaves only synthetic usage lines
        write_transcript(self.path, [line("m1", "<synthetic>", counts(0)),
                                     line(None, OPUS, counts(5))])
        with self.assertRaises(usage.TranscriptError) as ctx:
            usage.sum_transcript(self.path)
        self.assertEqual(ctx.exception.code, "schema_unknown")

    def test_a_transcript_without_usage_is_schema_unknown(self):
        write_transcript(self.path, [line("m1", usage_=None), {"type": "user"}])
        with self.assertRaises(usage.TranscriptError) as ctx:
            usage.sum_transcript(self.path)
        self.assertEqual(ctx.exception.code, "schema_unknown")

    def test_a_missing_transcript_is_transcript_missing(self):
        with self.assertRaises(usage.TranscriptError) as ctx:
            usage.sum_transcript(self.dir / "nope.jsonl")
        self.assertEqual(ctx.exception.code, "transcript_missing")

    def test_an_oversized_transcript_is_refused_before_reading(self):
        write_transcript(self.path, [line("m1")])
        with mock.patch.object(usage, "MAX_TRANSCRIPT_BYTES", 100):
            with mock.patch("builtins.open", side_effect=AssertionError("opened")):
                with self.assertRaises(usage.TranscriptError) as ctx:
                    usage.sum_transcript(self.path)
        self.assertEqual(ctx.exception.code, "transcript_too_large")

    def test_a_fifo_is_refused_without_opening_it(self):
        fifo = self.dir / "pipe"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(usage.TranscriptError) as ctx:
            usage.sum_transcript(fifo)
        self.assertEqual(ctx.exception.code, "transcript_not_regular")
        self.assertLess(time.monotonic() - started, 1)

    def test_a_passed_deadline_raises_deadline(self):
        write_transcript(self.path, [line("m1")])
        with self.assertRaises(usage.TranscriptError) as ctx:
            usage.sum_transcript(self.path, deadline=time.monotonic() - 1)
        self.assertEqual(ctx.exception.code, "deadline")

    def test_increment_reads_only_lines_since_the_run_began(self):
        write_transcript(self.path, [
            line("m1", OPUS, counts(10), ts="2026-10-22T10:00:00Z"),
            line("m2", OPUS, counts(20), ts="2026-10-22T12:00:00Z")])
        models, offset, seen = usage.read_increment(
            self.path, 0, "2026-10-22T11:00:00Z", [])
        self.assertEqual(models[OPUS]["output"], 20)
        self.assertEqual(offset, self.path.stat().st_size)
        self.assertEqual(seen, ["m2"])

    def test_increment_skips_sidechain_lines(self):
        write_transcript(self.path, [
            line("m1", OPUS, counts(10)),
            line("m2", OPUS, counts(20), sidechain=True)])
        models, _, _ = usage.read_increment(self.path, 0, "2026-10-22T00:00:00Z", [])
        self.assertEqual(models[OPUS]["output"], 10)

    def test_increment_resumes_at_its_offset_without_double_counting(self):
        write_transcript(self.path, [line("m1", OPUS, counts(10))])
        first, offset, seen = usage.read_increment(self.path, 0, "2026-10-22T00:00:00Z", [])
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line("m1", OPUS, counts(10))) + "\n")
            handle.write(json.dumps(line("m2", OPUS, counts(5))) + "\n")
        second, offset2, seen2 = usage.read_increment(
            self.path, offset, "2026-10-22T00:00:00Z", seen)
        self.assertEqual(first[OPUS]["output"], 10)
        self.assertEqual(second[OPUS]["output"], 5)
        self.assertEqual(offset2, self.path.stat().st_size)
        self.assertEqual(seen2, ["m1", "m2"])

    def test_a_partial_last_line_is_left_for_the_next_read(self):
        whole = json.dumps(line("m1", OPUS, counts(10))) + "\n"
        rest = json.dumps(line("m2", OPUS, counts(5)))
        self.path.write_text(whole + rest[:20], encoding="utf-8")
        models, offset, seen = usage.read_increment(self.path, 0, "2026-10-22T00:00:00Z", [])
        self.assertEqual(models[OPUS]["output"], 10)
        self.assertEqual(offset, len(whole.encode("utf-8")))
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(rest[20:] + "\n")
        models, offset, _ = usage.read_increment(
            self.path, offset, "2026-10-22T00:00:00Z", seen)
        self.assertEqual(models[OPUS]["output"], 5)
        self.assertEqual(offset, self.path.stat().st_size)

    def test_increment_stops_at_max_bytes_and_continues(self):
        write_transcript(self.path, [line("m%d" % i, OPUS, counts(1)) for i in range(5)])
        size = self.path.stat().st_size
        one = size // 5
        total, offset, seen = 0, 0, []
        for _ in range(10):
            models, offset, seen = usage.read_increment(
                self.path, offset, "2026-10-22T00:00:00Z", seen, max_bytes=one * 2)
            total += models.get(OPUS, {}).get("output", 0)
            if offset == size:
                break
        self.assertEqual(total, 5)
        self.assertEqual(offset, size)

    def test_increment_stops_at_a_passed_deadline_keeping_its_offset(self):
        write_transcript(self.path, [line("m1")])
        models, offset, _ = usage.read_increment(
            self.path, 0, "2026-10-22T00:00:00Z", [], deadline=time.monotonic() - 1)
        self.assertEqual((models, offset), ({}, 0))

    def log(self, events):
        with open(self.dir / "log.jsonl", "w", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")

    def test_the_newest_event_per_key_wins(self):
        self.log([
            {"event": "usage", "agent_id": "a1", "role": "ppl-code-worker",
             "models": {"m": {"output": 10}}},
            {"event": "usage", "agent_id": "a1", "role": "ppl-code-worker",
             "models": {"m": {"output": 25}}},
            {"event": "usage", "agent_id": "a2", "role": "ppl-verifier",
             "models": {"m": {"output": 5}}}])
        got = usage.run_usage(self.dir)
        self.assertEqual(got["ppl-code-worker"]["m"]["output"], 25)
        self.assertEqual(got["ppl-verifier"]["m"]["output"], 5)

    def test_error_events_and_odd_shapes_are_ignored(self):
        self.log([
            {"event": "usage", "agent_id": "a1", "role": "ppl-code-worker",
             "error": "deadline"},
            {"event": "usage", "agent_id": "a2", "role": "ppl-code-worker",
             "models": "x"},
            {"event": "usage", "agent_id": "a3", "role": "ppl-verifier",
             "models": {"m": {"output": 4}}}])
        self.assertEqual(usage.run_usage(self.dir),
                         {"ppl-verifier": {"m": {"output": 4}}})

    def write_config(self, text):
        paths.config_file().write_text(text, encoding="utf-8")

    def test_dollars_price_each_count_per_million(self):
        table = {"m": {"input": 3, "cache_write": 3.75, "cache_read": 0.3, "output": 15}}
        million = {name: 1000000 for name in usage.COUNTS}
        got, unpriced = usage.dollars({"m": million}, table)
        self.assertAlmostEqual(got, 22.05)
        self.assertEqual(unpriced, [])

    def test_an_unpriced_model_makes_dollars_undefined_and_is_named(self):
        table = {"m": {name: 1 for name in usage.COUNTS}}
        million = {name: 1 for name in usage.COUNTS}
        got, unpriced = usage.dollars({"m": million, "z": million, "a": million}, table)
        self.assertIsNone(got)
        self.assertEqual(unpriced, ["a", "z"])

    def test_prices_refuse_a_missing_key_naming_model_and_key(self):
        self.write_config('[prices."m1"]\ninput = 1\ncache_write = 1\noutput = 1\n')
        with self.assertRaises(PplError) as ctx:
            usage.prices()
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("m1", ctx.exception.message)
        self.assertIn("cache_read", ctx.exception.message)

    def test_prices_refuse_a_negative_or_non_number(self):
        for bad in ("-1", '"3"', "true"):
            self.write_config('[prices."m1"]\ninput = %s\ncache_write = 1\n'
                              'cache_read = 1\noutput = 1\n' % bad)
            with self.assertRaises(PplError) as ctx:
                usage.prices()
            self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
            self.assertIn("m1", ctx.exception.message)
            self.assertIn("input", ctx.exception.message)

    def test_no_prices_table_is_an_empty_table(self):
        self.assertEqual(usage.prices(), {})

    def test_prices_are_read_as_floats(self):
        self.write_config('[prices."m1"]\ninput = 3\ncache_write = 3.75\n'
                          'cache_read = 0.3\noutput = 15\n')
        self.assertEqual(usage.prices(), {"m1": {
            "input": 3.0, "cache_write": 3.75, "cache_read": 0.3, "output": 15.0}})

    def test_dollars_of_no_usage_is_undefined_not_zero(self):
        self.assertEqual(usage.dollars({}, {"m": {k: 1.0 for k in usage.COUNTS}}),
                         (None, []))

    def test_run_dollars_without_usage_is_undefined_not_zero(self):
        self.log([{"event": "usage", "agent_id": "a1", "role": "r1", "error": "deadline"}])
        self.assertEqual(usage.run_dollars(self.dir), (None, []))

    def test_a_usage_without_any_count_is_not_counted(self):
        write_transcript(self.path, [line("m1", OPUS, {"service_tier": "standard"})])
        with self.assertRaises(usage.TranscriptError) as ctx:
            usage.sum_transcript(self.path)
        self.assertEqual(ctx.exception.code, "schema_unknown")
        write_transcript(self.path, [line("m1", OPUS, {"output_tokens": 4})])
        self.assertEqual(usage.sum_transcript(self.path)[OPUS]["cache_read"], 0)

    def test_a_malformed_message_id_skips_the_line_not_the_transcript(self):
        write_transcript(self.path, [
            line({"x": 1}, OPUS, counts(50)), line(["y"], OPUS, counts(60)),
            line("m1", OPUS, counts(10))])
        self.assertEqual(usage.sum_transcript(self.path)[OPUS]["output"], 10)
        models, _, seen = usage.read_increment(self.path, 0, "2026-10-22T00:00:00Z", [])
        self.assertEqual(models[OPUS]["output"], 10)
        self.assertEqual(seen, ["m1"])

    def test_a_non_finite_count_does_not_crash_the_read(self):
        self.path.write_text(
            '{"type":"assistant","isSidechain":false,"timestamp":"2026-10-22T12:00:00Z",'
            '"message":{"id":"m1","model":"%s","usage":{"input_tokens":1e999,'
            '"output_tokens":7}}}\n' % OPUS, encoding="utf-8")
        self.assertEqual(usage.sum_transcript(self.path)[OPUS]["input"], 0)
        self.assertEqual(usage.sum_transcript(self.path)[OPUS]["output"], 7)
        models, _, _ = usage.read_increment(self.path, 0, "2026-10-22T00:00:00Z", [])
        self.assertEqual(models[OPUS]["output"], 7)

    def test_a_huge_integer_count_does_not_crash_the_read(self):
        # an int too large for a float is no count, it reads 0 like 1e999
        self.path.write_text(
            '{"type":"assistant","isSidechain":false,"timestamp":"2026-10-22T12:00:00Z",'
            '"message":{"id":"m1","model":"%s","usage":{"input_tokens":1%s,'
            '"output_tokens":7}}}\n' % (OPUS, "0" * 400), encoding="utf-8")
        got = usage.sum_transcript(self.path)
        self.assertEqual(got[OPUS]["input"], 0)
        self.assertEqual(got[OPUS]["output"], 7)
        models, _, _ = usage.read_increment(self.path, 0, "2026-10-22T00:00:00Z", [])
        self.assertEqual(models[OPUS]["input"], 0)
        self.assertEqual(models[OPUS]["output"], 7)

    def test_dollars_never_raise_on_a_logged_huge_count(self):
        # the log is append-only: one such event must not break every later read
        self.write_config('[prices."m"]\ninput = 1\ncache_write = 1\n'
                          'cache_read = 1\noutput = 10\n')
        self.log([
            {"event": "usage", "agent_id": "a1", "role": "r1",
             "models": {"m": {"input": 10 ** 400, "output": 1000000}}}])
        self.assertEqual(usage.run_dollars(self.dir), (10.0, []))
        self.assertEqual(usage.dollars({"m": {"output": 10 ** 400}}, usage.prices()), (0.0, []))

    def test_run_dollars_sum_every_role(self):
        self.write_config('[prices."m"]\ninput = 0\ncache_write = 0\n'
                          'cache_read = 0\noutput = 10\n')
        self.log([
            {"event": "usage", "agent_id": "a1", "role": "r1",
             "models": {"m": {"output": 1000000}}},
            {"event": "usage", "agent_id": "a2", "role": "r2",
             "models": {"m": {"output": 500000}}}])
        self.assertAlmostEqual(usage.run_dollars(self.dir)[0], 15.0)
        self.log([{"event": "usage", "agent_id": "a1", "role": "r1",
                   "models": {"x": {"output": 1}}}])
        self.assertEqual(usage.run_dollars(self.dir), (None, ["x"]))


class TestSpentToday(unittest.TestCase):
    """What every run spent since local midnight [R-BUDGET-USD-PER-DAY]. Model "m" costs 1 USD
    per million output tokens, "n" has no price."""

    def setUp(self):
        self.home = Path(tmphome.mkdtemp(prefix="pplday-"))
        self.enterContext(mock.patch.dict(os.environ, {"PPL_HOME": str(self.home)}))
        self.zone = os.environ.get("TZ")
        self.addCleanup(self.restore_zone)
        os.environ["TZ"] = "Europe/Amsterdam"
        time.tzset()
        # 2026-11-05 12:00 Amsterdam (UTC+1)
        self.now = time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))

    def restore_zone(self):
        if self.zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self.zone
        time.tzset()

    def make_run(self, name, prices=None, archived=False):
        root = self.home / "runs" / ("_archive/2026-11-01-%s" % name if archived else name)
        root.mkdir(parents=True)
        budgets = {"prices": {"m": {"input": 0, "cache_write": 0, "cache_read": 0,
                                    "output": 1.0}} if prices is None else prices}
        (root / "state.json").write_text(json.dumps({"task_id": name, "budgets": budgets}),
                                         encoding="utf-8")
        return root

    def log(self, root, at, output, agent="a1", model="m"):
        event = {"event": "usage", "role": "worker", "agent_id": agent, "at": at,
                 "models": {model: {"input": 0, "cache_write": 0, "cache_read": 0,
                                    "output": output, "thinking": 0}}}
        with open(root / "log.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")

    def test_only_the_spend_after_local_midnight_counts(self):
        # rule: R-BUDGET-USD-PER-DAY
        root = self.make_run("T-1")
        # 23:50 yesterday and 00:10 today, Amsterdam, are 22:50Z and 23:10Z
        self.log(root, "2026-11-04T22:50:00+00:00", 3000000)
        self.log(root, "2026-11-04T23:10:00+00:00", 5000000)
        usd, unpriced = usage.spent_today(self.now)
        self.assertEqual((round(usd, 6), unpriced), (2.0, []))

    def test_a_run_that_spent_only_before_midnight_counts_nothing(self):
        root = self.make_run("T-1")
        self.log(root, "2026-11-04T22:50:00+00:00", 3000000)
        self.assertEqual(usage.spent_today(self.now), (0.0, []))

    def test_an_agent_first_seen_today_counts_in_full(self):
        root = self.make_run("T-1")
        self.log(root, "2026-11-05T09:00:00+00:00", 1000000)
        self.assertEqual(round(usage.spent_today(self.now)[0], 6), 1.0)

    def test_archived_runs_count(self):
        # rule: R-BUDGET-USD-PER-DAY
        self.log(self.make_run("T-1"), "2026-11-05T09:00:00+00:00", 1000000)
        self.log(self.make_run("T-2", archived=True), "2026-11-05T10:00:00+00:00", 2000000)
        self.assertEqual(round(usage.spent_today(self.now)[0], 6), 3.0)

    def test_each_run_is_priced_with_its_frozen_prices(self):
        # rule: R-BUDGET-USD-PER-DAY
        self.log(self.make_run("T-1"), "2026-11-05T09:00:00+00:00", 1000000)
        dear = {"m": {"input": 0, "cache_write": 0, "cache_read": 0, "output": 10.0}}
        self.log(self.make_run("T-2", prices=dear), "2026-11-05T09:00:00+00:00", 1000000)
        config.update("prices", {"m": {"input": 0, "cache_write": 0, "cache_read": 0,
                                       "output": 100.0}})
        self.assertEqual(round(usage.spent_today(self.now)[0], 6), 11.0)

    def test_an_unpriced_model_is_listed(self):
        # rule: R-BUDGET-USD-PER-DAY
        root = self.make_run("T-1")
        self.log(root, "2026-11-05T09:00:00+00:00", 1000000, model="n")
        self.assertEqual(usage.spent_today(self.now), (None, ["n"]))

    def test_an_unpriced_model_that_did_not_spend_today_is_not_listed(self):
        root = self.make_run("T-1", prices={})
        self.log(root, "2026-11-04T09:00:00+00:00", 1000000, model="n")
        self.assertEqual(usage.spent_today(self.now), (0.0, []))

    def test_an_unreadable_run_is_skipped_and_listed(self):
        self.log(self.make_run("T-1"), "2026-11-05T09:00:00+00:00", 1000000)
        broken = self.make_run("T-2")
        (broken / "log.jsonl").mkdir()
        usd, issues = usage.spent_today(self.now)
        self.assertEqual((round(usd, 6), issues), (1.0, ["unreadable: T-2"]))

    def test_a_log_over_the_cap_is_unreadable(self):
        root = self.make_run("T-1")
        self.log(root, "2026-11-05T09:00:00+00:00", 1)
        with mock.patch.object(usage, "DAY_LOG_LIMIT", 10):
            self.assertEqual(usage.spent_today(self.now), (0.0, ["unreadable: T-1"]))

    def test_a_corrupt_archived_state_is_skipped_and_listed(self):
        self.log(self.make_run("T-1"), "2026-11-05T09:00:00+00:00", 1000000)
        old = self.make_run("T-2", archived=True)
        (old / "state.json").write_text("{not json", encoding="utf-8")
        usd, issues = usage.spent_today(self.now)
        self.assertEqual((round(usd, 6), len(issues)), (1.0, 1))
        self.assertTrue(issues[0].startswith("unreadable: "), issues)

    def test_a_counter_reset_mid_day_counts_in_full(self):
        root = self.make_run("T-1")
        self.log(root, "2026-11-04T22:50:00+00:00", 100000000)
        self.log(root, "2026-11-05T08:00:00+00:00", 130000000)
        self.log(root, "2026-11-05T09:00:00+00:00", 2000000)
        self.assertEqual(round(usage.spent_today(self.now)[0], 6), 32.0)

    def test_the_midnight_of_a_dst_change_day_is_the_calendar_midnight(self):
        # 2026-10-25 in Amsterdam is 25 hours: midnight is 2026-10-24T22:00Z, not 23:00Z
        root = self.make_run("T-1")
        self.log(root, "2026-10-24T21:30:00+00:00", 1000000)
        self.log(root, "2026-10-24T22:30:00+00:00", 4000000)
        self.log(root, "2026-10-25T23:30:00+00:00", 9000000)
        noon = time.mktime((2026, 10, 25, 12, 0, 0, 0, 0, -1))
        self.assertEqual(round(usage.spent_today(noon)[0], 6), 8.0)

    def test_negative_frozen_prices_do_not_lower_the_total_and_are_listed(self):
        self.log(self.make_run("T-1"), "2026-11-05T09:00:00+00:00", 1000000)
        bad = {"m": {"input": 0, "cache_write": 0, "cache_read": 0, "output": -50.0}}
        self.log(self.make_run("T-2", prices=bad), "2026-11-05T09:00:00+00:00", 1000000)
        usd, issues = usage.spent_today(self.now)
        self.assertEqual((round(usd, 6), issues), (1.0, ["unreadable: T-2"]))
        usd, issues = usage.spent_today(self.now, task="T-2")
        self.assertEqual((usd, issues), (None, ["blocked: T-2"]))

    def test_a_malformed_count_in_another_run_is_skipped_not_raised(self):
        self.log(self.make_run("T-1"), "2026-11-05T09:00:00+00:00", 1000000)
        root = self.make_run("T-2")
        self.log(root, "2026-11-05T09:00:00+00:00", "many")
        self.log(root, "2026-11-05T09:10:00+00:00", [1])
        usd, issues = usage.spent_today(self.now)
        self.assertEqual((round(usd, 6), issues), (1.0, ["unreadable: T-2"]))

    def test_a_malformed_count_in_the_checked_runs_own_log_blocks_it(self):
        root = self.make_run("T-2")
        self.log(root, "2026-11-05T09:00:00+00:00", 5)
        event = {"event": "usage", "agent_id": "a1", "at": "2026-11-05T09:10:00+00:00",
                 "models": {"m": 7}}
        with open(root / "log.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
        self.assertEqual(usage.spent_today(self.now, task="T-2"), (None, ["blocked: T-2"]))

    def test_a_live_run_over_the_read_cap_blocks_when_it_is_the_checked_run(self):
        root = self.make_run("T-1")
        self.log(root, "2026-11-05T09:00:00+00:00", 1)
        with mock.patch.object(usage, "DAY_LOG_LIMIT", 10):
            self.assertEqual(usage.spent_today(self.now, task="T-1"), (None, ["blocked: T-1"]))
            self.assertEqual(usage.spent_today(self.now, task="T-9"), (0.0, ["unreadable: T-1"]))

    def test_no_runs_spent_nothing(self):
        self.assertEqual(usage.spent_today(self.now), (0.0, []))


if __name__ == "__main__":
    unittest.main()

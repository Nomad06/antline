import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import config, paths
from ppl.errors import PplError


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def write(self, text):
        paths.config_file().write_text(text, encoding="utf-8")

    def test_a_missing_file_is_an_empty_config(self):
        self.assertEqual(config.load(), {})
        self.assertIsNone(config.active_harness())

    def test_load_returns_the_parsed_tables(self):
        self.write('[harness]\nactive = "example-maven"\n')
        self.assertEqual(config.load(), {"harness": {"active": "example-maven"}})
        self.assertEqual(config.active_harness(), "example-maven")

    def test_no_harness_table_or_no_active_key_means_none(self):
        self.write("[other]\nkey = 1\n")
        self.assertIsNone(config.active_harness())
        self.write("[harness]\n")
        self.assertIsNone(config.active_harness())
        self.write('[harness]\nactive = ""\n')
        self.assertIsNone(config.active_harness())

    def test_a_non_table_harness_entry_is_schema_invalid_not_a_traceback(self):
        self.write('harness = "example-maven"\n')
        with self.assertRaises(PplError) as caught:
            config.active_harness()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("[harness]", caught.exception.message)

    def test_a_non_string_active_is_schema_invalid_not_a_traceback(self):
        for value in ("5", "[1]", "true"):
            self.write("[harness]\nactive = %s\n" % value)
            with self.assertRaises(PplError) as caught:
                config.active_harness()
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
            self.assertIn("harness.active", caught.exception.message)

    def test_invalid_toml_is_schema_invalid_not_a_traceback(self):
        self.write("[harness\nactive = ")
        with self.assertRaises(PplError) as caught:
            config.load()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("config.toml", caught.exception.message)

    def test_a_non_utf8_file_is_schema_invalid_not_a_traceback(self):
        paths.config_file().write_bytes(b"\xff\xfe\x00[harness]")
        with self.assertRaises(PplError) as caught:
            config.load()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_lessons_reflect_defaults_to_true(self):
        self.assertIs(config.lessons_reflect(), True)
        self.write("[lessons]\n")
        self.assertIs(config.lessons_reflect(), True)
        self.write("[lessons]\nreflect = true\n")
        self.assertIs(config.lessons_reflect(), True)
        self.write("[lessons]\nreflect = false\n")
        self.assertIs(config.lessons_reflect(), False)

    def test_an_unknown_lessons_key_is_refused(self):
        self.write("[lessons]\nreflekt = false\n")
        with self.assertRaises(PplError) as caught:
            config.lessons_reflect()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("unknown key reflekt", caught.exception.message)
        self.assertIn("the known key is reflect", caught.exception.message)

    def test_a_non_boolean_reflect_is_refused(self):
        for value in ('"false"', "0", "1", "[]"):
            with self.subTest(value=value):
                self.write("[lessons]\nreflect = %s\n" % value)
                with self.assertRaises(PplError) as caught:
                    config.lessons_reflect()
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("lessons.reflect", caught.exception.message)

    def test_lanes_max_defaults_to_three(self):
        self.assertEqual(config.lanes_max(), 3)
        self.write("[lanes]\n")
        self.assertEqual(config.lanes_max(), 3)
        self.write("[lanes]\nmax = 9\n")
        self.assertEqual(config.lanes_max(), 9)

    def test_lanes_max_outside_one_to_nine_is_refused(self):
        for value in ("0", "10", '"3"', "true"):
            with self.subTest(value=value):
                self.write("[lanes]\nmax = %s\n" % value)
                with self.assertRaises(PplError) as caught:
                    config.lanes_max()
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("lanes.max", caught.exception.message)

    def test_an_unknown_lanes_key_is_refused_naming_max(self):
        self.write("[lanes]\nmaxx = 2\n")
        with self.assertRaises(PplError) as caught:
            config.lanes_max()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("unknown key maxx", caught.exception.message)
        self.assertIn("the known key is max", caught.exception.message)

    def test_stall_settings_default(self):
        expected = {"notify_after_minutes": 15, "silent_after_minutes": 60,
                    "review_notify_after_minutes": 45, "keep_awake": False}
        self.assertEqual(config.stall_settings(), expected)
        self.write("[stall]\n")
        self.assertEqual(config.stall_settings(), expected)
        self.write("[stall]\nnotify_after_minutes = 1\nsilent_after_minutes = 1440\n"
                   "keep_awake = true\n")
        self.assertEqual(config.stall_settings(), dict(expected, notify_after_minutes=1,
                                                       silent_after_minutes=1440,
                                                       keep_awake=True))

    def test_an_unknown_stall_key_is_refused(self):
        self.write("[stall]\nnotify_after = 5\n")
        with self.assertRaises(PplError) as caught:
            config.stall_settings()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("unknown key notify_after", caught.exception.message)
        self.assertIn("notify_after_minutes", caught.exception.message)

    def test_a_stall_minutes_value_outside_one_to_1440_or_not_an_integer_is_refused(self):
        for key in config.STALL_KEYS[:3]:
            for value in ("0", "1441", "true", '"15"', "15.0"):
                with self.subTest(key=key, value=value):
                    self.write("[stall]\n%s = %s\n" % (key, value))
                    with self.assertRaises(PplError) as caught:
                        config.stall_settings()
                    self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                    self.assertIn("stall.%s" % key, caught.exception.message)

    def test_keep_awake_must_be_a_boolean(self):
        for value in ("1", '"yes"'):
            with self.subTest(value=value):
                self.write("[stall]\nkeep_awake = %s\n" % value)
                with self.assertRaises(PplError) as caught:
                    config.stall_settings()
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("stall.keep_awake", caught.exception.message)

    def test_notify_command_is_none_when_absent(self):
        self.assertIsNone(config.notify_command())
        self.write("[notify]\n")
        self.assertIsNone(config.notify_command())
        self.write('[notify]\ncommand = ["/opt/hook", "-q"]\n')
        self.assertEqual(config.notify_command(), ["/opt/hook", "-q"])

    def test_a_notify_command_that_is_not_a_list_of_words_is_refused(self):
        for value in ('"x"', "[]", '[""]', "[1]", '["x", 2]'):
            with self.subTest(value=value):
                self.write("[notify]\ncommand = %s\n" % value)
                with self.assertRaises(PplError) as caught:
                    config.notify_command()
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("notify.command", caught.exception.message)

    def test_an_unknown_notify_key_is_refused(self):
        self.write('[notify]\ncmd = ["x"]\n')
        with self.assertRaises(PplError) as caught:
            config.notify_command()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("unknown key cmd", caught.exception.message)

    def test_the_example_tables_uncommented_are_accepted(self):
        # The example ships [stall] and [notify] commented out. With the leading "# " taken
        # off, what it shows must be what config accepts.
        example = (paths.defaults_dir() / "config.example.toml").read_text(encoding="utf-8")
        lines, keep = [], False
        for line in example.splitlines():
            bare = line[2:] if line.startswith("# ") else line
            if bare.startswith("["):
                keep = bare in ("[stall]", "[notify]")
            if keep and line.startswith("# ") and (bare.startswith("[") or " = " in bare):
                lines.append(bare)
        self.assertIn("[stall]", lines)
        self.assertIn("[notify]", lines)
        self.write("\n".join(lines) + "\n")
        self.assertEqual(sorted(config.stall_settings()), sorted(config.STALL_KEYS))
        self.assertIsInstance(config.notify_command(), list)


class TestWrite(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = str(Path(tmphome.mkdtemp(prefix="pplhome-")) / "fresh")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def test_set_active_harness_creates_the_file_and_reads_back(self):
        config.set_active_harness("example-maven")
        self.assertEqual(config.active_harness(), "example-maven")
        config.set_active_harness("other")
        self.assertEqual(config.load(), {"harness": {"active": "other"}})

    def test_set_active_harness_keeps_every_other_setting(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text(
            '[harness]\nactive = "a"\nnote = "kept"\n\n[claude]\nbinary = "/x"\n',
            encoding="utf-8")
        config.set_active_harness("b")
        self.assertEqual(config.load(), {"harness": {"active": "b", "note": "kept"},
                                         "claude": {"binary": "/x"}})

    def test_set_active_harness_refuses_a_non_table_harness_entry(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text('harness = "a"\n', encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            config.set_active_harness("b")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), 'harness = "a"\n')

    def test_remove_drops_the_named_keys_and_says_which_were_there(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text('[claude]\nbinary = "c"\nworkspace = "/w"\n',
                                       encoding="utf-8")
        self.assertEqual(config.remove("claude", ["workspace", "hooks"]), ["workspace"])
        self.assertEqual(config.load(), {"claude": {"binary": "c"}})
        self.assertEqual(config.remove("claude", ["hooks"]), [])

    def test_remove_refuses_a_non_table_entry(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text('claude = "x"\n', encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            config.remove("claude", ["workspace"])
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

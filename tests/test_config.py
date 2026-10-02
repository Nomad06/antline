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



class TestWrite(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = str(Path(tmphome.mkdtemp(prefix="pplhome-")) / "fresh")
        self._skills = os.environ.pop("PPL_SKILLS_DIR", None)

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home
        if self._skills is not None:
            os.environ["PPL_SKILLS_DIR"] = self._skills

    def test_set_active_harness_creates_the_file_and_reads_back(self):
        config.set_active_harness("example-maven")
        self.assertEqual(config.active_harness(), "example-maven")
        config.set_active_harness("other")
        self.assertEqual(config.load(), {"harness": {"active": "other"}})

    def test_set_active_harness_keeps_every_other_setting(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text(
            '[harness]\nactive = "a"\nnote = "kept"\n\n[claude]\nskills_dir = "/x"\n',
            encoding="utf-8")
        config.set_active_harness("b")
        self.assertEqual(config.load(), {"harness": {"active": "b", "note": "kept"},
                                         "claude": {"skills_dir": "/x"}})

    def test_set_active_harness_refuses_a_non_table_harness_entry(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text('harness = "a"\n', encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            config.set_active_harness("b")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(paths.config_file().read_text(encoding="utf-8"), 'harness = "a"\n')

    def test_skills_dir_prefers_the_environment_then_config_then_the_default(self):
        self.assertEqual(config.skills_dir(), Path.home() / ".claude" / "skills")
        paths.home().mkdir(parents=True)
        paths.config_file().write_text('[claude]\nskills_dir = "/from/config"\n', encoding="utf-8")
        self.assertEqual(config.skills_dir(), Path("/from/config"))
        os.environ["PPL_SKILLS_DIR"] = "/from/env"
        try:
            self.assertEqual(config.skills_dir(), Path("/from/env"))
        finally:
            del os.environ["PPL_SKILLS_DIR"]

    def test_skills_dir_refuses_a_non_table_claude_entry(self):
        paths.home().mkdir(parents=True)
        paths.config_file().write_text('claude = "x"\n', encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            config.skills_dir()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import paths
from ppl.errors import PplError

REPO = Path(__file__).resolve().parents[1]


class TestRoots(unittest.TestCase):
    def test_root_is_the_checkout_when_ppl_root_is_unset(self):
        saved = os.environ.pop("PPL_ROOT", None)
        try:
            self.assertEqual(paths.root(), REPO)
        finally:
            if saved is not None:
                os.environ["PPL_ROOT"] = saved

    def test_root_honours_ppl_root(self):
        checkout = Path(tmphome.mkdtemp(prefix="pplroot-"))
        (checkout / "defaults").mkdir()
        with mock.patch.dict(os.environ, {"PPL_ROOT": str(checkout)}):
            self.assertEqual(paths.root(), checkout)

    def test_a_ppl_root_without_defaults_is_a_schema_error_naming_the_fix(self):
        bare = tmphome.mkdtemp(prefix="pplroot-")
        with mock.patch.dict(os.environ, {"PPL_ROOT": bare}):
            with self.assertRaises(PplError) as caught:
                paths.root()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn(bare, caught.exception.message)
        self.assertIn("PPL_ROOT", caught.exception.message)

    def test_a_non_editable_install_is_a_schema_error_naming_the_fix(self):
        # pip install . copies the ppl package into site-packages, without the
        # defaults, templates, skill and hooks beside it
        package = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplsite-"))) / "site-packages" / "ppl"
        package.mkdir(parents=True)
        environment = {k: v for k, v in os.environ.items() if k != "PPL_ROOT"}
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(paths, "__file__", str(package / "paths.py")):
            with self.assertRaises(PplError) as caught:
                paths.root()
        message = caught.exception.message
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn(str(package.parent), message)
        self.assertIn("pip install .", message)
        self.assertIn("pip install -e .", message)
        self.assertIn("clone", message)

    def test_home_default_is_dot_ppl(self):
        saved = os.environ.pop("PPL_HOME")
        try:
            self.assertEqual(paths.home(), Path.home() / ".ppl")
        finally:
            os.environ["PPL_HOME"] = saved

    def test_data_files_live_under_home(self):
        home = paths.home()
        self.assertEqual(paths.config_file(), home / "config.toml")
        self.assertEqual(paths.routing_file(), home / "routing.toml")
        self.assertEqual(paths.models_file(), home / "models.json")
        self.assertEqual(paths.harnesses_dir(), home / "harnesses")
        self.assertEqual(paths.harness_dir("demo"), home / "harnesses" / "demo")

    def test_defaults_live_under_root(self):
        self.assertEqual(paths.defaults_dir(), paths.root() / "defaults")
        self.assertTrue((paths.defaults_dir() / "routing.example.toml").is_file())
        self.assertTrue((paths.defaults_dir() / "config.example.toml").is_file())


if __name__ == "__main__":
    unittest.main()

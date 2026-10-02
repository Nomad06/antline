import json
import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import paths, store
from ppl.errors import PplError


class TestPaths(unittest.TestCase):
    def test_home_follows_env(self):
        self.assertEqual(paths.home(), Path(os.environ["PPL_HOME"]))

    def test_run_dir_is_under_runs(self):
        self.assertEqual(paths.run_dir("DEMO-1"), paths.runs_dir() / "DEMO-1")

    def test_run_dir_rejects_traversal(self):
        # rule: R-TASK-ID-TRAVERSAL
        with self.assertRaises(PplError) as ctx:
            paths.run_dir("../escape")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestStore(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        target = paths.home() / "x.json"
        store.write_json(target, {"a": 1})
        self.assertEqual(store.read_json(target), {"a": 1})

    def test_read_missing_returns_default(self):
        self.assertIsNone(store.read_json(paths.home() / "nope.json"))

    def test_read_corrupt_json_raises_a_routable_error(self):
        # rule: R-STORE-CORRUPT-JSON (E1/W5(b), cycle 2 wave B). Any state a
        # session left half-written, or any document mangled by hand, used to
        # surface as a raw json.JSONDecodeError - exit 1, past the documented
        # 0/2/1 contract - at whichever call site happened to read it first.
        # Fixed once here, at the one function every reader in the codebase
        # goes through, rather than at each call site individually.
        target = paths.home() / "corrupt.json"
        target.write_text("{not valid json", encoding="utf-8")
        with self.assertRaises(PplError) as ctx:
            store.read_json(target)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_write_json_accepts_a_str_path(self):
        target = str(paths.home() / "z.json")
        store.write_json(target, {"c": 3})
        self.assertEqual(store.read_json(target), {"c": 3})

    def test_write_is_atomic_leaving_no_temp_files(self):
        target = paths.home() / "y.json"
        store.write_json(target, {"b": 2})
        leftovers = [p.name for p in paths.home().iterdir() if p.name.startswith(".y.json")]
        self.assertEqual(leftovers, [])

    def test_append_log_writes_one_json_object_per_line(self):
        (paths.run_dir("DEMO-2")).mkdir(parents=True, exist_ok=True)
        store.append_log("DEMO-2", {"event": "one"})
        store.append_log("DEMO-2", {"event": "two"})
        lines = (paths.run_dir("DEMO-2") / "log.jsonl").read_text().strip().split("\n")
        self.assertEqual([json.loads(x)["event"] for x in lines], ["one", "two"])

    def test_append_log_stamps_time(self):
        (paths.run_dir("DEMO-3")).mkdir(parents=True, exist_ok=True)
        store.append_log("DEMO-3", {"event": "e"})
        line = json.loads((paths.run_dir("DEMO-3") / "log.jsonl").read_text().strip())
        self.assertIn("at", line)


if __name__ == "__main__":
    unittest.main()

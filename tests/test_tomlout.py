import tomllib
import unittest
from pathlib import Path

from ppl import tomlout

SOURCE = Path(__file__).resolve().parents[1] / "ppl" / "_assets"


def harness_doc(name):
    with open(SOURCE / "defaults" / "harnesses" / name / "harness.toml", "rb") as handle:
        return tomllib.load(handle)


class TestRoundTrip(unittest.TestCase):
    def assertRoundTrips(self, doc):
        text = tomlout.dumps(doc)
        self.assertEqual(tomllib.loads(text), doc, text)
        return text

    def test_both_example_harnesses_round_trip(self):
        for name in ("example-maven", "example-generic"):
            with self.subTest(harness=name):
                self.assertRoundTrips(harness_doc(name))

    def test_a_nested_table_array_is_written_as_table_array_entries(self):
        text = self.assertRoundTrips(harness_doc("example-maven"))
        self.assertIn("[[guard.shape]]\n", text)
        self.assertIn("[[gate]]\n", text)
        self.assertIn('required_flags = [["-pl", "^:[A-Za-z][\\\\w.-]*$"], ["-am", ""]]', text)

    def test_an_empty_table_survives(self):
        self.assertRoundTrips({"env": {}, "harness": {"name": "x"}})

    def test_every_supported_shape(self):
        self.assertRoundTrips({
            "title": "top",
            "count": 3,
            "ratio": 0.5,
            "on": True,
            "off": False,
            "empty": [],
            "words": ["a", "b"],
            "pairs": [["-x", ""], ["-y", "z"]],
            "harness": {"active": "example-maven", "nested": {"deep": {"leaf": 1}}},
            "setup": {"version": 1, "done": ["routing"], "skipped": []},
            "gate": [{"name": "build", "command": "make build"}, {"name": "lint", "command": "x"}],
            "guard": {"shape": [{"argv0": "mvn", "env_prefix": True, "one_goal_of": ["test"]}]},
            "inline": [{"a": 1, "b": {"c": "d"}}],
        })

    def test_strings_are_escaped_and_ascii(self):
        doc = {"s": 'quote " backslash \\ tab \t newline \n del \x7f bell \x07 e\u00e9 \U0001f600'}
        text = self.assertRoundTrips(doc)
        self.assertEqual(text, text.encode("ascii").decode("ascii"))

    def test_keys_that_are_not_bare_are_quoted(self):
        self.assertRoundTrips({"a.b": 1, "with space": {"x y": "z", "ok_key-1": 2}})

    def test_keys_starting_with_an_underscore_are_left_out(self):
        doc = harness_doc("example-maven")
        doc["_dir"] = "/somewhere"
        text = tomlout.dumps(doc)
        self.assertNotIn("_dir", text)
        del doc["_dir"]
        self.assertEqual(tomllib.loads(text), doc)

    def test_an_unsupported_value_is_a_type_error(self):
        with self.assertRaises(TypeError):
            tomlout.dumps({"x": object()})


if __name__ == "__main__":
    unittest.main()

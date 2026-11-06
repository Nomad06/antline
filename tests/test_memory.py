import os
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import memory
from ppl.errors import PplError

ROLES = ("ppl-code-worker", "ppl-verifier", "orchestrator")


def entry(n=12, **over):
    row = {"id": "M-%d" % n, "role": "ppl-code-worker",
           "cue": "a change touches a Liquibase changelog",
           "lesson": "run the module's migration test gate before handing back",
           "sources": ["KEY-123:finding:F-3", "KEY-123:remark:thread-1279"],
           "accepted_at": "2026-10-18T10:02:11Z", "seen": 1, "status": "active"}
    row.update(over)
    return row


def text_of(*rows, fmt=1):
    return memory.dumps(rows).replace("format = 1", "format = %s" % fmt, 1)


class TestParse(unittest.TestCase):
    def refused(self, text):
        with self.assertRaises(PplError) as caught:
            memory.parse(text, "memory.toml", ROLES)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertTrue(caught.exception.message.startswith("memory.toml: "))
        return caught.exception.message

    def test_a_valid_file_parses_into_entries(self):
        # rule: R-MEMORY-CAP
        text = (
            'format = 1\n\n[[entry]]\nid = "M-12"\nrole = "ppl-code-worker"\n'
            'cue = "a change touches a Liquibase changelog"\n'
            'lesson = "run the module\'s migration test gate before handing back"\n'
            'sources = ["KEY-123:finding:F-3", "KEY-123:remark:thread-1279"]\n'
            'accepted_at = "2026-10-18T10:02:11Z"\nseen = 1\nstatus = "active"\n')
        self.assertEqual(memory.parse(text, "memory.toml", ROLES), [entry()])

    def test_an_empty_text_is_no_memory(self):
        self.assertEqual(memory.parse("", "memory.toml", ROLES), [])
        self.assertEqual(memory.parse(b"", "memory.toml", ROLES), [])

    def test_invalid_toml_and_a_wrong_format_are_refused(self):
        self.refused("format = ")
        self.assertIn("format", self.refused(text_of(entry(), fmt=2)))

    def test_a_top_level_key_other_than_format_and_entry_is_refused(self):
        self.assertIn("extra", self.refused(text_of(entry()) + 'extra = 1\n'))

    def test_unknown_role_is_refused(self):
        # rule: R-MEMORY-CAP
        with self.assertRaises(PplError) as caught:
            memory.parse(text_of(entry(role="ppl-nobody")), "memory.toml", ROLES)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("ppl-nobody", caught.exception.message)

    def test_a_thirteenth_active_entry_for_one_role_is_refused(self):
        # rule: R-MEMORY-CAP
        rows = [entry(n, cue="cue %d" % n) for n in range(1, 13)]
        rows.append(entry(13, cue="retired one", status="retired"))
        self.assertEqual(len(memory.parse(memory.dumps(rows), "memory.toml", ROLES)), 13)
        rows[-1]["status"] = "active"
        with self.assertRaises(PplError) as caught:
            memory.parse(memory.dumps(rows), "memory.toml", ROLES)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertIn("ppl-code-worker", caught.exception.message)
        self.assertIn("retire", caught.exception.message)

    def test_the_cap_is_per_role(self):
        rows = [entry(n, cue="cue %d" % n) for n in range(1, 13)]
        rows.append(entry(13, role="ppl-verifier"))
        self.assertEqual(len(memory.parse(memory.dumps(rows), "memory.toml", ROLES)), 13)

    def test_a_cue_over_300_characters_is_refused(self):
        # rule: R-MEMORY-CAP
        memory.parse(text_of(entry(cue="x" * 300)), "memory.toml", ROLES)
        with self.assertRaises(PplError) as caught:
            memory.parse(text_of(entry(cue="x" * 301)), "memory.toml", ROLES)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.refused(text_of(entry(lesson="y" * 301)))

    def test_a_cue_with_a_when_in_the_middle_is_accepted(self):
        row = entry(cue="a change when x lands")
        self.assertEqual(memory.parse(text_of(row), "memory.toml", ROLES), [row])

    def test_a_cue_that_is_empty_once_cleaned_is_refused(self):
        self.refused(text_of(entry(cue="##")))

    def test_an_empty_cue_or_lesson_is_refused(self):
        self.refused(text_of(entry(cue="")))
        self.refused(text_of(entry(lesson="")))

    def test_text_that_is_not_clean_is_refused(self):
        self.refused(text_of(entry(cue="a\n## x")))
        self.refused(text_of(entry(cue="when x")))
        self.refused(text_of(entry(lesson="two  spaces")))

    def test_sources_are_required(self):
        self.refused(text_of(entry(sources=[])))
        self.refused(text_of(entry(sources=[""])))
        self.refused(text_of(entry(sources="KEY-1:finding:F-1")))

    def test_a_repeated_id_is_refused(self):
        self.assertIn("M-12", self.refused(text_of(entry(), entry(cue="other"))))

    def test_a_malformed_id_is_refused(self):
        for bad in ("M-0", "M-01", "12", "m-1"):
            self.refused(text_of(entry(id=bad)))

    def test_seen_must_be_a_positive_integer(self):
        self.refused(text_of(entry(seen=0)))
        self.refused(text_of(entry()).replace("seen = 1", "seen = true"))

    def test_accepted_at_and_status_are_checked(self):
        self.refused(text_of(entry()).replace('accepted_at = "2026-10-18T10:02:11Z"',
                                              "accepted_at = 2026-10-18T10:02:11Z"))
        self.refused(text_of(entry(status="gone")))

    def test_unknown_and_missing_keys_are_refused(self):
        self.refused(text_of(entry()) + 'color = "red"\n')
        self.refused(text_of(entry()).replace('status = "active"\n', ""))


class TestClean(unittest.TestCase):
    def test_clean_strips_headings_comments_markers_and_fences(self):
        self.assertEqual(memory.clean("## Big\n<!-- include: harness/gates --> run ``` x"),
                         "Big run x")

    def test_clean_strips_a_heading_on_any_line(self):
        self.assertEqual(memory.clean("a\n## b"), "a b")
        self.assertEqual(memory.clean("a\r\n# h"), "a h")
        self.assertEqual(memory.clean("a\r# h\x0b## i"), "a h i")

    def test_clean_drops_an_unclosed_comment_and_control_characters(self):
        self.assertEqual(memory.clean("a <!-- b\nc"), "a")
        self.assertEqual(memory.clean("a\x00b\x1bc\td"), "a b c d")
        self.assertEqual(memory.clean("<!-- include: x"), "")

    def test_clean_cue_drops_a_leading_when(self):
        self.assertEqual(memory.clean_cue("When  a change lands"), "a change lands")
        self.assertEqual(memory.clean_cue("whenever"), "whenever")

    def test_clean_cue_keeps_a_when_in_the_middle(self):
        self.assertEqual(memory.clean_cue("a change when x"), "a change when x")
        self.assertEqual(memory.clean_cue("somewhen x"), "somewhen x")

    def test_normalize_cue_ignores_case_and_trailing_punctuation(self):
        self.assertEqual(memory.normalize_cue("When A Change Lands.!"), "a change lands")
        self.assertEqual(memory.normalize_cue("a change lands"), "a change lands")


class TestRoundTrip(unittest.TestCase):
    def test_a_lesson_round_trips_through_dumps(self):
        rows = [entry(lesson='say "hi" \\ café — done')]
        text = memory.dumps(rows)
        self.assertTrue(text.isascii())
        self.assertEqual(memory.parse(text, "memory.toml", ROLES), rows)

    def test_dumps_keys_entries_in_the_declared_order(self):
        shuffled = dict(reversed(list(entry().items())))
        text = memory.dumps([shuffled])
        self.assertEqual(memory.parse(text, "memory.toml", ROLES), [entry()])
        self.assertLess(text.index("id ="), text.index("status ="))

    def test_next_id_follows_the_highest(self):
        self.assertEqual(memory.next_id([]), "M-1")
        self.assertEqual(memory.next_id([entry(3), entry(12), entry(7)]), "M-13")

    def test_active_filters_by_status_and_role(self):
        rows = [entry(1), entry(2, status="retired"), entry(3, role="ppl-verifier")]
        self.assertEqual([r["id"] for r in memory.active(rows)], ["M-1", "M-3"])
        self.assertEqual([r["id"] for r in memory.active(rows, "ppl-verifier")], ["M-3"])


class TestRead(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tmphome.mkdtemp(prefix="pplmemory-"))

    def test_an_absent_file_reads_as_no_entries(self):
        self.assertEqual(memory.read(self.dir, ROLES), ([], b""))

    def test_a_file_reads_with_its_bytes(self):
        data = memory.dumps([entry()]).encode("utf-8")
        (self.dir / memory.FILE).write_bytes(data)
        self.assertEqual(memory.read(self.dir, ROLES), ([entry()], data))

    def test_a_directory_in_its_place_is_refused(self):
        (self.dir / memory.FILE).mkdir()
        with self.assertRaises(PplError) as caught:
            memory.read(self.dir, ROLES)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")

    def test_a_file_that_is_not_utf8_is_refused(self):
        (self.dir / memory.FILE).write_bytes(b"\xff\xfe")
        with self.assertRaises(PplError) as caught:
            memory.read(self.dir, ROLES)
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")


if __name__ == "__main__":
    unittest.main()

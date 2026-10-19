import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ.setdefault("PPL_HOME", tmphome.mkdtemp(prefix="pplhome-"))

from ppl import boundary
from ppl.errors import PplError

BOUND = {
    "write": ["source/core/orders/**"],
    "forbid": ["source/core/ledger/**"],
}


class TestGlob(unittest.TestCase):
    def test_double_star_spans_directories(self):
        self.assertTrue(boundary.glob_match("a/b/c/d.java", "a/**"))

    def test_single_star_does_not_span_separator(self):
        self.assertFalse(boundary.glob_match("a/b/c.java", "a/*.java"))

    def test_suffix_pattern(self):
        self.assertTrue(boundary.glob_match("x/y/FooIT.java", "**/*IT.java"))

    def test_suffix_pattern_at_root_level(self):
        self.assertTrue(boundary.glob_match("FooIT.java", "**/*IT.java"))


class TestPatternsFromSnapshot(unittest.TestCase):
    def test_a_snapshot_without_a_tests_key_means_the_java_defaults(self):
        self.assertEqual(boundary.patterns_from_snapshot({"format": 1}), boundary.TEST_PATTERNS)

    def test_a_valid_tests_value_is_the_list_as_a_tuple(self):
        doc = {"tests": {"sources": ["tests/**", "test/**"]}}
        self.assertEqual(boundary.patterns_from_snapshot(doc), ("tests/**", "test/**"))

    def test_a_malformed_tests_value_reads_as_untrustworthy(self):
        for tests in (None, 5, [], {}, {"sources": []}, {"sources": "x/**"},
                      {"sources": ["ok", ""]}, {"sources": [7]}):
            with self.subTest(tests=tests):
                self.assertIsNone(boundary.patterns_from_snapshot({"tests": tests}))
        for doc in (None, [], "x", 3):
            with self.subTest(doc=doc):
                self.assertIsNone(boundary.patterns_from_snapshot(doc))


class TestGlobProblem(unittest.TestCase):
    def test_the_java_defaults_and_ordinary_test_globs_have_no_problem(self):
        for glob in boundary.TEST_PATTERNS + ("tests/**", "test/**", "**/*_test.go", "**/*.spec.ts"):
            with self.subTest(glob=glob):
                self.assertIsNone(boundary.glob_problem(glob))

    def test_a_product_glob_names_the_probe_it_matches(self):
        self.assertIn("'src/main.py'", boundary.glob_problem("src/**"))


class TestCodeWorker(unittest.TestCase):
    def test_allows_path_inside_write_boundary(self):
        boundary.check_path("ppl-code-worker", "source/core/orders/Foo.java", BOUND)

    def test_rejects_path_outside_write_boundary(self):
        # rule: R-ROLE-CODE-WORKER-BOUNDARY
        with self.assertRaises(PplError) as ctx:
            boundary.check_path("ppl-code-worker", "source/core/other/Foo.java", BOUND)
        self.assertEqual(ctx.exception.code, "BOUNDARY_VIOLATION")

    def test_rejects_test_sources_even_inside_boundary(self):
        # rule: R-ROLE-CODE-WORKER-TESTS
        with self.assertRaises(PplError):
            boundary.check_path(
                "ppl-code-worker", "source/core/orders/src/test/java/FooTest.java", BOUND)

    def test_rejects_forbidden_even_if_write_pattern_matches(self):
        # rule: R-ROLE-FORBIDDEN-PATHS
        wide = {"write": ["source/**"], "forbid": ["source/core/ledger/**"]}
        with self.assertRaises(PplError):
            boundary.check_path("ppl-code-worker", "source/core/ledger/L.java", wide)

    def test_rejects_path_with_traversal_sequence(self):
        # rule: R-PATH-TRAVERSAL
        with self.assertRaises(PplError) as ctx:
            boundary.check_path(
                "ppl-code-worker", "source/core/orders/../../ledger/L.java", BOUND)
        self.assertEqual(ctx.exception.code, "BOUNDARY_VIOLATION")
        self.assertIn("..", ctx.exception.message)

    def test_rejects_absolute_path(self):
        # rule: R-PATH-ABSOLUTE
        with self.assertRaises(PplError) as ctx:
            boundary.check_path("ppl-code-worker", "/source/core/orders/Foo.java", BOUND)
        self.assertEqual(ctx.exception.code, "BOUNDARY_VIOLATION")
        self.assertIn("absolute", ctx.exception.message)


class TestTestWorker(unittest.TestCase):
    def test_allows_test_sources(self):
        boundary.check_path(
            "ppl-test-worker", "source/core/orders/src/test/java/FooIT.java", BOUND)

    def test_rejects_main_sources(self):
        # rule: R-ROLE-TEST-WORKER-MAIN
        with self.assertRaises(PplError):
            boundary.check_path(
                "ppl-test-worker", "source/core/orders/src/main/java/Foo.java", BOUND)

    def test_rejects_path_with_traversal_sequence(self):
        with self.assertRaises(PplError) as ctx:
            boundary.check_path(
                "ppl-test-worker", "source/core/orders/src/test/../../ledger/Evil.java", BOUND)
        self.assertEqual(ctx.exception.code, "BOUNDARY_VIOLATION")
        self.assertIn("..", ctx.exception.message)


class TestReadOnlyRoles(unittest.TestCase):
    def test_verifier_may_write_nothing(self):
        # rule: R-ROLE-READONLY-NO-WRITE
        for path in ["source/core/orders/Foo.java", "anything.txt"]:
            with self.assertRaises(PplError):
                boundary.check_path("ppl-verifier", path, BOUND)

    def test_every_read_only_role_may_write_nothing(self):
        # rule: R-ROLE-READONLY-NO-WRITE
        for role in ("ppl-briefer", "ppl-brief-challenger", "ppl-verifier",
                     "ppl-review-responder"):
            for path in ["source/core/orders/Foo.java", "anything.txt"]:
                with self.subTest(role=role, path=path), self.assertRaises(PplError):
                    boundary.check_path(role, path, BOUND)

    def test_every_generated_role_has_a_row(self):
        # The guard and the dispatch counter recognise a role by its row here,
        # so a generated role without one would pass the cage as a stranger.
        from ppl import routing
        self.assertEqual(set(boundary.ROLE_RULES), set(routing.REQUIRED_ROLES))


class TestDiff(unittest.TestCase):
    DIFF = (
        "diff --git a/source/core/orders/Foo.java b/source/core/orders/Foo.java\n"
        "--- a/source/core/orders/Foo.java\n"
        "+++ b/source/core/orders/Foo.java\n"
        "@@ -1 +1 @@\n-old\n+new\n"
        "diff --git a/source/core/ledger/L.java b/source/core/ledger/L.java\n"
        "--- a/source/core/ledger/L.java\n"
        "+++ b/source/core/ledger/L.java\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    def test_extracts_both_paths(self):
        self.assertEqual(
            boundary.diff_paths(self.DIFF),
            ["source/core/orders/Foo.java", "source/core/ledger/L.java"],
        )

    def test_handles_deleted_file_marker(self):
        text = "--- a/source/core/orders/Gone.java\n+++ /dev/null\n"
        self.assertEqual(boundary.diff_paths(text), ["source/core/orders/Gone.java"])

    def test_check_diff_rejects_when_any_path_escapes(self):
        # rule: R-APPLY-PATCH-BOUNDARY
        with self.assertRaises(PplError) as ctx:
            boundary.check_diff("ppl-code-worker", self.DIFF, BOUND)
        self.assertEqual(ctx.exception.code, "BOUNDARY_VIOLATION")
        self.assertIn("source/core/ledger/L.java", ctx.exception.message)

    def test_check_diff_rejects_crafted_traversal_path(self):
        # rule: R-PATH-TRAVERSAL
        traversal_diff = (
            "diff --git a/source/core/orders/Foo.java b/source/core/orders/Foo.java\n"
            "--- a/source/core/orders/../../ledger/L.java\n"
            "+++ b/source/core/orders/../../ledger/L.java\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        with self.assertRaises(PplError) as ctx:
            boundary.check_diff("ppl-code-worker", traversal_diff, BOUND)
        self.assertEqual(ctx.exception.code, "BOUNDARY_VIOLATION")
        self.assertIn("..", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()

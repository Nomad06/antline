"""House rules that no other test checks (N2, cycle 2 wave C).

The build plan stated "no semicolons in comments" as a repo house
rule, alongside ASCII-only and no tabs. Nothing enforced it: fourteen comment
lines had picked up a semicolon by cycle 2 wave B, none of them a routing
mistake, just an ordinary sentence written the ordinary way. This module is
the check that was missing, so the next one does not go unnoticed either.

`tokenize` rather than a line regex, so a semicolon inside a string literal
that happens to follow a `#` on the same line - `self.assertEqual(x, "a;b")`
after a trailing comment, or the reverse - is never mistaken for one inside
the comment itself.
"""
import io
import tokenize
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

EXCLUDED_DIR_NAMES = ("__pycache__", ".git")


def python_files(root):
    for path in sorted(root.rglob("*.py")):
        if any(part in EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        yield path


def semicolon_comments(path):
    """[(line_number, comment_text), ...] for every comment on `path` with a ';' in it."""
    found = []
    with open(path, "rb") as handle:
        tokens = tokenize.tokenize(handle.readline)
        for token in tokens:
            if token.type == tokenize.COMMENT and ";" in token.string:
                found.append((token.start[0], token.string))
    return found


class TestNoSemicolonsInComments(unittest.TestCase):
    """rule: N2"""

    def test_the_checker_finds_a_semicolon_in_a_synthetic_comment(self):
        source = "x = 1  # a comment; with a semicolon\n"
        found = semicolon_comments_from_text(source)
        self.assertEqual(len(found), 1)

    def test_the_checker_ignores_a_semicolon_inside_a_string_literal(self):
        # The bait this checker exists to resist: a semicolon that sits after
        # a '#' on the same line but inside a string, not inside the comment.
        source = 'x = "a;b"  # a clean comment\n'
        self.assertEqual(semicolon_comments_from_text(source), [])

    def test_the_checker_ignores_a_string_literal_containing_a_hash_and_semicolon(self):
        source = 'x = "#39;"\n'
        self.assertEqual(semicolon_comments_from_text(source), [])

    def test_no_tracked_python_file_has_a_semicolon_in_a_comment(self):
        problems = []
        for path in python_files(REPO):
            for line_number, comment in semicolon_comments(path):
                problems.append("%s:%d %s" % (path.relative_to(REPO), line_number, comment))
        self.assertEqual(problems, [], "\n".join(problems))


def semicolon_comments_from_text(source):
    found = []
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for token in tokens:
        if token.type == tokenize.COMMENT and ";" in token.string:
            found.append((token.start[0], token.string))
    return found


if __name__ == "__main__":
    unittest.main()

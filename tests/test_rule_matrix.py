"""The rule matrix, as a test.

Four times in cycle 1 a document claimed enforcement that no code performed:
the dispatch cap at stage boundaries, the Jira gate, `ppl apply-patch`
validating every worker patch, and the lease. Each survived a per-task review
and a whole-branch review, because prose and code drifted apart and nothing
mechanical noticed.

This module is the thing that notices. It is deliberately not a document: a
document would drift the same way.
"""
import ast
import os
import re
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported by
# path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ.setdefault("PPL_HOME", tmphome.mkdtemp(prefix="pplhome-"))

from ppl import rules, run
from ppl.errors import PplError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import ppl_guard

REPO = Path(__file__).resolve().parents[1]

# The documents whose claims have to name a row, as (label, text). The skill is
# the one the orchestrator reads at runtime, so a false claim there does more
# damage than one in DESIGN.md, which only people read - and the cycle-1
# Jira-gate claim lived in it. The skill on disk is rendered from skill/
# skeletons plus the forge and harness fragments, so the scanned copy is a
# rendered one: a claim a fragment brings in is checked the same as one
# written in the skeleton. Every file of both example skills is scanned,
# references/* included, since the orchestrator reads those on demand.
import fixtures


def skill_docs(name, files):
    return [("%s %s" % (name, path), text) for path, text in sorted(files.items())]


SCANNED_DOCS = [("DESIGN.md", (REPO / "docs" / "DESIGN.md").read_text(encoding="utf-8")),
                ("README.md", (REPO / "README.md").read_text(encoding="utf-8"))]
for _example in ("example-maven", "example-generic"):
    SCANNED_DOCS += skill_docs(_example, fixtures.skill_files(_example))
SCANNED_DOCS += skill_docs("github-issues", fixtures.skill_files("example-generic",
                                                                tables=fixtures.GITHUB_ISSUES))

GOOD_ROW = {
    "id": "R-EXAMPLE",
    "statement": "an example rule",
    "scope": "global",
    "event": "PreToolUse",
    "enforcement": "blocking",
    "hook_or_check": "ppl/boundary.py:check_path",
    "deny_test": "tests/test_boundary.py::TestCodeWorker::test_rejects_path_outside_write_boundary",
    "allow_test": "",
    "owner": "guard-hook",
    "residual": "",
}


def table(*rows):
    return {"rule": [dict(row) for row in rows]}


def row(**overrides):
    merged = dict(GOOD_ROW)
    merged.update(overrides)
    return merged


class PplHomeTestCase(unittest.TestCase):
    """PPL_HOME is set per test and restored, so no test can reach the real home."""

    def setUp(self):
        self._previous_home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        if self._previous_home is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = self._previous_home


class TestValidateRejectsATableThatWouldLie(PplHomeTestCase):
    """The checker's own deny tests, on synthetic tables.

    Every assertion the real matrix below makes is only worth the strictness of
    this validator, so each way a row could quietly claim more than it delivers
    is pinned here against input built for the purpose.
    """

    def test_a_complete_row_validates(self):
        self.assertEqual(len(rules.validate(table(row()))), 1)

    def test_an_empty_table_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate({"rule": []})
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_missing_field_is_refused(self):
        broken = row()
        del broken["owner"]
        with self.assertRaises(PplError) as caught:
            rules.validate(table(broken))
        self.assertIn("owner", caught.exception.message)

    def test_a_non_string_field_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(statement=["a", "b"])))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_an_unknown_enforcement_value_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(enforcement="mostly")))
        self.assertIn("mostly", caught.exception.message)

    def test_a_duplicate_id_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(), row()))
        self.assertIn("R-EXAMPLE", caught.exception.message)

    def test_an_id_that_does_not_look_like_one_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(id="rule 1")))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_an_empty_statement_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(statement="")))
        self.assertIn("statement", caught.exception.message)

    def test_a_blocking_row_without_a_deny_test_is_refused(self):
        # rule: R-DOC-RULE-IDS
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(deny_test="")))
        self.assertIn("deny_test", caught.exception.message)

    def test_a_blocking_row_without_a_check_is_refused(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(hook_or_check="")))
        self.assertIn("hook_or_check", caught.exception.message)

    def test_a_soft_row_still_needs_a_check(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(enforcement="soft", deny_test="", hook_or_check="")))
        self.assertIn("hook_or_check", caught.exception.message)

    def test_an_na_row_without_a_residual_is_refused(self):
        # "not applicable" and "needed but not built" must not look alike.
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(enforcement="n/a", hook_or_check="none",
                                     deny_test="", residual="")))
        self.assertIn("residual", caught.exception.message)

    def test_an_na_residual_must_say_which_kind_it_is(self):
        # rule: R-DOC-RULE-IDS
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(enforcement="n/a", hook_or_check="none", deny_test="",
                                     residual="nothing enforces this")))
        self.assertIn("GAP:", caught.exception.message)

    def test_an_na_row_may_not_name_a_check(self):
        # An n/a row naming a check reads as covered at a glance, which is the
        # confusion the three-value vocabulary exists to remove.
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(enforcement="n/a", deny_test="",
                                     hook_or_check="ppl/boundary.py:check_path",
                                     residual="GAP: nothing enforces this")))
        self.assertIn("hook_or_check", caught.exception.message)

    def test_an_na_row_may_not_name_a_deny_test(self):
        with self.assertRaises(PplError) as caught:
            rules.validate(table(row(enforcement="n/a", hook_or_check="none",
                                     residual="GAP: nothing enforces this")))
        self.assertIn("deny_test", caught.exception.message)

    def test_a_gap_row_is_accepted_with_an_honest_residual(self):
        rows = rules.validate(table(row(enforcement="n/a", hook_or_check="none", deny_test="",
                                        residual="GAP: no code performs this check at all")))
        self.assertEqual(rows[0]["enforcement"], "n/a")

    def test_a_property_row_is_accepted(self):
        rows = rules.validate(table(row(enforcement="n/a", hook_or_check="none", deny_test="",
                                        residual="PROPERTY: the harness makes this true")))
        self.assertEqual(rows[0]["enforcement"], "n/a")

    def test_gaps_counts_only_the_gap_rows(self):
        rows = rules.validate(table(
            row(),
            row(id="R-GAP", enforcement="n/a", hook_or_check="none", deny_test="",
                residual="GAP: not built"),
            row(id="R-PROP", enforcement="n/a", hook_or_check="none", deny_test="",
                residual="PROPERTY: nothing to build")))
        self.assertEqual([r["id"] for r in rules.gaps(rows)], ["R-GAP"])


FENCE_RE = re.compile(r"^\s*```")
TABLE_RE = re.compile(r"^\s*\|")
TABLE_RULE_RE = re.compile(r"^\s*\|[\s|:-]+\|\s*$")
LIST_RE = re.compile(r"^\s*(?:[-*]\s|\d+\.\s)")
HEADING_RE = re.compile(r"^\s*#")
EMPHASIS_RE = re.compile(r"\*\*?")
# Sentence end: a terminator followed by space, but not the period inside a
# common abbreviation - splitting there would strand a keyword in one fragment
# and its marker in the next, and the test would demand a marker for half a
# sentence.
SENTENCE_SPLIT_RE = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bvs)(?<!\bcf)(?<=[.!?])\s+")
def _keyword_pattern(word):
    """A `\\b`-bounded literal, except for a "..." placeholder in `word`,

    which stands for "the first phrase, then the second, later in the same
    sentence" - "only ... may" matches "only that role may write" without
    also matching every other use of either word alone. The split pieces are
    still escaped and still `\\b`-bounded; only the gap between them is a
    wildcard, and it cannot cross a sentence boundary because `sentences()`
    already cuts the text into one-sentence units before this pattern runs.

    A leading `(?<!-)` on the first piece keeps "read-only" out of it: `\b`
    already sits on both sides of "only" there (a hyphen is a non-word
    character), so "ppl-briefer ... read-only, may call context-explorer"
    matched "only ... may" across a role description that makes no claim at
    all - the exact false positive a whole rule-description table would have
    produced on every row.
    """
    if "..." in word:
        first, _, second = word.partition("...")
        return r"(?<!-)\b%s\b.*?\b%s\b" % (re.escape(first.strip()), re.escape(second.strip()))
    return r"\b%s\b" % re.escape(word)


KEYWORD_RE = re.compile(r"|".join(_keyword_pattern(word) for word in rules.KEYWORDS),
                        re.IGNORECASE)


def blocks(text):
    """(line number, text) for each markdown block, code fences dropped.

    A block is the unit a marker may sit in: one table row, one heading, one
    list item with its continuation lines, or one paragraph. Fenced code is
    dropped entirely - a JSON sample or a CLI listing makes no claim.
    """
    out = []
    current = []
    start = 0
    fenced = False
    lines = text.splitlines()

    def flush():
        nonlocal current, start
        if current:
            out.append((start, " ".join(line.strip() for line in current).strip()))
        current = []

    for number, line in enumerate(lines, start=1):
        if FENCE_RE.match(line):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not line.strip():
            flush()
            continue
        if TABLE_RE.match(line) or HEADING_RE.match(line) or LIST_RE.match(line):
            flush()
            if TABLE_RULE_RE.match(line):
                continue
            # A table header names its columns and claims nothing - "| Rule |
            # Reason | Scope |" is not a sentence about the code. It is
            # recognised by the delimiter row that must follow it.
            if TABLE_RE.match(line) and number < len(lines) and TABLE_RULE_RE.match(lines[number]):
                continue
            current = [line]
            start = number
            if TABLE_RE.match(line) or HEADING_RE.match(line):
                flush()
            continue
        if not current:
            start = number
        current.append(line)
    flush()
    return out


def sentences(block):
    return [part.strip() for part in SENTENCE_SPLIT_RE.split(EMPHASIS_RE.sub("", block))
            if part.strip()]


def claims(text):
    """Every sentence in `text` that says something about what the code does.

    A heading names a section, the same way a table header names its columns
    - "## When something stops" is not a sentence about the code, and was
    flagged as one the moment "never"/"stops" widened KEYWORDS to catch a
    plain English section title.
    """
    found = []
    for number, block in blocks(text):
        if HEADING_RE.match(block):
            continue
        for sentence in sentences(block):
            if KEYWORD_RE.search(sentence):
                found.append((number, sentence))
    return found


def cited(text):
    """(rule id, disclosed as n/a) for every marker in `text`, in order."""
    return [(found.group(1), bool(found.group(2))) for found in rules.MARKER_RE.finditer(text)]


def _citation_problem(rule_id, disclosed, rows):
    entry = rows.get(rule_id)
    if entry is None:
        return "names unknown rule %s" % rule_id
    is_na = entry["enforcement"] == "n/a"
    if is_na and not disclosed:
        return ("cites %s, which is n/a - write it [%s n/a] so the claim carries its own "
                "disclosure" % (rule_id, rule_id))
    if disclosed and not is_na:
        return "cites %s as n/a, but that row is %s" % (rule_id, entry["enforcement"])
    return None


def unmapped(text, rows):
    """Claims that name no rule, or name one that does not hold up.

    `rows` maps id -> row. A bare set of ids is accepted too, for synthetic
    input that does not care about enforcement.
    """
    if not isinstance(rows, dict):
        rows = {rule_id: {"enforcement": "blocking"} for rule_id in rows}
    problems = []
    for number, sentence in claims(text):
        marked = cited(sentence)
        if not marked:
            problems.append((number, sentence, "names no rule id"))
            continue
        for rule_id, disclosed in marked:
            problem = _citation_problem(rule_id, disclosed, rows)
            if problem:
                problems.append((number, sentence, problem))
    return problems


def unmapped_claims(docs, rows):
    problems = []
    for label, text in docs:
        problems += ["%s:%d %s -- %s" % (label, number, problem, sentence)
                     for number, sentence, problem in unmapped(text, rows)]
    return problems


def dangling(text, rows):
    """Markers anywhere in `text` - claim sentence or not - that do not hold up.

    `unmapped` only ever looks inside a sentence carrying a claim word, so a
    marker in a table cell, a heading or an ordinary sentence was checked by
    nothing: deleting its row left the citation pointing at nothing and the
    suite green. Ten of the markers in DESIGN.md, the whole cage table among
    them, were in exactly that position.
    """
    if not isinstance(rows, dict):
        rows = {rule_id: {"enforcement": "blocking"} for rule_id in rows}
    problems = []
    for rule_id, disclosed in cited(text):
        problem = _citation_problem(rule_id, disclosed, rows)
        if problem:
            problems.append((rule_id, problem))
    return problems


def test_body(locator):
    """The source of one `path::Class::test_name`, or None when it is absent."""
    try:
        path, class_name, test_name = locator.split("::")
    except ValueError:
        return None
    source = REPO / path
    if not source.is_file():
        return None
    lines = source.read_text(encoding="utf-8").splitlines()
    in_class = False
    body = []
    for line in lines:
        if line.startswith("class "):
            in_class = line.startswith("class %s(" % class_name)
            continue
        if not in_class:
            continue
        if body:
            if line.startswith("    def ") or line.startswith("class "):
                break
            body.append(line)
            continue
        if line.strip().startswith("def %s(" % test_name):
            body.append(line)
    return "\n".join(body) if body else None


def mentions(body, rule_id):
    """True when `body` names exactly this rule, not one that starts with it.

    A plain substring test would let a test annotated `R-BUDGET-USD-BEFORE-
    DISPATCH` stand as the deny test for `R-BUDGET-USD`, which is a quieter
    version of the same lie this module exists to catch.
    """
    return re.search(re.escape(rule_id) + r"(?![A-Z0-9-])", body or "") is not None


def code_only(body):
    """`body` with its comments and docstrings removed.

    The split this module rests on: the rule id may be named in a comment,
    because saying which rule a test enforces is prose and prose is the right
    tool for it - but the refusal must be proven by code, because proving is
    not something prose can do. Without the split, a body reading

        # rule: R-GIT-ADD-ALL
        # for unknown reasons this always passes without denying anything
        self.assertTrue(True)

    satisfied both halves of the deny-test check: `mentions` found the id in the
    first comment and the refusal shapes found "unknown" in the second. Widening
    or narrowing the word list cannot fix that - only reading the two halves
    from different text can.
    """
    source = textwrap.dedent(body or "")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # A body that does not parse standalone would fail the suite on its own
        # merits. Strip whole-line comments so the common annotation still does
        # not count, and leave the rest alone.
        return "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
    for node in ast.walk(tree):
        docstring_holder = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                             ast.ClassDef, ast.Module))
        if docstring_holder and node.body and isinstance(node.body[0], ast.Expr) \
                and isinstance(node.body[0].value, ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body.pop(0)
            if not node.body:
                node.body.append(ast.Pass())
    return ast.unparse(ast.fix_missing_locations(tree))


# A deny test has to actually assert a refusal. Without this, `mentions` alone
# would let any passing test be annotated into the deny-test slot - the rule id
# is a comment, and a comment asserts nothing. Applied to `code_only(body)`,
# never to the raw body.
REFUSAL_SHAPES = (
    re.compile(r"\bassertRaises\b"),
    re.compile(r"\berror_code\b"),
    re.compile(r"\bdeny_reason\b"),
    re.compile(r"\bassertNotIn\b"),
    re.compile(r"\bassertFalse\("),
    re.compile(r"\bassertIsNotNone\(\s*(?:ppl_guard\.)?decide|\bassertIsNotNone\(\s*result"),
    # a code from the closed failure vocabulary, or an HTTP refusal status
    re.compile(r"\b(?:BOUNDARY_VIOLATION|OWNER_DECISION|SCHEMA_INVALID|ILLEGAL_TRANSITION"
               r"|BUDGET_EXHAUSTED|IMPLEMENTATION_FAILURE|EXTERNAL_BLOCKED"
               r"|FORMAT_RETRY_EXHAUSTED)\b"),
    re.compile(r"assert\w+\(\s*b?['\"]?40[0-9]"),
    # the checker under test handed back a list of problems
    re.compile(r"\b(?:blockers|reasons|problems|offenders|unknown)\b|\bfound\["),
)


def proves_refusal(body):
    return any(shape.search(code_only(body)) for shape in REFUSAL_SHAPES)


COUNTS_RE = re.compile(
    r"the table carries (\d+) rows: (\d+) `blocking`, (\d+) `soft` and (\d+) `n/a`\."
    r" Of the `n/a` rows, (\d+) are `GAP:`.*?and (\d+) are `PROPERTY:`")


def stated_counts(text):
    """The row counts DESIGN.md states in prose, or None when it states none."""
    found = COUNTS_RE.search(" ".join(text.split()))
    if not found:
        return None
    keys = ("total", "blocking", "soft", "na", "gap", "property")
    return dict(zip(keys, (int(value) for value in found.groups())))


def actual_counts(entries):
    return {
        "total": len(entries),
        "blocking": sum(1 for e in entries if e["enforcement"] == "blocking"),
        "soft": sum(1 for e in entries if e["enforcement"] == "soft"),
        "na": sum(1 for e in entries if e["enforcement"] == "n/a"),
        "gap": len(rules.gaps(entries)),
        "property": sum(1 for e in entries if e["enforcement"] == "n/a"
                        and e["residual"].strip().startswith("PROPERTY:")),
    }


def check_exists(reference):
    """True when `path` or `path:symbol` resolves to something really there.

    The symbol has to be defined, not merely mentioned: a bare word match let a
    name that appears only in a docstring or a comment stand as the check that
    performs a rule.
    """
    path, _, symbol = reference.partition(":")
    source = REPO / path.strip()
    if not source.is_file():
        return False
    symbol = symbol.strip()
    if not symbol:
        return True
    name = re.escape(symbol)
    definition = re.compile(r"^\s*(?:def|class)\s+%s\b|^%s\s*[:=]" % (name, name), re.MULTILINE)
    return definition.search(source.read_text(encoding="utf-8")) is not None


def each(field_value):
    return [part.strip() for part in field_value.split(",") if part.strip()]


# The reverse sweep: code -> table, instead of table -> code. Every other
# assertion here starts from a row, so a rule could be relabelled n/a with a
# fabricated residual while the code went on denying it, and nothing would
# notice. This mapping is what the guard's own deny tables have to map onto,
# and it is bidirectional: an entry with no row fails, and a row whose entry
# has been deleted from the code fails too.
#
# The forge tool denials are built from the adapters' union, so a server rename
# cannot orphan a name. Approve is checked before merge because
# `approve_merge_request` carries both words.
GUARD_DENIAL_ROWS = {}
for _tool in ppl_guard.DENIED_TOOLS:
    if "resolve" in _tool:
        GUARD_DENIAL_ROWS[_tool] = "R-MCP-RESOLVE-THREAD"
    elif "approve" in _tool or "review" in _tool:
        GUARD_DENIAL_ROWS[_tool] = "R-MCP-APPROVE-MR"
    elif "merge" in _tool.rsplit("__", 1)[-1]:
        GUARD_DENIAL_ROWS[_tool] = "R-MCP-MERGE-MR"
    else:
        GUARD_DENIAL_ROWS[_tool] = "R-MCP-APPROVE-MR"

GUARD_COMMAND_ROWS = {
    "git add -A can sweep a live credential into a commit, add explicit paths":
        "R-GIT-ADD-ALL",
    "the stash stack is shared across worktrees, use git stash push -u -m <tag>": "R-GIT-STASH",
    "the stash stack is shared, pop and clear remove other sessions' entries, "
    "use git stash apply <sha> instead": "R-GIT-STASH",
    "a bare git stash drop takes the top entry, which is likely another session's, "
    "drop yours by the stash@{N} it is re-found at": "R-GIT-STASH",
    "force push is irreversible on a shared ref": "R-GIT-FORCE-PUSH",
    "hard reset discards worker output that has no receipt": "R-GIT-RESET-HARD",
}


def disowned(names, mapping, rows):
    """Problems where the code denies something the table does not own."""
    problems = []
    for name in names:
        rule_id = mapping.get(name)
        if rule_id is None:
            problems.append((name, "is denied in code and no row in the matrix claims it"))
        elif rule_id not in rows:
            problems.append((name, "maps to %s, which is not in the matrix" % rule_id))
        elif rows[rule_id]["enforcement"] != "blocking":
            problems.append((name, "maps to %s, which the matrix records as %s"
                             % (rule_id, rows[rule_id]["enforcement"])))
    for name in mapping:
        if name not in names:
            problems.append((name, "has a row but the code no longer denies it"))
    return problems


class TestTheScannerItself(PplHomeTestCase):
    """Synthetic input for the DESIGN scanner, so assertion 3 has a deny test.

    rule: R-DOC-RULE-IDS
    """

    def test_an_unmarked_enforcement_sentence_is_reported(self):
        # rule: R-DOC-RULE-IDS
        found = unmapped("The gate refuses a stale verdict.", {"R-KNOWN"})
        self.assertEqual(len(found), 1)
        self.assertIn("names no rule id", found[0][2])

    def test_a_marked_sentence_is_accepted(self):
        self.assertEqual(unmapped("The gate refuses a stale verdict [R-KNOWN].", {"R-KNOWN"}), [])

    def test_a_marker_naming_a_row_that_does_not_exist_is_reported(self):
        # rule: R-DOC-RULE-IDS
        found = unmapped("The gate refuses a stale verdict [R-INVENTED].", {"R-KNOWN"})
        self.assertIn("names unknown rule R-INVENTED", found[0][2])

    def test_a_sentence_with_no_keyword_needs_no_marker(self):
        self.assertEqual(unmapped("The gate reads git rev-parse HEAD.", set()), [])

    def test_a_claim_citing_an_na_row_must_disclose_it(self):
        # rule: R-DOC-RULE-IDS
        # Without the disclosure a claim can cite a row that admits the thing
        # was never built and still read as covered.
        rows = {"R-HOLE": {"enforcement": "n/a"}}
        found = unmapped("The worker cannot reach the diff [R-HOLE].", rows)
        self.assertIn("write it [R-HOLE n/a]", found[0][2])
        self.assertEqual(unmapped("The worker cannot reach the diff [R-HOLE n/a].", rows), [])

    def test_a_disclosure_on_an_enforced_row_is_reported(self):
        # rule: R-DOC-RULE-IDS
        rows = {"R-REAL": {"enforcement": "blocking"}}
        found = unmapped("The gate refuses a stale verdict [R-REAL n/a].", rows)
        self.assertIn("cites R-REAL as n/a, but that row is blocking", found[0][2])

    def test_a_marker_outside_a_claim_sentence_is_still_resolved(self):
        # rule: R-DOC-RULE-IDS
        # A table cell or an ordinary sentence carries markers too, and until
        # dangling() existed nothing checked them at all.
        text = "| a rule [R-GONE] | a reason | global |\n"
        self.assertEqual(unmapped(text, {"R-KNOWN"}), [])
        self.assertEqual(dangling(text, {"R-KNOWN"})[0][1], "names unknown rule R-GONE")

    def test_a_deny_test_body_must_assert_a_refusal(self):
        # rule: R-DOC-RULE-IDS
        # The rule id is a comment, and a comment asserts nothing.
        self.assertFalse(proves_refusal("        # rule: R-X\n        self.assertTrue(True)"))
        self.assertTrue(proves_refusal("        with self.assertRaises(PplError):\n pass"))

    def test_a_refusal_word_in_a_comment_does_not_count_as_proof(self):
        # rule: R-DOC-RULE-IDS
        # The exact bait that reopened this hole once: the shapes matched a
        # word in prose, so a test asserting nothing passed for a rule that
        # exists to keep credentials out of commits.
        bait = ("def test_x(self):\n"
                "    # rule: R-GIT-ADD-ALL\n"
                "    # for unknown reasons this always passes without denying anything\n"
                "    self.assertTrue(True)\n")
        self.assertTrue(mentions(bait, "R-GIT-ADD-ALL"))
        self.assertFalse(proves_refusal(bait))

    def test_a_refusal_word_in_a_docstring_does_not_count_either(self):
        # rule: R-DOC-RULE-IDS
        bait = ('def test_x(self):\n'
                '    """This one reports the problems it found[0] and the reasons."""\n'
                '    self.assertTrue(True)\n')
        self.assertFalse(proves_refusal(bait))

    def test_the_same_word_in_code_is_still_proof(self):
        # rule: R-DOC-RULE-IDS
        real = ("def test_x(self):\n"
                "    problems = check(thing)\n"
                "    self.assertEqual(problems, [])\n")
        self.assertTrue(proves_refusal(real))

    def test_code_only_keeps_a_string_literal_a_test_asserts_on(self):
        # A closed-vocabulary code lives inside a string, so stripping every
        # string rather than only docstrings would take the proof with it.
        body = 'def test_x(self):\n    self.assertIn("BOUNDARY_VIOLATION", reason)\n'
        self.assertIn("BOUNDARY_VIOLATION", code_only(body))

    def test_code_only_survives_a_body_that_does_not_parse(self):
        self.assertNotIn("rule: R-X", code_only("        # rule: R-X\n      if oops(:\n"))

    def test_a_marker_on_a_neighbouring_sentence_does_not_cover_this_one(self):
        text = "Something else [R-KNOWN]. The gate refuses a stale verdict."
        self.assertEqual(len(unmapped(text, {"R-KNOWN"})), 1)

    def test_every_keyword_is_detected(self):
        for word in rules.KEYWORDS:
            with self.subTest(word=word):
                # A "..." placeholder stands for "later in the same sentence" -
                # a literal "The gate only ... may a thing." would hand the real
                # sentence splitter three periods to cut on, severing "only"
                # from "may" before the keyword pattern ever sees them, so the
                # dummy sentence for this one entry is written out instead of
                # generated by substitution.
                sentence = ("The gate only lets one role act, and it may do so."
                            if "..." in word else "The gate %s a thing." % word)
                self.assertEqual(len(unmapped(sentence, set())), 1)

    def test_a_keyword_inside_a_fenced_block_is_not_a_claim(self):
        self.assertEqual(unmapped("```\nthis cannot happen\n```\n", set()), [])

    def test_a_heading_carrying_a_keyword_is_not_a_claim(self):
        # "## When something stops" is a section title, not a sentence about
        # the code - caught the moment "stops" widened KEYWORDS.
        self.assertEqual(unmapped("## When something stops\n", set()), [])

    def test_only_hyphenated_into_read_only_does_not_match_the_only_may_pattern(self):
        # `\b` already sits on both sides of "only" in "read-only" (a hyphen
        # is a non-word character), so a role-table description like this one
        # would otherwise read as an "only ... may" claim with no rule id.
        text = "ppl-briefer (opus/high, read-only, may call context-explorer) compiles."
        self.assertEqual(unmapped(text, set()), [])

    def test_a_real_only_may_construction_two_words_apart_is_still_caught(self):
        found = unmapped("Only that role may write those paths.", set())
        self.assertEqual(len(found), 1)

    def test_a_table_row_is_one_unit(self):
        found = unmapped("| a rule | it is denied | global |", {"R-KNOWN"})
        self.assertEqual(len(found), 1)
        self.assertEqual(unmapped("| a rule | it is denied [R-KNOWN] | global |", {"R-KNOWN"}), [])

    def test_a_table_header_naming_a_column_is_not_a_claim(self):
        header = "| Rule | Reason | Denied |\n|---|---|---|\n| a | b | c |\n"
        self.assertEqual(unmapped(header, set()), [])

    def test_an_abbreviation_does_not_split_a_sentence_away_from_its_marker(self):
        text = "A write outside the tree, e.g. in another project, cannot be denied [R-KNOWN]."
        self.assertEqual(unmapped(text, {"R-KNOWN"}), [])

    def test_a_locator_that_names_no_such_test_resolves_to_nothing(self):
        self.assertIsNone(test_body("tests/test_rule_matrix.py::TestTheScannerItself::test_absent"))

    def test_a_locator_that_names_a_real_test_returns_its_body(self):
        body = test_body("tests/test_rule_matrix.py::TestTheScannerItself::test_every_keyword_is_detected")
        self.assertIn("rules.KEYWORDS", body)

    def test_a_longer_rule_id_does_not_stand_in_for_its_prefix(self):
        # rule: R-DOC-RULE-IDS
        self.assertTrue(mentions("# rule: R-BUDGET-USD", "R-BUDGET-USD"))
        self.assertFalse(mentions("# rule: R-BUDGET-USD-BEFORE-DISPATCH", "R-BUDGET-USD"))

    def test_counts_stated_in_prose_are_parsed(self):
        text = ("As of this commit the table carries 3 rows: 1 `blocking`, 1 `soft` and 1 "
                "`n/a`. Of the `n/a` rows, 1 are `GAP:` - a hole - and 0 are `PROPERTY:`.")
        self.assertEqual(stated_counts(text)["total"], 3)
        self.assertEqual(stated_counts(text)["gap"], 1)

    def test_prose_stating_no_counts_at_all_parses_to_nothing(self):
        self.assertIsNone(stated_counts("The table carries some rows."))

    def test_a_denial_the_matrix_disowns_is_reported(self):
        # rule: R-MATRIX-GUARD-SELF-DESCRIBING
        # Attack D in miniature: the code still denies it, the row says n/a.
        rows = {"R-X": {"enforcement": "n/a"}}
        found = disowned(["some_tool"], {"some_tool": "R-X"}, rows)
        self.assertIn("the matrix records as n/a", found[0][1])

    def test_a_denial_with_no_row_at_all_is_reported(self):
        # rule: R-MATRIX-GUARD-SELF-DESCRIBING
        found = disowned(["some_tool"], {}, {})
        self.assertIn("no row in the matrix claims it", found[0][1])

    def test_a_row_whose_denial_left_the_code_is_reported(self):
        # rule: R-MATRIX-GUARD-SELF-DESCRIBING
        found = disowned([], {"gone_tool": "R-X"}, {"R-X": {"enforcement": "blocking"}})
        self.assertIn("the code no longer denies it", found[0][1])

    def test_a_check_reference_to_a_missing_file_does_not_resolve(self):
        self.assertFalse(check_exists("ppl/not_a_module.py"))

    def test_a_check_reference_to_a_missing_symbol_does_not_resolve(self):
        self.assertFalse(check_exists("ppl/rules.py:no_such_function"))

    def test_a_check_reference_to_a_real_symbol_resolves(self):
        self.assertTrue(check_exists("ppl/rules.py:validate"))


class TestTheRuleMatrix(PplHomeTestCase):
    """The three assertions this cycle exists for, against the real table."""

    def setUp(self):
        super().setUp()
        self.rows = rules.load()
        self.ids = rules.by_id(self.rows)

    def test_every_blocking_row_names_a_deny_test_that_knows_the_rule(self):
        # Assertion 1. A row pointing at a test that does not exist, or at a
        # test that does not know it is enforcing this rule, is the same
        # family of lie as a document claiming enforcement nothing performs.
        for entry in self.rows:
            if entry["enforcement"] != "blocking":
                continue
            for locator in each(entry["deny_test"]):
                with self.subTest(rule=entry["id"], test=locator):
                    body = test_body(locator)
                    self.assertIsNotNone(body, "%s names a deny_test that does not exist: %s"
                                         % (entry["id"], locator))
                    self.assertTrue(mentions(body, entry["id"]),
                                    "%s is not named in the body of %s, so that test does not "
                                    "know which rule it proves" % (entry["id"], locator))
                    self.assertTrue(proves_refusal(body),
                                    "%s names %s as its deny test, but that body asserts no "
                                    "refusal - the rule id in it is a comment, and a comment "
                                    "proves nothing" % (entry["id"], locator))

    def test_every_na_row_carries_an_honest_residual(self):
        # Assertion 2. Enforced by rules.validate on load, re-asserted here
        # because it is one of the three this task is for.
        for entry in self.rows:
            if entry["enforcement"] != "n/a":
                continue
            with self.subTest(rule=entry["id"]):
                self.assertTrue(entry["residual"].strip().startswith(rules.RESIDUAL_KINDS))
                self.assertGreater(len(entry["residual"].split()), 3,
                                   "%s residual says too little to be read as honest"
                                   % entry["id"])

    def test_every_enforcement_sentence_in_the_documents_names_a_rule(self):
        # Assertion 3. This is the one that would have caught the cycle-1
        # dispatch-cap claim mechanically. SKILL.md is scanned too, and matters
        # more than DESIGN.md: the orchestrator reads it at runtime, where the
        # cycle-1 Jira-gate claim ("enforced, not just a convention") lived.
        problems = unmapped_claims(SCANNED_DOCS, self.ids)
        self.assertEqual(problems, [], "\n".join(problems))

    def test_every_marker_in_the_documents_resolves_to_a_row(self):
        # Assertion 3 only resolves markers inside a claim sentence, so a
        # marker in a table cell survived the deletion of its row with the
        # suite green. Every marker, wherever it sits, is resolved here.
        problems = []
        for label, text in SCANNED_DOCS:
            problems += ["%s: %s" % (label, problem) for _, problem in dangling(text, self.ids)]
        self.assertEqual(problems, [], "\n".join(problems))

    def test_both_example_skills_are_scanned_file_by_file(self):
        labels = {label for label, _ in SCANNED_DOCS}
        for name in ("example-maven", "example-generic", "github-issues"):
            for path in ("SKILL.md", "references/review-loop.md", "references/escalation.md",
                         "references/gates.md", "references/brief.md", "references/verdict.md",
                         "references/guard-hook.md", *run.STAGE_REFERENCES.values()):
                self.assertIn("%s %s" % (name, path), labels)

    def test_an_unmarked_claim_a_forge_fragment_brings_into_a_reference_is_reported(self):
        # rule: R-DOC-RULE-IDS
        # The fixture skill carries the old review-loop wording in the fragment the
        # rendered references/review-loop.md comes from.
        from ppl import forges
        seeded = forges.get("gitlab").fragment("review-loop") + "\n**Never resolve the thread.**\n"
        docs = skill_docs("fixture", fixtures.skill_files("example-maven",
                                                          fragments={"review-loop": seeded}))
        problems = unmapped_claims(docs, self.ids)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("fixture references/review-loop.md:", problems[0])
        self.assertIn("names no rule id -- Never resolve the thread.", problems[0])

    def test_every_enforced_row_names_a_check_that_exists(self):
        # Assertion 4. Assertion 3 only fires on sentences carrying one of the
        # keywords, and a claim can be written without any of them - "ppl
        # apply-patch validates every touched path" carries none. This is the
        # side that catches a row naming code that was never written.
        for entry in self.rows:
            if entry["enforcement"] == "n/a":
                continue
            for reference in each(entry["hook_or_check"]):
                with self.subTest(rule=entry["id"], check=reference):
                    self.assertTrue(check_exists(reference),
                                    "%s names a check that does not exist: %s"
                                    % (entry["id"], reference))

    def test_every_named_allow_test_exists(self):
        for entry in self.rows:
            for locator in each(entry["allow_test"]):
                with self.subTest(rule=entry["id"], test=locator):
                    self.assertIsNotNone(test_body(locator),
                                         "%s names an allow_test that does not exist: %s"
                                         % (entry["id"], locator))

    def test_the_counts_design_states_match_the_table(self):
        # A paragraph of hand-written numbers about the table is exactly the
        # kind of prose that drifts from what it describes - one row flipped
        # from n/a to blocking in a later task and the sentence is quietly
        # wrong, in the document whose drift this module exists to end.
        text = (REPO / "docs" / "DESIGN.md").read_text(encoding="utf-8")
        stated = stated_counts(text)
        self.assertIsNotNone(stated, "DESIGN.md states no row counts for the rule matrix")
        self.assertEqual(stated, actual_counts(self.rows))

    def test_every_guard_denial_is_owned_by_a_blocking_row(self):
        # rule: R-MATRIX-GUARD-SELF-DESCRIBING
        # The one direction every other assertion misses: code -> table. A rule
        # cannot be disowned in the matrix while the cage still denies it.
        problems = disowned(list(ppl_guard.DENIED_TOOLS), GUARD_DENIAL_ROWS, self.ids)
        problems += disowned([reason for _, reason in ppl_guard.DENIED_COMMANDS],
                             GUARD_COMMAND_ROWS, self.ids)
        problems += disowned(list(ppl_guard.JIRA_WRITE_TOOLS),
                             {tool: "R-JIRA-GATE" for tool in ppl_guard.JIRA_WRITE_TOOLS},
                             self.ids)
        self.assertEqual(problems, [], "\n".join("%s %s" % pair for pair in problems))

    def test_each_forge_tool_denial_maps_to_the_row_its_verb_names(self):
        expected = {
            "mcp__GitLab-MCP__resolve_merge_request_thread": "R-MCP-RESOLVE-THREAD",
            "mcp__GitLab-MCP__approve_merge_request": "R-MCP-APPROVE-MR",
            "mcp__GitLab-MCP__merge_merge_request": "R-MCP-MERGE-MR",
            "mcp__github__merge_pull_request": "R-MCP-MERGE-MR",
            "mcp__github__create_and_submit_pull_request_review": "R-MCP-APPROVE-MR",
            "mcp__github__submit_pending_pull_request_review": "R-MCP-APPROVE-MR",
        }
        self.assertEqual(GUARD_DENIAL_ROWS, expected)

    def test_the_bash_write_shape_denial_is_owned_by_a_blocking_row(self):
        # rule: R-MATRIX-GUARD-SELF-DESCRIBING
        self.assertTrue(ppl_guard.BASH_WRITE_SHAPES.pattern)
        self.assertEqual(self.ids["R-RUNS-BASH-WRITE"]["enforcement"], "blocking")

    def test_the_test_worker_read_deny_tables_are_owned_by_blocking_rows(self):
        # rule: R-MATRIX-GUARD-SELF-DESCRIBING
        # Task 8 added an exact structured-tool check (READ_TOOLS) and a
        # lexical Bash allowlist (round 1 review rewrote the original
        # blocklist) - the same self-description guarantee this test's
        # siblings give the three older deny tables, so a later relabelling
        # of either row cannot go quiet.
        self.assertTrue(ppl_guard._SEGMENT_SEPARATOR_RE.pattern)
        self.assertTrue(ppl_guard.READ_TOOLS)
        self.assertEqual(self.ids["R-TEST-WORKER-BASH-READS"]["enforcement"], "blocking")
        self.assertEqual(self.ids["R-TEST-WORKER-READ-TOOLS"]["enforcement"], "blocking")

    def test_the_table_is_ascii_without_tabs_or_crlf(self):
        raw = (REPO / "rules.toml").read_bytes()
        self.assertEqual(raw, raw.decode("ascii", "ignore").encode("ascii"))
        self.assertNotIn(b"\t", raw)
        self.assertNotIn(b"\r", raw)


class TestLoad(PplHomeTestCase):
    def test_a_missing_file_is_a_routable_error(self):
        with self.assertRaises(PplError) as caught:
            rules.load(Path(os.environ["PPL_HOME"]) / "absent.toml")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_malformed_toml_is_a_routable_error(self):
        path = Path(os.environ["PPL_HOME"]) / "broken.toml"
        path.write_text("[[rule]\nid = ", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            rules.load(path)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_table_that_is_not_utf8_is_a_routable_error(self):
        path = Path(os.environ["PPL_HOME"]) / "latin1.toml"
        path.write_bytes(b'[[rule]]\nid = "R-X"\nstatement = "caf\xe9"\n')
        with self.assertRaises(PplError) as caught:
            rules.load(path)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_the_real_table_loads(self):
        self.assertTrue(rules.load())

    def test_the_default_table_follows_ppl_root(self):
        real = rules.load()
        checkout = Path(tmphome.mkdtemp(prefix="pplroot-"))
        (checkout / "defaults").mkdir()
        head, last = (REPO / "rules.toml").read_text(encoding="utf-8").rsplit("[[rule]]", 1)
        (checkout / "rules.toml").write_text(head, encoding="utf-8")
        with mock.patch.dict(os.environ, {"PPL_ROOT": str(checkout)}):
            self.assertEqual(rules.default_path(), checkout / "rules.toml")
            self.assertEqual(len(rules.load()), len(real) - 1)


if __name__ == "__main__":
    unittest.main()

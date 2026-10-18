import argparse
import os
import re
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ.setdefault("PPL_HOME", tmphome.mkdtemp(prefix="pplhome-"))

import fixtures
from ppl import cli

# The skill on disk is rendered from skill/ skeletons, so the contract is
# checked on a rendered copy, never on the real home.
RENDERED_DIR = Path(tmphome.mkdtemp(prefix="pplskill-"))
SKILL = fixtures.render_skill_to(RENDERED_DIR, "example-maven") / "SKILL.md"

WORD = r"[a-z][a-z-]*"


def _positionals(subparser):
    """Positional actions of a subparser, in declaration order.

    An optional (--flag) has non-empty option_strings. A positional has none.
    The automatically added -h/--help is an optional too, so it is excluded
    for free, not filtered specially.
    """
    return [a for a in subparser._actions if not a.option_strings]


def _subcommand_map():
    """command name -> its subparser, straight from the real argparse tree.

    Reading this off build_parser() instead of hand-listing choices means a
    renamed or added subcommand changes this map for free the next time the
    test runs, the same guarantee the brief asks for on top-level commands.
    """
    parser = cli.build_parser()
    sub_action = next(a for a in parser._actions
                      if isinstance(a, argparse._SubParsersAction))
    return sub_action.choices


class TestSkillContract(unittest.TestCase):
    skill = SKILL

    def setUp(self):
        self.assertTrue(self.skill.exists(), "orchestrator skill is missing")
        self.text = self.skill.read_text(encoding="utf-8")

    def test_every_ppl_command_it_names_exists(self):
        named = set(re.findall(r"\bppl (%s)" % WORD, self.text))
        unknown = named - set(cli.COMMANDS)
        self.assertEqual(unknown, set(), "skill references commands that do not exist")

    def test_it_forbids_resolving_threads(self):
        # cycle 2 wave C: "never resolve" was reworded to "do not resolve" -
        # "never" is now a rule-matrix claim keyword, and this sentence is
        # discipline, not something a hook enforces for the orchestrator's
        # own GitLab MCP calls in every case, so it no longer claims that.
        self.assertIn("do not resolve", self.text.lower())

    def test_it_forbids_jira_writes_before_merge(self):
        lowered = self.text.lower()
        self.assertIn("jira", lowered)
        self.assertIn("after merge", lowered)

    def test_it_names_no_tracker_field_of_one_particular_team(self):
        # the close-out note comes from the harness/tracker-close fragment, and
        # the shipped default asks for a comment only
        # one bracketed letter per phrase keeps this file out of the scrub's pattern
        self.assertIsNone(re.search(r"ai-usag[e]|progress fiel[d]", self.text, re.IGNORECASE))

    def test_it_starts_with_recovery(self):
        self.assertIn("ppl recover", self.text)

    def test_it_names_every_role(self):
        for role in ("ppl-briefer", "ppl-code-worker", "ppl-test-worker",
                     "ppl-verifier", "ppl-review-responder"):
            self.assertIn(role, self.text)

    def test_it_has_valid_frontmatter(self):
        self.assertTrue(self.text.startswith("---\n"))
        self.assertIn("name: pipeline", self.text)

    def test_it_carries_the_findings_dedupe_protocol(self):
        # rule: R-FINDINGS-DEDUPE-PROTOCOL
        # findings are no longer SHA-filtered (R-DONE-OPEN-FINDING), so the
        # orchestrator must list what is already open before dispatch rather
        # than let the verifier report the same defect again every round.
        self.assertIn("ppl findings list", self.text)
        self.assertIn("genuinely new", self.text)

    def test_every_ppl_subcommand_form_is_accepted_by_the_parser(self):
        """Every `ppl <command> <word> [<word>]` the doc shows names a subcommand
        word the parser actually defines, where the parser restricts that
        position with argparse `choices` at all.

        This walks the real subparsers instead of a hand-written table, so it
        cannot itself drift from the CLI: a renamed `stage enter` -> `stage
        begin`, or a `budget spend` kind that no longer exists, fails here
        instead of surfacing as a confused agent mid-run. Positions the CLI
        leaves free-form (a file path, a task id, a question) carry no
        `choices` and are not checked, since the parser itself accepts
        anything there.
        """
        subcommands = _subcommand_map()
        failures = []
        for match in re.finditer(r"\bppl (%s)" % WORD, self.text):
            command = match.group(1)
            if command not in subcommands:
                continue  # already reported by test_every_ppl_command_it_names_exists
            specs = _positionals(subcommands[command])
            cursor = match.end()
            for spec in specs:
                token_match = re.match(r"\s+(%s)\b" % WORD, self.text[cursor:])
                if not token_match:
                    break
                if spec.choices is None:
                    break
                token = token_match.group(1)
                if token not in spec.choices:
                    failures.append(
                        "ppl %s %s: %r is not one of %s"
                        % (command, spec.dest, token, sorted(spec.choices)))
                    break
                cursor += token_match.end()
        self.assertEqual(failures, [], "invalid subcommand forms: %s" % "; ".join(failures))

    def test_stage_seven_names_the_review_ledger_commands(self):
        # ppl review approved is the only writer of state.mr.approved_sha, so a
        # Stage 7 that never names it leaves the skill's own terminal condition
        # unreachable: ppl approval check would print "none" forever.
        for command in ("references/review-loop.md", "ppl mr set", "--author",
                        "ppl review pending", "ppl review answered",
                        "ppl review approved --sha"):
            self.assertIn(command, self.text)
        stage7 = self.text.split("## Stage 7 - review", 1)[1].split("## After merge only", 1)[0]
        self.assertLess(stage7.index("ppl review approved"), stage7.index("ppl transition done"),
                        "the approval must be recorded before the run is closed")

    def _after_merge(self):
        return " ".join(self.text.split("## After merge only", 1)[1]
                        .split("## Operator commands", 1)[0].split())

    def test_after_merge_records_the_merge_before_the_tracker_note(self):
        section = self._after_merge()
        self.assertIn("ppl merge observed --sha", section)
        self.assertLess(section.index("ppl merge observed"), section.index("write to the tracker"),
                        "the tracker gate opens only once the merge is recorded")

    def test_after_merge_recovers_from_review_through_closure(self):
        # A run still in review cannot enter merge_watch, the stage graph allows it only
        # from closure, and transition done is refused outside closure.
        section = self._after_merge()
        start = section.index("from `review`")
        order = [section.index(step, start) for step in
                 ("ppl stage enter closure", "ppl transition done", "ppl stage enter merge_watch")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("go back and run `ppl stage enter merge_watch` first", section)


class TestBothForgesRender(unittest.TestCase):
    def test_generic_github_skill_satisfies_the_same_contract(self):
        directory = Path(tmphome.mkdtemp(prefix="pplskill-"))
        skill = fixtures.render_skill_to(directory, "example-generic") / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        self.assertIn("ppl recover", text)
        self.assertIn("do not resolve", text.lower())
        self.assertNotIn("<!-- include:", text)
        for role in ("ppl-briefer", "ppl-code-worker", "ppl-test-worker", "ppl-verifier",
                     "ppl-review-responder"):
            self.assertIn(role, text)


class TestGenericSkillContract(TestSkillContract):
    """The same contract on the skill rendered for the GitHub harness, whose
    Stage 6 and 7 come from other fragments."""
    skill = fixtures.render_skill_to(Path(tmphome.mkdtemp(prefix="pplskill-")),
                                     "example-generic") / "SKILL.md"


if __name__ == "__main__":
    unittest.main()

import argparse
import io
import itertools
import os
import re
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ.setdefault("PPL_HOME", tmphome.mkdtemp(prefix="pplhome-"))

import fixtures
from ppl import cli, forges, fsm, gitfacts, run, sequences, verdict

# The skill on disk is rendered from skill/ skeletons, so the contract is
# checked on a rendered copy, never on the real home.
RENDERED_DIR = Path(tmphome.mkdtemp(prefix="pplskill-"))
SKILL = fixtures.render_skill_to(RENDERED_DIR, "example-maven") / "SKILL.md"
# Every file of both example skills, the forge fragments as they are included. The
# orchestrator reads references/* on demand, so a drift there misleads it as much.
EXAMPLES = ("example-maven", "example-generic")
RENDERED = {name: fixtures.skill_files(name) for name in EXAMPLES}

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


def unknown_commands(files):
    problems = []
    for path, text in sorted(files.items()):
        for command in sorted(set(re.findall(r"\bppl (%s)" % WORD, text)) - set(cli.COMMANDS)):
            problems.append("%s: `ppl %s` is not a ppl command" % (path, command))
    return problems


def invalid_forms(files):
    """Every `ppl <command> <word> [<word>]` names a subcommand word the parser defines,
    where the parser restricts that position with argparse `choices` at all.

    This walks the real subparsers instead of a hand-written table, so it cannot itself
    drift from the CLI: a renamed `stage enter` -> `stage begin`, or a `budget spend` kind
    that no longer exists, fails here instead of surfacing as a confused agent mid-run.
    Positions the CLI leaves free-form (a file path, a task id, a question) carry no
    `choices` and are not checked, since the parser itself accepts anything there.
    """
    subcommands = _subcommand_map()
    problems = []
    for path, text in sorted(files.items()):
        for match in re.finditer(r"\bppl (%s)" % WORD, text):
            command = match.group(1)
            if command not in subcommands:
                continue  # unknown_commands reports it
            cursor = match.end()
            for spec in _positionals(subcommands[command]):
                token_match = re.match(r"\s+(%s)\b" % WORD, text[cursor:])
                if not token_match or spec.choices is None:
                    break
                token = token_match.group(1)
                if token not in spec.choices:
                    problems.append("%s: ppl %s %s: %r is not one of %s"
                                    % (path, command, spec.dest, token, sorted(spec.choices)))
                    break
                cursor += token_match.end()
    return problems


def unknown_stages(files):
    """`ppl stage enter <s>` naming a stage fsm.STAGES does not have. invalid_forms reads
    words of letters and hyphens, so it would pass a misspelled `merge_wach` by."""
    problems = []
    for path, text in sorted(files.items()):
        for stage in re.findall(r"\bppl stage enter\s+([a-z_]+)", text):
            if stage not in fsm.STAGES:
                problems.append("%s: `ppl stage enter %s` names no stage" % (path, stage))
    return problems


# "from `review`, run `a`, `b`, then `c`", up to the end of the clause or the sentence
_RECOVERY_RE = re.compile(r"\b(?:from|in) `(%s)`,? run (.+?)(?=;|\.\s|\.$|$)"
                          % "|".join(fsm.STAGES), re.IGNORECASE)


def recovery_clauses(text):
    """(stage, [ppl command, ...]) for every "from `<stage>`, run ..." instruction."""
    found = []
    for match in _RECOVERY_RE.finditer(" ".join(text.split())):
        steps = re.findall(r"`(ppl [^`]+)`", match.group(2))
        if steps:
            found.append((match.group(1), steps))
    return found


def illegal_recoveries(files):
    problems = []
    for path, text in sorted(files.items()):
        for stage, steps in recovery_clauses(text):
            # merge_watch is entered only once the run is done [R-MERGE-WATCH-AFTER-DONE]
            refused = walk(steps, stage, "done" if stage == "merge_watch" else "in_progress")
            if refused:
                problems.append("%s: from `%s`: %s" % (path, stage, refused))
    return problems


WALK_SHA = "0123456789abcdef0123456789abcdef01234567"
_walks = itertools.count(1)


def _argv(step, task_id):
    def fill(match):
        name = match.group(1)
        if "sha" in name.lower():
            return WALK_SHA
        return task_id if name == "TASK-ID" else "x"
    return shlex.split(re.sub(r"<([^<>]*)>", fill, step))[1:]


def walk(steps, stage, status):
    """None when the real ppl accepts every step in order on a run that is in `stage` with
    lifecycle `status`, else the first refusal. The stage graph and every stage and status
    precondition are the real ones. Only git and the done gate's evidence checks are
    stubbed: what a step needs from the repo is another test's subject."""
    task_id = "WALK-%d" % next(_walks)
    with mock.patch.dict(os.environ, {"PPL_HOME": tmphome.mkdtemp(prefix="pplwalk-")}), \
            mock.patch.object(gitfacts, "head_sha", return_value=WALK_SHA), \
            mock.patch.object(gitfacts, "changed_paths", return_value=[]), \
            mock.patch.object(verdict, "done_blockers", return_value=[]):
        state = run.init(task_id, "u", "/nonexistent/walk-repo")
        state["stage"], state["status"] = stage, status
        run.save(state)
        for step in steps:
            before = run.load(task_id)
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.main(_argv(step, task_id))
            if code != 0:
                return "`%s` in stage %s, status %s: %s" % (step, before["stage"], before["status"],
                                                            err.getvalue().strip())
    return None


class TestSkillContract(unittest.TestCase):
    skill = SKILL

    def setUp(self):
        self.assertTrue(self.skill.exists(), "orchestrator skill is missing")
        self.text = self.skill.read_text(encoding="utf-8")

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

    def test_each_role_output_command_names_whose_output_it_records(self):
        joined = re.sub(r"\\\n\s*", " ", self.text)
        for command, role in (("ppl brief set", "ppl-briefer"),
                              ("ppl brief challenge", "ppl-brief-challenger"),
                              ("ppl verdict record", "ppl-verifier"),
                              ("ppl findings add", "ppl-verifier"),
                              ("ppl evidence add", "orchestrator")):
            self.assertRegex(joined, re.escape(command) + r" [^`]*?--from\s+" + re.escape(role))

    def test_it_sends_a_refusal_to_the_one_plus_one_rule(self):
        self.assertIn("FORMAT_RETRY_EXHAUSTED", self.text)
        self.assertNotIn("record it as `REVISE`", self.text)


class TestEscalationReference(unittest.TestCase):
    text = (SKILL.parent / "references" / "escalation.md").read_text(encoding="utf-8")

    def test_its_table_names_exactly_the_park_blocker_codes(self):
        from ppl import run
        codes = set()
        for line in self.text.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if line.startswith("| ") and re.fullmatch(r"[A-Z_]+", cells[0]):
                codes.add(cells[0])
        self.assertEqual(codes, set(run.BLOCKER_CODES))

    def test_it_states_the_one_plus_one_rule_once(self):
        flat = " ".join(self.text.split())
        self.assertEqual(flat.count("redispatch the same role once"), 1)
        self.assertEqual(flat.count("`ppl` refused your output:"), 1)
        for stale in ("three format failures", "retry once"):
            self.assertNotIn(stale, flat)
        self.assertIn("FORMAT_RETRY_EXHAUSTED", flat)


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


class TestEveryRenderedFile(unittest.TestCase):
    """The checks below read SKILL.md, every references/* file and the forge fragments as
    included, for both example skills. Reading SKILL.md alone let a review loop that skipped
    closure survive in references/review-loop.md."""

    def test_both_skills_carry_their_references(self):
        for name, files in RENDERED.items():
            for path in ("SKILL.md", "references/review-loop.md", "references/escalation.md",
                         "references/gates.md"):
                self.assertIn(path, files, name)

    def test_every_ppl_command_any_file_names_exists(self):
        for name, files in RENDERED.items():
            self.assertEqual(unknown_commands(files), [], name)

    def test_every_ppl_subcommand_form_is_accepted_by_the_parser(self):
        for name, files in RENDERED.items():
            self.assertEqual(invalid_forms(files), [], name)

    def test_every_stage_enter_names_a_stage_of_the_state_machine(self):
        for name, files in RENDERED.items():
            self.assertEqual(unknown_stages(files), [], name)

    def test_every_recovery_instruction_is_legal_from_the_stage_it_names(self):
        for name, files in RENDERED.items():
            self.assertEqual(illegal_recoveries(files), [], name)

    def test_the_recovery_check_reads_the_after_merge_instructions(self):
        # A check that matches no clause would pass any text.
        for name, files in RENDERED.items():
            stages = [stage for _, text in sorted(files.items())
                      for stage, _ in recovery_clauses(text)]
            self.assertEqual(stages, ["review", "closure", "merge_watch"], name)

    def test_the_close_out_is_rendered_where_stage_seven_and_the_review_loop_state_it(self):
        close_out = sequences.render(sequences.CLOSE_OUT)
        for name, files in RENDERED.items():
            for path in ("SKILL.md", "references/review-loop.md"):
                self.assertIn(close_out, " ".join(files[path].split()), "%s %s" % (name, path))

    def test_the_after_merge_steps_are_rendered_where_the_skill_states_them(self):
        section = " ".join(RENDERED["example-maven"]["SKILL.md"]
                           .split("## After merge only", 1)[1].split("## Operator commands", 1)[0]
                           .split())
        self.assertEqual(section.count(sequences.render(sequences.AFTER_MERGE)), 2)
        self.assertIn(sequences.render(sequences.SEQUENCES["close-out-from-closure"][1]), section)


class TestSequences(unittest.TestCase):
    """Each sequence is walked through the real ppl from the stage and status it starts in,
    so a step the state machine or a precondition refuses fails here, not in a run."""

    def test_the_close_out_is_the_five_steps_in_their_order(self):
        self.assertEqual(sequences.CLOSE_OUT,
                         ("ppl review approved --sha <SHA>", "ppl approval check",
                          "ppl stage enter closure", "ppl transition done",
                          "ppl stage enter merge_watch"))

    def test_every_sequence_is_legal_from_its_start(self):
        for name, ((stage, status), steps) in sequences.SEQUENCES.items():
            with self.subTest(sequence=name):
                self.assertIsNone(walk(steps, stage, status))

    def test_the_close_out_leads_into_the_after_merge_steps(self):
        self.assertIsNone(walk(sequences.CLOSE_OUT + sequences.AFTER_MERGE, "review", "in_progress"))

    def test_the_walk_refuses_the_two_step_ending_the_review_loop_once_had(self):
        # rule: R-DONE-FROM-CLOSURE
        refused = walk(("ppl review approved --sha <SHA>", "ppl transition done"),
                       "review", "in_progress")
        self.assertIn("`ppl transition done` in stage review", refused)
        self.assertIn("ILLEGAL_TRANSITION", refused)

    def test_the_walk_refuses_merge_watch_straight_from_review(self):
        # rule: R-FSM-STAGE-GRAPH
        refused = walk(("ppl stage enter merge_watch",), "review", "in_progress")
        self.assertIn("ILLEGAL_TRANSITION", refused)

    def test_the_walk_follows_the_lifecycle_status_as_well_as_the_stage(self):
        # rule: R-MERGE-WATCH-AFTER-DONE
        refused = walk(("ppl stage enter closure", "ppl stage enter merge_watch"),
                       "review", "in_progress")
        self.assertIn("`ppl stage enter merge_watch` in stage closure, status in_progress", refused)
        self.assertIn("ILLEGAL_TRANSITION", refused)

    def test_the_walk_refuses_the_merge_record_outside_merge_watch(self):
        # rule: R-MERGE-OBSERVED-ONCE
        refused = walk(sequences.AFTER_MERGE, "closure", "done")
        self.assertIn("ILLEGAL_TRANSITION", refused)

    def test_the_walk_refuses_a_step_the_parser_refuses(self):
        refused = walk(("ppl merge observed",), "merge_watch", "done")
        self.assertIn("SCHEMA_INVALID", refused)


# One seed per check, in a forge fragment, so the fixture skill carries it where the real
# text comes from and the checks have to find it in the rendered references/review-loop.md.
SEEDS = {
    "unknown command": "Then run `ppl close --task <TASK-ID>`.",
    "invalid form": "Then run `ppl review approve --sha <SHA>`.",
    "unknown stage": "Then run `ppl stage enter merge_wach`.",
    "illegal recovery": "From `review`, run `ppl stage enter merge_watch` to finish.",
}
SEEDED = fixtures.skill_files("example-maven", fragments={
    "review-loop": forges.get("gitlab").fragment("review-loop") + "\n" + "\n\n".join(SEEDS.values())})


class TestSeededDrift(unittest.TestCase):
    """A fixture skill with one drift seeded per check proves each check can fail. The
    unseeded skill passes all of them (TestEveryRenderedFile)."""

    def assert_reports(self, problems):
        self.assertTrue(problems, "the check found nothing in the seeded skill")
        self.assertTrue(all(p.startswith("references/review-loop.md: ") for p in problems), problems)
        return problems

    def test_an_unknown_command_in_a_reference_is_reported(self):
        problems = self.assert_reports(unknown_commands(SEEDED))
        self.assertEqual(problems, ["references/review-loop.md: `ppl close` is not a ppl command"])

    def test_an_invalid_subcommand_in_a_reference_is_reported(self):
        problems = self.assert_reports(invalid_forms(SEEDED))
        self.assertEqual(len(problems), 1)
        self.assertIn("ppl review action: 'approve'", problems[0])

    def test_a_misspelled_stage_in_a_reference_is_reported(self):
        problems = self.assert_reports(unknown_stages(SEEDED))
        self.assertEqual(problems,
                         ["references/review-loop.md: `ppl stage enter merge_wach` names no stage"])

    def test_an_illegal_recovery_instruction_in_a_reference_is_reported(self):
        # rule: R-FSM-STAGE-GRAPH
        problems = self.assert_reports(illegal_recoveries(SEEDED))
        self.assertEqual(len(problems), 1)
        self.assertIn("from `review`: `ppl stage enter merge_watch` in stage review", problems[0])
        self.assertIn("ILLEGAL_TRANSITION", problems[0])

    def test_the_parser_refuses_the_misspelled_stage_as_well(self):
        # rule: R-FSM-STAGE-GRAPH
        refused = walk(("ppl stage enter merge_wach",), "closure", "done")
        self.assertIn("SCHEMA_INVALID", refused)
        self.assertIn("invalid choice", refused)


if __name__ == "__main__":
    unittest.main()

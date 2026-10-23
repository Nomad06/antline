import argparse
import functools
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))
import ppl_guard

import fixtures
from ppl import cli, forges, fsm, gitfacts, harness, rules, run, sequences, verdict

# The skill on disk is rendered from skill/ skeletons, so the contract is
# checked on a rendered copy, never on the real home.
RENDERED_DIR = Path(tmphome.mkdtemp(prefix="pplskill-"))
SKILL = fixtures.render_skill_to(RENDERED_DIR, "example-maven") / "SKILL.md"
# Every file of both example skills, the forge fragments as they are included. The
# orchestrator reads references/* on demand, so a drift there misleads it as much.
EXAMPLES = ("example-maven", "example-generic")
RENDERED = {name: fixtures.skill_files(name) for name in EXAMPLES}
# The generic example on a GitHub Issues tracker: its tracker steps come from the other adapter.
RENDERED["github-issues"] = fixtures.skill_files("example-generic", tables=fixtures.GITHUB_ISSUES)

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


# "from `review`, run `a`, `b`, then `c`" or "from `closure` with status `done`, run `a`",
# up to the end of the clause or the sentence
_RECOVERY_RE = re.compile(r"\b(?:from|in) `(%s)`(?: with status `(%s)`)?,? run (.+?)(?=;|\.\s|\.$|$)"
                          % ("|".join(fsm.STAGES), "|".join(fsm.LIFECYCLE)), re.IGNORECASE)


def recovery_clauses(text):
    """(stage, status or None, [ppl command, ...]) for every "from `<stage>`, run ..."
    instruction, the status when the instruction names one."""
    found = []
    for match in _RECOVERY_RE.finditer(" ".join(text.split())):
        steps = re.findall(r"`(ppl [^`]+)`", match.group(3))
        if steps:
            found.append((match.group(1), match.group(2), steps))
    return found


@functools.lru_cache(maxsize=None)
def reachable_statuses():
    """stage -> the lifecycle statuses a run can be in there, found by walking the real CLI
    forward from a new run: every stage entry it accepts and `ppl transition done`. Park and
    escalation are left out, a resume returns a run to the stage and status it left
    [R-PARK-RESUME-EXACT] [R-PARK-RESUME-RESTORES-LIFECYCLE], and so are transition blocked
    and failed, which take a run off the path a recovery instruction is written for."""
    moves = ["ppl stage enter %s" % stage for stage in fsm.STAGES
             if stage not in ("parking", "escalation")] + ["ppl transition done"]
    seen = {("admission", "planned")}
    queue = list(seen)
    while queue:
        node = queue.pop()
        for move in moves:
            refused, reached = _walk((move,), *node)
            if refused is None and reached not in seen:
                seen.add(reached)
                queue.append(reached)
    statuses = {}
    for stage, status in seen:
        statuses.setdefault(stage, set()).add(status)
    return statuses


def illegal_recoveries(files):
    """A recovery instruction is walked from every status a run can be in at its stage, or
    from the one it names. Where one names a status, every status of that stage needs one."""
    statuses = reachable_statuses()
    problems = []
    for path, text in sorted(files.items()):
        covered = {}
        for stage, status, steps in recovery_clauses(text):
            possible = statuses.get(stage, set())
            if not possible:
                problems.append("%s: from `%s`: no run reaches this stage on the forward walk, so "
                                "the instruction cannot be checked" % (path, stage))
                continue
            if status is not None and status not in possible:
                problems.append("%s: from `%s` with status `%s`: no run is in that stage with that "
                                "status" % (path, stage, status))
                continue
            starts = possible if status is None else {status}
            covered.setdefault(stage, set()).update(starts)
            for start in sorted(starts):
                refused = walk(steps, stage, start)
                if refused:
                    problems.append("%s: from `%s` (status %s): %s" % (path, stage, start, refused))
        for stage, seen in sorted(covered.items()):
            for status in sorted(statuses[stage] - seen):
                problems.append("%s: from `%s`: no instruction for status %s" % (path, stage, status))
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


def walk(steps, stage, status, blockers=()):
    """None when the real ppl accepts every step in order on a run that is in `stage` with
    lifecycle `status`, else the first refusal. The stage graph and every stage and status
    precondition are the real ones. Only git and the done gate's evidence checks are
    stubbed, and the run carries a recorded baseline and an accepted brief challenge: what a
    step needs from the repo or from an artifact is another test's subject. The evidence
    checks are verdict.done_blockers, which both `ppl transition done` and the closure entry
    check read [R-CLOSURE-ONLY-WHEN-DONE-PASSES]: they answer `blockers`, none by default,
    so a walk models a run whose evidence holds unless a test says otherwise."""
    return _walk(steps, stage, status, blockers)[0]


def _walk(steps, stage, status, blockers=()):
    """(walk's answer, the (stage, status) the run ended in)"""
    task_id = "WALK-%d" % next(_walks)
    with mock.patch.dict(os.environ, {"PPL_HOME": tmphome.mkdtemp(prefix="pplwalk-")}), \
            mock.patch.object(gitfacts, "head_sha", return_value=WALK_SHA), \
            mock.patch.object(gitfacts, "changed_paths", return_value=[]), \
            mock.patch.object(verdict, "done_blockers", return_value=list(blockers)):
        state = run.init(task_id, "u", "/nonexistent/walk-repo")
        state.update(stage=stage, status=status, base_sha=WALK_SHA, spec_revision="walk",
                     brief_challenge={"spec_revision": "walk", "verdict": "ACCEPTED"})
        run.save(state)
        for step in steps:
            before = run.load(task_id)
            err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = cli.main(_argv(step, task_id))
            if code != 0:
                return ("`%s` in stage %s, status %s: %s"
                        % (step, before["stage"], before["status"], err.getvalue().strip()),
                        (before["stage"], before["status"]))
        after = run.load(task_id)
    return None, (after["stage"], after["status"])


class TestSkillContract(unittest.TestCase):
    skill = SKILL

    def setUp(self):
        self.assertTrue(self.skill.exists(), "orchestrator skill is missing")
        self.text = self.skill.read_text(encoding="utf-8")

    def reference(self, name):
        """references/<name> of the same rendered skill, where each stage's procedure lives."""
        return (self.skill.parent / "references" / name).read_text(encoding="utf-8")

    def test_it_forbids_resolving_threads(self):
        # cycle 2 wave C: "never resolve" was reworded to "do not resolve" -
        # "never" is now a rule-matrix claim keyword, and this sentence is
        # discipline, not something a hook enforces for the orchestrator's
        # own GitLab MCP calls in every case, so it no longer claims that.
        self.assertIn("do not resolve", self.text.lower())

    def test_it_names_no_tracker_field_of_one_particular_team(self):
        # the close-out note comes from the harness/tracker-close fragment, and
        # the shipped default asks for a comment only
        # one bracketed letter per phrase keeps this file out of the scrub's pattern
        files = {path.relative_to(self.skill.parent).as_posix(): path
                 for path in self.skill.parent.rglob("*.md")}
        self.assertIn("references/stage-8-after-merge.md", files)
        for relative, path in sorted(files.items()):
            self.assertIsNone(re.search(r"ai-usag[e]|progress fiel[d]",
                                        path.read_text(encoding="utf-8"), re.IGNORECASE), relative)

    def test_it_starts_with_recovery(self):
        self.assertIn("ppl recover", self.text)

    def test_the_operator_commands_name_every_command_an_operator_halt_refuses(self):
        # the eleven of R-OPERATOR-HALT-CLI, each a control.enforce call site in ppl/cli.py
        section = self.text[self.text.index("## Operator commands"):self.text.index("## Hard rules")]
        section = " ".join(section.split())
        for command in ("stage enter", "brief set", "baseline record", "boundary set", "mr set",
                        "merge observed", "review answered", "review approved", "budget spend",
                        "transition", "recover"):
            self.assertIn("`%s`" % command, section)

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
        stage5 = self.reference("stage-5-verification.md")
        self.assertIn("ppl findings list", stage5)
        self.assertIn("genuinely new", stage5)

    def test_stage_seven_names_the_review_ledger_commands(self):
        # ppl review approved is the only writer of state.mr.approved_sha, so a
        # Stage 7 that never names it leaves the skill's own terminal condition
        # unreachable: ppl approval check would print "none" forever.
        stage7 = self.reference("stage-7-review.md")
        for command in ("references/review-loop.md", "ppl mr set", "--author",
                        "ppl review pending", "ppl review answered",
                        "ppl review approved --sha"):
            self.assertIn(command, stage7)
        self.assertLess(stage7.index("ppl review approved"), stage7.index("ppl transition done"),
                        "the approval must be recorded before the run is closed")

    def _after_merge(self):
        return " ".join(self.reference("stage-8-after-merge.md").split())

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
        self.assertIn("--from <role>", self.text)
        for stage, command, role in (("stage-2-briefing.md", "ppl brief set", "ppl-briefer"),
                                     ("stage-2-briefing.md", "ppl brief challenge",
                                      "ppl-brief-challenger"),
                                     ("stage-5-verification.md", "ppl verdict record",
                                      "ppl-verifier"),
                                     ("stage-5-verification.md", "ppl findings add", "ppl-verifier"),
                                     ("stage-4-gates.md", "ppl evidence add", "orchestrator")):
            joined = re.sub(r"\\\n\s*", " ", self.reference(stage))
            self.assertRegex(joined, re.escape(command) + r" [^`]*?--from\s+" + re.escape(role))

    def test_it_sends_a_refusal_to_the_one_plus_one_rule(self):
        self.assertIn("FORMAT_RETRY_EXHAUSTED", self.text)
        self.assertNotIn("record it as `REVISE`", self.text)
        self.assertNotIn("record it as `REVISE`", self.reference("stage-2-briefing.md"))

    def test_the_gate_receipt_example_takes_its_result_from_the_rc_file(self):
        # a literal `--result pass` in the example is copied as is, whatever the gate returned
        stage4 = self.reference("stage-4-gates.md")
        example = stage4[stage4.index("```bash"):stage4.index("```", stage4.index("```bash") + 3)]
        self.assertIn("ppl evidence add", example)
        self.assertNotIn("--result pass", example)
        self.assertIn("from the rc file", " ".join(stage4.split()))

    def test_a_refused_receipt_of_your_own_is_corrected_from_the_rc_file_not_redispatched(self):
        rule = self.reference("escalation.md")
        rule = " ".join(rule[rule.index("## The role output rule"):].split())
        sentence = next(s for s in re.split(r"(?<=\.)\s", rule) if "--from orchestrator" in s)
        for needed in ("correct the receipt from the rc file", "run the command again",
                       "counts toward the cap"):
            self.assertIn(needed, sentence)


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

    def row(self, code):
        return next(line for line in self.text.splitlines() if line.startswith("| %s " % code))

    def test_leaving_baseline_without_one_is_routed_as_the_illegal_transition_it_raises(self):
        # ppl/run.py:stage_enter raises ILLEGAL_TRANSITION for it, not SCHEMA_INVALID
        self.assertNotIn("baseline", self.row("SCHEMA_INVALID"))
        illegal = self.row("ILLEGAL_TRANSITION")
        self.assertIn("leaving the baseline stage", illegal)
        self.assertIn("`ppl baseline record`", illegal)
        self.assertIn("[R-BASELINE-RECORDED-BEFORE-LEAVING]", illegal)

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

    def test_no_line_of_prose_is_a_lone_connective_left_by_a_rewrap(self):
        stranded = ["%s %s:%d" % (name, path, number)
                    for name, files in RENDERED.items() for path, text in sorted(files.items())
                    for number, line in enumerate(text.splitlines(), start=1)
                    if line.strip() in ("and", "or", "but", "then")]
        self.assertEqual(stranded, [])

    def test_every_recovery_instruction_is_legal_from_the_stage_it_names(self):
        for name, files in RENDERED.items():
            self.assertEqual(illegal_recoveries(files), [], name)

    def test_the_recovery_check_reads_the_after_merge_instructions(self):
        # A check that matches no clause would pass any text.
        for name, files in RENDERED.items():
            clauses = [(stage, status) for _, text in sorted(files.items())
                       for stage, status, _ in recovery_clauses(text)]
            self.assertEqual(clauses, [("review", None), ("closure", "in_progress"),
                                       ("closure", "done"), ("merge_watch", None)], name)

    def test_the_close_out_is_rendered_where_stage_seven_and_the_review_loop_state_it(self):
        close_out = sequences.render(sequences.CLOSE_OUT)
        for name, files in RENDERED.items():
            for path in ("references/stage-7-review.md", "references/review-loop.md"):
                self.assertIn(close_out, " ".join(files[path].split()), "%s %s" % (name, path))

    def test_the_after_merge_steps_are_rendered_where_the_skill_states_them(self):
        section = " ".join(RENDERED["example-maven"]["references/stage-8-after-merge.md"].split())
        self.assertEqual(section.count(sequences.render(sequences.AFTER_MERGE)), 2)
        self.assertIn(sequences.render(sequences.SEQUENCES["close-out-from-closure"][1]), section)


# Rework dispatches a fresh worker, and only the stage 3 references say which model to pass
# and when to review the diff against the boundary yourself. Every place that sends a run
# back to rework names the re-entry that prints them: (file, start of the passage, its end).
REWORK_TRIGGERS = (
    ("references/stage-4-gates.md", "A gate that fails inside the boundary", "\n\n"),
    ("references/stage-5-verification.md", "- **REWORK**", "- **BLOCKED**"),
    ("references/stage-7-review.md", "4. `NEEDS_CODE_CHANGE`", "\n5. "),
    ("references/review-loop.md", "- `NEEDS_CODE_CHANGE`", "- `NEEDS_REPLY_ONLY`"),
    ("references/review-loop.md", "8. Red CI on the candidate", "\n9. "),
)
REWORK_REENTRY = ("`ppl stage enter implementation`", "`references/stage-3-implementation.md`",
                  "`ppl stage enter tests`", "`references/stage-3-tests.md`")


def _flat(text):
    return " ".join(text.split())


class TestTrackerNeutralSkill(unittest.TestCase):
    """The tracker steps of the skill come from the tracker adapter: Jira keeps its MCP
    tools, GitHub Issues reads with ppl tracker fetch and writes with gh issue comment."""

    def stage(self, name, reference):
        return _flat(RENDERED[name]["references/%s" % reference])

    def test_the_tracker_write_waits_for_the_recorded_merge_for_each_tracker(self):
        for name in EXAMPLES:
            stage8 = self.stage(name, "stage-8-after-merge.md")
            self.assertIn("Jira", stage8, name)
            # the comment tool is the harness's, not a name fixed in the fragment
            self.assertIn("the Atlassian MCP comment tool among the harness's "
                          "`tracker.write_operations`", stage8, name)
            self.assertNotIn("addOrEditJiraIssueComment", stage8, name)
            self.assertIn("[R-JIRA-GATE]", stage8, name)
        stage8 = self.stage("github-issues", "stage-8-after-merge.md")
        self.assertIn("gh issue comment", stage8)
        self.assertIn("[R-TRACKER-CLI-GATE]", stage8)
        self.assertLess(stage8.index("ppl merge observed"), stage8.index("gh issue comment"))
        for path, text in sorted(RENDERED["github-issues"].items()):
            # a rule id names Jira in capitals, any other mention is Jira text
            self.assertNotIn("jira", rules.MARKER_RE.sub("", text).lower(), path)

    def test_the_note_is_introduced_once(self):
        for name in RENDERED:
            stage8 = self.stage(name, "stage-8-after-merge.md")
            self.assertIn("write to the tracker %s:" % harness.DEFAULT_TRACKER_CLOSE, stage8, name)
            self.assertEqual(stage8.count(harness.DEFAULT_TRACKER_CLOSE), 1, name)
            self.assertNotIn("saying a short comment", stage8, name)

    def test_stage_zero_runs_init_before_reading_the_task(self):
        for name in EXAMPLES:
            stage0 = self.stage(name, "stage-0-admission.md")
            self.assertIn("getJiraIssue", stage0, name)
            self.assertNotIn("ppl tracker fetch", stage0, name)
            self.assertLess(stage0.index("ppl init"), stage0.index("getJiraIssue"), name)
        stage0 = self.stage("github-issues", "stage-0-admission.md")
        self.assertIn("ppl tracker fetch --task", stage0)
        self.assertLess(stage0.index("ppl init"), stage0.index("ppl tracker fetch --task"))
        self.assertLess(stage0.index("ppl tracker fetch --task"), stage0.index("issue.md"))
        self.assertLess(stage0.index("issue.md"), stage0.index("admission list"))

    def test_stage_zero_names_the_fetched_revision_as_the_governing_one(self):
        stage0 = self.stage("github-issues", "stage-0-admission.md")
        self.assertIn("governing revision", stage0)
        self.assertIn("fetched revision", stage0)

    def test_no_reference_calls_the_task_a_jira_task_id(self):
        for name, files in RENDERED.items():
            for path, text in sorted(files.items()):
                self.assertNotIn("Jira task id", text, "%s %s" % (name, path))
                self.assertNotIn("Jira gate", text, "%s %s" % (name, path))
                self.assertNotIn("Jira note", text, "%s %s" % (name, path))

    def test_the_after_merge_checks_cover_every_example(self):
        for name in RENDERED:
            section = self.stage(name, "stage-8-after-merge.md")
            self.assertLess(section.index("ppl merge observed --sha"),
                            section.index("write to the tracker"), name)
            self.assertEqual(section.count(sequences.render(sequences.AFTER_MERGE)), 2, name)
            self.assertNotIn("<!-- include:", section, name)

    def test_the_recovery_step_names_the_stage_zero_reference(self):
        for name, files in RENDERED.items():
            flat = _flat(files["SKILL.md"])
            self.assertIn("Stage 0's `ppl init` step (`references/stage-0-admission.md`)", flat, name)

    def test_the_rendered_close_command_is_a_tracker_write_held_until_the_merge_is_recorded(self):
        stage8 = self.stage("github-issues", "stage-8-after-merge.md")
        found = re.findall(r"`(gh issue comment [^`]+)`", stage8)
        self.assertEqual(len(found), 1, found)
        # the run's scratch directory, since a write into the worktree or runs/ is denied
        written = re.search(r"write the note to `(/[^`]*)/<TASK-ID>/note\.md`", stage8)
        self.assertTrue(written, stage8)
        scratch = written.group(1)
        self.assertEqual(os.path.basename(scratch), "scratch")
        command = (found[0].replace("<number>", "45").replace("<owner/name>", "o/r")
                   .replace("<TASK-ID>", "GH-45"))
        self.assertEqual(command, "gh issue comment 45 -R o/r --body-file %s/GH-45/note.md" % scratch)
        self.assertNotIn("$", found[0])
        self.assertTrue(ppl_guard.tracker_cli_write(command), "the close is not read as a write")
        for stage in ("review", "closure", "merge_watch"):
            state = {"stage": stage, "mr": {}}
            denial = ppl_guard.tracker_gate_denial(state)
            self.assertIsNotNone(denial, stage)
        merged = {"stage": "merge_watch", "mr": {"merged_sha": "f" * 40}}
        self.assertIsNone(ppl_guard.tracker_gate_denial(merged))


class TestReworkReentersStageThree(unittest.TestCase):
    def test_every_rework_trigger_names_the_stage_three_reentry(self):
        problems = []
        for name, files in RENDERED.items():
            for path, start, end in REWORK_TRIGGERS:
                text = files[path]
                passage = text[text.index(start):]
                passage = " ".join(passage[:passage.index(end, len(start))].split())
                problems += ["%s %s: %s missing" % (name, path, needed)
                             for needed in REWORK_REENTRY if needed not in passage]
        self.assertEqual(problems, [])

    def test_the_rework_next_action_names_reentries_legal_from_verification(self):
        action = verdict.VERDICT_NEXT_ACTION["REWORK"]
        stages = re.findall(r"\bppl stage enter (\w+)", action)
        self.assertEqual(sorted(stages), ["implementation", "tests"], action)
        for stage in stages:
            self.assertIsNone(walk(("ppl stage enter %s" % stage,), "verification", "in_progress"))
        self.assertLess(action.index("ppl budget spend retry"),
                        action.index("ppl stage enter implementation"))


SKILL_BUDGET_BYTES = 14000
STAGE_REFERENCE_BUDGET_BYTES = 10000


class TestStageReferences(unittest.TestCase):
    """SKILL.md is read whole on every invocation and keeps what every stage needs. Each
    stage's procedure is a reference the orchestrator reads when the run enters that stage,
    named by `ppl stage enter`, so the budgets hold on both examples."""

    def stage_files(self, files):
        return sorted(path for path in files if path.startswith("references/stage-"))

    def test_the_rendered_skill_md_stays_within_its_budget(self):
        for name, files in RENDERED.items():
            size = len(files["SKILL.md"].encode("utf-8"))
            self.assertLessEqual(size, SKILL_BUDGET_BYTES, "%s SKILL.md is %d bytes" % (name, size))

    def test_each_stage_reference_stays_within_its_budget(self):
        for name, files in RENDERED.items():
            self.assertTrue(self.stage_files(files), "%s renders no stage reference" % name)
            for path in self.stage_files(files):
                size = len(files[path].encode("utf-8"))
                self.assertLessEqual(size, STAGE_REFERENCE_BUDGET_BYTES,
                                     "%s %s is %d bytes" % (name, path, size))

    def test_the_stage_references_rendered_are_the_ones_ppl_names(self):
        for name, files in RENDERED.items():
            self.assertEqual(set(self.stage_files(files)), set(run.STAGE_REFERENCES.values()), name)

    def test_every_stage_but_parking_and_escalation_names_a_reference(self):
        self.assertEqual(set(fsm.STAGES) - set(run.STAGE_REFERENCES), {"parking", "escalation"})

    def test_the_stage_map_in_skill_md_names_every_stage_reference(self):
        for name, files in RENDERED.items():
            for path in sorted(set(run.STAGE_REFERENCES.values())):
                self.assertIn("`%s`" % path, files["SKILL.md"], name)


class TestSequences(unittest.TestCase):
    """Each sequence is walked through the real ppl from the stage and status it starts in,
    so a step the state machine or a precondition refuses fails here, not in a run."""

    def test_the_statuses_a_stage_can_be_in_come_from_the_real_rules(self):
        statuses = reachable_statuses()
        self.assertEqual(statuses["review"], {"in_progress"})
        self.assertEqual(statuses["closure"], {"in_progress", "done"})
        self.assertEqual(statuses["merge_watch"], {"done"})
        self.assertEqual(statuses["admission"], {"planned"})

    def test_a_tail_of_the_close_out_starts_from_each_status_closure_can_be_in(self):
        starts = {start[1] for start, _ in sequences.SEQUENCES.values() if start[0] == "closure"}
        self.assertEqual(starts, reachable_statuses()["closure"])

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

    def test_the_walk_refuses_closure_while_the_done_gate_would_refuse(self):
        # rule: R-CLOSURE-ONLY-WHEN-DONE-PASSES
        blocker = "no verdict recorded for candidate %s" % WALK_SHA
        refused = walk(("ppl stage enter closure",), "review", "in_progress", blockers=[blocker])
        self.assertIn("`ppl stage enter closure` in stage review", refused)
        self.assertIn("ILLEGAL_TRANSITION", refused)
        self.assertIn(blocker, refused)
        # and the run, still in review, can go back to Stage 3
        self.assertIsNone(walk(("ppl stage enter implementation",), "review", "in_progress",
                               blockers=[blocker]))

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
    "recovery for one status": ("From `closure`, run `ppl transition done`, "
                                "`ppl stage enter merge_watch`."),
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
        problems = [p for p in self.assert_reports(illegal_recoveries(SEEDED)) if "`review`" in p]
        self.assertEqual(len(problems), 1)
        self.assertIn("from `review` (status in_progress): `ppl stage enter merge_watch` in stage "
                      "review", problems[0])
        self.assertIn("ILLEGAL_TRANSITION", problems[0])

    def test_a_recovery_that_holds_for_one_status_of_its_stage_only_is_reported(self):
        # rule: R-FSM-LIFECYCLE
        # A run stopped between `ppl transition done` and `ppl stage enter merge_watch` is in
        # closure with status done, where transition done is refused.
        problems = [p for p in self.assert_reports(illegal_recoveries(SEEDED)) if "`closure`" in p]
        self.assertEqual(len(problems), 1)
        self.assertIn("from `closure` (status done): `ppl transition done` in stage closure, "
                      "status done", problems[0])
        self.assertIn("ILLEGAL_TRANSITION", problems[0])

    def test_a_stage_whose_recovery_names_one_status_must_name_every_status_it_can_be_in(self):
        partial = {"references/x.md": "From `closure` with status `in_progress`, run "
                                      "`ppl transition done`, `ppl stage enter merge_watch`."}
        self.assertEqual(illegal_recoveries(partial),
                         ["references/x.md: from `closure`: no instruction for status done"])

    def test_a_recovery_naming_a_status_its_stage_is_never_in_is_reported(self):
        wrong = {"references/x.md": "From `review` with status `done`, run `ppl approval check`."}
        self.assertEqual(illegal_recoveries(wrong),
                         ["references/x.md: from `review` with status `done`: no run is in that "
                          "stage with that status"])

    def test_the_parser_refuses_the_misspelled_stage_as_well(self):
        # rule: R-FSM-STAGE-GRAPH
        refused = walk(("ppl stage enter merge_wach",), "closure", "done")
        self.assertIn("SCHEMA_INVALID", refused)
        self.assertIn("invalid choice", refused)


if __name__ == "__main__":
    unittest.main()

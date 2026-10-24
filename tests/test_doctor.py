import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported by
# path, before PPL_HOME (and, here, PPL_CLAUDE_HOME/PPL_LOCAL_SETTINGS_DIR)
# are chosen, exactly as every other test module does.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
os.environ["PPL_CLAUDE_HOME"] = tmphome.mkdtemp(prefix="pplclaudehome-")
os.environ["PPL_LOCAL_SETTINGS_DIR"] = tmphome.mkdtemp(prefix="ppllocalsettings-")

import fixtures
from ppl import config, doctor, forges, models, paths, setup, trackers
from ppl.trackers import github_issues


# The shape ppl setup claude writes: the interpreter that ran setup, then the hook.
GUARD_COMMAND = "/usr/local/bin/python3.12 /home/dev/antline/hooks/ppl_guard.py"

# A registration that names only the file-editing tools - the matcher a first
# reading of the hook's purpose suggests, and the one that leaves Bash, the
# GitLab denials and the Jira gate dead.
EDITING_TOOLS_ONLY = "Edit|Write|MultiEdit|NotebookEdit"


def write_settings(path, guarded=True, other_hooks=True, matchers=(doctor.RECOMMENDED_MATCHER,),
                   command=GUARD_COMMAND):
    """One guard entry per matcher in `matchers` - None for an entry with no matcher key."""
    doc = {"hooks": {"PreToolUse": []}}
    if other_hooks:
        doc["hooks"]["PreToolUse"].append({
            "matcher": "Bash", "hooks": [{"type": "command", "command": "sqz compress"}]})
    if guarded:
        for matcher in matchers:
            entry = {"hooks": [{"type": "command", "command": command, "timeout": 5}]}
            if matcher is not None:
                entry["matcher"] = matcher
            doc["hooks"]["PreToolUse"].append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


class TestRegistration(unittest.TestCase):
    def setUp(self):
        self.local_dir = Path(tmphome.mkdtemp(prefix="ppllocaldir-"))
        self.local_path = self.local_dir / ".claude" / "settings.local.json"
        self.global_path = doctor.global_settings_path()
        for p in (self.local_path, self.global_path):
            if p.exists():
                p.unlink()

    def tearDown(self):
        for p in (self.local_path, self.global_path):
            if p.exists():
                p.unlink()

    def test_none_when_neither_file_carries_the_guard(self):
        self.assertEqual(doctor.registration(cwd=self.local_dir), "none")

    def test_local_only(self):
        write_settings(self.local_path)
        self.assertEqual(doctor.registration(cwd=self.local_dir), "local")

    def test_global_only(self):
        write_settings(self.global_path)
        self.assertEqual(doctor.registration(cwd=self.local_dir), "global")

    def test_both(self):
        write_settings(self.local_path)
        write_settings(self.global_path)
        self.assertEqual(doctor.registration(cwd=self.local_dir), "both")

    def test_the_guard_is_recognised_whatever_interpreter_runs_it(self):
        # Matched on the hook script, not the interpreter: an entry written by
        # an older setup, by hand, or with a quoted path is the same guard.
        for command in ("python3 /home/dev/antline/hooks/ppl_guard.py",
                        "/usr/bin/python3 /home/dev/antline/hooks/ppl_guard.py",
                        "'/opt/my python/bin/python3.12' '/home/dev/antline/hooks/ppl_guard.py'",
                        "%s /home/dev/antline/hooks/ppl_guard.py" % shlex.quote(sys.executable)):
            write_settings(self.local_path, command=command)
            self.assertEqual(doctor.registration(cwd=self.local_dir), "local", command)

    def test_settings_file_present_but_hook_not_the_guard_is_none(self):
        write_settings(self.local_path, guarded=False)
        self.assertEqual(doctor.registration(cwd=self.local_dir), "none")

    def test_malformed_settings_file_is_unknown_not_absent(self):
        # A file that exists but cannot be parsed - a hand-added "//" comment
        # is an ordinary edit, not a contrived case - must not be reported as
        # "no hook here". That is "I could not confirm" reported as "clear",
        # which is the exact failure this module exists to remove.
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("// my settings\nnot json", encoding="utf-8")
        self.assertEqual(doctor.registration(cwd=self.local_dir), "unknown")

    def test_malformed_local_file_does_not_hide_a_confirmed_global_yes(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("not json", encoding="utf-8")
        write_settings(self.global_path)
        self.assertEqual(doctor.registration(cwd=self.local_dir), "global")

    def test_a_confirmed_local_yes_does_not_rescue_an_unreadable_global_file(self):
        # Round 3, Finding 1: isolated=true asserts two things - the hook
        # fires here (a confirmed local yes) AND it does not fire elsewhere
        # (a confirmed global no). A local yes only answers the first half.
        # An unreadable global file leaves the second half unconfirmed, so
        # this must be "unknown", not "local" - the global file might well
        # contain an armed hook that fires in every session, exactly the
        # thing isolation is supposed to rule out.
        write_settings(self.local_path)
        self.global_path.write_text("// my settings\nnot json", encoding="utf-8")
        self.assertEqual(doctor.registration(cwd=self.local_dir), "unknown")

    def test_non_object_json_is_unknown(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual(doctor.registration(cwd=self.local_dir), "unknown")

    def test_inspect_carries_paths_and_the_parse_detail(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("not json", encoding="utf-8")
        result = doctor.inspect(cwd=self.local_dir)
        self.assertEqual(result["registration"], "unknown")
        self.assertEqual(result["local"]["state"], "unknown")
        self.assertIn(str(self.local_path), result["local"]["detail"])
        self.assertEqual(result["global"]["path"], str(self.global_path))

    @unittest.skipIf(os.name != "posix" or os.getuid() == 0,
                     "chmod-based unreadable file needs a non-root POSIX user")
    def test_unreadable_permissions_is_unknown(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        write_settings(self.local_path)
        self.local_path.chmod(0o000)
        try:
            self.assertEqual(doctor.registration(cwd=self.local_dir), "unknown")
        finally:
            self.local_path.chmod(0o644)


class TestMatcher(unittest.TestCase):
    """A hook registered on a matcher that never passes a tool name to it is a
    denial that cannot fire, whatever the code behind it says. The Jira gate was
    dead exactly this way on the machine this was built on: the guard knew the
    tools under one Atlassian prefix, the matcher passed only that prefix, and
    the server registered under another. doctor now judges every registered
    guard matcher against the hook's own list of the tools it acts on.
    """

    def setUp(self):
        self.local_dir = Path(tmphome.mkdtemp(prefix="ppllocaldir-"))
        self.local_path = self.local_dir / ".claude" / "settings.local.json"
        self.global_path = doctor.global_settings_path()
        for p in (self.local_path, self.global_path):
            if p.exists():
                p.unlink()

    def tearDown(self):
        for p in (self.local_path, self.global_path):
            if p.exists():
                p.unlink()

    def inspect(self):
        return doctor.inspect(cwd=self.local_dir)

    def test_the_recommended_matcher_covers_every_guarded_tool(self):
        # rule: R-GUARD-MATCHER
        # The string README tells the operator to register and the list the
        # hook acts on are held together here - widen one, widen the other.
        names = doctor.guarded_tool_names()
        self.assertGreater(len(names), 5)
        for name in names:
            self.assertTrue(doctor.matcher_covers(doctor.RECOMMENDED_MATCHER, name), name)

    def test_the_readme_recipe_registers_the_recommended_matcher(self):
        # rule: R-GUARD-MATCHER
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
        self.assertIn('"matcher": "%s"' % doctor.RECOMMENDED_MATCHER, readme)

    def test_the_guarded_names_are_the_cage_surface(self):
        names = doctor.guarded_tool_names()
        for name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "WebFetch",
                     "mcp__GitLab-MCP__merge_merge_request",
                     "mcp__atlassian__addCommentToJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue",
                     "mcp__atlassian_2__addCommentToJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo_2__addCommentToJiraIssue",
                     "mcp__atlassian__addOrEditJiraIssueComment",
                     "mcp__atlassian__createJiraIssue",
                     "mcp__atlassian__executeWrite",
                     "mcp__claude_ai_Atlassian_Rovo__executeDestructive",
                     "mcp__atlassian_2__updateConfluenceContent"):
            self.assertIn(name, names)

    def test_a_complete_registration_is_ok_with_nothing_uncovered(self):
        # rule: R-GUARD-MATCHER
        write_settings(self.local_path)
        result = self.inspect()
        self.assertEqual(result["matcher"], "ok")
        self.assertEqual(result["local"]["uncovered"], [])
        self.assertIsNone(doctor.matcher_problem(result))

    def test_a_matcher_naming_only_the_editing_tools_is_incomplete(self):
        # rule: R-GUARD-MATCHER
        write_settings(self.local_path, matchers=(EDITING_TOOLS_ONLY,))
        result = self.inspect()
        self.assertEqual(result["registration"], "local")
        self.assertEqual(result["matcher"], "incomplete")
        uncovered = result["local"]["uncovered"]
        for name in ("Bash", "mcp__GitLab-MCP__merge_merge_request",
                     "mcp__atlassian__addCommentToJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo__editJiraIssue"):
            self.assertIn(name, uncovered)
        for name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            self.assertNotIn(name, uncovered)
        problem = doctor.matcher_problem(result)
        self.assertIn(str(self.local_path), problem)
        self.assertIn("Bash", problem)
        self.assertIn(doctor.RECOMMENDED_MATCHER, problem)

    def test_the_single_prefix_matcher_leaves_the_rovo_numbered_and_read_tool_names_uncovered(self):
        # rule: R-GUARD-MATCHER
        # The registration this pipeline shipped with, and the holes it had:
        # the Jira server under its live prefix, the numbered duplicate of the
        # hand-added prefix, the three read tools the test worker's read
        # isolation acts on, the GitHub MCP tools the forge union denies, and the
        # Agent tool whose dispatch of ppl-test-worker the leak check reads.
        old = "Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__GitLab-MCP__.*|mcp__atlassian__.*"
        write_settings(self.local_path, matchers=(old,))
        result = self.inspect()
        self.assertEqual(result["matcher"], "incomplete")
        expected = [name for name in doctor.guarded_tool_names()
                    if name.startswith("mcp__claude_ai_Atlassian_Rovo")
                    or name.startswith("mcp__atlassian_2__")
                    or name.startswith("mcp__github__")
                    or name in ("Read", "Grep", "Glob", "Agent", "Task", "WebFetch")]
        self.assertGreater(len(expected), 3)
        self.assertEqual(result["local"]["uncovered"], expected)
        for name in ("Read", "Grep", "Glob"):
            self.assertIn(name, result["local"]["uncovered"])

    def test_a_star_or_absent_matcher_covers_every_tool(self):
        for matcher in ("*", "", None):
            write_settings(self.local_path, matchers=(matcher,))
            result = self.inspect()
            self.assertEqual(result["matcher"], "ok", repr(matcher))
            self.assertEqual(result["local"]["uncovered"], [], repr(matcher))

    def test_a_partial_match_is_not_coverage(self):
        # rule: R-GUARD-MATCHER
        # Full match, not search: a prefix without `.*` reaches the hook for
        # nothing, and "Bash" must not be read as covering "BashOutput".
        self.assertFalse(doctor.matcher_covers("mcp__atlassian",
                                               "mcp__atlassian__editJiraIssue"))
        self.assertTrue(doctor.matcher_covers("mcp__atlassian__.*",
                                              "mcp__atlassian__editJiraIssue"))
        self.assertTrue(doctor.matcher_covers("Bash", "Bash"))
        self.assertFalse(doctor.matcher_covers("Bash", "BashOutput"))

    def test_a_matcher_that_does_not_compile_covers_nothing(self):
        # rule: R-GUARD-MATCHER
        write_settings(self.local_path, matchers=("mcp__(",))
        result = self.inspect()
        self.assertEqual(result["registration"], "local")
        self.assertEqual(result["matcher"], "incomplete")
        self.assertEqual(result["local"]["uncovered"], list(doctor.guarded_tool_names()))

    def test_coverage_is_the_union_over_the_guard_entries(self):
        # rule: R-GUARD-MATCHER
        # Two entries that each run the guard, neither complete on its own (the
        # first lacks Bash, the read tools and every MCP name, the second lacks
        # the editing tools): the hook fires when any one of them matches, so
        # together they cover.
        write_settings(self.local_path,
                       matchers=(EDITING_TOOLS_ONLY,
                                 "Bash|Read|Grep|Glob|Agent|Task|WebFetch|mcp__.*"))
        result = self.inspect()
        self.assertEqual(result["matcher"], "ok")
        self.assertEqual(result["local"]["uncovered"], [])

    def test_no_registration_has_no_matcher_verdict(self):
        result = self.inspect()
        self.assertEqual(result["registration"], "none")
        self.assertIsNone(result["matcher"])
        self.assertIsNone(result["local"]["uncovered"])
        self.assertIsNone(result["global"]["uncovered"])
        self.assertIsNone(doctor.matcher_problem(result))

    def test_a_file_without_the_guard_has_no_uncovered_list(self):
        write_settings(self.local_path, guarded=False)
        result = self.inspect()
        self.assertEqual(result["local"]["state"], "no")
        self.assertIsNone(result["local"]["uncovered"])

    def test_an_unreadable_file_is_left_to_the_registration_check(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("not json", encoding="utf-8")
        result = self.inspect()
        self.assertEqual(result["local"]["state"], "unknown")
        self.assertIsNone(result["local"]["uncovered"])
        self.assertIsNone(result["matcher"])
        self.assertIsNone(doctor.matcher_problem(result))

    def test_an_incomplete_global_registration_is_a_problem_wherever_the_run_is(self):
        # rule: R-GUARD-MATCHER
        write_settings(self.global_path, matchers=(EDITING_TOOLS_ONLY,))
        result = self.inspect()
        self.assertEqual(result["registration"], "global")
        self.assertEqual(result["matcher"], "incomplete")
        problem = doctor.matcher_problem(result)
        self.assertIn(str(self.global_path), problem)

    def test_a_complete_local_beside_an_incomplete_global_is_still_incomplete(self):
        # rule: R-GUARD-MATCHER
        # The global entry fires in every session on the machine with a hole in
        # it, and a working local one next to it does not close that hole.
        write_settings(self.local_path)
        write_settings(self.global_path, matchers=(EDITING_TOOLS_ONLY,))
        result = self.inspect()
        self.assertEqual(result["matcher"], "incomplete")
        self.assertEqual(result["local"]["uncovered"], [])
        self.assertIn("Bash", result["global"]["uncovered"])
        self.assertIn(str(self.global_path), doctor.matcher_problem(result))

    # Every guarded name spelled out, the explicit matcher an operator could keep. It
    # passes the guarded-name check and still misses the GitHub MCP issue writes.
    @staticmethod
    def explicit_guarded_names():
        return "|".join(re.escape(name) for name in doctor.guarded_tool_names())

    def test_an_explicit_matcher_missing_github_issue_writes_is_warned_not_refused(self):
        # rule: R-GUARD-MATCHER
        write_settings(self.local_path, matchers=(self.explicit_guarded_names(),))
        result = self.inspect()
        self.assertEqual(result["matcher"], "ok")
        self.assertIsNone(doctor.matcher_problem(result))
        warning = doctor.matcher_warning(cwd=self.local_dir)
        self.assertIn(str(self.local_path), warning)
        self.assertIn(doctor.GITHUB_ISSUE_WRITE_PROBE, warning)
        self.assertIn("GitHub MCP issue writes would not reach the guard", warning)
        self.assertIn('"%s"' % doctor.RECOMMENDED_MATCHER, warning)

    def test_a_catch_all_matcher_draws_no_github_issue_write_warning(self):
        self.assertEqual(doctor.GITHUB_ISSUE_WRITE_PROBE, "mcp__github__add_issue_comment")
        # the probe is a write the adapter gates, so the warning tracks the gate
        self.assertTrue(github_issues.mcp_write_regex({}).match(doctor.GITHUB_ISSUE_WRITE_PROBE))
        self.assertIsNone(doctor.matcher_warning(cwd=self.local_dir))
        installed = setup.hook_entries(fixtures.SOURCE)["PreToolUse"]["matcher"]
        for matchers in ((installed,), (None,), ("",), ("*",), (".*",),
                         (EDITING_TOOLS_ONLY, "Bash|Read|Grep|Glob|Agent|Task|mcp__.*")):
            write_settings(self.local_path, matchers=matchers)
            self.assertIsNone(doctor.matcher_warning(cwd=self.local_dir), matchers)
        write_settings(self.local_path, guarded=False)
        self.assertIsNone(doctor.matcher_warning(cwd=self.local_dir))

    def test_a_global_explicit_matcher_is_warned_beside_a_complete_local_one(self):
        # rule: R-GUARD-MATCHER
        write_settings(self.local_path)
        write_settings(self.global_path, matchers=(self.explicit_guarded_names(),))
        warning = doctor.matcher_warning(cwd=self.local_dir)
        self.assertIn(str(self.global_path), warning)
        self.assertNotIn(str(self.local_path), warning)


class TestMismatch(unittest.TestCase):
    def test_isolated_true_with_global_only_is_a_mismatch(self):
        self.assertIsNotNone(doctor.mismatch(True, "global"))

    def test_isolated_true_with_local_is_not_a_mismatch(self):
        self.assertIsNone(doctor.mismatch(True, "local"))

    def test_isolated_true_with_both_is_a_mismatch(self):
        # Round 3, Finding 2: a working local registration does not make an
        # armed global one safe - the global copy still fires everywhere.
        self.assertIsNotNone(doctor.mismatch(True, "both"))

    def test_isolated_false_with_both_is_not_a_mismatch(self):
        self.assertIsNone(doctor.mismatch(False, "both"))

    def test_both_mismatch_message_names_both_files_and_says_remove_global(self):
        message = doctor.mismatch(True, "both")
        self.assertIn("settings.local.json", message)
        self.assertIn("settings.json", message)
        self.assertIn("--no-isolated", message)

    def test_isolated_true_with_none_is_not_flagged_as_a_mismatch(self):
        # "not armed at all" is the pre-existing, openly documented state
        # (README.md "Hooks"), not a flag/reality mismatch.
        self.assertIsNone(doctor.mismatch(True, "none"))

    def test_isolated_false_with_global_is_not_a_mismatch(self):
        self.assertIsNone(doctor.mismatch(False, "global"))

    def test_isolated_false_with_local_is_not_a_mismatch(self):
        self.assertIsNone(doctor.mismatch(False, "local"))

    def test_mismatch_message_names_both_fixes(self):
        message = doctor.mismatch(True, "global")
        self.assertIn("settings.local.json", message)
        self.assertIn("--no-isolated", message)

    def test_isolated_true_with_unknown_is_a_mismatch(self):
        # Finding 1, round 2: "unknown" must refuse under isolated=true - a
        # checker that cannot see must not clear the way.
        self.assertIsNotNone(doctor.mismatch(True, "unknown"))

    def test_isolated_false_with_unknown_is_not_a_mismatch(self):
        self.assertIsNone(doctor.mismatch(False, "unknown"))

    def test_unknown_mismatch_message_says_isolation_cannot_be_confirmed(self):
        message = doctor.mismatch(True, "unknown")
        self.assertIn("--no-isolated", message)
        self.assertIn("confirmed", message)

    def test_unknown_mismatch_message_names_the_broken_file_from_an_inspect_dict(self):
        inspected = {
            "registration": "unknown",
            "local": {"path": "/x/.claude/settings.local.json", "state": "unknown",
                      "detail": "/x/.claude/settings.local.json is not valid JSON: boom"},
            "global": {"path": "/y/.claude/settings.json", "state": "no", "detail": None},
        }
        message = doctor.mismatch(True, inspected)
        self.assertIn("/x/.claude/settings.local.json", message)

    def test_mismatch_accepts_an_inspect_dict_for_the_global_case_too(self):
        inspected = {
            "registration": "global",
            "local": {"path": "/x", "state": "no", "detail": None},
            "global": {"path": "/y", "state": "yes", "detail": None},
        }
        self.assertIsNotNone(doctor.mismatch(True, inspected))


class TestInspectSetup(unittest.TestCase):
    def setUp(self):
        home = mock.patch.dict(os.environ, {"PPL_HOME": tmphome.mkdtemp(prefix="pplhome-")})
        home.start()
        self.addCleanup(home.stop)
        version = mock.patch.object(models, "claude_version", lambda binary="claude": "9.9.9")
        version.start()
        self.addCleanup(version.stop)

    def complete_setup(self):
        fixtures.install_routing()
        fixtures.install_harness("example-maven")
        config.ensure()
        config.update("claude", {"workspace": tmphome.mkdtemp(prefix="pplworkspace-")})
        for step in setup.STEPS:
            config.mark_step(step, "done")
        config.set_active_harness("example-maven")

    def test_a_code_root_without_defaults_lands_in_the_report(self):
        bare = tmphome.mkdtemp(prefix="pplroot-")
        with mock.patch.dict(os.environ, {"PPL_ROOT": bare}):
            report = doctor.inspect_setup()
        self.assertIsNone(report["code_root"])
        self.assertIn(bare, report["code_root_problem"])

    def test_fresh_home_reports_every_step_missing(self):
        report = doctor.inspect_setup()
        self.assertEqual(report["setup"]["missing"], list(setup.STEPS))
        self.assertIsNone(report["harness"]["active"])
        self.assertEqual(report["models"], [])
        self.assertIsNone(report["forge"])

    def test_after_setup_everything_is_reported(self):
        self.complete_setup()
        models.verify(runner=lambda m, binary="claude", env=None:
                      {"ok": True, "resolved_id": "id", "cost_usd": 0.0, "detail": ""})
        report = doctor.inspect_setup()
        self.assertEqual(report["code_root"], str(paths.root()))
        self.assertEqual(report["data_root"], str(paths.home()))
        self.assertEqual(report["setup"]["missing"], [])
        self.assertEqual(report["setup"]["done"], sorted(setup.STEPS))
        self.assertEqual(report["harness"]["active"], "example-maven")
        self.assertTrue(report["harness"]["valid"])
        self.assertFalse(report["harness"]["repo_exists"])
        self.assertEqual({row["state"] for row in report["models"]}, {"verified"})
        self.assertEqual(report["forge"]["kind"], "gitlab")
        self.assertIn("ok", report["forge"])

    def test_skipped_steps_are_listed_and_still_missing(self):
        config.ensure()
        config.mark_step("paths", "done")
        config.mark_step("forge", "skipped")
        report = doctor.inspect_setup()
        self.assertEqual(report["setup"]["done"], ["paths"])
        self.assertEqual(report["setup"]["skipped"], ["forge"])
        self.assertIn("forge", report["setup"]["missing"])

    def test_setup_problem_names_the_step_and_the_command(self):
        config.ensure()
        config.mark_step("paths", "done")
        problem = doctor.setup_problem(doctor.inspect_setup(), skip_model_check=False)
        self.assertIn("ppl setup harness", problem)
        self.assertIn("models", problem)

    def test_skip_model_check_waives_the_models_step_only(self):
        self.complete_setup()
        config.mark_step("models", None)
        self.assertIsNone(doctor.setup_problem(doctor.inspect_setup(), skip_model_check=True))
        self.assertIn("models",
                      doctor.setup_problem(doctor.inspect_setup(), skip_model_check=False))

    def test_only_models_harness_and_apply_are_required(self):
        self.complete_setup()
        for step in ("paths", "claude", "forge"):
            config.mark_step(step, None)
        report = doctor.inspect_setup()
        self.assertEqual(report["setup"]["missing"], ["paths", "claude", "forge"])
        self.assertIsNone(doctor.setup_problem(report, skip_model_check=False))
        for step in ("harness", "apply"):
            config.mark_step(step, None)
        problem = doctor.setup_problem(doctor.inspect_setup(), skip_model_check=False)
        self.assertIn("`ppl setup harness` and `ppl setup apply`", problem)

    def test_a_skipped_models_step_points_at_the_skip_flag(self):
        self.complete_setup()
        config.mark_step("models", "skipped")
        problem = doctor.setup_problem(doctor.setup_state(), skip_model_check=False)
        self.assertIn("`ppl setup models`", problem)
        self.assertIn("--skip-model-check", problem)
        config.mark_step("models", None)
        self.assertNotIn("--skip-model-check",
                         doctor.setup_problem(doctor.setup_state(), skip_model_check=False))

    def test_setup_state_reads_neither_the_models_nor_the_forge(self):
        self.complete_setup()
        with mock.patch.object(models, "status") as status, \
                mock.patch.object(forges, "get") as get:
            state = doctor.setup_state()
        status.assert_not_called()
        get.assert_not_called()
        self.assertEqual(state["setup"]["missing"], [])
        self.assertEqual(state["harness"], {"active": "example-maven", "problem": None})
        self.assertNotIn("models", state)
        self.assertNotIn("forge", state)

    def test_setup_state_never_raises(self):
        paths.config_file().write_text("[setup\n", encoding="utf-8")
        state = doctor.setup_state()
        self.assertEqual(state["setup"]["missing"], list(setup.STEPS))
        self.assertIn("config.toml", state["config_problem"])
        self.assertIn("config.toml", state["harness"]["problem"])
        paths.config_file().write_text("[harness]\nactive = 3\n", encoding="utf-8")
        state = doctor.setup_state()
        self.assertIn("harness.active", state["harness"]["problem"])

    def test_an_unreadable_config_lands_in_the_report(self):
        paths.config_file().write_text("[setup\n", encoding="utf-8")
        report = doctor.inspect_setup()
        self.assertEqual(report["setup"]["missing"], list(setup.STEPS))
        self.assertIn("config.toml", report["config_problem"])
        self.assertIsNone(report["harness"]["active"])
        self.assertIsNone(report["forge"])
        problem = doctor.setup_problem(report, skip_model_check=True)
        self.assertIn("config.toml", problem)

    def test_a_malformed_setup_table_lands_in_the_report(self):
        paths.config_file().write_text('[setup]\ndone = "paths"\n', encoding="utf-8")
        report = doctor.inspect_setup()
        self.assertEqual(report["setup"]["missing"], list(setup.STEPS))
        self.assertIn("setup.done", report["config_problem"])

    def test_a_broken_routing_file_lands_in_the_report(self):
        self.complete_setup()
        paths.routing_file().write_text("[roles\n", encoding="utf-8")
        report = doctor.inspect_setup()
        self.assertEqual(report["models"], [])
        self.assertTrue(report["models_problem"])
        self.assertEqual(report["setup"]["missing"], [])

    def test_an_active_harness_that_is_not_installed_lands_in_the_report(self):
        config.ensure()
        config.set_active_harness("ghost")
        report = doctor.inspect_setup()
        self.assertEqual(report["harness"]["active"], "ghost")
        self.assertFalse(report["harness"]["valid"])
        self.assertTrue(report["harness"]["problem"])
        self.assertIsNone(report["forge"])

    def test_a_non_string_active_harness_lands_in_the_report(self):
        paths.config_file().write_text("[harness]\nactive = 3\n", encoding="utf-8")
        report = doctor.inspect_setup()
        self.assertFalse(report["harness"]["valid"])
        self.assertIn("harness.active", report["harness"]["problem"])

    def test_an_invalid_harness_document_lands_in_the_report(self):
        self.complete_setup()
        broken = paths.harness_dir("example-maven") / "harness.toml"
        broken.write_text("[task\n", encoding="utf-8")
        report = doctor.inspect_setup()
        self.assertEqual(report["harness"]["active"], "example-maven")
        self.assertFalse(report["harness"]["valid"])
        self.assertIn("not valid TOML", report["harness"]["problem"])

    def test_the_report_carries_the_tracker_check(self):
        self.assertIsNone(doctor.inspect_setup()["tracker"])
        self.complete_setup()
        report = doctor.inspect_setup()
        self.assertEqual(report["tracker"], {"kind": "jira", "ok": True, "detail": "not probed"})

    def test_a_failing_tracker_check_is_reported_like_a_failing_forge_check(self):
        self.complete_setup()
        seen = []

        def check(table, runner=None):
            seen.append(table["kind"])
            return {"ok": False, "detail": "gh is not installed or not on PATH"}
        with mock.patch.object(trackers.get("jira"), "doctor_check", check):
            report = doctor.inspect_setup()
        self.assertEqual(seen, ["jira"])
        self.assertEqual(report["tracker"], {"kind": "jira", "ok": False,
                                             "detail": "gh is not installed or not on PATH"})

    def test_a_tracker_check_that_blows_up_lands_in_the_report(self):
        self.complete_setup()

        def exploding(table, runner=None):
            raise RuntimeError("tracker exploded")
        with mock.patch.object(trackers.get("jira"), "doctor_check", exploding):
            report = doctor.inspect_setup()
        self.assertEqual(report["tracker"]["kind"], "jira")
        self.assertFalse(report["tracker"]["ok"])
        self.assertIn("tracker exploded", report["tracker"]["detail"])

    def test_a_forge_check_that_blows_up_lands_in_the_report(self):
        self.complete_setup()

        class Exploding:
            @staticmethod
            def doctor_check(settings, claude_json_path, workspace):
                raise RuntimeError("forge exploded")

        with mock.patch.object(forges, "get", lambda kind: Exploding):
            report = doctor.inspect_setup()
        self.assertTrue(report["harness"]["valid"])
        self.assertEqual(report["forge"]["kind"], "gitlab")
        self.assertFalse(report["forge"]["ok"])
        self.assertIn("forge exploded", report["forge"]["detail"])


class TestHookRuns(unittest.TestCase):
    """Registered is not the same as executes: a guard entry whose interpreter
    cannot run the hook disarms the cage while every file check stays green."""

    def setUp(self):
        self.local_dir = Path(tmphome.mkdtemp(prefix="ppllocaldir-"))
        self.local_path = self.local_dir / ".claude" / "settings.local.json"
        self.global_path = doctor.global_settings_path()
        for p in (self.local_path, self.global_path):
            if p.exists():
                p.unlink()
        self.addCleanup(lambda: self.global_path.exists() and self.global_path.unlink())

    def fake_guard(self, body):
        """A stand-in guard script: GUARD_MARKER matches its file name, `body` decides."""
        script = Path(tmphome.mkdtemp(prefix="pplfakeguard-")) / "ppl_guard.py"
        script.write_text(body, encoding="utf-8")
        return "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(script))), script.parent

    def test_no_registered_guard_reports_null(self):
        self.assertEqual(doctor.hook_runs(cwd=self.local_dir), {"ok": None, "problem": None})

    def test_a_guard_that_runs_clean_reports_true(self):
        command, _ = self.fake_guard("import json, sys\njson.load(sys.stdin)\n")
        write_settings(self.local_path, command=command)
        self.assertEqual(doctor.hook_runs(cwd=self.local_dir), {"ok": True, "problem": None})

    def test_the_payload_is_an_unroled_read_of_dev_null_from_the_workspace(self):
        command, where = self.fake_guard(
            "import json, os, sys\n"
            "doc = {'payload': json.load(sys.stdin), 'cwd': os.getcwd()}\n"
            "open(os.path.join(os.path.dirname(__file__), 'seen.json'), 'w').write(json.dumps(doc))\n")
        write_settings(self.local_path, command=command)
        workspace = tmphome.mkdtemp(prefix="pplworkspace-")
        self.assertTrue(doctor.hook_runs(cwd=self.local_dir, workspace=workspace)["ok"])
        seen = json.loads((where / "seen.json").read_text(encoding="utf-8"))
        # the session_id marks the probe, so the guard does not log its deny against a run
        self.assertEqual(seen["payload"], {"tool_name": "Read", "tool_input": {"file_path": "/dev/null"},
                                           "cwd": workspace,
                                           "session_id": doctor.guard_probe_session_id()})
        self.assertEqual(doctor.guard_probe_session_id(), "ppl-doctor-hook-probe")
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(workspace))

    def test_a_guard_that_fails_reports_false_with_its_first_stderr_line_and_the_remedy(self):
        command, _ = self.fake_guard(
            "import sys\nsys.stderr.write('re.error: unknown extension ?> at position 13\\n"
            "second line\\n')\nsys.exit(1)\n")
        write_settings(self.local_path, command=command)
        result = doctor.hook_runs(cwd=self.local_dir)
        self.assertIs(result["ok"], False)
        self.assertIn("re.error: unknown extension ?> at position 13", result["problem"])
        self.assertNotIn("second line", result["problem"])
        self.assertIn("ppl setup claude", result["problem"])

    def test_a_guard_that_exits_zero_but_writes_stderr_reports_false(self):
        # the shape the old-interpreter floor takes: exit 0, one stderr line
        command, _ = self.fake_guard(
            "import sys\nsys.stdin.read()\nsys.stderr.write('ppl_guard: Python 3.9.6 is older "
            "than 3.11\\n')\n")
        write_settings(self.local_path, command=command)
        result = doctor.hook_runs(cwd=self.local_dir)
        self.assertIs(result["ok"], False)
        self.assertIn("Python 3.9.6 is older than 3.11", result["problem"])

    def test_the_remedy_is_named_once_when_the_hook_already_names_it(self):
        # The interpreter floor's own line already says to run ppl setup claude.
        import io
        hooks = str(fixtures.SOURCE / "hooks")
        if hooks not in sys.path:
            sys.path.append(hooks)
        import ppl_python_floor
        err = io.StringIO()
        ppl_python_floor.refuse_old_python((3, 9, 6), "ppl_guard", "PreToolUse",
                                           io.StringIO("{}"), io.StringIO(), err)
        command, _ = self.fake_guard("import sys\nsys.stdin.read()\nsys.stderr.write(%r)\n"
                                     % err.getvalue())
        write_settings(self.local_path, command=command)
        result = doctor.hook_runs(cwd=self.local_dir)
        self.assertIs(result["ok"], False)
        self.assertIn("Python 3.9.6", result["problem"])
        self.assertEqual(result["problem"].count("ppl setup claude"), 1, result["problem"])

    def test_a_guard_that_exits_nonzero_silently_reports_false(self):
        command, _ = self.fake_guard("import sys\nsys.exit(3)\n")
        write_settings(self.local_path, command=command)
        result = doctor.hook_runs(cwd=self.local_dir)
        self.assertIs(result["ok"], False)
        self.assertIn("exited 3", result["problem"])

    def test_a_guard_run_through_env_asks_the_interpreter_env_names(self):
        hook = fixtures.SOURCE / "hooks" / "ppl_guard.py"
        for env in ("/usr/bin/env", "/usr/bin/env -i PYTHONDONTWRITEBYTECODE=1",
                    "/usr/bin/env -u PYTHONPATH --"):
            with self.subTest(env=env):
                write_settings(self.local_path, command="%s %s %s"
                               % (env, shlex.quote(sys.executable), shlex.quote(str(hook))))
                self.assertEqual(doctor.hook_runs(cwd=self.local_dir),
                                 {"ok": True, "problem": None})

    def test_a_guard_under_an_interpreter_older_than_the_floor_reports_false(self):
        # rule: R-DOCTOR-HOOK-SHAPE
        old = Path(tmphome.mkdtemp(prefix="pplfakepython-")) / "python3"
        old.write_text("#!/bin/sh\necho 3.9.6\n", encoding="utf-8")
        old.chmod(0o755)
        hook = fixtures.SOURCE / "hooks" / "ppl_guard.py"
        write_settings(self.local_path, command="%s %s" % (old, hook))
        result = doctor.hook_runs(cwd=self.local_dir)
        self.assertFalse(result["ok"])
        self.assertIn("Python 3.9.6", result["problem"])
        self.assertIn("3.11", result["problem"])
        self.assertIn(doctor.HOOK_RUN_REMEDY, result["problem"])

    def test_a_guard_that_hangs_reports_false(self):
        command, _ = self.fake_guard("import time\ntime.sleep(5)\n")
        write_settings(self.local_path, command=command)
        with mock.patch.object(doctor, "HOOK_RUN_TIMEOUT_SECONDS", 0.5):
            result = doctor.hook_runs(cwd=self.local_dir)
        self.assertIs(result["ok"], False)
        self.assertIn("did not finish", result["problem"])

    def test_every_registered_guard_command_is_run(self):
        good, _ = self.fake_guard("import sys\nsys.stdin.read()\n")
        bad, _ = self.fake_guard("import sys\nsys.stderr.write('broken global\\n')\nsys.exit(1)\n")
        write_settings(self.local_path, command=good)
        write_settings(self.global_path, command=bad)
        result = doctor.hook_runs(cwd=self.local_dir)
        self.assertIs(result["ok"], False)
        self.assertIn("broken global", result["problem"])

    def test_the_entry_setup_writes_runs_the_real_guard_clean(self):
        command = setup.hook_entries(fixtures.SOURCE)["PreToolUse"]["hooks"][0]["command"]
        write_settings(self.local_path, command=command)
        self.assertEqual(doctor.hook_runs(cwd=self.local_dir), {"ok": True, "problem": None})


class TestHookShape(unittest.TestCase):
    """A hook entry of the wrong shape is a hook that never fires, or fires with
    nothing to bound it: doctor reports the shape of the guard and dispatch
    entries per settings file, and whether the dispatch hook is there at all."""

    def setUp(self):
        self.local_dir = Path(tmphome.mkdtemp(prefix="ppllocaldir-"))
        self.local_path = self.local_dir / ".claude" / "settings.local.json"
        self.global_path = doctor.global_settings_path()
        for p in (self.local_path, self.global_path):
            if p.exists():
                p.unlink()
        self.addCleanup(lambda: self.global_path.exists() and self.global_path.unlink())

    def write(self, guard=None, dispatch=None, path=None, usage=None):
        """Settings with setup's own entries, each hook dict updated by `guard`,
        `dispatch` and `usage` (None drops that entry, a None value drops that key)."""
        entries = setup.hook_entries(fixtures.SOURCE)
        doc = {"hooks": {}}
        for event, change in (("PreToolUse", guard), ("SubagentStart", dispatch),
                              ("SubagentStop", usage)):
            if change is None:
                continue
            entry = json.loads(json.dumps(entries[event]))
            entry["hooks"][0].update(change)
            entry["hooks"][0] = {k: v for k, v in entry["hooks"][0].items() if v is not None}
            doc["hooks"][event] = [entry]
        target = path or self.local_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(doc), encoding="utf-8")

    def local(self):
        return doctor.inspect(cwd=self.local_dir)["local"]

    def fake_python(self, version):
        script = Path(tmphome.mkdtemp(prefix="pplfakepython-")) / "python3"
        script.write_text("#!/bin/sh\necho %s\n" % version, encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_the_entries_setup_writes_are_well_shaped_and_register_the_dispatch_hook(self):
        self.write(guard={}, dispatch={}, usage={})
        local = self.local()
        self.assertEqual((local["hook_shape"], local["dispatch_hook"], local["usage_hook"]),
                         ("ok", "registered", "registered"))
        self.assertIsNone(doctor.guard_shape_problem(cwd=self.local_dir))
        self.assertIsNone(doctor.dispatch_warning(cwd=self.local_dir))
        self.assertIsNone(doctor.usage_warning(cwd=self.local_dir))
        self.assertIsNone(doctor.hook_location_warning(cwd=self.local_dir))

    def test_a_guard_entry_without_type_command_is_reported(self):
        self.write(guard={"type": "prompt"}, dispatch={})
        self.assertIn("type 'prompt'", self.local()["hook_shape"])
        problem = doctor.guard_shape_problem(cwd=self.local_dir)
        self.assertIn(str(self.local_path), problem)
        self.assertIn("ppl setup claude", problem)

    def test_a_guard_entry_without_a_timeout_is_reported(self):
        self.write(guard={"timeout": None}, dispatch={})
        self.assertIn("no timeout", self.local()["hook_shape"])
        self.assertIn("no timeout", doctor.guard_shape_problem(cwd=self.local_dir))

    def test_an_interpreter_below_the_floor_is_left_to_the_hook_run(self):
        # the shape check reads the file only, the version is asked where doctor runs the hook
        old = self.fake_python("3.9.6")
        hook = str(fixtures.SOURCE / "hooks" / "ppl_guard.py")
        self.write(guard={"command": "%s %s" % (shlex.quote(old), shlex.quote(hook))}, dispatch={})
        self.assertEqual(self.local()["hook_shape"], "ok")
        self.assertIsNone(doctor.guard_shape_problem(cwd=self.local_dir))

    def test_a_guard_whose_interpreter_is_not_an_executable_file_is_reported(self):
        hook = str(fixtures.SOURCE / "hooks" / "ppl_guard.py")
        self.write(guard={"command": "/nonexistent/python3 %s" % hook}, dispatch={})
        self.assertIn("/nonexistent/python3", self.local()["hook_shape"])
        self.assertIn("/nonexistent/python3", doctor.guard_shape_problem(cwd=self.local_dir))

    def test_a_guard_whose_interpreter_is_not_an_absolute_path_is_reported(self):
        hook = str(fixtures.SOURCE / "hooks" / "ppl_guard.py")
        self.write(guard={"command": "python3 %s" % hook}, dispatch={})
        self.assertIn("absolute", self.local()["hook_shape"])
        self.assertIn("python3", doctor.guard_shape_problem(cwd=self.local_dir))

    def test_a_guard_whose_script_is_missing_is_reported(self):
        self.write(guard={"command": "%s /nonexistent/hooks/ppl_guard.py"
                          % shlex.quote(sys.executable)}, dispatch={})
        self.assertIn("/nonexistent/hooks/ppl_guard.py", self.local()["hook_shape"])

    def test_a_guard_registered_at_the_old_hooks_path_names_ppl_setup_claude(self):
        old = os.path.join(tmphome.mkdtemp(prefix="oldroot-"), "hooks", "ppl_guard.py")
        self.write(guard={"command": "%s %s" % (shlex.quote(sys.executable), shlex.quote(old))},
                   dispatch={})
        self.assertIn("ppl setup claude", doctor.guard_shape_problem(cwd=self.local_dir))

    def test_a_guard_run_as_a_bare_script_is_reported(self):
        self.write(guard={"command": str(fixtures.SOURCE / "hooks" / "ppl_guard.py")}, dispatch={})
        self.assertIn("names no ppl_guard.py script", self.local()["hook_shape"])

    def test_inspecting_the_settings_never_runs_the_interpreter_they_name(self):
        marker = Path(tmphome.mkdtemp(prefix="pplmarker-")) / "ran"
        script = Path(tmphome.mkdtemp(prefix="pplfakepython-")) / "python3"
        script.write_text("#!/bin/sh\ntouch %s\necho 3.14.0\n" % shlex.quote(str(marker)),
                          encoding="utf-8")
        script.chmod(0o755)
        for event, hook in (("guard", "ppl_guard.py"), ("dispatch", "ppl_dispatch.py")):
            command = "%s %s" % (script, fixtures.SOURCE / "hooks" / hook)
            self.write(**{"guard": {}, "dispatch": {}, event: {"command": command}})
            self.assertEqual(self.local()["hook_shape"], "ok")
            self.assertIsNone(doctor.guard_shape_problem(cwd=self.local_dir))
            self.assertIsNone(doctor.dispatch_warning(cwd=self.local_dir))
        self.assertFalse(marker.exists())

    def test_a_missing_dispatch_hook_is_reported(self):
        self.write(guard={})
        local = self.local()
        self.assertEqual((local["hook_shape"], local["dispatch_hook"]), ("ok", "missing"))
        warning = doctor.dispatch_warning(cwd=self.local_dir)
        self.assertIn("ppl_dispatch.py", warning)
        self.assertIn("ppl setup claude", warning)

    def test_a_dispatch_hook_in_the_global_file_counts_for_a_local_guard(self):
        self.write(guard={})
        self.write(dispatch={}, path=self.global_path)
        self.assertEqual(doctor.inspect(cwd=self.local_dir)["global"]["dispatch_hook"], "registered")
        self.assertIsNone(doctor.dispatch_warning(cwd=self.local_dir))

    def test_a_malformed_dispatch_entry_is_a_warning_not_a_guard_problem(self):
        self.write(guard={}, dispatch={"type": "prompt"})
        self.assertIn("ppl_dispatch.py", self.local()["hook_shape"])
        self.assertIsNone(doctor.guard_shape_problem(cwd=self.local_dir))
        self.assertIn("type 'prompt'", doctor.dispatch_warning(cwd=self.local_dir))

    def test_no_hooks_at_all_is_neither_a_shape_problem_nor_a_dispatch_warning(self):
        inspected = doctor.inspect(cwd=self.local_dir)
        self.assertEqual(inspected["local"]["dispatch_hook"], "missing")
        self.assertIsNone(inspected["local"]["hook_shape"])
        self.assertIsNone(doctor.guard_shape_problem(cwd=self.local_dir))
        self.assertIsNone(doctor.dispatch_warning(cwd=self.local_dir))

    def test_an_unreadable_file_reports_neither(self):
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_text("not json", encoding="utf-8")
        local = self.local()
        self.assertIsNone(local["dispatch_hook"])
        self.assertIsNone(local["usage_hook"])
        self.assertIsNone(local["hook_shape"])

    def test_the_report_says_whether_the_usage_hook_is_registered(self):
        self.write(guard={}, dispatch={})
        self.write(guard={}, dispatch={}, usage={}, path=self.global_path)
        with mock.patch.object(doctor, "hook_runs", return_value={"ok": True, "problem": None}):
            result = doctor.report(cwd=self.local_dir)
        self.assertEqual(result["local"]["usage_hook"], "missing")
        self.assertEqual(result["global"]["usage_hook"], "registered")
        self.global_path.unlink()
        self.assertEqual(self.local()["usage_hook"], "missing")
        self.assertEqual(doctor.inspect(cwd=self.local_dir)["global"]["usage_hook"], "missing")

    def test_usage_warning_when_the_guard_is_registered_without_it(self):
        self.write(guard={}, dispatch={})
        warning = doctor.usage_warning(cwd=self.local_dir)
        self.assertIn("ppl_usage.py", warning)
        self.assertIn("SubagentStop", warning)
        self.assertIn("tokens are not recorded", warning)
        self.assertIn("ppl setup claude", warning)
        self.write(guard={}, dispatch={}, usage={"type": "prompt"})
        self.assertIn("type 'prompt'", doctor.usage_warning(cwd=self.local_dir))
        self.assertIn("ppl_usage.py", self.local()["hook_shape"])
        self.assertIsNone(doctor.guard_shape_problem(cwd=self.local_dir))
        self.write(guard={}, dispatch={}, usage={})
        self.assertIsNone(doctor.usage_warning(cwd=self.local_dir))
        # no guard registered at all: nothing to warn about
        self.write(dispatch={})
        self.assertIsNone(doctor.usage_warning(cwd=self.local_dir))

    def test_a_hook_outside_the_assets_hooks_directory_is_warned(self):
        elsewhere = Path(tmphome.mkdtemp(prefix="pplold-")) / "elsewhere" / "ppl_guard.py"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("", encoding="utf-8")
        self.write(guard={"command": "%s %s" % (shlex.quote(sys.executable),
                                                shlex.quote(str(elsewhere)))},
                   dispatch={}, usage={})
        self.assertEqual(self.local()["hook_shape"], "ok")
        warning = doctor.hook_location_warning(cwd=self.local_dir)
        self.assertIn(str(elsewhere), warning)
        # the warning names the resolved directory, a symlinked checkout included
        self.assertIn(str((paths.root() / "hooks").resolve()), warning)
        self.assertIn("ppl setup claude", warning)
        self.assertNotIn("ppl_dispatch.py", warning)

    def test_a_symlink_loop_in_a_hook_path_does_not_crash_the_warning(self):
        # ppl init prints this warning after the run exists, so it must not raise
        loop = Path(tmphome.mkdtemp(prefix="pplloop-")) / "loop"
        os.symlink(str(loop), str(loop))
        script = loop / "ppl_guard.py"
        self.write(guard={"command": "%s %s" % (shlex.quote(sys.executable),
                                                shlex.quote(str(script)))},
                   dispatch={}, usage={})
        real = Path.resolve
        # Python 3.11 and 3.12 raise RuntimeError on a loop, a strict resolve OSError
        for error in (RuntimeError("Symlink loop from %r" % str(loop)),
                      OSError(62, "Too many levels of symbolic links")):
            def resolve(path, *args, **kwargs):
                if str(path).startswith(str(loop)):
                    raise error
                return real(path, *args, **kwargs)
            with self.subTest(error=type(error).__name__), \
                    mock.patch.object(Path, "resolve", resolve):
                self.assertIsNone(doctor.hook_location_warning(cwd=self.local_dir))

    def test_hooks_from_this_install_raise_no_location_warning(self):
        self.assertEqual(fixtures.SOURCE.resolve(), paths.root().resolve())
        self.write(guard={}, dispatch={}, usage={})
        self.assertIsNone(doctor.hook_location_warning(cwd=self.local_dir))
        self.write(guard={}, dispatch={}, usage={}, path=self.global_path)
        self.assertIsNone(doctor.hook_location_warning(cwd=self.local_dir))

    def test_the_report_carries_both_per_settings_file(self):
        self.write(guard={"timeout": None})
        with mock.patch.object(doctor, "hook_runs", return_value={"ok": True, "problem": None}):
            result = doctor.report(cwd=self.local_dir)
        self.assertEqual(result["local"]["dispatch_hook"], "missing")
        self.assertIn("no timeout", result["local"]["hook_shape"])
        self.assertEqual(result["global"]["dispatch_hook"], "missing")


class TestMcpServersWarning(unittest.TestCase):
    TOKEN = "ghp_FAKETOKEN0123456789"

    def setUp(self):
        self.home = Path(tmphome.mkdtemp(prefix="pplhome-"))
        self.claude = Path(tmphome.mkdtemp(prefix="pplclaude-"))
        self.cwd = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplcwd-")))
        self.user_json = self.home / "dot-claude.json"
        patcher = mock.patch.dict(os.environ, {"PPL_HOME": str(self.home),
                                               "PPL_CLAUDE_HOME": str(self.claude)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def facts(self):
        self.write(self.home / "os_sandbox.json", {"version": 1})

    def warning(self):
        return doctor.mcp_servers_warning(cwd=str(self.cwd), claude_json=self.user_json)

    def test_mcp_warning_lists_workspace_and_user_servers_by_name_only(self):
        self.facts()
        self.write(self.cwd / ".mcp.json", {"mcpServers": {"serena": {
            "command": "uvx", "args": ["serena", "--token", self.TOKEN]}}})
        self.write(self.user_json, {"projects": {str(self.cwd): {"mcpServers": {
            "jetbrains": {"command": "npx", "env": {"TOKEN": self.TOKEN}}}}},
            "mcpServers": {"globalone": {"command": "node"}}})
        self.write(self.claude / "settings.json", {"mcpServers": {"fromuser": {"command": "x"}}})
        self.write(self.cwd / ".claude" / "settings.local.json",
                   {"mcpServers": {"fromlocal": {"command": "x"}}})
        warning = self.warning()
        for name in ("serena", "jetbrains", "globalone", "fromuser", "fromlocal"):
            self.assertIn(name, warning)
        self.assertIn("outside the OS sandbox", warning)
        for secret in (self.TOKEN, "uvx", "npx", "--token", "node"):
            self.assertNotIn(secret, warning)
        self.assertNotIn("\n", warning)

    def test_no_mcp_warning_without_facts(self):
        self.write(self.cwd / ".mcp.json", {"mcpServers": {"serena": {"command": "uvx"}}})
        self.assertIsNone(self.warning())

    def test_no_mcp_warning_without_servers(self):
        self.facts()
        self.write(self.cwd / ".mcp.json", {"mcpServers": {}})
        self.write(self.user_json, {"projects": {"/elsewhere": {"mcpServers": {"x": {}}}}})
        self.assertIsNone(self.warning())

    def test_the_scan_follows_the_configured_workspace_not_the_process_cwd(self):
        self.facts()
        self.write(self.cwd / ".mcp.json", {"mcpServers": {"serena": {"command": "uvx"}}})
        elsewhere = Path(tmphome.mkdtemp(prefix="pplelsewhere-"))
        env = {key: value for key, value in os.environ.items()
               if key != "PPL_LOCAL_SETTINGS_DIR"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(doctor, "workspace", lambda: str(self.cwd)), \
                mock.patch("os.getcwd", lambda: str(elsewhere)):
            self.assertIn("serena", doctor.mcp_servers_warning(claude_json=self.user_json))

    def test_doctor_report_carries_mcp_server_names(self):
        self.write(self.cwd / ".mcp.json", {"mcpServers": {"serena": {"args": [self.TOKEN]}}})
        result = doctor.report(cwd=str(self.cwd), env={"PATH": "/usr/bin"},
                               probe=lambda: "unknown")
        self.assertEqual(result["mcp_servers"], ["serena"])
        self.assertNotIn(self.TOKEN, json.dumps(result))


if __name__ == "__main__":
    unittest.main()


class TestForeignBashHooks(unittest.TestCase):
    """Another PreToolUse hook that sees Bash may rewrite a call with updatedInput, and
    whether that keeps or drops dangerouslyDisableSandbox is unverified, so doctor names
    every such hook, by its first word or script name only."""

    def setUp(self):
        self.home = Path(tmphome.mkdtemp(prefix="pplhome-"))
        self.claude = Path(tmphome.mkdtemp(prefix="pplclaude-"))
        self.cwd = Path(tmphome.mkdtemp(prefix="pplcwd-"))
        self.managed = self.home / "managed-settings.json"
        for patcher in (mock.patch.dict(os.environ, {"PPL_HOME": str(self.home),
                                                     "PPL_CLAUDE_HOME": str(self.claude)}),
                        mock.patch.object(doctor, "managed_settings_path",
                                          lambda: self.managed)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def pre_tool_use(self, *entries):
        return {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [
            {"type": "command", "command": command}]} for matcher, command in entries]}}

    def plugin(self, name, entries, enabled=True):
        root = self.claude / "plugins" / "cache" / name
        self.write(root / "hooks" / "hooks.json", self.pre_tool_use(*entries))
        registry = self.claude / "plugins" / "installed_plugins.json"
        doc = json.loads(registry.read_text()) if registry.exists() else {"version": 2,
                                                                          "plugins": {}}
        doc["plugins"][name] = [{"scope": "user", "installPath": str(root)}]
        self.write(registry, doc)
        user = doctor.global_settings_path()
        settings = json.loads(user.read_text()) if user.exists() else {}
        settings.setdefault("enabledPlugins", {})[name] = enabled
        self.write(user, settings)

    def found(self):
        return [(Path(item["source"]).name, item["hook"])
                for item in doctor.foreign_bash_hooks(str(self.cwd))]

    def test_every_source_is_read_and_only_names_are_reported(self):
        self.write(doctor.global_settings_path(), self.pre_tool_use(
            ("Bash", "sqz compress --token s3cr3t"), (".*", GUARD_COMMAND)))
        self.write(self.cwd / ".claude" / "settings.json", self.pre_tool_use(
            ("Bash|Read", "/usr/bin/python3 /opt/hooks/rewrite.py --key abc")))
        self.write(self.cwd / ".claude" / "settings.local.json", self.pre_tool_use(
            ("", "env FOO=1 rtk hook")))
        self.write(self.managed, self.pre_tool_use(("*", "bash /etc/corp/audit.sh")))
        found = self.found()
        self.assertIn(("settings.json", "sqz"), found)
        self.assertIn(("settings.json", "rewrite.py"), found)
        self.assertIn(("settings.local.json", "rtk"), found)
        self.assertIn(("managed-settings.json", "audit.sh"), found)
        self.assertEqual(len(found), 4)
        text = json.dumps(doctor.foreign_bash_hooks(str(self.cwd)))
        for secret in ("s3cr3t", "--key", "abc", "FOO=1", "ppl_guard"):
            self.assertNotIn(secret, text)

    def test_a_hook_whose_matcher_misses_bash_is_not_reported(self):
        self.write(doctor.global_settings_path(), self.pre_tool_use(
            ("Edit|Write", "fmt-hook"), ("mcp__.*", "mcp-audit"), ("Bash", "sqz")))
        self.assertEqual(self.found(), [("settings.json", "sqz")])

    def test_an_enabled_plugins_hooks_are_reported(self):
        self.plugin("lint@market", [("Bash", "${CLAUDE_PLUGIN_ROOT}/hooks/check.sh x")])
        self.plugin("off@market", [("Bash", "offhook")], enabled=False)
        self.assertEqual(self.found(), [("hooks.json", "check.sh")])

    def test_the_report_and_the_warning_name_them(self):
        self.write(doctor.global_settings_path(), self.pre_tool_use(("Bash", "sqz compress")))
        result = doctor.report(cwd=str(self.cwd), env={"PATH": "/usr/bin"},
                               probe=lambda: "unknown")
        self.assertEqual([item["hook"] for item in result["foreign_bash_hooks"]], ["sqz"])
        warning = doctor.foreign_bash_hooks_warning(str(self.cwd))
        self.assertIn("sqz", warning)
        self.assertIn("remove or disable", warning)
        self.assertIn("before a GitHub run", warning)


class TestOsSandboxFacts(unittest.TestCase):
    BLOCK = {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": True}

    def setUp(self):
        self.home = Path(tmphome.mkdtemp(prefix="pplhome-"))
        self.claude = Path(tmphome.mkdtemp(prefix="pplclaude-"))
        self.cwd = Path(tmphome.mkdtemp(prefix="pplcwd-"))
        self.managed = self.home / "managed-settings.json"
        for patcher in (mock.patch.dict(os.environ, {"PPL_HOME": str(self.home),
                                                     "PPL_CLAUDE_HOME": str(self.claude)}),
                        mock.patch.object(doctor, "managed_settings_path",
                                          lambda: self.managed)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_facts(self, **extra):
        facts = {"version": 1, "write_scope": [], "written_sandbox": self.BLOCK,
                 "pins": {"gh": {"path": "/opt/gh/bin/gh", "realpath": "/opt/gh/bin/gh"}}}
        facts.update(extra)
        (self.home / "os_sandbox.json").write_text(json.dumps(facts), encoding="utf-8")

    def write_settings(self, path, doc):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def set_profile(self):
        self.write_facts()
        self.write_settings(self.cwd / ".claude" / "settings.local.json", {"sandbox": self.BLOCK})

    def facts(self, probe="denied", env=None):
        env = {"SANDBOX_RUNTIME": "1", "PATH": "/usr/bin"} if env is None else env
        return doctor.os_sandbox_facts(cwd=str(self.cwd), env=env, probe=lambda: probe)

    def test_on_needs_marker_profile_and_a_denied_probe(self):
        self.set_profile()
        facts = self.facts()
        self.assertEqual(facts["state"], "on", facts)
        self.assertEqual((facts["env_marker"], facts["profile"], facts["live"]),
                         (True, True, "denied"))
        self.assertEqual(facts["undoing"], [])
        for missing in ({"SANDBOX_RUNTIME": "0"}, {}):
            self.assertEqual(self.facts(env=missing)["state"], "off")
        (self.cwd / ".claude" / "settings.local.json").write_text(
            json.dumps({"sandbox": dict(self.BLOCK, enabled=False)}), encoding="utf-8")
        self.assertFalse(self.facts()["profile"])
        self.assertEqual(self.facts()["state"], "off")

    def test_an_open_probe_is_off(self):
        self.set_profile()
        facts = self.facts(probe="open")
        self.assertEqual((facts["live"], facts["state"]), ("open", "off"))

    def test_native_windows_is_unavailable(self):
        self.set_profile()
        with mock.patch.object(doctor, "_native_windows", lambda: True):
            self.assertEqual(self.facts()["state"], "unavailable")

    def test_excluded_commands_in_user_settings_undo_the_boundary(self):
        self.set_profile()
        path = doctor.global_settings_path()
        self.write_settings(path, {"sandbox": {"excludedCommands": ["docker *"]}})
        facts = self.facts()
        self.assertEqual(facts["state"], "off")
        self.assertEqual(len(facts["undoing"]), 1)
        self.assertIn(str(path), facts["undoing"][0])
        self.assertIn("sandbox.excludedCommands", facts["undoing"][0])

    def test_project_local_and_managed_settings_are_scanned_too(self):
        self.set_profile()
        self.write_settings(self.cwd / ".claude" / "settings.json",
                            {"sandbox": {"enabled": False}})
        self.write_settings(self.managed, {"sandbox": {"excludedCommands": ["x"]}})
        undoing = " ".join(self.facts()["undoing"])
        self.assertIn(str(self.cwd / ".claude" / "settings.json"), undoing)
        self.assertIn("sandbox.enabled", undoing)
        self.assertIn(str(self.managed), undoing)

    def test_allow_unsandboxed_false_is_reported(self):
        self.set_profile()
        self.write_settings(doctor.global_settings_path(),
                            {"sandbox": {"allowUnsandboxedCommands": False}})
        facts = self.facts()
        self.assertIn("sandbox.allowUnsandboxedCommands", facts["undoing"][0])
        self.assertEqual(facts["state"], "off")

    def test_another_sources_write_grant_over_a_protected_path_is_undoing(self):
        self.set_profile()
        self.write_settings(doctor.global_settings_path(),
                            {"sandbox": {"filesystem": {"allowWrite": [str(self.claude),
                                                                       "/var/cache/x"]}}})
        self.write_settings(self.cwd / ".claude" / "settings.json",
                            {"permissions": {"additionalDirectories": [str(self.home)]}})
        facts = self.facts()
        undoing = " ".join(facts["undoing"])
        self.assertEqual(facts["state"], "off")
        self.assertIn("sandbox.filesystem.allowWrite %s" % self.claude, undoing)
        self.assertIn("permissions.additionalDirectories %s" % self.home, undoing)
        self.assertNotIn("/var/cache/x", undoing)
        self.assertEqual(len(facts["undoing"]), 2)

    def test_a_write_grant_under_a_protected_path_is_undoing(self):
        self.set_profile()
        self.write_settings(self.managed, {"sandbox": {"filesystem": {
            "allowWrite": [str(self.home / "harnesses" / "mine")]}}})
        self.assertIn("allowWrite", " ".join(self.facts()["undoing"]))

    def test_unix_socket_allowances_are_undoing(self):
        self.set_profile()
        self.write_settings(doctor.global_settings_path(), {"sandbox": {"network": {
            "allowUnixSockets": ["/var/run/docker.sock"]}}})
        self.write_settings(self.managed, {"sandbox": {"network": {"allowAllUnixSockets": True}}})
        undoing = " ".join(self.facts()["undoing"])
        self.assertIn("sandbox.network.allowUnixSockets", undoing)
        self.assertIn("sandbox.network.allowAllUnixSockets", undoing)

    def test_ppls_own_block_is_not_undoing(self):
        block = dict(self.BLOCK, filesystem={"allowWrite": [str(self.home / "runs")],
                                             "denyWrite": [str(self.cwd)]})
        self.write_facts(written_sandbox=block)
        self.write_settings(self.cwd / ".claude" / "settings.local.json", {"sandbox": block})
        self.assertEqual(self.facts()["undoing"], [])

    def test_a_github_token_in_the_environment_is_named_never_its_value(self):
        env = {"GH_TOKEN": "gho_secretvalue1", "GITHUB_TOKEN": "ghp_secretvalue2",
               "GH_ENTERPRISE_TOKEN": "", "OTHER": "x"}
        self.assertEqual(doctor.github_token_names(env), ["GH_TOKEN", "GITHUB_TOKEN"])
        warning = doctor.github_token_warning(env)
        self.assertIn("GH_TOKEN, GITHUB_TOKEN", warning)
        self.assertNotIn("secretvalue", warning)
        self.assertIsNone(doctor.github_token_warning({"PATH": "/usr/bin"}))
        result = doctor.report(cwd=str(self.cwd), env=dict(env, PATH="/usr/bin"),
                               probe=lambda: "unknown")
        self.assertEqual(result["github_token_env"], ["GH_TOKEN", "GITHUB_TOKEN"])
        self.assertNotIn("secretvalue", json.dumps(result))

    def test_a_variable_holding_a_github_token_is_named_whatever_its_name(self):
        value = "github_pat_11ABCDEFG0secretvalue"
        env = {"FOO": value, "BAR": "ordinary", "GITHUB_ENTERPRISE_TOKEN": "x",
               "BAZ": "ghs_secretvalue3", "QUX": "prefix ghp_notatstart", "PATH": "/usr/bin"}
        self.assertEqual(doctor.github_token_names(env),
                         ["GITHUB_ENTERPRISE_TOKEN", "BAZ", "FOO"])
        warning = doctor.github_token_warning(env)
        self.assertIn("FOO", warning)
        self.assertNotIn("BAR", warning)
        self.assertNotIn("QUX", warning)
        for prefix in ("ghp_", "ghs_", "github_pat_", "secretvalue"):
            self.assertNotIn(prefix, warning)
        result = doctor.report(cwd=str(self.cwd), env=env, probe=lambda: "unknown")
        self.assertIn("FOO", result["github_token_env"])
        self.assertNotIn("secretvalue", json.dumps(result))
        for prefix in ("ghu_", "gho_", "ghr_", "ghp_"):
            self.assertEqual(doctor.github_token_names({"V": prefix + "abc"}), ["V"])

    def test_a_pin_inside_the_write_scope_is_exposed(self):
        scope = tmphome.mkdtemp(prefix="pplscope-")
        pin = os.path.join(scope, "bin", "gh")
        self.write_facts(write_scope=[scope], pins={"gh": {"path": pin, "realpath": pin}})
        facts = self.facts(env={"SANDBOX_RUNTIME": "1", "PATH": "/usr/bin"})
        self.assertIn(pin, facts["exposed"])
        self.assertNotIn("/usr/bin", facts["exposed"])
        facts = self.facts(env={"SANDBOX_RUNTIME": "1", "PATH": scope + os.pathsep + "/usr/bin"})
        self.assertIn(scope, facts["exposed"])

    def test_the_proxy_url_is_never_in_the_report(self):
        self.set_profile()
        env = {"SANDBOX_RUNTIME": "1", "PATH": "/usr/bin",
               "HTTPS_PROXY": "http://user:secret@localhost:1"}
        result = doctor.report(cwd=str(self.cwd), env=env, probe=lambda: "unknown")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("localhost:1", json.dumps(result))

    def test_the_default_probe_reads_the_status_line_through_the_proxy(self):
        import socket
        import threading
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        seen = []

        def serve(status):
            conn, _ = server.accept()
            seen.append(conn.recv(4096).decode("latin-1"))
            conn.sendall(status)
            conn.close()

        port = server.getsockname()[1]
        env = {"HTTPS_PROXY": "http://user:secret@127.0.0.1:%d" % port}
        thread = threading.Thread(target=serve, args=(b"HTTP/1.1 403 Forbidden\r\n\r\n",),
                                  daemon=True)
        thread.start()
        self.assertEqual(doctor.probe_github(env), "denied")
        thread.join()
        server.close()
        self.assertIn("CONNECT api.github.com:443 HTTP/1.1", seen[0])
        self.assertIn("Proxy-Authorization: Basic ", seen[0])
        self.assertEqual(doctor.probe_github({}), "unknown")
        self.assertEqual(doctor.probe_github({"HTTPS_PROXY": "http://127.0.0.1:1"}), "unknown")

    def test_gh_checks_are_skipped_inside_the_sandbox(self):
        fixtures.install_routing()
        fixtures.install_harness("example-maven")
        config.ensure()
        config.set_active_harness("example-maven")
        with mock.patch.object(doctor, "_gh_kinds", lambda doc: (True, True)):
            skipped = doctor.inspect_setup(env={"SANDBOX_RUNTIME": "1"})
        detail = "skipped inside the OS sandbox, run ppl doctor --forge outside it"
        self.assertEqual(skipped["forge"]["ok"], None)
        self.assertEqual(skipped["forge"]["detail"], detail)
        self.assertEqual(skipped["tracker"]["ok"], None)
        self.assertEqual(skipped["tracker"]["detail"], detail)

    def test_doctor_checks_run_the_pinned_gh(self):
        from ppl.forges import github as github_forge
        pin = "/opt/pinned/gh"
        self.write_facts(pins={"gh": {"path": pin, "realpath": pin}})
        argv = []

        def runner(cmd, **kwargs):
            argv.append(cmd[0])
            return subprocess.CompletedProcess(cmd, 0, stdout="gh version 2\n", stderr="")

        github_forge.doctor_check({}, None, None, runner=runner)
        github_issues.doctor_check({"repo": "o/r", "task_prefix": "GH"}, runner=runner)
        self.assertTrue(argv)
        self.assertEqual(set(argv), {pin})
        (self.home / "os_sandbox.json").unlink()
        del argv[:]
        github_forge.doctor_check({}, None, None, runner=runner)
        self.assertEqual(set(argv), {"gh"})

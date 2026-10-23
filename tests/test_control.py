import os
import pathlib
import subprocess
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import control, run, status, store
from ppl.errors import PplError


class TestControl(unittest.TestCase):
    def test_no_control_file_means_no_op(self):
        run.init("DEMO-400", "u", "/tmp/repo")
        self.assertIsNone(control.read("DEMO-400"))
        control.enforce("DEMO-400")

    def test_pause_blocks_with_owner_decision(self):
        run.init("DEMO-401", "u", "/tmp/repo")
        control.set_command("DEMO-401", "pause")
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-401")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_resume_clears_the_pause(self):
        run.init("DEMO-402", "u", "/tmp/repo")
        control.set_command("DEMO-402", "pause")
        control.set_command("DEMO-402", "resume")
        control.enforce("DEMO-402")

    def test_unknown_command_is_refused(self):
        run.init("DEMO-403", "u", "/tmp/repo")
        with self.assertRaises(PplError):
            control.set_command("DEMO-403", "selfdestruct")

    def test_destroy_requires_a_scope(self):
        # rule: R-DESTROY-SCOPE
        run.init("DEMO-404", "u", "/tmp/repo")
        with self.assertRaises(PplError) as ctx:
            control.set_command("DEMO-404", "destroy")
        self.assertIn("scope", ctx.exception.message)

    def test_destroy_accepts_local_scope(self):
        run.init("DEMO-405", "u", "/tmp/repo")
        record = control.set_command("DEMO-405", "destroy", scope="local")
        self.assertEqual(record["scope"], "local")

    def test_stop_writes_a_parking_record(self):
        run.init("DEMO-406", "u", "/tmp/repo")
        run.stage_enter("DEMO-406", "baseline")
        control.set_command("DEMO-406", "stop")
        from ppl import paths, store
        record = store.read_json(paths.run_dir("DEMO-406") / "parking.json")
        self.assertEqual(record["blocker"], "OWNER_DECISION")
        self.assertEqual(record["resume_stage"], "baseline")


class TestDestroy(unittest.TestCase):
    def test_local_destroy_archives_the_run_and_clears_active(self):
        run.init("DEMO-430", "u", "/tmp/repo")
        result = run.destroy("DEMO-430", "local")
        self.assertEqual(result["status"], "archived")
        from ppl import paths
        self.assertFalse((paths.run_dir("DEMO-430") / "state.json").exists())
        self.assertTrue(any(paths.runs_dir().glob("_archive/DEMO-430-*")))
        self.assertIsNone(run.active())

    def test_full_destroy_queues_remote_cleanup_and_keeps_the_run(self):
        # rule: R-DESTROY-REMOTE-QUEUED
        run.init("DEMO-431", "u", "/tmp/repo")
        result = run.destroy("DEMO-431", "full")
        self.assertEqual(result["status"], "queued_remote_cleanup")
        from ppl import paths, store
        request = store.read_json(paths.run_dir("DEMO-431") / "remote_cleanup.json")
        self.assertEqual(request["scope"], "full")
        self.assertTrue((paths.run_dir("DEMO-431") / "state.json").exists())

    def test_unknown_scope_is_refused(self):
        run.init("DEMO-432", "u", "/tmp/repo")
        with self.assertRaises(PplError):
            run.destroy("DEMO-432", "everything")

    def test_two_consecutive_local_destroys_do_not_collide(self):
        # store.now() has second resolution, so two destroys of the same
        # task id within the same second must not produce the same archive
        # directory name, or shutil.move would nest the second archive
        # inside the first instead of placing it beside it.
        run.init("DEMO-433", "u", "/tmp/repo")
        first = run.destroy("DEMO-433", "local")
        run.init("DEMO-433", "u", "/tmp/repo")
        second = run.destroy("DEMO-433", "local")
        self.assertNotEqual(first["archive"], second["archive"])
        from ppl import paths
        archives = sorted(paths.runs_dir().glob("_archive/DEMO-433-*"))
        self.assertEqual(len(archives), 2)
        for archive in archives:
            self.assertTrue((archive / "state.json").exists())


class TestPark(unittest.TestCase):
    def test_park_writes_a_full_record(self):
        run.init("DEMO-410", "u", "/tmp/repo")
        run.stage_enter("DEMO-410", "baseline")
        state = run.park("DEMO-410", "BASELINE_FAILURE", "resolve dependency then rerun", [])
        self.assertEqual(state["stage"], "parking")
        self.assertEqual(state["status"], "blocked")
        from ppl import store, paths
        record = store.read_json(paths.run_dir("DEMO-410") / "parking.json")
        self.assertEqual(record["blocker"], "BASELINE_FAILURE")
        self.assertEqual(record["resume_stage"], "baseline")
        self.assertEqual(record["safe_next_action"], "resolve dependency then rerun")

    def test_a_second_park_preserves_the_original_resume_stage(self):
        # rule: R-PARK-PRESERVES-RESUME-STAGE (N1, cycle 2 wave B). park() read
        # state["stage"] to fill resume_stage, then set state["stage"] to
        # "parking" - reachable via `ppl park` followed by an operator stop
        # (control.set_command("stop") calls run.park again while the run is
        # already parked): the second call recorded resume_stage as
        # "parking" itself, losing the real resume point. A second park must
        # not erase the first one's resume_stage.
        run.init("DEMO-412", "u", "/tmp/repo")
        run.stage_enter("DEMO-412", "baseline")
        run.park("DEMO-412", "BASELINE_FAILURE", "resolve dependency then rerun", [])
        run.park("DEMO-412", "OWNER_DECISION", "operator stopped the run", [])
        from ppl import paths, store
        record = store.read_json(paths.run_dir("DEMO-412") / "parking.json")
        self.assertEqual(record["resume_stage"], "baseline")
        self.assertEqual(record["blocker"], "OWNER_DECISION")

    def test_park_rejects_an_unknown_blocker_code(self):
        # rule: R-PARK-BLOCKER-CODE
        run.init("DEMO-411", "u", "/tmp/repo")
        with self.assertRaises(PplError):
            run.park("DEMO-411", "IT_BROKE", "shrug", [])


class TestControlFailsClosed(unittest.TestCase):
    """control.json is the one carve-out to "only ppl writes under runs/": an
    external panel writes it, so it must fail closed (halt) rather than be
    ignored or crash when it is malformed. A halt control that fails open is
    worse than no control at all.
    """

    def _write_raw(self, task_id, text):
        from ppl import paths
        path = paths.run_dir(task_id) / "control.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_enforce_halts_on_invalid_json_bytes(self):
        # rule: R-CONTROL-FAIL-CLOSED
        run.init("DEMO-440", "u", "/tmp/repo")
        self._write_raw("DEMO-440", "{not json")
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-440")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_enforce_halts_on_valid_json_non_dict(self):
        run.init("DEMO-441", "u", "/tmp/repo")
        self._write_raw("DEMO-441", "[1, 2, 3]")
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-441")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_enforce_halts_on_dict_with_no_command(self):
        run.init("DEMO-442", "u", "/tmp/repo")
        self._write_raw("DEMO-442", '{"scope": null}')
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-442")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_enforce_halts_on_unknown_command(self):
        run.init("DEMO-443", "u", "/tmp/repo")
        self._write_raw("DEMO-443", '{"command": "selfdestruct"}')
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-443")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_enforce_halts_on_missing_requested_at(self):
        # valid JSON, valid command, but missing the field enforce's
        # HALTING branch reads unguarded - same defect, different key
        run.init("DEMO-444", "u", "/tmp/repo")
        self._write_raw("DEMO-444", '{"command": "pause"}')
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-444")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_enforce_halts_on_destroy_without_scope(self):
        # set_command can never produce this shape (it requires scope in
        # SCOPES before writing), but a direct panel write bypassing
        # set_command could, and destroy with no scope is unactionable intent
        run.init("DEMO-445", "u", "/tmp/repo")
        self._write_raw("DEMO-445",
                        '{"command": "destroy", "requested_at": "2026-01-01T00:00:00+00:00"}')
        with self.assertRaises(PplError) as ctx:
            control.enforce("DEMO-445")
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")


class TestStatus(unittest.TestCase):
    def test_card_is_human_language_and_names_the_next_action(self):
        run.init("DEMO-420", "u", "/tmp/repo")
        text = status.card("DEMO-420")
        self.assertIn("DEMO-420", text)
        self.assertIn("Needs you:", text)
        self.assertIn("Next:", text)

    def test_card_reports_paused_state(self):
        run.init("DEMO-421", "u", "/tmp/repo")
        control.set_command("DEMO-421", "pause")
        self.assertIn("PAUSED", status.card("DEMO-421"))

    def test_card_renders_without_raising_on_invalid_control_file(self):
        run.init("DEMO-422", "u", "/tmp/repo")
        from ppl import paths
        path = paths.run_dir("DEMO-422") / "control.json"
        path.write_text("{not json", encoding="utf-8")
        text = status.card("DEMO-422")
        self.assertIn("DEMO-422", text)
        self.assertIn("Needs you:", text)

    def test_card_shows_no_cap_wording_by_default(self):
        run.init("DEMO-423", "u", "/tmp/repo")
        self.assertIn("usd 0.0 (no cap)", status.card("DEMO-423"))

    def test_card_shows_capped_wording_when_usd_max_is_set(self):
        run.init("DEMO-424", "u", "/tmp/repo")
        state = run.load("DEMO-424")
        state["budgets"]["usd_max"] = 50.0
        state["budgets"]["usd_used"] = 12.5
        run.save(state)
        self.assertIn("usd 12.5/50.0", status.card("DEMO-424"))


class TestStatusUsage(unittest.TestCase):
    PRICE = {"input": 1.0, "cache_write": 1.0, "cache_read": 1.0, "output": 1.0}

    def _usage(self, task, role, agent, model, **counts):
        full = {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "thinking": 0}
        full.update(counts)
        store.append_log(task, {"event": "usage", "role": role, "agent_id": agent,
                                "models": {model: full}})

    def _set(self, task, **budgets):
        state = run.load(task)
        state["budgets"].update(budgets)
        run.save(state)

    def test_the_card_shows_tokens_per_role(self):
        run.init("USG-430", "u", "/tmp/repo")
        self._usage("USG-430", "ppl-code-worker", "a1", "claude-x", input=300000, output=112000)
        self._usage("USG-430", "ppl-verifier", "a2", "claude-x", input=198000)
        self._usage("USG-430", "orchestrator", "orchestrator:s", "claude-x",
                    cache_read=1170000, output=30000)
        line = [row for row in status.card("USG-430").splitlines()
                if row.startswith("Tokens:")]
        self.assertEqual(line, ["Tokens:   code-worker 412k, verifier 198k, "
                                "orchestrator 1.2M (out 142k)"])

    def test_the_card_says_none_recorded_without_usage(self):
        run.init("USG-431", "u", "/tmp/repo")
        self.assertIn("Tokens:   none recorded", status.card("USG-431"))
        self.assertEqual(status.tokens_line("USG-431"), "none recorded")

    def test_token_numbers_are_formatted_short(self):
        run.init("USG-432", "u", "/tmp/repo")
        self._usage("USG-432", "ppl-a", "a1", "m", input=999)
        self._usage("USG-432", "ppl-b", "a2", "m", input=1200)
        self._usage("USG-432", "ppl-c", "a3", "m", input=999600)
        self.assertEqual(status.tokens_line("USG-432"),
                         "a 999, b 1.2k, c 1.0M (out 0)")

    def test_the_budget_line_shows_priced_dollars(self):
        run.init("USG-433", "u", "/tmp/repo")
        self._set("USG-433", usd_max=5.0, usd_used=0.2, prices={"claude-x": self.PRICE})
        self._usage("USG-433", "ppl-code-worker", "a1", "claude-x", input=1000000)
        self.assertIn("usd 1.2/5.0", status.card("USG-433"))
        self.assertEqual(status.usd_text("USG-433"), "usd 1.2/5.0")

    def test_the_budget_line_without_a_cap_says_no_cap(self):
        run.init("USG-434", "u", "/tmp/repo")
        self._set("USG-434", usd_used=0.2, prices={"claude-x": self.PRICE})
        self._usage("USG-434", "ppl-code-worker", "a1", "claude-x", input=1000000)
        self.assertEqual(status.usd_text("USG-434"), "usd 1.2 (no cap)")

    def test_the_budget_line_names_an_unpriced_model(self):
        run.init("USG-435", "u", "/tmp/repo")
        self._set("USG-435", prices={})
        self._usage("USG-435", "ppl-code-worker", "a1", "claude-x", input=10)
        self.assertIn("usd 0.0 + unpriced claude-x", status.card("USG-435"))

    def test_the_budget_line_does_not_pass_off_no_usage_as_measured(self):
        run.init("USG-436", "u", "/tmp/repo")
        self.assertEqual(status.usd_text("USG-436"), "usd 0.0 (no cap), no usage recorded")


class TestDestroyRemovesTheWorktree(unittest.TestCase):
    """I1: the removal used to run without -C, so git executed it against ppl's
    own cwd rather than the target repository, and the discarded exit code let
    the archive report success over an orphaned worktree.
    """

    def make_worktree(self):
        main = tmphome.mkdtemp(prefix="pplrepo-")
        subprocess.run(["git", "init", "-q", "-b", "master", main], check=True)
        pathlib.Path(main, "a.txt").write_text("one\n", encoding="utf-8")
        subprocess.run(["git", "-C", main, "add", "a.txt"], check=True)
        subprocess.run(["git", "-C", main, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-q", "-m", "base"], check=True)
        tree = os.path.join(tmphome.mkdtemp(prefix="pplwt-"), "work")
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", "-b", "work", tree],
                       check=True)
        return main, tree

    def test_a_real_worktree_is_actually_removed(self):
        main, tree = self.make_worktree()
        run.init("DEMO-460", "u", tree)
        result = run.destroy("DEMO-460", "local")
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["worktree"], "removed")
        self.assertNotIn("worktree_error", result)
        self.assertFalse(os.path.isdir(tree))
        listed = subprocess.run(["git", "-C", main, "worktree", "list"],
                                capture_output=True, text=True, check=True).stdout
        self.assertNotIn(tree, listed)

    def test_a_failed_removal_is_reported_rather_than_swallowed(self):
        not_a_worktree = tmphome.mkdtemp(prefix="pplrepo-")
        run.init("DEMO-461", "u", not_a_worktree)
        result = run.destroy("DEMO-461", "local")
        self.assertEqual(result["worktree"], "left_behind")
        self.assertTrue(result["worktree_error"])
        self.assertEqual(result["status"], "archived")

    def test_destroy_does_not_resurrect_the_run_directory_it_archived(self):
        from ppl import paths
        run.init("DEMO-462", "u", "/tmp/definitely-not-a-repo-xyz")
        run.destroy("DEMO-462", "local")
        self.assertFalse(paths.run_dir("DEMO-462").exists())


if __name__ == "__main__":
    unittest.main()

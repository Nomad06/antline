import os
import pathlib
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import control, lanes, paths, run, status, store, trail
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
    def test_local_destroy_archives_the_run_and_releases_the_lane(self):
        entry = fixtures.bind_lane("DEMO-430", "/tmp/repo")
        other = fixtures.bind_lane("DEMO-434", "/tmp/repo-b", lane=2)
        run.init("DEMO-430", "u", "/tmp/repo")
        result = run.destroy("DEMO-430", "local")
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["lane"], "released")
        self.assertFalse((paths.run_dir("DEMO-430") / "state.json").exists())
        self.assertTrue(any(paths.runs_dir().glob("_archive/DEMO-430-*")))
        self.assertIsNone(lanes.entry("DEMO-430"))
        self.assertFalse(os.path.lexists(entry["start_dir"]))
        self.assertEqual(lanes.entry("DEMO-434"), other)
        self.assertTrue(os.path.isdir(other["start_dir"]))
        self.assertFalse((paths.runs_dir() / "ACTIVE").exists())

    def test_local_destroy_of_a_run_whose_lane_was_closed_removes_its_start_dir(self):
        entry = fixtures.bind_lane("DEMO-435", "/tmp/repo")
        run.init("DEMO-435", "u", "/tmp/repo")
        lanes.release("DEMO-435")
        result = run.destroy("DEMO-435", "local")
        self.assertEqual((result["status"], result["lane"]), ("archived", "none"))
        self.assertFalse(os.path.lexists(entry["start_dir"]))

    def test_local_destroy_removes_the_scratch_dir(self):
        from ppl import os_sandbox
        run.init("DEMO-432", "u", "/tmp/repo")
        scratch = os_sandbox.scratch_dir("DEMO-432")
        scratch.mkdir(parents=True)
        (scratch / "install.log").write_text("x", encoding="utf-8")
        other = os_sandbox.scratch_dir("DEMO-433")
        other.mkdir(parents=True)
        run.destroy("DEMO-432", "local")
        self.assertFalse(scratch.exists())
        self.assertTrue(other.exists())

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

    def test_the_card_shows_the_os_sandbox_line(self):
        for task, on in (("GH-4211", True), ("GH-4221", False)):
            run.init(task, "u", "/tmp/repo", os_sandboxed=on)
            store.write_json(paths.run_dir(task) / "harness.json",
                             {"os_sandbox": {"needed": True}})
        self.assertIn("OS sandbox: on\n", status.card("GH-4211"))
        self.assertIn("OS sandbox: off\n", status.card("GH-4221"))
        run.init("DEMO-4299", "u", "/tmp/repo")
        self.assertNotIn("OS sandbox", status.card("DEMO-4299"))

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
        self.assertIn("usd 0.0 (no cap) + unpriced claude-x, not in config.toml [prices]",
                      status.card("USG-435"))

    def _live_prices(self, text):
        target = paths.config_file()
        if target.exists():
            self.addCleanup(target.write_text, target.read_text(encoding="utf-8"),
                            encoding="utf-8")
        else:
            self.addCleanup(target.unlink)
        target.write_text(text, encoding="utf-8")

    def test_an_uncapped_run_that_froze_no_prices_reads_the_live_prices(self):
        run.init("USG-437", "u", "/tmp/repo")
        self._set("USG-437", prices={})
        self._usage("USG-437", "ppl-code-worker", "a1", "claude-x", input=1000000)
        self._live_prices('[prices."claude-x"]\ninput = 3\ncache_write = 3\n'
                          'cache_read = 3\noutput = 3\n')
        self.assertEqual(status.usd_text("USG-437"), "usd 3.0 (no cap)")
        self.assertEqual(trail.measures(paths.run_dir("USG-437"))["usd"], 3.0)

    def test_a_capped_unpriced_line_keeps_the_cap_and_names_the_init_snapshot(self):
        run.init("USG-438", "u", "/tmp/repo")
        self._set("USG-438", usd_max=5.0, prices={})
        self._usage("USG-438", "ppl-code-worker", "a1", "claude-x", input=1000000)
        self._live_prices('[prices."claude-x"]\ninput = 3\ncache_write = 3\n'
                          'cache_read = 3\noutput = 3\n')
        self.assertEqual(status.usd_text("USG-438"),
                         "usd 0.0/5.0 + unpriced claude-x, not in the prices frozen at ppl init")

    def test_an_unreadable_live_price_table_reads_unknown(self):
        run.init("USG-439", "u", "/tmp/repo")
        self._set("USG-439", prices={})
        self._usage("USG-439", "ppl-code-worker", "a1", "claude-x", input=10)
        self._live_prices('[prices."claude-x"]\ninput = "x"\n')
        text = status.usd_text("USG-439")
        self.assertTrue(text.startswith("usd unknown (no cap), config.toml"), text)
        self.assertIn("prices.claude-x", text)

    def test_the_budget_line_does_not_pass_off_no_usage_as_measured(self):
        run.init("USG-436", "u", "/tmp/repo")
        self.assertEqual(status.usd_text("USG-436"), "usd 0.0 (no cap), no usage recorded")

    def test_a_float_count_prints_as_a_whole_number(self):
        run.init("USG-440", "u", "/tmp/repo")
        self._usage("USG-440", "ppl-a", "a1", "m", input=405.0, output=7.0)
        self.assertEqual(status.tokens_line("USG-440"), "a 412 (out 7)")


def name_as_the_legacy_run(test, task_id):
    """Write runs/ACTIVE naming `task_id`, as ppl init did before lanes, removed again after
    the test."""
    pointer = paths.legacy_active_file()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(task_id + "\n", encoding="utf-8")
    test.addCleanup(pointer.unlink, missing_ok=True)


class TestDestroyRemovesTheWorktree(unittest.TestCase):
    """I1: the removal used to run without -C, so git executed it against ppl's
    own cwd rather than the target repository, and the discarded exit code let
    the archive report success over an orphaned worktree. These runs are from
    before lanes: runs/ACTIVE names each, so destroy takes its state.json repo.
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
        name_as_the_legacy_run(self, "DEMO-460")
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
        name_as_the_legacy_run(self, "DEMO-461")
        result = run.destroy("DEMO-461", "local")
        self.assertEqual(result["worktree"], "left_behind")
        self.assertTrue(result["worktree_error"])
        self.assertEqual(result["status"], "archived")

    def test_destroy_does_not_resurrect_the_run_directory_it_archived(self):
        from ppl import paths
        run.init("DEMO-462", "u", "/tmp/definitely-not-a-repo-xyz")
        run.destroy("DEMO-462", "local")
        self.assertFalse(paths.run_dir("DEMO-462").exists())


class TestDestroyCleansUpTheLane(unittest.TestCase):
    """ppl destroy --scope local takes the lane with the run: its worktree, its branch while
    nothing of it was pushed (D23), its registry slot and its start directory, before ppl init
    too (R15). Its git in the project checkout carries the operator's pinned -c words (R19)."""

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), "-c", "user.email=t@t",
                               "-c", "user.name=t", *args],
                              check=True, capture_output=True, text=True).stdout.strip()

    def checkout(self):
        """A project checkout with a bare origin, its default branch pushed."""
        base = Path(tmphome.mkdtemp(prefix="ppldestroy-"))
        origin, main = base / "origin.git", base / "shop"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
        (main / "a.txt").write_text("one\n", encoding="utf-8")
        self.git(main, "add", "a.txt")
        self.git(main, "commit", "-q", "-m", "base")
        self.git(main, "remote", "add", "origin", str(origin))
        self.git(main, "push", "-q", "origin", "main")
        return main

    def lane(self, task_id, main, branch=None, lane=1, record_branch=False):
        """A worktree on `branch` (<task>-work by default), as ppl lane adds it, and its lane,
        whose record names the branch when `record_branch`."""
        tree = main.parent / ("shop-" + task_id)
        branch = branch or task_id + "-work"
        self.git(main, "worktree", "add", "-q", "-b", branch, str(tree))
        return fixtures.bind_lane(task_id, main, tree, lane=lane,
                                  branch=branch if record_branch else None), tree

    def branches(self, main):
        return self.git(main, "branch", "--list", "--format=%(refname:short)").split()

    def test_local_destroy_deletes_an_unpushed_branch_and_keeps_a_pushed_one(self):
        main = self.checkout()
        _, pushed_tree = self.lane("DEMO-470", main)
        self.git(pushed_tree, "push", "-q", "origin", "DEMO-470-work")
        self.git(main, "fetch", "-q", "origin")
        _, tree = self.lane("DEMO-471", main, lane=2)
        for task_id in ("DEMO-470", "DEMO-471"):
            run.init(task_id, "u", str(main.parent / ("shop-" + task_id)))
        kept = run.destroy("DEMO-470", "local")
        self.assertEqual((kept["worktree"], kept["branch"], kept["branch_name"]),
                         ("removed", "kept", "DEMO-470-work"))
        deleted = run.destroy("DEMO-471", "local")
        self.assertEqual((deleted["worktree"], deleted["branch"], deleted["branch_name"]),
                         ("removed", "deleted", "DEMO-471-work"))
        self.assertNotIn("branch_error", deleted)
        self.assertEqual(self.branches(main), ["DEMO-470-work", "main"])
        self.assertFalse(tree.exists())

    def test_local_destroy_after_lane_close_still_deletes_the_branch(self):
        # D24: ppl lane close frees the slot and leaves the worktree, so the branch is reached
        # through the git dir the worktree names
        main = self.checkout()
        entry, tree = self.lane("DEMO-477", main)
        run.init("DEMO-477", "u", str(tree))
        lanes.release("DEMO-477")
        result = run.destroy("DEMO-477", "local")
        self.assertEqual((result["worktree"], result["branch"], result["lane"]),
                         ("removed", "deleted", "none"))
        self.assertNotIn("DEMO-477-work", self.branches(main))
        self.assertFalse(tree.exists())
        self.assertFalse(os.path.lexists(entry["start_dir"]))

    def test_a_closed_lanes_destroy_never_removes_the_worktree_its_state_names(self):
        # rule: R-LANE-CROSS-WRITE
        # R53: state.json is in the lane's own sandbox scope, so after ppl lane close a role
        # could point its repo at lane B's worktree: destroy reads the lane's record instead
        main = self.checkout()
        _, tree_a = self.lane("DEMO-479", main, lane=1)
        _, tree_b = self.lane("DEMO-480", main, lane=2)
        (tree_b / "work-in-progress.txt").write_text("not committed\n", encoding="utf-8")
        state = run.init("DEMO-479", "u", str(tree_a))
        state["repo"] = str(tree_b)
        run.save(state)
        lanes.release("DEMO-479")
        result = run.destroy("DEMO-479", "local")
        self.assertEqual((result["worktree"], result["branch"]), ("removed", "deleted"))
        self.assertFalse(tree_a.exists())
        self.assertTrue((tree_b / "work-in-progress.txt").is_file())
        self.assertIn(str(tree_b), self.git(main, "worktree", "list"))
        self.assertIsNotNone(lanes.entry("DEMO-480"))

    def test_a_run_from_before_lanes_leaves_a_live_lanes_worktree_alone(self):
        # rule: R-LANE-CROSS-WRITE
        # no registry entry and no start directory: state.json's repo is all there is, and a
        # path that is, or lies in, a live lane's worktree is left behind
        main = self.checkout()
        _, tree_b = self.lane("DEMO-481", main)
        (tree_b / "src").mkdir()
        for task_id, repo in (("DEMO-482", tree_b), ("DEMO-483", tree_b / "src")):
            run.init(task_id, "u", str(repo))
            name_as_the_legacy_run(self, task_id)
            result = run.destroy(task_id, "local")
            self.assertEqual(result["worktree"], "left_behind", task_id)
            self.assertIn("DEMO-481", result["worktree_error"])
            self.assertNotIn("branch_name", result)
            self.assertEqual(result["status"], "archived")
        self.assertTrue(tree_b.is_dir())
        self.assertIn(str(tree_b), self.git(main, "worktree", "list"))

    def test_the_legacy_fallback_places_paths_as_the_guard_does(self):
        # rule: R-LANE-CROSS-WRITE
        # R54: a letter-case variant of a live lane's worktree, and a closed lane's recorded
        # worktree, are left behind
        main = self.checkout()
        _, live = self.lane("DEMO-486", main, lane=1)
        (live / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
        _, closed = self.lane("DEMO-487", main, lane=2)
        lanes.release("DEMO-487")
        variant = str(live.parent / live.name.upper())
        for task_id, repo, holder in (("DEMO-488", variant, "DEMO-486"),
                                      ("DEMO-489", str(closed), "DEMO-487")):
            run.init(task_id, "u", repo)
            name_as_the_legacy_run(self, task_id)
            result = run.destroy(task_id, "local")
            self.assertEqual(result["worktree"], "left_behind", task_id)
            self.assertIn(holder, result["worktree_error"])
            self.assertNotIn("branch_name", result)
        self.assertTrue((live / "wip.txt").is_file())
        self.assertTrue(closed.is_dir())
        listed = self.git(main, "worktree", "list")
        self.assertIn(str(live), listed)
        self.assertIn(str(closed), listed)

    def test_the_legacy_fallback_fails_closed_on_a_record_it_cannot_read(self):
        main = self.checkout()
        tree = main.parent / "shop-old"
        self.git(main, "worktree", "add", "-q", "-b", "DEMO-490-work", str(tree))
        self.lane("DEMO-491", main)
        lanes.release("DEMO-491")
        lanes.record_path("DEMO-491").write_text("not json", encoding="utf-8")
        # the unreadable record would hold every later fallback in this data root
        self.addCleanup(shutil.rmtree, str(lanes.start_dir("DEMO-491")), True)
        run.init("DEMO-490", "u", str(tree))
        name_as_the_legacy_run(self, "DEMO-490")
        result = run.destroy("DEMO-490", "local")
        self.assertEqual(result["worktree"], "left_behind")
        self.assertIn("lane.json", result["worktree_error"])
        self.assertTrue(tree.is_dir())

    def test_a_run_no_pointer_names_keeps_its_worktree_whatever_its_state_says(self):
        # rule: R-LANE-CROSS-WRITE
        # R54: no entry, no record, and runs/ACTIVE names another run (or none): only the
        # pointer's run is from before lanes, so state.json's repo is named, not removed
        main = self.checkout()
        tree = main.parent / "shop-orphan"
        self.git(main, "worktree", "add", "-q", "-b", "DEMO-492-work", str(tree))
        run.init("DEMO-492", "u", str(tree))
        for pointer in (None, "DEMO-493"):
            if pointer:
                name_as_the_legacy_run(self, pointer)
            result = run.destroy("DEMO-492", "local")
            self.assertEqual(result["worktree"], "left_behind", pointer)
            self.assertIn(str(tree), result["worktree_error"])
            self.assertIn("git worktree remove", result["worktree_error"])
            self.assertNotIn("branch_name", result)
            self.assertTrue(tree.is_dir())
            run.init("DEMO-492", "u", str(tree))
        self.assertIn("DEMO-492-work", self.branches(main))

    def test_a_run_dir_left_behind_keeps_the_record_for_the_retry(self):
        # rule: R-LANE-CROSS-WRITE
        # R54, the re-review's repro: a role links runs/<task>/log.jsonl so the archive
        # fails and points state.json's repo at another lane's worktree. The slot is freed,
        # the start directory and its lane.json are kept, and the retry reads the record.
        main = self.checkout()
        entry, tree_a = self.lane("DEMO-494", main, lane=1, record_branch=True)
        _, tree_b = self.lane("DEMO-495", main, lane=2, record_branch=True)
        (tree_b / "wip.txt").write_text("uncommitted\n", encoding="utf-8")
        state = run.init("DEMO-494", "u", str(tree_a))
        state["repo"] = str(tree_b)
        run.save(state)
        log = paths.run_dir("DEMO-494") / "log.jsonl"
        log.unlink(missing_ok=True)
        log.symlink_to(main.parent / "elsewhere.txt")
        lanes.release("DEMO-494")
        lanes.release("DEMO-495")
        first = run.destroy("DEMO-494", "local")
        self.assertEqual((first["worktree"], first["branch"], first["run_dir"],
                          first["lane"], first["start_dir"]),
                         ("removed", "deleted", "left_behind", "none", "kept"))
        self.assertTrue(lanes.record_path("DEMO-494").is_file())
        second = run.destroy("DEMO-494", "local")
        self.assertEqual((second["worktree"], second["run_dir"], second["start_dir"]),
                         ("absent", "left_behind", "kept"))
        self.assertTrue((tree_b / "wip.txt").is_file())
        self.assertIn(str(tree_b), self.git(main, "worktree", "list"))
        # the owner removes the link: the next destroy archives the run and the start dir
        log.unlink()
        third = run.destroy("DEMO-494", "local")
        self.assertEqual((third["status"], third["start_dir"]), ("archived", "removed"))
        self.assertFalse(os.path.lexists(entry["start_dir"]))
        self.assertTrue(tree_b.is_dir())

    def test_destroy_deletes_the_recorded_branch_after_a_role_moved_head(self):
        # N2, D23: the record names the run's branch, so a HEAD a role moved elsewhere in the
        # worktree does not decide which branch goes
        main = self.checkout()
        _, tree = self.lane("DEMO-496", main, record_branch=True)
        self.git(tree, "checkout", "-q", "-b", "DEMO-496-elsewhere")
        run.init("DEMO-496", "u", str(tree))
        result = run.destroy("DEMO-496", "local")
        self.assertEqual((result["worktree"], result["branch"], result["branch_name"]),
                         ("removed", "deleted", "DEMO-496-work"))
        self.assertNotIn("DEMO-496-work", self.branches(main))
        self.assertIn("DEMO-496-elsewhere", self.branches(main))

    def test_a_start_dir_without_its_record_never_falls_back_to_the_state(self):
        main = self.checkout()
        entry, tree = self.lane("DEMO-484", main)
        run.init("DEMO-484", "u", str(tree))
        lanes.release("DEMO-484")
        lanes.record_path("DEMO-484").unlink()
        result = run.destroy("DEMO-484", "local")
        self.assertEqual(result["worktree"], "left_behind")
        self.assertIn("lane.json", result["worktree_error"])
        self.assertTrue(tree.is_dir())
        self.assertFalse(os.path.lexists(entry["start_dir"]))

    def test_a_run_directory_that_is_a_link_is_left_and_the_lane_still_freed(self):
        target = Path(tmphome.mkdtemp(prefix="pplelsewhere-"))
        entry = fixtures.bind_lane("DEMO-485", "/tmp/definitely-not-a-repo-xyz")
        paths.runs_dir().mkdir(parents=True, exist_ok=True)
        paths.run_dir("DEMO-485").symlink_to(target)
        result = run.destroy("DEMO-485", "local")
        self.assertEqual((result["status"], result["run_dir"], result["lane"],
                          result["start_dir"]), ("released", "left_behind", "released", "kept"))
        self.assertIn("link", result["run_dir_error"])
        self.assertTrue(target.is_dir())
        self.assertTrue(lanes.record_path("DEMO-485").is_file())
        paths.run_dir("DEMO-485").unlink()
        result = run.destroy("DEMO-485", "local")
        self.assertEqual((result["status"], result["start_dir"]), ("released", "removed"))
        self.assertFalse(os.path.lexists(entry["start_dir"]))

    def test_local_destroy_keeps_a_branch_not_named_for_the_run(self):
        # the worktree's HEAD is in a role's reach: a branch it checked out there that is not
        # <TASK-ID>-<slug> is not the run's to delete
        main = self.checkout()
        self.lane("DEMO-472", main, branch="release-notes")
        run.init("DEMO-472", "u", str(main.parent / "shop-DEMO-472"))
        result = run.destroy("DEMO-472", "local")
        self.assertEqual((result["worktree"], result["branch"]), ("removed", "none"))
        self.assertIn("release-notes", self.branches(main))

    def test_local_destroy_runs_no_hook_the_checkout_plants(self):
        # rule: R-LANE-LAUNCH-GIT-CONFIG
        main = self.checkout()
        self.lane("DEMO-473", main)
        run.init("DEMO-473", "u", str(main.parent / "shop-DEMO-473"))
        marker = main.parent / "hook.ran"
        hook = main / ".git" / "hooks" / "reference-transaction"
        hook.write_text("#!/bin/sh\necho ran >> %s\n" % marker, encoding="utf-8")
        hook.chmod(0o755)
        result = run.destroy("DEMO-473", "local")
        self.assertEqual(result["branch"], "deleted")
        self.assertFalse(marker.exists())

    def test_local_destroy_frees_a_lane_whose_run_never_started(self):
        main = self.checkout()
        entry, tree = self.lane("DEMO-474", main)
        # a denial the guard logged before ppl init made the run directory
        store.append_log("DEMO-474", {"event": "denial"})
        result = run.destroy("DEMO-474", "local")
        self.assertEqual((result["status"], result["lane"], result["start_dir"]),
                         ("archived", "released", "removed"))
        self.assertEqual((result["worktree"], result["branch"]), ("removed", "deleted"))
        self.assertIsNone(lanes.entry("DEMO-474"))
        self.assertFalse(os.path.lexists(entry["start_dir"]))
        self.assertFalse(tree.exists())
        self.assertNotIn("DEMO-474-work", self.branches(main))
        self.assertFalse(paths.run_dir("DEMO-474").exists())
        self.assertTrue((Path(result["archive"]) / "log.jsonl").is_file())

    def test_local_destroy_of_a_lane_with_no_run_directory_releases_it(self):
        entry = fixtures.bind_lane("DEMO-475", "/tmp/definitely-not-a-repo-xyz")
        result = run.destroy("DEMO-475", "local")
        self.assertEqual((result["status"], result["archive"], result["lane"]),
                         ("released", None, "released"))
        self.assertFalse(paths.run_dir("DEMO-475").exists())
        self.assertFalse(os.path.lexists(entry["start_dir"]))
        # the empty run directory ppl lane makes is removed, not archived
        fixtures.bind_lane("DEMO-478", "/tmp/definitely-not-a-repo-xyz")
        paths.run_dir("DEMO-478").mkdir(parents=True)
        result = run.destroy("DEMO-478", "local")
        self.assertEqual((result["status"], result["archive"]), ("released", None))
        self.assertFalse(paths.run_dir("DEMO-478").exists())
        self.assertFalse(any(paths.runs_dir().glob("_archive/DEMO-478-*")))

    def test_local_destroy_with_no_run_and_no_lane_is_refused(self):
        with self.assertRaises(PplError) as caught:
            run.destroy("DEMO-476", "local")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("DEMO-476", caught.exception.message)

    def test_destroying_the_run_from_before_lanes_retires_its_pointer(self):
        # rule: R-LANE-LEGACY-RUN
        # ledger 283: runs/ACTIVE stayed after the destroy, so a later lane that reused the
        # task id counted as the run from before lanes. A pointer naming another run stays
        run.init("DEMO-520", "u", "/tmp/definitely-not-a-repo-xyz")
        name_as_the_legacy_run(self, "DEMO-521")
        run.destroy("DEMO-520", "local")
        self.assertEqual(paths.legacy_active_file().read_text(encoding="utf-8"), "DEMO-521\n")
        run.init("DEMO-521", "u", "/tmp/definitely-not-a-repo-xyz")
        result = run.destroy("DEMO-521", "local")
        self.assertEqual((result["status"], result["pointer"]), ("archived", "retired"))
        self.assertFalse(os.path.lexists(paths.legacy_active_file()))

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a 000 file")
    def test_a_pointer_that_cannot_be_read_leaves_the_worktree_and_raises_nothing(self):
        # rule: R-LANE-LEGACY-RUN
        # ledger 284: an unreadable runs/ACTIVE raised a traceback in destroy and in ppl lane.
        # destroy now leaves the worktree it cannot place, and ppl lane refuses
        main = self.checkout()
        tree = main.parent / "shop-DEMO-522"
        self.git(main, "worktree", "add", "-q", "-b", "DEMO-522-work", str(tree))
        run.init("DEMO-522", "u", str(tree))
        name_as_the_legacy_run(self, "DEMO-522")
        pointer = paths.legacy_active_file()
        os.chmod(pointer, 0)
        self.addCleanup(os.chmod, pointer, 0o600)
        result = run.destroy("DEMO-522", "local")
        self.assertEqual(result["worktree"], "left_behind")
        self.assertIn("runs/ACTIVE cannot be read", result["worktree_error"])
        self.assertTrue(tree.is_dir())
        from ppl import lane_launch
        with self.assertRaises(PplError) as caught:
            lane_launch.start("KEY-1", exec_=False)
        self.assertEqual(caught.exception.code, "OWNER_DECISION")
        self.assertIn("runs/ACTIVE cannot be read", caught.exception.message)

    def test_a_lane_whose_state_cannot_be_parsed_is_destroyed_all_the_same(self):
        # rule: R-LANE-CROSS-WRITE
        # Final review I2: destroy takes the worktree from the lane's entry or record, so a
        # state.json its sandbox made unparseable holds neither the slot nor the cleanup, for
        # a live lane and for one ppl lane close freed. Its own data root: the archived states
        # are no other test's business.
        self.enterContext(mock.patch.dict(os.environ,
                                          {"PPL_HOME": tmphome.mkdtemp(prefix="pplhome-")}))
        main = self.checkout()
        tasks = iter(("DEMO-510", "DEMO-511", "DEMO-512", "DEMO-513"))
        for name, text in sorted(fixtures.UNPARSEABLE_JSON.items()):
            for closed in (False, True):
                task = next(tasks)
                with self.subTest(state=name, closed=closed):
                    entry, tree = self.lane(task, main, record_branch=True)
                    run.init(task, "u", str(tree))
                    run.state_path(task).write_text(text, encoding="utf-8")
                    if closed:
                        lanes.release(task)
                    result = run.destroy(task, "local")
                    self.assertEqual((result["worktree"], result["branch"], result["status"],
                                      result["start_dir"]),
                                     ("removed", "deleted", "archived", "removed"))
                    self.assertEqual(result["lane"], "none" if closed else "released")
                    self.assertIsNone(lanes.entry(task))
                    self.assertFalse(tree.exists())
                    self.assertFalse(os.path.lexists(entry["start_dir"]))

    def test_a_worktree_a_role_locked_is_removed(self):
        # rule: R-LANE-CROSS-WRITE
        # Final review I3: R35 leaves the lane's own .git/worktrees/<id> writable, and a lock
        # planted there made one --force refuse, so destroy freed the slot and deleted the
        # record while the worktree and branch stayed. A lock is a planted cleanup blocker
        # (R19): two --force take a locked worktree.
        main = self.checkout()
        entry, tree = self.lane("DEMO-514", main, record_branch=True)
        run.init("DEMO-514", "u", str(tree))
        git_dir = Path(self.git(tree, "rev-parse", "--absolute-git-dir"))
        (git_dir / "locked").write_text("planted\n", encoding="utf-8")
        result = run.destroy("DEMO-514", "local")
        self.assertEqual((result["worktree"], result["branch"], result["lane"],
                          result["start_dir"]), ("removed", "deleted", "released", "removed"))
        self.assertNotIn("worktree_error", result)
        self.assertFalse(tree.exists())
        self.assertNotIn("DEMO-514-work", self.branches(main))

    def test_a_worktree_left_behind_keeps_the_record_for_the_retry(self):
        # rule: R-LANE-CROSS-WRITE
        # Final review I3: a worktree destroy could not remove keeps the start directory and
        # its lane.json, as a run directory left behind does, so the retry reads the record
        main = self.checkout()
        entry, tree = self.lane("DEMO-515", main, record_branch=True)
        run.init("DEMO-515", "u", str(tree))
        real = run._git

        def git(directory, args):
            if args[:2] == ["worktree", "remove"]:
                return subprocess.CompletedProcess(args, 128, "", "fatal: cannot remove it")
            return real(directory, args)
        with mock.patch.object(run, "_git", git):
            first = run.destroy("DEMO-515", "local")
        self.assertEqual((first["worktree"], first["lane"], first["start_dir"]),
                         ("left_behind", "released", "kept"))
        self.assertIn("cannot remove it", first["worktree_error"])
        self.assertIsNone(lanes.entry("DEMO-515"))
        self.assertTrue(lanes.record_path("DEMO-515").is_file())
        self.assertTrue(tree.is_dir())
        second = run.destroy("DEMO-515", "local")
        self.assertEqual((second["worktree"], second["branch"], second["start_dir"]),
                         ("removed", "deleted", "removed"))
        self.assertFalse(tree.exists())
        self.assertNotIn("DEMO-515-work", self.branches(main))
        self.assertFalse(os.path.lexists(entry["start_dir"]))


if __name__ == "__main__":
    unittest.main()

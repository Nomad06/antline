import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
# cmd_init consults doctor.registration(), which otherwise reads this
# machine's real ~/.claude/settings.json and this process's real cwd. Pointed
# at empty tmp directories, registration() is deterministically "none" for
# every ordinary test in this module - isolated=true's default refusal fires
# only on "global", so "none" never trips it. Tests that exercise doctor
# itself override these per-test and restore them.
os.environ["PPL_CLAUDE_HOME"] = tmphome.mkdtemp(prefix="pplclaudehome-")
os.environ["PPL_LOCAL_SETTINGS_DIR"] = tmphome.mkdtemp(prefix="ppllocalsettings-")

import fixtures
from ppl import cli, config, doctor, models as ppl_models, paths, setup

ROUTING_EXAMPLE = Path(__file__).resolve().parents[1] / "defaults" / "routing.example.toml"
MODULE_HOME = os.environ["PPL_HOME"]


def seed_routing():
    shutil.copy(ROUTING_EXAMPLE, paths.home() / "routing.toml")


def mark_all_verified():
    ppl_models.verify(runner=lambda model, binary="claude", env=None:
                      {"ok": True, "resolved_id": "id-" + model, "cost_usd": 0.0, "detail": ""})


def seed_setup():
    """A completed `ppl setup`: every step recorded done and example-maven installed and active,
    which is what `ppl init --harness example-maven` requires. It starts from a fresh config.toml,
    so a test may leave one it made unreadable behind."""
    fixtures.install_harness("example-maven")
    paths.config_file().unlink(missing_ok=True)
    config.ensure()
    for step in setup.STEPS:
        config.mark_step(step, "done")
    config.set_active_harness("example-maven")


def setUpModule():
    """Every ordinary `call("init", ...)` below runs against a home whose models
    are verified and whose setup is complete, so the init gates only bite where
    a test removes that.

    The version lookup is stubbed for the module and restored afterwards: a
    permanent replacement would hide the real claude_version from test_models,
    and a real `claude --version` would be recorded as the verified version.
    PPL_HOME is pinned to this module's own home because `discover` imports
    every test module first, which leaves the last one's home active.
    """
    previous_home = os.environ["PPL_HOME"]
    os.environ["PPL_HOME"] = MODULE_HOME
    version = mock.patch.object(ppl_models, "claude_version", lambda binary="claude": "9.9.9")
    version.start()

    def restore():
        version.stop()
        os.environ["PPL_HOME"] = previous_home

    unittest.addModuleCleanup(restore)
    seed_routing()
    mark_all_verified()
    seed_setup()


def call(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


def make_repo():
    repo = tmphome.mkdtemp(prefix="pplrepo-")
    subprocess.run(["git", "init", "-q", "-b", "master", repo], check=True)
    Path(repo, "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", "base"], check=True)
    return repo


def head_sha(repo):
    return subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


def commit(repo, name, text):
    Path(repo, name).write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", repo, "add", name], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "-m", name], check=True)
    return head_sha(repo)


def write_json_file(obj):
    handle, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


def write_text_file(text):
    handle, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def stamp_challenge(task_id):
    """Binds an ACCEPTED brief challenge to the run's current spec_revision the
    minimal way `ppl brief challenge` would after validating the report, so
    fixtures unrelated to R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION can walk
    past briefing."""
    from ppl import run as run_module
    state = run_module.load(task_id)
    if state.get("spec_revision") is None:
        state["spec_revision"] = "sha256:" + "0" * 64
    state["brief_challenge"] = {"spec_revision": state["spec_revision"],
                                "verdict": "ACCEPTED", "at": "2026-01-01T00:00:00+00:00"}
    run_module.save(state)


class TestCli(unittest.TestCase):
    def test_init_then_show(self):
        code, out, _ = call("init", "DEMO-600", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--harness", "example-maven")
        self.assertEqual(code, 0)
        code, out, _ = call("show", "--task", "DEMO-600", "--json")
        self.assertEqual(json.loads(out)["task_id"], "DEMO-600")

    def test_error_exits_two_with_a_json_code_on_stderr(self):
        code, _, err = call("show", "--task", "DEMO-NOPE")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_status_prints_the_card(self):
        call("init", "DEMO-601", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, _ = call("status", "--task", "DEMO-601")
        self.assertEqual(code, 0)
        self.assertIn("Needs you:", out)

    def test_task_defaults_to_the_active_run(self):
        call("init", "DEMO-602", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, _ = call("status")
        self.assertEqual(code, 0)
        self.assertIn("DEMO-602", out)

    def test_control_set_and_get(self):
        call("init", "DEMO-603", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self.assertEqual(call("control", "set", "pause", "--task", "DEMO-603")[0], 0)
        code, out, _ = call("control", "get", "--task", "DEMO-603")
        self.assertEqual(code, 0)
        self.assertIn("pause", out)

    def test_paused_run_refuses_stage_enter(self):
        # rule: R-OPERATOR-HALT-CLI
        call("init", "DEMO-604", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        call("control", "set", "pause", "--task", "DEMO-604")
        code, _, err = call("stage", "enter", "baseline", "--task", "DEMO-604")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")

    def test_a_run_can_walk_from_init_to_a_lifecycle_transition(self):
        from ppl import run as run_module
        call("init", "DEMO-605", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        for stage in ["baseline", "briefing", "implementation", "tests", "gates"]:
            code, _, err = call("stage", "enter", stage, "--task", "DEMO-605")
            self.assertEqual(code, 0, err)
            if stage == "baseline":
                # E1, cycle 2 wave E: stage_enter now refuses to leave
                # "baseline" without a recorded base_sha - stamped directly
                # since "/tmp/repo" is not a real git repository here.
                state = run_module.load("DEMO-605")
                state["base_sha"] = "a" * 40
                run_module.save(state)
            if stage == "briefing":
                stamp_challenge("DEMO-605")
        code, out, _ = call("show", "--task", "DEMO-605", "--json")
        self.assertEqual(json.loads(out)["status"], "in_progress")
        code, _, err = call("transition", "blocked", "--task", "DEMO-605")
        self.assertEqual(code, 0, err)

    def test_every_documented_command_is_registered(self):
        for name in ["init", "show", "status", "stage", "brief", "boundary", "baseline",
                     "evidence", "verdict", "transition", "park", "escalate", "budget",
                     "approval", "recover", "serve", "control", "routing", "dispatch",
                     "apply-patch", "mr", "merge", "review"]:
            self.assertIn(name, cli.COMMANDS)

    # --- carried-forward requirement 1: recover is gated by control.enforce ---

    def test_recover_halts_with_owner_decision_when_operator_pauses(self):
        repo = make_repo()
        call("init", "DEMO-610", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("control", "set", "pause", "--task", "DEMO-610")
        code, out, err = call("recover", "--task", "DEMO-610")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        # the advisory actions must still be printed before the hard gate fires
        self.assertTrue(out.strip().startswith("-"))

    def test_recover_halts_with_owner_decision_when_control_file_is_unusable(self):
        from ppl import paths, store
        repo = make_repo()
        call("init", "DEMO-611", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        store.write_json(paths.run_dir("DEMO-611") / "control.json", {"command": "pause"})
        code, out, err = call("recover", "--task", "DEMO-611")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")

    def test_recover_exits_two_when_reconcile_reports_blocked(self):
        call("init", "DEMO-612", "--tracker-url", "u", "--repo", "/tmp/definitely-not-a-repo-xyz",
             "--harness", "example-maven")
        code, out, err = call("recover", "--task", "DEMO-612")
        self.assertEqual(code, 2)
        # E4, cycle 2 wave E: the repo path does not exist at all, so this is
        # the never-created-worktree shape - SCHEMA_INVALID, whose remedy
        # ("create it now") is the right one, not EXTERNAL_BLOCKED's
        # "park, retry on the next tick", which never creates anything.
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_recover_after_parking_before_the_worktree_exists_is_schema_invalid(self):
        # E4, cycle 2 wave E, the exact confirmation-walk repro: a run parked
        # before Stage 1 ever created its worktree. SKILL.md's own advice for
        # EXTERNAL_BLOCKED ("do not attempt to continue past this one") plus
        # escalation.md's remedy ("park, retry on the next tick") would loop
        # forever here - the run is already parked, and retrying creates
        # nothing. SCHEMA_INVALID's remedy ("create it now") is the one that
        # actually gets unstuck.
        call("init", "DEMO-6231", "--tracker-url", "u", "--repo", "/tmp/definitely-not-a-repo-xyz",
             "--harness", "example-maven")
        code, _, err = call("park", "--blocker", "OWNER_DECISION",
                            "--safe-next", "waiting on the owner", "--task", "DEMO-6231")
        self.assertEqual(code, 0, err)
        code, out, err = call("recover", "--task", "DEMO-6231")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("Stage 1", json.loads(err)["message"])

    def test_recover_on_a_directory_that_exists_but_is_not_a_repo_is_external_blocked(self):
        # The other half of the same distinction: a path that DOES exist
        # (unlike the never-created-worktree case above) but is not a usable
        # git repository is a genuinely different, plausibly-transient
        # condition - EXTERNAL_BLOCKED, whose "park, retry on the next tick"
        # remedy still fits that shape.
        plain_dir = tmphome.mkdtemp(prefix="not-a-repo-")
        call("init", "DEMO-6232", "--tracker-url", "u", "--repo", plain_dir,
             "--harness", "example-maven")
        code, out, err = call("recover", "--task", "DEMO-6232")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "EXTERNAL_BLOCKED")

    def test_recover_exits_zero_when_nothing_is_wrong(self):
        repo = make_repo()
        call("init", "DEMO-613", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        code, out, err = call("recover", "--task", "DEMO-613")
        self.assertEqual(code, 0, err)

    # --- carried-forward requirement 3: baseline records the branch ---

    def test_baseline_records_the_branch_alongside_base_sha(self):
        repo = make_repo()
        call("init", "DEMO-614", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-614")
        base = head_sha(repo)
        commit(repo, "work.txt", "work\n")
        baseline_file = write_json_file({"base_sha": base})
        code, out, err = call("baseline", "record", baseline_file, "--task", "DEMO-614")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-614", "--json")
        state = json.loads(out)
        self.assertEqual(state["branch"], "master")
        self.assertEqual(state["base_sha"], base)

    # --- carried-forward requirement 4: evidence producer default shape ---

    def test_evidence_defaults_producer_when_not_given(self):
        from ppl import evidence
        repo = make_repo()
        call("init", "DEMO-615", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        log_file = tempfile.mkstemp()[1]
        Path(log_file).write_text("ok", encoding="utf-8")
        sha = "a" * 40
        code, out, err = call("evidence", "add", "--check", "focused-tests", "--result", "pass",
                              "--sha", sha, "--log", log_file, "--rc", rc_file,
                              "--generation", "1", "--task", "DEMO-615")
        self.assertEqual(code, 0, err)
        receipts = evidence.for_sha("DEMO-615", sha)
        self.assertEqual(receipts["focused-tests"]["producer"],
                         {"provider": "claude-code-subagent", "model": "unknown", "params_hash": ""})

    # --- review finding 2: approval reports machine-readable state on stdout ---

    def test_approval_reports_none_when_no_approval_is_recorded(self):
        repo = make_repo()
        call("init", "DEMO-620", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        code, out, err = call("approval", "check", "--task", "DEMO-620")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(doc["approval"], "none")
        self.assertIsNone(doc["approved_sha"])
        self.assertTrue(doc["head_sha"])

    def test_approval_reports_current_when_approved_sha_matches_head(self):
        from ppl import gitfacts, run
        repo = make_repo()
        call("init", "DEMO-621", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        state = run.load("DEMO-621")
        head = gitfacts.head_sha(repo)
        state["mr"]["approved_sha"] = head
        run.save(state)
        code, out, err = call("approval", "check", "--task", "DEMO-621")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(doc["approval"], "current")
        self.assertEqual(doc["approved_sha"], head)
        self.assertEqual(doc["head_sha"], head)

    def test_approval_reports_stale_when_approved_sha_is_not_head(self):
        from ppl import run
        repo = make_repo()
        call("init", "DEMO-622", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        state = run.load("DEMO-622")
        state["mr"]["approved_sha"] = "0" * 40
        run.save(state)
        code, out, err = call("approval", "check", "--task", "DEMO-622")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(doc["approval"], "stale")
        self.assertEqual(doc["approved_sha"], "0" * 40)

    # --- review finding 3 & 4: a missing or malformed JSON input file is routable ---

    def test_boundary_set_missing_file_is_schema_invalid(self):
        call("init", "DEMO-623", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("boundary", "set", "/no/such/file.json", "--task", "DEMO-623")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_boundary_set_malformed_file_is_schema_invalid(self):
        call("init", "DEMO-624", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        bad = write_text_file("not json")
        code, _, err = call("boundary", "set", bad, "--task", "DEMO-624")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_boundary_set_refuses_a_wildcard_write_boundary(self):
        # rule: R-BOUNDARY-SET-NO-WILDCARD (N5, cycle 2 wave B). Unlike
        # `ppl brief set`, `ppl boundary set` never validated its write list
        # at all beyond non-emptiness - {"write": ["**"]} was accepted,
        # matching every path in the tree and making the done gate's
        # diff-boundary check (condition 4) unconditionally true. A boundary
        # this wide can be recorded AFTER a verdict already passed, so it is
        # the same shape as the C3 baseline attack: rewrite the criterion the
        # gate reads, then run the gate.
        call("init", "DEMO-626", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        bound_file = write_json_file({"write": ["**"], "forbid": []})
        code, _, err = call("boundary", "set", bound_file, "--task", "DEMO-626")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("**", err)

    def test_boundary_set_still_accepts_a_narrow_glob(self):
        call("init", "DEMO-627", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        bound_file = write_json_file({"write": ["src/**"], "forbid": []})
        code, _, err = call("boundary", "set", bound_file, "--task", "DEMO-627")
        self.assertEqual(code, 0, err)

    def test_brief_set_missing_file_is_schema_invalid(self):
        call("init", "DEMO-625", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("brief", "set", "/no/such/file.json", "--task", "DEMO-625")
        stamp_challenge("DEMO-625")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_verdict_record_malformed_file_is_schema_invalid(self):
        repo = make_repo()
        call("init", "DEMO-626", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        bad = write_text_file("{not json")
        code, _, err = call("verdict", "record", bad, "--task", "DEMO-626")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_dispatch_missing_input_file_is_schema_invalid(self):
        out_file = os.path.join(tmphome.mkdtemp(), "out.json")
        code, _, err = call("dispatch", "--role", "ppl-code-worker",
                            "--in", "/no/such/input.json", "--out", out_file)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_evidence_malformed_producer_is_schema_invalid(self):
        repo = make_repo()
        call("init", "DEMO-627", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        log_file = tempfile.mkstemp()[1]
        Path(log_file).write_text("ok", encoding="utf-8")
        code, _, err = call("evidence", "add", "--check", "focused-tests", "--result", "pass",
                            "--sha", "a" * 40, "--log", log_file, "--rc", rc_file,
                            "--producer", "not-json", "--task", "DEMO-627")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    # --- baseline writes only after the git call succeeds ---

    def test_baseline_leaves_no_file_when_repo_is_unusable(self):
        from ppl import paths
        call("init", "DEMO-628", "--tracker-url", "u", "--repo", "/tmp/definitely-not-a-repo-xyz",
             "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-628")
        baseline_file = write_json_file({"base_sha": "abc123"})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-628")
        self.assertEqual(code, 2)
        # E1/E4, cycle 2 wave E: a repo path that does not exist at all is a
        # missing-precondition (the worktree was never created), not a
        # transient outside failure - SCHEMA_INVALID, not EXTERNAL_BLOCKED.
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertFalse((paths.run_dir("DEMO-628") / "baseline.json").exists())

    # --- review finding 5: argparse's own errors stay inside the JSON contract ---

    def test_unknown_top_level_command_is_schema_invalid_json(self):
        code, _, err = call("bogus-command")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_invalid_subcommand_choice_is_schema_invalid_json(self):
        call("init", "DEMO-629", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("control", "set", "not-a-real-command", "--task", "DEMO-629")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    # --- mr and review subcommands (task 14) ---

    def test_mr_set_then_state_reports_none_before_approval(self):
        repo = make_repo()
        call("init", "DEMO-630", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        code, out, err = call("mr", "set", "--iid", "15500", "--url", "https://gitlab/x/-/mr/1",
                              "--task", "DEMO-630")
        self.assertEqual(code, 0, err)
        self.assertIn("15500", out)
        code, out, err = call("mr", "state", "--task", "DEMO-630")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "none")

    def test_mr_set_without_iid_is_schema_invalid(self):
        call("init", "DEMO-631", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("mr", "set", "--url", "https://gitlab/x/-/mr/1", "--task", "DEMO-631")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_mr_set_without_url_is_schema_invalid(self):
        call("init", "DEMO-632", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("mr", "set", "--iid", "1", "--task", "DEMO-632")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_review_answered_then_approved_flips_mr_state_to_current(self):
        from ppl import gitfacts
        repo = make_repo()
        call("init", "DEMO-633", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        head = gitfacts.head_sha(repo)
        code, out, err = call("review", "answered", "--thread", "t1", "--sha", head,
                              "--classification", "NEEDS_REPLY_ONLY",
                              "--generation", "1", "--task", "DEMO-633")
        self.assertEqual(code, 0, err)
        self.assertIn("NEEDS_REPLY_ONLY", out)
        code, out, err = call("review", "approved", "--sha", head, "--task", "DEMO-633")
        self.assertEqual(code, 0, err)
        code, out, err = call("mr", "state", "--task", "DEMO-633")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "current")

    def test_review_answered_without_thread_is_schema_invalid(self):
        call("init", "DEMO-634", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("review", "answered", "--sha", "abc",
                            "--classification", "NEEDS_REPLY_ONLY", "--task", "DEMO-634")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_review_answered_with_unknown_classification_is_schema_invalid(self):
        call("init", "DEMO-635", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("review", "answered", "--thread", "t1", "--sha", "abc",
                            "--classification", "LOOKS_FINE_TO_ME", "--task", "DEMO-635")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    # --- mr --author and review --last-note-id (task 14 fix-review findings) ---

    def test_mr_set_with_author_is_visible_through_the_pending_filter(self):
        from ppl import review
        repo = make_repo()
        call("init", "DEMO-636", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        code, _, err = call("mr", "set", "--iid", "1", "--url", "u",
                            "--author", "ppl-bot-42", "--task", "DEMO-636")
        self.assertEqual(code, 0, err)
        threads = [{"id": "human", "author": "reviewer"},
                   {"id": "self", "author": "ppl-bot-42"}]
        pending = review.pending("DEMO-636", threads, "abc")
        self.assertEqual([t["id"] for t in pending], ["human"])

    def test_review_answered_with_last_note_id_lets_a_follow_up_note_reopen_it(self):
        from ppl import gitfacts, review
        repo = make_repo()
        call("init", "DEMO-637", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        head = gitfacts.head_sha(repo)
        code, _, err = call("review", "answered", "--thread", "t1", "--sha", head,
                            "--classification", "NEEDS_REPLY_ONLY",
                            "--last-note-id", "n1", "--generation", "1", "--task", "DEMO-637")
        self.assertEqual(code, 0, err)
        self.assertTrue(review.already_answered("DEMO-637", "t1", head, "n1"))
        self.assertFalse(review.already_answered("DEMO-637", "t1", head, "n2"))

    # --- ppl review pending: the JSON-contract surface an LLM orchestrator
    #     can actually call (fix round 2, finding 1) ---

    def test_review_pending_prints_the_filtered_array_as_the_last_stdout_line(self):
        call("init", "DEMO-638", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        threads_file = write_json_file([
            {"id": "t1", "author": "reviewer", "resolved": False},
            {"id": "t2", "author": "reviewer", "resolved": True},
        ])
        code, out, err = call("review", "pending", "--threads", threads_file,
                              "--sha", "abc", "--task", "DEMO-638")
        self.assertEqual(code, 0, err)
        last_line = out.strip().splitlines()[-1]
        doc = json.loads(last_line)
        self.assertEqual([t["id"] for t in doc], ["t1"])

    def test_review_pending_honors_an_explicit_author_override(self):
        call("init", "DEMO-639", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        threads_file = write_json_file([
            {"id": "t1", "author": "reviewer"},
            {"id": "t2", "author": "some-bot"},
        ])
        code, out, err = call("review", "pending", "--threads", threads_file,
                              "--sha", "abc", "--author", "some-bot", "--task", "DEMO-639")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.strip().splitlines()[-1])
        self.assertEqual([t["id"] for t in doc], ["t1"])

    def test_review_pending_uses_a_note_id_recorded_through_answered(self):
        from ppl import gitfacts
        repo = make_repo()
        call("init", "DEMO-640", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        head = gitfacts.head_sha(repo)
        call("review", "answered", "--thread", "t1", "--sha", head,
             "--classification", "NEEDS_REPLY_ONLY", "--last-note-id", "41",
             "--generation", "1", "--task", "DEMO-640")
        threads_file = write_json_file([
            {"id": "t1", "author": "reviewer", "last_note_id": 41},
            {"id": "t2", "author": "reviewer", "last_note_id": 42},
        ])
        code, out, err = call("review", "pending", "--threads", threads_file,
                              "--sha", head, "--task", "DEMO-640")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.strip().splitlines()[-1])
        self.assertEqual([t["id"] for t in doc], ["t2"])

    def test_review_pending_missing_threads_file_is_schema_invalid(self):
        call("init", "DEMO-641", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("review", "pending", "--threads", "/no/such/file.json",
                            "--sha", "abc", "--task", "DEMO-641")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_review_pending_malformed_threads_file_is_schema_invalid(self):
        call("init", "DEMO-642", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        bad = write_text_file("not json")
        code, _, err = call("review", "pending", "--threads", bad,
                            "--sha", "abc", "--task", "DEMO-642")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_review_pending_non_array_threads_file_is_schema_invalid(self):
        call("init", "DEMO-643", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        not_a_list = write_json_file({"id": "t1"})
        code, _, err = call("review", "pending", "--threads", not_a_list,
                            "--sha", "abc", "--task", "DEMO-643")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_review_pending_without_threads_flag_is_schema_invalid(self):
        call("init", "DEMO-644", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("review", "pending", "--sha", "abc", "--task", "DEMO-644")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    # --- fix round 3: malformed thread payload must not crash the process ---

    def test_review_pending_thread_missing_id_is_schema_invalid_not_a_traceback(self):
        call("init", "DEMO-645", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        threads_file = write_json_file([{"author": "reviewer"}])
        code, _, err = call("review", "pending", "--threads", threads_file,
                            "--sha", "abc", "--task", "DEMO-645")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_review_pending_second_element_malformed_first_element_fine(self):
        call("init", "DEMO-646", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        threads_file = write_json_file([{"id": "t1", "author": "reviewer"}, "not-a-thread"])
        code, _, err = call("review", "pending", "--threads", threads_file,
                            "--sha", "abc", "--task", "DEMO-646")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    # --- fix round 3: explicit --author must not skip the existence check ---

    def test_review_pending_with_author_on_an_uninitialised_task_is_schema_invalid(self):
        threads_file = write_json_file([{"id": "t1", "author": "reviewer"}])
        code, _, err = call("review", "pending", "--threads", threads_file, "--sha", "abc",
                            "--author", "X", "--task", "DEMO-999999")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    # --- fix round 3: --author "" is a deliberate explicit no-filter, not an omission ---

    def test_review_pending_empty_author_disables_filtering_without_using_the_stored_identity(self):
        call("init", "DEMO-647", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        call("mr", "set", "--iid", "1", "--url", "u", "--author", "ppl-bot-42", "--task", "DEMO-647")
        threads_file = write_json_file([
            {"id": "t1", "author": "reviewer"},
            {"id": "t2", "author": "ppl-bot-42"},
        ])
        code, out, err = call("review", "pending", "--threads", threads_file, "--sha", "abc",
                              "--author", "", "--task", "DEMO-647")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(sorted(t["id"] for t in doc), ["t1", "t2"])


class TestBaselineGuards(unittest.TestCase):
    """C1(a): base_sha is what the done-gate diffs against, so it is written
    once, and only to a commit HEAD actually descends from. Re-recording it at
    HEAD after the work commit made changed_paths empty and passed the gate's
    boundary rule with a commit entirely outside the boundary.
    """

    def test_leaving_baseline_without_recording_one_is_refused(self):
        # E1, cycle 2 wave E: confirmed by a confirmation walk. Nothing used
        # to check that `ppl baseline record` was ever called at all - a run
        # could walk `stage enter briefing` straight out of "baseline" with
        # base_sha still None, and every stage after that exited 0 too,
        # failing only much later at `ppl transition done`
        # [R-BASELINE-RECORDED-BEFORE-LEAVING].
        repo = make_repo()
        call("init", "DEMO-658", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-658")
        code, _, err = call("stage", "enter", "briefing", "--task", "DEMO-658")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertIn("recorded baseline", err)
        code, out, _ = call("show", "--task", "DEMO-658", "--json")
        self.assertEqual(json.loads(out)["stage"], "baseline")
        # Recording it first, then leaving, works exactly as before.
        baseline_file = write_json_file({"base_sha": head_sha(repo)})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-658")
        self.assertEqual(code, 0, err)
        code, _, err = call("stage", "enter", "briefing", "--task", "DEMO-658")
        self.assertEqual(code, 0, err)

    def test_escalating_out_of_baseline_without_a_baseline_is_still_legal(self):
        # Escalation must stay reachable from a stuck baseline stage (e.g. the
        # worktree itself could not be created, EXTERNAL_BLOCKED/SCHEMA_INVALID)
        # - `_resume_target` already forces the eventual resume back into
        # "baseline", so nothing is skipped by letting this one edge through.
        repo = make_repo()
        call("init", "DEMO-659", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-659")
        code, _, err = call("stage", "enter", "escalation", "--task", "DEMO-659")
        self.assertEqual(code, 0, err)

    def test_recording_a_baseline_twice_is_an_illegal_transition(self):
        # rule: R-BASELINE-WRITE-ONCE
        repo = make_repo()
        call("init", "DEMO-650", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-650")
        base = head_sha(repo)
        commit(repo, "work.txt", "work\n")
        first = write_json_file({"base_sha": base})
        code, _, err = call("baseline", "record", first, "--task", "DEMO-650")
        self.assertEqual(code, 0, err)
        later = commit(repo, "b.txt", "two\n")
        second = write_json_file({"base_sha": later})
        code, _, err = call("baseline", "record", second, "--task", "DEMO-650")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        code, out, _ = call("show", "--task", "DEMO-650", "--json")
        self.assertNotEqual(json.loads(out)["base_sha"], later)

    def test_a_base_sha_that_is_not_an_ancestor_of_head_is_refused(self):
        # rule: R-BASELINE-ANCESTOR
        repo = make_repo()
        orphan = commit(repo, "b.txt", "two\n")
        subprocess.run(["git", "-C", repo, "reset", "--hard", "-q", "HEAD~1"], check=True)
        call("init", "DEMO-651", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-651")
        baseline_file = write_json_file({"base_sha": orphan})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-651")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertFalse((paths.run_dir("DEMO-651") / "baseline.json").exists())

    def test_an_ancestor_base_sha_is_accepted_after_later_commits(self):
        repo = make_repo()
        base = head_sha(repo)
        commit(repo, "b.txt", "two\n")
        call("init", "DEMO-652", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-652")
        baseline_file = write_json_file({"base_sha": base})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-652")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-652", "--json")
        self.assertEqual(json.loads(out)["base_sha"], base)

    def test_baseline_is_refused_while_the_run_is_paused(self):
        from ppl import control
        repo = make_repo()
        call("init", "DEMO-653", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-653")
        control.set_command("DEMO-653", "pause")
        try:
            baseline_file = write_json_file({"base_sha": head_sha(repo)})
            code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-653")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        finally:
            control.clear("DEMO-653")

    def test_base_sha_equal_to_head_on_a_fresh_branch_is_accepted(self):
        # rule: R-BASELINE-NOT-AT-HEAD (D2, cycle 2 wave D, correcting C3).
        # A branch freshly cut with no work of its own yet has HEAD equal to
        # the merge-base with whatever it was cut from - exactly what
        # SKILL.md Stage 1 tells the orchestrator to record, and exactly the
        # case the old outright base_sha == HEAD refusal made impossible.
        # This is what makes it safe: the run is still IN the baseline stage
        # when this call happens.
        repo = make_repo()
        call("init", "DEMO-655", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-655")
        head = head_sha(repo)
        baseline_file = write_json_file({"base_sha": head})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-655")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-655", "--json")
        self.assertEqual(json.loads(out)["base_sha"], head)

    def test_recording_after_the_stage_has_moved_on_is_refused_even_at_an_ancestor(self):
        # rule: R-BASELINE-NOT-AT-HEAD
        # The actual hazard C3 named: a baseline recorded AFTER work already
        # landed, which empties the done-gate's diff and lets every finding
        # derive as foreign - not the base_sha == HEAD value itself. Caught
        # here by WHEN the call happens (the run has moved past the baseline
        # stage), not by which commit it names - an ancestor base_sha is
        # refused exactly the same as one equal to HEAD would be.
        #
        # E1, cycle 2 wave E closed the ordinary way to reach this shape:
        # `stage_enter` itself now refuses to leave "baseline" without a
        # recorded base_sha, so a bare `ppl stage enter briefing` can no
        # longer produce a run that is past baseline with none recorded. This
        # check is still defense in depth against a hand-edited or otherwise
        # corrupted state.json, which is simulated directly below rather than
        # through the ordinary CLI sequence (which this same wave made
        # impossible, on purpose).
        from ppl import run as run_module
        repo = make_repo()
        base = head_sha(repo)
        commit(repo, "work.txt", "work\n")
        call("init", "DEMO-657", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-657")
        state = run_module.load("DEMO-657")
        state["base_sha"] = base
        run_module.save(state)
        call("stage", "enter", "briefing", "--task", "DEMO-657")
        state = run_module.load("DEMO-657")
        state["base_sha"] = None
        run_module.save(state)
        baseline_file = write_json_file({"base_sha": base})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-657")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertIn("baseline stage", err)
        self.assertFalse((paths.run_dir("DEMO-657") / "baseline.json").exists())

    def test_a_json_array_where_a_document_belongs_is_schema_invalid(self):
        repo = make_repo()
        call("init", "DEMO-654", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        array_file = write_json_file([{"base_sha": head_sha(repo)}])
        code, _, err = call("baseline", "record", array_file, "--task", "DEMO-654")
        self.assertEqual(code, 2, "a JSON array must route, not crash with exit 1")
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertFalse((paths.run_dir("DEMO-654") / "baseline.json").exists())


class TestNextSafeActionAfterArtifactCommands(unittest.TestCase):
    """E2, cycle 2 wave E: next_safe_action is restamped by stage_enter,
    transition, park, escalate and recover (D6, cycle 2 wave D), but the
    four one-shot artifact commands - baseline record, brief set, verdict
    record, mr set - were never generalised to do the same. Confirmed by a
    confirmation walk: each left next_safe_action naming the action that
    had just succeeded, and repeating that exact action errored
    ILLEGAL_TRANSITION.
    """

    def test_baseline_record_advances_next_safe_action(self):
        repo = make_repo()
        call("init", "DEMO-6301", "--tracker-url", "u", "--repo", repo,
             "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-6301")
        baseline_file = write_json_file({"base_sha": head_sha(repo)})
        code, _, err = call("baseline", "record", baseline_file, "--task", "DEMO-6301")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-6301", "--json")
        next_action = json.loads(out)["next_safe_action"]
        self.assertNotIn("set up the worktree", next_action)
        self.assertIn("briefing", next_action)

    def test_brief_set_advances_next_safe_action(self):
        repo = make_repo()
        call("init", "DEMO-6302", "--tracker-url", "u", "--repo", repo,
             "--harness", "example-maven")
        brief_file = write_json_file({
            "task_id": "DEMO-6302", "purpose": "p", "success_outcome": "s",
            "governing_requirements": [], "acceptance": ["AC-1"],
            "allowed_write_paths": ["**"], "forbidden_paths": [],
            "verification": ["focused-tests"], "open_decisions": [],
            "name_contract": {"Foo.bar": "int"},
        })
        code, _, err = call("brief", "set", brief_file, "--task", "DEMO-6302")
        stamp_challenge("DEMO-6302")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-6302", "--json")
        next_action = json.loads(out)["next_safe_action"]
        self.assertNotIn("dispatch ppl-briefer", next_action)
        self.assertIn("implementation", next_action)

    def test_verdict_record_advances_next_safe_action_by_outcome(self):
        from ppl import run
        repo = make_repo()
        base = head_sha(repo)
        call("init", "DEMO-6303", "--tracker-url", "u", "--repo", repo,
             "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-6303")
        call("baseline", "record", write_json_file({"base_sha": base}), "--task", "DEMO-6303")
        brief_file = write_json_file({
            "task_id": "DEMO-6303", "purpose": "p", "success_outcome": "s",
            "governing_requirements": [], "acceptance": ["AC-1"],
            "allowed_write_paths": ["**"], "forbidden_paths": [],
            "verification": ["focused-tests"], "open_decisions": [],
            "name_contract": {"Foo.bar": "int"},
        })
        call("brief", "set", brief_file, "--task", "DEMO-6303")
        stamp_challenge("DEMO-6303")
        head = commit(repo, "work.txt", "work\n")
        revision = run.load("DEMO-6303")["spec_revision"]
        doc = write_json_file({
            "task_id": "DEMO-6303", "candidate_sha": head, "spec_revision": revision,
            "verdict": "REWORK", "acceptance_evidence": {}, "findings": [],
            "producer": {"role": "verifier"},
        })
        code, _, err = call("verdict", "record", doc, "--generation", "1",
                            "--task", "DEMO-6303")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-6303", "--json")
        next_action = json.loads(out)["next_safe_action"]
        self.assertNotIn("dispatch ppl-verifier", next_action)
        self.assertIn("retry", next_action)

    def test_mr_set_advances_next_safe_action(self):
        call("init", "DEMO-6304", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("mr", "set", "--iid", "1", "--url", "https://gitlab/mr/1",
                            "--author", "someone", "--task", "DEMO-6304")
        self.assertEqual(code, 0, err)
        code, out, _ = call("show", "--task", "DEMO-6304", "--json")
        next_action = json.loads(out)["next_safe_action"]
        self.assertIn("review loop", next_action)


class TestTransitionOrdering(unittest.TestCase):
    """N4, cycle 2 wave B: cmd_transition ran done_blockers (which needs
    base_sha and a real repo diff) BEFORE fsm.check_lifecycle (a pure,
    cheap, always-available check). A run whose lifecycle transition is
    illegal on its face - still "planned", say - got whatever confusing
    error done_blockers happened to raise first (EXTERNAL_BLOCKED for a
    missing base_sha, here) instead of the much more direct
    ILLEGAL_TRANSITION, making the real problem (W3's deadlock, concretely)
    harder to diagnose.
    """

    def test_an_illegal_lifecycle_target_is_reported_before_the_done_gate_runs(self):
        # rule: R-DONE-GATE
        repo = make_repo()
        call("init", "DEMO-656", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        # No brief, no baseline: done_blockers would fail on a missing
        # base_sha (EXTERNAL_BLOCKED) long before it ever got to checking
        # acceptance or a verdict - but status is still "planned", and
        # planned -> done is illegal regardless of any of that.
        code, _, err = call("transition", "done", "--task", "DEMO-656")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")


def init_at_stage(task_id, stage, status="in_progress"):
    """A run placed directly at `stage`: the walk there is not what these tests prove."""
    from ppl import run
    call("init", task_id, "--tracker-url", "u", "--repo", "/tmp/repo", "--harness", "example-maven")
    state = run.load(task_id)
    state["stage"] = stage
    state["status"] = status
    run.save(state)


class TestDoneOnlyFromClosure(unittest.TestCase):
    """The review loop used to end with `ppl transition done` straight from review, which left
    a run with stage review and status done. Done is now refused outside closure."""

    def test_transition_done_from_review_is_refused_naming_closure(self):
        # rule: R-DONE-FROM-CLOSURE
        from ppl import run
        init_at_stage("DEMO-5810", "review")
        code, _, err = call("transition", "done", "--task", "DEMO-5810")
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "ILLEGAL_TRANSITION")
        self.assertIn("closure", payload["message"])
        state = run.load("DEMO-5810")
        self.assertEqual((state["stage"], state["status"]), ("review", "in_progress"))

    def test_transition_done_from_merge_watch_is_refused_too(self):
        # rule: R-DONE-FROM-CLOSURE
        init_at_stage("DEMO-5811", "merge_watch")
        code, _, err = call("transition", "done", "--task", "DEMO-5811")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")

    def test_other_lifecycle_targets_are_not_tied_to_closure(self):
        init_at_stage("DEMO-5812", "review")
        code, out, err = call("transition", "blocked", "--task", "DEMO-5812")
        self.assertEqual(code, 0, err)
        self.assertIn("blocked", out)


class TestMergeObserved(unittest.TestCase):
    """`ppl merge observed` records the merge the orchestrator saw on the forge, the fact the
    tracker gate waits for. It is valid in merge_watch only, and only once."""

    def test_merge_observed_records_the_sha_and_url_and_logs_the_event(self):
        from ppl import run, store
        init_at_stage("DEMO-5801", "merge_watch", status="done")
        code, out, err = call("merge", "observed", "--sha", "a" * 40,
                              "--url", "https://forge/mr/1", "--task", "DEMO-5801")
        self.assertEqual(code, 0, err)
        self.assertIn("a" * 40, out)
        mr = run.load("DEMO-5801")["mr"]
        self.assertEqual((mr["merged_sha"], mr["merged_url"]), ("a" * 40, "https://forge/mr/1"))
        events = [e for e in store.read_log("DEMO-5801") if e["event"] == "merge_observed"]
        self.assertEqual([(e["sha"], e["url"]) for e in events], [("a" * 40, "https://forge/mr/1")])

    def test_merge_observed_without_a_url_records_none(self):
        from ppl import run
        init_at_stage("DEMO-5802", "merge_watch", status="done")
        code, _, err = call("merge", "observed", "--sha", "b" * 40, "--task", "DEMO-5802")
        self.assertEqual(code, 0, err)
        mr = run.load("DEMO-5802")["mr"]
        self.assertEqual((mr["merged_sha"], mr["merged_url"]), ("b" * 40, None))

    def test_merge_observed_outside_merge_watch_is_refused(self):
        # rule: R-MERGE-OBSERVED-ONCE
        from ppl import run
        for task_id, stage in (("DEMO-5803", "review"), ("DEMO-5804", "closure")):
            init_at_stage(task_id, stage)
            code, _, err = call("merge", "observed", "--sha", "c" * 40, "--task", task_id)
            self.assertEqual(code, 2, stage)
            payload = json.loads(err)
            self.assertEqual(payload["error_code"], "ILLEGAL_TRANSITION", stage)
            self.assertIn("merge_watch", payload["message"], stage)
            self.assertIsNone(run.load(task_id)["mr"].get("merged_sha"), stage)

    def test_a_second_merge_observed_is_refused_and_keeps_the_first(self):
        # rule: R-MERGE-OBSERVED-ONCE
        from ppl import run
        init_at_stage("DEMO-5805", "merge_watch", status="done")
        code, _, err = call("merge", "observed", "--sha", "d" * 40, "--task", "DEMO-5805")
        self.assertEqual(code, 0, err)
        code, _, err = call("merge", "observed", "--sha", "e" * 40, "--task", "DEMO-5805")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(run.load("DEMO-5805")["mr"]["merged_sha"], "d" * 40)

    def test_merge_observed_refuses_a_sha_that_is_not_hexadecimal(self):
        init_at_stage("DEMO-5806", "merge_watch", status="done")
        code, _, err = call("merge", "observed", "--sha", "main", "--task", "DEMO-5806")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")


class TestNeedsYouFlagsParkingAtEveryBoundary(unittest.TestCase):
    """E3, cycle 2 wave E, confirmed by a confirmation walk: `ppl park` only
    ever demotes lifecycle status "in_progress" -> "blocked" (wave B's W3,
    deliberately narrow so a run parked at "planned" or "done" is not lied
    into activity it never had). status.card's "Needs you" line used to key
    off status == "blocked" alone, so a run parked at either boundary showed
    "Needs you: no" - an OWNER_DECISION question sitting invisible on the one
    surface built so a human notices. ppl/panel/server.py:_needs_you already
    keyed off stage instead - this brings ppl show/status into agreement
    with the panel rather than leaving the two answering differently for the
    same run.
    """

    def test_park_at_planned_still_needs_you(self):
        call("init", "DEMO-6401", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, err = call("park", "--blocker", "SPEC_GAP",
                              "--safe-next", "escalate to the owner", "--task", "DEMO-6401")
        self.assertEqual(code, 0, err)
        self.assertIn("planned", out)
        self.assertNotIn("Needs you: no", out)

    def test_park_at_done_still_needs_you(self):
        from ppl import run
        call("init", "DEMO-6402", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        state = run.load("DEMO-6402")
        state["status"] = "done"
        run.save(state)
        code, out, err = call("park", "--blocker", "OWNER_DECISION",
                              "--safe-next", "post-merge follow-up needed", "--task", "DEMO-6402")
        self.assertEqual(code, 0, err)
        self.assertIn("(done)", out)
        self.assertNotIn("Needs you: no", out)

    def test_ppl_escalate_still_needs_you(self):
        # `ppl escalate` is `run.park` under the other name (its own CLI
        # help says so) - the identical gap, exercised through the actual
        # command an orchestrator runs rather than through `ppl park`
        # directly.
        call("init", "DEMO-6403", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("escalate", "--code", "SPEC_GAP",
                            "--question", "is X in scope?", "--task", "DEMO-6403")
        self.assertEqual(code, 0, err)
        code, out, _ = call("status", "--task", "DEMO-6403")
        self.assertNotIn("Needs you: no", out)

    def test_stage_enter_escalation_directly_still_needs_you(self):
        # The real "escalation" FSM stage, reached directly with
        # `ppl stage enter escalation` (fsm.STAGES), never through `ppl
        # escalate`. It carries the identical status-untouched shape at
        # every lifecycle status, not just at the two boundaries `ppl park`
        # exposes - status.card must catch it too.
        call("init", "DEMO-6404", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("stage", "enter", "escalation", "--task", "DEMO-6404")
        self.assertEqual(code, 0, err)
        code, out, _ = call("status", "--task", "DEMO-6404")
        self.assertNotIn("Needs you: no", out)

    def test_needs_you_no_when_nothing_is_wrong(self):
        # The negative case, so the assertions above are not vacuously true
        # for a run that always prints something other than exactly "no".
        call("init", "DEMO-6405", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, _ = call("status", "--task", "DEMO-6405")
        self.assertIn("Needs you: no", out)


class TestDispatchHardCap(unittest.TestCase):
    """I3: SubagentStart cannot block a dispatch, so ppl's refusal at stage
    boundaries and transitions is the only enforcement this budget has.
    """

    def spend_to_the_cap(self, task_id):
        from ppl import run
        state = run.load(task_id)
        state["budgets"]["dispatch_used"] = state["budgets"]["dispatch_hard"]
        run.save(state)

    def test_stage_enter_is_refused_once_the_hard_cap_is_reached(self):
        # rule: R-BUDGET-DISPATCH-CAP
        repo = make_repo()
        call("init", "DEMO-660", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        self.spend_to_the_cap("DEMO-660")
        code, _, err = call("stage", "enter", "baseline", "--task", "DEMO-660")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "BUDGET_EXHAUSTED")

    def test_transition_is_refused_once_the_hard_cap_is_reached(self):
        # rule: R-BUDGET-DISPATCH-CAP
        repo = make_repo()
        call("init", "DEMO-661", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", "DEMO-661")
        self.spend_to_the_cap("DEMO-661")
        code, _, err = call("transition", "done", "--task", "DEMO-661")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "BUDGET_EXHAUSTED")

    def test_stage_enter_still_works_one_dispatch_below_the_cap(self):
        from ppl import run
        repo = make_repo()
        call("init", "DEMO-662", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        state = run.load("DEMO-662")
        state["budgets"]["dispatch_used"] = state["budgets"]["dispatch_hard"] - 1
        run.save(state)
        code, _, err = call("stage", "enter", "baseline", "--task", "DEMO-662")
        self.assertEqual(code, 0, err)


class TestEvidenceShaDefault(unittest.TestCase):
    def test_sha_defaults_to_git_head_when_not_given(self):
        repo = make_repo()
        call("init", "DEMO-670", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        code, out, err = call("evidence", "add", "--check", "focused-tests",
                              "--result", "pass", "--log", "/tmp/log.txt",
                              "--rc", rc_file, "--generation", "1", "--task", "DEMO-670")
        self.assertEqual(code, 0, err)
        self.assertIn(head_sha(repo), out)

    def test_an_explicit_sha_still_wins(self):
        repo = make_repo()
        call("init", "DEMO-671", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        code, out, err = call("evidence", "add", "--check", "focused-tests",
                              "--result", "pass", "--sha", "b" * 40,
                              "--log", "/tmp/log.txt", "--rc", rc_file,
                              "--generation", "1", "--task", "DEMO-671")
        self.assertEqual(code, 0, err)
        self.assertIn("b" * 40, out)


class TestLeaseFencesTheWritePaths(unittest.TestCase):
    """A stale attempt - one still holding a lease generation `ppl recover`
    has since bumped past - may not write. R-LEASE-STALE-WRITE. `ppl findings
    add/status/delete` are lease-fenced the same way as the original four
    (evidence, verdict, review answered, apply-patch).
    """

    def test_evidence_add_refuses_a_stale_generation(self):
        # rule: R-LEASE-STALE-WRITE
        repo = make_repo()
        call("init", "DEMO-940", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("recover", "--task", "DEMO-940")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        code, _, err = call("evidence", "add", "--check", "focused-tests", "--result", "pass",
                            "--sha", "a" * 40, "--log", "/tmp/log.txt", "--rc", rc_file,
                            "--generation", "1", "--task", "DEMO-940")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")

    def test_evidence_add_without_generation_is_schema_invalid(self):
        call("init", "DEMO-941", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        code, _, err = call("evidence", "add", "--check", "focused-tests", "--result", "pass",
                            "--sha", "a" * 40, "--log", "/tmp/log.txt", "--rc", rc_file,
                            "--task", "DEMO-941")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_verdict_record_refuses_a_stale_generation(self):
        # rule: R-LEASE-STALE-WRITE
        from ppl import gitfacts
        repo = make_repo()
        call("init", "DEMO-942", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("recover", "--task", "DEMO-942")
        head = gitfacts.head_sha(repo)
        doc = write_json_file({
            "task_id": "DEMO-942", "candidate_sha": head, "spec_revision": "sha256:x",
            "verdict": "PASS", "acceptance_evidence": {}, "findings": [],
            "producer": {"role": "verifier"},
        })
        code, _, err = call("verdict", "record", doc, "--generation", "1", "--task", "DEMO-942")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")

    def test_review_answered_refuses_a_stale_generation(self):
        # rule: R-LEASE-STALE-WRITE
        from ppl import gitfacts
        repo = make_repo()
        call("init", "DEMO-943", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("recover", "--task", "DEMO-943")
        head = gitfacts.head_sha(repo)
        code, _, err = call("review", "answered", "--thread", "t1", "--sha", head,
                            "--classification", "NEEDS_REPLY_ONLY",
                            "--generation", "1", "--task", "DEMO-943")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")

    def test_apply_patch_refuses_a_stale_generation(self):
        # rule: R-LEASE-STALE-WRITE
        repo = make_repo()
        call("init", "DEMO-944", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("recover", "--task", "DEMO-944")
        code, _, err = call("apply-patch", "--role", "ppl-code-worker",
                            "--diff", "/no/such/diff.patch",
                            "--generation", "1", "--task", "DEMO-944")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")

    def test_findings_add_refuses_a_stale_generation(self):
        # rule: R-LEASE-STALE-WRITE
        repo = make_repo()
        call("init", "DEMO-9440", "--tracker-url", "u", "--repo", repo,
             "--harness", "example-maven")
        call("recover", "--task", "DEMO-9440")
        code, _, err = call("findings", "add", "--severity", "blocking",
                            "--evidence", "e", "--affected-sha", head_sha(repo),
                            "--affected-path", "a.txt",
                            "--generation", "1", "--task", "DEMO-9440")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")

    def test_findings_add_without_generation_is_schema_invalid(self):
        call("init", "DEMO-9441", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("findings", "add", "--severity", "blocking",
                            "--evidence", "e", "--affected-sha", "a" * 40,
                            "--affected-path", "a.txt", "--task", "DEMO-9441")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_stage_enter_reports_the_generation_to_capture(self):
        call("init", "DEMO-945", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, err = call("stage", "enter", "baseline", "--task", "DEMO-945")
        self.assertEqual(code, 0, err)
        self.assertIn("generation: 1", out)


class TestNameContractSpecRevision(unittest.TestCase):
    """R-BRIEF-SPEC-REVISION-STALE end to end through the real CLI: a rebrief
    that changes name_contract must invalidate a PASS already recorded on the
    same candidate SHA, all the way to `ppl transition done`.
    """

    def _brief_file(self, name_contract, acceptance=None, purpose="p",
                    governing_requirements=None):
        return write_json_file({
            "task_id": "DEMO-960", "purpose": purpose, "success_outcome": "s",
            "governing_requirements": governing_requirements or [],
            "acceptance": acceptance or ["AC-1"],
            "allowed_write_paths": ["**"], "forbidden_paths": [],
            "verification": ["focused-tests"], "open_decisions": [],
            "name_contract": name_contract,
        })

    def test_brief_set_binds_a_spec_revision_to_state(self):
        from ppl import run
        repo = make_repo()
        call("init", "DEMO-960", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        code, out, err = call("brief", "set", self._brief_file({"Foo.bar": "int"}),
                              "--task", "DEMO-960")
        stamp_challenge("DEMO-960")
        self.assertEqual(code, 0, err)
        self.assertIn("spec_revision sha256:", out)
        self.assertTrue(run.load("DEMO-960")["spec_revision"].startswith("sha256:"))

    def _rc_zero(self):
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        return rc_file

    def _to_verified_pass(self, task_id, repo, name_contract, acceptance=None,
                          acceptance_evidence=None):
        """init -> baseline -> brief -> verdict PASS -> both required receipts.

        base_sha is the repo's initial commit, recorded while the run is
        still in the baseline stage (R-BASELINE-NOT-AT-HEAD); a work commit
        on top of it then moves HEAD forward, so the done-gate's boundary
        diff is a single path inside the brief's wide-open "**" write
        boundary and cannot be what blocks the transition below - the only
        thing left to fail is spec_revision.
        """
        from ppl import gitfacts, run
        base = gitfacts.head_sha(repo)
        call("init", task_id, "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", task_id)
        call("baseline", "record", write_json_file({"base_sha": base}), "--task", task_id)
        # `ppl transition done` also checks the lifecycle FSM (planned -> ...
        # -> in_progress -> done) and runs only in stage closure. Irrelevant to
        # what this test proves, so both are set directly rather than walked
        # through every intermediate stage.
        state = run.load(task_id)
        state["status"] = "in_progress"
        state["stage"] = "closure"
        run.save(state)
        call("brief", "set", self._brief_file(name_contract, acceptance=acceptance),
             "--task", task_id)
        stamp_challenge(task_id)
        head = commit(repo, "work-%s.txt" % task_id, "work\n")
        from ppl import run
        revision = run.load(task_id)["spec_revision"]
        doc = write_json_file({
            "task_id": task_id, "candidate_sha": head, "spec_revision": revision,
            "verdict": "PASS",
            "acceptance_evidence": acceptance_evidence or {"AC-1": "evidence:focused-tests"},
            "findings": [], "producer": {"role": "verifier"},
        })
        call("verdict", "record", doc, "--generation", "1", "--task", task_id)
        rc = self._rc_zero()
        call("evidence", "add", "--check", "focused-tests", "--result", "pass",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        call("evidence", "add", "--check", "secret-scan", "--result", "pass",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        call("evidence", "add", "--check", "simplification", "--result", "skip",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)

    def test_transition_done_succeeds_when_the_name_contract_never_changed(self):
        repo = make_repo()
        self._to_verified_pass("DEMO-961", repo, {"Foo.bar": "int"})
        code, out, err = call("transition", "done", "--task", "DEMO-961")
        self.assertEqual(code, 0, err)

    def test_transition_done_stamps_a_current_next_safe_action(self):
        # rule: R-NEXT-SAFE-ACTION-CURRENT (D6, cycle 2 wave D). Before this
        # fix, next_safe_action still read whatever admission wrote at
        # `ppl init` - "complete admission checklist" - even once the task
        # was actually done.
        from ppl import run
        repo = make_repo()
        self._to_verified_pass("DEMO-963", repo, {"Foo.bar": "int"})
        call("transition", "done", "--task", "DEMO-963")
        self.assertEqual(run.load("DEMO-963")["next_safe_action"], "none - task is done")

    def test_a_rebrief_that_changes_the_name_contract_stales_an_already_recorded_pass(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE
        repo = make_repo()
        self._to_verified_pass("DEMO-962", repo, {"Foo.bar": "int"})
        # The verdict above is durably PASS, matching the contract at the time
        # it was recorded. Only now does a rebrief move the run's binding.
        call("brief", "set", self._brief_file({"Foo.bar": "long"}), "--task", "DEMO-962")
        stamp_challenge("DEMO-962")
        code, _, err = call("transition", "done", "--task", "DEMO-962")
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "IMPLEMENTATION_FAILURE")
        self.assertIn("spec_revision", payload["message"])

    def test_a_rebrief_that_shrinks_acceptance_stales_an_already_recorded_pass(self):
        # rule: R-BRIEF-SPEC-REVISION-STALE, widened cycle 2 wave F.
        #
        # THE DEFECT this wave fixes, driven end to end through the real
        # CLI: acceptance = [AC-1, AC-2], only AC-1 ever proven (see
        # acceptance_evidence below), name_contract untouched throughout.
        repo = make_repo()
        self._to_verified_pass("DEMO-9970", repo, {"Foo.bar": "int"},
                              acceptance=["AC-1", "AC-2"],
                              acceptance_evidence={"AC-1": "evidence:focused-tests"})
        # Confirm the pre-fix-relevant state first: AC-2 has no evidence, so
        # the run is blocked on it, not yet on staleness.
        code, _, err = call("transition", "done", "--task", "DEMO-9970")
        self.assertEqual(code, 2)
        self.assertIn("AC-2", json.loads(err)["message"])
        # Drop AC-2 from acceptance. Nothing else changes - not name_contract,
        # not the write boundary. Before this fix: gate OPEN, spec_revision
        # unchanged, because spec_revision hashed name_contract alone.
        call("brief", "set", self._brief_file({"Foo.bar": "int"}, acceptance=["AC-1"]),
             "--task", "DEMO-9970")
        stamp_challenge("DEMO-9970")
        code, _, err = call("transition", "done", "--task", "DEMO-9970")
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "IMPLEMENTATION_FAILURE")
        self.assertIn("spec_revision", payload["message"])

    def test_a_rebrief_that_changes_nothing_gate_relevant_does_not_stale_a_pass(self):
        # The mirror-image risk named in the wave F brief: the fingerprint
        # must not be so wide that an ordinary, legitimate re-brief (the FSM
        # allows implementation/verification back to briefing for exactly
        # this) forces a needless re-verification. purpose and
        # governing_requirements are not read by verdict.done_blockers at
        # all, so changing only them must leave the PASS verdict current.
        repo = make_repo()
        self._to_verified_pass("DEMO-9971", repo, {"Foo.bar": "int"})
        call("brief", "set",
             self._brief_file({"Foo.bar": "int"}, purpose="a materially different purpose",
                              governing_requirements=["REQ-9"]),
             "--task", "DEMO-9971")
        stamp_challenge("DEMO-9971")
        code, out, err = call("transition", "done", "--task", "DEMO-9971")
        self.assertEqual(code, 0, err)


class TestBoundaryChangeSpecRevisionStaleness(unittest.TestCase):
    """Reopened N5 ruling (cycle 2 wave B), re-decided in cycle 2 wave F.

    Wave B refused `ppl boundary set {"write": ["**"]}` outright and decided
    a boundary change should NOT otherwise stale an already-recorded PASS,
    reasoning that `done_blockers` re-reads `boundary.json` fresh on every
    `ppl transition done` call. That same freshness is exactly what makes
    the acceptance-shrink defect in TestNameContractSpecRevision work: a
    criterion the gate re-reads live can be rewritten by the same session
    being gated to fit a diff that already exists. Widening the write
    boundary to newly cover a path an already-produced diff had already
    escaped to is that same move with a different noun. Re-decided: it
    stales the same way a name_contract or acceptance change does.
    """

    def _brief_file(self, write_paths, acceptance=None):
        return write_json_file({
            "task_id": "DEMO-9980", "purpose": "p", "success_outcome": "s",
            "governing_requirements": [], "acceptance": acceptance or ["AC-1"],
            "allowed_write_paths": write_paths, "forbidden_paths": [],
            "verification": ["focused-tests"], "open_decisions": [],
            "name_contract": {"Foo.bar": "int"},
        })

    def _rc_zero(self):
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        return rc_file

    def _to_verified_pass_outside_boundary(self, task_id, repo):
        """Same shape as TestNameContractSpecRevision._to_verified_pass, but
        with a narrow write boundary ("src/**") and a work commit OUTSIDE it
        ("escaped.txt" at the repo root), so the done gate's boundary
        condition (not spec_revision) is what blocks first.
        """
        from ppl import gitfacts, run
        base = gitfacts.head_sha(repo)
        call("init", task_id, "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", task_id)
        call("baseline", "record", write_json_file({"base_sha": base}), "--task", task_id)
        state = run.load(task_id)
        state["status"] = "in_progress"
        state["stage"] = "closure"
        run.save(state)
        call("brief", "set", self._brief_file(["src/**"]), "--task", task_id)
        stamp_challenge(task_id)
        head = commit(repo, "escaped.txt", "work\n")
        revision = run.load(task_id)["spec_revision"]
        doc = write_json_file({
            "task_id": task_id, "candidate_sha": head, "spec_revision": revision,
            "verdict": "PASS", "acceptance_evidence": {"AC-1": "evidence:focused-tests"},
            "findings": [], "producer": {"role": "verifier"},
        })
        call("verdict", "record", doc, "--generation", "1", "--task", task_id)
        rc = self._rc_zero()
        call("evidence", "add", "--check", "focused-tests", "--result", "pass",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        call("evidence", "add", "--check", "secret-scan", "--result", "pass",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        call("evidence", "add", "--check", "simplification", "--result", "skip",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)

    def test_widening_the_write_boundary_after_a_pass_stales_the_verdict(self):
        # rule: R-BOUNDARY-SPEC-REVISION-STALE
        repo = make_repo()
        self._to_verified_pass_outside_boundary("DEMO-9981", repo)
        # Confirm blocked on the boundary condition specifically, before the
        # widen - this is the exact pre-fix opening move (rewrite the
        # criterion the gate reads, then run the gate).
        code, _, err = call("transition", "done", "--task", "DEMO-9981")
        self.assertEqual(code, 2)
        self.assertIn("outside the write boundary", json.loads(err)["message"])
        call("boundary", "set", write_json_file({"write": ["src/**", "escaped.txt"],
                                                  "forbid": []}),
             "--task", "DEMO-9981")
        code, _, err = call("transition", "done", "--task", "DEMO-9981")
        self.assertEqual(code, 2)
        payload = json.loads(err)
        self.assertEqual(payload["error_code"], "IMPLEMENTATION_FAILURE")
        self.assertIn("spec_revision", payload["message"])

    def test_rewriting_the_boundary_to_the_same_content_does_not_stale_the_verdict(self):
        # rule: R-BOUNDARY-SPEC-REVISION-STALE
        # The mirror-image risk: an operator re-running `ppl boundary set`
        # with byte-identical content (a no-op correction, or tooling that
        # always rewrites the file) must not force a needless
        # re-verification of an otherwise-current PASS.
        repo = make_repo()
        from ppl import gitfacts, run
        base = gitfacts.head_sha(repo)
        task_id = "DEMO-9982"
        call("init", task_id, "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        call("stage", "enter", "baseline", "--task", task_id)
        call("baseline", "record", write_json_file({"base_sha": base}), "--task", task_id)
        state = run.load(task_id)
        state["status"] = "in_progress"
        state["stage"] = "closure"
        run.save(state)
        # "work.txt", not "**": cmd_boundary refuses "**" outright
        # (R-BOUNDARY-SET-NO-WILDCARD), so re-setting it below would silently
        # no-op on a refused write rather than exercising a real same-content
        # rewrite - a narrow, settable boundary is required to prove anything.
        call("brief", "set", self._brief_file(["work.txt"]), "--task", task_id)
        stamp_challenge(task_id)
        head = commit(repo, "work.txt", "work\n")
        revision = run.load(task_id)["spec_revision"]
        doc = write_json_file({
            "task_id": task_id, "candidate_sha": head, "spec_revision": revision,
            "verdict": "PASS", "acceptance_evidence": {"AC-1": "evidence:focused-tests"},
            "findings": [], "producer": {"role": "verifier"},
        })
        call("verdict", "record", doc, "--generation", "1", "--task", task_id)
        rc = self._rc_zero()
        call("evidence", "add", "--check", "focused-tests", "--result", "pass",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        call("evidence", "add", "--check", "secret-scan", "--result", "pass",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        call("evidence", "add", "--check", "simplification", "--result", "skip",
             "--sha", head, "--log", "/tmp/l", "--rc", rc, "--generation", "1", "--task", task_id)
        # Re-set the boundary to the same values already on record - and
        # confirm the write actually took, so an accidental refusal (as
        # above) cannot make this pass vacuously.
        code, _, err = call("boundary", "set",
                            write_json_file({"write": ["work.txt"], "forbid": []}),
                            "--task", task_id)
        self.assertEqual(code, 0, err)
        code, out, err = call("transition", "done", "--task", task_id)
        self.assertEqual(code, 0, err)


class TestWatchdog(unittest.TestCase):
    """R-WATCHDOG-DEADLINE: a state-changing command refuses once the current
    stage has overrun its deadline, parking the run and bumping the lease
    generation - through both of the CLI's shared gates.
    """

    def _expire(self, task_id):
        from ppl import run
        state = run.load(task_id)
        state["deadline"] = "2000-01-01T00:00:00+00:00"
        run.save(state)

    def test_a_control_enforce_gated_command_refuses_past_the_deadline(self):
        # rule: R-WATCHDOG-DEADLINE
        call("init", "DEMO-950", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self._expire("DEMO-950")
        code, _, err = call("budget", "spend", "dispatch", "--task", "DEMO-950")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "TIMED_OUT")

    def test_the_refusal_parks_the_run_and_bumps_the_generation(self):
        from ppl import run
        call("init", "DEMO-951", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self._expire("DEMO-951")
        call("budget", "spend", "dispatch", "--task", "DEMO-951")
        state = run.load("DEMO-951")
        self.assertEqual(state["stage"], "parking")
        self.assertEqual(state["lease_generation"], 2)

    def test_evidence_add_refuses_past_the_deadline_even_with_a_current_generation(self):
        # rule: R-WATCHDOG-DEADLINE
        # require_current_generation is evidence/verdict/review-answered/apply-patch's
        # shared gate, and control.enforce is not in any of their call paths.
        repo = make_repo()
        call("init", "DEMO-952", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        self._expire("DEMO-952")
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        code, _, err = call("evidence", "add", "--check", "focused-tests", "--result", "pass",
                            "--sha", "a" * 40, "--log", "/tmp/log.txt", "--rc", rc_file,
                            "--generation", "1", "--task", "DEMO-952")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "TIMED_OUT")

    def test_ppl_park_stays_free_of_the_watchdog_gate(self):
        # Same reasoning as control.enforce's operator-halt case: parking is
        # how a timed-out run is supposed to be recorded, so the one command
        # that records a blocker must not itself be refused for the run
        # already being past its deadline.
        call("init", "DEMO-953", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self._expire("DEMO-953")
        code, _, err = call("park", "--blocker", "OWNER_DECISION",
                            "--safe-next", "ask the owner", "--task", "DEMO-953")
        self.assertEqual(code, 0, err)

    def test_watchdog_check_reports_not_timed_out_before_the_deadline(self):
        call("init", "DEMO-954", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, _ = call("watchdog", "check", "--task", "DEMO-954")
        self.assertEqual(code, 0)
        self.assertIn("not timed out", out)

    def test_watchdog_check_parks_and_reports_timed_out(self):
        # rule: R-WATCHDOG-DEADLINE
        call("init", "DEMO-955", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self._expire("DEMO-955")
        code, _, err = call("watchdog", "check", "--task", "DEMO-955")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "TIMED_OUT")

    def test_recover_reports_the_timeout_as_an_exit_zero_fact(self):
        # Matches the existing convention (see SKILL.md): a parked run is an
        # exit-0 fact recover surfaces in its actions, not one of recover's
        # own exit-2 cases (EXTERNAL_BLOCKED, OWNER_DECISION).
        repo = make_repo()
        call("init", "DEMO-956", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        self._expire("DEMO-956")
        code, out, _ = call("recover", "--task", "DEMO-956")
        self.assertEqual(code, 0)
        self.assertIn("TIMED_OUT", out)

    def test_a_control_enforce_gated_command_refreshes_the_deadline(self):
        # (a), at the CLI level: rule: R-WATCHDOG-DEADLINE
        from ppl import run, store
        call("init", "DEMO-957", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        state = run.load("DEMO-957")
        state["deadline"] = store.deadline_in(60)
        run.save(state)
        code, _, err = call("budget", "spend", "dispatch", "--task", "DEMO-957")
        self.assertEqual(code, 0, err)
        self.assertGreater(run.load("DEMO-957")["deadline"], store.deadline_in(3600))

    def test_a_stale_generation_write_does_not_refresh_the_deadline(self):
        # (c) rule: R-WATCHDOG-DEADLINE
        from ppl import run, store
        repo = make_repo()
        call("init", "DEMO-958", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        # Simulate a recover that ran elsewhere and left this attempt stale.
        run.bump_generation("DEMO-958")
        state = run.load("DEMO-958")
        marker = store.deadline_in(600)
        state["deadline"] = marker
        run.save(state)
        rc_file = tempfile.mkstemp()[1]
        Path(rc_file).write_text("0", encoding="utf-8")
        code, _, err = call("evidence", "add", "--check", "focused-tests", "--result", "pass",
                            "--sha", "a" * 40, "--log", "/tmp/log.txt", "--rc", rc_file,
                            "--generation", "1", "--task", "DEMO-958")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(run.load("DEMO-958")["deadline"], marker)

    def test_review_answered_with_a_stale_generation_does_not_refresh_the_deadline(self):
        # rule: R-WATCHDOG-DEADLINE (W2, cycle 2 wave B). `review answered` is
        # the one write path that calls control.enforce (which used to refresh
        # unconditionally on its own success path) BEFORE
        # require_current_generation (which is the call that actually refuses
        # a stale generation) - so a refused stale write still extended the
        # deadline via enforce's own refresh, before the generation check ever
        # ran. That contradicts this file's own stated invariant: "each only
        # after their own gate has passed, so a stale write can never refresh
        # it".
        from ppl import gitfacts, run, store
        repo = make_repo()
        call("init", "DEMO-959", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        head = gitfacts.head_sha(repo)
        run.bump_generation("DEMO-959")
        state = run.load("DEMO-959")
        marker = store.deadline_in(600)
        state["deadline"] = marker
        run.save(state)
        code, _, err = call("review", "answered", "--thread", "t1", "--sha", head,
                            "--classification", "NEEDS_REPLY_ONLY",
                            "--generation", "1", "--task", "DEMO-959")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "ILLEGAL_TRANSITION")
        self.assertEqual(run.load("DEMO-959")["deadline"], marker)

    def test_a_paused_run_is_not_refreshed_merely_for_being_asked_to_act(self):
        # rule: R-WATCHDOG-DEADLINE
        # control.enforce's own invariant: refresh only after the
        # control-record check passes, so a halted run is not refreshed
        # merely for being asked to act (round-2 review: this was asserted
        # but not proven - a mutation moving the refresh above `record =
        # read(task_id)` left the suite green).
        from ppl import run, store
        call("init", "DEMO-971", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        call("control", "set", "pause", "--task", "DEMO-971")
        state = run.load("DEMO-971")
        marker = store.deadline_in(3600 * 24)
        state["deadline"] = marker
        run.save(state)
        code, _, err = call("budget", "spend", "dispatch", "--task", "DEMO-971")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        self.assertEqual(run.load("DEMO-971")["deadline"], marker)

    def test_a_malformed_control_file_is_not_refreshed_merely_for_being_asked_to_act(self):
        # rule: R-WATCHDOG-DEADLINE
        # Same invariant, same raise site (control.enforce's `invalid`
        # branch), different way of reaching it.
        from ppl import paths, run, store
        call("init", "DEMO-972", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        store.write_json(paths.run_dir("DEMO-972") / "control.json", {"command": "pause"})
        state = run.load("DEMO-972")
        marker = store.deadline_in(3600 * 24)
        state["deadline"] = marker
        run.save(state)
        code, _, err = call("budget", "spend", "dispatch", "--task", "DEMO-972")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        self.assertEqual(run.load("DEMO-972")["deadline"], marker)


class TestBinEntryPoint(unittest.TestCase):
    """C4: README tells the operator to put bin/ on PATH and the skill runs
    bare `ppl ...` from a worktree, so the launcher has to find its own root.
    """

    def test_bin_ppl_runs_from_an_unrelated_working_directory(self):
        launcher = Path(__file__).resolve().parents[1] / "bin" / "ppl"
        elsewhere = tmphome.mkdtemp(prefix="pplcwd-")
        env = dict(os.environ)
        env["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        env.pop("PYTHONPATH", None)
        try:
            result = subprocess.run([str(launcher), "--help"], cwd=elsewhere, env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("usage: ppl", result.stdout)
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)
            shutil.rmtree(env["PPL_HOME"], ignore_errors=True)

    def test_bin_ppl_under_an_old_python3_names_the_version_it_found(self):
        # The traceback an older python3 gave (No module named 'tomllib') named
        # neither the cause nor the fix.
        found = fixtures.older_system_python()
        if found is None:
            self.skipTest("no python3 older than 3.11 at %s" % fixtures.SYSTEM_PYTHON)
        interpreter, version = found
        launcher = Path(__file__).resolve().parents[1] / "bin" / "ppl"
        shim = tmphome.mkdtemp(prefix="pplshim-")
        os.symlink(interpreter, os.path.join(shim, "python3"))
        env = fixtures.isolated_env(PATH=shim + os.pathsep + "/usr/bin:/bin")
        env.pop("PYTHONPATH", None)
        result = subprocess.run([str(launcher), "doctor"], cwd=shim, env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn(version, lines[0])
        self.assertIn("3.11", lines[0])
        self.assertNotIn("Traceback", result.stderr)


class TestControlEnforceOnTheFourMissingGuards(unittest.TestCase):
    """R3: control.enforce was missing from boundary set, mr set, review approved
    and budget spend. A paused run could have the write boundary the guard hook
    reads rewritten from under it, or its budget/mr/review state advanced, while
    halted. park and escalate must stay free - an operator halt must not block
    recording why the run stopped - and control set resume must still work while
    the pause it lifts is itself in force. review answered joined the gated set
    in the cycle-2 review round: it writes run state (the review ledger) too, and
    is not a park/escalate-style "record why we stopped" carve-out.
    """

    def test_boundary_set_is_refused_while_paused(self):
        # rule: R-OPERATOR-HALT-CLI
        from ppl import control
        call("init", "DEMO-900", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        control.set_command("DEMO-900", "pause")
        try:
            bound_file = write_json_file({"write": ["src/**"], "forbid": []})
            code, _, err = call("boundary", "set", bound_file, "--task", "DEMO-900")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        finally:
            control.clear("DEMO-900")

    def test_mr_set_is_refused_while_paused(self):
        from ppl import control
        repo = make_repo()
        call("init", "DEMO-901", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        control.set_command("DEMO-901", "pause")
        try:
            code, _, err = call("mr", "set", "--iid", "1",
                                "--url", "https://gitlab/x/-/mr/1", "--task", "DEMO-901")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        finally:
            control.clear("DEMO-901")

    def test_review_approved_is_refused_while_paused(self):
        from ppl import control, gitfacts
        repo = make_repo()
        call("init", "DEMO-902", "--tracker-url", "u", "--repo", repo, "--harness", "example-maven")
        head = gitfacts.head_sha(repo)
        control.set_command("DEMO-902", "pause")
        try:
            code, _, err = call("review", "approved", "--sha", head, "--task", "DEMO-902")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        finally:
            control.clear("DEMO-902")

    def test_review_answered_is_refused_while_paused(self):
        # Closed on review, cycle 2: "answered" writes run state (the review
        # ledger, threads.json) same as "approved" does, so it is one of the
        # gated writes now too, not a park/escalate-style carve-out.
        from ppl import control
        call("init", "DEMO-903", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        control.set_command("DEMO-903", "pause")
        try:
            code, _, err = call("review", "answered", "--thread", "t1", "--sha", "a" * 40,
                                "--classification", "NEEDS_REPLY_ONLY", "--task", "DEMO-903")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        finally:
            control.clear("DEMO-903")

    def test_budget_spend_is_refused_while_paused(self):
        # rule: R-OPERATOR-HALT-CLI
        from ppl import control
        call("init", "DEMO-904", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        control.set_command("DEMO-904", "pause")
        try:
            code, _, err = call("budget", "spend", "dispatch", "--task", "DEMO-904")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "OWNER_DECISION")
        finally:
            control.clear("DEMO-904")

    def test_park_still_works_while_paused(self):
        from ppl import control
        call("init", "DEMO-905", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        control.set_command("DEMO-905", "pause")
        try:
            code, _, err = call("park", "--blocker", "OWNER_DECISION",
                                "--safe-next", "wait for the operator", "--task", "DEMO-905")
            self.assertEqual(code, 0, err)
        finally:
            control.clear("DEMO-905")

    def test_escalate_still_works_while_paused(self):
        from ppl import control
        call("init", "DEMO-906", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        control.set_command("DEMO-906", "pause")
        try:
            code, _, err = call("escalate", "--code", "OWNER_DECISION",
                                "--question", "which way?", "--task", "DEMO-906")
            self.assertEqual(code, 0, err)
        finally:
            control.clear("DEMO-906")

    def test_control_set_resume_still_works_while_paused(self):
        from ppl import control
        call("init", "DEMO-907", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        control.set_command("DEMO-907", "pause")
        code, _, err = call("control", "set", "resume", "--task", "DEMO-907")
        self.assertEqual(code, 0, err)
        # resume itself is not a HALTING command, so the run is unblocked again
        control.enforce("DEMO-907")


class TestInitIsolatedFlag(unittest.TestCase):
    def test_init_defaults_isolated_true(self):
        call("init", "DEMO-908", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, _ = call("show", "--task", "DEMO-908", "--json")
        self.assertIs(json.loads(out)["isolated"], True)

    def test_init_no_isolated_records_false(self):
        call("init", "DEMO-909", "--tracker-url", "u", "--repo", "/tmp/repo", "--no-isolated",
             "--harness", "example-maven")
        code, out, _ = call("show", "--task", "DEMO-909", "--json")
        self.assertIs(json.loads(out)["isolated"], False)


def write_global_guard_registration(matcher=doctor.RECOMMENDED_MATCHER):
    doc = {"hooks": {"PreToolUse": [{
        "matcher": matcher,
        "hooks": [{"type": "command",
                  "command": "python3 /home/dev/antline/hooks/ppl_guard.py",
                  "timeout": 5}]}]}}
    path = doctor.global_settings_path()
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def write_local_guard_registration(cwd, matcher=doctor.RECOMMENDED_MATCHER):
    path = Path(cwd) / ".claude" / "settings.local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {"hooks": {"PreToolUse": [{
        "matcher": matcher,
        "hooks": [{"type": "command",
                  "command": "python3 /home/dev/antline/hooks/ppl_guard.py",
                  "timeout": 5}]}]}}
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class TestInitVerifiesTheGuardMatcher(unittest.TestCase):
    """A registered guard whose matcher never passes a tool name to it is a
    denial that cannot fire, whatever the code behind it says - the Jira gate
    was dead this way on the machine this was built on. ppl init refuses to
    start a run under such a registration, isolated or not: --no-isolated
    waives the isolation check, not this one.
    """

    EDITING_TOOLS_ONLY = "Edit|Write|MultiEdit|NotebookEdit"

    def setUp(self):
        self.global_path = doctor.global_settings_path()
        if self.global_path.exists():
            self.global_path.unlink()
        self.local_dir = tmphome.mkdtemp(prefix="ppllocaldir-")
        self.previous = os.environ.get("PPL_LOCAL_SETTINGS_DIR")
        os.environ["PPL_LOCAL_SETTINGS_DIR"] = self.local_dir

    def tearDown(self):
        if self.global_path.exists():
            self.global_path.unlink()
        if self.previous is None:
            del os.environ["PPL_LOCAL_SETTINGS_DIR"]
        else:
            os.environ["PPL_LOCAL_SETTINGS_DIR"] = self.previous

    def test_init_refuses_when_the_registered_matcher_leaves_guarded_tools_uncovered(self):
        # rule: R-GUARD-MATCHER
        write_local_guard_registration(self.local_dir, matcher=self.EDITING_TOOLS_ONLY)
        code, _, err = call("init", "DEMO-9910", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--harness", "example-maven")
        self.assertEqual(code, 2)
        doc = json.loads(err)
        self.assertEqual(doc["error_code"], "SCHEMA_INVALID")
        self.assertIn("settings.local.json", doc["message"])
        self.assertIn("Bash", doc["message"])
        self.assertIn(doctor.RECOMMENDED_MATCHER, doc["message"])

    def test_init_no_isolated_still_refuses_an_incomplete_matcher(self):
        # rule: R-GUARD-MATCHER
        # The hook is armed globally here and cannot see Bash in any session.
        write_global_guard_registration(matcher=self.EDITING_TOOLS_ONLY)
        code, _, err = call("init", "DEMO-9911", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--no-isolated", "--harness", "example-maven")
        self.assertEqual(code, 2)
        doc = json.loads(err)
        self.assertEqual(doc["error_code"], "SCHEMA_INVALID")
        self.assertIn("Bash", doc["message"])

    def test_init_is_not_refused_when_the_matcher_covers_every_guarded_tool(self):
        # rule: R-GUARD-MATCHER
        write_local_guard_registration(self.local_dir)
        code, _, err = call("init", "DEMO-9912", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--harness", "example-maven")
        self.assertEqual(code, 0, err)


class TestInitVerifiesTheGuardHookRegistration(unittest.TestCase):
    """Finding 1(b): isolated=true asserts a fact - that the guard hook is
    registered only where an isolated run assumes it is - that nothing
    checked before. ppl init now refuses the one configuration that makes
    that assertion silently false: hook registered only globally.
    """

    def setUp(self):
        self.global_path = doctor.global_settings_path()
        if self.global_path.exists():
            self.global_path.unlink()

    def tearDown(self):
        if self.global_path.exists():
            self.global_path.unlink()

    def test_init_refuses_when_hook_is_registered_only_globally(self):
        # rule: R-INIT-ISOLATION-CHECK
        write_global_guard_registration()
        code, _, err = call("init", "DEMO-920", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--harness", "example-maven")
        self.assertEqual(code, 2)
        doc = json.loads(err)
        self.assertEqual(doc["error_code"], "SCHEMA_INVALID")
        self.assertIn("settings.local.json", doc["message"])
        self.assertIn("--no-isolated", doc["message"])

    def test_init_no_isolated_is_not_refused_when_hook_is_registered_only_globally(self):
        write_global_guard_registration()
        code, _, err = call("init", "DEMO-921", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--no-isolated", "--harness", "example-maven")
        self.assertEqual(code, 0, err)

    def test_init_is_not_refused_when_the_hook_is_registered_locally(self):
        local_dir = tmphome.mkdtemp(prefix="ppllocaldir-")
        previous = os.environ.get("PPL_LOCAL_SETTINGS_DIR")
        os.environ["PPL_LOCAL_SETTINGS_DIR"] = local_dir
        write_local_guard_registration(local_dir)
        try:
            code, _, err = call("init", "DEMO-922", "--tracker-url", "u", "--repo", "/tmp/repo",
                                "--harness", "example-maven")
            self.assertEqual(code, 0, err)
        finally:
            if previous is None:
                del os.environ["PPL_LOCAL_SETTINGS_DIR"]
            else:
                os.environ["PPL_LOCAL_SETTINGS_DIR"] = previous

    def test_init_is_not_refused_when_the_hook_is_registered_nowhere(self):
        # "not armed yet" (README) is the pre-existing, honestly documented
        # state, not the mismatch this refusal targets.
        code, _, err = call("init", "DEMO-923", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--harness", "example-maven")
        self.assertEqual(code, 0, err)

    def test_init_refuses_when_the_hook_is_registered_both_locally_and_globally(self):
        # rule: R-INIT-ISOLATION-CHECK
        # Round 3, Finding 2: a working local registration does not make an
        # armed global one safe.
        local_dir = tmphome.mkdtemp(prefix="ppllocaldir-")
        previous = os.environ.get("PPL_LOCAL_SETTINGS_DIR")
        os.environ["PPL_LOCAL_SETTINGS_DIR"] = local_dir
        write_local_guard_registration(local_dir)
        write_global_guard_registration()
        try:
            code, _, err = call("init", "DEMO-926", "--tracker-url", "u", "--repo", "/tmp/repo",
                                "--harness", "example-maven")
            self.assertEqual(code, 2)
            doc = json.loads(err)
            self.assertEqual(doc["error_code"], "SCHEMA_INVALID")
            self.assertIn("--no-isolated", doc["message"])
        finally:
            if previous is None:
                del os.environ["PPL_LOCAL_SETTINGS_DIR"]
            else:
                os.environ["PPL_LOCAL_SETTINGS_DIR"] = previous

    def test_init_refuses_when_local_is_confirmed_but_global_is_unreadable(self):
        # rule: R-INIT-ISOLATION-CHECK
        # Round 3, Finding 1: a confirmed local yes must not rescue an
        # unreadable global file - the global side might genuinely be armed.
        local_dir = tmphome.mkdtemp(prefix="ppllocaldir-")
        previous = os.environ.get("PPL_LOCAL_SETTINGS_DIR")
        os.environ["PPL_LOCAL_SETTINGS_DIR"] = local_dir
        write_local_guard_registration(local_dir)
        self.global_path.write_text("// my settings\nnot json", encoding="utf-8")
        try:
            code, _, err = call("init", "DEMO-927", "--tracker-url", "u", "--repo", "/tmp/repo",
                                "--harness", "example-maven")
            self.assertEqual(code, 2)
            doc = json.loads(err)
            self.assertEqual(doc["error_code"], "SCHEMA_INVALID")
            self.assertIn("confirmed", doc["message"])
        finally:
            if previous is None:
                del os.environ["PPL_LOCAL_SETTINGS_DIR"]
            else:
                os.environ["PPL_LOCAL_SETTINGS_DIR"] = previous

    def test_init_refuses_when_the_global_settings_file_cannot_be_parsed(self):
        # Finding 1, round 2: a "//" comment in a hand-edited settings.json is
        # an ordinary edit, not a contrived case - it must refuse, not read as
        # "no hook here" just because this check could not parse the file.
        self.global_path.write_text('// my settings\n{"hooks": {}}', encoding="utf-8")
        code, _, err = call("init", "DEMO-924", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--harness", "example-maven")
        self.assertEqual(code, 2)
        doc = json.loads(err)
        self.assertEqual(doc["error_code"], "SCHEMA_INVALID")
        self.assertIn("confirmed", doc["message"])

    def test_init_no_isolated_is_not_refused_when_a_settings_file_cannot_be_parsed(self):
        self.global_path.write_text('// my settings\n{"hooks": {}}', encoding="utf-8")
        code, _, err = call("init", "DEMO-925", "--tracker-url", "u", "--repo", "/tmp/repo",
                            "--no-isolated", "--harness", "example-maven")
        self.assertEqual(code, 0, err)


class TestDoctorCommand(unittest.TestCase):
    def setUp(self):
        self.global_path = doctor.global_settings_path()
        if self.global_path.exists():
            self.global_path.unlink()

    def tearDown(self):
        if self.global_path.exists():
            self.global_path.unlink()

    def test_doctor_reports_none_when_nowhere_registered(self):
        call("init", "DEMO-930", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, out, err = call("doctor", "--task", "DEMO-930")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["registration"], "none")
        self.assertIsNone(doc["mismatch"])

    def test_doctor_reports_the_mismatch_against_an_isolated_run(self):
        call("init", "DEMO-931", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        write_global_guard_registration()
        code, out, err = call("doctor", "--task", "DEMO-931")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["registration"], "global")
        self.assertIsNotNone(doc["mismatch"])

    def test_doctor_without_an_active_run_still_reports_registration(self):
        code, out, err = call("doctor", "--task", "DEMO-NOPE-999")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["registration"], "none")
        self.assertIsNone(doc["isolated"])

    def test_doctor_output_names_the_inspected_paths(self):
        # Finding 3, round 2: PPL_CLAUDE_HOME/PPL_LOCAL_SETTINGS_DIR make this
        # check trust its own environment (DESIGN 5.4's residual list) - the
        # least this command can do is say which paths it actually looked at.
        code, out, err = call("doctor")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["local"]["path"], str(doctor.local_settings_path()))
        self.assertEqual(doc["global"]["path"], str(doctor.global_settings_path()))

    def test_doctor_reports_setup_models_harness_and_forge(self):
        self.addCleanup(seed_setup)
        config.update("claude", {"workspace": tmphome.mkdtemp(prefix="pplworkspace-")})
        code, out, err = call("doctor")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["code_root"], str(paths.root()))
        self.assertEqual(doc["data_root"], str(paths.home()))
        self.assertEqual(doc["setup"]["missing"], [])
        self.assertEqual(doc["harness"]["active"], "example-maven")
        self.assertTrue(doc["harness"]["valid"])
        self.assertEqual(doc["forge"]["kind"], "gitlab")
        self.assertEqual({row["state"] for row in doc["models"]}, {"verified"})

    def test_doctor_exits_zero_on_an_unreadable_config(self):
        self.addCleanup(seed_setup)
        paths.config_file().write_text("[setup\n", encoding="utf-8")
        code, out, err = call("doctor")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["setup"]["missing"], list(setup.STEPS))
        self.assertIn("config.toml", doc["config_problem"])

    def test_doctor_reports_unknown_for_an_unparseable_settings_file(self):
        self.global_path.write_text('// my settings\n{"hooks": {}}', encoding="utf-8")
        call("init", "DEMO-932", "--tracker-url", "u", "--repo", "/tmp/repo", "--no-isolated",
             "--harness", "example-maven")
        code, out, err = call("doctor", "--task", "DEMO-932")
        self.assertEqual(code, 0, err)
        doc = json.loads(out.splitlines()[0])
        self.assertEqual(doc["registration"], "unknown")
        self.assertIsNotNone(doc["global"]["detail"])
        # isolated=false for this run: reporting registration honestly is not
        # the same claim as refusing to start it, so no mismatch here either.
        self.assertIsNone(doc["mismatch"])


class TestMetricsCommandPrintsUndefinedNotZero(unittest.TestCase):
    """metrics.compute() itself is tested exhaustively in test_metrics.py.
    This is the one test that goes through the real `ppl metrics` CLI path -
    without it, a later edit to cmd_metrics that formats a bare `value or 0.0`
    would pass every metrics.py test while silently breaking the actual
    deliverable: what gets printed.
    """

    def setUp(self):
        # An isolated, empty fleet: metrics.compute() has no per-task scope
        # by design (it reads every run under PPL_HOME), so this test cannot
        # share the module-level PPL_HOME with the rest of this file without
        # some other test's verdict silently making a metric look defined.
        # Restored in tearDown so later tests in this module keep seeing the
        # module-level PPL_HOME they were written against.
        self._real_ppl_home = os.environ["PPL_HOME"]
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._real_ppl_home

    def test_every_metric_prints_undefined_with_null_value_on_an_empty_fleet(self):
        code, out, err = call("metrics")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        for metric, entry in report["overall"].items():
            with self.subTest(metric=metric):
                self.assertFalse(entry["defined"], "%s reported defined on no data" % metric)
                self.assertIsNone(entry["value"], "%s printed a value instead of null" % metric)
                self.assertTrue(entry["reason"])


class TestLabelAndMetricsSurviveDestroyThroughTheRealCli(unittest.TestCase):
    """The blocking defect found in review round 1, reproduced end to end
    through the real `ppl` CLI rather than through metrics.py directly:
    `ppl init` -> `ppl label` -> `ppl destroy --scope local` -> `ppl label`
    again -> `ppl metrics` used to fail the second label with "no run for
    ... Run ppl init first" and report the metric as undefined forever,
    because both record_label's existence check and every metrics reader
    only ever looked at the live run directory `ppl destroy` had just moved
    away [R-METRIC-SCANS-ARCHIVE].
    """

    def setUp(self):
        self._real_ppl_home = os.environ["PPL_HOME"]
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        seed_routing()
        mark_all_verified()
        seed_setup()

    def tearDown(self):
        os.environ["PPL_HOME"] = self._real_ppl_home

    def test_label_destroy_label_again_then_metrics_sees_both(self):
        # rule: R-METRIC-SCANS-ARCHIVE
        from ppl import paths, store
        call("init", "DEMO-44444", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        store.write_json(paths.run_dir("DEMO-44444") / "verdicts" / "deadbeef.json", {
            "task_id": "DEMO-44444", "candidate_sha": "deadbeef",
            "spec_revision": "sha256:x", "verdict": "PASS",
            "acceptance_evidence": {}, "findings": [],
            "producer": {"provider": "claude-code-subagent", "model": "opus", "params_hash": "h"},
        })
        code, _, err = call("label", "--sha", "deadbeef", "--actual", "defective",
                            "--task", "DEMO-44444")
        self.assertEqual(code, 0, err)

        code, _, err = call("destroy", "--scope", "local", "--task", "DEMO-44444")
        self.assertEqual(code, 0, err)

        # This is the call that used to exit 2 with "no run for DEMO-44444.
        # Run ppl init first" - the run really does still exist, archived.
        code, _, err = call("label", "--sha", "c0ffee", "--actual", "correct",
                            "--task", "DEMO-44444")
        self.assertEqual(code, 0, err)

        code, out, err = call("metrics")
        self.assertEqual(code, 0, err)
        report = json.loads(out)
        entry = report["producers"]["claude-code-subagent:opus"]["defect_escape_rate"]
        # This is the reason string that used to be printed while lying: it
        # recommends `ppl label`, which had in fact already been run twice.
        self.assertTrue(entry["defined"],
                        "still undefined after two labels - reason: %r" % entry["reason"])
        self.assertAlmostEqual(entry["value"], 1.0)


class TestLabelIsNotLivenessOrLease(unittest.TestCase):
    """ppl label records a human's ground truth about a past SHA. It is
    deliberately NOT wired through control.enforce or
    require_current_generation: see ppl/metrics.py:record_label.
    """

    def _expire(self, task_id):
        from ppl import run
        state = run.load(task_id)
        state["deadline"] = "2000-01-01T00:00:00+00:00"
        run.save(state)
        return state["deadline"]

    def test_label_succeeds_even_though_the_deadline_has_already_passed(self):
        # rule: R-LABEL-NO-LIVENESS-REFRESH
        # Any control.enforce-gated command would refuse this with TIMED_OUT
        # (see TestWatchdog) - labelling a past SHA must not be one of them.
        call("init", "DEMO-980", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self._expire("DEMO-980")
        code, _, err = call("label", "--sha", "a" * 40, "--actual", "defective",
                            "--task", "DEMO-980")
        self.assertEqual(code, 0, err)

    def test_label_does_not_refresh_the_deadline(self):
        # rule: R-LABEL-NO-LIVENESS-REFRESH
        from ppl import run
        call("init", "DEMO-981", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        expired = self._expire("DEMO-981")
        call("label", "--sha", "b" * 40, "--actual", "correct", "--task", "DEMO-981")
        self.assertEqual(run.load("DEMO-981")["deadline"], expired)

    def test_label_does_not_park_the_run(self):
        # A run this stale would be found and parked TIMED_OUT by
        # control.enforce or require_current_generation - ppl label calls
        # neither, so the stage must stay exactly where it was.
        from ppl import run
        call("init", "DEMO-982", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        self._expire("DEMO-982")
        call("label", "--sha", "c" * 40, "--actual", "defective", "--task", "DEMO-982")
        self.assertEqual(run.load("DEMO-982")["stage"], "admission")

    def test_label_does_not_bump_the_lease_generation(self):
        # rule: R-LABEL-NO-LEASE-FENCE
        from ppl import run
        call("init", "DEMO-983", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        before = run.load("DEMO-983")["lease_generation"]
        code, _, err = call("label", "--sha", "d" * 40, "--actual", "correct",
                            "--task", "DEMO-983")
        self.assertEqual(code, 0, err)
        self.assertEqual(run.load("DEMO-983")["lease_generation"], before)

    def test_label_takes_no_generation_argument_at_all(self):
        # rule: R-LABEL-NO-LEASE-FENCE
        # Contrast evidence/verdict/review-answered/apply-patch, which all
        # require --generation (see TestLeaseFencesTheWritePaths):
        # requiring one here would be theatre, since a label is a fact about
        # an already-past SHA, never a write a dispatched attempt races.
        call("init", "DEMO-984", "--tracker-url", "u", "--repo", "/tmp/repo",
             "--harness", "example-maven")
        code, _, err = call("label", "--sha", "e" * 40, "--actual", "defective",
                            "--task", "DEMO-984", "--generation", "1")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")


class TestInitModelCheck(unittest.TestCase):
    def setUp(self):
        seed_routing()
        seed_setup()
        if paths.models_file().exists():
            paths.models_file().unlink()
        self.addCleanup(mark_all_verified)

    def test_init_refuses_when_models_are_unverified(self):
        # rule: R-INIT-MODELS-VERIFIED
        code, out, err = call("init", "DEMO-501", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "example-maven")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "MODELS_UNVERIFIED")
        self.assertFalse(paths.run_dir("DEMO-501").exists())

    def test_init_passes_when_verified(self):
        mark_all_verified()
        code, out, err = call("init", "DEMO-502", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "example-maven")
        self.assertEqual(code, 0, err)
        state = json.loads((paths.run_dir("DEMO-502") / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["model_check"], "verified")
        code, out, err = call("status", "--task", "DEMO-502")
        self.assertIn("Models:   verified", out)

    def test_skip_model_check_is_recorded(self):
        code, out, err = call("init", "DEMO-503", "--tracker-url", "u", "--repo", make_repo(),
                              "--skip-model-check", "--harness", "example-maven")
        self.assertEqual(code, 0, err)
        state = json.loads((paths.run_dir("DEMO-503") / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["model_check"], "skipped")
        code, out, err = call("status", "--task", "DEMO-503")
        self.assertIn("Models:   skipped", out)

    def test_the_init_log_event_carries_the_model_check(self):
        call("init", "DEMO-504", "--tracker-url", "u", "--repo", make_repo(), "--skip-model-check",
             "--harness", "example-maven")
        log = (paths.run_dir("DEMO-504") / "log.jsonl").read_text(encoding="utf-8")
        event = json.loads(log.splitlines()[0])
        self.assertEqual((event["event"], event["model_check"]), ("init", "skipped"))

    def test_the_refusal_names_the_models_and_the_way_out(self):
        code, out, err = call("init", "DEMO-505", "--tracker-url", "u", "--repo", make_repo(),
                              "--risk", "high", "--harness", "example-maven")
        self.assertEqual(code, 2)
        message = json.loads(err)["message"]
        self.assertIn("ppl models verify", message)
        self.assertIn("--skip-model-check", message)



class TestInitHarness(unittest.TestCase):
    def setUp(self):
        seed_routing()
        mark_all_verified()
        seed_setup()
        self.addCleanup(seed_setup)

    def test_init_with_harness_writes_the_snapshot(self):
        code, out, err = call("init", "DEMO-2601", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "example-maven")
        self.assertEqual(code, 0, err)
        state = json.loads((paths.run_dir("DEMO-2601") / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["harness"]["name"], "example-maven")
        snap = json.loads((paths.run_dir("DEMO-2601") / "harness.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["forge"]["kind"], "gitlab")
        self.assertEqual(snap["shapes"][0]["argv0"], "mvn")
        self.assertEqual(snap["tracker"]["mcp_prefixes"][0], "mcp__atlassian__")
        self.assertEqual(snap["task"]["key_pattern"], "^[A-Z][A-Z0-9]+-[0-9]+$")

    def test_init_refuses_a_task_id_outside_the_harness_pattern(self):
        # rule: R-INIT-TASK-KEY-PATTERN
        path = paths.harness_dir("example-maven") / "harness.toml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            'key_pattern = "^[A-Z][A-Z0-9]+-[0-9]+$"', 'key_pattern = "^ACME-[0-9]+$"'),
            encoding="utf-8")
        code, out, err = call("init", "DEMO-608", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "example-maven")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertIn("^ACME-[0-9]+$", json.loads(err)["message"])

    def test_init_refuses_a_harness_that_is_not_the_applied_one(self):
        # rule: R-INIT-HARNESS-APPLIED
        fixtures.install_harness("example-generic")
        code, out, err = call("init", "DEMO-609", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "example-generic")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_NOT_APPLIED")
        self.assertIn("ppl harness apply example-generic", json.loads(err)["message"])

    def test_init_refuses_a_harness_directory_outside_the_data_root(self):
        # rule: R-INIT-HARNESS-APPLIED
        outside = Path(tmphome.mkdtemp(prefix="pplharness-")) / "example-maven"
        shutil.copytree(fixtures.SOURCE / "defaults" / "harnesses" / "example-maven", outside)
        code, out, err = call("init", "DEMO-606", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", str(outside))
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_NOT_APPLIED")
        self.assertIn("is not an installed harness", json.loads(err)["message"])
        self.assertFalse(paths.run_dir("DEMO-606").exists())

    def test_init_records_the_installed_directory_name(self):
        fixtures.install_harness("example-maven", as_name="mine")
        config.set_active_harness("mine")
        code, out, err = call("init", "DEMO-607", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "mine")
        self.assertEqual(code, 0, err)
        state = json.loads((paths.run_dir("DEMO-607") / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["harness"]["name"], "mine")
        self.assertEqual(state["harness"]["path"], str(paths.harness_dir("mine")))
        snap = json.loads((paths.run_dir("DEMO-607") / "harness.json").read_text(encoding="utf-8"))
        self.assertEqual(snap["name"], "mine")

    def test_init_refuses_when_no_harness_was_ever_applied(self):
        # rule: R-INIT-HARNESS-APPLIED
        config.set_active_harness("")
        code, out, err = call("init", "DEMO-616", "--tracker-url", "u", "--repo", make_repo(),
                              "--harness", "example-maven")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_NOT_APPLIED")


class TestInitRequiresSetup(unittest.TestCase):
    def setUp(self):
        seed_setup()
        self.addCleanup(seed_setup)

    def init(self, task_id, *extra):
        return call("init", task_id, "--tracker-url", "u", "--repo", make_repo(), *extra)

    def test_init_refuses_when_setup_is_incomplete(self):
        # rule: R-INIT-SETUP-COMPLETE
        config_path = paths.config_file()
        saved = config_path.read_text(encoding="utf-8") if config_path.exists() else None
        if config_path.exists():
            config_path.unlink()
        try:
            code, out, err = call("init", "DEMO-801", "--tracker-url", "u", "--repo", make_repo(),
                                  "--harness", "example-maven")
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(err)["error_code"], "SETUP_INCOMPLETE")
            for step in ("models", "harness", "apply"):
                self.assertIn("`ppl setup %s`" % step, json.loads(err)["message"])
            self.assertFalse(paths.run_dir("DEMO-801").exists())
        finally:
            if saved is not None:
                config_path.write_text(saved, encoding="utf-8")

    def test_the_refusal_names_the_missing_step_and_its_command(self):
        # rule: R-INIT-SETUP-COMPLETE
        config.mark_step("harness", None)
        code, out, err = self.init("DEMO-802", "--harness", "example-maven")
        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertEqual(error["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("ppl setup harness", error["message"])
        self.assertFalse(paths.run_dir("DEMO-802").exists())

    def test_a_skipped_required_step_is_still_missing(self):
        config.mark_step("apply", "skipped")
        code, out, err = self.init("DEMO-803", "--harness", "example-maven")
        self.assertEqual(code, 2)
        self.assertIn("ppl setup apply", json.loads(err)["message"])

    def test_a_missing_models_step_refuses_unless_the_model_check_is_skipped(self):
        config.mark_step("models", None)
        code, out, err = self.init("DEMO-804", "--harness", "example-maven")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("ppl setup models", json.loads(err)["message"])
        code, out, err = self.init("DEMO-805", "--harness", "example-maven", "--skip-model-check")
        self.assertEqual(code, 0, err)

    def test_the_optional_steps_do_not_block_a_run(self):
        for step in ("paths", "claude", "forge"):
            config.mark_step(step, None)
        code, out, err = self.init("DEMO-806", "--harness", "example-maven")
        self.assertEqual(code, 0, err)

    def test_init_without_a_harness_refuses_and_names_the_active_one(self):
        # rule: R-INIT-SETUP-COMPLETE
        code, out, err = self.init("DEMO-807")
        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertEqual(error["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("pass --harness example-maven", error["message"])
        self.assertFalse(paths.run_dir("DEMO-807").exists())

    def test_init_without_a_harness_and_none_applied_says_how_to_apply_one(self):
        config.set_active_harness("")
        code, out, err = self.init("DEMO-810")
        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertEqual(error["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("no harness is applied yet", error["message"])
        self.assertIn("`ppl setup harness` and `ppl setup apply`", error["message"])

    def test_a_skipped_models_step_points_at_the_skip_flag(self):
        config.mark_step("models", "skipped")
        code, out, err = self.init("DEMO-811", "--harness", "example-maven")
        self.assertEqual(code, 2)
        message = json.loads(err)["message"]
        self.assertIn("`ppl setup models`", message)
        self.assertIn("--skip-model-check", message)
        code, out, err = self.init("DEMO-812", "--harness", "example-maven", "--skip-model-check")
        self.assertEqual(code, 0, err)

    def test_init_does_not_run_the_forge_check(self):
        fixtures.install_harness("example-generic")
        config.set_active_harness("example-generic")
        with mock.patch("ppl.forges.github.doctor_check",
                        return_value={"ok": True, "detail": ""}) as forge_check:
            code, out, err = self.init("DEMO-813", "--harness", "example-generic")
        self.assertEqual(code, 0, err)
        forge_check.assert_not_called()
        state = json.loads((paths.run_dir("DEMO-813") / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["harness"]["name"], "example-generic")

    def test_an_unreadable_config_is_a_refusal_not_a_crash(self):
        paths.config_file().write_text("[setup\n", encoding="utf-8")
        code, out, err = self.init("DEMO-808", "--harness", "example-maven")
        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertEqual(error["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("config.toml", error["message"])
        self.assertFalse(paths.run_dir("DEMO-808").exists())

    def test_the_setup_check_comes_before_the_model_gate(self):
        config.mark_step("harness", None)
        paths.models_file().unlink(missing_ok=True)
        self.addCleanup(mark_all_verified)
        code, out, err = self.init("DEMO-809", "--harness", "example-maven")
        self.assertEqual(json.loads(err)["error_code"], "SETUP_INCOMPLETE")


class TestSetupInTheBrowser(unittest.TestCase):
    """A first `ppl setup` can run in the browser: --web, or the offer a terminal
    gets when no step is done yet. serve is patched, a real one never returns."""

    TERMINAL_ONLY = {
        "step names": {"step": ["models"]},
        "--answer": {"answer": ["workspace=x"]},
        "--non-interactive": {"non_interactive": True},
        "--tier": {"tier": ["fast=x"]},
        "--skip": {"skip": ["forge"]},
        "--no-verify": {"no_verify": True},
    }
    TERMINAL_ONLY_ARGV = {
        "step names": ["models"],
        "--answer": ["--answer", "workspace=x"],
        "--non-interactive": ["--non-interactive"],
        "--tier": ["--tier", "fast=x"],
        "--skip": ["--skip", "forge"],
        "--no-verify": ["--no-verify"],
    }

    def setUp(self):
        self._real_ppl_home = os.environ["PPL_HOME"]
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._real_ppl_home

    @staticmethod
    def setup_args(**given):
        args = dict(step=[], answer=None, non_interactive=False, tier=None, skip=None,
                    no_verify=False)
        args.update(given)
        return argparse.Namespace(**args)

    def test_setup_web_opens_the_studio_on_the_wizard(self):
        with mock.patch("ppl.panel.server.serve") as serve:
            self.assertEqual(cli.main(["setup", "--web", "--port", "8799"]), 0)
        serve.assert_called_once_with(8799, open_browser=True, fragment="#/setup")

    def test_setup_web_without_a_port_serves_the_default_port(self):
        with mock.patch("ppl.panel.server.serve") as serve:
            self.assertEqual(cli.main(["setup", "--web"]), 0)
        serve.assert_called_once_with(8787, open_browser=True, fragment="#/setup")

    def test_an_interactive_first_setup_offers_the_browser(self):
        with mock.patch("ppl.panel.server.serve") as serve, \
                mock.patch("ppl.setup.run", side_effect=AssertionError("terminal wizard ran")), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="y"):
            self.assertEqual(cli.main(["setup"]), 0)
        serve.assert_called_once_with(8787, open_browser=True, fragment="#/setup")

    def test_declining_the_browser_keeps_the_terminal_wizard(self):
        args = self.setup_args()
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="n"):
            self.assertFalse(cli._offer_browser(args))
        with mock.patch("sys.stdin.isatty", return_value=False):
            self.assertFalse(cli._offer_browser(args))
        named = self.setup_args(step=["models"])
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="y"):
            self.assertFalse(cli._offer_browser(named))

    def test_an_option_the_browser_wizard_ignores_keeps_the_terminal_wizard_without_asking(self):
        for name, given in self.TERMINAL_ONLY.items():
            with self.subTest(option=name), \
                    mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("builtins.input", return_value="y") as asked:
                self.assertFalse(cli._offer_browser(self.setup_args(**given)))
                asked.assert_not_called()

    def test_setup_web_refuses_an_option_the_browser_wizard_ignores(self):
        for name, argv in self.TERMINAL_ONLY_ARGV.items():
            with self.subTest(option=name), mock.patch("ppl.panel.server.serve") as serve:
                code, _, err = call("setup", "--web", *argv)
                self.assertEqual(code, 2, err)
                error = json.loads(err)
                self.assertEqual(error["error_code"], "SCHEMA_INVALID")
                self.assertIn("takes only --port", error["message"])
                self.assertIn(name, error["message"])
                serve.assert_not_called()

    def test_port_without_web_is_refused(self):
        with mock.patch("ppl.panel.server.serve") as serve, \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=AssertionError("asked")):
            code, _, err = call("setup", "--port", "8799")
        self.assertEqual(code, 2, err)
        error = json.loads(err)
        self.assertEqual(error["error_code"], "SCHEMA_INVALID")
        self.assertIn("--port is for --web", error["message"])
        serve.assert_not_called()


if __name__ == "__main__":
    unittest.main()

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

HOME = tmphome.mkdtemp(prefix="pplhome-")
os.environ["PPL_HOME"] = HOME
SOURCE = Path(__file__).resolve().parents[1]
shutil.copy(SOURCE / "defaults" / "routing.example.toml", Path(HOME) / "routing.toml")

from ppl import cli, models, paths, routing
from ppl.errors import PplError


def fake_runner(results):
    """A probe stand-in: results maps model -> probe dict, anything else fails."""
    calls = []

    def runner(model, binary="claude", env=None):
        calls.append(model)
        return results.get(model, {"ok": False, "resolved_id": None, "cost_usd": 0.0,
                                   "detail": "unrecognized model"})
    runner.calls = calls
    return runner


def ok(resolved):
    return {"ok": True, "resolved_id": resolved, "cost_usd": 0.001, "detail": ""}


class ModelsTestCase(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = HOME
        if paths.models_file().exists():
            paths.models_file().unlink()
        self.use_claude_version("9.9.9")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def use_claude_version(self, version):
        patcher = mock.patch.object(models, "claude_version", lambda binary="claude": version)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestVerify(ModelsTestCase):
    def test_verify_probes_every_model_in_use_once_and_records_it(self):
        in_use = routing.models_in_use()
        runner = fake_runner({m: ok("id-" + m) for m in in_use})
        report = models.verify(runner=runner)
        self.assertEqual(sorted(runner.calls), sorted(in_use))
        stored = json.loads(paths.models_file().read_text(encoding="utf-8"))
        for m in in_use:
            self.assertEqual(stored[m]["resolved_id"], "id-" + m)
            self.assertEqual(stored[m]["claude_version"], "9.9.9")
            self.assertIn("verified_at", stored[m])
        self.assertEqual(report["failed"], [])

    def test_a_failed_probe_records_nothing_for_that_model(self):
        in_use = sorted(routing.models_in_use())
        results = {m: ok("id") for m in in_use[1:]}
        report = models.verify(runner=fake_runner(results))
        stored = json.loads(paths.models_file().read_text(encoding="utf-8"))
        self.assertNotIn(in_use[0], stored)
        self.assertEqual(report["failed"], [in_use[0]])

    def test_ok_without_a_resolved_id_counts_as_failed(self):
        # a probe that exits 0 but names no model is not proof the model answered
        in_use = sorted(routing.models_in_use())
        results = {m: ok("id") for m in in_use}
        results[in_use[0]] = {"ok": True, "resolved_id": None, "cost_usd": 0.0, "detail": ""}
        report = models.verify(runner=fake_runner(results))
        self.assertEqual(report["failed"], [in_use[0]])

    def test_verified_models_are_skipped_unless_forced(self):
        in_use = routing.models_in_use()
        models.verify(runner=fake_runner({m: ok("id") for m in in_use}))
        again = fake_runner({m: ok("id") for m in in_use})
        models.verify(runner=again)
        self.assertEqual(again.calls, [])
        forced = fake_runner({m: ok("id") for m in in_use})
        models.verify(force=True, runner=forced)
        self.assertEqual(sorted(forced.calls), sorted(in_use))

    def test_dry_run_prints_commands_and_writes_nothing(self):
        runner = fake_runner({})
        report = models.verify(dry_run=True, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertFalse(paths.models_file().exists())
        self.assertTrue(all("claude -p" in line for line in report["commands"]))

    def test_dry_run_lines_are_shell_safe_and_round_trip_to_the_probe_argv(self):
        report = models.verify(dry_run=True, runner=fake_runner({}))
        self.assertEqual(len(report["commands"]), len(routing.models_in_use()))
        for line in report["commands"]:
            self.assertIn("--tools ''", line)
            self.assertIn("--setting-sources ''", line)
            self.assertIn(shlex.split(line)[3], routing.models_in_use())
            self.assertEqual(shlex.split(line), models.probe_command(shlex.split(line)[3], "claude"))

    def test_only_the_named_models_are_probed(self):
        target = sorted(routing.models_in_use())[0]
        runner = fake_runner({target: ok("id")})
        models.verify(models_to_check=[target], runner=runner)
        self.assertEqual(runner.calls, [target])


class TestStatusAndRequire(ModelsTestCase):
    def test_unverified_until_verified(self):
        states = {row["state"] for row in models.status()}
        self.assertEqual(states, {"unverified"})
        with self.assertRaises(PplError) as caught:
            models.require_verified("normal")
        self.assertEqual(caught.exception.code, "MODELS_UNVERIFIED")
        self.assertIn("ppl models verify", caught.exception.message)

    def test_verified_then_required_passes(self):
        models.verify(runner=fake_runner({m: ok("id") for m in routing.models_in_use()}))
        self.assertEqual({row["state"] for row in models.status()}, {"verified"})
        models.require_verified("normal")

    def test_stale_by_ttl(self):
        models.verify(runner=fake_runner({m: ok("id") for m in routing.models_in_use()}))
        stored = json.loads(paths.models_file().read_text(encoding="utf-8"))
        old = (datetime.now(timezone.utc) - timedelta(days=models.DEFAULT_TTL_DAYS + 1))
        for entry in stored.values():
            entry["verified_at"] = old.strftime("%Y-%m-%dT%H:%M:%SZ")
        paths.models_file().write_text(json.dumps(stored), encoding="utf-8")
        self.assertEqual({row["state"] for row in models.status()}, {"stale"})
        with self.assertRaises(PplError) as caught:
            models.require_verified("normal")
        self.assertEqual(caught.exception.code, "MODELS_UNVERIFIED")

    def test_stale_by_binary_version(self):
        models.verify(runner=fake_runner({m: ok("id") for m in routing.models_in_use()}))
        self.use_claude_version("10.0.0")
        self.assertEqual({row["state"] for row in models.status()}, {"stale"})

    def test_status_with_all_tiers_lists_a_tier_no_role_routes_to(self):
        unrouted = {tier: entry["model"] for tier, entry in routing.tiers().items()
                    if entry["model"] not in routing.models_in_use()}
        self.assertTrue(unrouted, "the example routing routes every tier now")
        self.assertFalse(set(unrouted.values()) & {row["model"] for row in models.status()})
        rows = {row["tier"]: row for row in models.status(all_tiers=True)}
        self.assertEqual(set(rows), set(routing.tiers()))
        for tier, model in unrouted.items():
            self.assertEqual((rows[tier]["model"], rows[tier]["state"]), (model, "unverified"))

    def test_a_models_file_that_is_not_an_object_is_refused(self):
        paths.models_file().write_text("[]", encoding="utf-8")
        for call in (models._load, models.status, models.verify):
            with self.assertRaises(PplError) as caught:
                call()
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")


class TestRiskScopedVerification(ModelsTestCase):
    """The example routing sends every risk to the same models, so these tests
    give one role a by_risk tier no other role uses."""

    def setUp(self):
        super().setUp()
        original = paths.routing_file().read_text(encoding="utf-8")
        self.addCleanup(paths.routing_file().write_text, original, encoding="utf-8")
        edited = original.replace('by_risk = { high = "good" }', 'by_risk = { high = "heavy" }')
        self.assertNotEqual(edited, original, "the example routing no longer has the by_risk line")
        paths.routing_file().write_text(edited, encoding="utf-8")
        self.heavy = routing.tiers()["heavy"]["model"]
        self.assertNotIn(self.heavy, routing.models_in_use(risk="normal"))
        self.assertIn(self.heavy, routing.models_in_use(risk="high"))
        normal_only = sorted(routing.models_in_use(risk="normal"))
        models.verify(models_to_check=normal_only,
                      runner=fake_runner({m: ok("id") for m in normal_only}))

    def test_status_lists_the_heavy_model_only_when_the_risk_can_route_to_it(self):
        self.assertNotIn(self.heavy, {row["model"] for row in models.status("normal")})
        for risk in ("high", None):
            rows = {row["model"]: row for row in models.status(risk)}
            self.assertEqual(rows[self.heavy]["state"], "unverified")
            self.assertEqual(rows[self.heavy]["tier"], "heavy")

    def test_require_checks_only_the_runs_risk_tiers(self):
        models.require_verified("normal")
        with self.assertRaises(PplError) as caught:
            models.require_verified("high")
        self.assertEqual(caught.exception.code, "MODELS_UNVERIFIED")
        self.assertIn(self.heavy, caught.exception.message)
        models.verify(models_to_check=[self.heavy], runner=fake_runner({self.heavy: ok("id")}))
        models.require_verified("high")


class TestProbeParsing(unittest.TestCase):
    def test_parse_success_output(self):
        out = json.dumps({"result": "OK", "total_cost_usd": 0.0007,
                          "modelUsage": {"claude-haiku-4-5-20251001": {}}})
        parsed = models.parse_probe(0, out, "")
        self.assertTrue(parsed["ok"])
        self.assertEqual(parsed["resolved_id"], "claude-haiku-4-5-20251001")
        self.assertAlmostEqual(parsed["cost_usd"], 0.0007)

    def test_parse_unrecognized_model(self):
        err = '[claude-code:unrecognized_model] {"model":"nope","query_source":"sdk"}'
        parsed = models.parse_probe(1, "{}", err)
        self.assertFalse(parsed["ok"])
        self.assertIn("unrecognized_model", parsed["detail"])

    def test_parse_not_logged_in(self):
        out = json.dumps({"result": "Not logged in \u00b7 Please run /login", "modelUsage": {}})
        parsed = models.parse_probe(1, out, "")
        self.assertFalse(parsed["ok"])
        self.assertIn("Not logged in", parsed["detail"])

    def test_parse_garbage(self):
        parsed = models.parse_probe(0, "not json", "")
        self.assertFalse(parsed["ok"])

    def test_probe_command_shape(self):
        argv = models.probe_command("some-model", "claude")
        self.assertEqual(argv[:3], ["claude", "-p", "--model"])
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--tools", argv)
        self.assertNotIn("--bare", argv)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestProbeProcess(unittest.TestCase):
    PROBE_OUTPUT = json.dumps({"result": "OK", "total_cost_usd": 0.001,
                               "modelUsage": {"resolved-id": {}}})

    def test_the_probe_env_drops_the_session_markers_but_keeps_the_rest(self):
        marked = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "PPL_TEST_KEEP": "yes"}
        with mock.patch.dict(os.environ, marked), \
                mock.patch.object(subprocess, "run", return_value=completed(self.PROBE_OUTPUT)) as run:
            result = models.probe("some-model")
            self.assertEqual(os.environ["CLAUDECODE"], "1", "probe must not mutate os.environ")
        self.assertTrue(result["ok"])
        env = run.call_args.kwargs["env"]
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_ENTRYPOINT", env)
        self.assertEqual(env["PPL_TEST_KEEP"], "yes")

    def test_an_explicit_env_is_stripped_without_being_mutated(self):
        given = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "OTHER": "kept"}
        with mock.patch.object(subprocess, "run", return_value=completed(self.PROBE_OUTPUT)) as run:
            models.probe("some-model", env=given)
        self.assertEqual(run.call_args.kwargs["env"], {"OTHER": "kept"})
        self.assertEqual(given, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "OTHER": "kept"})

    def test_a_missing_binary_is_a_failed_probe_not_an_exception(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("claude")):
            result = models.probe("some-model", binary="claude")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["resolved_id"])
        self.assertIn("not found", result["detail"])

    def test_a_timeout_is_a_failed_probe_not_an_exception(self):
        timeout = subprocess.TimeoutExpired(cmd="claude", timeout=models.PROBE_TIMEOUT_SECONDS)
        with mock.patch.object(subprocess, "run", side_effect=timeout):
            result = models.probe("some-model")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["resolved_id"])
        self.assertIn("timed out", result["detail"])

    def test_a_nonzero_exit_is_a_failed_probe_with_the_stderr_detail(self):
        err = '[claude-code:unrecognized_model] {"model":"nope"}'
        with mock.patch.object(subprocess, "run", return_value=completed("", err, returncode=1)):
            result = models.probe("nope")
        self.assertFalse(result["ok"])
        self.assertIn("unrecognized_model", result["detail"])


class TestClaudeVersion(unittest.TestCase):
    def test_first_token_of_stdout(self):
        with mock.patch.object(subprocess, "run", return_value=completed("2.1.0 (Claude Code)\n")) as run:
            self.assertEqual(models.claude_version("claude"), "2.1.0")
        self.assertEqual(run.call_args.args[0], ["claude", "--version"])

    def test_stderr_is_read_when_stdout_is_empty(self):
        with mock.patch.object(subprocess, "run", return_value=completed("", "3.0.1 (Claude Code)\n")):
            self.assertEqual(models.claude_version(), "3.0.1")

    def test_unknown_for_a_missing_binary(self):
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("claude")):
            self.assertEqual(models.claude_version("claude"), "unknown")

    def test_unknown_for_a_timeout(self):
        timeout = subprocess.TimeoutExpired(cmd="claude", timeout=models.VERSION_TIMEOUT_SECONDS)
        with mock.patch.object(subprocess, "run", side_effect=timeout):
            self.assertEqual(models.claude_version(), "unknown")

    def test_unknown_for_empty_output(self):
        for out, err in (("", ""), ("  \n", "\n")):
            with mock.patch.object(subprocess, "run", return_value=completed(out, err)):
                self.assertEqual(models.claude_version(), "unknown")


class TestModelsCli(ModelsTestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_verify_with_a_failing_probe_reports_on_stdout_and_refuses_on_stderr(self):
        failing = {"ok": False, "resolved_id": None, "cost_usd": 0.0, "detail": "unrecognized model"}
        with mock.patch.object(models, "probe", return_value=failing):
            code, out, err = self.run_cli(["models", "verify"])
        in_use = sorted(routing.models_in_use())
        self.assertEqual(code, 2)
        report = json.loads(out)
        self.assertEqual(report["failed"], in_use)
        self.assertEqual(sorted(report["details"]), in_use)
        self.assertEqual(report["verified"], [])
        refusal = json.loads(err)
        self.assertEqual(refusal["error_code"], "MODELS_UNVERIFIED")
        self.assertIn("unrecognized model", refusal["message"])
        self.assertEqual(json.loads(paths.models_file().read_text(encoding="utf-8")), {})

    def test_verify_with_a_passing_probe_exits_zero(self):
        passing = ok("resolved")
        with mock.patch.object(models, "probe", return_value=passing):
            code, out, err = self.run_cli(["models", "verify"])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(json.loads(out)["failed"], [])

    def test_status_prints_a_table_then_the_json_rows(self):
        code, out, err = self.run_cli(["models", "status", "--risk", "normal"])
        self.assertEqual(code, 0)
        rows = json.loads(out.strip().splitlines()[-1])
        self.assertEqual({row["state"] for row in rows}, {"unverified"})
        self.assertEqual({row["model"] for row in rows}, routing.models_in_use(risk="normal"))
        self.assertEqual(len(out.strip().splitlines()), len(rows) + 1)


class TestPatchHygiene(unittest.TestCase):
    def test_a_models_test_restores_claude_version_when_it_ends(self):
        before = models.claude_version
        case = TestVerify("test_only_the_named_models_are_probed")
        result = unittest.TestResult()
        case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        self.assertIs(models.claude_version, before)


if __name__ == "__main__":
    unittest.main()

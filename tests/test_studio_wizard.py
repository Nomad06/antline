import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
os.environ["PPL_CLAUDE_HOME"] = tmphome.mkdtemp(prefix="pplclaudehome-")
import panel_client
from ppl import config, harness, paths, setup
from ppl.studio import wizard


def ok_probe(model, binary="claude", env=None):
    return {"ok": True, "resolved_id": "id-" + model, "cost_usd": 0.001, "detail": ""}


def bad_probe(model, binary="claude", env=None):
    return {"ok": False, "resolved_id": None, "cost_usd": 0.0, "detail": "nope"}


def ok_forge(*args, **kwargs):
    return {"ok": True, "detail": "ok"}


class WizardTestCase(panel_client.StudioTestCase):
    def setUp(self):
        super().setUp()
        # the wizard renders into the workspace it configures, as a user's run does
        os.environ.pop("PPL_AGENTS_DIR", None)
        os.environ.pop("PPL_SKILLS_DIR", None)
        self.workspace = Path(tmphome.mkdtemp(prefix="pplws-"))
        self.answers = {
            "claude_binary": "claude", "workspace": str(self.workspace), "hooks": "local",
            "verify": "yes", "harness_from": "example-generic", "harness_name": "mine",
            "repo": str(self.workspace / "repo"), "default_branch": "main",
            "task_url_template": "https://tracker.example/<TASK-ID>",
            "key_pattern": "^DEMO-[0-9]+$", "forge": "github",
        }
        for name, value in (("PROBE", ok_probe), ("FORGE_CHECK", ok_forge)):
            patcher = mock.patch.object(wizard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_step(self, step, answers=None):
        status, payload, _ = panel_client.call("POST", "/api/setup/" + step,
                                               body={"answers": answers or {}})
        self.assertEqual(status, 200, payload)
        return self.wait(payload)


class TestWizardApi(WizardTestCase):
    def test_state_lists_the_six_steps_and_names_the_next(self):
        status, state, _ = panel_client.call("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertEqual([s["name"] for s in state["steps"]], list(setup.STEPS))
        self.assertEqual({s["state"] for s in state["steps"]}, {"todo"})
        self.assertEqual(state["next"], "paths")
        self.assertEqual(state["data_root"], str(paths.home()))

    def test_a_step_runs_as_a_job_and_is_recorded_done(self):
        job = self.run_step("paths")
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["done"], ["paths"])
        self.assertIn("data root: %s" % paths.home(), job["log"])
        self.assertIn("paths", config.steps_state()["done"])

    def test_an_unknown_step_is_refused(self):
        status, payload, _ = panel_client.call("POST", "/api/setup/nope", body={"answers": {}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_an_unknown_answer_key_is_refused(self):
        status, payload, _ = panel_client.call("POST", "/api/setup/claude",
                                               body={"answers": {"nonsense": "1"}})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error_code"], "SCHEMA_INVALID")

    def test_skipping_a_step_records_it_and_moves_on(self):
        status, state, _ = panel_client.call("POST", "/api/setup/paths/skip", body={})
        self.assertEqual(status, 200)
        self.assertEqual(state["next"], "claude")
        self.assertIn("paths", config.steps_state()["skipped"])

    def test_skipping_a_done_step_keeps_it_done(self):
        self.run_step("paths")
        panel_client.call("POST", "/api/setup/paths/skip", body={})
        self.assertIn("paths", config.steps_state()["done"])

    def test_a_failed_step_reports_its_reason(self):
        self.run_step("paths")
        with mock.patch.object(wizard, "PROBE", bad_probe):
            job = self.run_step("models", {"verify": "yes"})
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["result"]["failed"], ["models"])
        self.assertIn("probe failed", job["result"]["reasons"]["models"])

    def test_a_step_that_raises_fails_the_job_with_the_envelope(self):
        job = self.run_step("claude", {"hooks": "local"})
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["error_code"], "SETUP_INCOMPLETE")

    def test_the_harness_route_refuses_a_name_setup_keeps_so_no_install_deletes_it_later(self):
        # rule: R-STUDIO-SAME-VALIDATION
        self.run_step("paths")
        source = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "x.replacing"
        shutil.copytree(paths.defaults_dir() / "harnesses" / "example-generic", source)
        answers = dict(self.answers, harness_from=str(source))
        del answers["harness_name"]
        for given in (answers, dict(answers, harness_name="X.Replaced")):
            with self.subTest(name=given.get("harness_name", "x.replacing")):
                job = self.run_step("harness", given)
                self.assertEqual(job["state"], "failed")
                self.assertEqual(job["error"]["error_code"], "SCHEMA_INVALID")
        self.assertFalse(paths.harness_dir("x.replacing").exists())
        job = self.run_step("harness", dict(answers, harness_from="example-generic", harness_name="x"))
        self.assertEqual(job["result"]["done"], ["harness"])
        self.assertEqual(harness.list_names(), ["x"])

    def test_the_harness_route_refuses_a_relative_source_before_any_copy(self):
        # rule: R-STUDIO-SAME-VALIDATION
        self.run_step("paths")
        elsewhere = self.go_elsewhere()
        shutil.copytree(paths.defaults_dir() / "harnesses" / "example-generic", elsewhere / "team")
        job = self.run_step("harness", dict(self.answers, harness_from="team"))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["error"]["error_code"], "SCHEMA_INVALID")
        self.assertIn("absolute", job["error"]["message"])
        self.assertFalse(paths.harnesses_dir().exists() and any(paths.harnesses_dir().iterdir()))

    def go_elsewhere(self):
        """Make the server process cwd an empty directory, as `ppl serve` started anywhere else has."""
        elsewhere = Path(tmphome.mkdtemp(prefix="pplcwd-"))
        previous = os.getcwd()
        os.chdir(elsewhere)
        self.addCleanup(os.chdir, previous)
        return elsewhere

    def test_an_answer_that_is_not_a_string_is_refused_and_starts_no_job(self):
        elsewhere = self.go_elsewhere()
        for answers in ({"workspace": None}, {"verify": False}, {"hooks": ["local"]}):
            with self.subTest(answers=answers):
                status, payload, _ = panel_client.call("POST", "/api/setup/claude",
                                                       body={"answers": answers})
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
                self.assertIn(next(iter(answers)), payload["message"])
                self.assertNotIn("job", payload)
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_answers_that_are_not_an_object_are_refused(self):
        for answers in ([], False, 0, "claude"):
            with self.subTest(answers=answers):
                status, payload, _ = panel_client.call("POST", "/api/setup/paths",
                                                       body={"answers": answers})
                self.assertEqual(status, 400, payload)
                self.assertEqual(payload["error_code"], "SCHEMA_INVALID")
                self.assertNotIn("job", payload)

    def test_the_doctor_route_returns_the_doctor_report(self):
        status, report, _ = panel_client.call("GET", "/api/doctor")
        self.assertEqual(status, 200)
        for key in ("data_root", "harness", "hook_runs", "registration", "setup"):
            self.assertIn(key, report)


    def test_the_doctor_route_reports_the_configured_workspace_not_the_server_cwd(self):
        job = self.run_step("claude", self.answers)
        self.assertEqual(job["result"]["done"], ["claude"])
        self.go_elsewhere()
        status, report, _ = panel_client.call("GET", "/api/doctor")
        self.assertEqual(status, 200)
        self.assertEqual(report["registration"], "local")
        self.assertEqual(report["local"]["path"],
                         str(self.workspace / ".claude" / "settings.local.json"))
        self.assertIs(report["hook_runs"], True)


class TestSameResultAsTheTerminal(WizardTestCase):
    def snapshot(self):
        agents = self.workspace / ".claude" / "agents"
        settings = (self.workspace / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        def unhome(text):
            for home in (os.path.realpath(paths.home()), str(paths.home())):
                text = text.replace(home, "<PPL_HOME>")
            return text
        settings = unhome(settings)
        files = {"settings": settings,
                 "config": paths.config_file().read_text(encoding="utf-8"),
                 "routing": paths.routing_file().read_text(encoding="utf-8"),
                 "harness": (paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8"),
                 "skill": unhome((self.workspace / ".claude" / "skills" / "pipeline" / "SKILL.md")
                                 .read_text(encoding="utf-8"))}
        for path in sorted(agents.glob("*.md")):
            files["agent:" + path.name] = unhome(path.read_text(encoding="utf-8"))
        return files

    def test_the_browser_wizard_writes_what_the_terminal_wizard_writes(self):
        for step in setup.STEPS:
            job = self.run_step(step, self.answers)
            self.assertEqual(job["result"]["done"], [step], job)
        browser = self.snapshot()
        # the terminal run has to write every file itself, none may be left over from the browser's
        shutil.rmtree(self.workspace / ".claude")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        result = setup.run(answers=self.answers, non_interactive=True, probe=ok_probe,
                           forge_check=ok_forge, emit=lambda line: None)
        self.assertEqual(result["done"], list(setup.STEPS))
        self.assertEqual(self.snapshot(), browser)


if __name__ == "__main__":
    unittest.main()

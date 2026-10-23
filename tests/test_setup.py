import io
import json
import os
import shlex
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
os.environ["PPL_CLAUDE_HOME"] = tmphome.mkdtemp(prefix="pplclaudehome-")

from ppl import cli, config, harness, models, os_sandbox, paths, setup
from ppl.errors import PplError

SOURCE = Path(__file__).resolve().parents[1] / "ppl" / "_assets"
_DIR_VARS = ("PPL_AGENTS_DIR", "PPL_SKILLS_DIR")


def ok_probe(model, binary="claude", env=None):
    return {"ok": True, "resolved_id": "id-" + model, "cost_usd": 0.001, "detail": ""}


def bad_probe(model, binary="claude", env=None):
    return {"ok": False, "resolved_id": None, "cost_usd": 0.0, "detail": "nope"}


def ok_forge(*args, **kwargs):
    return {"ok": True, "detail": "ok"}


class SetupTestCase(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
        # Another module sets these at import time, and the dirs are read
        # environment first, so the apply step would render into its dirs.
        self._dirs = {name: os.environ.pop(name, None) for name in _DIR_VARS}
        self.workspace = Path(tmphome.mkdtemp(prefix="pplws-"))
        self.lines = []
        patcher = mock.patch.object(models, "claude_version", lambda binary="claude": "9.9.9")
        patcher.start()
        self.addCleanup(patcher.stop)
        pins = {name: {"path": "/usr/bin/" + name, "realpath": "/usr/bin/" + name}
                for name in os_sandbox.PIN_NAMES}
        patcher = mock.patch.object(os_sandbox, "pins", lambda which=None: pins)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.answers = {
            "claude_binary": "claude", "workspace": str(self.workspace), "hooks": "local",
            "verify": "yes",
            "harness_from": "example-generic", "harness_name": "mine",
            "repo": str(self.workspace / "repo"), "default_branch": "main",
            "task_url_template": "https://tracker.example/<TASK-ID>",
            "key_pattern": "^DEMO-[0-9]+$", "forge": "github",
        }

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home
        for name, value in self._dirs.items():
            if value is not None:
                os.environ[name] = value

    def settings(self):
        path = self.workspace / ".claude" / "settings.local.json"
        return json.loads(path.read_text(encoding="utf-8"))


class TestNonInteractive(SetupTestCase):
    def test_full_run_writes_everything(self):
        result = setup.run(answers=self.answers, non_interactive=True, probe=ok_probe,
                           forge_check=ok_forge, emit=self.lines.append)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["done"], list(setup.STEPS))
        self.assertTrue(paths.config_file().is_file())
        self.assertTrue(paths.routing_file().is_file())
        self.assertTrue(paths.models_file().is_file())
        doc = config.load()
        self.assertEqual(doc["claude"]["workspace"], str(self.workspace))
        self.assertEqual(doc["harness"]["active"], "mine")
        self.assertNotIn("pending", doc["harness"])
        self.assertEqual(sorted(doc["setup"]["done"]), sorted(setup.STEPS))
        settings = self.settings()
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertTrue(any("ppl_guard.py" in c for c in commands))
        self.assertTrue(any("ppl_dispatch.py" in c for h in settings["hooks"]["SubagentStart"]
                            for c in [x["command"] for x in h["hooks"]]))
        self.assertTrue(any("ppl_usage.py" in c for h in settings["hooks"]["SubagentStop"]
                            for c in [x["command"] for x in h["hooks"]]))
        self.assertTrue((self.workspace / ".claude" / "skills" / "pipeline" / "SKILL.md").is_file())
        self.assertTrue((self.workspace / ".claude" / "agents" / "ppl-code-worker.md").is_file())
        from ppl import harness
        doc = harness.load("mine")
        self.assertEqual(doc["harness"]["name"], "mine")
        self.assertEqual(doc["task"]["key_pattern"], "^DEMO-[0-9]+$")
        self.assertEqual(doc["forge"]["kind"], "github")
        self.assertEqual(doc["repo"]["worktrees"], str(self.workspace / "repo") + "/.worktrees")

    def test_running_twice_is_idempotent(self):
        from ppl import harness
        kwargs = dict(answers=self.answers, non_interactive=True, probe=ok_probe,
                      forge_check=ok_forge, emit=self.lines.append)
        setup.run(**kwargs)
        harness_toml = paths.harness_dir("mine") / "harness.toml"
        written = harness_toml.read_text(encoding="utf-8")
        # The same answers again: the candidate equals the installed harness, so it is kept.
        second = setup.run(**kwargs)
        self.assertEqual(second["done"], list(setup.STEPS))
        self.assertEqual(second["failed"], [])
        self.assertEqual(harness_toml.read_text(encoding="utf-8"), written)
        self.assertEqual(harness.list_names(), ["mine"])
        self.assertIn("harness mine is already installed and identical, kept", self.lines)
        settings = self.settings()
        guard_entries = [e for e in settings["hooks"]["PreToolUse"]
                         if any("ppl_guard.py" in h["command"] for h in e["hooks"])]
        self.assertEqual(len(guard_entries), 1)
        dispatch_entries = [e for e in settings["hooks"]["SubagentStart"]
                            if any("ppl_dispatch.py" in h["command"] for h in e["hooks"])]
        self.assertEqual(len(dispatch_entries), 1)
        usage_entries = [e for e in settings["hooks"]["SubagentStop"]
                         if any("ppl_usage.py" in h["command"] for h in e["hooks"])]
        self.assertEqual(len(usage_entries), 1)
        self.assertEqual(len(config.load()["setup"]["done"]), len(setup.STEPS))
        self.assertEqual(config.load()["harness"]["active"], "mine")

    def test_a_hand_edited_harness_is_not_rebuilt_from_the_same_answers(self):
        kwargs = dict(answers=self.answers, non_interactive=True, probe=ok_probe,
                      forge_check=ok_forge, emit=self.lines.append)
        setup.run(**kwargs)
        harness_toml = paths.harness_dir("mine") / "harness.toml"
        with open(harness_toml, "a", encoding="utf-8") as handle:
            handle.write('\n[[gate]]\nname = "hand-added"\ncommand = "make extra"\n')
        edited = harness_toml.read_text(encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            setup.run(**kwargs)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("harness_from=mine", caught.exception.message)
        self.assertEqual(harness_toml.read_text(encoding="utf-8"), edited)
        self.assertFalse(paths.harness_dir("mine.replacing").exists())
        self.answers["harness_from"] = "mine"
        third = setup.run(**kwargs)
        self.assertEqual(third["done"], list(setup.STEPS))
        self.assertEqual(harness_toml.read_text(encoding="utf-8"), edited)

    def test_replace_harness_yes_replaces_a_differing_installed_harness(self):
        setup.run(answers=self.answers, non_interactive=True, probe=ok_probe,
                  forge_check=ok_forge, emit=self.lines.append)
        changed = dict(self.answers, harness_from="example-maven", replace_harness="yes")
        result = setup.run(steps=["harness"], answers=changed, non_interactive=True,
                           probe=ok_probe, forge_check=ok_forge, emit=self.lines.append)
        self.assertEqual(result["done"], ["harness"])
        doc = harness.load("mine")
        self.assertIn("mvn", [shape["argv0"] for shape in doc["guard"]["shape"]])

    def test_replace_harness_no_keeps_the_installed_harness(self):
        setup.run(answers=self.answers, non_interactive=True, probe=ok_probe,
                  forge_check=ok_forge, emit=self.lines.append)
        kept = dict(self.answers, harness_from="example-maven", replace_harness="no")
        setup.run(steps=["harness"], answers=kept, non_interactive=True,
                  probe=ok_probe, forge_check=ok_forge, emit=self.lines.append)
        doc = harness.load("mine")
        self.assertEqual([shape["argv0"] for shape in doc["guard"]["shape"]], ["make"])

    def test_a_hand_edited_fragment_also_counts_as_a_difference(self):
        kwargs = dict(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                      emit=self.lines.append)
        setup.run(**kwargs)
        gates = paths.harness_dir("mine") / "skill" / "gates.md"
        gates.write_text(gates.read_text(encoding="utf-8") + "\nOne more rule.\n", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            setup.run(**kwargs)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertTrue(gates.read_text(encoding="utf-8").endswith("One more rule.\n"))

    def test_a_changed_answer_onto_an_installed_harness_is_refused(self):
        kwargs = dict(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                      emit=self.lines.append)
        setup.run(**kwargs)
        self.answers["default_branch"] = "develop"
        with self.assertRaises(PplError) as caught:
            setup.run(**kwargs)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.answers["harness_from"] = "mine"
        setup.run(**kwargs)
        from ppl import harness
        self.assertEqual(harness.load("mine")["repo"]["default_branch"], "develop")

    def test_missing_answer_is_setup_incomplete(self):
        del self.answers["workspace"]
        with self.assertRaises(PplError) as caught:
            setup.run(answers=self.answers, non_interactive=True, probe=ok_probe,
                      emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("workspace", caught.exception.message)

    def test_missing_repo_answer_is_setup_incomplete(self):
        del self.answers["repo"]
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                      emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("repo", caught.exception.message)

    def test_skipping_a_step_is_recorded(self):
        result = setup.run(steps=["paths", "models"], answers={"verify": "no"},
                           non_interactive=True, probe=ok_probe, skip=["models"],
                           emit=self.lines.append)
        self.assertEqual(result["done"], ["paths"])
        self.assertEqual(result["skipped"], ["models"])
        self.assertEqual(config.steps_state()["skipped"], {"models"})

    def test_no_verify_leaves_models_unverified(self):
        self.answers["verify"] = "no"
        result = setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                           probe=ok_probe, emit=self.lines.append)
        self.assertFalse(paths.models_file().exists())
        self.assertNotIn("models", config.steps_state()["done"])
        self.assertIn("models", result["failed"])

    def test_failed_probe_keeps_models_step_undone(self):
        result = setup.run(steps=["paths", "models"], answers=self.answers,
                           non_interactive=True, probe=bad_probe, emit=self.lines.append)
        self.assertNotIn("models", result["done"])
        self.assertIn("models", result["failed"])
        self.assertNotIn("models", config.steps_state()["done"])

    def test_a_failed_rerun_takes_back_an_earlier_done(self):
        setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                  probe=ok_probe, emit=self.lines.append)
        self.assertIn("models", config.steps_state()["done"])
        setup.run(steps=["models"], answers=self.answers, non_interactive=True,
                  probe=bad_probe, emit=self.lines.append)
        self.assertEqual(config.steps_state(), {"done": {"paths"}, "skipped": set()})

    def test_skipping_a_done_step_leaves_it_done(self):
        setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                  probe=ok_probe, emit=self.lines.append)
        result = setup.run(steps=["models"], answers=self.answers, non_interactive=True,
                           probe=bad_probe, skip=["models"], emit=self.lines.append)
        self.assertEqual(result["skipped"], ["models"])
        self.assertEqual(config.steps_state(), {"done": {"models", "paths"}, "skipped": set()})

    def test_an_empty_answer_is_setup_incomplete(self):
        self.answers["workspace"] = " "
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["paths", "claude"], answers=self.answers, non_interactive=True,
                      emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("workspace", caught.exception.message)

    def test_tier_answers_override_the_example_models(self):
        self.answers["tier.heavy"] = "custom-heavy"
        setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                  probe=ok_probe, emit=self.lines.append)
        import tomllib
        routing = tomllib.loads(paths.routing_file().read_text(encoding="utf-8"))
        example_file = SOURCE / "defaults" / "routing.example.toml"
        example = tomllib.loads(example_file.read_text(encoding="utf-8"))
        self.assertEqual(routing["tiers"]["heavy"]["model"], "custom-heavy")
        self.assertEqual(routing["tiers"]["good"], example["tiers"]["good"])
        self.assertEqual(routing["roles"], example["roles"])

    def test_the_models_step_verifies_every_configured_tier(self):
        # Every model the step just configured is probed, not only the ones a
        # role routes to today: heavy and light back no role in the example.
        probed = []

        def counting_probe(model, binary="claude", env=None):
            probed.append(model)
            return ok_probe(model)

        self.answers.update({"tier.heavy": "m-heavy", "tier.good": "m-good",
                             "tier.average": "m-average", "tier.light": "m-light"})
        result = setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                           probe=counting_probe, emit=self.lines.append)
        self.assertIn("models", result["done"])
        self.assertEqual(sorted(probed), ["m-average", "m-good", "m-heavy", "m-light"])
        listed = " ".join(self.lines)
        for tier in ("heavy", "good", "average", "light"):
            self.assertIn("m-%s" % tier, listed)

    def test_a_failed_probe_on_a_tier_no_role_routes_to_fails_the_step(self):
        def probe(model, binary="claude", env=None):
            return bad_probe(model) if model == "m-light" else ok_probe(model)

        self.answers["tier.light"] = "m-light"
        result = setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                           probe=probe, emit=self.lines.append)
        self.assertIn("models", result["failed"])
        self.assertNotIn("models", config.steps_state()["done"])

    def test_untouched_tiers_keep_routing_toml_as_copied(self):
        setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                  probe=ok_probe, emit=self.lines.append)
        self.assertEqual(paths.routing_file().read_text(encoding="utf-8"),
                         (SOURCE / "defaults" / "routing.example.toml").read_text(encoding="utf-8"))

    def test_hooks_manual_prints_the_snippet_and_writes_nothing(self):
        self.answers["hooks"] = "manual"
        lines = []
        setup.run(steps=["paths", "claude"], answers=self.answers, non_interactive=True,
                  emit=lines.append)
        self.assertFalse((self.workspace / ".claude" / "settings.local.json").exists())
        self.assertTrue(any("ppl_guard.py" in line for line in lines))

    def test_unknown_hooks_answer_is_setup_incomplete(self):
        self.answers["hooks"] = "everywhere"
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["paths", "claude"], answers=self.answers, non_interactive=True,
                      emit=lambda line: None)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")

    def test_harness_warnings_are_printed(self):
        lines = []
        setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                  emit=lines.append)
        (paths.harness_dir("mine") / "skill" / "notes.md").write_text("x\n", encoding="utf-8")
        self.answers["harness_from"] = "mine"
        lines.clear()
        setup.run(steps=["harness"], answers=self.answers, non_interactive=True, emit=lines.append)
        self.assertTrue(any(line.startswith("warning:") and "skill/notes.md" in line
                            for line in lines), lines)

    def reserved_source(self):
        source = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "x.replacing"
        shutil.copytree(SOURCE / "defaults" / "harnesses" / "example-generic", source)
        return source

    def test_a_reserved_harness_name_given_or_defaulted_is_refused_before_any_copy(self):
        # rule: R-STUDIO-SAME-VALIDATION
        setup.run(steps=["paths"], answers=self.answers, non_interactive=True, emit=self.lines.append)
        defaulted = dict(self.answers, harness_from=str(self.reserved_source()))
        del defaulted["harness_name"]
        for answers in (defaulted, dict(self.answers, harness_name="Demo.Replaced"),
                        dict(self.answers, harness_name="mine.GIT")):
            with self.subTest(name=answers.get("harness_name", "x.replacing")):
                with mock.patch.object(harness, "_copy") as copied:
                    with self.assertRaises(PplError) as caught:
                        setup.run(steps=["harness"], answers=answers, non_interactive=True,
                                  emit=self.lines.append)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                copied.assert_not_called()
        self.assertFalse(paths.harnesses_dir().exists() and any(paths.harnesses_dir().iterdir()))

    def test_a_relative_harness_source_is_refused_before_any_copy(self):
        # rule: R-STUDIO-SAME-VALIDATION
        setup.run(steps=["paths"], answers=self.answers, non_interactive=True, emit=self.lines.append)
        for source in ("defaults/harnesses/example-maven", "./mine", "harnesses"):
            with self.subTest(source=source):
                with mock.patch.object(harness, "_copy") as copied:
                    with self.assertRaises(PplError) as caught:
                        setup.run(steps=["harness"], answers=dict(self.answers, harness_from=source),
                                  non_interactive=True, emit=self.lines.append)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("absolute", caught.exception.message)
                copied.assert_not_called()
        self.assertFalse(paths.harnesses_dir().exists() and any(paths.harnesses_dir().iterdir()))

    def test_a_home_relative_harness_source_is_taken(self):
        home = Path(tmphome.mkdtemp(prefix="pplfakehome-"))
        shutil.copytree(SOURCE / "defaults" / "harnesses" / "example-generic", home / "team")
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            result = setup.run(steps=["paths", "harness"],
                               answers=dict(self.answers, harness_from="~/team"),
                               non_interactive=True, emit=self.lines.append)
        self.assertEqual(result["done"], ["paths", "harness"])
        self.assertEqual(harness.list_names(), ["mine"])

    def test_a_path_source_onto_an_installed_name_is_refused_non_interactively(self):
        setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                  emit=self.lines.append)
        self.answers["harness_from"] = str(SOURCE / "defaults" / "harnesses" / "example-maven")
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["harness"], answers=self.answers, non_interactive=True,
                      emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("harness mine is already installed", caught.exception.message)

    def replace_interactively(self, source, reply):
        setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                  emit=self.lines.append)
        self.answers["harness_from"] = source
        asked = []

        def read(prompt):
            asked.append(prompt)
            return reply
        return asked, lambda: setup.run(steps=["harness"], answers=self.answers,
                                        non_interactive=False, read=read, emit=self.lines.append)

    def test_another_source_replaces_an_installed_harness_once_it_loads(self):
        from ppl import harness
        asked, rerun = self.replace_interactively("example-maven", "yes")
        rerun()
        self.assertEqual(len(asked), 1)
        self.assertIn("Replace installed harness mine? yes/no [no]", asked[0])
        doc = harness.load("mine")
        self.assertEqual(doc["env"], {"JAVA_HOME": "/opt/jdk"})
        # The forge answer applies to the new copy as well.
        self.assertEqual(doc["forge"], {"kind": "github"})
        self.assertEqual(doc["harness"]["name"], "mine")
        self.assertEqual(harness.list_names(), ["mine"])

    def test_declining_the_replacement_keeps_the_installed_harness(self):
        from ppl import harness
        asked, rerun = self.replace_interactively("example-maven", "")
        rerun()
        self.assertEqual(harness.load("mine")["forge"]["kind"], "github")

    def test_a_bad_source_leaves_the_installed_harness_as_it_was(self):
        from ppl import harness
        asked, rerun = self.replace_interactively(str(self.workspace / "nowhere"), "yes")
        before = (paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            rerun()
        self.assertEqual(caught.exception.code, "HARNESS_INVALID")
        self.assertEqual((paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8"),
                         before)
        self.assertEqual(harness.list_names(), ["mine"])
        self.assertFalse(paths.harness_dir("mine.replacing").exists())

    def test_a_changed_forge_is_written_to_the_harness(self):
        from ppl import harness
        self.answers.update(forge="gitlab", gitlab_mcp_server="MyGL")
        checked = []

        def forge_check(settings, claude_json, workspace):
            checked.append(settings)
            return {"ok": True, "detail": "ok"}
        result = setup.run(steps=["paths", "harness", "forge"], answers=self.answers,
                           non_interactive=True, forge_check=forge_check, emit=self.lines.append)
        self.assertEqual(result["done"], ["paths", "harness", "forge"])
        self.assertEqual(harness.load("mine")["forge"], {"kind": "gitlab", "mcp_server": "MyGL"})
        self.assertEqual(checked, [{"kind": "gitlab", "mcp_server": "MyGL"}])
        written = (paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8")
        second = setup.run(steps=["paths", "harness", "forge"], answers=self.answers,
                           non_interactive=True, forge_check=forge_check, emit=self.lines.append)
        self.assertEqual(second["done"], ["paths", "harness", "forge"])
        self.assertEqual((paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8"),
                         written)

    def test_a_failed_forge_check_keeps_the_step_undone(self):
        result = setup.run(steps=["paths", "harness", "forge"], answers=self.answers,
                           non_interactive=True, emit=self.lines.append,
                           forge_check=lambda *a, **k: {"ok": False, "detail": "gh missing"})
        self.assertEqual(result["failed"], ["forge"])
        self.assertNotIn("forge", config.steps_state()["done"])
        self.assertIn("github: gh missing", self.lines)

    def assert_identical_rerun(self):
        kwargs = dict(answers=self.answers, non_interactive=True, probe=ok_probe,
                      forge_check=ok_forge, emit=self.lines.append)
        first = setup.run(**kwargs)
        self.assertEqual(first["failed"], [])
        written = (paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8")
        second = setup.run(**kwargs)
        self.assertEqual(second["done"], list(setup.STEPS))
        self.assertEqual(second["failed"], [])
        self.assertEqual((paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8"),
                         written)
        self.assertIn("harness mine is already installed and identical, kept", self.lines)
        return written

    def test_an_identical_rerun_with_a_forge_other_than_the_example_is_kept(self):
        self.answers.update(harness_from="example-maven", forge="github")
        self.assertIn('[forge]\nkind = "github"\n\n', self.assert_identical_rerun())

    def test_an_identical_rerun_with_a_gitlab_server_answer_is_kept(self):
        self.answers.update(forge="gitlab", gitlab_mcp_server="MyGL")
        self.assertIn('mcp_server = "MyGL"', self.assert_identical_rerun())

    def test_an_installed_harness_that_does_not_load_is_refused_non_interactively(self):
        setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                  emit=self.lines.append)
        harness_toml = paths.harness_dir("mine") / "harness.toml"
        harness_toml.write_text("[harness\n", encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["harness"], answers=self.answers, non_interactive=True,
                      emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("installed harness mine is not valid (", caught.exception.message)
        self.assertIn("pass harness_from=mine after fixing it, or let the wizard replace it",
                      caught.exception.message)
        self.assertEqual(harness_toml.read_text(encoding="utf-8"), "[harness\n")

    def test_an_installed_harness_that_does_not_load_is_replaced_on_yes(self):
        from ppl import harness
        setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                  emit=self.lines.append)
        (paths.harness_dir("mine") / "harness.toml").write_text("[harness\n", encoding="utf-8")
        asked = []

        def read(prompt):
            asked.append(prompt)
            return "yes"
        setup.run(steps=["harness"], answers=self.answers, non_interactive=False, read=read,
                  emit=self.lines.append)
        self.assertEqual(len(asked), 1)
        self.assertIn("Replace installed harness mine?", asked[0])
        self.assertEqual(harness.load("mine")["task"]["key_pattern"], "^DEMO-[0-9]+$")
        self.assertEqual(harness.list_names(), ["mine"])

    def test_a_switch_to_github_drops_the_gitlab_server(self):
        from ppl import harness
        self.answers.update(harness_from="example-maven", forge="github")
        setup.run(steps=["paths", "harness", "forge"], answers=self.answers, non_interactive=True,
                  forge_check=ok_forge, emit=self.lines.append)
        self.assertEqual(harness.load("mine")["forge"], {"kind": "github"})

    def test_a_workspace_that_cannot_be_created_fails_the_step_not_the_wizard(self):
        blocker = self.workspace / "a-file"
        blocker.write_text("x", encoding="utf-8")
        self.answers["workspace"] = str(blocker / "ws")
        result = setup.run(steps=["paths", "claude", "models"], answers=self.answers,
                           non_interactive=True, probe=ok_probe, emit=self.lines.append)
        self.assertEqual(result["failed"], ["claude"])
        self.assertEqual(result["done"], ["paths", "models"])
        expected = "cannot read or write %s" % (blocker / "ws")
        self.assertTrue(any(line.startswith(expected) for line in self.lines), self.lines)

    def test_forge_and_apply_without_a_harness_fail_and_the_wizard_goes_on(self):
        result = setup.run(answers=self.answers, non_interactive=True, probe=ok_probe,
                           forge_check=ok_forge, skip=["models", "harness"], emit=self.lines.append)
        self.assertEqual(result["failed"], ["forge", "apply"])
        self.assertIn("no harness configured, run `ppl setup harness` first", self.lines)

    def test_a_bad_default_from_config_is_setup_incomplete(self):
        config.update("claude", {"hooks": "bogus"})
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["paths", "claude"], answers={"workspace": str(self.workspace)},
                      non_interactive=True, emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("default hooks='bogus'", caught.exception.message)

    def test_an_unknown_tier_answer_is_setup_incomplete(self):
        self.answers["tier.huge"] = "custom"
        with self.assertRaises(PplError) as caught:
            setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                      probe=ok_probe, emit=self.lines.append)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("unknown tier huge", caught.exception.message)
        self.assertIn("average, good, heavy, light", caught.exception.message)

    def test_a_declined_verify_on_a_rerun_keeps_an_earlier_done(self):
        setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                  probe=ok_probe, emit=self.lines.append)
        self.answers["verify"] = "no"
        result = setup.run(steps=["models"], answers=self.answers, non_interactive=True,
                           probe=bad_probe, emit=self.lines.append)
        self.assertEqual(result["skipped"], ["models"])
        self.assertIn("models", config.steps_state()["done"])

    def test_a_declined_verify_after_a_tier_change_is_not_done(self):
        setup.run(steps=["paths", "models"], answers=self.answers, non_interactive=True,
                  probe=ok_probe, emit=self.lines.append)
        # tier good backs the briefer, so its model is in use.
        self.answers.update({"verify": "no", "tier.good": "custom-good"})
        result = setup.run(steps=["models"], answers=self.answers, non_interactive=True,
                           probe=ok_probe, emit=self.lines.append)
        self.assertEqual(result["failed"], ["models"])
        self.assertNotIn("models", config.steps_state()["done"])

    def test_every_step_opens_with_a_line_saying_what_it_does(self):
        setup.run(steps=["paths"], non_interactive=True, emit=self.lines.append)
        self.assertEqual(self.lines[:2], ["== paths", setup.STEP_INTROS["paths"]])
        self.assertEqual(set(setup.STEP_INTROS), set(setup.STEPS))


class TestTrackerStep(SetupTestCase):
    def run_forge(self, tracker_check=None, **kwargs):
        checked = []

        def default_check(table):
            checked.append(table)
            return {"ok": True, "detail": "ok"}
        result = setup.run(steps=["paths", "harness", "forge"], answers=self.answers,
                           non_interactive=True, forge_check=ok_forge, emit=self.lines.append,
                           tracker_check=tracker_check or default_check, **kwargs)
        return result, checked

    def test_a_github_forge_offers_github_issues_and_writes_its_table(self):
        self.answers.update(tracker="github-issues", tracker_repo="o/r")
        result, checked = self.run_forge()
        self.assertEqual(result["done"], ["paths", "harness", "forge"])
        doc = harness.load("mine")
        self.assertEqual(doc["tracker"], {"kind": "github-issues", "repo": "o/r",
                                          "task_prefix": "GH"})
        self.assertEqual(doc["task"]["key_pattern"], "^GH-[0-9]+$")
        self.assertEqual(doc["task"]["url_template"],
                         "https://github.com/o/r/issues/<TASK-ID-number>")
        self.assertEqual(checked, [doc["tracker"]])
        self.assertIn("github-issues: ok", self.lines)

    def test_an_identical_rerun_with_a_changed_tracker_is_kept(self):
        self.answers.update(tracker="github-issues", tracker_repo="o/r")
        kwargs = dict(answers=self.answers, non_interactive=True, probe=ok_probe,
                      forge_check=ok_forge, emit=self.lines.append,
                      tracker_check=lambda table: {"ok": True, "detail": "ok"})
        self.assertEqual(setup.run(**kwargs)["failed"], [])
        written = (paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8")
        second = setup.run(**kwargs)
        self.assertEqual(second["done"], list(setup.STEPS))
        self.assertIn("harness mine is already installed and identical, kept", self.lines)
        self.assertEqual((paths.harness_dir("mine") / "harness.toml").read_text(encoding="utf-8"),
                         written)

    def test_a_non_interactive_run_without_a_tracker_answer_keeps_the_tracker(self):
        result, checked = self.run_forge()
        self.assertEqual(result["done"], ["paths", "harness", "forge"])
        doc = harness.load("mine")
        self.assertEqual(doc["tracker"]["kind"], "jira")
        self.assertEqual(doc["task"]["key_pattern"], "^DEMO-[0-9]+$")
        self.assertEqual(checked, [doc["tracker"]])
        self.assertIn("jira: ok", self.lines)

    def test_a_failing_tracker_check_fails_the_step(self):
        self.answers.update(tracker="github-issues", tracker_repo="o/r")
        result, _ = self.run_forge(
            tracker_check=lambda table: {"ok": False, "detail": "gh is not installed"})
        self.assertEqual(result["failed"], ["forge"])
        self.assertNotIn("forge", config.steps_state()["done"])
        self.assertIn("github-issues: gh is not installed", self.lines)

    def test_switching_back_to_jira_writes_the_jira_table_and_its_task_defaults(self):
        self.answers.update(tracker="github-issues", tracker_repo="o/r")
        self.run_forge()
        self.answers.update(tracker="jira", harness_from="mine")
        result, _ = self.run_forge()
        self.assertEqual(result["failed"], [])
        doc = harness.load("mine")
        self.assertEqual(doc["tracker"]["kind"], "jira")
        self.assertIn("mcp_prefixes", doc["tracker"])
        self.assertNotIn("repo", doc["tracker"])
        self.assertEqual(doc["task"]["url_template"],
                         "https://example.atlassian.net/browse/<TASK-ID>")

    def test_github_issues_without_a_repo_or_a_github_remote_is_setup_incomplete(self):
        self.answers.update(tracker="github-issues")
        with self.assertRaises(PplError) as caught:
            self.run_forge()
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("tracker_repo", caught.exception.message)

    def test_the_repo_defaults_to_the_origin_remote_of_the_checkout(self):
        import subprocess
        checkout = self.workspace / "repo"
        checkout.mkdir()
        for argv in (["init", "-q"], ["remote", "add", "origin", "git@github.com:acme/widgets.git"]):
            subprocess.run(["git", "-C", str(checkout)] + argv, check=True)
        self.answers.update(tracker="github-issues")
        result, _ = self.run_forge()
        self.assertEqual(result["failed"], [])
        self.assertEqual(harness.load("mine")["tracker"]["repo"], "acme/widgets")

    def test_a_malformed_repo_answer_is_refused_by_the_harness_check(self):
        self.answers.update(tracker="github-issues", tracker_repo="not a repo")
        with self.assertRaises(PplError) as caught:
            self.run_forge()
        self.assertIn("tracker.repo", caught.exception.message)

    def test_an_unknown_tracker_answer_is_setup_incomplete(self):
        self.answers.update(tracker="linear")
        with self.assertRaises(PplError) as caught:
            self.run_forge()
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")

    def test_an_interactive_github_forge_offers_github_issues_while_the_tracker_is_untouched(self):
        asked = []

        def read(prompt):
            asked.append(prompt)
            return "o/r" if "owner/name" in prompt else ""
        setup.run(steps=["paths", "harness"], answers=self.answers, non_interactive=True,
                  emit=self.lines.append)
        setup.run(steps=["forge"], answers={}, non_interactive=False, read=read,
                  forge_check=ok_forge, emit=self.lines.append,
                  tracker_check=lambda table: {"ok": True, "detail": "ok"})
        self.assertTrue(any("github-issues" in prompt for prompt in asked), asked)
        self.assertEqual(harness.load("mine")["tracker"]["kind"], "github-issues")


class TestLocalSettings(SetupTestCase):
    def test_own_entries_are_replaced_and_the_user_hooks_kept(self):
        path = self.workspace / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        user_hook = {"type": "command", "command": "echo mine"}
        path.write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls)"]},
            "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [user_hook]},
                {"matcher": ".*", "hooks": [
                    {"type": "command", "command": "python3 /old/hooks/ppl_guard.py"},
                    dict(user_hook, command="echo shared")]},
            ]}}), encoding="utf-8")
        setup.write_local_settings(self.workspace, SOURCE)
        setup.write_local_settings(self.workspace, SOURCE)
        settings = self.settings()
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls)"]})
        commands = [h["command"] for e in settings["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual(sum("ppl_guard.py" in c for c in commands), 1)
        self.assertIn("echo mine", commands)
        self.assertIn("echo shared", commands)
        self.assertNotIn("python3 /old/hooks/ppl_guard.py", commands)
        self.assertIn(str(SOURCE / "hooks" / "ppl_guard.py"), " ".join(commands))

    def test_hook_entries_pin_the_interpreter_that_ran_setup(self):
        # Bare python3 is whatever Claude Code's PATH finds, and an older one
        # disarms the cage. The interpreter running setup is proven new enough.
        interpreter = "/opt/my python/bin/python3.12"
        with mock.patch.object(sys, "executable", interpreter):
            entries = setup.hook_entries(SOURCE)
        hooks_dir = SOURCE / "hooks"
        self.assertEqual(entries["PreToolUse"]["hooks"][0]["command"],
                         "%s %s" % (shlex.quote(interpreter),
                                    shlex.quote(str(hooks_dir / "ppl_guard.py"))))
        self.assertEqual(entries["SubagentStart"]["hooks"][0]["command"],
                         "%s %s" % (shlex.quote(interpreter),
                                    shlex.quote(str(hooks_dir / "ppl_dispatch.py"))))
        self.assertEqual(entries["SubagentStop"]["hooks"][0]["command"],
                         "%s %s" % (shlex.quote(interpreter),
                                    shlex.quote(str(hooks_dir / "ppl_usage.py"))))
        self.assertEqual(sorted(entries), ["PreToolUse", "SubagentStart", "SubagentStop"])
        setup.write_local_settings(self.workspace, SOURCE)
        commands = [h["command"] for e in self.settings()["hooks"]["PreToolUse"]
                    for h in e["hooks"]]
        self.assertTrue(all(c.startswith(shlex.quote(sys.executable) + " ") for c in commands),
                        commands)

    def test_setup_registers_the_usage_hook_on_subagentstop(self):
        entry = setup.hook_entries(SOURCE)["SubagentStop"]
        self.assertEqual(entry["matcher"], "")
        self.assertEqual(len(entry["hooks"]), 1)
        self.assertTrue(entry["hooks"][0]["command"].endswith("ppl_usage.py"),
                        entry["hooks"][0]["command"])
        self.assertEqual(entry["hooks"][0]["type"], "command")
        setup.write_local_settings(self.workspace, SOURCE)
        setup.write_local_settings(self.workspace, SOURCE)
        commands = [h["command"] for e in self.settings()["hooks"]["SubagentStop"]
                    for h in e["hooks"]]
        self.assertEqual(sum("ppl_usage.py" in c for c in commands), 1, commands)

    def test_a_settings_file_of_the_wrong_shape_is_setup_incomplete(self):
        path = self.workspace / ".claude" / "settings.local.json"
        path.parent.mkdir(parents=True)
        for text in ("{not json", "[]", '{"hooks": []}', '{"hooks": {"PreToolUse": {}}}',
                     '{"hooks": {"PreToolUse": [5]}}'):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(PplError) as caught:
                setup.write_local_settings(self.workspace, SOURCE)
            self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE", text)
            self.assertEqual(path.read_text(encoding="utf-8"), text)


class TestOsSandboxSettings(SetupTestCase):
    SANDBOX = {"enabled": True, "network": {"deniedDomains": ["github.com"]}}
    PROFILE = {"sandbox": SANDBOX, "allow": ["Bash(/opt/x/gh pr view *)", "Bash(/opt/x/gh auth status)"]}

    def path(self):
        return self.workspace / ".claude" / "settings.local.json"

    def record(self, **extra):
        (paths.home() / os_sandbox.FACTS_FILE).write_text(json.dumps(extra), encoding="utf-8")

    def test_a_profile_is_written_with_the_allow_entries_and_the_hooks_kept(self):
        setup.write_local_settings(self.workspace, SOURCE)
        setup.write_os_sandbox_settings(self.workspace, self.PROFILE)
        doc = self.settings()
        self.assertEqual(doc["sandbox"], self.SANDBOX)
        self.assertEqual(doc["permissions"]["allow"], self.PROFILE["allow"])
        self.assertIn("PreToolUse", doc["hooks"])

    def test_a_foreign_sandbox_key_refuses_and_leaves_the_file(self):
        self.path().parent.mkdir(parents=True)
        text = json.dumps({"sandbox": {"enabled": False}, "model": "x"})
        self.path().write_text(text, encoding="utf-8")
        before = self.path().read_bytes()
        with self.assertRaises(PplError) as caught:
            setup.write_os_sandbox_settings(self.workspace, self.PROFILE)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("sandbox", caught.exception.message)
        self.assertEqual(self.path().read_bytes(), before)

    def test_a_hand_edited_ppl_block_is_foreign(self):
        self.record(written_sandbox=self.SANDBOX)
        self.path().parent.mkdir(parents=True)
        self.path().write_text(json.dumps({"sandbox": dict(self.SANDBOX, enabled=False)}),
                               encoding="utf-8")
        before = self.path().read_bytes()
        with self.assertRaises(PplError):
            setup.write_os_sandbox_settings(self.workspace, self.PROFILE)
        self.assertEqual(self.path().read_bytes(), before)

    def test_the_recorded_block_is_replaced(self):
        self.record(written_sandbox=self.SANDBOX)
        self.path().parent.mkdir(parents=True)
        self.path().write_text(json.dumps({"sandbox": self.SANDBOX}), encoding="utf-8")
        newer = {"sandbox": {"enabled": True, "x": 1}, "allow": []}
        setup.write_os_sandbox_settings(self.workspace, newer)
        self.assertEqual(self.settings()["sandbox"], newer["sandbox"])

    def test_invalid_json_refuses_and_leaves_the_file(self):
        self.path().parent.mkdir(parents=True)
        for text in ("{not json", "[]", '{"permissions": []}', '{"permissions": {"allow": 3}}'):
            self.path().write_text(text, encoding="utf-8")
            before = self.path().read_bytes()
            with self.assertRaises(PplError) as caught:
                setup.write_os_sandbox_settings(self.workspace, self.PROFILE)
            self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE", text)
            self.assertEqual(self.path().read_bytes(), before)

    def test_none_removes_the_block_and_the_recorded_entries_only(self):
        self.record(written_sandbox=self.SANDBOX, written_allow=self.PROFILE["allow"])
        self.path().parent.mkdir(parents=True)
        self.path().write_text(json.dumps({
            "sandbox": self.SANDBOX, "model": "x",
            "permissions": {"allow": ["Bash(ls *)"] + self.PROFILE["allow"], "deny": ["Bash(rm *)"]}}),
            encoding="utf-8")
        setup.write_os_sandbox_settings(self.workspace, None)
        doc = self.settings()
        self.assertNotIn("sandbox", doc)
        self.assertEqual(doc["permissions"], {"allow": ["Bash(ls *)"], "deny": ["Bash(rm *)"]})
        self.assertEqual(doc["model"], "x")

    def test_none_without_a_file_creates_nothing(self):
        setup.write_os_sandbox_settings(self.workspace, None)
        self.assertFalse(self.path().exists())

    def test_none_leaves_a_foreign_block_alone(self):
        self.path().parent.mkdir(parents=True)
        text = json.dumps({"sandbox": {"enabled": False}})
        self.path().write_text(text, encoding="utf-8")
        setup.write_os_sandbox_settings(self.workspace, None)
        self.assertEqual(self.path().read_text(encoding="utf-8"), text)


class TestInteractive(SetupTestCase):
    def test_enter_takes_defaults_s_skips_q_quits(self):
        # paths asks nothing. claude: binary Enter, workspace typed, hooks Enter. models: s. harness: q.
        script = iter(["", str(self.workspace), "", "s", "q"])
        result = setup.run(answers={}, non_interactive=False, read=lambda prompt: next(script),
                           emit=lambda line: None, probe=ok_probe)
        self.assertEqual(result["done"], ["paths", "claude"])
        self.assertEqual(result["skipped"], ["models"])
        self.assertTrue(result["quit"])
        state = config.steps_state()
        self.assertEqual(state["done"], {"paths", "claude"})
        self.assertEqual(state["skipped"], {"models"})
        self.assertEqual(config.load()["claude"]["binary"], "claude")
        self.assertEqual(config.load()["claude"]["hooks"], "local")

    def test_resume_starts_at_the_first_incomplete_step(self):
        config.ensure()
        config.mark_step("paths", "done")
        config.mark_step("claude", "done")
        asked = []

        def read(prompt):
            asked.append(prompt)
            return "q"
        setup.run(answers={}, non_interactive=False, read=read, emit=lambda line: None, probe=ok_probe)
        self.assertTrue(asked[0].lower().startswith("models"), asked[0])

    def test_the_harness_source_prompt_names_the_choices(self):
        config.ensure()
        for step in ("paths", "claude", "models"):
            config.mark_step(step, "done")
        asked = []

        def read(prompt):
            asked.append(prompt)
            return "q"
        setup.run(answers={}, non_interactive=False, read=read, emit=lambda line: None)
        self.assertTrue(asked[0].startswith("harness: Source (example-generic, example-maven, "
                                            "an absolute directory, or a git URL) [example-generic]"), asked[0])

    def test_the_hooks_prompt_carries_one_label(self):
        asked = []

        def read(prompt):
            asked.append(prompt)
            return str(self.workspace) if len(asked) == 2 else ""
        setup.run(steps=["paths", "claude"], answers={}, non_interactive=False, read=read,
                  emit=lambda line: None)
        self.assertTrue(asked[2].startswith("claude: Register the hooks how? local (write the "
                                            "workspace settings)"), asked[2])

    def test_end_of_input_quits(self):
        def read(prompt):
            raise EOFError()
        result = setup.run(answers={}, non_interactive=False, read=read, emit=lambda line: None)
        self.assertEqual(result["done"], ["paths"])
        self.assertTrue(result["quit"])

    def test_a_rerun_offers_the_configured_workspace(self):
        setup.run(steps=["paths", "claude"], answers=self.answers, non_interactive=True,
                  emit=lambda line: None)
        asked = []

        def read(prompt):
            asked.append(prompt)
            return ""
        setup.run(steps=["claude"], answers={}, non_interactive=False, read=read,
                  emit=lambda line: None)
        self.assertIn(str(self.workspace), asked[1])
        self.assertEqual(config.load()["claude"]["workspace"], str(self.workspace))


class TestConfigBookkeeping(SetupTestCase):
    def test_ensure_copies_the_examples_once(self):
        doc = config.ensure()
        self.assertEqual(doc["setup"], {"version": 1, "done": [], "skipped": []})
        self.assertTrue(paths.routing_file().is_file())
        config.update("claude", {"binary": "/opt/claude"})
        config.ensure()
        self.assertEqual(config.load()["claude"]["binary"], "/opt/claude")

    def test_mark_step_moves_a_step_between_done_and_skipped(self):
        config.mark_step("models", "skipped")
        config.mark_step("models", "done")
        config.mark_step("models", "done")
        self.assertEqual(config.steps_state(), {"done": {"models"}, "skipped": set()})
        self.assertEqual(config.load()["setup"]["done"], ["models"])

    def test_mark_step_with_no_state_clears_it(self):
        config.mark_step("models", "done")
        config.mark_step("paths", "skipped")
        config.mark_step("models", None)
        config.mark_step("paths", None)
        self.assertEqual(config.steps_state(), {"done": set(), "skipped": set()})

    def test_mark_step_refuses_an_unknown_state(self):
        with self.assertRaises(PplError) as caught:
            config.mark_step("models", "maybe")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_non_list_done_is_schema_invalid(self):
        paths.home().mkdir(parents=True, exist_ok=True)
        paths.config_file().write_text('[setup]\ndone = "paths"\n', encoding="utf-8")
        with self.assertRaises(PplError) as caught:
            config.steps_state()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_agents_dir_prefers_the_environment_then_config_then_the_default(self):
        self.assertEqual(config.agents_dir(), Path.home() / ".claude" / "agents")
        config.update("claude", {"agents_dir": "/from/config"})
        self.assertEqual(config.agents_dir(), Path("/from/config"))
        with mock.patch.dict(os.environ, {"PPL_AGENTS_DIR": "/from/env"}):
            self.assertEqual(config.agents_dir(), Path("/from/env"))
            from ppl import routing
            self.assertEqual(routing.agents_dir(), Path("/from/env"))


class TestCli(SetupTestCase):
    def call(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_an_unknown_step_is_schema_invalid(self):
        code, _, err = self.call("setup", "bogus", "--non-interactive")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_a_named_step_runs_alone(self):
        code, out, err = self.call("setup", "paths", "--non-interactive")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out.splitlines()[-1])["done"], ["paths"])

    def test_a_failed_step_prints_the_result_then_exits_setup_incomplete(self):
        with mock.patch.object(models, "probe", bad_probe):
            code, out, err = self.call("setup", "paths", "models", "--non-interactive",
                                       "--tier", "heavy=custom-heavy", "--answer", "verify=yes")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out.splitlines()[-1])["failed"], ["models"])
        self.assertEqual(json.loads(err)["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("custom-heavy", paths.routing_file().read_text(encoding="utf-8"))

    def test_no_verify_and_skip_flags(self):
        code, out, err = self.call("setup", "paths", "models", "claude", "--non-interactive",
                                   "--no-verify", "--skip", "claude")
        self.assertEqual(code, 2, err)
        result = json.loads(out.splitlines()[-1])
        self.assertEqual(result["skipped"], ["claude"])
        self.assertEqual(result["failed"], ["models"])
        self.assertFalse(paths.models_file().exists())

    def test_an_unwritable_workspace_prints_the_result_then_refuses(self):
        blocker = self.workspace / "a-file"
        blocker.write_text("x", encoding="utf-8")
        code, out, err = self.call("setup", "paths", "claude", "--non-interactive",
                                   "--answer", "workspace=%s" % (blocker / "ws"))
        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", err)
        refusal = json.loads(err)
        self.assertEqual(refusal["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("claude: cannot read or write %s" % (blocker / "ws"), refusal["message"])
        result = json.loads(out.splitlines()[-1])
        self.assertEqual(result["failed"], ["claude"])
        self.assertIn("cannot read or write %s" % (blocker / "ws"), result["reasons"]["claude"])
        self.assertIn("cannot read or write %s" % (blocker / "ws"), out)

    def test_skipping_the_harness_still_offers_forge_and_apply(self):
        code, out, err = self.call("setup", "--non-interactive", "--skip", "models", "--skip", "harness",
                                   "--answer", "workspace=%s" % self.workspace)
        self.assertEqual(code, 2)
        result = json.loads(out.splitlines()[-1])
        self.assertEqual(result["done"], ["paths", "claude"])
        self.assertEqual(result["skipped"], ["models", "harness"])
        self.assertEqual(result["failed"], ["forge", "apply"])
        refusal = json.loads(err)
        self.assertEqual(refusal["error_code"], "SETUP_INCOMPLETE")
        self.assertIn("forge: no harness configured", refusal["message"])
        self.assertIn("apply: no harness configured", refusal["message"])

    def test_a_second_run_with_identical_answers_exits_zero(self):
        argv = ["setup", "--non-interactive", "--answer", "workspace=%s" % self.workspace,
                "--answer", "repo=%s" % (self.workspace / "repo"), "--answer", "harness_name=mine"]
        with mock.patch.object(models, "probe", ok_probe), \
                mock.patch.object(setup, "_default_forge_check", ok_forge):
            first = self.call(*argv)
            second = self.call(*argv)
        self.assertEqual(first[0], 0, first[2])
        self.assertEqual(second[0], 0, second[2])
        self.assertEqual(json.loads(second[1].splitlines()[-1])["done"], list(setup.STEPS))
        commands = [h["command"] for e in self.settings()["hooks"]["PreToolUse"] for h in e["hooks"]]
        self.assertEqual(sum("ppl_guard.py" in c for c in commands), 1)

    def test_a_malformed_answer_is_schema_invalid(self):
        code, _, err = self.call("setup", "paths", "--non-interactive", "--answer", "novalue")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")

    def test_the_harness_step_refuses_a_reserved_name_or_a_relative_source(self):
        # rule: R-STUDIO-SAME-VALIDATION
        source = Path(tmphome.mkdtemp(prefix="pplharnesssrc-")) / "x.replacing"
        shutil.copytree(SOURCE / "defaults" / "harnesses" / "example-generic", source)
        base = ["setup", "paths", "harness", "--non-interactive",
                "--answer", "repo=%s" % (self.workspace / "repo")]
        for extra in (["--answer", "harness_from=%s" % source],
                      ["--answer", "harness_from=defaults/harnesses/example-generic"]):
            with self.subTest(extra=extra):
                code, _, err = self.call(*(base + extra))
                self.assertEqual(code, 2)
                self.assertNotIn("Traceback", err)
                self.assertEqual(json.loads(err)["error_code"], "SCHEMA_INVALID")
        self.assertEqual(harness.list_names(), [])


if __name__ == "__main__":
    unittest.main()

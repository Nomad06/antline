import json
import os
import shlex
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

import fixtures
from ppl import harness, lane_render, lanes, os_sandbox, paths, routing
from ppl.errors import PplError

TASK = "KEY-7"
SOURCE = {"atlassian": {"type": "http", "url": "https://a.example/mcp"},
          "GitLab-MCP": {"command": "npx", "args": ["gitlab"]},
          "serena": {"command": "uvx"}}


def tree(*roots):
    found = set()
    for root in roots:
        for base, dirs, files in os.walk(root):
            found.update(os.path.join(base, name) for name in dirs + files)
    return found


class LaneRenderCase(unittest.TestCase):
    def setUp(self):
        self.enterContext(fixtures.git_identity())
        self.home = Path(tmphome.mkdtemp(prefix="pplhome-"))
        self.fake_home = Path(tmphome.mkdtemp(prefix="pplfakehome-"))
        self.enterContext(mock.patch.dict(os.environ, {"PPL_HOME": str(self.home),
                                                       "HOME": str(self.fake_home)}))
        self.enterContext(mock.patch.object(os_sandbox, "pins", lambda which=None: fixtures.FAKE_PINS))
        fixtures.install_routing()

    def lane(self, name="example-maven", **tables):
        """Install the harness, bind a lane for it and return (doc, start)."""
        if tables:
            fixtures.install_variant(name, "lane-harness", **tables)
            name = "lane-harness"
        else:
            fixtures.install_harness(name)
        repo = Path(tmphome.mkdtemp(prefix="pplrepo-"))
        fixtures.bind_lane(TASK, repo, lane=2, harness=name)
        start = lanes.start_dir(TASK)
        return harness.load(start / "harness" / name), start

    def render(self, doc, start, source=SOURCE, routing_config=None, worktree=None):
        worktree = worktree or Path(tmphome.mkdtemp(prefix="pplworktree-"))
        return lane_render.render(doc, TASK, 2, start, worktree, routing_config
                                  or routing.load(), source)

    def sandboxed(self):
        return self.lane(**fixtures.GITHUB_ISSUES)


class TestSettings(LaneRenderCase):
    def test_every_hook_command_carries_the_task(self):
        doc, start = self.lane()
        settings = json.loads(self.render(doc, start)["settings"].read_text(encoding="utf-8"))
        self.assertEqual(set(settings["hooks"]), {"PreToolUse", "SubagentStart", "SubagentStop"})
        for event, entries in settings["hooks"].items():
            for entry in entries:
                for hook in entry["hooks"]:
                    self.assertEqual(shlex.split(hook["command"])[-2:], ["--task", TASK], event)

    def test_the_settings_hold_the_profile_for_the_start_directory(self):
        doc, start = self.sandboxed()
        result = self.render(doc, start)
        settings = json.loads(result["settings"].read_text(encoding="utf-8"))
        self.assertIn(os.path.realpath(start), settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertTrue(settings["permissions"]["allow"])
        facts = json.loads(result["facts"].read_text(encoding="utf-8"))
        self.assertEqual(facts["workspace"], os.path.realpath(start))
        self.assertEqual((facts["lane"], facts["task"]), (2, TASK))
        self.assertEqual(facts["written_sandbox"], settings["sandbox"])
        self.assertEqual(facts["written_allow"], settings["permissions"]["allow"])

    def test_the_profile_lets_bash_write_only_its_own_worktree_and_the_checkouts_git(self):
        # rule: R-LANE-CROSS-WRITE
        # R25: a lane's sandboxed Bash writes neither the project checkout's working tree nor
        # another lane's worktree, and git in its own worktree writes under the checkout's .git
        doc, start = self.sandboxed()
        worktree = Path(tmphome.mkdtemp(prefix="pplworktree-"))
        result = self.render(doc, start, worktree=worktree)
        settings = json.loads(result["settings"].read_text(encoding="utf-8"))
        allow = settings["sandbox"]["filesystem"]["allowWrite"]
        checkout = os_sandbox.checkout(doc)
        self.assertIn(os.path.realpath(worktree), allow)
        self.assertIn(os.path.join(checkout, ".git"), allow)
        self.assertNotIn(checkout, allow)
        self.assertNotIn(os.path.realpath(doc["repo"]["worktrees"]), allow)
        for name in ("runs", "labels", "scratch"):
            self.assertIn(os.path.realpath(paths.home() / name), allow)
        facts = json.loads(result["facts"].read_text(encoding="utf-8"))
        self.assertEqual(facts["write_scope"], allow)

    def test_the_profile_write_denies_the_checkouts_shared_git_parts(self):
        # rule: R-LANE-SHARED-GIT-DIR
        doc, start = self.sandboxed()
        result = self.render(doc, start)
        filesystem = json.loads(result["settings"].read_text(encoding="utf-8"))[
            "sandbox"]["filesystem"]
        facts = json.loads(result["facts"].read_text(encoding="utf-8"))
        git_dir = os.path.join(os_sandbox.checkout(doc), ".git")
        self.assertIn(git_dir, filesystem["allowWrite"])
        for name in ("config", "hooks", "info"):
            self.assertIn(os.path.join(git_dir, name), filesystem["denyWrite"])
            self.assertIn(os.path.join(git_dir, name), facts["deny_write"])
            self.assertNotIn(os.path.join(git_dir, name), filesystem["allowWrite"])

    def test_a_harness_without_the_os_sandbox_gets_no_profile_and_no_facts(self):
        doc, start = self.lane()
        result = self.render(doc, start)
        settings = json.loads(result["settings"].read_text(encoding="utf-8"))
        self.assertNotIn("sandbox", settings)
        self.assertNotIn("permissions", settings)
        self.assertIsNone(result["facts"])
        self.assertFalse((start / "os_sandbox.json").exists())


class TestAgents(LaneRenderCase):
    def test_agents_json_has_every_required_role_with_its_model(self):
        doc, start = self.lane()
        config = routing.load()
        result = self.render(doc, start, routing_config=config)
        agents = json.loads(result["agents_json"])
        self.assertEqual(set(agents), set(routing.REQUIRED_ROLES))
        for name, agent in agents.items():
            self.assertEqual(agent["model"], routing.role(name, config)["model"], name)
            self.assertEqual(set(agent) - {"effort"}, {"description", "prompt", "tools", "model",
                                                       "maxTurns", "permissionMode"}, name)
            self.assertIsInstance(agent["tools"], list)
            self.assertIsInstance(agent["maxTurns"], int)
            self.assertTrue(agent["prompt"].strip())
        self.assertEqual(json.loads((start / "agents.json").read_text(encoding="utf-8")), agents)

    def test_agents_over_the_limit_are_refused_naming_the_largest_role(self):
        doc, start = self.lane()
        with mock.patch.object(lane_render, "MAX_AGENTS_BYTES", 100):
            with self.assertRaises(PplError) as caught:
                self.render(doc, start)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertRegex(str(caught.exception), r"largest role is ppl-\w+(-\w+)* at \d+ bytes")
        self.assertFalse((start / "settings.json").exists())


class TestSkill(LaneRenderCase):
    def test_the_skill_is_a_plugin_named_antline_with_the_pipeline_skill(self):
        doc, start = self.lane()
        plugin = self.render(doc, start)["plugin"]
        manifest = json.loads((plugin / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "antline")
        skill = (plugin / "skills" / "pipeline" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("name: pipeline", skill)
        self.assertTrue((plugin / "skills" / "pipeline" / "references" / "gates.md").is_file())

    def test_the_lane_number_is_rendered_into_gate_commands_and_env(self):
        doc, start = self.lane(env={"PORT": "80<lane>"})
        plugin = self.render(doc, start)["plugin"]
        gates = (plugin / "skills" / "pipeline" / "references" / "gates.md").read_text(encoding="utf-8")
        self.assertIn("PORT=802", gates)
        self.assertNotIn("<lane>", gates)

    def test_without_a_lane_the_placeholder_stays_literal(self):
        fixtures.install_variant("example-maven", "plain", env={"PORT": "80<lane>"})
        gates = harness.render_skill(harness.load("plain"))["references/gates.md"]
        self.assertIn("80<lane>", gates)


class TestMcp(LaneRenderCase):
    def test_mcp_json_holds_only_the_servers_the_harness_names(self):
        doc, start = self.lane()
        path = self.render(doc, start)["mcp"]
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        self.assertEqual(servers, {"atlassian": SOURCE["atlassian"],
                                   "GitLab-MCP": SOURCE["GitLab-MCP"]})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_named_server_missing_from_the_operator_config_is_refused(self):
        doc, start = self.lane()
        for gone in ("GitLab-MCP", "atlassian"):
            source = {k: v for k, v in SOURCE.items() if k != gone}
            with self.assertRaises(PplError) as caught:
                self.render(doc, start, source=source)
            self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
            self.assertIn(gone, str(caught.exception))
            self.assertIn("claude mcp add --scope user", str(caught.exception))

    def test_the_source_is_read_from_the_operator_files(self):
        claude_json = self.fake_home / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": SOURCE}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"PPL_CLAUDE_HOME": str(self.fake_home / ".claude")}):
            self.assertEqual(lane_render.mcp_source(project_dir=str(self.fake_home)), SOURCE)


class TestMcpSourceIsOperatorOwned(LaneRenderCase):
    def setUp(self):
        super().setUp()
        self.project = Path(os.path.realpath(tmphome.mkdtemp(prefix="pplproject-")))
        self.enterContext(mock.patch.dict(os.environ,
                                          {"PPL_CLAUDE_HOME": str(self.fake_home / ".claude")}))

    def user(self, doc):
        (self.fake_home / ".claude.json").write_text(json.dumps(doc), encoding="utf-8")

    def project_file(self, doc):
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": doc}), encoding="utf-8")

    def test_a_project_mcp_json_is_not_copied_and_the_lane_is_refused(self):
        self.user({"mcpServers": {"atlassian": {"command": "a"}}})
        self.project_file({"GitLab-MCP": {"command": "evil"}})
        source = lane_render.mcp_source(project_dir=str(self.project))
        self.assertNotIn("GitLab-MCP", source)
        doc, start = self.lane()
        with self.assertRaises(PplError) as caught:
            self.render(doc, start, source=source)
        self.assertEqual(caught.exception.code, "SETUP_INCOMPLETE")
        self.assertIn("GitLab-MCP", str(caught.exception))
        self.assertIn("claude mcp add --scope user", str(caught.exception))

    def test_a_project_mcp_json_never_overrides_a_claude_json_entry(self):
        self.user({"mcpServers": {"atlassian": {"command": "mine"}}})
        self.project_file({"atlassian": {"command": "evil"}})
        source = lane_render.mcp_source(project_dir=str(self.project))
        self.assertEqual(source["atlassian"], {"command": "mine"})

    def test_the_project_entry_beats_the_top_level_entry(self):
        self.user({"mcpServers": {"atlassian": {"command": "top"}, "other": {"command": "o"}},
                   "projects": {str(self.project): {"mcpServers": {"atlassian": {"command": "local"}}}}})
        source = lane_render.mcp_source(project_dir=str(self.project))
        self.assertEqual(source, {"atlassian": {"command": "local"}, "other": {"command": "o"}})


class TestFiles(LaneRenderCase):
    def test_routing_is_copied_not_linked(self):
        doc, start = self.lane()
        path = self.render(doc, start)["routing"]
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.read_bytes(), paths.routing_file().read_bytes())
        paths.routing_file().write_text("changed = 1\n", encoding="utf-8")
        self.assertNotEqual(path.read_bytes(), paths.routing_file().read_bytes())

    def test_nothing_is_written_outside_the_start_directory(self):
        doc, start = self.sandboxed()
        roots = (self.home, self.fake_home)
        before = tree(*roots)
        self.render(doc, start)
        added = tree(*roots) - before
        self.assertTrue(added)
        for path in added:
            self.assertTrue(path == str(start) or path.startswith(str(start) + os.sep), path)


if __name__ == "__main__":
    unittest.main()

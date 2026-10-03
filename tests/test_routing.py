import io
import json
import os
import shutil
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

HOME = tmphome.mkdtemp(prefix="pplhome-")
AGENTS_DIR = tmphome.mkdtemp(prefix="pplagents-")
os.environ["PPL_HOME"] = HOME
os.environ["PPL_AGENTS_DIR"] = AGENTS_DIR

SOURCE = Path(__file__).resolve().parents[1]
shutil.copy(SOURCE / "defaults" / "routing.example.toml", Path(HOME) / "routing.toml")

import fixtures
fixtures.install_harness("example-maven")

from ppl import cli, harness, routing
from ppl.errors import PplError


class TestRouting(unittest.TestCase):
    # Under `unittest discover`, every test module is imported before any test
    # runs, so the last-imported module's PPL_HOME assignment wins for all of
    # them. Unlike the other test modules, this one needs a specific directory
    # pre-seeded with routing.toml, so ambient env drift breaks it. Pin the env
    # vars for the duration of each test and restore them after, so this module
    # is a good citizen under both `discover` and a single-module run.
    def setUp(self):
        self._saved_home = os.environ.get("PPL_HOME")
        self._saved_agents = os.environ.get("PPL_AGENTS_DIR")
        os.environ["PPL_HOME"] = HOME
        os.environ["PPL_AGENTS_DIR"] = AGENTS_DIR
        self.doc = harness.load("example-maven")

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = self._saved_home
        if self._saved_agents is None:
            os.environ.pop("PPL_AGENTS_DIR", None)
        else:
            os.environ["PPL_AGENTS_DIR"] = self._saved_agents

    def test_load_returns_all_six_roles(self):
        self.assertEqual(sorted(routing.load()["roles"]), [
            "ppl-brief-challenger", "ppl-briefer", "ppl-code-worker",
            "ppl-review-responder", "ppl-test-worker", "ppl-verifier"])

    def test_tiers_name_provider_and_model(self):
        tiers = routing.tiers()
        self.assertEqual(sorted(tiers), ["average", "good", "heavy", "light"])
        self.assertEqual(tiers["good"]["provider"], "claude-code-subagent")

    def test_role_resolves_its_tier_to_a_concrete_model(self):
        entry = routing.role("ppl-briefer")
        self.assertEqual(entry["tier"], "good")
        self.assertEqual(entry["model"], routing.tiers()["good"]["model"])
        self.assertEqual(entry["provider"], "claude-code-subagent")

    def test_resolve_applies_the_risk_override(self):
        # rule: R-ROUTING-BY-RISK
        entry = routing.resolve("ppl-code-worker", "high")
        self.assertEqual(entry["model"], routing.tiers()["good"]["model"])
        self.assertEqual(entry["tier"], "good")
        self.assertEqual(entry["risk"], "high")

    def test_resolve_keeps_the_default_for_other_tiers_and_roles(self):
        # rule: R-ROUTING-BY-RISK
        tiers = routing.tiers()
        average, good = tiers["average"]["model"], tiers["good"]["model"]
        self.assertEqual(routing.resolve("ppl-code-worker", "normal")["model"], average)
        self.assertEqual(routing.resolve("ppl-code-worker", None)["model"], average)
        self.assertEqual(routing.resolve("ppl-verifier", "high")["model"], good)
        self.assertEqual(routing.resolve("ppl-brief-challenger", "low")["model"], good)

    def test_a_role_naming_an_unknown_tier_is_refused(self):
        bad_home = tmphome.mkdtemp(prefix="pplhome-")
        text = (SOURCE / "defaults" / "routing.example.toml").read_text(encoding="utf-8")
        Path(bad_home, "routing.toml").write_text(
            text.replace('tier = "good"', 'tier = "colossal"', 1), encoding="utf-8")
        os.environ["PPL_HOME"] = bad_home
        with self.assertRaises(PplError) as caught:
            routing.role("ppl-briefer")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("colossal", caught.exception.message)

    def test_a_role_may_still_name_a_model_directly(self):
        home = tmphome.mkdtemp(prefix="pplhome-")
        text = (SOURCE / "defaults" / "routing.example.toml").read_text(encoding="utf-8")
        text = text.replace('[roles.ppl-verifier]\ntier = "good"',
                            '[roles.ppl-verifier]\nprovider = "claude-code-subagent"\n'
                            'model = "custom-id"')
        Path(home, "routing.toml").write_text(text, encoding="utf-8")
        os.environ["PPL_HOME"] = home
        self.assertEqual(routing.role("ppl-verifier")["model"], "custom-id")
        self.assertIn("custom-id", routing.models_in_use())

    def test_models_in_use_covers_by_risk_tiers(self):
        tiers = routing.tiers()
        self.assertEqual(routing.models_in_use(),
                         {tiers["good"]["model"], tiers["average"]["model"]})
        self.assertEqual(routing.models_in_use(risk="normal"),
                         {tiers["good"]["model"], tiers["average"]["model"]})

    def test_resolve_refuses_a_by_risk_tier_whose_provider_is_unknown(self):
        # rule: R-ROUTING-BY-RISK
        bad_home = tmphome.mkdtemp(prefix="pplhome-")
        text = (SOURCE / "defaults" / "routing.example.toml").read_text(encoding="utf-8")
        Path(bad_home, "routing.toml").write_text(
            text.replace('good    = { provider = "claude-code-subagent"',
                         'good    = { provider = "ghost-provider"', 1), encoding="utf-8")
        os.environ["PPL_HOME"] = bad_home
        with self.assertRaises(PplError) as caught:
            routing.resolve("ppl-code-worker", "high")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("ghost-provider", caught.exception.message)
        self.assertIn("'good'", caught.exception.message)

    def test_models_in_use_refuses_an_unknown_by_risk_tier(self):
        bad_home = tmphome.mkdtemp(prefix="pplhome-")
        text = (SOURCE / "defaults" / "routing.example.toml").read_text(encoding="utf-8")
        Path(bad_home, "routing.toml").write_text(
            text.replace('by_risk = { high = "good" }', 'by_risk = { high = "colossal" }', 1),
            encoding="utf-8")
        os.environ["PPL_HOME"] = bad_home
        with self.assertRaises(PplError) as caught:
            routing.models_in_use()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("colossal", caught.exception.message)
        with self.assertRaises(PplError) as at_dispatch:
            routing.resolve("ppl-code-worker", "high")
        self.assertEqual(at_dispatch.exception.code, "SCHEMA_INVALID")
        self.assertEqual(caught.exception.message, at_dispatch.exception.message)

    def use_example_with(self, old, new):
        """Point PPL_HOME at a scratch copy of the example with one line changed."""
        text = (SOURCE / "defaults" / "routing.example.toml").read_text(encoding="utf-8")
        self.assertIn(old, text)
        home = tmphome.mkdtemp(prefix="pplhome-")
        Path(home, "routing.toml").write_text(text.replace(old, new, 1), encoding="utf-8")
        os.environ["PPL_HOME"] = home

    def test_models_in_use_counts_a_by_risk_tier_only_for_its_risk(self):
        self.use_example_with('by_risk = { high = "good" }', 'by_risk = { high = "heavy" }')
        heavy = routing.tiers()["heavy"]["model"]
        self.assertIn(heavy, routing.models_in_use())
        self.assertIn(heavy, routing.models_in_use("high"))
        self.assertNotIn(heavy, routing.models_in_use("normal"))

    def test_a_non_table_by_risk_is_schema_invalid_for_resolve_and_models_in_use(self):
        self.use_example_with('by_risk = { high = "good" }', 'by_risk = "high"')
        for call in (lambda: routing.models_in_use(),
                     lambda: routing.resolve("ppl-code-worker", "high")):
            with self.assertRaises(PplError) as caught:
                call()
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
            self.assertIn("by_risk must be a table", caught.exception.message)

    def test_a_non_table_params_is_schema_invalid_when_the_agent_file_is_rendered(self):
        self.use_example_with('[roles.ppl-verifier]\ntier = "good"\nparams = { effort = "xhigh" }',
                              '[roles.ppl-verifier]\ntier = "good"\nparams = "fast"')
        with self.assertRaises(PplError) as caught:
            routing.render("ppl-verifier", self.doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("ppl-verifier", caught.exception.message)

    def test_a_non_string_tier_or_provider_name_is_schema_invalid(self):
        cases = {
            "role tier": ('[roles.ppl-briefer]\ntier = "good"',
                          '[roles.ppl-briefer]\ntier = ["good"]',
                          lambda: routing.role("ppl-briefer")),
            "role provider": ('[roles.ppl-verifier]\ntier = "good"',
                              '[roles.ppl-verifier]\nprovider = ["claude-code-subagent"]\n'
                              'model = "custom-id"',
                              lambda: routing.role("ppl-verifier")),
            "by_risk tier": ('by_risk = { high = "good" }', 'by_risk = { high = ["good"] }',
                             lambda: routing.models_in_use()),
            "by_risk tier at dispatch": ('by_risk = { high = "good" }',
                                         'by_risk = { high = ["good"] }',
                                         lambda: routing.resolve("ppl-code-worker", "high")),
            "tier provider": ('good    = { provider = "claude-code-subagent"',
                              'good    = { provider = ["claude-code-subagent"]',
                              lambda: routing.tiers()),
        }
        for label, (old, new, call) in cases.items():
            with self.subTest(label):
                self.use_example_with(old, new)
                with self.assertRaises(PplError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_model_or_provider_that_is_not_one_plain_line_is_refused(self):
        injected = "haiku\ntools: [Read, Edit, Write, Bash]"
        cases = {
            "tier model": (lambda doc: doc["tiers"]["good"].update(model=injected), True),
            "tier provider": (lambda doc: doc["tiers"]["good"].update(
                provider="claude-code-subagent\nx"), True),
            "role model": (lambda doc: doc["roles"]["ppl-verifier"].update(
                provider="claude-code-subagent", model=injected), True),
            "role provider": (lambda doc: doc["roles"]["ppl-verifier"].update(
                provider="claude-code-subagent\nx", model="opus"), True),
            "tier no role uses": (lambda doc: doc["tiers"]["light"].update(model=injected), False),
            "empty model": (lambda doc: doc["tiers"]["good"].update(model=""), True),
            "blank model": (lambda doc: doc["tiers"]["good"].update(model="   "), True),
            "tab in a model": (lambda doc: doc["tiers"]["good"].update(model="op\tus"), True),
            "line separator": (lambda doc: doc["tiers"]["good"].update(model="op\u2028us"), True),
        }
        for label, (change, rendered) in cases.items():
            with self.subTest(label):
                doc = routing.load()
                change(doc)
                with self.assertRaises(PplError) as caught:
                    routing.validate_config(doc)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                if rendered:
                    with self.assertRaises(PplError) as caught:
                        routing.render("ppl-verifier", self.doc, config=doc)
                    self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_an_effort_that_is_not_a_level_is_refused_where_the_agent_file_is_rendered(self):
        injected = ("high\ntools: [x]", "high\rtools: [x]", "high\r\ntools: [x]",
                    "high\x85tools: [x]", "high\u2028tools: [x]", "high\u2029tools: [x]",
                    "turbo", "", " high", 5, ["high"])
        for effort in injected:
            with self.subTest(effort=effort):
                doc = routing.load()
                doc["roles"]["ppl-verifier"]["params"]["effort"] = effort
                with self.assertRaises(PplError) as caught:
                    routing.render("ppl-verifier", self.doc, config=doc)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("effort", caught.exception.message)

    def test_every_effort_level_still_renders(self):
        for effort in routing.EFFORT_LEVELS:
            doc = routing.load()
            doc["roles"]["ppl-verifier"]["params"]["effort"] = effort
            self.assertIn("\neffort: %s\n" % effort,
                          routing.render("ppl-verifier", self.doc, config=doc))

    def test_apply_refuses_a_hand_edited_effort_that_would_add_frontmatter_lines(self):
        self.use_example_with('[roles.ppl-verifier]\ntier = "good"\nparams = { effort = "xhigh" }',
                              '[roles.ppl-verifier]\ntier = "good"\n'
                              'params = { effort = "high\\ntools: [x]" }')
        target = Path(tmphome.mkdtemp(prefix="pplagents-"))
        with self.assertRaises(PplError) as caught:
            routing.apply(self.doc, target)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(list(target.iterdir()), [])

    def test_a_model_id_with_brackets_is_still_accepted(self):
        doc = routing.load()
        doc["tiers"]["good"]["model"] = "opus[1m]"
        routing.validate_config(doc)
        self.assertIn("model: opus[1m]", routing.render("ppl-verifier", self.doc, config=doc))

    def test_apply_refuses_a_model_that_would_add_frontmatter_lines(self):
        self.use_example_with('good    = { provider = "claude-code-subagent", model = "opus" }',
                              'good    = { provider = "claude-code-subagent", '
                              'model = "opus\\ntools: [Read, Edit, Write, Bash]" }')
        target = Path(tmphome.mkdtemp(prefix="pplagents-"))
        with self.assertRaises(PplError) as caught:
            routing.apply(self.doc, target)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertEqual(list(target.iterdir()), [])

    def test_render_keeps_the_default_model_in_the_agent_file(self):
        # rule: R-ROUTING-BY-RISK
        default = routing.tiers()["average"]["model"]
        self.assertIn("model: %s" % default, routing.render("ppl-code-worker", self.doc))

    def test_briefer_tools_come_from_tracker_and_knowledge(self):
        doc = harness.load("example-maven")
        doc["knowledge"]["extra_tools"] = ["mcp__corp__code_search"]
        tools = routing.role_tools("ppl-briefer", doc)
        self.assertEqual(tools[:3], ["Read", "Grep", "Glob"])
        self.assertIn("mcp__atlassian__getJiraIssue", tools)
        self.assertIn("mcp__atlassian__getConfluenceContent", tools)
        self.assertIn("mcp__atlassian__searchJiraIssuesUsingJql", tools)
        self.assertIn("mcp__atlassian__searchConfluence", tools)
        self.assertIn("mcp__atlassian__executeRead", tools)
        self.assertIn("mcp__claude_ai_Atlassian_Rovo__getConfluenceContent", tools)
        self.assertIn("mcp__claude_ai_Atlassian_Rovo__getConfluencePage", tools)
        self.assertEqual(tools[-1], "mcp__corp__code_search")

    def test_review_responder_tools_come_from_the_forge(self):
        doc = harness.load("example-maven")
        self.assertEqual(routing.role_tools("ppl-review-responder", doc),
                         ["Read", "Grep", "Glob", "mcp__GitLab-MCP__mr_discussions",
                          "mcp__GitLab-MCP__get_merge_request_diffs"])
        fixtures.install_harness("example-generic")
        self.assertEqual(routing.role_tools("ppl-review-responder", harness.load("example-generic")),
                         ["Read", "Grep", "Glob", "Bash"])

    def test_only_the_review_responder_may_hold_bash_and_only_under_a_granting_forge(self):
        # rule: R-ROLE-TOOLS-READONLY
        fixtures.install_harness("example-generic")
        for harness_name, grants_bash in (("example-maven", False), ("example-generic", True)):
            doc = harness.load(harness_name)
            for name in ("ppl-briefer", "ppl-brief-challenger", "ppl-verifier",
                         "ppl-review-responder"):
                tools = routing.role_tools(name, doc)
                if name == "ppl-review-responder" and grants_bash:
                    self.assertIn("Bash", tools)
                else:
                    self.assertNotIn("Bash", tools, "%s under %s" % (name, harness_name))

    def test_only_the_briefer_and_the_review_responder_gain_tools(self):
        for name in ("ppl-brief-challenger", "ppl-code-worker", "ppl-test-worker", "ppl-verifier"):
            self.assertEqual(routing.role_tools(name, self.doc), routing.BASE_TOOLS[name]["tools"])

    def test_base_tools_name_no_mcp_server(self):
        for name, meta in routing.BASE_TOOLS.items():
            for tool in meta["tools"]:
                self.assertFalse(tool.startswith("mcp__"), "%s holds %s" % (name, tool))

    def test_render_appends_the_harness_overlay_under_project_notes(self):
        doc = harness.load("example-maven")
        text = routing.render("ppl-code-worker", doc)
        self.assertIn("## Project notes", text)
        self.assertIn("mvn -pl :<module> -am", text)
        self.assertNotIn("## Project notes", routing.render("ppl-verifier", doc))

    def test_render_lists_knowledge_skills_for_the_briefer(self):
        doc = harness.load("example-maven")
        doc["knowledge"]["skills"] = ["corp-kb-core"]
        text = routing.render("ppl-briefer", doc)
        self.assertIn("corp-kb-core", text)

    def test_render_asks_for_the_knowledge_skills_once(self):
        doc = harness.load("example-maven")
        doc["knowledge"]["skills"] = ["corp-kb-core", "corp-kb-data"]
        text = routing.render("ppl-briefer", doc)
        self.assertEqual(text.count("before reading the tree"), 1)
        self.assertIn("`corp-kb-core`, `corp-kb-data`", text)
        self.assertNotIn("## Project notes", routing.render("ppl-briefer", self.doc))

    def scratch_harness(self, **parts):
        target = fixtures.install_harness("example-maven", as_name="scratch-part")
        for role_name, content in parts.items():
            (target / "templates" / ("%s.part" % role_name.replace("_", "-"))).write_bytes(content)
        return harness.load("scratch-part")

    def test_a_part_file_that_is_not_utf8_is_refused(self):
        doc = self.scratch_harness(ppl_verifier=b"- note \xff\xfe\n")
        with self.assertRaises(PplError) as caught:
            routing.render("ppl-verifier", doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("is not UTF-8", caught.exception.message)
        self.assertIn("ppl-verifier.part", caught.exception.message)

    def test_a_whitespace_only_part_file_adds_no_project_notes(self):
        doc = self.scratch_harness(ppl_verifier=b"  \n\n\t\n")
        self.assertNotIn("## Project notes", routing.render("ppl-verifier", doc))

    def test_render_states_the_env_for_the_two_workers_only(self):
        for name in ("ppl-code-worker", "ppl-test-worker"):
            self.assertIn("`JAVA_HOME=/opt/jdk`", routing.render(name, self.doc))
        for name in ("ppl-briefer", "ppl-brief-challenger", "ppl-verifier", "ppl-review-responder"):
            self.assertNotIn("JAVA_HOME", routing.render(name, self.doc))

    def test_render_without_an_overlay_leaves_the_body_untouched(self):
        fixtures.install_harness("example-generic")
        generic = harness.load("example-generic")
        text = routing.render("ppl-review-responder", generic)
        self.assertNotIn("## Project notes", text)
        body = (SOURCE / "templates" / "ppl-review-responder.md.body").read_text(encoding="utf-8")
        self.assertTrue(text.endswith(body.rstrip() + "\n"))

    def test_templates_carry_no_repo_specific_lines(self):
        for name in routing.REQUIRED_ROLES:
            body = (SOURCE / "templates" / ("%s.md.body" % name)).read_text(encoding="utf-8").lower()
            for word in ("mvn", "maven", "java_home", "checkstyle", "jira", "gitlab"):
                self.assertNotIn(word, body, "%s names %s" % (name, word))

    def test_unknown_role_raises(self):
        with self.assertRaises(PplError):
            routing.role("ppl-nobody")

    def test_render_puts_model_and_effort_in_frontmatter(self):
        text = routing.render("ppl-verifier", self.doc)
        self.assertIn("model: %s" % routing.tiers()["good"]["model"], text)
        self.assertIn("effort: xhigh", text)

    def test_render_marks_the_file_as_generated(self):
        self.assertIn("generated by ppl harness apply", routing.render("ppl-briefer", self.doc))

    def test_render_starts_with_the_frontmatter_delimiter(self):
        # A frontmatter parser anchors "---" at byte 0. Anything ahead of it
        # (like the generated-file notice used to be) is invisible to the
        # parser, and every declared model/tools/effort/maxTurns is lost
        # with it - silently, since the string still "looks" fine.
        for name in routing.REQUIRED_ROLES:
            self.assertTrue(routing.render(name, self.doc).startswith("---"),
                            "%s frontmatter does not start at byte 0" % name)

    def test_read_only_roles_get_no_write_tools(self):
        # rule: R-ROLE-TOOLS-READONLY
        for name in ("ppl-briefer", "ppl-verifier", "ppl-review-responder"):
            text = routing.render(name, self.doc)
            self.assertNotIn("Write", text.split("---")[1])
            self.assertNotIn("Edit", text.split("---")[1])

    def test_verifier_gets_no_bash(self):
        # rule: R-ROLE-TOOLS-READONLY
        # Bash calls carry no path, so the per-role boundary.check_path cage
        # never runs for them - a verifier with Bash could edit the very
        # candidate it is supposed to only judge.
        text = routing.render("ppl-verifier", self.doc)
        self.assertNotIn("Bash", text.split("---")[1])

    def test_code_worker_gets_edit_tools(self):
        self.assertIn("Edit", routing.render("ppl-code-worker", self.doc).split("---")[1])

    def test_apply_writes_six_agent_files(self):
        written = routing.apply(self.doc)
        self.assertEqual(len(written), 6)
        for path in written:
            self.assertTrue(Path(path).exists())

    def test_apply_is_idempotent(self):
        first = [Path(path).read_text(encoding="utf-8") for path in routing.apply(self.doc)]
        second = [Path(path).read_text(encoding="utf-8") for path in routing.apply(self.doc)]
        self.assertEqual(first, second)

    def test_load_raises_on_malformed_toml(self):
        bad_home = tmphome.mkdtemp(prefix="pplhome-bad-toml-")
        Path(bad_home, "routing.toml").write_text("this is [ not valid toml", encoding="utf-8")
        os.environ["PPL_HOME"] = bad_home
        with self.assertRaises(PplError) as ctx:
            routing.load()
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_check_egress_raises_on_unknown_provider(self):
        config = routing.load()
        config["roles"]["ppl-code-worker"]["provider"] = "does-not-exist"
        with self.assertRaises(PplError) as ctx:
            routing.check_egress("ppl-code-worker", {"context_files": []}, config)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_dispatch_refuses_source_upload_when_the_provider_forbids_it(self):
        # rule: R-EGRESS-SOURCE-UPLOAD
        config = routing.load()
        config["providers"]["claude-code-subagent"]["allow_source_upload"] = False
        with self.assertRaises(PplError) as ctx:
            routing.check_egress("ppl-code-worker",
                                 {"context_files": [{"path": "a.java"}]}, config)
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_dispatch_refuses_source_upload_when_the_key_is_absent(self):
        # rule: R-EGRESS-SOURCE-UPLOAD
        # The default-deny case that matters: a hand-written provider entry
        # that simply never set allow_source_upload, not one that set it to
        # False explicitly.
        config = routing.load()
        del config["providers"]["claude-code-subagent"]["allow_source_upload"]
        with self.assertRaises(PplError) as ctx:
            routing.check_egress("ppl-code-worker",
                                 {"context_files": [{"path": "a.java"}]}, config)
        self.assertEqual(ctx.exception.code, "OWNER_DECISION")

    def test_dispatch_for_a_subagent_provider_tells_the_caller_to_use_the_agent_tool(self):
        result = routing.dispatch("ppl-verifier", {"context_files": []})
        self.assertEqual(result["status"], "use_subagent")
        self.assertEqual(result["agent"], "ppl-verifier")


class _PinnedEnv(unittest.TestCase):
    """A test class that composes role tools needs the module's PPL_HOME, whatever
    another module left in the environment under `unittest discover`."""

    def setUp(self):
        self._saved_home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = HOME
        self.doc = harness.load("example-maven")

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = self._saved_home


class TestNoRoleHoldsAJiraWriteTool(_PinnedEnv):
    """R1's whole argument rests on this: only the unroled main session can
    reach a tracker write tool, because no ppl-* role's tool list carries
    one (DESIGN 5.4). Pin it so a future routing.toml or harness edit that
    grants one breaks loudly here, not silently in production.
    """

    def test_no_role_tools_include_a_tracker_write_tool(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        sys.path.insert(0, str(SOURCE / "hooks"))
        import ppl_guard

        # A vacuous pass over an empty table would prove nothing.
        self.assertGreater(len(routing.REQUIRED_ROLES), 0)
        for name in routing.REQUIRED_ROLES:
            # The predicate, not the enumerated table: it covers every
            # Atlassian prefix the gate knows, numbered duplicates included.
            for tool in routing.role_tools(name, self.doc):
                self.assertFalse(ppl_guard.is_jira_write_tool(tool), tool)

    def test_a_doc_that_would_grant_a_tracker_write_tool_is_refused_at_render(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        doc = harness.load("example-maven")
        doc["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        with self.assertRaises(PplError) as caught:
            routing.role_tools("ppl-briefer", doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("mcp__atlassian__editJiraIssue", caught.exception.message)
        self.assertIn("ppl-briefer", caught.exception.message)
        with self.assertRaises(PplError):
            routing.render("ppl-briefer", doc)

    def test_a_hand_edited_read_tool_list_is_refused_at_render(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        doc = harness.load("example-maven")
        doc["tracker"]["read_tools"] = ["getJiraIssue", "transitionJiraIssue"]
        with self.assertRaises(PplError) as caught:
            routing.role_tools("ppl-briefer", doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("transitionJiraIssue", caught.exception.message)

    def test_a_tracker_write_tool_check_follows_the_harness_own_prefixes(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        doc = harness.load("example-maven")
        doc["tracker"]["mcp_prefixes"] = ["mcp__corp_jira__"]
        doc["tracker"]["write_operations"] = ["publishIssue"]
        doc["knowledge"]["extra_tools"] = ["mcp__corp_jira_2__publishIssue"]
        with self.assertRaises(PplError) as caught:
            routing.role_tools("ppl-briefer", doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_tool_entry_that_is_not_a_single_tool_name_is_refused_at_render(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        smuggled = "mcp__atlassian__editJiraIssue"
        for bad in ("mcp__corp__a, " + smuggled, " mcp__corp__a", "mcp__corp__a\nmodel: other",
                    "mcp__corp__a]", ""):
            doc = harness.load("example-maven")
            doc["knowledge"]["extra_tools"] = [bad]
            with self.assertRaises(PplError) as caught:
                routing.role_tools("ppl-briefer", doc)
            self.assertEqual(caught.exception.code, "SCHEMA_INVALID", repr(bad))
            self.assertIn("not a single tool name", caught.exception.message, repr(bad))
            with self.assertRaises(PplError):
                routing.render("ppl-briefer", doc)
        doc = harness.load("example-maven")
        doc["tracker"]["read_tools"] = ["getJiraIssue, " + smuggled]
        with self.assertRaises(PplError) as caught:
            routing.role_tools("ppl-briefer", doc)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
        self.assertIn("not a single tool name", caught.exception.message)

    def test_every_tool_source_is_deduplicated_in_first_seen_order(self):
        doc = harness.load("example-maven")
        doc["tracker"]["read_tools"] = ["getJiraIssue", "getJiraIssue", "getConfluencePage"]
        doc["knowledge"]["extra_tools"] = ["mcp__atlassian__getJiraIssue", "mcp__corp__a",
                                           "Read", "mcp__corp__a"]
        tools = routing.role_tools("ppl-briefer", doc)
        self.assertEqual(len(tools), len(set(tools)))
        self.assertEqual(tools, ["Read", "Grep", "Glob",
                                 "mcp__atlassian__getJiraIssue",
                                 "mcp__atlassian__getConfluencePage",
                                 "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
                                 "mcp__claude_ai_Atlassian_Rovo__getConfluencePage",
                                 "mcp__corp__a"])


class TestRoutingApplyCli(unittest.TestCase):
    """`ppl routing apply` renders for a harness: the one named by --harness, else
    the active one, else it refuses."""

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in ("PPL_HOME", "PPL_AGENTS_DIR")}
        self.home = tmphome.mkdtemp(prefix="pplhome-cli-")
        self.agents = tmphome.mkdtemp(prefix="pplagents-cli-")
        os.environ["PPL_HOME"] = self.home
        os.environ["PPL_AGENTS_DIR"] = self.agents
        shutil.copy(SOURCE / "defaults" / "routing.example.toml", Path(self.home) / "routing.toml")
        fixtures.install_harness("example-maven")

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_apply_with_a_harness_flag_writes_six_agent_files(self):
        code, out, err = self.call("routing", "apply", "--harness", "example-maven")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(out.strip().splitlines()), 6)
        self.assertEqual(len(list(Path(self.agents).glob("ppl-*.md"))), 6)
        worker = (Path(self.agents) / "ppl-code-worker.md").read_text(encoding="utf-8")
        self.assertIn("## Project notes", worker)

    def test_apply_without_a_harness_refuses(self):
        code, out, err = self.call("routing", "apply")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["error_code"], "HARNESS_NOT_APPLIED")
        self.assertIn("--harness", json.loads(err)["message"])
        self.assertEqual(list(Path(self.agents).glob("ppl-*.md")), [])

    def test_apply_of_a_refused_doc_writes_no_agent_file(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        doc = harness.load("example-maven")
        doc["knowledge"]["extra_tools"] = ["mcp__atlassian__editJiraIssue"]
        with self.assertRaises(PplError):
            routing.apply(doc)
        self.assertEqual(list(Path(self.agents).glob("ppl-*.md")), [])

    def test_apply_falls_back_to_the_active_harness(self):
        (Path(self.home) / "config.toml").write_text('[harness]\nactive = "example-maven"\n',
                                                     encoding="utf-8")
        code, out, err = self.call("routing", "apply")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(list(Path(self.agents).glob("ppl-*.md"))), 6)


if __name__ == "__main__":
    unittest.main()


class TestNoRoleCanDelegate(_PinnedEnv):
    """DESIGN 5.2 leans on the harness for "no recursive delegation". The half
    that is ours to keep is that no role's tool list invites it, which nothing
    pinned until the rule matrix asked which test proved it.
    """

    def test_no_role_holds_a_subagent_spawning_tool(self):
        # rule: R-ROLE-TOOLS-NO-AGENT
        self.assertGreater(len(routing.BASE_TOOLS), 0)
        for name in routing.REQUIRED_ROLES:
            for tool in routing.role_tools(name, self.doc):
                self.assertNotIn(tool, ("Agent", "Task", "SendMessage"),
                                 "%s must not be able to dispatch another agent: %s"
                                 % (name, tool))


class TestDispatchUnimplementedProviderKind(unittest.TestCase):
    """Error-vocabulary audit, cycle 2 wave E. `routing.dispatch` used to
    raise EXTERNAL_BLOCKED for a provider kind ppl has no adapter for -
    whose documented remedy ("park, retry on the next tick") never becomes
    true by waiting, since the missing adapter is a permanent property of
    this ppl build, not an outside outage. Fixed to SCHEMA_INVALID, matching
    every other "routing.toml does not match ppl's contract" raise already
    in this module (unknown role, unknown provider, missing keys).

    Uses its own scratch PPL_HOME with a synthetic routing.toml naming an
    unimplemented provider kind - never the tracked repo routing.toml, and
    never `ppl routing apply`.
    """

    def setUp(self):
        self.home = tmphome.mkdtemp(prefix="pplhome-unimplemented-")
        agents_dir = tmphome.mkdtemp(prefix="pplagents-unimplemented-")
        lines = ['[providers.some-http-api]', 'kind = "http"', ""]
        for name in routing.REQUIRED_ROLES:
            lines += ["[roles.%s]" % name, 'provider = "some-http-api"',
                     'model = "whatever"', ""]
        (Path(self.home) / "routing.toml").write_text("\n".join(lines), encoding="utf-8")
        self._saved_home = os.environ.get("PPL_HOME")
        self._saved_agents = os.environ.get("PPL_AGENTS_DIR")
        os.environ["PPL_HOME"] = self.home
        os.environ["PPL_AGENTS_DIR"] = agents_dir

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = self._saved_home
        if self._saved_agents is None:
            os.environ.pop("PPL_AGENTS_DIR", None)
        else:
            os.environ["PPL_AGENTS_DIR"] = self._saved_agents

    def test_unimplemented_provider_kind_is_schema_invalid_not_external_blocked(self):
        with self.assertRaises(PplError) as ctx:
            routing.dispatch("ppl-briefer", {"context_files": []})
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("no adapter implemented", ctx.exception.message)


class TestProviderAgnosticWorkflow(unittest.TestCase):
    """routing.toml is meant to be the single place a model is named. If a
    module, hook, template or tool names one, moving a role to another provider
    stops being a config change, which is the claim DESIGN 10.1 makes.
    """

    MODEL_NAMES = ("fable", "opus", "sonnet", "haiku", "gpt-", "gemini", "claude-3", "claude-4")

    def test_no_model_name_appears_outside_routing_toml(self):
        offenders = []
        for folder in ("ppl", "hooks", "templates", "tools"):
            for path in sorted((SOURCE / folder).rglob("*")):
                if not path.is_file() or path.suffix == ".pyc" or "__pycache__" in str(path):
                    continue
                lowered = path.read_text(encoding="utf-8", errors="replace").lower()
                for name in self.MODEL_NAMES:
                    if name in lowered:
                        offenders.append("%s names %r" % (path.relative_to(SOURCE), name))
        self.assertEqual(offenders, [], "\n".join(offenders))

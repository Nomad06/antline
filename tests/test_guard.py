import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))

import fixtures
from ppl import boundary, control, harness, run, store, paths
import ppl_guard
import ppl_python_floor


def payload(agent, tool, tool_input, cwd="/tmp/repo"):
    return {"hook_event_name": "PreToolUse", "agent_type": agent,
            "tool_name": tool, "tool_input": tool_input, "cwd": cwd}


RESPONDER_DIFF = "git -C /work/repo diff --no-ext-diff --no-textconv abc1234..def5678"


class TestGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run.init("DEMO-700", "u", "/tmp/repo")
        store.write_json(paths.run_dir("DEMO-700") / "boundary.json",
                         {"write": ["source/core/orders/**"], "forbid": []})
        fixtures.install_harness("example-maven")
        store.write_json(paths.run_dir("DEMO-700") / "harness.json",
                         harness.snapshot(harness.load("example-maven")))

    def deny_reason(self, result):
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_code_worker_may_edit_inside_boundary(self):
        self.assertIsNone(ppl_guard.decide(
            payload("ppl-code-worker", "Edit",
                    {"file_path": "/tmp/repo/source/core/orders/Foo.java"})))

    def test_code_worker_may_not_edit_a_test(self):
        # rule: R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Write",
            {"file_path": "/tmp/repo/source/core/orders/src/test/java/FooIT.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_may_not_edit_main_source(self):
        # rule: R-ROLE-TEST-WORKER-MAIN
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Edit",
            {"file_path": "/tmp/repo/source/core/orders/src/main/java/Foo.java"}))
        self.assertIsNotNone(result)

    def test_verifier_may_not_write_anything(self):
        # rule: R-ROLE-READONLY-NO-WRITE
        result = ppl_guard.decide(payload(
            "ppl-verifier", "Write", {"file_path": "/tmp/repo/notes.txt"}))
        self.assertIsNotNone(result)

    def test_brief_challenger_may_not_write_anything(self):
        # rule: R-ROLE-READONLY-NO-WRITE
        inside = ppl_guard.decide(payload(
            "ppl-brief-challenger", "Write", {"file_path": "/tmp/repo/notes.txt"}))
        self.assertIn("may not write any path", self.deny_reason(inside))
        outside = ppl_guard.decide(payload(
            "ppl-brief-challenger", "Write", {"file_path": "/tmp/somewhere-else/notes.txt"},
            cwd="/tmp/somewhere-else"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(outside))

    def test_git_add_all_is_denied_for_every_agent(self):
        # rule: R-GIT-ADD-ALL
        for agent in [None, "ppl-code-worker", "ppl-verifier"]:
            result = ppl_guard.decide(payload(agent, "Bash", {"command": "git add -A"}))
            self.assertIsNotNone(result, agent)
            self.assertIn("credential", self.deny_reason(result).lower())

    def test_bare_git_stash_is_denied(self):
        # rule: R-GIT-STASH
        self.assertIsNotNone(ppl_guard.decide(
            payload("ppl-code-worker", "Bash", {"command": "git stash"})))

    def test_named_git_stash_push_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(
            payload("ppl-code-worker", "Bash", {"command": "git stash push -u -m ppl-700"})))

    def test_force_push_is_denied(self):
        # rule: R-GIT-FORCE-PUSH
        self.assertIsNotNone(ppl_guard.decide(
            payload("ppl-code-worker", "Bash", {"command": "git push --force origin x"})))

    def test_resolving_a_thread_is_denied(self):
        # rule: R-MCP-RESOLVE-THREAD
        result = ppl_guard.decide(payload(
            None, "mcp__GitLab-MCP__resolve_merge_request_thread", {"thread_id": "1"}))
        self.assertIn("remark author", self.deny_reason(result))

    def test_merging_is_denied(self):
        # rule: R-MCP-MERGE-MR
        self.assertIsNotNone(ppl_guard.decide(
            payload(None, "mcp__GitLab-MCP__merge_merge_request", {})))

    def test_approving_its_own_mr_is_denied(self):
        # rule: R-MCP-APPROVE-MR
        # The third denied MCP tool had no test of its own until the rule
        # matrix asked which test proved it.
        result = ppl_guard.decide(payload(
            None, "mcp__GitLab-MCP__approve_merge_request", {"merge_request_iid": 1}))
        self.assertIn("never approves", self.deny_reason(result))

    def test_writing_state_json_directly_is_denied(self):
        # rule: R-RUNS-WRITE-PROTECTION
        target = str(paths.run_dir("DEMO-700") / "state.json")
        self.assertIsNotNone(ppl_guard.decide(
            payload("ppl-code-worker", "Write", {"file_path": target})))

    def test_writing_control_json_is_allowed(self):
        target = str(paths.run_dir("DEMO-700") / "control.json")
        self.assertIsNone(ppl_guard.decide(
            payload(None, "Write", {"file_path": target})))

    def test_pause_denies_every_tool_call(self):
        # rule: R-OPERATOR-HALT-HOOK
        control.set_command("DEMO-700", "pause")
        try:
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": "ls"}))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            control.clear("DEMO-700")

    def test_unknown_agent_outside_the_pipeline_is_not_boundary_checked(self):
        # The load-bearing property: an ordinary session, in an unrelated
        # directory, writing an unrelated path is untouched by an active run.
        self.assertIsNone(ppl_guard.decide(
            payload(None, "Edit", {"file_path": "/tmp/somewhere-else/anywhere.java"},
                    cwd="/tmp/somewhere-else")))

    # --- C3: the orchestrator has a write boundary too ---

    def test_absent_agent_type_writing_inside_the_run_repo_is_denied(self):
        # rule: R-UNROLED-REPO-WRITE
        # agent_type is absent for the main session, so ROLE_RULES has nothing
        # to say about it. The discriminator is the target path: inside the
        # active run's repo it is repo code, and DESIGN 5.3 denies the
        # orchestrator Edit/Write on repo code.
        result = ppl_guard.decide(payload(
            None, "Write", {"file_path": "/tmp/repo/DANGER.txt"},
            cwd="/home/dev/some/other/project"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_unrecognised_agent_writing_inside_the_run_repo_is_denied(self):
        # rule: R-UNROLED-REPO-WRITE
        result = ppl_guard.decide(payload(
            "SomeOtherProjectAgent", "Write", {"file_path": "/tmp/repo/DANGER.txt"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    # --- C2: cwd is not the scope ---

    def test_role_write_into_the_repo_is_denied_from_any_cwd(self):
        result = ppl_guard.decide(payload(
            "ppl-verifier", "Write", {"file_path": "/tmp/repo/notes.txt"},
            cwd="/home/dev/some/other/project"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_role_write_outside_the_repo_is_denied(self):
        # rule: R-ROLE-OUTSIDE-REPO
        # boundary.check_path returns early for a test-shaped path under the
        # test worker's rules, so a role writing a test-shaped path outside the
        # tree has to be refused before that check, not by it.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Write",
            {"file_path": "/tmp/somewhere-else/src/test/java/FooTest.java"},
            cwd="/tmp/somewhere-else"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_pause_denies_a_role_wherever_its_session_sits(self):
        # rule: R-OPERATOR-HALT-HOOK
        control.set_command("DEMO-700", "pause")
        try:
            result = ppl_guard.decide(
                payload("ppl-code-worker", "Bash", {"command": "ls"},
                        cwd="/home/dev/some/other/project"))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            control.clear("DEMO-700")

    def test_pause_denies_the_brief_challenger_wherever_its_session_sits(self):
        # rule: R-OPERATOR-HALT-HOOK
        control.set_command("DEMO-700", "pause")
        try:
            result = ppl_guard.decide(
                payload("ppl-brief-challenger", "Read", {"file_path": "/tmp/repo/README.md"},
                        cwd="/home/dev/some/other/project"))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            control.clear("DEMO-700")

    def test_pause_does_not_touch_a_named_agent_from_another_project(self):
        # A recognisable, non-pipeline agent_type is never the orchestrator's own
        # session, isolated or not, so it stays untouched by DEMO-700's pause.
        control.set_command("DEMO-700", "pause")
        try:
            self.assertIsNone(ppl_guard.decide(
                payload("SomeOtherProjectAgent", "Edit",
                        {"file_path": "/tmp/somewhere-else/Foo.java"},
                        cwd="/tmp/somewhere-else")))
        finally:
            control.clear("DEMO-700")

    # --- R1: isolated scoping for the unroled session (DESIGN 5.4) ---
    #
    # DEMO-700 is created isolated=True by setUpClass (the default). Under
    # isolated=True the Jira gate and the pause fire for the unroled session
    # (agent_type absent) whenever this run is active, regardless of cwd - the
    # documented workflow creates the worktree beside the orchestrator's own
    # session, so cwd never lines up with the run's repo for that session in
    # the first place. This is a known, accepted residual (DESIGN 5.4): if the
    # operator's actual hook registration is broader than an isolated launch
    # directory despite the flag, both gates reach their other projects' own
    # unroled sessions too, for as long as this run stays active.

    def test_pause_denies_unroled_session_from_anywhere_when_isolated(self):
        control.set_command("DEMO-700", "pause")
        try:
            result = ppl_guard.decide(payload(
                None, "Bash", {"command": "ls"}, cwd="/home/dev/some/other/project"))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            control.clear("DEMO-700")

    def test_jira_gate_fires_for_unroled_session_outside_repo_when_isolated(self):
        # rule: R-JIRA-GATE
        result = ppl_guard.decide(payload(
            None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"},
            cwd="/home/dev/some/other/project"))
        self.assertIn("after the merge", self.deny_reason(result))

    def test_pause_does_not_reach_a_non_isolated_runs_unroled_session_elsewhere(self):
        run.init("DEMO-710", "u", "/tmp/repo710", isolated=False)
        try:
            control.set_command("DEMO-710", "pause")
            self.assertIsNone(ppl_guard.decide(
                payload(None, "Bash", {"command": "ls"},
                        cwd="/home/dev/some/other/project")))
        finally:
            control.clear("DEMO-710")
            run.set_active("DEMO-700")

    def test_jira_gate_does_not_reach_a_non_isolated_runs_unroled_session_elsewhere(self):
        run.init("DEMO-711", "u", "/tmp/repo711", isolated=False)
        try:
            result = ppl_guard.decide(payload(
                None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"},
                cwd="/home/dev/some/other/project"))
            self.assertIsNone(result)
        finally:
            run.set_active("DEMO-700")

    def test_unroled_ordinary_write_outside_repo_is_still_untouched_when_isolated(self):
        # The broadened scope must stay confined to the Jira gate and the pause
        # check: it must never start denying an unroled session's ordinary file
        # write outside any run's repo, isolated or not.
        self.assertIsNone(ppl_guard.decide(
            payload(None, "Edit", {"file_path": "/tmp/somewhere-else/anywhere.java"},
                    cwd="/tmp/somewhere-else")))

    def test_role_outside_any_run_repo_is_still_denied_regardless_of_isolation(self):
        # rule: R-ROLE-OUTSIDE-REPO
        # Broadening the unroled session's scope must not weaken the existing
        # property that a ppl-* role writing an unrelated path outside any run
        # repo is denied - that check never depended on cwd in the first place.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Write", {"file_path": "/tmp/somewhere-else/Foo.java"},
            cwd="/tmp/somewhere-else"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_landmine_denies_apply_even_outside_the_run_directory(self):
        # rule: R-GUARD-GLOBAL-DENIALS
        self.assertIsNotNone(ppl_guard.decide(
            payload(None, "Bash", {"command": "git add -A"},
                    cwd="/home/dev/some/other/project")))

    def test_jira_write_is_denied_before_merge_watch(self):
        # rule: R-JIRA-GATE
        result = ppl_guard.decide(payload(
            None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"}))
        self.assertIn("after the merge", self.deny_reason(result))

    @contextlib.contextmanager
    def at_merge_watch(self, merged_sha="f" * 40, mr_missing=False):
        """DEMO-700 in merge_watch, with the merge recorded unless merged_sha is None."""
        state = run.load("DEMO-700")
        saved = (state["stage"], state.get("mr"))
        state["stage"] = "merge_watch"
        if mr_missing:
            state.pop("mr", None)
        else:
            state["mr"] = dict(state.get("mr") or {}, merged_sha=merged_sha)
        run.save(state)
        try:
            yield
        finally:
            state = run.load("DEMO-700")
            state["stage"], state["mr"] = saved
            run.save(state)

    def test_jira_write_is_allowed_at_merge_watch(self):
        with self.at_merge_watch():
            self.assertIsNone(ppl_guard.decide(payload(
                None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"})))

    def test_tracker_write_in_merge_watch_before_the_merge_is_recorded_is_denied(self):
        # rule: R-JIRA-GATE
        # Entering merge_watch is not observing the merge: the tracker stays shut until
        # `ppl merge observed` recorded the merged sha.
        with self.at_merge_watch(merged_sha=None):
            for tool in ("mcp__atlassian__addCommentToJiraIssue",
                         "mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue",
                         "mcp__atlassian__executeWrite"):
                result = ppl_guard.decide(payload(None, tool, {"body": "x"}))
                self.assertIn("OWNER_DECISION", self.deny_reason(result), tool)
                self.assertIn("ppl merge observed", self.deny_reason(result), tool)

    def test_tracker_write_in_merge_watch_without_any_mr_record_is_denied(self):
        # rule: R-JIRA-GATE
        # a state written before the mr record had merged fields, or with none at all
        with self.at_merge_watch(mr_missing=True):
            result = ppl_guard.decide(payload(
                None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"}))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        with self.at_merge_watch(merged_sha=""):
            result = ppl_guard.decide(payload(
                None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"}))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))

    def test_tracker_write_is_allowed_after_ppl_merge_observed(self):
        # rule: R-JIRA-GATE
        from ppl import cli
        with self.at_merge_watch(merged_sha=None):
            with contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["merge", "observed", "--sha", "a" * 40,
                                 "--url", "https://forge/mr/7", "--task", "DEMO-700"])
            self.assertEqual(code, 0)
            self.assertIsNone(ppl_guard.decide(payload(
                None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"})))

    def test_jira_write_under_the_rovo_prefix_is_denied_before_merge_watch(self):
        # rule: R-JIRA-GATE
        # The same remote server registers as mcp__claude_ai_Atlassian_Rovo__ when
        # added through claude.ai - the prefix this machine actually has. A gate
        # that only knew mcp__atlassian__ was dead here.
        result = ppl_guard.decide(payload(
            None, "mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue", {"body": "x"}))
        self.assertIn("after the merge", self.deny_reason(result))

    def test_jira_write_under_a_numbered_rovo_prefix_is_denied_before_merge_watch(self):
        # rule: R-JIRA-GATE
        # Claude Code suffixes a duplicate registration _2, _3 and so on.
        result = ppl_guard.decide(payload(
            None, "mcp__claude_ai_Atlassian_Rovo_2__transitionJiraIssue", {"body": "x"}))
        self.assertIn("after the merge", self.deny_reason(result))

    def test_jira_write_under_the_rovo_prefix_is_allowed_at_merge_watch(self):
        # rule: R-JIRA-GATE
        with self.at_merge_watch():
            self.assertIsNone(ppl_guard.decide(payload(
                None, "mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue",
                {"body": "x"})))

    def test_jira_read_tools_are_not_gated_under_either_prefix(self):
        # rule: R-JIRA-GATE
        # The gate is on the tracker write operations, not on the server: reading
        # an issue or a page stays open before merge_watch under both prefixes.
        for tool in ("mcp__atlassian__getJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo__getConfluencePage"):
            self.assertFalse(ppl_guard.is_jira_write_tool(tool), tool)
            self.assertIsNone(
                ppl_guard.decide(payload(None, tool, {"issueKey": "X-1"})), tool)

    # The tool names the Atlassian server exposes today. The older names stay gated
    # too, the tests above and the snapshot tests below still use them.
    CURRENT_WRITE_TOOLS = (
        "mcp__atlassian__addOrEditJiraIssueComment",
        "mcp__atlassian__editJiraIssue",
        "mcp__atlassian__transitionJiraIssue",
        "mcp__claude_ai_Atlassian_Rovo__createJiraIssue",
        "mcp__atlassian__executeWrite",
        "mcp__atlassian__executeDestructive",
        "mcp__atlassian__createConfluenceContent",
        "mcp__claude_ai_Atlassian_Rovo__updateConfluenceContent",
        "mcp__atlassian_2__executeWrite",
    )
    CURRENT_READ_TOOLS = (
        "mcp__atlassian__getJiraIssue",
        "mcp__atlassian__getConfluenceContent",
        "mcp__atlassian__searchJiraIssuesUsingJql",
        "mcp__atlassian__searchConfluence",
        "mcp__atlassian__executeRead",
        "mcp__claude_ai_Atlassian_Rovo__getConfluenceContent",
        "mcp__claude_ai_Atlassian_Rovo__executeRead",
    )

    def test_the_current_atlassian_write_tools_are_denied_before_merge_watch(self):
        # rule: R-JIRA-GATE
        for tool in self.CURRENT_WRITE_TOOLS:
            self.assertTrue(ppl_guard.is_jira_write_tool(tool), tool)
            result = ppl_guard.decide(payload(None, tool, {"issueKey": "X-1"}))
            self.assertIn("after the merge", self.deny_reason(result), tool)

    def test_the_graph_context_write_is_denied_before_merge_watch(self):
        # rule: R-JIRA-GATE
        for tool in ("mcp__atlassian__addGraphContext",
                     "mcp__claude_ai_Atlassian_Rovo__addGraphContext",
                     "mcp__atlassian_2__addGraphContext"):
            self.assertTrue(ppl_guard.is_jira_write_tool(tool), tool)
            result = ppl_guard.decide(payload(None, tool, {"issueKey": "X-1"}))
            self.assertIn("after the merge", self.deny_reason(result), tool)

    def test_the_older_confluence_write_tools_are_denied_before_merge_watch(self):
        # rule: R-JIRA-GATE
        # the names earlier server builds used, still registered on some machines
        for operation in ("createConfluencePage", "updateConfluencePage",
                          "createConfluenceFooterComment", "createConfluenceInlineComment"):
            for prefix in ("mcp__atlassian__", "mcp__claude_ai_Atlassian_Rovo__",
                           "mcp__atlassian_2__"):
                tool = prefix + operation
                result = ppl_guard.decide(payload(None, tool, {"pageId": "1"}))
                self.assertIn("OWNER_DECISION", self.deny_reason(result), tool)
                self.assertIn("after the merge", self.deny_reason(result), tool)

    def test_the_current_atlassian_write_tools_are_allowed_at_merge_watch(self):
        # rule: R-JIRA-GATE
        with self.at_merge_watch():
            for tool in self.CURRENT_WRITE_TOOLS:
                self.assertIsNone(
                    ppl_guard.decide(payload(None, tool, {"issueKey": "X-1"})), tool)

    def test_the_current_atlassian_read_tools_are_not_gated(self):
        # rule: R-JIRA-GATE
        for tool in self.CURRENT_READ_TOOLS:
            self.assertFalse(ppl_guard.is_jira_write_tool(tool), tool)
            self.assertIsNone(
                ppl_guard.decide(payload(None, tool, {"issueKey": "X-1"})), tool)

    def test_tracker_gate_uses_the_snapshot_prefixes(self):
        # rule: R-JIRA-GATE
        snap = paths.run_dir("DEMO-700") / "harness.json"
        doc = json.loads(snap.read_text(encoding="utf-8"))
        doc["tracker"]["mcp_prefixes"] = ["mcp__corp_jira__"]
        saved = snap.read_text(encoding="utf-8")
        snap.write_text(json.dumps(doc), encoding="utf-8")
        try:
            result = ppl_guard.decide(payload(None, "mcp__corp_jira__editJiraIssue", {},
                                              cwd="/tmp/repo"))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
            self.assertIsNone(ppl_guard.decide(payload(None, "mcp__corp_jira__getJiraIssue", {},
                                                       cwd="/tmp/repo")))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_tracker_gate_falls_back_to_the_defaults_without_a_snapshot(self):
        # rule: R-JIRA-GATE
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        snap.unlink()
        try:
            result = ppl_guard.decide(payload(None, "mcp__atlassian__editJiraIssue", {},
                                              cwd="/tmp/repo"))
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_tracker_gate_survives_a_malformed_tracker_table(self):
        # rule: R-JIRA-GATE
        # decide() raising is the same as allowing, so every malformed table must
        # land on the defaults and keep the gate shut.
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        good_ops = doc["tracker"]["write_operations"]
        tables = [None, "jira", 5, [], {}, {"kind": "jira"},
                  {"mcp_prefixes": "mcp__atlassian__", "write_operations": good_ops},
                  {"mcp_prefixes": [], "write_operations": good_ops},
                  {"mcp_prefixes": ["mcp__atlassian__", 7], "write_operations": good_ops},
                  {"mcp_prefixes": ["mcp__atlassian__"], "write_operations": None},
                  {"mcp_prefixes": ["mcp__atlassian__"], "write_operations": [["x"]]}]
        try:
            for table in tables:
                doc["tracker"] = table
                snap.write_text(json.dumps(doc), encoding="utf-8")
                result = ppl_guard.decide(payload(None, "mcp__atlassian__editJiraIssue", {},
                                                  cwd="/tmp/repo"))
                self.assertIn("OWNER_DECISION", self.deny_reason(result), table)
            for table in tables:
                self.assertTrue(ppl_guard.is_tracker_write_tool(
                    "mcp__atlassian__editJiraIssue", table), table)
            self.assertFalse(ppl_guard.is_tracker_write_tool(None, ppl_guard.default_tracker()))
            self.assertFalse(ppl_guard.is_tracker_write_tool(7, None))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_tracker_gate_keeps_the_defaults_when_a_snapshot_prefix_is_not_mcp_shaped(self):
        # rule: R-JIRA-GATE
        # A prefix like mcp__corp_jira (no trailing __) matches no real tool name, so
        # honouring it would silently drop the Atlassian defaults and open the gate.
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        try:
            for prefixes in (["mcp__corp_jira"], ["corp_"], ["mcp__x"], [""],
                             ["mcp__a.b__"], ["mcp__corp_jira__", "mcp__corp_jira"]):
                doc["tracker"]["mcp_prefixes"] = prefixes
                snap.write_text(json.dumps(doc), encoding="utf-8")
                result = ppl_guard.decide(payload(None, "mcp__atlassian__editJiraIssue", {},
                                                  cwd="/tmp/repo"))
                self.assertIn("OWNER_DECISION", self.deny_reason(result), prefixes)
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_the_guarded_names_list_every_numbered_duplicate_the_gate_accepts(self):
        # The explicit-matcher check (ppl doctor) judges a matcher against this list,
        # so a numbered name the gate denies but the list lacks is a denial that
        # cannot fire under that matcher.
        stems = ("atlassian", "claude_ai_Atlassian_Rovo", "other_server")
        candidates = ["mcp__%s_2__%s" % (stem, operation)
                      for stem in stems
                      for operation in ppl_guard.JIRA_WRITE_OPERATIONS + ("getJiraIssue",)]
        accepted = [name for name in candidates if ppl_guard.is_jira_write_tool(name)]
        self.assertEqual(len(accepted), 2 * len(ppl_guard.JIRA_WRITE_OPERATIONS))
        for name in accepted:
            self.assertIn(name, ppl_guard.GUARDED_TOOL_NAMES)

    def test_the_enumerated_jira_write_table_and_the_predicate_agree(self):
        # The table the rule matrix and the routing test iterate and the predicate
        # the cage runs must never drift apart, and neither may match a write
        # operation under a server that is not Atlassian's.
        self.assertEqual(len(ppl_guard.JIRA_WRITE_TOOLS),
                         len(ppl_guard.JIRA_MCP_PREFIXES)
                         * len(ppl_guard.JIRA_WRITE_OPERATIONS))
        for tool in ppl_guard.JIRA_WRITE_TOOLS:
            self.assertTrue(ppl_guard.is_jira_write_tool(tool), tool)
        self.assertFalse(ppl_guard.is_jira_write_tool("mcp__other__addCommentToJiraIssue"))
        self.assertFalse(ppl_guard.is_jira_write_tool("mcp__atlassian__addCommentToJiraIssueX"))
        self.assertFalse(ppl_guard.is_jira_write_tool(None))

    # --- defects found in the brief, not covered by its own test list ---

    def test_invalid_control_file_denies_rather_than_permits(self):
        # rule: R-CONTROL-FAIL-CLOSED
        # control.read returns an {"invalid": True, "command": None, ...} marker for a
        # broken control.json. A check that only tests `record["command"] in HALTING`
        # sees command is None, is not in HALTING, and silently permits every tool call
        # against a run whose halt state cannot be trusted. That is the opposite of the
        # fail-closed behaviour the rest of the system has (see control.enforce).
        control_path = paths.run_dir("DEMO-700") / "control.json"
        store.write_json(control_path, {"not": "a usable control record"})
        try:
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": "ls"}))
            self.assertIsNotNone(result)
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            control.clear("DEMO-700")

    def test_broken_run_state_denies_pipeline_agent_without_raising(self):
        # rule: R-STATE-UNREADABLE-FAIL-CLOSED
        # run.load raises PplError when state.json is missing, but a state.json that
        # exists and is corrupt (half-written by a crashed process) makes json.load
        # raise json.JSONDecodeError, which is not a PplError. A hook that lets that
        # escape crashes on every tool call for as long as the run directory is
        # broken. On top of not crashing: a ppl-* agent_type is pipeline-internal,
        # so a corrupt state.json must deny it outright rather than fail open just
        # because cwd-based scoping is impossible without a readable repo path.
        run.init("DEMO-701", "u", "/tmp/repo2")
        state_file = paths.run_dir("DEMO-701") / "state.json"
        state_file.write_text("{not valid json", encoding="utf-8")
        try:
            result = ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": "ls"}, cwd="/tmp/repo2"))
            self.assertIsNotNone(result)
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            run.set_active("DEMO-700")

    def test_broken_run_state_denies_a_boundary_sensitive_edit(self):
        # The concrete case that matters: a real write, not just an inert `ls`,
        # must be denied while state cannot be read - not merely non-crashing.
        run.init("DEMO-704", "u", "/tmp/repo4")
        state_file = paths.run_dir("DEMO-704") / "state.json"
        state_file.write_text("not json at all", encoding="utf-8")
        try:
            result = ppl_guard.decide(payload(
                "ppl-code-worker", "Edit",
                {"file_path": "/tmp/repo4/anywhere/Foo.java"}, cwd="/tmp/repo4"))
            self.assertIsNotNone(result)
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            run.set_active("DEMO-700")

    def test_broken_run_state_permits_a_non_pipeline_agent_without_raising(self):
        # A tool call with no recognised agent_type cannot be assumed to belong to
        # this specific broken run, so it must not be denied on that basis alone -
        # only that it must not crash. This is what keeps a corrupt run directory
        # from taking down unrelated sessions on the same machine.
        run.init("DEMO-706", "u", "/tmp/repo2")
        state_file = paths.run_dir("DEMO-706") / "state.json"
        state_file.write_text("{not valid json", encoding="utf-8")
        try:
            result = ppl_guard.decide(payload(
                None, "Bash", {"command": "ls"}, cwd="/tmp/repo2"))
            self.assertIsNone(result)
        finally:
            run.set_active("DEMO-700")

    def test_non_dict_state_file_denies_pipeline_agent_without_raising(self):
        # state.json can be syntactically valid JSON that is not an object (e.g. a
        # crashed writer left "[]" or "null"). run.load returns that as-is, so
        # active_state must reject a non-dict result rather than let a later
        # `state.get(...)` raise AttributeError deep inside decide - and, as above,
        # must deny a pipeline agent rather than silently permit it.
        run.init("DEMO-702", "u", "/tmp/repo3")
        state_file = paths.run_dir("DEMO-702") / "state.json"
        state_file.write_text("[]", encoding="utf-8")
        try:
            result = ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": "ls"}, cwd="/tmp/repo3"))
            self.assertIsNotNone(result)
            self.assertIn("OWNER_DECISION", self.deny_reason(result))
        finally:
            run.set_active("DEMO-700")

    def test_decide_does_not_raise_on_non_dict_payload(self):
        for bad in (None, [], "git add -A", 42):
            self.assertIsNone(ppl_guard.decide(bad))

    def test_decide_does_not_raise_on_non_dict_tool_input(self):
        self.assertIsNone(ppl_guard.decide(
            payload("ppl-code-worker", "Bash", "git add -A")))

    # --- finding 1: a global git flag between "git" and the subcommand bypassed
    # every DENIED_COMMANDS pattern at once ---

    def test_add_all_bypass_via_global_dir_flag_is_denied(self):
        # rule: R-GIT-ADD-ALL
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git -C /tmp/repo add -A"}))
        self.assertIsNotNone(result)
        self.assertIn("credential", self.deny_reason(result).lower())

    def test_bare_stash_bypass_via_global_dir_flag_is_denied(self):
        self.assertIsNotNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git -C /tmp/repo stash"})))

    def test_bare_stash_bypass_via_git_dir_flag_is_denied(self):
        # the coordinator's exact reproduction
        self.assertIsNotNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "git --git-dir=/x stash"})))

    def test_stash_pop_bypass_via_global_dir_flag_is_denied(self):
        # rule: R-GIT-STASH
        self.assertIsNotNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git -C /tmp/repo stash pop"})))

    def test_force_push_bypass_via_global_dir_flag_is_denied(self):
        self.assertIsNotNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git -C /tmp/repo push --force origin x"})))

    def test_reset_hard_bypass_via_global_dir_flag_is_denied(self):
        # rule: R-GIT-RESET-HARD
        self.assertIsNotNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git -C /tmp/repo reset --hard"})))

    def test_add_all_bypass_via_c_flag_is_denied(self):
        # rule: R-GIT-ADD-ALL
        # the coordinator's exact reproduction
        self.assertIsNotNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "git -C /tmp/repo add -A"})))

    # --- finding 2: git push -f, the shorthand for --force, was not denied ---

    def test_force_push_shorthand_f_is_denied(self):
        # rule: R-GIT-FORCE-PUSH
        self.assertIsNotNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git push -f origin x"})))

    def test_force_push_long_flag_still_denied(self):
        self.assertIsNotNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git push --force origin x"})))

    def test_plain_git_push_is_allowed(self):
        # regression guard: the -f pattern must not over-match an ordinary push
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git push origin main"})))

    def test_refspec_force_push_is_denied(self):
        # rule: R-GIT-FORCE-PUSH
        # A leading + on a refspec is a force push of that ref, no flag needed.
        for command in ("git push origin +main", "git push origin +HEAD:main",
                        "git push origin '+main'", "git -C /tmp/repo push origin +HEAD:refs/heads/x",
                        "git push origin main +feature", "git push -u origin +feature",
                        # an fd redirect's `&` is no separator
                        "git push 2>&1 origin +main", "git push -q 2>&1 origin +HEAD:main",
                        "git push &>log origin +main", "git push &>>log origin +main",
                        "git push >&log origin +main"):
            for agent in (None, "ppl-code-worker"):
                result = ppl_guard.decide(payload(agent, "Bash", {"command": command}))
                self.assertIn("force push", self.deny_reason(result), command)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)

    def test_a_push_with_no_plus_refspec_is_still_allowed(self):
        for command in ("git push origin main", "git push origin HEAD:main",
                        "git push -u origin feature-x", "git push origin main:main",
                        "git push origin main && echo '1 + 2'",
                        "git push origin main && git log --grep +x",
                        "git push origin main; echo +x", "git push origin main | grep +x",
                        "git push origin main 2>&1",
                        "git push origin main 2>&1 && git log --grep +x",
                        "git push origin main &>/dev/null; echo +x",
                        "git push origin main & echo +x"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_the_refspec_span_stays_fast_on_fd_redirects(self):
        # rule: R-GIT-FORCE-PUSH
        # The span's alternatives share no character, so a run of `>&` and
        # `&>` is read once, at the role's length cap too.
        for command in ("git push " + ">&" * 2000, "git push " + "&>" * 2000,
                        "git push " + ">&>" * 1300, "git push 2>&1 " * 290):
            with self.subTest(command=command[:20]):
                start = time.perf_counter()
                result = ppl_guard.decide(payload(None, "Bash", {"command": command},
                                                  cwd="/tmp/elsewhere"))
                self.assertLess(time.perf_counter() - start, 0.2)
                self.assertIsNone(result)
        result = ppl_guard.decide(payload(None, "Bash",
                                          {"command": "git push " + ">&" * 2000 + " +main"},
                                          cwd="/tmp/elsewhere"))
        self.assertIn("force push", self.deny_reason(result)[:80])

    def test_git_stash_clear_and_a_bare_drop_are_denied(self):
        # rule: R-GIT-STASH
        # clear empties every session's stash, a drop that names no entry takes
        # the top one, likely another session's
        for command in ("git stash clear", "git -C /tmp/repo stash clear", "git stash drop",
                        "git stash drop -q", "git stash drop; git status", "git stash 'drop'",
                        "git -C /tmp/repo stash drop", "git stash drop $REF",
                        "git stash drop\nstash@{0}", "git stash drop 3",
                        "git stash drop stash@{0}x", "git stash drop -q\n-q stash@{0}"):
            for agent in (None, "ppl-code-worker"):
                result = ppl_guard.decide(payload(agent, "Bash", {"command": command}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
                self.assertIn("stash", self.deny_reason(result), command)

    def test_git_stash_pop_is_still_denied(self):
        # rule: R-GIT-STASH
        for command in ("git stash pop", "git stash pop stash@{0}", "git stash 'pop'"):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)

    def test_a_stash_drop_naming_its_entry_is_allowed(self):
        # the shared-stack recipe: push -m <tag>, apply <sha>, then drop the
        # entry by the index it is re-found at
        for command in ("git stash drop stash@{0}", "git stash drop -q stash@{3}",
                        "git stash drop 'stash@{12}'", "git stash drop --quiet stash@{1}",
                        "git -C /tmp/repo stash drop stash@{2}",
                        'git stash drop "stash@{1}" && git stash list'):
            for agent in (None, "ppl-code-worker"):
                self.assertIsNone(ppl_guard.decide(payload(
                    agent, "Bash", {"command": command})), command)

    def test_the_stash_drop_shape_stays_fast_on_decoys(self):
        # rule: R-GIT-STASH
        # one parse per token, like the other git shapes: a hook that runs past
        # its timeout denies nothing
        for command, denied in (("git stash drop" + " -q" * 6000, True),
                                ("git stash drop" + " -q" * 6000 + " stash@{0}", False),
                                ("git stash drop" + " -q" * 6000 + " x", True),
                                ("git stash" + " " * 20000 + "drop", True),
                                ("git stash drop " * 2000, True),
                                ("git stash drop" + " --quiet-quiet" * 3000 + "=", True),
                                ("git" + " -a" * 34 + " stash drop", True)):
            with self.subTest(command=command[:40], denied=denied):
                start = time.perf_counter()
                result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
                self.assertLess(time.perf_counter() - start, 0.2)
                if denied:
                    self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                else:
                    self.assertIsNone(result)

    def test_the_stash_commands_that_leave_the_stack_alone_are_allowed(self):
        for command in ("git stash push -u -m ppl-700", "git stash push -m x",
                        "git stash apply 1a2b3c4", "git stash list",
                        "git stash show -p stash@{0}"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_git_denials_stay_fast_on_backtracking_flag_decoys(self):
        # rule: R-GIT-ADD-ALL, R-GIT-FORCE-PUSH, R-GIT-RESET-HARD
        # A hook that runs past its timeout denies nothing, so a decoy that
        # makes the flag grammar backtrack is a bypass. The first one took 14 s
        # before the grammar had one parse per token.
        for command, why in (("git" + " -a" * 34 + " zzz; git add -A", "credential"),
                             ("git" + " -a" * 34 + " zzz; git push --force", "force push"),
                             ("git" + " -c k=v" * 40 + " reset --hard", "hard reset"),
                             # A quoted value that a bare-word alternative could
                             # also match is the same ambiguity in another coat.
                             ("git" + " -C 'a'" * 40 + " zzz; git add -A", "credential"),
                             ("git" + ' -C "a b"' * 40 + " zzz; git add -A", "credential"),
                             ("git" + ' --x="y z"' * 40 + " zzz; git add -A", "credential"),
                             ("git" + ' -C  "a b"' * 40 + " zzz; git add -A", "credential"),
                             ("git" + " --x=" * 40 + " zzz; git add -A", "credential"),
                             # A quoted flag is a flag and a quoted word at once:
                             # the value grammar must refuse it or each token
                             # parses twice.
                             ("git" + " '-a'" * 40 + " zzz; git add -A", "credential"),
                             ("git" + r" \-a" * 40 + " zzz; git add -A", "credential"),
                             ("git -C" + " " * 20000 + "x add -A", "credential"),
                             ('git -C "' + 'a\\"' * 2000 + '" add -A', "credential")):
            with self.subTest(command=command[:40]):
                start = time.perf_counter()
                result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
                self.assertLess(time.perf_counter() - start, 0.2)
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
                self.assertIn(why, self.deny_reason(result))

    def test_git_denials_still_see_through_value_and_value_less_global_flags(self):
        # rule: R-GIT-ADD-ALL, R-GIT-FORCE-PUSH, R-GIT-STASH
        # The shapes a stricter grammar would have lost: a config value naming
        # a verb, a value-less flag before a value flag, a value starting with
        # a dash, and the spaced form of a long value flag. Then the values the
        # bare-word grammar could not read: a dash after `=`, a quoted value
        # with a space, a quoted piece glued to a bare one, an escaped quote.
        for command in ("git -c push.default=current add -A",
                        "git -c rebase.autoStash=true push --force origin x",
                        "git --no-pager -C /tmp/repo add -A",
                        "git --no-pager add -A",
                        "git -C -odd stash pop",
                        "git --git-dir /x stash",
                        "git -c user.name=a -c user.email=b push -f origin x",
                        "git --exec-path=-x add -A",
                        "git --exec-path=-x push --force",
                        'git -C "/my dir" add -A',
                        "git -C '/my dir' stash",
                        "git -c 'core.x=a b' add -A",
                        'git -C "/my dir"/sub add -A',
                        r"git -C a\'b add -A",
                        r"git -C /my\ dir add -A",
                        r'git -C "a\"b c" add -A',
                        "git -C - add -A",
                        "git --attr-source  HEAD add -A",
                        "git -C\t/tmp/x add -A",
                        "git --git-dir= add -A",
                        "git --attr-source '-x y' add -A",
                        # A backslash-newline inside a value is still the value.
                        'git -C "a\\\nb" add -A',
                        'git -c "k=a\\\nb" push --force origin x',
                        "git -C a\\\nb add -A"):
            result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
        for command in ("git -c push.default=current push origin main",
                        "git -C /tmp/x status -s",
                        "git stash push -u -m tag"):
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": command})),
                              command)

    def test_git_denials_see_a_terminator_glued_to_the_option(self):
        # rule: R-GIT-ADD-ALL, R-GIT-STASH, R-GIT-FORCE-PUSH
        # The shell ends a word at a semicolon, `&`, `|`, `)` and `>`, so a
        # terminator glued to the dangerous option is still that option. A different flag
        # (`-Ax`), a subcommand (`stash-list`, `stash push`) or a path (`./src`)
        # is not.
        for command in ("git add -A;echo",
                        "git add -A&&echo x",
                        "git add .;echo",
                        "(git add -A)",
                        "git add -A>/dev/null",
                        "git add ./",
                        "git stash;echo",
                        "git stash 2>&1",
                        "git stash\necho",
                        "git stash </dev/null",
                        "git stash `x`",
                        "git add ..",
                        "git push -f;echo",
                        "git push origin x -f>/dev/null"):
            result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
        for command in ("git add -Ax",
                        "git add src/test/Foo.java",
                        "git add ./src",
                        "git add ./.gitignore",
                        "git add .gitignore",
                        "git stash-list",
                        "git stash list",
                        "git stash push -u -m tag"):
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": command})),
                              command)

    def test_git_denials_skip_plain_options_before_the_dangerous_one(self):
        # rule: R-GIT-ADD-ALL, R-GIT-STASH, R-GIT-FORCE-PUSH, R-GIT-RESET-HARD
        # git accepts its options in any order, so a plain option before the
        # dangerous one (or `--` before `.`) must not hide it, and a force
        # flag may ride in a short-flag cluster. An option before a path, a
        # ref or a stash subcommand is still an ordinary command.
        for command in ("git add -v -A",
                        "git add --verbose --all",
                        "git add -f .",
                        "git add -- .",
                        "git add -v -v -v -A",
                        "git add '-v' -A",
                        "git add --chmod=+x -A",
                        "git reset -q --hard",
                        "git stash -q",
                        "git stash --quiet",
                        "git stash -u",
                        "git add -vA",
                        "git add -Av",
                        "git add -v -vA",
                        "git push -fu origin x",
                        "git push origin x -uf",
                        "git push '-fu' origin x",
                        "git push -f4 origin x",
                        "git push -4f origin x"):
            result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
        for command in ("git add -v src/test/Foo.java",
                        "git add -f src/x",
                        "git add -v",
                        "git add -p src/x",
                        "git add -N src/x",
                        "git reset -q HEAD~1",
                        "git stash -q push -u -m tag",
                        "git stash -m tag",
                        "git stash list",
                        "git push -u origin x",
                        "git push -4 origin x",
                        "git push --follow-tags origin x"):
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": command})),
                              command)
        # A long option run has one parse whether the dangerous option follows
        # or not, so both shapes are decided at once.
        for command, denied in (("git add " + "-v " * 2000 + "-A", True),
                                ("git add " + "-v " * 2000 + "src/x", False)):
            start = time.perf_counter()
            result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
            self.assertLess(time.perf_counter() - start, 0.2)
            self.assertEqual(result is not None, denied, command[:20])

    def test_a_role_bash_command_over_the_length_cap_is_denied_before_any_pattern(self):
        # rule: R-GUARD-ROLE-COMMAND-LENGTH
        # The patterns are linear per token but every `git` start re-scans the
        # tail, so a padded command from a role is refused on length alone,
        # before a single pattern runs. 16 KB of `git -a ` took 4.8 s inside
        # decide, within 5% of the hook budget, hence the 4 KB cap.
        limit = ppl_guard.ROLE_COMMAND_LENGTH_LIMIT
        self.assertEqual(limit, 4096)
        just_over = ("git -a " * 700)[:limit + 1]
        for agent in ("ppl-code-worker", "ppl-test-worker", "ppl-review-responder"):
            start = time.perf_counter()
            result = ppl_guard.decide(payload(agent, "Bash", {"command": just_over}))
            self.assertLess(time.perf_counter() - start, 0.01)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), agent)
            self.assertIn("may not exceed 4096 characters", self.deny_reason(result), agent)
        # The cap is about length, not content: a harmless long command is
        # refused the same way, and one exactly at the cap is not.
        harmless = "echo " + "x" * limit
        result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": harmless}))
        self.assertIn("may not exceed 4096 characters", self.deny_reason(result))
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": harmless[:limit]})))
        # Just under the cap every pattern runs on the worst shapes, and the
        # decision must still land well inside the hook budget. The two
        # clusters end in a digit, so no pattern matches and every `git`
        # start scans the whole letter run: that shape took 8 s before the
        # cluster was read in one pass.
        push_cluster = "git push " * 80 + "-" + "f" * 3374 + "1"
        add_cluster = "git add " * 50 + "-" + "v" * 3693 + "A1"
        self.assertEqual((len(push_cluster), len(add_cluster)), (limit, limit))
        for command in (("git -a " * 700)[:limit], ("gh pr review x " * 300)[:limit],
                        ("git push " * 80 + "-" + "f" * 3900 + "1")[:limit],
                        push_cluster, add_cluster):
            start = time.perf_counter()
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertLess(time.perf_counter() - start, 1.0, command[:20])
            self.assertTrue(result is None or "may not exceed" not in self.deny_reason(result))

    def test_an_unroled_bash_command_has_no_length_cap(self):
        # rule: R-GUARD-ROLE-COMMAND-LENGTH
        # The orchestrator session keeps the patterns and no cap: a long
        # harmless command passes, and a long denied one is denied for its
        # own reason, not for its length.
        harmless = "echo " + "x" * 16384
        self.assertGreater(len(harmless), ppl_guard.ROLE_COMMAND_LENGTH_LIMIT)
        self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": harmless})))
        result = ppl_guard.decide(payload(None, "Bash", {"command": harmless + "; git reset --hard"}))
        self.assertIn("hard reset", self.deny_reason(result))
        self.assertNotIn("may not exceed", self.deny_reason(result))

    def test_git_denials_see_through_a_quoted_or_escaped_verb_or_option(self):
        # rule: R-GIT-ADD-ALL, R-GIT-STASH, R-GIT-FORCE-PUSH, R-GIT-RESET-HARD
        # The shell unquotes before git runs, so one quote or backslash around
        # the verb, the dangerous option or a global flag must not hide the
        # command. The verb must still sit in the subcommand position: a
        # commit message that says "add -A" is not a staging command.
        for command in ("git 'add' -A",
                        'git "add" -A',
                        "git add '-A'",
                        r"git \add -A",
                        r"git add \-A",
                        'git "stash"',
                        "git 'stash' 'pop'",
                        r"git \-C x add -A",
                        "git '-C' x add -A",
                        "git 'push' --force origin x",
                        "git push origin x '-f'",
                        r"git push origin x \-f",
                        "git push>/dev/null --force origin x",
                        "git 'reset' '--hard'",
                        r"git reset \--hard"):
            result = ppl_guard.decide(payload(None, "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
        for command in ('git commit -m "add -A later"',
                        "git commit -m 'stash'",
                        "git push origin main"):
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": command})),
                              command)

    # --- finding 3: a Bash write into runs/ bypassed the run-state protection
    # entirely, since only file_path-style tool_input was inspected ---

    def test_bash_redirect_into_runs_dir_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        target = str(paths.run_dir("DEMO-700") / "state.json")
        result = ppl_guard.decide(payload(
            None, "Bash", {"command": "echo x > %s" % target}))
        self.assertIsNotNone(result)

    def test_bash_tee_into_runs_dir_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        target = str(paths.run_dir("DEMO-700") / "state.json")
        self.assertIsNotNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "echo x | tee %s" % target})))

    def test_bash_sed_inplace_on_runs_dir_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        target = str(paths.run_dir("DEMO-700") / "state.json")
        self.assertIsNotNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "sed -i '' 's/a/b/' %s" % target})))

    def test_bash_rm_in_runs_dir_is_denied(self):
        target = str(paths.run_dir("DEMO-700") / "state.json")
        self.assertIsNotNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "rm %s" % target})))

    def test_bash_cp_into_runs_dir_is_denied(self):
        target = str(paths.run_dir("DEMO-700") / "state.json")
        self.assertIsNotNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "cp /tmp/x.json %s" % target})))

    def test_bash_read_of_runs_dir_is_allowed(self):
        # conservative deny only, not a blanket ban - a plain read must pass
        target = str(paths.run_dir("DEMO-700") / "state.json")
        self.assertIsNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "cat %s" % target})))

    # --- the runs-root check resolves the target, so another spelling of the
    # same path is the same path ---

    def home_spelled(self):
        """HOME pinned to the data root's parent, so `~/<name>` is the data root."""
        data_root = paths.home()
        return (mock.patch.dict(os.environ, {"HOME": str(data_root.parent)}),
                "~/%s" % data_root.name)

    def run_state_denial(self, result, label):
        """The deny reason, after checking it is the runs-root one."""
        self.assertIsNotNone(result, label)
        self.assertIn("run state is written only by ppl", self.deny_reason(result), label)
        return self.deny_reason(result)

    def test_tilde_spelled_bash_write_into_runs_is_denied_for_every_caller(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        commands = ("echo '{}' > %s/runs/DEMO-700/state.json" % tilde,
                    "echo '{}' | tee %s/runs/DEMO-700/boundary.json" % tilde,
                    "cp /tmp/x.json %s/runs/DEMO-700/harness.json" % tilde)
        with patched:
            for agent in (None, "ppl-code-worker", "ppl-test-worker"):
                for command in commands:
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_cd_relative_bash_write_into_runs_is_denied_for_every_caller(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        data_root = str(paths.home())
        cd_command = "cd %s && cd runs/DEMO-700 && echo x > state.json" % tilde
        with patched:
            for agent in (None, "ppl-code-worker"):
                result = ppl_guard.decide(payload(agent, "Bash", {"command": cd_command},
                                                  cwd="/tmp/elsewhere"))
                self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, agent))
                # relative to the session's own cwd, with no cd at all
                result = ppl_guard.decide(payload(
                    agent, "Bash", {"command": "echo x > runs/DEMO-700/state.json"},
                    cwd=data_root))
                self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, agent))

    def test_bash_write_into_runs_with_no_active_run_is_still_denied(self):
        # rule: R-RUNS-BASH-WRITE
        # global, no run required: the check runs before active_state()
        patched, tilde = self.home_spelled()
        command = "echo x > %s/runs/DEMO-700/state.json" % tilde
        with patched, mock.patch.object(ppl_guard.run, "active", return_value=None):
            result = ppl_guard.decide(payload(None, "Bash", {"command": command},
                                              cwd="/tmp/elsewhere"))
        self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, "no active run"))

    def test_home_variable_spelling_is_denied_for_a_role_by_the_unresolvable_target_rule(self):
        # rule: R-RUNS-BASH-WRITE
        # The resolver does not expand variables, so the runs-root check cannot
        # judge `$HOME/...`. A role is still denied, by the write boundary's
        # fail-closed rule for a target it cannot resolve. An unroled session
        # outside the repo is not: R-RUNS-BASH-WRITE's residual says so.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x > $HOME/.ppl/runs/DEMO-700/state.json"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("could not be resolved", self.deny_reason(result))

    def test_tilde_spelled_bash_write_to_control_json_is_allowed_for_the_operator(self):
        # control.json is the operator's inbox: the unroled session keeps the carve-out
        patched, tilde = self.home_spelled()
        with patched:
            self.assertIsNone(ppl_guard.decide(payload(
                None, "Bash",
                {"command": "echo '{}' > %s/runs/DEMO-700/control.json" % tilde},
                cwd="/tmp/elsewhere")))
            result = ppl_guard.decide(payload(
                None, "Bash", {"command": "echo '{}' > %s/runs/DEMO-700/state.json" % tilde},
                cwd="/tmp/elsewhere"))
        self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, "unroled state.json"))

    def test_a_role_gets_no_control_json_carve_out_in_bash(self):
        # rule: R-RUNS-BASH-WRITE
        # A role that rewrites the control inbox can lift the operator's pause.
        patched, tilde = self.home_spelled()
        commands = ("echo x > %s/runs/DEMO-700/control.json" % tilde,
                    "cd %s/runs/DEMO-700 && echo '{}' > control.json" % tilde,
                    "echo '{}' | tee %s/runs/DEMO-700/control.json" % tilde)
        with patched:
            for agent in ("ppl-code-worker", "ppl-test-worker", "ppl-review-responder"):
                for command in commands:
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_subshell_brace_group_pushd_or_env_cd_into_runs_is_followed(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        run_dir = "%s/runs/DEMO-700" % tilde
        commands = ("(cd %s && echo x > boundary.json)" % run_dir,
                    "( cd %s && echo x > boundary.json )" % run_dir,
                    "((cd %s && echo x > boundary.json))" % run_dir,
                    "{ cd %s; echo x > boundary.json; }" % run_dir,
                    "pushd %s && echo x > boundary.json" % run_dir,
                    "env -C %s cp /tmp/x boundary.json" % run_dir,
                    "env --chdir=%s tee boundary.json" % run_dir,
                    "env --chdir %s cp /tmp/x boundary.json" % run_dir,
                    "env -i FOO=1 -C %s rm state.json" % run_dir)
        with patched:
            for agent in (None, "ppl-code-worker"):
                for command in commands:
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_wrapper_in_front_of_a_write_verb_is_unwrapped(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        run_dir = "%s/runs/DEMO-700" % tilde
        commands = ("nohup cp /tmp/x %s/state.json" % run_dir,
                    "exec tee %s/boundary.json" % run_dir,
                    "exec -a name cp /tmp/x %s/state.json" % run_dir,
                    "command mv %s /tmp/x" % tilde,
                    "command -p rm %s/state.json" % run_dir,
                    "builtin cd %s && echo x > state.json" % run_dir,
                    "nice -n 5 install /tmp/x %s/y" % run_dir,
                    "nice -n5 cp /tmp/x %s/y" % run_dir,
                    "nice -5 cp /tmp/x %s/y" % run_dir,
                    "time cp /tmp/x %s/z" % run_dir,
                    "time -p cp /tmp/x %s/z" % run_dir,
                    "nohup nice -n 5 cp /tmp/x %s/state.json" % run_dir,
                    "time nohup env -C %s cp /tmp/x state.json" % run_dir,
                    "command cd %s && echo x > state.json" % run_dir,
                    "time cd %s && echo x > state.json" % run_dir)
        with patched:
            for agent in (None, "ppl-code-worker"):
                for command in commands:
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_path_spelled_verb_or_wrapper_is_read_by_its_name(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        run_dir = "%s/runs/DEMO-700" % tilde
        commands = ("/bin/cp /tmp/x %s/state.json" % run_dir,
                    "/usr/bin/nohup cp /tmp/x %s/state.json" % run_dir,
                    "/usr/bin/env -C %s cp /tmp/x state.json" % run_dir,
                    "/bin/rm %s/state.json" % run_dir,
                    "/usr/bin/tee %s/boundary.json" % run_dir)
        with patched:
            for agent in (None, "ppl-code-worker"):
                for command in commands:
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))
            # a cd spelled by path is a program of its own and moves no shell
            self.assertIsNone(ppl_guard.decide(payload(
                None, "Bash", {"command": "/usr/bin/cd %s && echo x > notes.txt" % run_dir},
                cwd="/tmp/elsewhere")))

    def test_a_path_spelled_verb_into_the_repo_gets_the_role_decision(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("/bin/cp /tmp/x %s" % test_source,
                        "/usr/bin/nohup cp /tmp/x %s" % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)

    def test_a_wrapped_command_elsewhere_is_still_allowed(self):
        # nohup, nice and exec start a new process, so a cd behind them does not
        # move the shell, and command -v runs nothing
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("nohup cp x /tmp/y", "nice -n 5 make build", "time make test",
                            "command -v cp", "exec ls",
                            "nohup cd %s/runs/DEMO-700 && echo x > notes.txt" % tilde,
                            "nice cd %s/runs/DEMO-700; echo x > notes.txt" % tilde):
                for agent in (None, "ppl-code-worker"):
                    self.assertIsNone(ppl_guard.decide(payload(
                        agent, "Bash", {"command": command}, cwd="/tmp/elsewhere")),
                        "%s: %s" % (agent, command))

    def test_a_wrapper_in_front_of_a_repo_write_gets_the_role_decision(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        for command, why in (
                ("nohup cp /tmp/x source/core/orders/src/test/java/FooTest.java", "test"),
                ("nice -n 5 tee source/core/orders/src/test/java/FooTest.java", "test"),
                # a wrapper option this does not know: the target is not pinned down
                ("nice --weird cp /tmp/x source/core/orders/Foo.java", "could not be resolved")):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn(why, self.deny_reason(result), command)

    def test_the_wrapper_unwrap_stays_linear(self):
        # rule: R-RUNS-BASH-WRITE
        for command in ("nohup " * 20000 + "cp /tmp/x /tmp/y",
                        "nice -n 5 " * 10000 + "cp /tmp/x /tmp/y",
                        "command -p " * 10000 + "cp /tmp/x /tmp/y",
                        "time -p exec -a n " * 5000 + "cp /tmp/x /tmp/y",
                        "nohup nice env -C /tmp " * 5000 + "cp /tmp/x /tmp/y"):
            with self.subTest(command=command[:30]):
                start = time.perf_counter()
                self.assertFalse(ppl_guard.bash_write_lands_in_runs(command, "/tmp"))
                self.assertLess(time.perf_counter() - start, 0.2)

    def test_a_shell_string_behind_env_cd_is_opaque_and_allowed(self):
        # Pinned residual: the cd lands in runs/, but the write sits inside a
        # quoted sh -c string, which is not parsed (R-RUNS-BASH-WRITE residual).
        patched, tilde = self.home_spelled()
        command = "env -C %s/runs/DEMO-700 sh -c 'echo x > boundary.json'" % tilde
        with patched:
            for agent in (None, "ppl-code-worker"):
                self.assertIsNone(ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                           cwd="/tmp/elsewhere")), agent)

    def test_a_subshell_or_pushd_cd_ends_where_the_shell_says(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # A subshell's cd ends at its `)`, popd returns, and env -C moves only
        # the command it runs, never the shell's own redirect.
        patched, tilde = self.home_spelled()
        test_source = "source/core/orders/src/test/java/FooTest.java"
        with patched:
            for command in ("(cd /tmp); echo x > %s" % test_source,
                            "(cd /tmp && true) && echo x > %s" % test_source,
                            "pushd /tmp && popd && echo x > %s" % test_source,
                            "env -C /tmp true > %s" % test_source):
                result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            for command, cwd in (("(cd /tmp && echo x > y)", "/tmp/repo"),
                                 ("pushd %s/runs/DEMO-700 && cat state.json" % tilde, "/tmp/elsewhere"),
                                 ("(cd %s/runs/DEMO-700) && echo x > notes.txt" % tilde, "/tmp/elsewhere"),
                                 ("env -C %s/runs/DEMO-700 true > notes.txt" % tilde, "/tmp/elsewhere")):
                self.assertIsNone(ppl_guard.decide(payload(
                    "ppl-code-worker", "Bash", {"command": command}, cwd=cwd)), command)

    def test_a_redirect_on_a_cd_pushd_or_popd_is_still_a_write(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # The shell opens a cd's redirect in the directory it starts from.
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("pushd /tmp > %s" % test_source, "popd > %s" % test_source,
                        "command cd /tmp > %s" % test_source,
                        "builtin cd . > %s" % test_source, "cd . > %s" % test_source,
                        "pushd /tmp >> %s" % test_source, "cd /tmp 2>%s" % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)
        result = ppl_guard.decide(payload(None, "Bash",
                                          {"command": "pushd /tmp > %s" % test_source}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_redirect_on_a_cd_into_runs_is_denied_for_every_caller(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        state = "%s/runs/DEMO-700/state.json" % tilde
        with patched:
            for command in ("pushd /tmp > %s" % state, "popd > %s" % state,
                            "command cd /tmp > %s" % state, "cd /tmp > %s" % state,
                            "cd > %s" % state):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_cd_with_a_harmless_redirect_still_moves_the_shell(self):
        # the redirect is not an operand, so the cd lands where it says
        for command in ("pushd /tmp > /dev/null && echo x > y",
                        "cd /tmp 2>/dev/null && echo x > y",
                        "pushd /tmp >/dev/null 2>&1; cp /tmp/a b"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_a_quoted_paren_does_not_close_the_subshell(self):
        # rule: R-RUNS-BASH-WRITE
        # Grouping is read off the raw text outside quotes, not off tokens
        # shlex has already unquoted.
        patched, tilde = self.home_spelled()
        run_dir = "%s/runs/DEMO-700" % tilde
        with patched:
            for command in ('(cd %s && echo "(ok)" && echo x > state.json)' % run_dir,
                            "(cd %s && echo 'a)'; echo x > state.json)" % run_dir,
                            "(cd %s && echo x > 'notes)')" % run_dir):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_quoted_paren_inside_a_subshell_keeps_its_directory(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ('(cd /tmp && echo "x)" > y)',
                        "(cd /tmp && echo x > 'y)' && echo z > %s)" % test_source):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "(cd /tmp && echo 'y)'); echo z > %s" % test_source}))
        self.assertIn("test", self.deny_reason(result))

    def test_a_redirect_after_a_group_opens_where_the_group_opened(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # The shell opens a redirect after a `)` or `}` outside the group, in
        # the cwd the group started in, not where its cd moved.
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("(cd /tmp && echo x) > %s" % test_source,
                        "{ cd /tmp; echo x; } > %s" % test_source,
                        "( cd /tmp && ( cd /var && echo n ) > x ) > %s" % test_source,
                        '(cd /tmp && echo "x)") > %s' % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)

    def test_a_closer_with_no_group_resolves_its_redirect_everywhere(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        test_source = "source/core/orders/src/test/java/FooTest.java"
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "echo x ) > %s" % test_source}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("test", self.deny_reason(result))

    def test_a_closed_group_gives_its_cwd_back_whatever_follows_its_closer(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # An input redirect, a here-string or a comment after a `)` does not
        # change where the subshell ends, so a later write opens outside it.
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("(cd /tmp && echo x) < /dev/null; echo y > %s" % test_source,
                        "(cd /tmp && echo x) <<< hi; echo y > %s" % test_source,
                        "(cd /tmp && echo x) # note\necho y > %s" % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)
        # a `}` keeps the cwd its group moved to, an input redirect or not
        self.assertEqual(ppl_guard._bash_write_targets(
            "{ cd /tmp; } < /dev/null; echo z > g", "/tmp/repo"), [("g", "/tmp", False)])

    def test_a_closed_group_into_runs_gives_its_cwd_back_for_every_caller(self):
        # rule: R-RUNS-BASH-WRITE
        patched, _ = self.home_spelled()
        with patched:
            for agent in (None, "ppl-code-worker"):
                command = "(cd /tmp) < /dev/null; echo x > state.json"
                result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                  cwd=str(paths.run_dir("DEMO-700"))))
                self.assertIn("BOUNDARY_VIOLATION",
                              self.run_state_denial(result, "%s: %s" % (agent, command)))

    def test_a_bare_ampersand_ends_a_command(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # A bare `&` ends a command like a semicolon, and the list before it
        # runs in a subshell, so a cd in it does not move the shell.
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("true & cp /tmp/x %s" % test_source,
                        "true&cp /tmp/x %s" % test_source,
                        "true |& tee %s" % test_source,
                        "cd /tmp & echo y > %s" % test_source,
                        "cd /tmp && echo x & echo y > %s" % test_source,
                        "(cd /tmp && echo x) & echo y; echo z > %s" % test_source,
                        "{ cd /tmp; } & echo y; echo z > %s" % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)

    def test_a_bare_ampersand_does_not_hide_a_runs_write(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("true & rm -rf %s" % tilde,
                            "true & cp /tmp/x %s/runs/DEMO-700/state.json" % tilde):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_an_fd_ampersand_or_a_quoted_one_is_no_command_end(self):
        for command in ('curl "http://x?a=1&b=2" > /tmp/o.json',
                        "make 2>&1 & echo y > /tmp/out", "make &> /tmp/l; echo $? > /tmp/rc",
                        "cat <&0 > /tmp/out"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)
        self.assertEqual(ppl_guard._bash_write_targets(
            "cd /tmp && echo x & cd /var; echo y > g", "/tmp/repo"), [("g", "/var", False)])

    def test_a_redirect_after_a_group_outside_the_repo_is_still_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "(cd /tmp && echo x) > /tmp/out"})))

    def test_a_group_redirect_target_is_resolved_where_the_shell_opens_it(self):
        # A `)` gives its cwd back, a `}` does not, and nested groups unwind
        # one at a time.
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command, expected in (
                ("(cd /tmp && make) > build.log", [("build.log", "/tmp/repo", False)]),
                ("{ cd /tmp; echo x; } > f; echo y > g",
                 [("f", "/tmp/repo", False), ("g", "/tmp", False)]),
                ("( cd /tmp && ( cd /var && echo n ) > x ) > %s" % test_source,
                 [("x", "/tmp", False), (test_source, "/tmp/repo", False)])):
            self.assertEqual(ppl_guard._bash_write_targets(command, "/tmp/repo"), expected,
                             command)

    def test_a_group_redirect_after_a_cd_into_runs_opens_outside_it(self):
        patched, tilde = self.home_spelled()
        command = "(cd %s/runs/DEMO-700 && echo x) > out.txt" % tilde
        with patched:
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": command},
                                                       cwd="/tmp/elsewhere")))

    def test_a_group_redirect_into_runs_with_no_active_run_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        command = "(cd /tmp && echo x) > %s/runs/DEMO-700/state.json" % tilde
        with patched, mock.patch.object(ppl_guard.run, "active", return_value=None):
            result = ppl_guard.decide(payload(None, "Bash", {"command": command},
                                              cwd="/tmp/elsewhere"))
        self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, "no active run"))

    def test_a_redirect_glued_to_a_word_is_a_redirect(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # The shell ends a word at an unquoted `>`, so `x>f` writes f
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("echo x>%s" % test_source, "echo x>|%s" % test_source,
                        "echo x>>%s" % test_source, "echo x&>%s" % test_source,
                        "echo 1&>%s" % test_source, "cat /tmp/a.txt>%s" % test_source,
                        "echo '1'>%s" % test_source, "printf %%s --x=1>%s" % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)

    def test_a_redirect_glued_to_a_word_into_runs_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("echo x>%s/runs/DEMO-700/state.json" % tilde,
                            "echo x>|%s/runs/DEMO-700/state.json" % tilde):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_quoted_or_escaped_angle_bracket_is_no_redirect(self):
        main_source = "source/core/orders/src/main/java/Foo.java"
        for command in ("awk '$1>2' %s" % main_source, 'grep ">x" %s' % main_source,
                        'echo "a>b" > /tmp/x', "echo a\\>b > /tmp/x",
                        "awk '$1>2' %s > /tmp/out" % main_source,
                        "echo x 2>/dev/null", "echo x 2>&1", "echo $?>/tmp/rc"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_closing_an_fd_names_no_file(self):
        for command in ("echo x 2>&-", "echo x >&-", "exec 3>&-"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_popd_on_an_empty_stack_keeps_the_directory(self):
        # rule: R-RUNS-BASH-WRITE
        # popd fails on an empty stack and the shell stays where it was
        patched, tilde = self.home_spelled()
        run_dir = "%s/runs/DEMO-700" % tilde
        with patched:
            for command, cwd in (("cd %s; popd; echo x > state.json" % run_dir, "/tmp/elsewhere"),
                                 ("popd; echo x > state.json", str(paths.run_dir("DEMO-700"))),
                                 ("pushd /tmp; popd; popd; echo x > state.json",
                                  str(paths.run_dir("DEMO-700")))):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd=cwd))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_the_cd_and_grouping_walk_stays_linear(self):
        # rule: R-RUNS-BASH-WRITE
        # Leading groups were taken off one token at a time with pop(0), which
        # is quadratic on spaced `( ( ( `. A wall-clock ceiling flaked on slow CI
        # runners, so each shape is timed at half size and at full size instead:
        # a linear walk about doubles, a quadratic one about quadruples.
        shapes = ((9000, lambda n: "pushd /tmp && popd && " * n + "echo x > y"),
                  (100000, lambda n: "( " * n + "cd /tmp && echo x > y"),
                  (100000, lambda n: "{ " * n + "cd /tmp && echo x > y"),
                  (100000, lambda n: "cd /tmp && echo x > y" + " )" * n),
                  (20000, lambda n: "(cd /tmp && " * n + "echo x > y" + ")" * n),
                  (200 * 1024 // 29, lambda n: "( cd /tmp && echo x ) > y && " * n + "echo x > y"))

        def best_of_three(command):
            times = []
            for _ in range(3):
                start = time.perf_counter()
                self.assertFalse(ppl_guard.bash_write_lands_in_runs(command, "/tmp"))
                times.append(time.perf_counter() - start)
            return min(times)

        for size, shape in shapes:
            with self.subTest(command=shape(size)[:30]):
                self.assertLess(best_of_three(shape(size)), 3 * best_of_three(shape(size // 2)))

    def test_a_clobber_redirect_is_a_redirect(self):
        # rule: R-RUNS-BASH-WRITE
        # `>|` writes like `>`, and its `|` is not a pipe
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("echo x >| %s/runs/DEMO-700/state.json" % tilde,
                            "echo x >|%s/runs/DEMO-700/state.json" % tilde,
                            "echo x 2>| %s/runs/DEMO-700/state.json" % tilde):
                result = ppl_guard.decide(payload(None, "Bash", {"command": command},
                                                  cwd="/tmp/elsewhere"))
                self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, command))
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x >| source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        for command in ("echo x >| /tmp/x", "echo x >|/tmp/x"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_removing_or_moving_a_parent_of_runs_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("rm -rf %s" % tilde, "mv %s /tmp/x" % tilde, "rm -rf ~",
                            "rm -rf %s/runs" % tilde, "cd %s && rm -rf ." % tilde,
                            "rm -rf /"):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_writing_beside_runs_under_the_data_root_is_allowed(self):
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("rm -rf %s/harnesses/x" % tilde,
                            "mv %s/harnesses/a %s/harnesses/b" % (tilde, tilde),
                            "rm -rf /tmp/elsewhere/build",
                            "rm -rf %s/runs-old" % tilde, "rm -rf %s/runsX" % tilde,
                            "mv %s/runsX %s/runs-old" % (tilde, tilde)):
                for agent in (None, "ppl-code-worker"):
                    self.assertIsNone(ppl_guard.decide(payload(
                        agent, "Bash", {"command": command}, cwd="/tmp/elsewhere")),
                        "%s: %s" % (agent, command))

    def test_a_copy_into_a_parent_of_runs_is_allowed(self):
        # Only a removal can take runs/ away with its parent: a copy, a move's
        # destination or a sync into ~ writes beside runs/, not over it.
        patched, tilde = self.home_spelled()
        with patched:
            for command, cwd in (("cp x ~", "/tmp/elsewhere"), ("cp x ~/", "/tmp/elsewhere"),
                                 ("mv /tmp/a ~/", "/tmp/elsewhere"),
                                 ("rsync -a /tmp/src/ ~/", "/tmp/elsewhere"),
                                 ("cp x %s/" % tilde, "/tmp/elsewhere"),
                                 ("cp /tmp/x .", str(paths.home().parent))):
                for agent in (None, "ppl-code-worker"):
                    self.assertIsNone(ppl_guard.decide(payload(
                        agent, "Bash", {"command": command}, cwd=cwd)),
                        "%s: %s" % (agent, command))

    def test_a_removal_that_takes_runs_with_it_is_still_denied(self):
        # rule: R-RUNS-BASH-WRITE
        patched, tilde = self.home_spelled()
        with patched:
            for command in ("rm -rf %s" % tilde, "mv %s /tmp/x" % tilde,
                            "git rm -r %s" % tilde, "git mv %s /tmp/x" % tilde,
                            "rsync -a --delete /tmp/src/ ~/", "rsync -a --del /tmp/src/ ~/"):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_a_copy_whose_own_name_lands_on_runs_is_denied(self):
        # rule: R-RUNS-BASH-WRITE
        # A source copied into a directory lands at <dest>/<its name>, and a
        # recursive copy merges into it.
        patched, tilde = self.home_spelled()
        name = paths.home().name
        with patched:
            for command in ("cp -r /tmp/fake/runs %s/" % tilde, "cp -r /tmp/fake/runs %s" % tilde,
                            "cp -R /tmp/fake/%s ~/" % name, "rsync -a /tmp/fake/runs %s/" % tilde,
                            "mv /tmp/fake/runs %s" % tilde):
                for agent in (None, "ppl-code-worker"):
                    label = "%s: %s" % (agent, command)
                    result = ppl_guard.decide(payload(agent, "Bash", {"command": command},
                                                      cwd="/tmp/elsewhere"))
                    self.assertIn("BOUNDARY_VIOLATION", self.run_state_denial(result, label))

    def test_the_resolved_runs_check_stays_linear_on_a_long_command(self):
        # The whole-command write-shape regex rescans the line from every `git`,
        # so it must not run on every command: 58 KB of git words took 0.6 s.
        command = "git stash drop -q stash@{0}; " * 2000
        start = time.perf_counter()
        self.assertFalse(ppl_guard.bash_write_lands_in_runs(command, "/tmp"))
        self.assertLess(time.perf_counter() - start, 0.2)

    def test_tilde_spelled_bash_write_under_the_data_root_but_outside_runs_is_allowed(self):
        patched, tilde = self.home_spelled()
        with patched:
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": "echo x > %s/notes.txt" % tilde},
                cwd="/tmp/elsewhere")))

    def test_structured_write_into_runs_through_a_symlinked_alias_is_denied(self):
        # rule: R-RUNS-WRITE-PROTECTION
        runs_root = str(paths.runs_dir())
        real = os.path.realpath(runs_root)
        if real == runs_root:
            self.skipTest("the runs directory has no symlinked alias on this machine")
        target = os.path.join(real, "DEMO-700", "state.json")
        result = ppl_guard.decide(payload(None, "Write", {"file_path": target},
                                          cwd="/tmp/elsewhere"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("run state is written only by ppl", self.deny_reason(result))

    def test_structured_write_into_runs_relative_to_the_session_cwd_is_denied(self):
        # rule: R-RUNS-WRITE-PROTECTION
        result = ppl_guard.decide(payload(None, "Write",
                                          {"file_path": "runs/DEMO-700/state.json"},
                                          cwd=str(paths.home())))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("run state is written only by ppl", self.deny_reason(result))

    # --- finding 4: cwd/repo comparison broke on a symlinked path (e.g. macOS's
    # /tmp -> /private/tmp), silently disabling every scoped check ---

    def test_symlinked_cwd_still_resolves_to_the_run_repo(self):
        real_repo = Path(tmphome.mkdtemp(prefix="pplrepo-"))
        link_parent = Path(tmphome.mkdtemp(prefix="ppllink-"))
        link_repo = link_parent / "repo"
        link_repo.symlink_to(real_repo, target_is_directory=True)
        run.init("DEMO-705", "u", str(real_repo))
        store.write_json(paths.run_dir("DEMO-705") / "boundary.json",
                         {"write": ["src/**"], "forbid": []})
        try:
            result = ppl_guard.decide(payload(
                "ppl-code-worker", "Edit",
                {"file_path": str(link_repo / "outside" / "Foo.java")},
                cwd=str(link_repo)))
            self.assertIsNotNone(result)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        finally:
            run.set_active("DEMO-700")

    # --- finding 6: minor robustness fixes folded in alongside the above ---

    def test_writing_evilcontrol_json_is_denied(self):
        # rule: R-RUNS-WRITE-PROTECTION
        # the control.json carve-out must match the filename exactly, not just a
        # suffix - "evilcontrol.json" is not "control.json". agent=None (as in
        # test_writing_control_json_is_allowed) so this exercises the runs/
        # protection in isolation: a pipeline-role agent would also be caught by
        # the downstream boundary check, masking a regression in this specific
        # exact-match fix.
        target = str(paths.run_dir("DEMO-700") / "evilcontrol.json")
        self.assertIsNotNone(ppl_guard.decide(
            payload(None, "Write", {"file_path": target})))

    def test_jira_check_does_not_raise_on_state_missing_stage_key(self):
        state = run.load("DEMO-700")
        stage_backup = state.pop("stage")
        run.save(state)
        try:
            result = ppl_guard.decide(payload(
                None, "mcp__atlassian__addCommentToJiraIssue", {"body": "x"}))
            self.assertIsNotNone(result)
        finally:
            state["stage"] = stage_backup
            run.save(state)

    # --- round 1 review, C4: a role driving its own ppl CLI through Bash
    # bypasses the registry, the lease and the done gate entirely, since none
    # of those write a path BASH_WRITE_SHAPES or the runs-root check would
    # ever see. R-GUARD-PPL-CLI-DENIED. ---

    def test_code_worker_marking_its_own_finding_fixed_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ppl findings status --finding-id F1 --status fixed "
                        "--fix-link x --generation 1"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_deleting_a_finding_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "ppl findings delete --finding-id F1 --reason done"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_recording_its_own_passing_evidence_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ppl evidence add --check build --result pass "
                        "--generation 1"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_opening_its_own_done_gate_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl transition done"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_role_recording_the_merge_to_open_the_tracker_gate_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        for agent in ("ppl-code-worker", "ppl-review-responder"):
            result = ppl_guard.decide(payload(
                agent, "Bash", {"command": "ppl merge observed --sha " + "a" * 40}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), agent)

    def test_code_worker_listing_findings_is_still_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl findings list --task DEMO-700"})))

    def test_code_worker_running_ppl_show_is_still_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl show --task DEMO-700"})))

    def test_the_deny_message_names_ppl_show_in_the_form_the_cage_allows(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        # `ppl show` takes --task, not a positional id, and the cage reads
        # `show DEMO-700` as an unknown pair. The message names the form that passes.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl transition done"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("ppl show --task <id>", self.deny_reason(result))
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl show --task DEMO-700"})))
        positional = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl show DEMO-700"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(positional))

    def test_the_data_root_named_dot_ppl_is_not_a_ppl_invocation(self):
        # ~/.ppl is the default data root, so a role listing it is ordinary
        for command in ("ls ~/.ppl", "cd ~/.ppl && ls", "ls -la ~/.ppl; echo done",
                        "ls /home/dev/.ppl | head"):
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Bash", {"command": command})), command)

    def test_a_ppl_invoked_by_path_is_still_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        # only a preceding dot is excluded: a path to the launcher is still ppl
        for command in ("bin/ppl transition done", "ppl transition done",
                        "~/antline/bin/ppl transition done", "./ppl transition done"):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)

    def test_orchestrator_with_no_role_may_still_run_ppl_transition_done(self):
        # the orchestrator (agent_type None) is not a ppl-* role and drives
        # ppl itself - this check must not reach it at all.
        self.assertIsNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "ppl transition done"})))

    # --- round 2 review: .search() found only the FIRST "ppl ..." occurrence,
    # so a read-only call in front of anything at all waved a compound command
    # through, and an unparseable ppl invocation (e.g. a reordered flag) was
    # read as "no match" and defaulted to allow instead of to deny. ---

    def test_a_readonly_call_does_not_hide_a_second_unsafe_one_after_and(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ppl show --json && ppl findings status abc fixed "
                        "--fix-link x --generation 1"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_readonly_call_does_not_hide_a_second_unsafe_one_after_semicolon(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "ppl status; ppl transition done"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_readonly_findings_list_does_not_hide_a_findings_delete(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ppl findings list && ppl findings delete abc --reason x"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_an_unparseable_ppl_invocation_is_denied_not_allowed(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        # a flag ahead of the subcommand (--task before findings) does not
        # match the expected shape at all - that must not default to allow.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ppl --task DEMO-1 findings status abc fixed"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    # --- round 3 review: a worker whose PATH lacks `ppl` may reach for
    # `python3 -m ppl` / `python3 -m ppl.cli` / a bare `ppl.cli` token with no
    # intent to evade anything - those shapes get the same read-only
    # allowlist and the same fail-closed default as the word `ppl` itself. ---

    def test_python_dash_m_ppl_cli_invocation_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "python3 -m ppl.cli findings status abc fixed"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_python_dash_m_ppl_invocation_without_dot_cli_is_denied(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "python3 -m ppl transition done"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_readonly_ppl_call_does_not_hide_a_python_dash_m_one(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ppl show && python3 -m ppl.cli transition done"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_python_dash_m_ppl_read_only_lookup_is_still_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "python3 -m ppl.cli show --task DEMO-700"})))

    def test_git_stash_tag_containing_ppl_is_still_not_a_false_positive(self):
        # the false-positive guard from round 2 - "ppl" inside an unrelated
        # token (a git stash tag) must not itself be read as an invocation.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git stash push -u -m ppl-700"})))

    # --- Task 8: read isolation for the test worker. The exact half
    # (Read/Grep/Glob carry a structured path argument) is
    # R-TEST-WORKER-READ-TOOLS, and the lexical half (Bash is a string) is
    # R-TEST-WORKER-BASH-READS. ---

    def test_test_worker_may_read_a_test_source(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Read",
            {"file_path": "/tmp/repo/source/core/orders/src/test/java/FooTest.java"})))

    def test_test_worker_may_not_read_a_main_source(self):
        # rule: R-TEST-WORKER-READ-TOOLS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Read",
            {"file_path": "/tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_with_no_path_defaults_to_cwd_and_is_denied(self):
        # rule: R-TEST-WORKER-READ-TOOLS
        # cwd is the repo root in this fixture, not a test path - an omitted
        # path must not be read as "unscoped, therefore allowed".
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Grep", {"pattern": "TODO"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_scoped_to_a_test_directory_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Grep",
            {"pattern": "AC-1", "path": "/tmp/repo/source/core/orders/src/test"})))

    def test_test_worker_relative_read_path_resolves_against_the_payload_cwd(self):
        # rule: R-TEST-WORKER-READ-TOOLS
        # The hook process's own cwd is not the session's: a relative path is
        # the session's, resolved against the payload cwd.
        self.assertNotEqual(os.path.realpath(os.getcwd()), os.path.realpath("/tmp/repo"))
        for tool, tool_input in (("Grep", {"pattern": "x", "path": "source/core/orders"}),
                                 ("Glob", {"pattern": "*.java", "path": "source/core/orders/src/main"}),
                                 ("Read", {"file_path": "source/core/orders/Foo.java"})):
            result = ppl_guard.decide(payload("ppl-test-worker", tool, tool_input, cwd="/tmp/repo"))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), tool)

    def test_test_worker_relative_read_of_a_test_path_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Grep", {"pattern": "AC-1", "path": "source/core/orders/src/test"},
            cwd="/tmp/repo")))

    def test_test_worker_relative_read_path_with_no_cwd_is_denied(self):
        # rule: R-TEST-WORKER-READ-TOOLS
        tool_call = payload("ppl-test-worker", "Read", {"file_path": "source/core/orders/Foo.java"})
        del tool_call["cwd"]
        result = ppl_guard.decide(tool_call)
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_glob_over_test_sources_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Glob",
            {"pattern": "**/*Test.java",
             "path": "/tmp/repo/source/core/orders/src/test"})))

    def test_test_worker_reading_the_brief_outside_the_repo_is_allowed(self):
        target = str(paths.run_dir("DEMO-700") / "brief.json")
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Read", {"file_path": target})))

    def test_test_worker_cat_of_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "cat /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_cat_of_a_test_source_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "cat /tmp/repo/source/core/orders/src/test/java/FooTest.java"})))

    def test_test_worker_git_diff_with_no_pathspec_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # an unrestricted diff can surface any changed file, main sources
        # included - "no pathspec" is not "no risk".
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "git diff"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_git_show_of_a_main_source_object_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git show HEAD:source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_git_diff_is_denied_even_restricted_to_a_test_path(self):
        # rule: R-TEST-WORKER-BASH-READS
        # round 1 review, allowlist rewrite: git diff/show are not in the
        # derived allowlist at all (the role has no documented need for
        # either), so this denies unconditionally now, not only when
        # unrestricted.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git diff -- source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_of_main_source_via_bash_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "grep -rn TODO /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_dash_n_of_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # "-n" means something different per verb (a line count on head/tail,
        # a flag with no value at all on grep/rg's own "-n" for line numbers)
        # - a shared value-flag set would misread "TODO" here as -n's value
        # and let the real path fall through as grep's own pattern instead.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "grep -n TODO /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_sed_dash_n_of_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "sed -n '10,20p' /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_dash_e_of_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # "-e" supplies grep's pattern as a flag value instead of
        # positionally - the file argument must still be checked as a path.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "grep -e TODO /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_git_diff_with_a_global_flag_before_the_subcommand_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # a global git flag (-C <dir>) sits between "git" and "diff" - the
        # gates recipe itself uses "git -C <worktree> diff" verbatim.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "git -C /tmp/repo diff"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_head_dash_n_of_a_test_source_is_still_allowed(self):
        # regression guard: fixing grep's per-verb value-flags must not break
        # head/tail's own "-n <count>" reading a test source.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "head -n 20 /tmp/repo/source/core/orders/"
                        "src/test/java/FooTest.java"})))

    # --- Round 1 review, allowlist rewrite: a blocklist can never satisfy
    # the design's "any command shape that cannot be classified is denied", since
    # an unrecognised command is by construction unclassified and therefore
    # allowed. R-TEST-WORKER-BASH-READS is now an allowlist of the shapes the
    # role is derived to need - everything else, including a shape this
    # cannot parse, denies. ---

    def test_test_worker_mvn_install_shape_from_gates_recipe_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn install -pl :cat-service -am -DskipTests "
                        "-Dmaven.gitcommitid.skip=true"})))

    def test_test_worker_mvn_checkstyle_shape_from_gates_recipe_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn -pl :cat-service -am "
                        "-Dmaven.gitcommitid.skip=true checkstyle:check"})))

    def test_test_worker_mvn_verify_shape_from_gates_recipe_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn -pl :cat-service -am "
                        "-Dmaven.gitcommitid.skip=true verify"})))

    def test_test_worker_mvn_with_no_recognised_goal_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # -pl and -am alone, with an arbitrary -D property (e.g. one that
        # could invoke a plugin goal like exec:exec), must not pass just
        # because it looks like an mvn invocation - exactly one of the four
        # derived goals must be present.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn -pl :cat-service -am -Dexec.mainClass=Foo"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_shape_matcher_reproduces_the_maven_allowlist(self):
        # rule: R-GUARD-SHAPES-FROM-SNAPSHOT
        matcher = ppl_guard.ShapeMatcher.from_rows(
            harness.shape_rows(harness.load("example-maven")))
        self.assertTrue(matcher.allows(["mvn", "-pl", ":m", "-am", "test"]))
        self.assertTrue(matcher.allows(["JAVA_HOME=/opt/jdk", "mvn", "install", "-pl", ":m",
                                        "-am", "-DskipTests"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m", "-am"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m", "test"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", "../x", "-am", "test"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m", "-am", "test", "exec:java"]))
        self.assertFalse(matcher.allows(["gradle", "test"]))

    def test_generic_harness_shapes_allow_make_and_nothing_else(self):
        # rule: R-GUARD-SHAPES-FROM-SNAPSHOT
        fixtures.install_harness("example-generic")
        matcher = ppl_guard.ShapeMatcher.from_rows(
            harness.shape_rows(harness.load("example-generic")))
        self.assertTrue(matcher.allows(["make", "test", "TEST=FooTest"]))
        self.assertFalse(matcher.allows(["make", "deploy"]))
        self.assertFalse(matcher.allows(["make", "test", "TEST=$(cat x)"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m", "-am", "test"]))

    def test_a_shape_with_no_constraints_still_requires_argv0(self):
        matcher = ppl_guard.ShapeMatcher.from_rows(
            [{"argv0": "make", "env_prefix": False, "required_flags": [], "one_goal_of": [],
              "free_args": ""}])
        self.assertTrue(matcher.allows(["make"]))
        self.assertFalse(matcher.allows(["make", "anything"]))
        self.assertFalse(matcher.allows(["mak"]))

    def test_an_unanchored_value_regex_matches_the_whole_token_not_a_prefix(self):
        # rule: R-GUARD-SHAPES-FROM-SNAPSHOT
        matcher = ppl_guard.ShapeMatcher.from_rows(
            [{"argv0": "mvn", "env_prefix": False, "required_flags": [["-pl", ":[a-z]+"]],
              "one_goal_of": [], "free_args": "-D[a-z]+"}])
        self.assertTrue(matcher.allows(["mvn", "-pl", ":m", "-Dfoo"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m", "-Dfoo=bar"]))
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m/../x", "-Dfoo"]))

    def test_an_env_prefix_is_accepted_only_where_the_shape_declares_it(self):
        matcher = ppl_guard.ShapeMatcher.from_rows(
            [{"argv0": "make", "env_prefix": False, "required_flags": [],
              "one_goal_of": ["test"], "free_args": ""}])
        self.assertTrue(matcher.allows(["make", "test"]))
        self.assertFalse(matcher.allows(["CC=gcc", "make", "test"]))

    def test_test_worker_build_is_denied_without_a_snapshot(self):
        # rule: R-GUARD-SNAPSHOT-FAIL-CLOSED
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        snap.unlink()
        try:
            result = ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                              {"command": "mvn -pl :m -am test"}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
            self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                       {"command": "git status"})))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_test_worker_build_is_denied_on_a_corrupt_snapshot(self):
        # rule: R-GUARD-SNAPSHOT-FAIL-CLOSED
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        snap.write_text("{not json", encoding="utf-8")
        try:
            result = ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                              {"command": "mvn -pl :m -am test"}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
            # the test globs come from the same file, so the deny names it
            self.assertIn("runs/DEMO-700/harness.json", self.deny_reason(result))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_malformed_snapshot_shapes_deny_the_build_instead_of_raising(self):
        # rule: R-GUARD-SNAPSHOT-FAIL-CLOSED
        # The hook swallows an exception and allows, so a shape row that
        # crashed the matcher would turn a hand-edited snapshot into an allow
        # for every test-worker Bash call, a main-source read included.
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        nested = "(" * 3000 + "a" + ")" * 3000
        malformed = [
            5,
            "mvn",
            ["mvn"],
            [{"argv0": "mvn", "one_goal_of": 5}],
            [{"argv0": "mvn", "one_goal_of": [["test"]]}],
            [{"argv0": "mvn", "required_flags": [[["-pl"], ""]], "one_goal_of": ["test"]}],
            [{"argv0": "mvn", "required_flags": [["-pl"]], "one_goal_of": ["test"]}],
            [{"argv0": "mvn", "required_flags": [["-pl", "("]], "one_goal_of": ["test"]}],
            [{"argv0": "mvn", "required_flags": [["-pl", "a{4294967296}"]],
              "one_goal_of": ["test"]}],
            [{"argv0": "mvn", "one_goal_of": ["test"], "free_args": nested}],
            [{"argv0": "mvn", "one_goal_of": ["test"], "free_args": 7}],
            [{"argv0": ["mvn"], "one_goal_of": ["test"]}],
        ]
        documents = ([json.dumps({"format": 1, "shapes": shapes}) for shapes in malformed]
                     + ["[" * 100000 + "]" * 100000])
        commands = ("mvn -pl :m -am test",
                    "cat /tmp/repo/source/core/orders/src/main/java/Foo.java")
        try:
            for text in documents:
                snap.write_text(text, encoding="utf-8")
                for command in commands:
                    with self.subTest(snapshot=text[:80], command=command):
                        result = ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                          {"command": command}))
                        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_a_token_longer_than_the_cap_is_denied_before_any_regex_runs(self):
        # rule: R-GUARD-SHAPES-FROM-SNAPSHOT
        matcher = ppl_guard.ShapeMatcher.from_rows(
            harness.shape_rows(harness.load("example-maven")))
        self.assertTrue(matcher.allows(["mvn", "-pl", ":m", "-am", "test", "-D" + "x" * 100]))
        long_token = "-D" + "x" * 4998
        self.assertEqual(len(long_token), 5000)
        self.assertFalse(matcher.allows(["mvn", "-pl", ":m", "-am", "test", long_token]))

    def test_test_worker_git_status_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "git status"})))

    def test_test_worker_git_add_of_a_test_path_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git add /tmp/repo/source/core/orders/"
                        "src/test/java/FooTest.java"})))

    def test_test_worker_git_add_of_a_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git add /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_git_commit_with_a_message_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "git commit -m 'add tests'"})))

    def test_test_worker_ls_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "ls -la source"})))

    def test_test_worker_command_substitution_denies_the_whole_command(self):
        # rule: R-TEST-WORKER-BASH-READS
        # the one construct that could smuggle an unrelated read inside an
        # otherwise-allowed shape (here, a commit message).
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git commit -m \"$(cat /tmp/repo/source/core/orders/Foo.java)\""}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_process_substitution_inside_a_free_arg_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # ${IFS} keeps the whole thing one shlex token that the -D free_args
        # regex accepts, and bash still runs the <( ) or >( ) mid-word.
        for command in ("mvn -pl :m -am test -Dx=<(cp${IFS}src/main/java/Foo.java"
                        "${IFS}src/test/java/Leak.java)",
                        "mvn -pl :m -am test -Dx=>(cp${IFS}src/main/java/Foo.java"
                        "${IFS}src/test/java/Leak.java)"):
            with self.subTest(command=command):
                result = ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                  {"command": command}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    # --- The coordinator's own 18-attempt probe against the blocklist
    # version, round 1 review. All of these must now deny. ---

    def test_probe_nl_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "nl /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_tac_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "tac /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_cut_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "cut -d' ' -f1 /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_od_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "od -c /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_xxd_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "xxd /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_strings_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "strings /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_more_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "more /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_diff_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "diff /tmp/repo/source/core/orders/Foo.java /dev/null"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_git_log_dash_p_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "git log -p"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_git_blame_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git blame /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_git_cat_file_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "git cat-file -p HEAD:source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_python_dash_c_reading_a_file_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "python3 -c \"print(open('source/Main.java').read())\""}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_probe_while_read_redirected_from_a_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "while read l; do echo $l; done < "
                        "/tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_with_no_file_argument_reads_a_pipe_and_is_allowed(self):
        # no file argument at all means grep is filtering piped stdin, not
        # reading a path - a definite classification, not an unclassifiable
        # one, so this is not covered by the conservative-deny default.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "echo hello | grep hello"})))

    def test_test_worker_bash_c_wrapper_around_cat_of_main_source_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # the literal word "cat" survives inside the wrapper's quoted string -
        # same doctrine as R-GUARD-PPL-CLI-DENIED's bash -c case in Task 6.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "bash -c 'cat /tmp/repo/source/core/orders/Foo.java'"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_mvn_build_and_test_commands_are_unaffected(self):
        # the gates recipe's real command shapes must keep working - none of
        # them is one of the watched read verbs, and the log/rc paths are
        # outside the repo, matching the recipe's own mktemp -d convention.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "JAVA_HOME=/opt/jdk mvn -pl :cat-service -am "
                        "-Dtest=FooTest -DfailIfNoTests=false "
                        "-Dmaven.gitcommitid.skip=true test "
                        "> /tmp/run/focused.log 2>&1; echo $? > /tmp/run/focused.rc"})))

    def test_test_worker_artifact_id_containing_a_verb_word_is_not_a_false_positive(self):
        # "cat" sits inside "cat-service" with no word boundary after it -
        # must not be misread as the cat command, same trap as the
        # "ppl-700" stash tag in R-GUARD-PPL-CLI-DENIED.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn -pl :cat-service -am test"})))

    def test_test_worker_cd_then_climb_past_the_test_dir_via_dotdot_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # "cd" is not one of the allowed shapes at all (the derived allowlist
        # never needs it - the gates recipe uses "-pl :module", not cd), so
        # the first segment already denies the whole command, so no
        # cwd-tracking is needed to catch the climb through ".." that would
        # follow it.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "cd source/core/orders/src/test && cat ../Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_brace_expansion_in_a_read_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # shlex keeps the brace as one token that matches **/src/test/**, and
        # the shell expands it into a read of src/main.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "cat src/test/{x,../main/Foo.java}"}))
        reason = self.deny_reason(result)
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("shell expansion", reason)

    def test_code_worker_reading_a_test_source_is_not_restricted(self):
        # mirror decision, recorded in DESIGN as R-CODE-WORKER-READS-UNRESTRICTED:
        # the code worker is NOT denied reads of test sources.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Read",
            {"file_path": "/tmp/repo/source/core/orders/src/test/java/FooTest.java"})))

    def test_code_worker_cat_of_a_main_source_is_not_restricted_by_this_check(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "cat /tmp/repo/source/core/orders/Foo.java"})))

    # --- C1 (whole-branch review): the write boundary was enforced only for
    # Edit/Write/MultiEdit/NotebookEdit, and both workers hold Bash.
    # R-GUARD-BASH-WRITE-BOUNDARY closes it by extracting the target out of
    # every write-shaped Bash construct and running it through the same
    # per-role decision boundary.check_path already makes for a structured
    # write. ---

    def test_code_worker_bash_redirect_into_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # the exact C1 repro: identical to test_code_worker_may_not_edit_a_test
        # above, but through Bash instead of Write.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x > /tmp/repo/source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_bash_redirect_into_a_main_source_is_denied_absolute(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-TEST-WORKER-MAIN
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "echo pwned > /tmp/repo/source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_bash_redirect_into_a_main_source_is_denied_relative(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-TEST-WORKER-MAIN
        # the relative-path variant: the target is resolved against the
        # session's cwd, not merely matched as a literal string.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "echo pwned > source/core/orders/Foo.java"},
            cwd="/tmp/repo"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_unroled_bash_redirect_into_the_run_repo_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-UNROLED-REPO-WRITE
        # the orchestrator's Bash write boundary too - cwd is deliberately
        # outside the repo, matching R-UNROLED-REPO-WRITE's own "the target
        # path is the honest question, not the cwd" doctrine.
        result = ppl_guard.decide(payload(
            None, "Bash",
            {"command": "echo x > /tmp/repo/source/core/orders/Foo.java"},
            cwd="/home/dev/some/other/project"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_redirect_inside_boundary_is_still_allowed(self):
        # the recipe workers actually use must keep working.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x > source/core/orders/Foo.java"},
            cwd="/tmp/repo")))

    def test_test_worker_bash_redirect_into_a_test_source_is_still_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "echo x > source/core/orders/src/test/java/FooTest.java"},
            cwd="/tmp/repo")))

    def test_code_worker_bash_write_outside_the_repo_is_unaffected(self):
        # a role's Bash legitimately writes build logs and rc files outside
        # the repo (references/gates.md's own recipe) - only a target that
        # resolves INSIDE the repo is this check's business.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "mvn -pl :m -am test > /tmp/build.log 2>&1; "
                        "echo $? > /tmp/build.rc"},
            cwd="/tmp/repo")))

    def test_code_worker_cp_into_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # cp is not a redirect - the same boundary decision must still apply.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "cp /tmp/x.java source/core/orders/src/test/java/FooTest.java"},
            cwd="/tmp/repo"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_rm_outside_the_repo_is_unaffected(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "rm /tmp/scratch.txt"},
            cwd="/tmp/repo")))

    def test_code_worker_ambiguous_sed_inplace_on_a_repo_path_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # GNU's optional attached suffix and BSD's mandatory separate one
        # cannot be told apart lexically - fail closed rather than guess.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "sed -i 's/a/b/' source/core/orders/Foo.java"},
            cwd="/tmp/repo"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_command_substitution_write_target_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # a command substitution could be hiding the real target of a
        # write-shaped construct - fail closed rather than skip the check.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "cp $(pwd)/x.java source/core/orders/Foo.java"},
            cwd="/tmp/repo"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_unrelated_session_ambiguous_write_elsewhere_is_not_denied(self):
        # the fail-closed direction above must not leak into other projects:
        # an ordinary session with no ppl-* role, working outside any run's
        # repo, must not be denied a sed -i just because some run happens to
        # be active on this machine.
        self.assertIsNone(ppl_guard.decide(payload(
            None, "Bash", {"command": "sed -i 's/a/b/' notes.txt"},
            cwd="/home/dev/some/other/project")))

    def test_code_worker_bash_write_to_a_forbidden_path_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-FORBIDDEN-PATHS
        run.init("DEMO-707", "u", "/tmp/repo")
        store.write_json(paths.run_dir("DEMO-707") / "boundary.json",
                         {"write": ["source/**"], "forbid": ["source/secrets/**"]})
        try:
            result = ppl_guard.decide(payload(
                "ppl-code-worker", "Bash",
                {"command": "echo x > source/secrets/key.pem"}, cwd="/tmp/repo"))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        finally:
            run.set_active("DEMO-700")

    def test_code_worker_bash_write_outside_the_write_boundary_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-BOUNDARY
        # a path that is neither a test source nor forbidden, but simply does
        # not match boundary.write ("source/core/orders/**"), through Bash.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x > source/some-other-module/Foo.java"},
            cwd="/tmp/repo"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    # --- Round 2 (whole-branch review, wave A): touch/truncate/ln/install
    # were missing from BASH_WRITE_SHAPES. Probed against ppl-code-worker,
    # which carries no read allowlist to accidentally cover them the way
    # R-TEST-WORKER-BASH-READS did for ppl-test-worker. ---

    def test_code_worker_bash_touch_of_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "touch source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_truncate_of_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # truncate -s 0 destroys a file's content in place - a real write,
        # not a metadata touch.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "truncate -s 0 source/core/orders/"
                        "src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_ln_replacing_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # ln -sf <target> <linkname> writes linkname, not target - denying
        # the source argument instead would be a false deny that looks like
        # a pass, so this pins the destination specifically: /etc/passwd
        # (the symlink's target) is not what gets checked or denied here.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ln -sf /etc/passwd source/core/orders/"
                        "src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_install_into_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # install writes its LAST argument, like cp/mv - /dev/null here is
        # the source, not the checked path.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "install -m 644 /dev/null source/core/orders/"
                        "src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_touch_inside_boundary_is_still_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "touch source/core/orders/Foo.java"})))

    def test_code_worker_bash_ln_with_a_single_argument_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # a single positional argument leaves the real linkname implicit
        # (cwd + basename(target)) - fail closed rather than guess it.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "ln -s source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    # --- C2 (whole-branch review): _read_verb_allowed treated "no path
    # operand" as "filtering a pipe" for every read verb, but rg is
    # recursive by default with no path at all, and grep -r/-R defaults to
    # the cwd - neither reads stdin, piped or not. ---

    def test_test_worker_rg_with_no_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "rg 'class Main'"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_rg_with_a_flag_and_no_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "rg -n pwn"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_dash_r_with_no_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "grep -r pwn"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_bundled_dash_rn_with_no_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # "-rn" bundles "-r" with "-n" - "-r" is not only ever standalone.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "grep -rn pwn"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_piped_grep_bundled_dash_rn_is_still_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # the discriminating case for bundled-flag detection: with no pipe,
        # an unrecognised recursive flag would already deny for the OTHER
        # reason ("not fed by a pipe"), so a command with no pipe cannot
        # prove the bundling check does anything. Feeding it through a real
        # pipe removes that confound - only recognising "-r" inside "-rn"
        # stops this from being misread as a genuine pipe filter.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "echo x | grep -rn pwn"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_dash_capital_r_with_no_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "grep -R pwn"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_grep_long_include_and_dash_r_with_no_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "grep --include=*.java -r class"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_test_worker_genuine_pipe_filter_with_no_path_is_still_allowed(self):
        # regression guard: the fix must not touch the actual pipe-filter
        # case the original allow test was built on.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "echo hello | grep hello"})))

    def test_test_worker_rg_with_an_explicit_test_path_is_still_allowed(self):
        # regression guard: an explicit path operand still goes through the
        # ordinary path-scoping check, unaffected by the recursive-flag fix.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "rg TODO /tmp/repo/source/core/orders/"
                        "src/test/java/FooTest.java"})))

    # --- Round 3 (post-branch review, finding 1): bash's `&>` / `&>>` (stdout
    # and stderr into one file) and the csh-style `>&` were not shapes
    # the redirect extraction knew, so `echo x &> <test source>` extracted
    # no target and walked past the write boundary for every role and for an
    # unroled session - an ordinary idiom, the same class as the `2>&1` the
    # extractor already handled. `git rm/mv/checkout/restore/apply`, `rsync`
    # and `patch` write tree paths with no redirect at all and were not shapes
    # either. The done gate does not backstop the code-worker/tests rule (test
    # paths are exempt from its boundary check, because the test worker
    # legitimately writes them), so the hook is the only line here. ---

    def test_code_worker_bash_ampersand_redirect_into_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x &> source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_ampersand_append_into_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x &>> source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_glued_ampersand_redirect_into_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x &>source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_csh_redirect_into_a_test_source_names_the_real_target(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # `>& file` used to deny by accident with "&" as the checked path - a
        # false deny that read as coverage. The file is the target.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x >& source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("FooTest.java", self.deny_reason(result))

    def test_test_worker_bash_ampersand_redirect_into_a_main_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-TEST-WORKER-MAIN
        # echo itself is on the test worker's read allowlist, so only the
        # write-boundary half stands between this and a main source.
        result = ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "echo pwned &> source/core/orders/Foo.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_unroled_bash_ampersand_redirect_into_the_run_repo_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-UNROLED-REPO-WRITE
        result = ppl_guard.decide(payload(
            None, "Bash", {"command": "echo x &> /tmp/repo/source/core/orders/Foo.java"},
            cwd="/home/dev/some/other/project"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_bash_ampersand_redirect_of_build_output_is_still_allowed(self):
        # `&>` is exactly what a worker types to capture a build log, and a
        # target inside the write boundary is still inside it.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "mvn -pl :m -am test &> /tmp/build.log; echo $? > /tmp/build.rc"})))
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "echo x &> source/core/orders/Foo.java"})))

    def test_test_worker_mvn_with_ampersand_redirect_is_still_allowed(self):
        # the read allowlist strips `&> file` like `> file 2>&1` before classifying
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn -pl :m -am -Dtest=FooTest test &> /tmp/focused.log; "
                        "echo $? > /tmp/focused.rc"})))

    def test_test_worker_and_responder_read_a_clobber_redirect_like_a_plain_one(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        # `>|` is stripped like `>` before the test worker's shape is matched,
        # and the read-only responder may send it nowhere but /dev/null
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash",
            {"command": "mvn -pl :m -am -Dtest=FooTest test >| /tmp/focused.log 2>&1; "
                        "echo $? >| /tmp/focused.rc"})))
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        doc["forge"] = {"kind": "github"}
        snap.write_text(json.dumps(doc), encoding="utf-8")
        try:
            for command in (RESPONDER_DIFF + " >| /tmp/x", RESPONDER_DIFF + " >|/tmp/x"):
                result = ppl_guard.decide(payload("ppl-review-responder", "Bash",
                                                  {"command": command}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-review-responder", "Bash", {"command": RESPONDER_DIFF + " >| /dev/null"})))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_code_worker_moving_a_test_source_out_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # a moved source is removed from where it was
        test_source = "source/core/orders/src/test/java/FooTest.java"
        for command in ("mv %s /tmp/x" % test_source,
                        "git mv %s source/core/orders/Bar.java" % test_source):
            result = ppl_guard.decide(payload("ppl-code-worker", "Bash", {"command": command}))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            self.assertIn("test", self.deny_reason(result), command)

    def test_moving_a_main_source_or_a_file_outside_the_repo_is_allowed(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git mv source/core/orders/src/main/java/Foo.java "
                        "source/core/orders/src/main/java/Bar.java"})))
        for agent, cwd in ((None, "/tmp/elsewhere"), (None, "/tmp/repo"),
                           ("ppl-code-worker", "/tmp/repo")):
            self.assertIsNone(ppl_guard.decide(payload(
                agent, "Bash", {"command": "mv /tmp/a /tmp/b"}, cwd=cwd)), (agent, cwd))

    def test_code_worker_bash_tilde_redirect_resolves_to_home_not_cwd(self):
        # `~/x` is $HOME/x - resolving it against cwd used to land it inside
        # the repo and deny a write that is outside it.
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "echo x > ~/ppl-notes.txt"})))

    def test_code_worker_git_checkout_of_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # `git checkout <ref> -- <path>` overwrites exactly that path from history
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git checkout origin/master -- "
                        "source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_git_restore_of_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git restore --source=HEAD~1 "
                        "source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_git_rm_of_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git rm -q source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_git_mv_onto_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # like mv/cp/install: the LAST positional is what gets written
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git mv source/core/orders/Foo.java "
                        "source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_git_dash_c_relocates_the_rm_target(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        # `git -C <dir>` makes every path operand relative to <dir>, from any cwd
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "git -C /tmp/repo/source rm core/orders/src/test/java/FooTest.java"},
            cwd="/tmp/somewhere-else"))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_git_apply_is_denied_as_unresolvable(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # the paths a diff writes are inside the diff, not on the command
        # line - nothing lexical can name them, so a role's `git apply` fails
        # closed like any other target this cannot pin down.
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git apply /tmp/evil.diff"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("could not be resolved", self.deny_reason(result))

    def test_code_worker_git_checkout_of_a_ref_is_denied_as_unresolvable(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        # without `--` the operand may be a ref, and switching refs rewrites
        # the whole tree
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "git checkout origin/master"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_rsync_onto_a_test_source_is_denied(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY / R-ROLE-CODE-WORKER-TESTS
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash",
            {"command": "rsync -a /tmp/x.java source/core/orders/src/test/java/FooTest.java"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_patch_is_denied_as_unresolvable(self):
        # rule: R-GUARD-BASH-WRITE-BOUNDARY
        result = ppl_guard.decide(payload(
            "ppl-code-worker", "Bash", {"command": "patch -p1 < /tmp/evil.diff"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_code_worker_git_add_commit_and_read_subcommands_are_still_allowed(self):
        # the git shapes references/gates.md and the worker templates send a
        # role to, plus a checkout of a path INSIDE the boundary
        for command in ("git add source/core/orders/Foo.java && git commit -m 'x'",
                        "git status --short; git diff; git log --oneline -3",
                        "git stash push -u -m ppl-700 && git stash apply stash@{0}",
                        "git apply --check /tmp/x.diff",
                        "git checkout -- source/core/orders/Foo.java"):
            with self.subTest(command=command):
                self.assertIsNone(ppl_guard.decide(payload(
                    "ppl-code-worker", "Bash", {"command": command})))

    def test_orchestrator_stage_one_worktree_setup_is_still_allowed(self):
        # SKILL.md Stage 1, verbatim shape: run from the MAIN checkout, beside
        # the run's repo. `git worktree add` is not a shape this parses at all,
        # and nothing here may start denying the documented setup.
        self.assertIsNone(ppl_guard.decide(payload(
            None, "Bash",
            {"command": "cd /home/dev/src/project && git fetch origin master "
                        "&& git worktree add ../project-DEMO-700 -b DEMO-700-slug "
                        "origin/master"},
            cwd="/home/dev/ppl-workspace")))

    # --- Round 3 (post-branch review, finding 3): references/gates.md's
    # recipe redirected every log to "$RUN/...", and the write-boundary check
    # fails closed on a `$VAR` target - so the recipe the mvn allowlist was
    # derived from was itself refused for any role, and for the orchestrator
    # whenever its cwd sat inside the worktree. The recipe now uses literal
    # paths, and this test feeds the whole fenced block of the rendered
    # references/gates.md, the harness gates in their log-and-rc shape
    # included, through the hook so the two cannot drift apart again. ---

    def test_the_documented_gates_recipe_is_allowed_where_the_orchestrator_runs_it(self):
        import re
        rendered = fixtures.render_skill_to(tmphome.mkdtemp(prefix="pplskill-"), "example-maven")
        recipe = (rendered / "references" / "gates.md").read_text(encoding="utf-8")
        block = recipe.split("```bash", 1)[1].split("```", 1)[0]
        commands, current = [], ""
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            continued = line.rstrip().endswith("\\")
            current += line.rstrip().rstrip("\\") + " "
            if continued:
                continue
            commands.append(current.strip())
            current = ""
        self.assertGreaterEqual(len(commands), 12, commands)
        substituted = []
        for command in commands:
            command = re.sub(r"<pass\|fail from [^>]*>", "pass", command)
            for placeholder, value in (("<worktree>", "/tmp/repo"), ("<module>", "m"),
                                       ("<test_class>", "FooTest"), ("<TASK-ID>", "DEMO-700"),
                                       ("<SHA>", "abc123"), ("<GENERATION>", "1"),
                                       ("<default-branch>", "master")):
                command = command.replace(placeholder, value)
            self.assertIsNone(re.search(r"<[A-Za-z]", command),
                              "unknown placeholder in the recipe: %s" % command)
            substituted.append(command)
        argv0s = {row["argv0"] for row in harness.shape_rows(harness.load("example-maven"))}
        checked = 0
        for command in substituted:
            # the orchestrator: no role, from beside the worktree (the
            # documented layout) AND from inside it
            for cwd in ("/home/dev/ppl-workspace", "/tmp/repo"):
                with self.subTest(command=command, cwd=cwd):
                    self.assertIsNone(ppl_guard.decide(payload(
                        None, "Bash", {"command": command}, cwd=cwd)))
            # the test worker runs the build/test lines itself. The renderer
            # puts the harness [env] in front of every gate line.
            bare = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+", "", command)
            if bare.split()[0] in argv0s:
                checked += 1
                with self.subTest(command=command, role="ppl-test-worker"):
                    self.assertIsNone(ppl_guard.decide(payload(
                        "ppl-test-worker", "Bash", {"command": command}, cwd="/tmp/repo")))
        self.assertGreaterEqual(checked, 4, substituted)

    # --- Forge denials come from the adapter the run's harness snapshot names. ---

    def test_resolve_merge_and_approve_are_denied_under_the_snapshot_server_name(self):
        # rule: R-FORGE-DENIALS-FROM-ADAPTER, R-MCP-RESOLVE-THREAD, R-MCP-MERGE-MR, R-MCP-APPROVE-MR
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        doc["forge"] = {"kind": "gitlab", "mcp_server": "Corp-GitLab"}
        snap.write_text(json.dumps(doc), encoding="utf-8")
        try:
            for tool in ("mcp__Corp-GitLab__resolve_merge_request_thread",
                         "mcp__Corp-GitLab__merge_merge_request",
                         "mcp__Corp-GitLab__approve_merge_request"):
                result = ppl_guard.decide(payload(None, tool, {}, cwd="/tmp/repo"))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), tool)
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_without_a_run_both_adapters_denials_apply(self):
        # rule: R-FORGE-DENIALS-FROM-ADAPTER
        active = paths.active_file()
        saved = active.read_text(encoding="utf-8")
        active.unlink()
        try:
            for tool in ("mcp__GitLab-MCP__merge_merge_request", "mcp__github__merge_pull_request"):
                result = ppl_guard.decide(payload(None, tool, {}, cwd="/elsewhere"))
                self.assertIsNotNone(result, tool)
        finally:
            active.write_text(saved, encoding="utf-8")

    def test_github_merge_approve_resolve_bash_are_denied_for_everyone(self):
        # rule: R-FORGE-GH-NO-APPROVE-MERGE
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        doc["forge"] = {"kind": "github"}
        snap.write_text(json.dumps(doc), encoding="utf-8")
        try:
            for command, why in (
                    ("gh pr merge 7", "a human performs the merge"),
                    ("gh pr review 7 --approve", "never approves its own pull request"),
                    ("gh api graphql -f query='mutation{resolveReviewThread(input:{})}'",
                     "only the remark author may resolve a thread")):
                for agent in (None, "ppl-review-responder"):
                    reason = self.deny_reason(ppl_guard.decide(
                        payload(agent, "Bash", {"command": command})))
                    self.assertIn("BOUNDARY_VIOLATION", reason, command)
                    self.assertIn(why, reason, command)
            self.assertIsNone(ppl_guard.decide(payload(None, "Bash", {"command": "gh pr view 7"})))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_gh_merge_is_denied_under_a_gitlab_run_too(self):
        # rule: R-FORGE-GH-NO-APPROVE-MERGE
        # The maven fixture's snapshot names the gitlab forge, whose adapter
        # denies no Bash at all. The union still applies.
        snapshot = ppl_guard.load_snapshot("DEMO-700")
        self.assertEqual(snapshot["forge"]["kind"], "gitlab")
        for cwd in ("/tmp/repo", "/elsewhere"):
            result = ppl_guard.decide(payload(None, "Bash", {"command": "gh pr merge 7"}, cwd=cwd))
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), cwd)

    def _responder_with_github_forge(self):
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        doc["forge"] = {"kind": "github"}
        snap.write_text(json.dumps(doc), encoding="utf-8")
        self.addCleanup(snap.write_text, saved, encoding="utf-8")

    def _responder_bash(self, command):
        return ppl_guard.decide(payload("ppl-review-responder", "Bash", {"command": command}))

    def test_the_responder_may_git_diff_two_shas(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        for command in ("git -C /work/repo diff --no-ext-diff --no-textconv abc1234..def5678",
                        "git -C /work/repo diff --no-ext-diff --no-textconv "
                        "origin/main..0123456789abcdef0123456789abcdef01234567",
                        "git -C /work/repo log --oneline abc1234..origin/feature-x"):
            self.assertIsNone(self._responder_bash(command), command)

    def test_the_responder_may_git_show_a_sha(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        self.assertIsNone(self._responder_bash(
            "git -C /work/repo show --no-ext-diff --no-textconv abc1234"))

    def test_the_responder_may_not_run_gh_pr_view(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        for command in ("gh pr view 7 --json=reviews,headRefOid", "gh pr diff 7",
                        "gh pr checks 7 --repo=acme/widgets",
                        "gh api repos/acme/widgets/pulls/7/comments --paginate"):
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self._responder_bash(command)),
                          command)

    def test_the_responder_may_not_git_push(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        for command in ("git -C /work/repo push origin abc1234", "git push",
                        "git -C /work/repo fetch origin", "git -C /work/repo checkout abc1234",
                        "git -C /work/repo status", "git -C /work/repo diff abc1234..def5678"):
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self._responder_bash(command)),
                          command)

    def test_the_responder_may_not_pass_dash_c_to_git(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        base = "-C /work/repo diff --no-ext-diff --no-textconv abc1234..def5678"
        for command in ("git -c core.pager=x " + base, "git -c diff.external=x " + base,
                        "git " + base + " --output=/tmp/x",
                        "git " + base + " --ext-diff", "git " + base + " --textconv",
                        "git -C /work/repo diff abc1234..def5678 --no-ext-diff",
                        "git -C ../repo diff --no-ext-diff --no-textconv abc1234..def5678",
                        "git -C /work/repo show --no-ext-diff --no-textconv abc123",
                        "git -C /work/repo show --no-ext-diff --no-textconv --output=x abc1234",
                        "git -C /work/repo show --no-ext-diff --no-textconv -O/tmp/x abc1234",
                        "git -C /work/repo log --oneline -p abc1234..def5678",
                        "git -C /work/repo log abc1234..def5678",
                        "git -C /work/repo diff --no-ext-diff --no-textconv HEAD..main",
                        "git -C /work/repo diff --no-ext-diff --no-textconv origin/-x..abc1234"):
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self._responder_bash(command)),
                          command)

    def test_a_redos_shaped_dash_c_value_is_denied_fast(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        rest = " diff --no-ext-diff --no-textconv abc1234..def5678"
        for value in ("/" + "a" * 4000 + "!", "/" + "a/" * 1500 + "!", "/" + "a" * 4000 + "/..",
                      "/" + "a" * 30 + "!"):
            start = time.monotonic()
            result = self._responder_bash("git -C " + value + rest)
            self.assertLess(time.monotonic() - start, 0.05)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_normal_absolute_repo_path_is_still_allowed(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        for path in ("/", "/work/repo", "/Users/a.b/my-repo_1/", "/w/.worktrees/x@1"):
            self.assertIsNone(self._responder_bash(
                "git -C %s diff --no-ext-diff --no-textconv abc1234..def5678" % path), path)

    def test_github_review_responder_bash_is_an_allowlist(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        self._responder_with_github_forge()
        for command in ("cat src/main/java/Foo.java", "gh pr comment 7 -b hi", "gh pr close 7",
                        "git -C /work/repo show --no-ext-diff --no-textconv abc1234; rm -rf /tmp/x"):
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(self._responder_bash(command)),
                          command)

    def test_gitlab_review_responder_has_no_bash_at_all(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        result = ppl_guard.decide(payload("ppl-review-responder", "Bash", {"command": "ls"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        self.assertIn("ppl-review-responder may run only the forge's read commands: ls",
                      self.deny_reason(result))

    def test_review_responder_redirect_into_a_file_is_denied(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        # The role writes nothing, so a trailing redirect is harmless only as
        # an fd duplication or into /dev/null. Every target here sits outside
        # the repo, where the Bash write boundary does not look.
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        doc = json.loads(saved)
        doc["forge"] = {"kind": "github"}
        snap.write_text(json.dumps(doc), encoding="utf-8")
        try:
            for command in (RESPONDER_DIFF + " > /tmp/x", RESPONDER_DIFF + " >> ~/x",
                            RESPONDER_DIFF + " 2> /tmp/e", RESPONDER_DIFF + " 1> /tmp/x",
                            RESPONDER_DIFF + " &> /tmp/x", RESPONDER_DIFF + " >/tmp/x",
                            RESPONDER_DIFF + " > /tmp/x 2>&1", RESPONDER_DIFF + " > /dev/null > /tmp/x"):
                result = ppl_guard.decide(payload("ppl-review-responder", "Bash",
                                                  {"command": command}))
                self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
            for command in (RESPONDER_DIFF + " 2>&1", RESPONDER_DIFF + " 2>/dev/null",
                            RESPONDER_DIFF + " > /dev/null 2>&1"):
                self.assertIsNone(ppl_guard.decide(payload("ppl-review-responder", "Bash",
                                                           {"command": command})), command)
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_review_responder_redirect_glued_inside_a_rev_is_denied(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        # shlex keeps each of these one token, bash splits it at the redirect.
        self._responder_with_github_forge()
        for command in (RESPONDER_DIFF + ">/tmp/x", RESPONDER_DIFF + ">>~/.zshrc",
                        RESPONDER_DIFF + "</tmp/x"):
            result = self._responder_bash(command)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
        self.assertIsNone(self._responder_bash(RESPONDER_DIFF))

    def test_review_responder_substitution_inside_an_allowed_shape_is_denied(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        # bash runs a substitution mid-word, where shlex sees an ordinary token,
        # so the responder's pre-check refuses the marker before any segment is read.
        self._responder_with_github_forge()
        show = "git -C /work/repo show --no-ext-diff --no-textconv "
        for command in (show + "$(id)", show + "`id`", show + "<(id)", show + ">(id)"):
            self.assertTrue(ppl_guard.review_responder_bash_denied(command, "DEMO-700"), command)
            result = self._responder_bash(command)
            self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), command)
        self.assertIsNone(self._responder_bash(show + "abc1234"))

    def test_a_malformed_forge_table_falls_back_to_the_union_and_no_shapes(self):
        # rule: R-FORGE-DENIALS-FROM-ADAPTER
        snap = paths.run_dir("DEMO-700") / "harness.json"
        saved = snap.read_text(encoding="utf-8")
        try:
            for forge in ("gitlab", ["github"], None, {"kind": "bitbucket"}, {"kind": ["gitlab"]},
                          {"kind": "gitlab"}, {"kind": "gitlab", "mcp_server": 5},
                          {"kind": "gitlab", "mcp_server": {"a": 1}}):
                doc = json.loads(saved)
                doc["forge"] = forge
                snap.write_text(json.dumps(doc), encoding="utf-8")
                with self.subTest(forge=forge):
                    for tool in ppl_guard.DENIED_TOOLS:
                        result = ppl_guard.decide(payload(None, tool, {}))
                        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result), tool)
                    result = ppl_guard.decide(payload("ppl-review-responder", "Bash",
                                                      {"command": RESPONDER_DIFF}))
                    self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))
        finally:
            snap.write_text(saved, encoding="utf-8")

    def test_an_internal_error_denies_a_role_and_still_allows_an_unroled_session(self):
        # rule: R-GUARD-INTERNAL-ERROR-DENIES-ROLES
        def broken(_payload):
            raise RuntimeError("boom")

        outputs = {}
        for agent in ("ppl-code-worker", None, ["ppl-code-worker"]):
            stdout, stderr = io.StringIO(), io.StringIO()
            stdin = io.StringIO(json.dumps(payload(agent, "Bash", {"command": "ls"})))
            with mock.patch.object(ppl_guard, "_decide", broken), \
                    mock.patch.object(sys, "stdin", stdin), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(ppl_guard.main(), 0)
            self.assertIn("internal error", stderr.getvalue())
            outputs[str(agent)] = stdout.getvalue()
        reason = self.deny_reason(json.loads(outputs["ppl-code-worker"]))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("RuntimeError", reason)
        self.assertEqual(outputs["None"], "")
        # an agent_type that is not a string names no role
        self.assertEqual(outputs[str(["ppl-code-worker"])], "")

    def test_an_internal_error_denies_every_generated_role(self):
        # rule: R-GUARD-INTERNAL-ERROR-DENIES-ROLES
        from ppl import routing

        def broken(_payload):
            raise RuntimeError("boom")

        for agent in routing.REQUIRED_ROLES:
            stdout = io.StringIO()
            stdin = io.StringIO(json.dumps(payload(agent, "Read", {"file_path": "/tmp/x"})))
            with self.subTest(agent=agent), mock.patch.object(ppl_guard, "_decide", broken), \
                    mock.patch.object(sys, "stdin", stdin), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ppl_guard.main(), 0)
                self.assertIn("BOUNDARY_VIOLATION",
                              self.deny_reason(json.loads(stdout.getvalue())))

    def test_brief_challenger_may_not_run_a_state_changing_ppl_command(self):
        # rule: R-GUARD-PPL-CLI-DENIED
        result = ppl_guard.decide(payload(
            "ppl-brief-challenger", "Bash", {"command": "ppl brief challenge report.json"}))
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(result))

    def test_a_broken_stderr_does_not_undo_the_role_deny(self):
        # rule: R-GUARD-INTERNAL-ERROR-DENIES-ROLES
        class BrokenStream:
            def write(self, _text):
                raise OSError("stderr is gone")

        def broken(_payload):
            raise RuntimeError("boom")

        stdout = io.StringIO()
        stdin = io.StringIO(json.dumps(payload("ppl-code-worker", "Bash", {"command": "ls"})))
        with mock.patch.object(ppl_guard, "_decide", broken), \
                mock.patch.object(sys, "stdin", stdin), \
                mock.patch.object(sys, "stderr", BrokenStream()), \
                contextlib.redirect_stdout(stdout):
            self.assertEqual(ppl_guard.main(), 0)
        self.assertIn("BOUNDARY_VIOLATION", self.deny_reason(json.loads(stdout.getvalue())))


HOOKS = Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"


class TestPythonFloor(unittest.TestCase):
    """Every hook needs Python 3.11, and under an older one they died on import,
    which Claude Code reads as a non-blocking error: every call allowed. The
    floor runs before any ppl import and fails closed for a role
    [R-GUARD-PYTHON-FLOOR]."""

    def floor(self, version, hook_payload, event="PreToolUse"):
        out, err = io.StringIO(), io.StringIO()
        handled = ppl_python_floor.refuse_old_python(
            version, "ppl_guard", event, io.StringIO(json.dumps(hook_payload)), out, err)
        return handled, out.getvalue(), err.getvalue()

    def test_an_old_python_denies_a_role_and_names_the_version(self):
        # rule: R-GUARD-PYTHON-FLOOR
        handled, out, err = self.floor((3, 9, 6), payload(
            "ppl-code-worker", "Edit", {"file_path": "/tmp/repo/source/Main.java"}))
        self.assertTrue(handled)
        decision = json.loads(out)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("BOUNDARY_VIOLATION", decision["permissionDecisionReason"])
        self.assertIn("3.9.6", decision["permissionDecisionReason"])
        self.assertEqual(len(err.splitlines()), 1, err)
        self.assertIn("3.9.6", err)
        self.assertIn("3.11", err)

    def test_every_generated_role_and_any_other_ppl_name_is_denied(self):
        # rule: R-GUARD-PYTHON-FLOOR
        # boundary.ROLE_RULES cannot be imported under the old interpreter, so
        # the floor denies by the ppl- prefix, a superset of the roles.
        for agent in sorted(boundary.ROLE_RULES) + ["ppl-not-generated-yet"]:
            handled, out, _ = self.floor((3, 10, 14), payload(agent, "Bash", {"command": "ls"}))
            self.assertTrue(handled, agent)
            self.assertIn("BOUNDARY_VIOLATION",
                          json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_an_old_python_lets_an_unroled_session_through_with_the_message(self):
        handled, out, err = self.floor((3, 10, 14), payload(None, "Read",
                                                             {"file_path": "/dev/null"}))
        self.assertTrue(handled)
        self.assertEqual(out, "")
        self.assertIn("3.10.14", err)
        self.assertIn("3.11", err)

    def test_an_unreadable_payload_under_an_old_python_still_explains_itself(self):
        out, err = io.StringIO(), io.StringIO()
        handled = ppl_python_floor.refuse_old_python(
            (3, 9, 6), "ppl_guard", "PreToolUse", io.StringIO("not json"), out, err)
        self.assertTrue(handled)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("3.9.6", err.getvalue())

    def test_the_dispatch_hook_names_its_own_event(self):
        handled, out, _ = self.floor((3, 9, 6), payload("ppl-verifier", "Task", {}),
                                     event="SubagentStart")
        self.assertTrue(handled)
        self.assertEqual(json.loads(out)["hookSpecificOutput"]["hookEventName"], "SubagentStart")

    def test_a_supported_python_passes_through_untouched(self):
        stdin = mock.Mock()
        out, err = io.StringIO(), io.StringIO()
        for version in ((3, 11, 0), (3, 14, 6), (4, 0, 0)):
            self.assertFalse(ppl_python_floor.refuse_old_python(
                version, "ppl_guard", "PreToolUse", stdin, out, err))
        stdin.read.assert_not_called()
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def test_the_hooks_deny_a_role_under_the_system_python(self):
        # rule: R-GUARD-PYTHON-FLOOR
        # The real files under the real old interpreter: this is what proves
        # every line above the floor, and the floor module, parse there.
        found = fixtures.older_system_python()
        if found is None:
            self.skipTest("no python3 older than 3.11 at %s" % fixtures.SYSTEM_PYTHON)
        interpreter, version = found
        env = fixtures.isolated_env()
        for hook, event in (("ppl_guard.py", "PreToolUse"), ("ppl_dispatch.py", "SubagentStart"),
                            ("ppl_usage.py", "SubagentStop")):
            with self.subTest(hook=hook):
                result = subprocess.run(
                    [interpreter, str(HOOKS / hook)],
                    input=json.dumps(payload("ppl-code-worker", "Bash", {"command": "ls"})),
                    capture_output=True, text=True, env=env, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                decision = json.loads(result.stdout)["hookSpecificOutput"]
                self.assertEqual(decision["hookEventName"], event)
                self.assertEqual(decision["permissionDecision"], "deny")
                self.assertIn("BOUNDARY_VIOLATION", decision["permissionDecisionReason"])
                self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
                self.assertIn(version, result.stderr)
                self.assertIn("3.11", result.stderr)

    def test_the_hooks_start_with_their_own_directory_off_sys_path(self):
        # rule: R-GUARD-PYTHON-FLOOR
        # PYTHONSAFEPATH (and -P, -I) keeps the script's directory off sys.path.
        # The floor module sits beside the hooks, so a hook that relied on that
        # entry died on import, and a dead hook allows every call.
        env = fixtures.isolated_env(PYTHONSAFEPATH="1")
        cases = (("ppl_guard.py", payload(None, "Read", {"file_path": "/dev/null"}, cwd="/tmp"), None),
                 ("ppl_dispatch.py", payload("ppl-code-worker", "Task", {}, cwd="/tmp"), None),
                 ("ppl_usage.py", payload("ppl-code-worker", "Task", {}, cwd="/tmp"), None),
                 ("ppl_guard.py", payload("ppl-code-worker", "Bash", {"command": "git add -A"},
                                          cwd="/tmp"), "BOUNDARY_VIOLATION"))
        for hook, hook_payload, refusal in cases:
            with self.subTest(hook=hook, refusal=refusal):
                result = subprocess.run(
                    [sys.executable, str(HOOKS / hook)], input=json.dumps(hook_payload),
                    capture_output=True, text=True, env=env, cwd="/", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                if refusal is None:
                    self.assertEqual(result.stdout, "")
                else:
                    self.assertIn(refusal, json.loads(result.stdout)["hookSpecificOutput"]
                                  ["permissionDecisionReason"])

    def test_an_unroled_session_under_the_system_python_is_allowed(self):
        found = fixtures.older_system_python()
        if found is None:
            self.skipTest("no python3 older than 3.11 at %s" % fixtures.SYSTEM_PYTHON)
        interpreter, version = found
        env = fixtures.isolated_env()
        result = subprocess.run(
            [interpreter, str(HOOKS / "ppl_guard.py")],
            input=json.dumps(payload(None, "Read", {"file_path": "/dev/null"})),
            capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(version, result.stderr)


class TestHookBootstrap(unittest.TestCase):
    """Each hook imports the ppl package it ships in from the directory above
    ppl/, which is site-packages when installed."""

    FORCE_PUSH = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                  "tool_input": {"command": "git push --force origin main"}, "cwd": "/tmp"}
    ROLE_START = {"hook_event_name": "SubagentStart", "agent_type": "ppl-code-worker",
                  "cwd": "/tmp"}
    ROLE_STOP = {"hook_event_name": "SubagentStop", "agent_type": "ppl-code-worker",
                 "agent_id": "a1", "cwd": "/tmp"}

    def site_with_ppl(self):
        site = Path(tmphome.mkdtemp(prefix="pplsite-"))
        shutil.copytree(Path(__file__).resolve().parents[1] / "ppl", site / "ppl",
                        ignore=shutil.ignore_patterns("__pycache__"))
        return site

    def assert_every_hook_works(self, site, **extra):
        env = fixtures.isolated_env()
        env.pop("PYTHONPATH", None)
        env.pop("PPL_ROOT", None)
        env.update(extra)
        cases = (("ppl_guard.py", self.FORCE_PUSH, "BOUNDARY_VIOLATION"),
                 ("ppl_dispatch.py", self.ROLE_START, None),
                 ("ppl_usage.py", self.ROLE_STOP, None))
        for hook, hook_payload, refusal in cases:
            with self.subTest(hook=hook):
                result = subprocess.run(
                    [sys.executable, str(site / "ppl" / "_assets" / "hooks" / hook)],
                    input=json.dumps(hook_payload), capture_output=True, text=True, env=env,
                    cwd="/tmp", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                if refusal is None:
                    self.assertEqual(result.stdout, "")
                else:
                    decision = json.loads(result.stdout)["hookSpecificOutput"]
                    self.assertEqual(decision["permissionDecision"], "deny")
                    self.assertTrue(decision["permissionDecisionReason"].startswith(refusal),
                                    decision["permissionDecisionReason"])

    def test_a_stdlib_name_beside_the_package_does_not_crash_the_hooks(self):
        # An old backport in site-packages can ship a top-level module named
        # like a stdlib one. Here every stdlib name sits beside the copy of ppl
        # and fails on import. A hook that kept that directory first on
        # sys.path imported them and crashed, and Claude Code allows the call
        # when a hook crashes.
        site = self.site_with_ppl()
        for name in sys.stdlib_module_names:
            (site / (name + ".py")).write_text('raise ImportError("shadowed stdlib")\n')
        self.assert_every_hook_works(site)

    def test_the_ppl_beside_the_hook_wins_over_another_one_on_the_path(self):
        other = Path(tmphome.mkdtemp(prefix="pplother-"))
        (other / "ppl").mkdir()
        (other / "ppl" / "__init__.py").write_text('raise ImportError("the other ppl")\n')
        self.assert_every_hook_works(self.site_with_ppl(), PYTHONPATH=str(other))

if __name__ == "__main__":
    unittest.main()

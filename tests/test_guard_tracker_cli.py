import contextlib
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

import fixtures
from ppl import boundary, doctor, harness, paths, run, setup, store
from ppl.trackers import jira
import ppl_guard

TASK = "GH-7"
REPO = "/tmp/ghrepo"
# The orchestrator's session sits beside the run's repo, never inside it.
ELSEWHERE = "/tmp/elsewhere"
COMMENT = "gh issue comment 7 -R o/r --body-file /tmp/n.md"
CLOSE_MUTATION = ("gh api graphql -f query='mutation { closeIssue(input: {issueId: \"I_1\"}) "
                  "{ clientMutationId } }'")
JIRA_WRITES = ("mcp__atlassian__addCommentToJiraIssue",
               "mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue",
               "mcp__atlassian_2__editJiraIssue")
GITHUB_MCP_WRITES = ("mcp__github__add_issue_comment", "mcp__github__issue_write",
                     "mcp__github_2__update_issue", "mcp__corp_github__create_issue")
# The orchestrator's own gh commands from the github forge fragments (stage 6, stage 7
# and the review loop), placeholders filled in. None of them writes an issue.
PIPELINE_GH_COMMANDS = (
    "gh pr create --title \"GH-7 add the cart total\" --body-file /tmp/pr-body.md",
    "gh api user --jq .login",
    "gh api user",
    "gh api repos/o/r/pulls/12/comments --paginate",
    "gh api repos/o/r/pulls/12/reviews",
    "gh api graphql -f query='query { repository(owner: \"o\", name: \"r\") { pullRequest("
    "number: 12) { reviewThreads(first: 100) { nodes { isResolved comments(first: 1) "
    "{ nodes { databaseId } } } } } } }'",
    "gh pr checks 12",
    "gh pr view 12 --json=reviews,headRefOid",
    "gh api repos/o/r/pulls/12/comments/345/replies -f body='Fixed in abc1234, the total "
    "now rounds half up.'",
    "gh api repos/o/r/pulls/12/comments/345/replies -f body=\"$(cat /tmp/reply.md)\"",
    "gh auth status",
)


def payload(agent, tool, tool_input, cwd=REPO):
    return {"hook_event_name": "PreToolUse", "agent_type": agent,
            "tool_name": tool, "tool_input": tool_input, "cwd": cwd}


def bash(agent, command, cwd=REPO):
    return ppl_guard.decide(payload(agent, "Bash", {"command": command}, cwd))


def snapshot_path():
    return paths.run_dir(TASK) / "harness.json"


def github_issues_snapshot():
    """What `ppl init` freezes from a GitHub Issues variant of example-generic."""
    fixtures.install_variant("example-generic", "gh-issues", **fixtures.GITHUB_ISSUES)
    return harness.snapshot(harness.load("gh-issues"))


def setUpModule():
    if not run.state_path(TASK).exists():
        run.init(TASK, "https://github.com/o/r/issues/7", REPO)
    store.write_json(paths.run_dir(TASK) / "boundary.json", {"write": ["src/**"], "forbid": []})
    store.write_json(snapshot_path(), github_issues_snapshot())


class GhRunCase(unittest.TestCase):
    def setUp(self):
        run.set_active(TASK)

    def deny_reason(self, result):
        self.assertIsNotNone(result, "the call was allowed")
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    @contextlib.contextmanager
    def at_merge_watch(self, merged_sha="f" * 40, mr_missing=False):
        """GH-7 in merge_watch, with the merge recorded unless merged_sha is None."""
        state = run.load(TASK)
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
            state = run.load(TASK)
            state["stage"], state["mr"] = saved
            run.save(state)

    @contextlib.contextmanager
    def snapshot_text(self, text):
        """harness.json replaced by `text`, or removed when `text` is None."""
        saved = snapshot_path().read_text(encoding="utf-8")
        if text is None:
            snapshot_path().unlink()
        else:
            snapshot_path().write_text(text, encoding="utf-8")
        try:
            yield
        finally:
            snapshot_path().write_text(saved, encoding="utf-8")

    def with_tracker(self, table):
        doc = json.loads(snapshot_path().read_text(encoding="utf-8"))
        doc["tracker"] = table
        return self.snapshot_text(json.dumps(doc))


class TestTrackerCliGate(GhRunCase):
    """The orchestrator's gh issue writes wait for the observed merge, like its MCP
    tracker writes [R-TRACKER-CLI-GATE]."""

    def test_gh_issue_comment_before_merge_watch_is_denied_for_the_orchestrator(self):
        # rule: R-TRACKER-CLI-GATE
        reason = self.deny_reason(bash(None, COMMENT, ELSEWHERE))
        self.assertIn("OWNER_DECISION", reason)
        self.assertIn("merge is observed", reason)

    def test_gh_issue_comment_in_merge_watch_before_the_merge_is_recorded_is_denied(self):
        # rule: R-TRACKER-CLI-GATE
        with self.at_merge_watch(merged_sha=None):
            reason = self.deny_reason(bash(None, COMMENT, ELSEWHERE))
        self.assertIn("OWNER_DECISION", reason)
        self.assertIn("ppl merge observed", reason)
        with self.at_merge_watch(mr_missing=True):
            self.assertIn("OWNER_DECISION", self.deny_reason(bash(None, COMMENT, ELSEWHERE)))

    def test_gh_issue_comment_is_allowed_once_the_merge_is_recorded(self):
        with self.at_merge_watch():
            self.assertIsNone(bash(None, COMMENT, ELSEWHERE))

    def test_every_write_shape_is_gated_before_the_merge_is_observed(self):
        # rule: R-TRACKER-CLI-GATE
        shapes = ("gh -R o/r issue close 5",
                  "gh api -X POST repos/o/r/issues/5/comments",
                  "gh api repos/o/r/issues/5/comments -f body=x",
                  "gh api graphql -f query='mutation { addComment(input: {subjectId: "
                  "\"I_1\", body: \"x\"}) { clientMutationId } }'")
        for command in shapes:
            reason = self.deny_reason(bash(None, command, ELSEWHERE))
            self.assertIn("OWNER_DECISION", reason, command)
            self.assertIn("merge is observed", reason, command)

    def test_issue_reads_are_not_gated(self):
        for command in ("gh issue view 7 -R o/r", "gh issue view 7 -R o/r --comments",
                        "gh issue list -R o/r", "gh api repos/o/r/issues/7",
                        "gh api repos/o/r/issues/7/comments --paginate"):
            self.assertIsNone(bash(None, command, ELSEWHERE), command)

    def test_a_corrupt_snapshot_gates_every_cli_tracker(self):
        # rule: R-TRACKER-CLI-GATE
        # the snapshot does not choose which CLI checks apply, so no state of it opens one
        doc = json.loads(snapshot_path().read_text(encoding="utf-8"))
        broken = ["{not json", "[]", None,
                  json.dumps(dict(doc, tracker={"kind": "github-issues"})),
                  json.dumps(dict(doc, tracker={"kind": "linear", "repo": "o/r"})),
                  json.dumps(dict(doc, tracker="github-issues")),
                  json.dumps({key: value for key, value in doc.items() if key != "tracker"})]
        for text in broken:
            with self.snapshot_text(text):
                reason = self.deny_reason(bash(None, COMMENT, ELSEWHERE))
            self.assertIn("OWNER_DECISION", reason, text)
            self.assertIn("merge is observed", reason, text)

    def test_a_jira_run_also_gates_gh_issue_writes(self):
        # rule: R-TRACKER-CLI-GATE
        with self.with_tracker(dict(jira.DEFAULTS, kind="jira")):
            reason = self.deny_reason(bash(None, COMMENT, ELSEWHERE))
            self.assertIn("OWNER_DECISION", reason)
            self.assertIn("merge is observed", reason)
            with self.at_merge_watch():
                self.assertIsNone(bash(None, COMMENT, ELSEWHERE))

    def test_a_github_issues_run_still_gates_jira_mcp_writes(self):
        # rule: R-JIRA-GATE
        for tool in JIRA_WRITES:
            reason = self.deny_reason(ppl_guard.decide(payload(None, tool, {"body": "x"},
                                                               ELSEWHERE)))
            self.assertIn("OWNER_DECISION", reason, tool)
            self.assertIn("merge is observed", reason, tool)
        with self.at_merge_watch():
            for tool in JIRA_WRITES:
                self.assertIsNone(ppl_guard.decide(payload(None, tool, {"body": "x"},
                                                           ELSEWHERE)), tool)

    def test_a_jira_run_also_gates_github_mcp_issue_writes(self):
        # rule: R-JIRA-GATE
        with self.with_tracker(dict(jira.DEFAULTS, kind="jira")):
            for tool in GITHUB_MCP_WRITES:
                reason = self.deny_reason(ppl_guard.decide(payload(None, tool, {"body": "x"},
                                                                   ELSEWHERE)))
                self.assertIn("OWNER_DECISION", reason, tool)
            # the GitHub server's reads stay open
            self.assertIsNone(ppl_guard.decide(payload(
                None, "mcp__github__get_issue", {"issue_number": 7}, ELSEWHERE)))

    def test_a_failing_check_reads_as_a_write(self):
        # rule: R-TRACKER-CLI-GATE, R-TRACKER-CLI-NO-ROLE, R-JIRA-GATE
        # main() allows the orchestrator's call when decide() raises, so the gate
        # turns a check's own failure into a deny instead
        class Broken(object):
            pattern = "broken"

            def search(self, command):
                raise RuntimeError("defect")

        with mock.patch.object(ppl_guard, "ALL_CLI_TRACKER_DENIALS", (Broken(),)):
            reason = self.deny_reason(bash(None, "gh pr checks 12", ELSEWHERE))
            self.assertIn("OWNER_DECISION", reason)
            self.assertIn("merge is observed", reason)
            role = self.deny_reason(bash("ppl-code-worker", "gh pr checks 12"))
            self.assertIn("BOUNDARY_VIOLATION", role)
        with mock.patch.object(ppl_guard, "is_tracker_write_tool",
                               side_effect=RuntimeError("defect")):
            for tool in JIRA_WRITES + GITHUB_MCP_WRITES:
                reason = self.deny_reason(ppl_guard.decide(payload(None, tool, {"body": "x"},
                                                                   ELSEWHERE)))
                self.assertIn("OWNER_DECISION", reason, tool)

    def test_the_installed_matcher_passes_github_mcp_issue_writes_to_the_guard(self):
        # rule: R-GUARD-MATCHER, R-JIRA-GATE
        # the gate matches these under any server name, so only a catch-all
        # matcher hands every one of them to the hook
        matcher = setup.hook_entries(fixtures.SOURCE)["PreToolUse"]["matcher"]
        self.assertEqual(matcher, doctor.RECOMMENDED_MATCHER)
        self.assertEqual(matcher, ".*")
        for tool in GITHUB_MCP_WRITES:
            self.assertTrue(doctor.matcher_covers(matcher, tool), tool)
            reason = self.deny_reason(ppl_guard.decide(payload(None, tool, {"body": "x"},
                                                               ELSEWHERE)))
            self.assertIn("OWNER_DECISION", reason, tool)

    def test_the_pipeline_gh_commands_pass_before_the_merge(self):
        for command in PIPELINE_GH_COMMANDS:
            self.assertIsNone(bash(None, command, ELSEWHERE), command)
        with self.at_merge_watch(merged_sha=None):
            for command in PIPELINE_GH_COMMANDS:
                self.assertIsNone(bash(None, command, ELSEWHERE), command)


class TestTrackerCliNoRole(GhRunCase):
    """No role writes a tracker through its CLI, merged or not, run or no run
    [R-TRACKER-CLI-NO-ROLE]."""

    def test_the_code_worker_is_denied_an_issue_write_after_the_merge_is_recorded(self):
        # rule: R-TRACKER-CLI-NO-ROLE
        with self.at_merge_watch():
            reason = self.deny_reason(bash("ppl-code-worker", COMMENT))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("ppl-code-worker may not write the tracker", reason)

    def test_a_role_is_denied_tracker_writes_with_no_active_run(self):
        # rule: R-TRACKER-CLI-NO-ROLE
        paths.active_file().unlink()
        try:
            self.assertIsNone(run.active())
            for role in boundary.ROLE_RULES:
                reason = self.deny_reason(bash(role, CLOSE_MUTATION, ELSEWHERE))
                self.assertIn("BOUNDARY_VIOLATION", reason, role)
                self.assertIn("%s may not write the tracker" % role, reason)
        finally:
            run.set_active(TASK)

    def test_the_review_responder_has_no_gh_issue_shape(self):
        # rule: R-REVIEW-RESPONDER-BASH-SHAPES
        # an issue read is no tracker write, so the responder's own allowlist refuses it
        for command in ("gh issue view 7 --repo=o/r", "gh issue list --repo=o/r"):
            self.assertTrue(ppl_guard.review_responder_bash_denied(command, TASK), command)
            reason = self.deny_reason(bash("ppl-review-responder", command))
            self.assertIn("BOUNDARY_VIOLATION", reason, command)
            self.assertIn("may run only the forge's read commands", reason, command)

    def test_a_role_keeps_its_own_commands(self):
        for command in ("gh api repos/o/r/pulls/12/comments --paginate",
                        "gh pr view 12 --json=reviews,headRefOid"):
            self.assertIsNone(bash("ppl-review-responder", command), command)
        for command in ("python3 -m pytest tests -q", "gh issue view 7 -R o/r"):
            self.assertIsNone(bash("ppl-code-worker", command), command)


class TestTrackerMcpNoRole(GhRunCase):
    """No role calls a tracker MCP write tool of any known adapter, merged or not, run
    or no run: the MCP twin of the CLI ban [R-JIRA-NO-ROLE-TOOL]."""

    # mcp__github__add_issue_comment first, the write the ruling names
    WRITES = GITHUB_MCP_WRITES + JIRA_WRITES

    def calls(self, tools, cwd=REPO):
        """(role, tool, decide() result) for every role calling every tool."""
        return [(role, tool, ppl_guard.decide(payload(role, tool, {"body": "x"}, cwd)))
                for role in boundary.ROLE_RULES for tool in tools]

    def test_a_role_is_denied_a_github_mcp_issue_write_with_no_active_run(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        paths.active_file().unlink()
        try:
            self.assertIsNone(run.active())
            for role, tool, result in self.calls(self.WRITES, ELSEWHERE):
                reason = self.deny_reason(result)
                self.assertIn("BOUNDARY_VIOLATION", reason, (role, tool))
                self.assertIn("%s may not write the tracker" % role, reason)
        finally:
            run.set_active(TASK)

    def test_a_role_is_denied_a_github_mcp_issue_write_after_the_merge_is_recorded(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        with self.at_merge_watch():
            for role, tool, result in self.calls(self.WRITES):
                reason = self.deny_reason(result)
                self.assertIn("BOUNDARY_VIOLATION", reason, (role, tool))
                self.assertIn("%s may not write the tracker" % role, reason)

    def test_a_role_is_denied_the_run_prefixed_and_the_default_jira_writes(self):
        # rule: R-JIRA-NO-ROLE-TOOL
        # a Jira run naming its own prefix: for a role the defaults stay shut too
        table = dict(jira.DEFAULTS, kind="jira", mcp_prefixes=["mcp__corp_jira__"])
        tools = ("mcp__corp_jira__editJiraIssue", "mcp__atlassian__editJiraIssue",
                 "mcp__github__issue_write")
        with self.with_tracker(table), self.at_merge_watch():
            for role, tool, result in self.calls(tools):
                reason = self.deny_reason(result)
                self.assertIn("BOUNDARY_VIOLATION", reason, (role, tool))
                self.assertIn("%s may not write the tracker" % role, reason)

    def test_a_role_keeps_the_tracker_read_tools(self):
        for tool in ("mcp__atlassian__getJiraIssue", "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
                     "mcp__github__get_issue"):
            self.assertIsNone(ppl_guard.decide(payload("ppl-briefer", tool, {"issueKey": "X-1"})),
                              tool)


if __name__ == "__main__":
    unittest.main()

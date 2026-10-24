import json
import os
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import forges

GITLAB = {"kind": "gitlab", "mcp_server": "GitLab-MCP"}
GITHUB = {"kind": "github"}


class TestSurface(unittest.TestCase):
    def test_both_kinds_resolve(self):
        self.assertEqual(forges.get("gitlab").KIND, "gitlab")
        self.assertEqual(forges.get("github").KIND, "github")
        with self.assertRaises(ValueError):
            forges.get("bitbucket")

    def test_every_member_exists_on_both(self):
        for kind in forges.KINDS:
            module = forges.get(kind)
            for member in ("VOCABULARY", "role_tools", "denied_tools", "denied_bash",
                           "bash_shapes", "fragment", "doctor_check"):
                self.assertTrue(hasattr(module, member), "%s lacks %s" % (kind, member))

    def test_gitlab_tools_follow_the_configured_server_name(self):
        gitlab = forges.get("gitlab")
        tools = gitlab.role_tools("ppl-review-responder", {"kind": "gitlab", "mcp_server": "GL"})
        self.assertEqual(tools, ["mcp__GL__mr_discussions", "mcp__GL__get_merge_request_diffs"])
        denied = gitlab.denied_tools({"kind": "gitlab", "mcp_server": "GL"})
        self.assertEqual(sorted(denied), ["mcp__GL__approve_merge_request",
                                          "mcp__GL__merge_merge_request",
                                          "mcp__GL__resolve_merge_request_thread"])
        self.assertEqual(gitlab.role_tools("ppl-briefer", GITLAB), [])
        self.assertEqual(gitlab.bash_shapes("ppl-review-responder"), [])

    def test_github_review_responder_gets_bash_and_read_only_git_shapes(self):
        github = forges.get("github")
        self.assertEqual(github.role_tools("ppl-review-responder", GITHUB), ["Bash"])
        shapes = github.bash_shapes("ppl-review-responder")
        self.assertEqual(len(shapes), 3)
        self.assertTrue(all(row["argv0"] == "git" for row in shapes))
        self.assertEqual([row["one_goal_of"] for row in shapes], [["diff"], ["show"], ["log"]])
        denied = github.denied_tools(GITHUB)
        self.assertIn("mcp__github__merge_pull_request", denied)

    def test_github_denied_bash_catches_merge_approve_and_resolve(self):
        github = forges.get("github")
        patterns = github.denied_bash()
        for command in ("gh pr merge 12", "gh pr review 12 --approve",
                        "gh api graphql -f query='mutation { resolveReviewThread(input: {}) }'",
                        "gh pr review 7 -a",
                        "gh pr review 7 -ab ok",
                        "gh pr review --approve 7",
                        "gh pr -R o/r merge 7",
                        "gh pr -Ro/r merge 7",
                        "gh pr -Ro/r 'merge' 7",
                        "gh pr -R'o/r' merge 7",
                        "gh pr --draft -R o/r merge 7",
                        "gh pr --verbose --repo o/r merge 7",
                        "gh pr --a -R o/r --b -R p/q merge 7",
                        "gh pr --body-file - merge 7",
                        "gh pr -b - merge 7",
                        "gh pr -R -1 merge 7",
                        "gh pr -R  o/r merge 7",
                        "gh pr -R\to/r merge 7",
                        'gh pr --title "a\\"b c" merge 7',
                        'gh pr --title "a\\\nb" merge 7',
                        "gh pr --repo= merge 7",
                        "gh pr --repo=o/r merge 7",
                        "gh pr merge --squash 7",
                        'gh pr "merge" 7',
                        "gh pr 'merge' 7",
                        "gh pr --title 'a b' merge 7",
                        "gh pr --verbose merge 7",
                        'gh pr -R "o/r" "merge" 7',
                        "gh pr $(echo merge) 7",
                        "gh pr `echo merge` 7",
                        "gh pr -R o/r review 7 --approve",
                        "gh pr merge 7 --auto",
                        "gh api -X PUT repos/o/r/pulls/7/merge",
                        "gh api repos/o/r/pulls/7/merge --method=PUT",
                        "gh api repos/o/r/pulls/7/reviews -f event=APPROVE",
                        "gh api repos/o/r/pulls/7/reviews -f event='APPROVE'",
                        "gh api repos/o/r/pulls/7/reviews --field event=approve",
                        "gh api graphql -f query='mutation { mergePullRequest(input: {}) }'",
                        "gh api graphql -f query='mutation { enablePullRequestAutoMerge(input: {}) }'",
                        "gh api graphql -f query='mutation { addPullRequestReview(input: {}) }'",
                        "gh api graphql -f query='mutation { submitPullRequestReview(input: {}) }'"):
            self.assertTrue(any(p.search(command) for p, _ in patterns), command)
        for command in ("gh pr view 12", "gh pr review 12 --comment -b ok", "gh pr checks 12",
                        "gh pr review 7 --request-changes -b fix",
                        "gh pr view 7 --json=mergeable,mergeStateStatus,reviews",
                        'gh pr create --title "Fix merge conflict handling" --body-file x',
                        "gh pr create --title merge --body-file x",
                        "gh pr create --title 'Fix a merge bug' --body-file x",
                        "gh api repos/o/r/pulls/7/comments/1/replies -f body=hi",
                        "gh api repos/o/r/pulls/7/reviews",
                        "gh api graphql -f query='mutation { addPullRequestReviewThreadReply(input: {}) }'"):
            self.assertFalse(any(p.search(command) for p, _ in patterns), command)

    def test_github_denied_bash_stays_fast_on_backtracking_decoys(self):
        # A hook that runs past its timeout denies nothing, so a decoy that
        # makes a pattern backtrack is a bypass, not a slowdown. Before the
        # flag grammar was made unambiguous the first decoy took 11 s.
        patterns = forges.get("github").denied_bash()
        merge, approve = "a human performs the merge", "never approves its own pull request"
        decoys = (("gh pr " + "--a 'x' " * 13 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "--a " * 18 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "--a 'x' " * 40 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "--a " * 40 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "-Ro/r " * 40 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "-R'o/r' " * 40 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "--a  'x'  " * 40 + "zzz; gh pr merge 7", merge),
                  ("gh pr " + "-R -1 " * 40 + "zzz; gh pr merge 7", merge),
                  ("gh pr -R" + " " * 40000 + "zzz; gh pr merge 7", merge),
                  ('gh pr --title "' + 'a\\"' * 2000 + '" zzz; gh pr merge 7', merge),
                  ("gh pr review " * 500 + "; gh pr review 7 --approve", approve))
        for command, reason in decoys:
            with self.subTest(command=command[:40], length=len(command)):
                start = time.perf_counter()
                hits = [why for pattern, why in patterns if pattern.search(command)]
                self.assertLess(time.perf_counter() - start, 0.2)
                self.assertTrue(hits)
                self.assertIn(reason, hits[0])

    def test_every_github_denied_bash_pattern_carries_a_reason(self):
        for pattern, reason in forges.get("github").denied_bash():
            self.assertTrue(reason.strip(), pattern.pattern)

    def test_fragments_exist_and_use_their_own_vocabulary(self):
        for kind, foreign in (("gitlab", "pull request"), ("github", "merge request")):
            module = forges.get(kind)
            for name in ("stage6", "stage7", "review-loop"):
                text = module.fragment(name)
                self.assertTrue(text.strip(), "%s/%s is empty" % (kind, name))
                self.assertNotIn(foreign, text.lower(), "%s/%s names the other forge" % (kind, name))

    def test_stage_seven_and_the_review_loop_include_the_close_out_instead_of_writing_it(self):
        # The review loop once ended with its own two-command close-out while stage 7 had five.
        for kind in ("gitlab", "github"):
            module = forges.get(kind)
            for name in ("review-loop", "stage7"):
                text = module.fragment(name)
                terminal = text[text.index("Terminal"):]
                self.assertIn("<!-- include: sequence/close-out -->", terminal, "%s/%s" % (kind, name))
                self.assertNotIn("ppl transition done", text, "%s/%s" % (kind, name))

    def test_github_fragments_write_the_json_flag_in_its_equals_form(self):
        github = forges.get("github")
        for name in ("stage6", "stage7", "review-loop"):
            self.assertNotIn("--json ", github.fragment(name), name)

    def test_github_fragments_fetch_the_threads_through_ppl(self):
        # ppl review fetch writes the thread ids as JSON strings, the model builds no JSON
        github = forges.get("github")
        for name in ("stage7", "review-loop"):
            text = " ".join(github.fragment(name).split())
            self.assertIn("`<!-- include: os/ppl --> review fetch --pr <number>`", text, name)
            self.assertNotIn("graphql", text, name)

    def test_github_fragment_commands_are_not_denied(self):
        github = forges.get("github")
        spans = [span for name in ("stage6", "stage7", "review-loop")
                 for span in re.findall(r"`<!-- include: os/gh --> ([^`]+)`",
                                        github.fragment(name))]
        self.assertTrue(spans)
        for span in spans:
            command = "/opt/pin/bin/gh " + span
            hit = [why for p, why in github.denied_bash() if p.search(command)]
            self.assertEqual(hit, [], command)

    def test_union_of_denied_tools(self):
        union = forges.all_denied_tools()
        self.assertIn("mcp__GitLab-MCP__resolve_merge_request_thread", union)
        self.assertIn("mcp__github__merge_pull_request", union)


class TestDoctorChecks(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tmphome.mkdtemp(prefix="pplforge-"))
        self.claude_json = self.dir / ".claude.json"
        self.workspace = self.dir / "ws"
        self.workspace.mkdir()

    def test_gitlab_check_finds_the_server_in_claude_json(self):
        self.claude_json.write_text(json.dumps({"mcpServers": {"GitLab-MCP": {"url": "x"}}}),
                                    encoding="utf-8")
        result = forges.get("gitlab").doctor_check(GITLAB, self.claude_json, self.workspace)
        self.assertTrue(result["ok"], result)

    def test_gitlab_check_finds_the_server_in_workspace_mcp_json(self):
        self.claude_json.write_text("{}", encoding="utf-8")
        (self.workspace / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"GitLab-MCP": {"url": "x"}}}), encoding="utf-8")
        result = forges.get("gitlab").doctor_check(GITLAB, self.claude_json, self.workspace)
        self.assertTrue(result["ok"], result)

    def test_gitlab_check_fails_when_absent_or_unreadable(self):
        self.claude_json.write_text("{not json", encoding="utf-8")
        result = forges.get("gitlab").doctor_check(GITLAB, self.claude_json, self.workspace)
        self.assertFalse(result["ok"])
        self.assertIn("GitLab-MCP", result["detail"])

    def test_gitlab_check_finds_the_server_under_a_project_entry(self):
        self.claude_json.write_text(json.dumps(
            {"projects": {"/some/path": {"mcpServers": {"GitLab-MCP": {"url": "x"}}}}}),
            encoding="utf-8")
        result = forges.get("gitlab").doctor_check(GITLAB, self.claude_json, self.workspace)
        self.assertTrue(result["ok"], result)

    def test_gitlab_check_survives_valid_json_of_the_wrong_shape(self):
        gitlab = forges.get("gitlab")
        for body in ("[]", "null", '"s"', "7", '{"mcpServers": ["a"]}', '{"mcpServers": null}',
                     '{"projects": ["a"]}', '{"projects": {"p": ["a"]}}',
                     '{"projects": {"p": {"mcpServers": "x"}}}'):
            self.claude_json.write_text(body, encoding="utf-8")
            result = gitlab.doctor_check(GITLAB, self.claude_json, self.workspace)
            self.assertFalse(result["ok"], body)
            self.assertIn("GitLab-MCP", result["detail"], body)

    def test_gitlab_check_reports_a_wrong_shape_file_as_unreadable_but_still_uses_the_other(self):
        gitlab = forges.get("gitlab")
        self.claude_json.write_text("[]", encoding="utf-8")
        result = gitlab.doctor_check(GITLAB, self.claude_json, self.workspace)
        self.assertIn("unreadable", result["detail"])
        (self.workspace / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"GitLab-MCP": {"url": "x"}}}), encoding="utf-8")
        self.assertTrue(gitlab.doctor_check(GITLAB, self.claude_json, self.workspace)["ok"])

    def test_github_check_runs_gh_auth_status(self):
        github = forges.get("github")
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            class Done:
                returncode = 0 if argv[1] == "--version" or argv[1:3] == ["auth", "status"] else 1
                stdout = "gh version 2.0.0"
                stderr = ""
            return Done()
        result = github.doctor_check(GITHUB, self.claude_json, self.workspace, runner=fake_run)
        self.assertTrue(result["ok"], result)
        self.assertEqual(calls[0][:2], ["gh", "--version"])
        self.assertEqual(calls[1][:3], ["gh", "auth", "status"])

    def test_github_check_fails_when_gh_is_missing(self):
        def missing(argv, **kwargs):
            raise FileNotFoundError("gh")
        result = forges.get("github").doctor_check(GITHUB, self.claude_json, self.workspace,
                                                   runner=missing)
        self.assertFalse(result["ok"])
        self.assertIn("gh", result["detail"])

    def test_github_check_fails_when_gh_cannot_be_executed(self):
        def denied(argv, **kwargs):
            raise PermissionError("gh")
        result = forges.get("github").doctor_check(GITHUB, self.claude_json, self.workspace,
                                                   runner=denied)
        self.assertFalse(result["ok"])
        self.assertIn("gh", result["detail"])

    def test_github_check_tolerates_blank_version_output(self):
        def blank(argv, **kwargs):
            class Done:
                returncode = 0
                stdout = "  \n"
                stderr = ""
            return Done()
        result = forges.get("github").doctor_check(GITHUB, self.claude_json, self.workspace,
                                                   runner=blank)
        self.assertTrue(result["ok"], result)


if __name__ == "__main__":
    unittest.main()

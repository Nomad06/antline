import os
import random
import re
import subprocess
import time
import unittest
from unittest import mock

from ppl import forges, trackers
from ppl.errors import PplError
from ppl.trackers import jira

PREFIXES = jira.DEFAULTS["mcp_prefixes"]
WRITES = jira.DEFAULTS["write_operations"]


class TestRegistry(unittest.TestCase):
    def test_each_kind_resolves_to_its_adapter(self):
        self.assertEqual(trackers.KINDS, ("jira", "github-issues"))
        self.assertIs(trackers.get("jira"), jira)
        self.assertEqual(trackers.get("github-issues").KIND, "github-issues")
        self.assertEqual(trackers.get("github-issues").__name__, "ppl.trackers.github_issues")

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            trackers.get("linear")

    def test_the_mcp_prefix_shape(self):
        for good in ("mcp__atlassian__", "mcp__claude_ai_Atlassian_Rovo__", "mcp__a-b_c__"):
            self.assertTrue(trackers.is_mcp_prefix(good), good)
        for bad in ("mcp__corp_jira", "corp_", "mcp__x", "mcp____", "", "mcp__a.b__",
                    "mcp__x__\n", "xmcp__x__", None, 7):
            self.assertFalse(trackers.is_mcp_prefix(bad), bad)


def jira_table(**override):
    table = {"kind": "jira", "mcp_prefixes": list(PREFIXES), "write_operations": list(WRITES),
             "read_tools": list(jira.DEFAULTS["read_tools"])}
    table.update(override)
    return table


class TestAdapterInterface(unittest.TestCase):
    def test_every_kind_exports_the_interface(self):
        self.assertEqual(tuple(trackers._MODULES), trackers.KINDS)
        for kind in trackers.KINDS:
            adapter = trackers.get(kind)
            self.assertEqual(adapter.KIND, kind)
            self.assertIn(adapter.TRANSPORT, ("mcp", "cli"), kind)
            self.assertIsInstance(adapter.DEFAULTS, dict, kind)
            self.assertIsInstance(adapter.KEYS, tuple, kind)
            self.assertIn("kind", adapter.KEYS, kind)
            for name in ("validate", "usable", "mcp_write_regex", "mcp_read_tools",
                         "write_denials", "fragment", "doctor_check", "task_defaults"):
                self.assertTrue(callable(getattr(adapter, name, None)), (kind, name))

    def test_every_kind_reads_its_two_fragments_and_refuses_an_unknown_one(self):
        for kind in trackers.KINDS:
            for name in ("admission", "close"):
                text = trackers.get(kind).fragment(name)
                self.assertTrue(text.strip(), (kind, name))
                self.assertEqual(text, trackers.read_fragment(kind, name))
            with self.assertRaises(OSError):
                trackers.get(kind).fragment("nope")

    def test_usable_is_false_for_a_non_dict_and_reads_an_absent_kind_as_jira(self):
        for bad in (None, "jira", 5, [], ()):
            self.assertFalse(trackers.usable(bad), bad)
        self.assertTrue(trackers.usable(jira_table()))
        without_kind = jira_table()
        del without_kind["kind"]
        self.assertTrue(trackers.usable(without_kind))
        self.assertFalse(trackers.usable(jira_table(kind="linear")))
        self.assertFalse(trackers.usable(jira_table(kind=["jira"])))
        self.assertFalse(trackers.usable(jira_table(mcp_prefixes=["mcp__corp_jira"])))
        self.assertFalse(trackers.usable(jira_table(write_operations=[])))

    def test_jira_table_functions_match_its_two_argument_helpers(self):
        table = jira_table(mcp_prefixes=["mcp__corp_jira__"], write_operations=["editJiraIssue"],
                           read_tools=["getJiraIssue"])
        self.assertEqual(jira.mcp_write_regex(table).pattern,
                         jira.write_tool_regex(["mcp__corp_jira__"], ["editJiraIssue"]).pattern)
        self.assertEqual(jira.mcp_read_tools(table), ["mcp__corp_jira__getJiraIssue"])
        self.assertEqual(jira.mcp_read_tools(jira_table()),
                         jira.read_tool_names(PREFIXES, jira.DEFAULTS["read_tools"]))
        self.assertTrue(jira.usable(jira_table()))
        self.assertFalse(jira.usable(jira_table(mcp_prefixes=[])))

    def test_jira_validate_reports_what_the_harness_check_refused(self):
        self.assertEqual(jira.validate(jira_table()), ([], []))
        errors, _ = jira.validate(jira_table(mcp_prefixes=["mcp__atlassian__", "mcp__corp_jira"]))
        self.assertEqual(errors, ["tracker.mcp_prefixes[1] 'mcp__corp_jira' must look like "
                                  "mcp__<name>__"])
        errors, _ = jira.validate(jira_table(read_tools=["getJiraIssue", "editJiraIssue"]))
        self.assertEqual(len(errors), 1)
        self.assertIn("tracker.read_tools[1] 'editJiraIssue' is also a tracker.write_operations "
                      "entry", errors[0])
        errors, _ = jira.validate(jira_table(read_tools=["a b"]))
        self.assertIn("tracker.read_tools[0] 'a b' is not a bare tool name", errors[0])
        errors, _ = jira.validate({"kind": "jira"})
        self.assertEqual(errors, ["tracker.mcp_prefixes is required"])
        errors, _ = jira.validate(jira_table(write_operations="x"))
        self.assertEqual(errors, ["tracker.write_operations must be a list"])
        errors, _ = jira.validate(jira_table(write_operations=[1]))
        self.assertEqual(errors, ["tracker.write_operations must hold only strings"])
        errors, warnings = jira.validate(jira_table(mcp_prefixes=["mcp__corp_jira__"]))
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("tracker.mcp_prefixes keeps none of the jira defaults", warnings[0])

    def test_jira_writes_nothing_through_bash(self):
        self.assertEqual(jira.write_denials(jira_table()), [])
        self.assertEqual(jira.TRANSPORT, "mcp")


def github_issues_table(**override):
    table = {"kind": "github-issues", "repo": "o/r", "task_prefix": "GH"}
    table.update(override)
    return table


# Every issue-writing GraphQL mutation behind a gh issue verb the denials cover.
ISSUE_MUTATIONS = ("addComment", "updateIssue", "closeIssue", "reopenIssue", "deleteIssue",
                   "addLabelsToLabelable", "removeLabelsFromLabelable", "createIssue",
                   "updateIssueComment", "deleteIssueComment", "clearLabelsFromLabelable",
                   "addAssigneesToAssignable", "removeAssigneesFromAssignable",
                   "transferIssue", "pinIssue", "unpinIssue", "lockLockable", "unlockLockable",
                   "createLinkedBranch", "deleteLinkedBranch", "addSubIssue", "removeSubIssue",
                   "reprioritizeSubIssue", "markIssueAsDuplicate", "unmarkIssueAsDuplicate",
                   "updateIssueIssueType")


class GithubIssuesCase(unittest.TestCase):
    def setUp(self):
        self.adapter = trackers.get("github-issues")
        self.denials = self.adapter.write_denials(github_issues_table())

    def denied(self, command):
        return any(pattern.search(command) for pattern in self.denials)

    def assert_denied(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(self.denied(command), command)

    def assert_allowed(self, commands):
        for command in commands:
            with self.subTest(command=command):
                self.assertFalse(self.denied(command), command)

    def assert_fast(self, commands, denied):
        # A hook that runs past its timeout denies nothing, so a command that makes
        # a check backtrack or recurse is a bypass, not a slowdown.
        for command in commands:
            with self.subTest(command=command[:40], length=len(command)):
                start = time.perf_counter()
                hit = self.denied(command)
                self.assertLess(time.perf_counter() - start, 0.2)
                self.assertEqual(hit, denied)

    def assert_fast_and_clean(self, commands):
        self.assert_fast(commands, False)


class TestGithubIssuesAdapter(GithubIssuesCase):
    def test_the_adapter_writes_through_gh_and_reads_no_mcp_tool(self):
        self.assertEqual(self.adapter.TRANSPORT, "cli")
        self.assertEqual(self.adapter.DEFAULTS, {"task_prefix": "GH"})
        self.assertEqual(self.adapter.KEYS, ("kind", "repo", "task_prefix"))
        self.assertEqual(self.adapter.mcp_read_tools(github_issues_table()), [])

    def test_the_github_mcp_issue_writes_are_tracker_writes_under_any_server(self):
        pattern = self.adapter.mcp_write_regex(github_issues_table())
        for operation in ("create_issue", "update_issue", "add_issue_comment", "issue_write",
                          "sub_issue_write", "add_sub_issue", "remove_sub_issue",
                          "reprioritize_sub_issue", "assign_copilot_to_issue"):
            for prefix in ("mcp__github__", "mcp__github_2__", "mcp__claude_ai_GitHub__",
                           "mcp__corp-gh__"):
                self.assertTrue(pattern.match(prefix + operation), prefix + operation)
        for name in ("", "mcp__github__get_issue", "mcp__github__list_issues",
                     "mcp__github__search_issues", "mcp__github__issue_read",
                     "mcp__github__add_issue_comments", "github__add_issue_comment",
                     "mcp__github__add_issue_comment\n", "mcp____add_issue_comment",
                     "mcp__atlassian__editJiraIssue", "Bash"):
            self.assertFalse(pattern.match(name), name)

    def test_the_issue_number_is_the_digits_after_the_prefix(self):
        self.assertEqual(self.adapter.issue_number("GH-123"), 123)
        self.assertEqual(self.adapter.issue_number("PROJ2-7"), 7)
        for bad in ("gh-12", "GH-012", "GH-0", "GH-", "GH-12a", "123", "GH-12\n", "", None, 12):
            with self.subTest(task_id=bad):
                with self.assertRaises(PplError) as caught:
                    self.adapter.issue_number(bad)
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_validate_requires_an_owner_name_repo_and_a_plain_prefix(self):
        self.assertEqual(self.adapter.validate(github_issues_table()), ([], []))
        without_prefix = github_issues_table()
        del without_prefix["task_prefix"]
        self.assertEqual(self.adapter.validate(without_prefix), ([], []))
        for repo in ("", "o", "o/r/x", "https://github.com/o/r", "o r/x", 5, None):
            with self.subTest(repo=repo):
                errors, _ = self.adapter.validate(github_issues_table(repo=repo))
                self.assertEqual(len(errors), 1)
                self.assertIn("tracker.repo", errors[0])
        for prefix in ("gh", "G-H", "", 7):
            with self.subTest(task_prefix=prefix):
                errors, _ = self.adapter.validate(github_issues_table(task_prefix=prefix))
                self.assertEqual(len(errors), 1)
                self.assertIn("tracker.task_prefix", errors[0])
        errors, _ = self.adapter.validate({"kind": "github-issues"})
        self.assertEqual(errors, ["tracker.repo is required, the owner/name of the GitHub "
                                  "repository the issues live in"])

    def test_a_valid_table_is_usable_and_a_broken_one_is_not(self):
        self.assertTrue(self.adapter.usable(github_issues_table()))
        self.assertTrue(trackers.usable(github_issues_table()))
        for bad in (github_issues_table(repo="o"), github_issues_table(task_prefix="gh"),
                    {"kind": "github-issues"}, None, "o/r"):
            self.assertFalse(self.adapter.usable(bad), bad)
        self.assertFalse(trackers.usable(github_issues_table(repo="https://github.com/o/r")))

    def test_the_denials_cover_any_repo_whatever_the_table_names(self):
        other = self.adapter.write_denials(github_issues_table(repo="x/y", task_prefix="ZZ"))
        self.assertEqual([p.pattern for p in other], [p.pattern for p in self.denials])
        self.assert_denied(["gh issue comment 5 -R someone/else -b x",
                            "gh api repos/someone/else/issues/5/comments -f body=x"])


class FakeGh:
    """A runner that answers each gh call from a list of (code, stdout, stderr) or an
    exception to raise, and records the argv and keyword arguments."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return subprocess.CompletedProcess(argv, code, out, err)


class TestGithubIssuesFetchGh(GithubIssuesCase):
    def fetch(self, gh):
        import tempfile
        from ppl import store
        with mock.patch.object(store, "checked_run_path", side_effect=lambda p: p):
            return self.adapter.fetch({"repo": "o/r"}, "GH-7", tempfile.mkdtemp(), runner=gh)

    def test_gh_runs_by_the_pinned_path(self):
        from ppl import os_sandbox
        gh = FakeGh((1, "", "boom"))
        with mock.patch.object(os_sandbox, "gh_path", return_value="/pin/gh"):
            with self.assertRaises(PplError):
                self.fetch(gh)
        self.assertEqual(gh.calls[0][0][0], "/pin/gh")

    def test_inside_the_sandbox_the_block_names_the_flag(self):
        with mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": "1"}):
            with self.assertRaises(PplError) as ctx:
                self.fetch(FakeGh((1, "", "boom")))
        self.assertIn("this command reaches GitHub, run it with dangerouslyDisableSandbox",
                      str(ctx.exception))
        with mock.patch.dict(os.environ, {"SANDBOX_RUNTIME": ""}):
            with self.assertRaises(PplError) as ctx:
                self.fetch(FakeGh((1, "", "boom")))
        self.assertNotIn("dangerouslyDisableSandbox", str(ctx.exception))


class TestTrackerChoice(GithubIssuesCase):
    def test_repo_from_remote_reads_github_remotes_only(self):
        read = self.adapter.repo_from_remote
        for remote in ("https://github.com/o/r", "https://github.com/o/r.git",
                       "git@github.com:o/r", "git@github.com:o/r.git",
                       "https://github.com/o/r/"):
            self.assertEqual(read(remote), "o/r", remote)
        self.assertEqual(read("https://github.com/o.x/r-1_2.git"), "o.x/r-1_2")
        for remote in (None, "", "https://gitlab.com/o/r.git", "git@gitlab.com:o/r.git",
                       "https://github.com/o", "https://github.com/o/r/extra",
                       "https://notgithub.com/o/r", "https://github.com.evil.io/o/r",
                       "git@github.com:o", "o/r", "https://github.com/o r/x", 5):
            self.assertIsNone(read(remote), remote)

    def test_dot_only_and_dot_led_repo_parts_are_refused(self):
        for repo in ("o/..", "../r", "./r", "o/.", ".o/r", "o/.."):
            errors, _ = self.adapter.validate(github_issues_table(repo=repo))
            self.assertEqual(len(errors), 1, repo)
            self.assertFalse(self.adapter.usable(github_issues_table(repo=repo)), repo)
            self.assertIsNone(self.adapter.repo_from_remote("https://github.com/" + repo), repo)
        for repo in ("o/.github", "o/r.js", "o/r-1_2"):
            self.assertEqual(self.adapter.validate(github_issues_table(repo=repo)), ([], []), repo)
            self.assertEqual(self.adapter.repo_from_remote("git@github.com:%s.git" % repo), repo)

    def test_the_github_issues_doctor_check_runs_auth_then_a_one_issue_list(self):
        gh = FakeGh((0, "", "logged in"), (0, "[]", ""))
        result = self.adapter.doctor_check(github_issues_table(), runner=gh)
        self.assertEqual(result["ok"], True)
        self.assertIn("o/r", result["detail"])
        self.assertEqual([argv for argv, _ in gh.calls],
                         [["gh", "auth", "status"],
                          ["gh", "issue", "list", "-R", "o/r", "-L", "1"]])
        for _, kwargs in gh.calls:
            self.assertEqual(kwargs["timeout"], 20)

    def test_a_failing_gh_call_fails_the_check_and_skips_the_next(self):
        gh = FakeGh((1, "", "not logged in to any host"))
        result = self.adapter.doctor_check(github_issues_table(), runner=gh)
        self.assertFalse(result["ok"])
        self.assertIn("not logged in to any host", result["detail"])
        self.assertEqual(len(gh.calls), 1)
        gh = FakeGh((0, "", ""), (1, "", "Could not resolve to a Repository"))
        result = self.adapter.doctor_check(github_issues_table(), runner=gh)
        self.assertFalse(result["ok"])
        self.assertIn("Could not resolve to a Repository", result["detail"])

    def test_a_missing_gh_fails_the_check(self):
        result = self.adapter.doctor_check(github_issues_table(),
                                           runner=FakeGh(FileNotFoundError("gh")))
        self.assertEqual(result, {"ok": False,
                                  "detail": "gh is not installed or not on PATH"})

    def test_a_gh_that_does_not_answer_fails_the_check(self):
        timeout = subprocess.TimeoutExpired(["gh"], 20)
        result = self.adapter.doctor_check(github_issues_table(), runner=FakeGh(timeout))
        self.assertFalse(result["ok"])
        self.assertIn("20 s", result["detail"])

    def test_a_table_the_adapter_refuses_runs_no_gh(self):
        gh = FakeGh()
        result = self.adapter.doctor_check({"kind": "github-issues"}, runner=gh)
        self.assertFalse(result["ok"])
        self.assertIn("tracker.repo", result["detail"])
        self.assertEqual(gh.calls, [])

    def test_jira_is_not_probed(self):
        self.assertEqual(jira.doctor_check(jira_table(), runner=FakeGh()),
                         {"ok": True, "detail": "not probed"})

    def test_task_defaults_name_the_key_pattern_and_the_issue_url(self):
        self.assertEqual(self.adapter.task_defaults(github_issues_table()),
                         {"key_pattern": "^GH-[0-9]+$",
                          "url_template": "https://github.com/o/r/issues/<TASK-ID-number>"})
        self.assertEqual(self.adapter.task_defaults(
            github_issues_table(task_prefix="ISS", repo="a/b"))["key_pattern"], "^ISS-[0-9]+$")
        self.assertEqual(self.adapter.task_defaults({"kind": "github-issues"}),
                         {"key_pattern": "^GH-[0-9]+$",
                          "url_template": "https://github.com/<repo>/issues/<TASK-ID-number>"})
        self.assertEqual(jira.task_defaults(jira_table()),
                         {"key_pattern": "^[A-Z][A-Z0-9]+-[0-9]+$",
                          "url_template": "https://example.atlassian.net/browse/<TASK-ID>"})


class TestGithubIssuesWriteDenials(GithubIssuesCase):
    def test_every_issue_write_shape_is_denied(self):
        self.assert_denied(
            ["gh issue comment 5 --body-file /tmp/n.md", "gh issue -R o/r comment 5 -b x",
             "gh -R o/r issue close 5", "gh --repo=o/r issue edit 5 --add-label x",
             "GH_REPO=o/r gh issue reopen 5", 'gh issue "close" 5', "gh issue create --title x",
             "gh issue delete 5 --yes"]
            + ["gh issue %s 5" % verb
               for verb in ("transfer", "pin", "unpin", "lock", "unlock", "develop")]
            + ["gh api -X POST repos/o/r/issues/5/comments -f body=x",
               "gh api --method=PATCH repos/o/r/issues/5",
               "gh api repos/o/r/issues/5/comments -f body=x",
               "gh api repos/o/r/issues/5/labels --input l.json"]
            + ["gh api graphql -f query='mutation{%s(input:{}){clientMutationId}}'" % name
               for name in ("addComment", "updateIssue", "closeIssue", "reopenIssue",
                            "deleteIssue", "addLabelsToLabelable", "removeLabelsFromLabelable")]
            + ["gh api graphql --input q.json", "gh issue view $(echo 5)"])

    def test_issue_reads_are_not_denied(self):
        self.assert_allowed(
            ["gh issue view 5 --json title,body", "gh issue list -R o/r -L 1", "gh issue status",
             "gh api repos/o/r/issues/5", "gh api -X GET repos/o/r/issues -f state=open",
             "gh api repos/o/r/pulls/5/comments", 'gh pr comment 5 -b "the issue comment"',
             "gh api graphql -f query='query{repository(owner:\"o\",name:\"r\")"
             "{issue(number:5){title}}}'"]
            # what the pipeline itself runs before the merge, filled in
            + ["gh api graphql -f query='query { repository(owner: \"o\", name: \"r\") { "
               "pullRequest(number: 7) { reviewThreads(first: 100) { nodes { isResolved "
               "comments(first: 1) { nodes { databaseId } } } } } } }'",
               "gh api graphql -f query='{viewer{login}}'",
               "gh api graphql -f query='query($o:String!){repository(owner:$o,name:\"r\")"
               "{issue(number:1){title}}}' -f o=x",
               "gh api repos/o/r/pulls/7/comments/123/replies -f body='Fixed in abc123; "
               "the | pipe & ampersand stay (quoted)'",
               "gh api repos/o/r/pulls/7/comments/123/replies -f body=\"$(cat reply.md)\"",
               "gh api repos/o/r/pulls/7/comments/123/replies -F body=@reply.md",
               "gh api repos/o/r/pulls/7/comments/123/replies --input reply.json",
               "gh api repos/{owner}/{repo}/pulls/7/comments/123/replies -f body='mentions "
               "issues/5 and mutation addComment'",
               "gh api repos/o/r/pulls/7/comments --paginate", "gh api repos/o/r/pulls/7/reviews",
               "gh api user --jq .login", "gh pr checks 7",
               "gh pr view 7 --json=reviews,headRefOid",
               "gh pr create --title \"GH-12 fix the issue\" --body-file /tmp/body.md",
               "gh auth status", "gh auth status --hostname github.com", "gh --version",
               "gh version", "gh help issue", "gh alias list", "gh extension list",
               "gh api https://api.github.com/repos/o/r/issues/5",
               "curl https://api.github.com/repos/o/r/pulls/7", "curl https://api.github.com/user",
               "xargs -I{} gh issue view {}", "{ gh issue view 5; }",
               "gh pr comment 7 --body-file - <<'EOF'\nIt's done, see gh issue #5\nEOF",
               "git commit -m \"it's the gh docs\""])

    def test_the_write_denials_stay_fast_on_backtracking_decoys(self):
        self.assert_fast_and_clean(["gh issue " + "--a 'x' " * 13 + "zzz; gh issue view 1",
                                    "gh " + "-R o/r " * 600 + "issue view 1",
                                    "gh api " + "-f a=b " * 600 + "repos/o/r/pulls/1"])

    def test_a_flag_quote_escape_or_redirect_around_the_verb_still_denies(self):
        self.assert_denied(
            ["gh issue new --title x", "gh 'issue' close 5", "gh \"issue\" comment 5 -b x",
             "gh issue -Ro/r close 5", "gh issue --repo o/r 'close' 5",
             "gh -R o/r issue -R o/r edit 5", "gh --re\"po\" o/r issue close 5",
             "gh -R \"o/r\" issue close 5", "gh issue \\close 5",
             # gh reads -c as close's --comment and view as its value
             "gh issue -c view close 5", "gh issue -\"c\" view close 5",
             "gh issue \"-c\" view close 5", "gh issue \\-c view close 5",
             "gh 2>/dev/null issue close 5", "gh issue >/dev/null close 5",
             "\"gh\" issue close 5", "/usr/bin/gh issue close 5", "command gh issue close 5",
             "bash -c 'gh issue close 5'", "echo ok && gh issue lock 5",
             "gh issue view 5; gh issue close 5", "gh issue comment 5 -b 'a; b'"])

    def test_an_issue_verb_or_a_gh_subcommand_the_shell_would_rewrite_is_denied(self):
        self.assert_denied(
            ["gh issue cl\\ose 5", "gh issue cl\"ose\" 5", "gh issue c''lose 5",
             "gh issue $VERB 5", "gh issue ${VERB} 5", "gh issue -R $R view",
             "gh issue view `echo 5`", "gh issue view \"$(echo 5)\"", "gh issue $'\\x63lose' 5",
             "gh iss\\ue close 5", "gh \"is\"sue close 5", "gh $SUB close 5",
             "gh $(echo issue) close 5",
             # gh refuses these, the denial does not rely on that
             "gh issue close5", "gh issue comment5 -b x", "gh issue CLOSE 5",
             "gh issue 'close'' ' 5", "gh issue close\\ 5", "gh issue 'close 5'"])

    def test_quotes_and_escapes_inside_a_word_are_removed_before_matching(self):
        self.assert_denied(
            ["gh api repos/o/r/is''sues/5/comments -f body=x",
             "gh api repos/o/r/is\"\"sues/5/comments -f body=x",
             "gh api repos/o/r/'issues'/5/comments -f body=x",
             "gh api re''pos/o/r/issues/5/comments -f body=x",
             "gh api repos/o/r/\\issues/5/comments -f body=x",
             "gh api 'repos/o/r/iss'ues/5/comments -f body=x",
             "gh api repos/o/r'/'issues/5/comments -f body=x",
             "gh api repos/o/r/%69ssues/5/comments -f body=x",
             "gh api repos%2Fo%2Fr%2Fissues/5/comments -f body=x",
             "gh api graphql -f query='mutation{add''Comment(input:{}){clientMutationId}}'",
             "gh api graphql -f query='mut''ation{addComment(input:{}){clientMutationId}}'",
             "gh api graph''ql --input q.json", "gh api 'graph'ql --input q.json",
             "gh api repos/o/r/issues/5/comments -\"f\" body=x",
             "gh api repos/o/r/issues/5/comments -\\f body=x",
             "gh api repos/o/r/issues/5/comments --fie\"ld\" body=x",
             "gh api --meth\"od\" POST repos/o/r/issues/5", "gh api -\\X POST repos/o/r/issues/5",
             "gh api -X P\"OST\" repos/o/r/issues/5", "gh api graphql --in\"put\" q.json",
             "gh api graphql -\"F\" query=@m.graphql",
             "g\"h\" issue close 5", "g''h issue close 5", "g\\h issue close 5",
             "gh i\"ssue\" close 5", "gh issue 'clo'se 5", "gh issue \\c\\l\\o\\s\\e 5",
             "gh issue \\\n close 5", "eval 'gh issue close 5'", "sh -c \"gh issue close 5\"",
             "python3 -c \"import os; os.system('gh issue close 5')\"",
             "G=gh; $G issue close 5", "\"$(which gh)\" issue close 5",
             "gh${IFS}issue${IFS}close${IFS}5", "gh$IFS'issue' close 5", "GH issue close 5",
             "/opt/homebrew/bin/GH issue close 5", "echo close 5 | xargs gh issue",
             "echo -f body=x | xargs gh api repos/o/r/issues/5/comments",
             "find . -exec gh issue {} \\;", "parallel gh issue ::: close", "xargs gh"])
        self.assert_allowed(["xargs -I{} gh issue view {}", "echo 5 | xargs gh issue view",
                             "echo $HOME | xargs ls", "ls gh*"])

    def test_a_graphql_document_naming_an_issue_mutation_in_any_spelling_is_denied(self):
        query = "gh api graphql -f query="
        self.assert_denied(
            [query + "'{addComment(input:{}){clientMutationId}}'",
             query + "'MUTATION{addComment(input:{}){clientMutationId}}'",
             query + "'mutation{AddComment(input:{}){clientMutationId}}'",
             query + "'mutation{\\u0061ddComment(input:{}){clientMutationId}}'",
             query + "'mutation{add#c\nComment(input:{}){clientMutationId}}'",
             query + "\"mutation{add\\Comment(input:{}){clientMutationId}}\"",
             query + "'mutation{add\"\"Comment(input:{}){clientMutationId}}'",
             "gh api graphql -f query=@m.graphql", "gh api graphql -F query@=m",
             "gh api graphql -F qu''ery=@m.graphql", "gh api graphql -iF query=@m.graphql",
             "gh api graphql -F query=@m.graphql -H 'X: repos/o/r/pulls/1'",
             "gh api GRAPHQL --input q.json", "gh api graphql/ --input q.json",
             "gh api api.github.com/graphql --input q.json"])

    def test_flag_spellings_gh_refuses_are_still_read_as_the_flag(self):
        # gh rejects these, the denial does not rely on that
        self.assert_denied(["gh api --metho POST repos/o/r/issues/5",
                            "gh api repos/o/r/issues/5/comments --fiel body=x",
                            "gh api repos/o/r/issues/5/comments --raw-f body=x",
                            "gh api repos/o/r/issues/5/comments --inp n.json",
                            "gh api repos/o/r/issues/5/comments -pf body=x",
                            "gh api repos/o/r/issues/5/comments -sf body=x"])

    def test_a_glob_or_brace_expansion_in_a_gh_call_is_unreadable_and_denied(self):
        self.assert_denied(
            ["gh issue {--repo=o/r,close} 5", "gh issue {-Ro/r,comment} 5 -b x",
             "gh {-Ro/r,issue} close 5", "gh issue {close,} 5",
             "gh api {-Hx:,repos/o/r/iss}ues/5/comments -f body=x",
             "gh api repos/o/r/issues/5/comments {-Hx:,-f} body=x",
             "gh api graphql {-Hx:,-F} query=@m.graphql", "gh api graphql -F{x=1,query=@m.graphql}",
             "gh api -X{GET,POST} repos/o/r/issues/5/comments -f body=x",
             "touch close; gh issue clos? 5", "gh issue c[l]ose 5", "gh issue clo* 5",
             "gh i[s]sue close 5", "gh api repos/o/r/issue{s,}/5/comments -f body=x",
             "gh api repos/o/r/issue[s]/5/comments -f body=x", "gh pr view {7,8}",
             "gh api repos/o/r/issu*/5/comments -f body=x", "gh issue {1..3} close"])
        self.assert_allowed(["gh api repos/{owner}/{repo}/pulls/7/comments --paginate",
                             "gh api graphql -f query='{viewer{login}}'",
                             "gh api user --jq '.[0]'", "gh issue view 5 --jq '.title,.body'"])

    def test_a_glob_after_a_literal_jq_flag_stays_a_read(self):
        # whatever the shell makes of such a word keeps its --jq= or -q= prefix, so it
        # cannot become a verb, a subcommand, a method or a route
        self.assert_allowed(
            ["gh api repos/o/r/pulls/12/comments --jq=.[].body",
             "gh api repos/o/r/pulls/12/comments --jq=.[0].id",
             "gh pr view 12 --json=reviews --jq=.reviews[0].state",
             "gh api repos/o/r/pulls/12/comments -q=.[].body",
             "gh api repos/o/r/pulls/12/comments --jq=.[]?.body",
             "gh issue view 7 -R o/r --jq=.labels[].name",
             "gh api repos/o/r/pulls/12/comments --jq='.[].body'",
             "gh api repos/o/r/pulls/12/comments '--jq=.[0].id'"])
        # braces, a glob in any other word, and a prefix that is quoted, expanded or
        # itself a glob stay refused
        self.assert_denied(
            ["gh api repos/o/r/pulls/12/comments --jq={a,b}",
             "gh api repos/o/r/pulls/12/comments --jq=.[{0,1}].id",
             "gh api repos/o/r/pulls/1*/comments --jq=.[].body",
             "gh api repos/o/r/issue[s]/5/comments -f body=x --jq=.[].id",
             "gh api repos/o/r/pulls/12/comments \"--jq=\".[]",
             "gh api repos/o/r/pulls/12/comments \"$J\".[]",
             "gh api repos/o/r/pulls/12/comments --j?=.[]",
             "gh api repos/o/r/pulls/12/comments x--jq=.[]",
             "gh issue clo* 5 --jq=.[].id"])

    def test_an_alias_an_extension_or_a_subcommand_gh_does_not_ship_is_denied(self):
        self.assert_denied(
            ["gh alias set ic 'issue comment'", "gh alias import aliases.yml",
             "gh alias set --clobber ic 'issue comment'", "gh extension install o/gh-x",
             "gh ext install o/gh-x", "gh extensions exec x", "gh extension exec x --flag",
             "gh ic 5 -b x", "gh co 7", "gh ISSUE close 5", "gh -R o/r zzz",
             "gh copilot -p 'comment on issue 5'", "if gh ic 5; then :; fi", "X=1 gh ic 5",
             "env X=1 gh ic 5", "sudo -u x gh co 7", "timeout 5 gh ic 5",
             "echo 5 | xargs -n1 gh ic", "bash -c 'gh ic 5 -b x'", "gh pr view {7,8}"])
        # a gh that is no program, only a word another command reads
        self.assert_allowed(["grep -rn gh .", "rg gh src", "grep gh *.py", "mkdir gh docs",
                             "file /opt/homebrew/bin/gh", "echo gh foo", "echo gh${IFS}a",
                             "git log --grep gh -- docs"])
        self.assertIn("issue", self.adapter.BUILTINS)
        self.assertNotIn("co", self.adapter.BUILTINS)
        self.assertEqual(self.adapter.GH_VERSION, "2.92.0")

    def test_a_token_export_or_another_client_naming_an_issues_endpoint_is_denied(self):
        self.assert_denied(
            ["gh auth token", "gh auth token --hostname github.com", "gh auth status -t",
             "gh auth status --show-token", "gh auth status -at",
             "gh auth status -h github.com --show-token",
             "curl -X POST https://api.github.com/repos/o/r/issues/5/comments -d '{}'",
             "curl https://api.github.com/repos/o/r/issues/5",
             "wget --post-data x https://api.github.com/repos/o/r/issues/5/comments",
             "http POST api.github.com/repos/o/r/issues/5/comments body=x",
             "python3 -c \"import urllib.request as u; "
             "u.urlopen('https://api.github.com/repos/o/r/issues/5/comments', b'{}')\"",
             "curl https://ghe.example/api/v3/repos/o/r/issues/5/comments -d x",
             "curl -X POST https://api.github.com/graphql -d @q.json",
             "curl https://ghe.example/api/graphql -d x",
             "curl \"https://api.github.com/repos/o/r/iss\"\"ues/5/comments\" -d x",
             "curl https://api.github.com/repositories/1/issues/5/comments -d x",
             "curl https://api.github.com/repos/o/r/pulls/7/../../issues/5/comments -d x",
             "curl https://api.github.com/repos/o/r/%69ssues/5/comments -d x"])

    def test_copying_or_linking_the_gh_binary_under_another_name_is_denied(self):
        self.assert_denied(
            ["cp /opt/homebrew/bin/gh /tmp/g", "cp \"$(which gh)\" ~/bin/g",
             "ln -s $(command -v gh) /tmp/g", "install -m 755 `which gh` /tmp/g",
             "rsync -a /usr/local/bin/gh /tmp/x", "mv /opt/homebrew/bin/gh /opt/homebrew/bin/g",
             "cat /opt/homebrew/bin/gh > /tmp/g", "tee /tmp/g < /opt/homebrew/bin/gh",
             "dd if=/opt/homebrew/bin/gh of=/tmp/g", "sudo cp $(which gh) /usr/local/bin/g",
             "ditto /opt/homebrew/bin/GH /tmp/g", "bash -c 'cp $(which gh) /tmp/g'",
             "$(which gh) co 7", "\"$(command -v gh)\" co 7",
             "bash -c \"cp $(which gh) /tmp/g\"", "watch \"cp $(which gh) /tmp/g\"",
             "sh -c \"ln -s `command -v gh` /tmp/g\""])
        self.assert_allowed(["which gh", "command -v gh", "ls -l /opt/homebrew/bin/gh",
                             "cp README.md /tmp/", "gh --version", "type -p gh"])

    def test_a_string_a_runner_hands_to_a_shell_is_read_as_a_command(self):
        # gh co is a user alias, denied only where the string is read as a command
        self.assert_denied(
            ["watch -n1 'gh co 7'", "env -S 'gh co 7'", "env --split-string='gh co 7'",
             "env -S'gh co 7'", "script -q -c 'gh co 7' /dev/null", "flock /tmp/l -c 'gh co 7'",
             "ssh host 'gh co 7'", "su -c 'gh co 7' me", "runuser -u me -c 'gh co 7'",
             "sudo -s 'gh co 7'", "sudo sh -c 'gh co 7'", "bash -c \"gh${IFS}co${IFS}7\"",
             "watch 'gh issue close 5'", "ssh host 'gh issue comment 5 -b x'",
             "env -S 'gh api repos/o/r/issues/5/comments -f body=x'"])
        self.assert_allowed(["watch -n5 'gh pr checks 7'", "ssh host 'ls /tmp'",
                             "env -S 'echo hi'", "echo 'gh co 7'"])

    def test_a_write_to_the_gh_config_or_gh_config_set_is_denied(self):
        self.assert_denied(
            ["gh config set editor vim", "gh config set -h github.com git_protocol ssh",
             "echo 'aliases: {}' > ~/.config/gh/config.yml",
             "printf x >> \"$HOME/.config/gh/config.yml\"", "tee ~/.config/gh/config.yml < x",
             "cp /tmp/c.yml ~/.config/gh/config.yml", "mv /tmp/c.yml $GH_CONFIG_DIR/config.yml",
             "sed -i '' 's/a/b/' ~/.config/gh/config.yml",
             "cp x \"${XDG_CONFIG_HOME}/gh/config.yml\"", "cd ~/.config/gh && echo x > config.yml",
             "> ~/.config/gh/hosts.yml", "sudo tee -a /Users/me/.config/gh/config.yml < x"])
        self.assert_denied(["cp /tmp/x.yml ~/.config/gh/", "rm -rf ~/.config/gh",
                            "cp -t ~/.config/gh /tmp/c.yml", "touch ~/.config/gh/x",
                            "perl -pi -e 's/a/b/' ~/.config/gh/config.yml",
                            # macOS file systems ignore case
                            "echo x > ~/.CONFIG/GH/config.yml", "cp x ~/.Config/Gh/config.yml",
                            # every operand of a copy may be its target
                            "cp src ~/.config/gh/ -v", "cp -v src ~/.config/gh/ --",
                            "cp -t~/.config/gh/ src", "install src ~/.config/gh/x -m 600",
                            "rsync -a src ~/.config/gh/ --delete",
                            "cp ~/.config/gh/config.yml /tmp/backup.yml"])
        self.assert_allowed(["gh config get editor", "gh config list", "gh config clear-cache",
                             "cat ~/.config/gh/config.yml", "ls ~/.config/gh",
                             "sed -n 1p ~/.config/gh/config.yml",
                             "mkdir -p ~/.config/other", "cat file > out"])

    def test_the_api_host_is_read_in_any_case_and_with_a_trailing_dot(self):
        self.assert_denied(["curl https://api.github.com./repos/o/r/issues/5/comments -d x",
                            "curl https://API.GITHUB.COM/repos/o/r/issues/5/comments -d x",
                            "curl https://Api.GitHub.Com.:443/graphql -d x",
                            "wget api.github.com./repos/o/r/issues/1"])

    def test_the_module_states_the_lexical_limit(self):
        self.assertIn("The check reads the command's words, so a renamed gh binary, an API "
                      "host held in a variable, an interpreter or a script file, and a gh "
                      "config the session changed outside Bash are not seen. The boundary that "
                      "would close them is a GitHub credential the role sessions never hold.",
                      " ".join(self.adapter.__doc__.split()))

    def test_a_blank_bash_does_not_split_on_is_part_of_the_word(self):
        # bash splits on space, tab and newline only, so these stay inside the word
        self.assert_allowed(["gh pr view 7\xa0", "echo a\x0cb", "echo  x",
                             "gh\x0bissue close 5"])
        self.assert_denied(["gh issue close\xa05", "gh issue comment 5 -b \xa0x\x0b"])

    def test_an_answer_read_past_the_depth_limit_is_not_reused_for_a_shallow_read(self):
        # past the limit the text only gets the loose reading, which does not split
        # gh${IFS}issue, while a shallow reading does
        text = "gh${IFS}issue${IFS}close${IFS}5"
        self.adapter._SEEN.clear()
        try:
            self.assertIsNone(self.adapter._check_text(text, False, self.adapter._MAX_DEPTH + 1))
            self.assertTrue(self.adapter._check_text(text, False, 1))
        finally:
            self.adapter._SEEN.clear()

    def test_a_defect_in_the_check_denies_a_command_naming_gh_instead_of_raising(self):
        with mock.patch.object(self.adapter, "_check_text", side_effect=RuntimeError("defect")):
            self.assertTrue(self.denied("gh issue close 5"))
            self.assertTrue(self.denied("curl https://api.github.com/graphql -d x"))
            self.assertFalse(self.denied("ls -la"))

    def test_a_check_past_its_time_budget_reads_as_a_write(self):
        # a hook past its own timeout lets the call through, so a slow reading denies
        with mock.patch.object(self.adapter, "READ_BUDGET_SECONDS", 0):
            for command in ("ls -la", "gh pr checks 12", "echo api.github.com/repos/a/b/"):
                with self.subTest(command=command):
                    self.assertTrue(self.denied(command))
                    self.assertIn("took too long to read", self.adapter.write_reason(command))
        self.assertEqual(self.adapter.READ_BUDGET_SECONDS, 0.5)
        self.assertFalse(self.denied("ls -la"))

    def test_the_budget_is_read_on_the_clock_between_words_and_commands(self):
        def clock_over_after(reads, limit):
            """A clock at 0 for its first `limit` reads, then past any 0.5 s deadline."""
            def clock():
                reads.append(None)
                return 0.0 if len(reads) <= limit else 1.0
            return clock

        short, padded, twice = "echo a", "echo " + "a " * 50, "echo a; echo a"
        counts = {}
        for command in (short, padded, twice):
            reads = []
            with mock.patch.object(self.adapter, "_clock", clock_over_after(reads, 10 ** 6)):
                self.assertIsNone(self.adapter.write_reason(command))
            counts[command] = len(reads)
        # a read for every word and every command, not one at the start
        self.assertGreater(counts[padded], counts[short] + 50)
        self.assertGreater(counts[twice], counts[short])
        # past the deadline once the short command's reads are spent: the short one is
        # read whole, the padded one stops at the next word
        reads = []
        with mock.patch.object(self.adapter, "_clock", clock_over_after(reads, counts[short])):
            self.assertIsNone(self.adapter.write_reason(short))
        reads = []
        with mock.patch.object(self.adapter, "_clock", clock_over_after(reads, counts[short])):
            self.assertIn("took too long to read", self.adapter.write_reason(padded))
        self.assertEqual(len(reads), counts[short] + 1)
        with mock.patch.object(self.adapter, "_clock", lambda: 0.0):
            self.assertEqual(self.adapter.write_reason("gh issue close 5"),
                             "gh issue close writes the issue")

    def test_a_reading_outside_a_search_has_no_budget(self):
        with mock.patch.object(self.adapter, "READ_BUDGET_SECONDS", 0):
            self.assertTrue(self.denied("ls"))
            self.adapter._SEEN.clear()
            try:
                self.assertIsNone(self.adapter._check_text("ls -la", True, 0))
            finally:
                self.adapter._SEEN.clear()

    def test_a_command_the_shell_cannot_split_denies_when_it_could_reach_gh(self):
        self.assert_denied(["gh issue close 5 'unterminated", "echo \"x; gh issue close 5",
                            "gh issue view $(echo 5", "gh issue view `echo 5",
                            "g\"h issue close 5", "curl https://api.github.com/graphql 'x",
                            "bash <<'EOF'\ngh issue close 5\nEOF",
                            "bash <<'EOF'\necho it's\ngh issue close 5\nEOF",
                            "case x in x) gh issue close 5;; esac"])
        self.assert_allowed(["echo 'unterminated", "echo \"it's ok"])

    def test_a_rest_issue_write_is_denied_however_the_route_and_method_are_spelled(self):
        self.assert_denied(
            ["gh api -XPOST repos/o/r/issues/5/comments", "gh api -X=post repos/o/r/issues/5",
             "gh api -X 'DELETE' repos/o/r/issues/comments/9",
             "gh api -iX PUT repos/o/r/issues/5/lock", "gh api \"-X\" PATCH repos/o/r/issues/5",
             "gh api --method \"POST\" repos/o/r/issues", "gh -X POST api repos/o/r/issues",
             "gh api repos/{owner}/{repo}/issues/5/comments -F body=@n.md",
             "gh api repos/:owner/:repo/issues/5/comments -f body=x",
             "gh api /repos/o/r/issues/5/comments -fbody=x",
             "gh api https://api.github.com/repos/o/r/issues/5/comments --raw-field body=x",
             "gh api 'repos/o/r/issues/5/comments' -f body=x",
             "gh api \"https://ghe.example/api/v3/repos/o/r/issues/5/comments\" --field=body=x",
             "gh api -f body='a;b' repos/o/r/issues/5/comments",
             "gh api -f body=\"a | b && c\" repos/o/r/issues/5/comments",
             "gh api repos/o/r/issues/5/comments -f body='use -X GET'",
             "gh api repositories/123/issues/5/comments -f body=x",
             "gh api repos/o/r/pulls/7/../../issues/5/comments -f body=x",
             "gh api Repos/o/r/Issues/5/comments -f body=x",
             "gh api repos/o/r/issues/5/comments --input=-",
             "echo '{}' | gh api repos/o/r/issues/5/comments --input -",
             "gh api repos/o/r/issues -f title=x; gh api -X GET repos/o/r/issues",
             "gh api repos/o/r/issues/5/comments 2>&1 -f body=x",
             "gh api -X POST 2>&1 repos/o/r/issues/5/comments",
             "gh api repos/o/r/issues/5/comments -f body='line one\nline two'",
             "gh api repos/o/r/issues/5/comments \\\n  -f body=x"])

    def test_a_get_or_a_pull_request_route_keeps_a_rest_call_open(self):
        self.assert_allowed(
            ["gh api -X HEAD repos/o/r/issues/5", "gh api --method get repos/o/r/issues -F a=1",
             "gh api repos/o/r/issues/5 -X 'GET' -f x=y", "gh api -XGET repos/o/r/issues -f a=b",
             "gh api repos/o/r/issues/5 --jq .body", "gh api repos/o/r/issues --paginate",
             "gh api repos/o/r/pulls/7/comments/1/replies -f body=hi",
             "gh api -X POST repos/o/r/pulls/7/comments/1/replies -f body=hi",
             "gh api repos/o/r/pulls/7/comments/1/replies -f body='see "
             "https://github.com/o/r/issues/12'",
             "gh api repos/o/r/pulls/7/comments/1/replies -f body=\"$(cat reply.md)\"",
             "gh api user --jq .login",
             "gh api repos/o/r/pulls/7/reviews -f event=COMMENT"])

    def test_every_issue_mutation_and_an_unreadable_graphql_document_is_denied(self):
        self.assert_denied(
            ["gh api graphql -f query='mutation { %s(input: {}) { clientMutationId } }'" % name
             for name in ISSUE_MUTATIONS]
            + ["gh api graphql -f query='mutation($b: String!) {\n  addComment(input: "
               "{body: $b}) { clientMutationId }\n}' -f b=x",
               "gh api graphql -F b='a;b' -f query='mutation{closeIssue(input:{}){issue{id}}}'",
               "gh api /graphql -f query='mutation{createIssue(input:{}){issue{id}}}'",
               "gh api -X POST https://api.github.com/graphql -f "
               "query='mutation{deleteIssue(input:{}){clientMutationId}}'",
               "gh api graphql -F query=@m.graphql", "gh api graphql -Fquery=@m.graphql",
               "gh api graphql --field 'query=@m.graphql'", "gh api graphql -F query=@-",
               "gh api graphql --input=q.json",
               "gh api \"https://api.github.com/graphql\" --input q.json",
               "gh api graphql -f query=\"$(cat m.graphql)\"",
               "gh api graphql -f query=\"$QUERY\""])
        self.assert_allowed(
            ["gh api graphql -f query='mutation { addPullRequestReviewThreadReply(input: {}) "
             "{ clientMutationId } }'",
             "gh api graphql -f query='query { repository(owner: \"o\", name: \"r\") { "
             "issue(number: 5) { comments(first: 5) { nodes { body } } } } }'",
             "gh api graphql -f query='query($n: Int!) { repository(owner: \"o\", "
             "name: \"r\") { issue(number: $n) { title } } }' -F n=5"])

    def test_an_expansion_hides_a_gh_api_call_unless_it_names_a_pull_request_route(self):
        self.assert_denied(["gh api $EP -f body=x", "gh api $(cat args)",
                            "gh api repos/o/r/$KIND/5/comments -f body=x",
                            "gh api \"repos/o/r/${KIND}/5/comments\" -f body=x",
                            "gh api `cat endpoint` --input body.json",
                            "gh api repos/o/r/commits/$SHA --input <(echo x)"])
        self.assert_allowed(["gh api repos/o/r/issues/5 --jq '.body | $x'",
                             "gh api user --jq '.login'"])

    def test_the_pulls_exception_needs_a_literal_route_and_no_word_an_expansion_can_add(self):
        self.assert_denied(
            ["gh api repos/o/r/pulls/7/../../$K/5/comments -f body=x",
             "gh api repos/o/r/pulls/7/../../iss$E/5/comments -f body=x",
             "gh api repos/o/r/pulls/./7/comments/1/replies -f body=\"$(cat r)\"",
             "gh api repos/o/r/pulls/%2e%2e/x/comments -f body=\"$(cat r)\"",
             "gh api repos/o/r/pulls/$N/comments", "gh api \"repos/$OWNER/$REPO/pulls/7/reviews\"",
             "gh api repos/$REPO/pulls/$N/comments --paginate", "gh api repos/o/r/pulls/1 $EXTRA",
             "gh api https://evil/repos/o/r/pulls/1 -f body=$X",
             "gh api $ROUTE -f body=x repos/o/r/pulls/1",
             "gh api graphql -f query=\"$Q\" repos/o/r/pulls/1/",
             "gh api repos/o/r/pulls/1/comments/2/replies -f body=\"$(cat r)\" "
             "$(echo --hostname x)",
             "gh api repos/o/r/issues/5/comments -f body=\"$(cat x)\" -H 'X: repos/o/r/pulls/1'"])

    def test_the_github_forge_fragment_commands_pass_the_tracker_denials(self):
        # The review loop runs before the merge is observed, so a denial here
        # would stop it.
        github = forges.get("github")
        # the include markers filled as a render fills them, the pinned gh as it is named
        markers = {"os/gh": "/opt/pin/bin/gh", "os/forge-repo": "o/r",
                   "harness/scratch": "/tmp/s", "harness/default-branch": "main"}
        spans = [re.sub(r"<!-- include: ([a-z/-]+) -->", lambda m: markers[m.group(1)], span)
                 for name in ("stage6", "stage7", "review-loop")
                 for span in re.findall(r"`(<!-- include: os/gh -->[^`]+)`", github.fragment(name))]
        self.assertTrue(spans)
        values = {"<number>": "7", "<id>": "123", "<branch>": "GH-12-fix",
                  "<TASK-ID>": "GH-12", "<summary>": "fix the issue view"}
        filled = [re.sub(r"<[A-Za-z-]+>", lambda m: values[m.group(0)], span) for span in spans]
        self.assert_allowed(spans + filled)

    def test_prose_naming_gh_issue_is_not_a_write(self):
        self.assert_allowed(
            ["git commit -m 'trackers: deny gh issue writes'",
             "git commit -m \"gh issue numbers are digits\"", "echo \"gh issue\"",
             "rg -n 'gh issue' docs", "grep -rn \"gh api\" ppl", "gh pr view 7 --json=title",
             "gh pr create --title \"Fix the issue view\" --body-file x", "gh --version",
             "gh auth status", "brew upgrade gh", "cd gh && ls", "gh issue", "gh issue --help",
             "gh issue -R o/r view 5", "gh issue ls", "gh issue view 5 --comments",
             "gh issue list --search 'close it'", "gh issue view 5 --jq '.body | $x'",
             "gh issue view 5; echo $HOME", "gh issue view 5 2>&1"])

    def test_the_write_denials_stay_fast_on_padded_commands(self):
        # 4 KB, the role command cap
        self.assert_fast_and_clean(
            ["gh api -X POST" + " x" * 2000, "gh api -f x" + " -f y" * 800,
             "gh api repos/" + "repos/" * 680, "gh api " + "repos/x/" * 500,
             "gh api -f '" + "a" * 4000 + "'", "gh -R" + " " * 4000 + "x",
             "gh issue -R" + " " * 4000 + "x", "gh issue " + "-a b " * 800 + "view",
             "gh api repos/o/r/issues " + "-X GET " * 600, "echo " + "'gh a' " * 580,
             "echo " + "\"$(true)\" " * 450, "echo " + "x" * 4000, "true;" * 800,
             "echo " + "a\\ " * 1300, "cat <<E\n" + "x\n" * 2000 + "E",
             "echo " + "$(" * 1000 + ")" * 1000, "echo " + "'a b gh c' " * 400,
             "gh api graphql -f query='query{" + "a{" * 1000 + "}" * 1000 + "}'",
             "curl " + "https://api.github.com/x " * 160, "echo " + "`" * 4000,
             # a run of slashes, or of encoded ones, after an API route prefix
             "echo api.github.com/repos/a/b/" + "/" * 4000,
             "echo /api/v3/repos/a/b/" + "/" * 4000,
             "echo api.github.com/repositories/1/" + "/" * 4000,
             "echo api.github.com/repos/o/r/" + "%2F" * 1300])
        # a command naming gh this many times is no command the pipeline runs, and
        # a gh command the shell cannot split is denied
        self.assert_fast(["gh api " * 585, "gh issue " * 455, "gh " * 1365, "gh api r " * 455,
                          "gh api - " * 455, "gh api 'r' " * 370, "gh api \"-\" " * 370,
                          "gh api \"" + "x" * 4000, "echo \"" + "$(gh " * 600 + "\""], True)


class TestWriteToolRegex(unittest.TestCase):
    def setUp(self):
        self.pattern = jira.write_tool_regex(PREFIXES, WRITES)

    def test_every_default_write_name_is_accepted(self):
        names = jira.write_tool_names(PREFIXES, WRITES)
        self.assertEqual(len(names), len(PREFIXES) * len(WRITES))
        for name in names:
            self.assertTrue(self.pattern.match(name), name)

    def test_numbered_duplicates_of_every_prefix_are_accepted(self):
        for name in ("mcp__atlassian_2__editJiraIssue",
                     "mcp__atlassian_13__transitionJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo_2__addCommentToJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo_10__addWorklogToJiraIssue"):
            self.assertTrue(self.pattern.match(name), name)

    def test_reads_other_servers_and_near_misses_are_not_accepted(self):
        for name in ("mcp__atlassian__getJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo__getConfluencePage",
                     "mcp__other__editJiraIssue",
                     "mcp__atlassian__editJiraIssueX",
                     "mcp__atlassian__XeditJiraIssue",
                     "mcp__atlassian_x__editJiraIssue",
                     "mcp__atlassian_2_3__editJiraIssue",
                     "mcp__claude_ai_Atlassian_Rovo_a__editJiraIssue",
                     "Xmcp__atlassian__editJiraIssue",
                     "atlassian__editJiraIssue",
                     "editJiraIssue",
                     ""):
            self.assertFalse(self.pattern.match(name), name)

    def test_a_custom_prefix_replaces_the_defaults(self):
        custom = jira.write_tool_regex(["mcp__corp_jira__"], ["editJiraIssue"])
        self.assertTrue(custom.match("mcp__corp_jira__editJiraIssue"))
        self.assertTrue(custom.match("mcp__corp_jira_2__editJiraIssue"))
        self.assertFalse(custom.match("mcp__corp_jira__transitionJiraIssue"))
        self.assertFalse(custom.match("mcp__atlassian__editJiraIssue"))

    def test_a_prefix_not_shaped_like_an_mcp_server_is_matched_literally(self):
        odd = jira.write_tool_regex(["corp_", "mcp__x", "a.b__"], ["editJiraIssue"])
        for name in ("corp_editJiraIssue", "mcp__xeditJiraIssue", "a.b__editJiraIssue"):
            self.assertTrue(odd.match(name), name)
        for name in ("corp_2_editJiraIssue", "aXb__editJiraIssue", "mcp__x__editJiraIssue"):
            self.assertFalse(odd.match(name), name)

    def test_no_prefix_or_no_operation_matches_nothing(self):
        self.assertFalse(jira.write_tool_regex([], WRITES).match("editJiraIssue"))
        self.assertFalse(jira.write_tool_regex(PREFIXES, []).match("mcp__atlassian__"))
        self.assertFalse(jira.write_tool_regex([], []).match(""))

    def test_the_regex_accepts_every_name_write_tool_names_produces(self):
        # The gate must never be narrower than the names the adapter enumerates,
        # whatever the prefix list looks like.
        fixed = [PREFIXES, ["mcp__corp_jira__"], ["mcp__corp_jira"], ["corp_"], ["mcp__x"],
                 [""], ["mcp__a.b__"], ["mcp__a(b__", "mcp__c*__"], ["mcp__a_2__"],
                 ["mcp__a__", "mcp__a_2__", "plain"], ["mcp____"], ["mcp__a-b__", "x\\y"]]
        rng = random.Random(7)
        alphabet = "ab_-.*+()[]\\|$^ 12"
        generated = [["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 8)))
                      for _ in range(rng.randint(1, 4))] for _ in range(300)]
        operations = ["editJiraIssue", "add.Comment", "a|b", "w(x)"]
        for prefixes in fixed + generated:
            pattern = jira.write_tool_regex(prefixes, operations)
            for name in jira.write_tool_names(prefixes, operations):
                self.assertTrue(pattern.match(name), (prefixes, name))


class TestDefaults(unittest.TestCase):
    """The default lists cover the names the server has used across builds, so an
    older or newer registration is gated."""

    def test_the_write_operations_cover_the_current_and_the_older_names(self):
        for name in ("addOrEditJiraIssueComment", "editJiraIssue", "transitionJiraIssue",
                     "createJiraIssue", "executeWrite", "executeDestructive",
                     "createConfluenceContent", "updateConfluenceContent", "addGraphContext",
                     "addCommentToJiraIssue", "addWorklogToJiraIssue",
                     "createConfluencePage", "updateConfluencePage",
                     "createConfluenceFooterComment", "createConfluenceInlineComment"):
            self.assertIn(name, WRITES)

    def test_the_read_tools_cover_the_current_and_the_older_names(self):
        for name in ("getJiraIssue", "getConfluenceContent", "searchJiraIssuesUsingJql",
                     "searchConfluence", "executeRead", "getConfluencePage"):
            self.assertIn(name, jira.DEFAULTS["read_tools"])

    def test_no_read_tool_is_also_a_write_operation(self):
        self.assertEqual(set(jira.DEFAULTS["read_tools"]) & set(WRITES), set())

    def test_the_default_lists_hold_no_duplicates(self):
        self.assertEqual(len(set(WRITES)), len(WRITES))
        self.assertEqual(len(set(jira.DEFAULTS["read_tools"])),
                         len(jira.DEFAULTS["read_tools"]))

    def test_the_current_write_names_match_under_both_prefixes_and_a_numbered_duplicate(self):
        pattern = jira.write_tool_regex(PREFIXES, WRITES)
        for name in ("mcp__atlassian__addOrEditJiraIssueComment",
                     "mcp__claude_ai_Atlassian_Rovo__createJiraIssue",
                     "mcp__atlassian__executeWrite",
                     "mcp__claude_ai_Atlassian_Rovo__executeDestructive",
                     "mcp__atlassian_2__createConfluenceContent",
                     "mcp__claude_ai_Atlassian_Rovo_3__updateConfluenceContent"):
            self.assertTrue(pattern.match(name), name)

    def test_the_current_read_names_do_not_match_the_write_pattern(self):
        pattern = jira.write_tool_regex(PREFIXES, WRITES)
        for name in jira.read_tool_names(PREFIXES, jira.DEFAULTS["read_tools"]):
            self.assertFalse(pattern.match(name), name)


class TestNameExpansion(unittest.TestCase):
    def test_read_tool_names_expand_every_prefix_by_every_tool(self):
        self.assertEqual(
            jira.read_tool_names(PREFIXES, ["getJiraIssue", "getConfluenceContent"]),
            ["mcp__atlassian__getJiraIssue", "mcp__atlassian__getConfluenceContent",
             "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
             "mcp__claude_ai_Atlassian_Rovo__getConfluenceContent"])

    def test_the_default_read_tools_expand_to_one_name_per_prefix_and_tool(self):
        names = jira.read_tool_names(PREFIXES, jira.DEFAULTS["read_tools"])
        self.assertEqual(len(names), len(PREFIXES) * len(jira.DEFAULTS["read_tools"]))
        self.assertEqual(len(set(names)), len(names))

    def test_read_tool_names_of_nothing_is_empty(self):
        self.assertEqual(jira.read_tool_names([], ["getJiraIssue"]), [])
        self.assertEqual(jira.read_tool_names(PREFIXES, []), [])

    def test_write_tool_names_expand_every_prefix_by_every_operation(self):
        names = jira.write_tool_names(["mcp__a__", "mcp__b__"], ["x", "y"])
        self.assertEqual(names, ("mcp__a__x", "mcp__a__y", "mcp__b__x", "mcp__b__y"))


if __name__ == "__main__":
    unittest.main()

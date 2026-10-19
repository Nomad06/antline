import random
import re
import time
import unittest

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
                         "write_denials"):
                self.assertTrue(callable(getattr(adapter, name, None)), (kind, name))

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

    def assert_fast_and_clean(self, commands):
        # A hook that runs past its timeout denies nothing, so a command that makes
        # a pattern backtrack is a bypass, not a slowdown.
        for command in commands:
            with self.subTest(command=command[:40], length=len(command)):
                start = time.perf_counter()
                hits = [pattern.pattern[:60] for pattern in self.denials
                        if pattern.search(command)]
                self.assertLess(time.perf_counter() - start, 0.2)
                self.assertEqual(hits, [])


class TestGithubIssuesAdapter(GithubIssuesCase):
    def test_the_adapter_writes_through_gh_and_holds_no_mcp_tool(self):
        self.assertEqual(self.adapter.TRANSPORT, "cli")
        self.assertEqual(self.adapter.DEFAULTS, {"task_prefix": "GH"})
        self.assertEqual(self.adapter.KEYS, ("kind", "repo", "task_prefix"))
        self.assertEqual(self.adapter.mcp_read_tools(github_issues_table()), [])
        pattern = self.adapter.mcp_write_regex(github_issues_table())
        for name in ("", "mcp__github__add_issue_comment", "mcp__atlassian__editJiraIssue",
                     "Bash"):
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
             "{issue(number:5){title}}}'"])

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
             "gh $(echo issue) close 5"])

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
             "gh api repos/$REPO/pulls/$N/comments --paginate",
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
        self.assert_allowed(["gh api repos/o/r/pulls/$N/comments",
                             "gh api \"repos/$OWNER/$REPO/pulls/7/reviews\"",
                             "gh api repos/o/r/issues/5 --jq '.body | $x'",
                             "gh api user --jq '.login'"])

    def test_the_github_forge_fragment_commands_pass_the_tracker_denials(self):
        # The review loop runs before the merge is observed, so a denial here
        # would stop it.
        github = forges.get("github")
        spans = [span for name in ("stage6", "stage7", "review-loop")
                 for span in re.findall(r"`(gh [^`]+)`", github.fragment(name))]
        self.assertTrue(spans)
        self.assert_allowed(spans)

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
        self.assert_fast_and_clean(
            ["gh api " * 585, "gh issue " * 455, "gh " * 1365, "gh api r " * 455,
             "gh api - " * 455, "gh api 'r' " * 370, "gh api \"-\" " * 370,
             "gh api -X POST " * 270, "gh api mutation " * 250, "gh api graphql " * 270,
             "gh api -f x " * 340, "gh api m a c d r u " * 200, "gh api repos/" + "repos/" * 680,
             "gh api " + "repos/x/" * 500, "gh api -f '" + "a" * 4000 + "'",
             "gh api \"" + "x" * 4000, "gh -R" + " " * 4000 + "x",
             "gh issue -R" + " " * 4000 + "x", "gh issue " + "-a b " * 800 + "view",
             "gh api repos/o/r/issues " + "-X GET " * 600])


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

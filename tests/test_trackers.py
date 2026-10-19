import random
import unittest

from ppl import trackers
from ppl.trackers import jira

PREFIXES = jira.DEFAULTS["mcp_prefixes"]
WRITES = jira.DEFAULTS["write_operations"]


class TestRegistry(unittest.TestCase):
    def test_jira_is_the_only_kind_and_resolves_to_the_adapter(self):
        self.assertEqual(trackers.KINDS, ("jira",))
        self.assertIs(trackers.get("jira"), jira)

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

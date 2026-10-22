import json
import os
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ppl" / "_assets" / "hooks"))

import fixtures
from ppl import boundary, harness, paths, run, store
import ppl_guard

TASK = "PYRUN-1"
REPO = "/tmp/pyrepo"
PYTHON = ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]
PRODUCT = REPO + "/src/shop/cart.py"
TEST = REPO + "/tests/unit/test_cart.py"


def payload(agent, tool, tool_input, cwd=REPO):
    return {"hook_event_name": "PreToolUse", "agent_type": agent,
            "tool_name": tool, "tool_input": tool_input, "cwd": cwd}


def snapshot_reason():
    """The one wording every refusal over an untrusted snapshot carries."""
    return boundary.unknown_patterns_reason(TASK)


def python_snapshot():
    """What `ppl init` freezes from example-generic once its [tests] lists PYTHON."""
    snap = harness.snapshot(harness.load("example-generic"))
    snap["tests"] = {"sources": list(PYTHON)}
    return snap


class TestPythonRun(unittest.TestCase):
    """A run whose snapshot names Python test globs: the guard splits tests from
    product code by those globs, not by the Java defaults."""

    @classmethod
    def setUpClass(cls):
        if not run.state_path(TASK).exists():
            run.init(TASK, "u", REPO)
        store.write_json(paths.run_dir(TASK) / "boundary.json",
                         {"write": ["src/**"], "forbid": []})
        fixtures.install_harness("example-generic")
        store.write_json(cls.snapshot_path(), python_snapshot())

    @staticmethod
    def snapshot_path():
        return paths.run_dir(TASK) / "harness.json"

    def setUp(self):
        run.set_active(TASK)

    def deny_reason(self, result):
        self.assertIsNotNone(result, "the call was allowed")
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    def replace_snapshot(self, text):
        saved = self.snapshot_path().read_text(encoding="utf-8")
        self.snapshot_path().write_text(text, encoding="utf-8")
        return saved

    def test_the_test_worker_may_write_a_python_test(self):
        self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Write",
                                                   {"file_path": TEST})))

    def test_the_test_worker_may_not_write_product_code(self):
        # rule: R-ROLE-TEST-WORKER-MAIN
        reason = self.deny_reason(ppl_guard.decide(payload("ppl-test-worker", "Write",
                                                           {"file_path": PRODUCT})))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("may only write test sources", reason)

    def test_the_code_worker_may_not_write_a_python_test(self):
        # rule: R-ROLE-CODE-WORKER-TESTS
        # src/shop/test_cart.py sits inside the write boundary, so only the
        # Python globs can refuse it.
        for target in (TEST, REPO + "/src/shop/test_cart.py"):
            with self.subTest(target=target):
                reason = self.deny_reason(ppl_guard.decide(payload(
                    "ppl-code-worker", "Write", {"file_path": target})))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("may not write test sources", reason)
                reason = self.deny_reason(ppl_guard.decide(payload(
                    "ppl-code-worker", "Bash", {"command": "echo x > %s" % target})))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("may not write test sources", reason)

    def test_the_test_worker_reads_what_it_may_write(self):
        # rule: R-TEST-WORKER-READ-TOOLS
        self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Read",
                                                   {"file_path": TEST})))
        self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Grep",
                                                   {"pattern": "cart", "path": REPO + "/tests"})))
        for tool, tool_input in (("Read", {"file_path": PRODUCT}),
                                 ("Grep", {"pattern": "cart", "path": REPO + "/pkg/tests"})):
            with self.subTest(tool=tool, target=tool_input):
                reason = self.deny_reason(ppl_guard.decide(payload("ppl-test-worker", tool,
                                                                   tool_input)))
                self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_bash_reads_follow_the_python_patterns(self):
        # rule: R-TEST-WORKER-BASH-READS
        self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                   {"command": "cat tests/unit/test_cart.py"})))
        reason = self.deny_reason(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "cat src/shop/cart.py"})))
        self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_a_corrupt_snapshot_denies_both_workers_writes(self):
        # rule: R-TEST-PATTERNS-FROM-SNAPSHOT
        saved = self.replace_snapshot("{not json")
        try:
            calls = (("ppl-code-worker", "Write", {"file_path": PRODUCT}),
                     ("ppl-code-worker", "Bash", {"command": "echo x > src/shop/cart.py"}),
                     ("ppl-test-worker", "Write", {"file_path": TEST}))
            for agent, tool, tool_input in calls:
                with self.subTest(agent=agent, tool=tool):
                    reason = self.deny_reason(ppl_guard.decide(payload(agent, tool,
                                                                       tool_input)))
                    self.assertIn("OWNER_DECISION", reason)
                    self.assertIn("harness.json", reason)
                    self.assertIn(snapshot_reason(), reason)
        finally:
            self.snapshot_path().write_text(saved, encoding="utf-8")

    def test_a_malformed_tests_table_denies_the_test_workers_write_and_read(self):
        # rule: R-TEST-PATTERNS-FROM-SNAPSHOT
        snap = python_snapshot()
        snap["tests"] = {"sources": []}
        saved = self.replace_snapshot(json.dumps(snap))
        try:
            reason = self.deny_reason(ppl_guard.decide(payload("ppl-test-worker", "Write",
                                                               {"file_path": TEST})))
            self.assertIn("OWNER_DECISION", reason)
            self.assertIn("harness.json", reason)
            self.assertIn(snapshot_reason(), reason)
            # the read deny names the snapshot too, not "main sources" (M1)
            for tool, tool_input in (("Read", {"file_path": TEST}),
                                     ("Bash", {"command": "cat tests/unit/test_cart.py"})):
                with self.subTest(tool=tool):
                    reason = self.deny_reason(ppl_guard.decide(payload("ppl-test-worker", tool,
                                                                       tool_input)))
                    self.assertIn("BOUNDARY_VIOLATION", reason)
                    self.assertIn(snapshot_reason(), reason)
        finally:
            self.snapshot_path().write_text(saved, encoding="utf-8")

    def test_a_corrupt_snapshot_denies_the_test_worker_dispatch(self):
        # rule: R-TEST-PATTERNS-FROM-SNAPSHOT
        saved = self.replace_snapshot("{not json")
        try:
            dispatch = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": REPO,
                        "tool_input": {"description": "write the tests",
                                       "prompt": "AC-1: an empty cart totals zero.",
                                       "subagent_type": "ppl-test-worker"}}
            reason = self.deny_reason(ppl_guard.decide(dispatch))
            self.assertIn("OWNER_DECISION", reason)
            self.assertIn("harness.json", reason)
            self.assertIn(snapshot_reason(), reason)
        finally:
            self.snapshot_path().write_text(saved, encoding="utf-8")

    def test_a_snapshot_from_before_phase_two_reads_as_the_java_defaults(self):
        snap = python_snapshot()
        del snap["tests"]
        saved = self.replace_snapshot(json.dumps(snap))
        try:
            reason = self.deny_reason(ppl_guard.decide(payload(
                "ppl-code-worker", "Write",
                {"file_path": REPO + "/src/test/java/FooTest.java"})))
            self.assertIn("BOUNDARY_VIOLATION", reason)
            self.assertIn("may not write test sources", reason)
            self.assertIsNone(ppl_guard.decide(payload(
                "ppl-code-worker", "Write", {"file_path": REPO + "/src/tests_helper.py"})))
        finally:
            self.snapshot_path().write_text(saved, encoding="utf-8")

    def test_a_tests_glob_validation_refuses_denies_the_test_workers_bash_write_and_dispatch(self):
        # rule: R-TEST-PATTERNS-FROM-SNAPSHOT
        # valid JSON, but src/** matches product code, so the table cannot be trusted
        snap = python_snapshot()
        snap["tests"] = {"sources": ["src/**"]}
        saved = self.replace_snapshot(json.dumps(snap))
        try:
            dispatch = {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": REPO,
                        "tool_input": {"description": "write the tests",
                                       "prompt": "AC-1: an empty cart totals zero.",
                                       "subagent_type": "ppl-test-worker"}}
            for call in (payload("ppl-test-worker", "Bash",
                                 {"command": "echo x > tests/unit/test_cart.py"}),
                         dispatch):
                with self.subTest(tool=call["tool_name"]):
                    reason = self.deny_reason(ppl_guard.decide(call))
                    self.assertIn("OWNER_DECISION", reason)
                    self.assertIn(snapshot_reason(), reason)
        finally:
            self.snapshot_path().write_text(saved, encoding="utf-8")

    def test_shell_expansion_in_a_read_path_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # Each path is test-shaped as written, and the shell turns it into
        # something else: braces and .-matching globs reach src/, a .. segment
        # follows a symlink, ~user and =cmd name other directories, ( ) is a
        # pattern group or a zsh glob qualifier that can run code.
        cases = (("cat tests/{x,../src/shop/cart.py}", REPO),
                 ("cat tests/unit/test_{cart,x}.py", REPO),
                 ("head -n 5 tests/.*/src/shop/cart.py", REPO),
                 ("cat tests/.?/src/shop/cart.py", REPO),
                 ("cat tests/[.][.]/src/shop/cart.py", REPO),
                 ("cat tests/unit/*/../../../src/shop/cart.py", REPO),
                 ("cat tests/link/../unit/test_cart.py", REPO),
                 ("cat ~nobody/tests/unit/test_cart.py", REPO),
                 ("cat ~-/tests/unit/test_cart.py", REPO),
                 ("cat =test_cart.py", REPO + "/tests/unit"),
                 ("cat tests/@(..)/src/shop/cart.py", REPO),
                 ("cat tests/*(e:true:)", REPO),
                 ("cat tests/unit/test_$X.py", REPO),
                 ("grep -n total tests/{unit,../src}", REPO))
        for command, cwd in cases:
            with self.subTest(command=command):
                reason = self.deny_reason(ppl_guard.decide(payload(
                    "ppl-test-worker", "Bash", {"command": command}, cwd=cwd)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn("shell expansion", reason)
        # a command substitution is refused before any path is looked at
        reason = self.deny_reason(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "cat tests/`echo x`.py"})))
        self.assertIn("BOUNDARY_VIOLATION", reason)

    def test_git_add_of_a_brace_token_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        reason = self.deny_reason(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": "git add tests/{x,../src/shop/cart.py}"})))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("shell expansion", reason)

    def test_a_plain_glob_inside_a_test_directory_is_still_allowed(self):
        for command in ("cat tests/*.py", "cat tests/unit/test_*.py",
                        "head -n 5 tests/unit/*.py", "grep -n total tests/unit/test_?art.py",
                        "git add tests/unit/*.py"):
            with self.subTest(command=command):
                self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                           {"command": command})))

    def assert_denied(self, code, command, *words):
        """The test worker's Bash `command` is denied with `code` and every word."""
        reason = self.deny_reason(ppl_guard.decide(payload(
            "ppl-test-worker", "Bash", {"command": command})))
        self.assertIn(code, reason)
        for word in words:
            self.assertIn(word, reason)
        return reason

    # --- H1: read verbs are readers, and a file-valued option is a path ---

    def test_sed_is_not_a_read_verb(self):
        # rule: R-TEST-WORKER-BASH-READS
        # sed's r reads a file named in its script, w writes one, GNU e runs one
        for command in ("sed -e 'r src/shop/cart.py' tests/x.py",
                        "sed -n 1p tests/unit/test_cart.py"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_awk_is_not_a_read_verb(self):
        # rule: R-TEST-WORKER-BASH-READS
        for command in ("awk '{while((getline l < \"src/shop/cart.py\")>0) print l}' tests/x.py",
                        "awk 1 tests/unit/test_cart.py"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_less_is_not_a_read_verb(self):
        # rule: R-TEST-WORKER-BASH-READS
        # less runs LESSOPEN preprocessors, and +:e opens another file
        for command in ("less '+:e src/shop/cart.py' tests/unit/test_cart.py",
                        "less tests/unit/test_cart.py"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_file_valued_options_are_refused(self):
        # rule: R-TEST-WORKER-BASH-READS
        # A pattern or ignore file read from a test path still left the first
        # positional to be swallowed as the pattern (src/shop/cart.py never
        # judged), or filled the path list so the tree read went unnoticed
        # (rg -f tests/p.txt walks cwd). The options are refused outright.
        swallowed = ("grep -f tests/p.txt src/shop/cart.py",
                     "grep --file tests/p.txt src/shop/cart.py",
                     "grep --file=tests/p.txt src/shop/cart.py",
                     "grep -ftests/p.txt src/shop/cart.py",
                     "grep -rf tests/p.txt src/shop/cart.py",
                     "grep -nf tests/p.txt src/shop/cart.py",
                     "rg -f tests/p.txt src/shop/cart.py",
                     "rg --file=tests/p.txt src/shop/cart.py",
                     "rg -ftests/p.txt src/shop/cart.py",
                     "rg --ignore-file tests/p.txt src/shop/cart.py")
        tree_reads = ("rg -f tests/p.txt", "grep -rf tests/p.txt", "grep -r -f tests/p.txt",
                      "grep -r --file=tests/p.txt",
                      "grep -r --exclude-from=tests/p.txt total",
                      "rg --ignore-file tests/p.txt total")
        product_files = ("grep -f src/shop/cart.py tests/x.py",
                         "grep --exclude-from=src/shop/cart.py -r total tests",
                         "rg --ignore-file=src/shop/cart.py total tests",
                         "grep -f tests/patterns.txt tests/x.py",
                         "rg -f tests/patterns.txt tests")
        for command in swallowed + tree_reads + product_files:
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command, "option")

    def test_a_pattern_glued_to_its_option_does_not_hide_a_path(self):
        # rule: R-TEST-WORKER-BASH-READS
        # read as the pattern, src/shop/cart.py would have passed as a pipe filter
        for command in ("echo x | grep -etotal src/shop/cart.py",
                        "echo x | grep --regexp=total src/shop/cart.py",
                        "echo x | grep -ie total src/shop/cart.py"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_grep_directories_recurse_is_a_tree_read(self):
        # rule: R-TEST-WORKER-BASH-READS
        for command in ("echo x | grep --directories=recurse total",
                        "echo x | grep -d recurse tests",
                        "echo x | grep -drecurse total"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_an_option_the_guard_does_not_classify_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # grep is GNU, BSD or ugrep by machine: ugrep's --filter runs a program
        # and --config reads options from a file, rg's --pre runs one per file
        for command in ("rg --pre cat total tests", "rg --pre=cat total tests",
                        "rg --hostname-bin=x total tests",
                        "grep --filter=cat total tests/x.py",
                        "grep --config=src/shop/cart.py total tests/x.py",
                        "grep --fi src/shop/cart.py tests/x.py"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command, "option")

    def test_verbs_that_run_programs_are_not_on_the_list(self):
        # rule: R-TEST-WORKER-BASH-READS
        for command in ("find tests -exec cat {} +", "xargs cat < tests/list",
                        "find tests -name '*.py'"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_exporting_a_variable_that_rewrites_read_options_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        for command in ("export GREP_OPTIONS=-r; echo x | grep total",
                        "export RIPGREP_CONFIG_PATH=tests/rc; rg total tests"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_the_read_verbs_keep_their_ordinary_options(self):
        for command in ("echo x | grep -e total", "grep -e total tests/x.py",
                        "grep -n -e total -e count tests/unit/test_cart.py",
                        "grep -rn --include='*.py' total tests",
                        "grep -C 2 total tests/unit/test_cart.py",
                        "rg -n total tests", "rg -e total tests",
                        "head -n 5 tests/unit/test_cart.py", "tail -n5 tests/unit/test_cart.py",
                        "cat -n tests/unit/test_cart.py", "echo x | grep -c total"):
            with self.subTest(command=command):
                self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                           {"command": command})))

    # --- H2: .. is not resolved for the test worker ---

    def test_a_read_tool_path_with_dotdot_is_denied(self):
        # rule: R-TEST-WORKER-READ-TOOLS
        # tests/link -> src/shop would take tests/link/../shop/cart.py to src/
        for tool, tool_input in (("Read", {"file_path": REPO + "/tests/link/../shop/cart.py"}),
                                 ("Grep", {"pattern": "x", "path": REPO + "/tests/link/.."}),
                                 ("Glob", {"pattern": "*.py", "path": REPO + "/tests/link/.."}),
                                 ("Glob", {"pattern": "../src/**/*.py", "path": REPO + "/tests"})):
            with self.subTest(tool=tool, target=tool_input):
                reason = self.deny_reason(ppl_guard.decide(payload("ppl-test-worker", tool,
                                                                   tool_input)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(".. paths are not resolved", reason)

    def test_a_test_worker_write_with_dotdot_is_denied(self):
        # rule: R-ROLE-TEST-WORKER-MAIN
        target = REPO + "/tests/link/../unit/test_new.py"
        for tool, tool_input in (("Write", {"file_path": target}),
                                 ("Edit", {"file_path": target}),
                                 ("NotebookEdit", {"notebook_path": target}),
                                 ("Bash", {"command": "echo x > tests/link/../unit/test_new.py"})):
            with self.subTest(tool=tool):
                reason = self.deny_reason(ppl_guard.decide(payload("ppl-test-worker", tool,
                                                                   tool_input)))
                self.assertIn("BOUNDARY_VIOLATION", reason)
                self.assertIn(".. paths are not resolved", reason)

    def test_the_code_workers_dotdot_write_is_judged_as_before(self):
        self.assertIsNone(ppl_guard.decide(payload(
            "ppl-code-worker", "Write", {"file_path": REPO + "/src/x/../shop/cart.py"})))

    # --- H3: every word of a read or git add segment, not only its paths ---

    def test_a_brace_in_a_pattern_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # grep {x,../src/shop/cart.py} tests/x.py is grep x ../src/shop/cart.py tests/x.py
        self.assert_denied("BOUNDARY_VIOLATION", "grep {x,../src/shop/cart.py} tests/x.py", "shell expansion")

    def test_a_brace_in_an_option_value_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        for command in ("grep -e {a,b} tests/x.py", "grep -m {1,2} total tests/x.py",
                        "grep -r --include={a,b} total tests", "rg -g {a,b} total tests"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command, "shell expansion")

    def test_an_unquoted_glob_in_a_pattern_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        # from the repo root s* expands to scripts src, a recursive read of src
        for command in ("grep -r s* tests", "grep -e s* tests/x.py", "grep -r .* tests"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command, "shell expansion")

    def test_an_ifs_assignment_is_denied(self):
        # rule: R-TEST-WORKER-BASH-READS
        for command in ("export IFS=/; cat tests/unit/test_cart.py", "IFS=/ make test"):
            with self.subTest(command=command):
                self.assert_denied("BOUNDARY_VIOLATION", command)

    def test_a_quoted_pattern_stays_allowed(self):
        # The guard's own quote mask tells a quoted brace, group or glob from an
        # unquoted one, so those stay literal patterns. A $ is refused even
        # quoted, since double quotes still expand it.
        for command in ("grep 'foo.*bar' tests/x.py", "grep '{a,b}' tests/x.py",
                        "grep -E '(ab)+' tests/x.py", 'grep "foo.*bar" tests/x.py',
                        "CC=gcc make test"):
            with self.subTest(command=command):
                self.assertIsNone(ppl_guard.decide(payload("ppl-test-worker", "Bash",
                                                           {"command": command})))


if __name__ == "__main__":
    unittest.main()

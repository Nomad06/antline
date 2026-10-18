import os
import subprocess
import sys
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))

from ppl import paths, run, store
import ppl_guard

MAIN = "source/core/orders/src/main/java/orders/PriceCalculator.java"
TEST = "source/core/orders/src/test/java/orders/PriceCalculatorTest.java"
SIGNATURE = "public long totalWithDiscount(long amountCents, int percent) {"
BODY_ONE = "long discount = amountCents * percent / 100;"
BODY_TWO = "return Math.max(0L, amountCents - discount);"
IMPLEMENTATION = """package orders;

public class PriceCalculator {
    %s
        %s
        %s
    }
}
""" % (SIGNATURE, BODY_ONE, BODY_TWO)
ACCEPTANCE = ("AC-1: a 10 percent discount on 1000 cents gives 900 cents.\n"
              "AC-2: the total never goes below zero.")


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
                   + list(args), check=True, capture_output=True)


def write(repo, rel, text):
    target = Path(repo) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def dispatch(prompt, subagent_type="ppl-test-worker", tool="Agent", cwd="/tmp/ws"):
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "cwd": cwd,
            "tool_input": {"description": "write the tests", "prompt": prompt,
                           "subagent_type": subagent_type}}


class TestDispatchLeak(unittest.TestCase):
    """R-TEST-WORKER-DISPATCH-LEAK: the orchestrator's dispatch of ppl-test-worker
    is refused when its prompt carries the implementation."""

    def setUp(self):
        self.repo = Path(tmphome.mkdtemp(prefix="ppl-leak-repo-"))
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        write(self.repo, "README.md", "orders\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "base")
        self.base = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"],
                                   check=True, capture_output=True, text=True).stdout.strip()
        self.task = "LEAK-%d" % (abs(hash(self.id())) % 100000)
        run.init(self.task, "u", str(self.repo))
        state = run.load(self.task)
        state["base_sha"] = self.base
        state["stage"] = "tests"
        run.save(state)
        store.write_json(paths.run_dir(self.task) / "brief.json",
                         {"name_contract": {"interfaces": [SIGNATURE]},
                          "acceptance": [ACCEPTANCE]})

    def commit_implementation(self):
        write(self.repo, MAIN, IMPLEMENTATION)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "implementation")

    def reason(self, result):
        self.assertIsNotNone(result, "the dispatch was allowed")
        return result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_a_dispatch_carrying_a_unified_diff_is_denied(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        self.commit_implementation()
        prompt = ACCEPTANCE + "\n\ndiff --git a/x b/x\n@@ -1,2 +1,3 @@\n+changed\n"
        reason = self.reason(ppl_guard.decide(dispatch(prompt)))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("diff", reason)

    def test_a_dispatch_carrying_two_added_lines_is_denied(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        self.commit_implementation()
        prompt = ACCEPTANCE + "\n\nThe code does:\n" + BODY_ONE + "\n" + BODY_TWO + "\n"
        reason = self.reason(ppl_guard.decide(dispatch(prompt)))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("PriceCalculator.java", reason)

    def test_added_lines_quoted_as_markdown_still_count(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        self.commit_implementation()
        prompt = ACCEPTANCE + "\n- `%s`\n>   %s\n" % (BODY_ONE, BODY_TWO)
        self.assertIn("BOUNDARY_VIOLATION", self.reason(ppl_guard.decide(dispatch(prompt))))

    def test_the_older_task_tool_name_is_checked_too(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        self.commit_implementation()
        prompt = BODY_ONE + "\n" + BODY_TWO
        self.assertIn("BOUNDARY_VIOLATION",
                      self.reason(ppl_guard.decide(dispatch(prompt, tool="Task"))))

    def test_an_uncommitted_new_file_counts_as_added(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        write(self.repo, MAIN, IMPLEMENTATION)
        prompt = BODY_ONE + "\n" + BODY_TWO
        self.assertIn("BOUNDARY_VIOLATION", self.reason(ppl_guard.decide(dispatch(prompt))))

    def test_a_non_isolated_run_dispatching_from_elsewhere_is_checked(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        # ppl-test-worker is a pipeline role name, so the dispatch is the run's
        # business wherever the orchestrator's session sits.
        state = run.load(self.task)
        state["isolated"] = False
        run.save(state)
        self.commit_implementation()
        prompt = BODY_ONE + "\n" + BODY_TWO
        result = ppl_guard.decide(dispatch(prompt, cwd="/somewhere/else"))
        self.assertIn("BOUNDARY_VIOLATION", self.reason(result))

    def test_a_dispatch_without_a_recorded_base_is_denied(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        state = run.load(self.task)
        state["base_sha"] = None
        run.save(state)
        reason = self.reason(ppl_guard.decide(dispatch(ACCEPTANCE)))
        self.assertIn("BOUNDARY_VIOLATION", reason)
        self.assertIn("could not be checked", reason)

    def test_a_dispatch_against_unreadable_state_is_denied(self):
        # rule: R-TEST-WORKER-DISPATCH-LEAK
        run.state_path(self.task).write_text("{not json", encoding="utf-8")
        reason = self.reason(ppl_guard.decide(dispatch(ACCEPTANCE)))
        self.assertIn("OWNER_DECISION", reason)
        self.assertIn("unreadable", reason)

    def test_acceptance_criteria_only_are_allowed(self):
        self.commit_implementation()
        self.assertIsNone(ppl_guard.decide(dispatch(ACCEPTANCE)))

    def test_interface_lines_the_brief_agreed_are_allowed(self):
        self.commit_implementation()
        prompt = ACCEPTANCE + "\nThe interface is:\n" + SIGNATURE + "\n"
        self.assertIsNone(ppl_guard.decide(dispatch(prompt)))

    def test_one_added_line_alone_is_allowed(self):
        self.commit_implementation()
        prompt = ACCEPTANCE + "\n" + BODY_ONE + "\n"
        self.assertIsNone(ppl_guard.decide(dispatch(prompt)))

    def test_added_lines_in_test_sources_do_not_count(self):
        write(self.repo, TEST, "class T {\n    %s\n    %s\n}\n" % (BODY_ONE, BODY_TWO))
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "tests")
        prompt = BODY_ONE + "\n" + BODY_TWO
        self.assertIsNone(ppl_guard.decide(dispatch(prompt)))

    def test_the_same_prompt_to_the_code_worker_is_allowed(self):
        self.commit_implementation()
        prompt = "diff --git a/x b/x\n" + BODY_ONE + "\n" + BODY_TWO
        self.assertIsNone(ppl_guard.decide(dispatch(prompt, subagent_type="ppl-code-worker")))

    def test_without_an_active_run_a_dispatch_is_left_alone(self):
        paths.active_file().unlink()
        self.assertIsNone(ppl_guard.decide(dispatch("diff --git a/x b/x\n")))

    def test_the_agent_tools_are_in_the_guarded_names_doctor_checks(self):
        # rule: R-GUARD-MATCHER
        for name in ("Agent", "Task"):
            self.assertIn(name, ppl_guard.GUARDED_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()

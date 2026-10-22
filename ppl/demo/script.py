"""The demo's walk: one issue from `ppl setup` to the recorded merge, as a table of steps."""
import json
import re
import subprocess
import sys

from . import sandbox

AGENT = "ppl-code-worker"
TEST_AGENT = "ppl-test-worker"
IMPLEMENTATION = ("def total(items, discount=0):\n"
                  "    subtotal = sum(items)\n"
                  "    return subtotal - subtotal * discount / 100\n")
FIXED = ("def total(items, discount=0):\n"
         "    if not 0 <= discount <= 100:\n"
         "        raise ValueError(\"discount must be 0-100\")\n"
         "    subtotal = sum(items)\n"
         "    return subtotal - subtotal * discount / 100\n")
TEST = ("import unittest\n\nfrom shop.cart import total\n\n\n"
        "class TestDiscount(unittest.TestCase):\n"
        "    def test_ten_percent(self):\n"
        "        self.assertEqual(total([50, 50], 10), 90)\n")
PRODUCER = {"provider": "demo", "model": "stand-in", "params_hash": ""}
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}")
PR_URL = "https://github.com/demo/shop/pull/1"
CLOSE = "gh issue close 1 -R demo/shop"


class Refused(Exception):
    """A ppl call that exited non-zero: the step's answer is a refusal."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


class Walk:
    def __init__(self, box):
        self.box = box
        self.base = None
        self.head = None
        self.generation = None
        self.revision = None

    def ppl(self, *args):
        done = self.box.ppl(*args)
        if done.returncode != 0:
            lines = [line for line in done.stderr.strip().splitlines() if line]
            try:
                error = json.loads(lines[-1])
                raise Refused(error["error_code"], error["message"])
            except (IndexError, ValueError, KeyError, TypeError):
                raise Refused("EXIT_%d" % done.returncode, done.stderr.strip() or done.stdout.strip())
        return done.stdout

    def stage(self, name):
        self.ppl("stage", "enter", name)

    def document(self, name, doc):
        path = self.box.root / name
        path.write_text(json.dumps(doc), encoding="utf-8")
        return str(path)

    def commit_file(self, rel, text, message):
        target = self.box.repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        self.head = self.box.commit([rel], message)

    def guard(self, tool, tool_input, role=None):
        cwd = self.box.repo if role else self.box.workspace
        decision = self.box.guard({"hook_event_name": "PreToolUse", "agent_type": role,
                                   "tool_name": tool, "tool_input": tool_input, "cwd": str(cwd)})
        if decision["decision"] == "allow":
            return "allow"
        if decision["decision"] == "deny":
            return ("deny", decision["code"], decision["reason"])
        raise RuntimeError("the guard hook failed: %s" % decision["reason"])

    def dispatch(self, prompt):
        return self.guard("Agent", {"description": "tests", "subagent_type": TEST_AGENT,
                                    "prompt": prompt})

    def refresh(self):
        shown = json.loads(self.ppl("show", "--json"))
        self.generation = str(shown["lease_generation"])
        self.revision = shown["spec_revision"]

    def run_gate(self, check, argv, scan=False):
        log, rc = self.box.root / ("%s.log" % check), self.box.root / ("%s.rc" % check)
        if scan:
            text = self.box.git("diff", self.base, "HEAD")
            hit = SECRET.search(text)
            output, code = ("secret found: %s\n" % hit.group(0)) if hit else "no secrets\n", int(bool(hit))
        else:
            done = subprocess.run(argv, cwd=str(self.box.repo), env=self.box.env,
                                  capture_output=True, text=True, timeout=sandbox.TIMEOUT_SECONDS)
            output, code = done.stdout + done.stderr, done.returncode
        log.write_text(output, encoding="utf-8")
        rc.write_text(str(code), encoding="utf-8")
        self.ppl("evidence", "add", "--check", check, "--result", "pass" if code == 0 else "fail",
                 "--log", str(log), "--rc", str(rc), "--generation", self.generation,
                 "--from", "orchestrator")

    def gates(self):
        self.stage("gates")
        self.run_gate("focused-tests", [sys.executable, "-m", "unittest", "-q", "tests.test_discount"])
        self.run_gate("secret-scan", None, scan=True)

    def verdict(self, name, verdict, acceptance):
        doc = {"task_id": sandbox.TASK, "candidate_sha": self.head, "spec_revision": self.revision,
               "verdict": verdict, "acceptance_evidence": acceptance, "findings": [],
               "producer": PRODUCER}
        self.ppl("verdict", "record", self.document(name, doc), "--generation", self.generation,
                 "--from", "ppl-verifier")


def setup(walk):
    settings = walk.box.workspace / ".claude" / "settings.local.json"
    if "ppl_guard" not in settings.read_text(encoding="utf-8"):
        raise RuntimeError("ppl setup registered no guard hook")
    return "ok"


def init(walk):
    shown = json.loads(walk.ppl("show", "--json"))
    if (shown["task_id"], shown["stage"]) != (sandbox.TASK, "admission"):
        raise RuntimeError("ppl init left the run at %s" % shown["stage"])
    return "ok"


def baseline(walk):
    walk.stage("baseline")
    walk.base = walk.box.git("rev-parse", "HEAD").strip()
    walk.head = walk.base
    walk.ppl("baseline", "record", walk.document("baseline.json", {"base_sha": walk.base}))
    return "ok"


def brief_before_fetch(walk):
    walk.stage("briefing")
    try:
        walk.ppl("brief", "set", walk.document("brief.json", BRIEF), "--from", "ppl-briefer")
    except Refused as refusal:
        return ("refuse", refusal.code, refusal.message)
    return "ok"


def fetch(walk):
    walk.ppl("tracker", "fetch")
    return "ok"


def brief_set(walk):
    walk.ppl("brief", "set", walk.document("brief.json", BRIEF), "--from", "ppl-briefer")
    walk.refresh()
    return "ok"


def implementation_early(walk):
    try:
        walk.stage("implementation")
    except Refused as refusal:
        return ("refuse", refusal.code, refusal.message)
    return "ok"


def challenge(walk):
    doc = {"task_id": sandbox.TASK, "spec_revision": walk.revision, "verdict": "ACCEPTED",
           "new_classes": [], "wire_shapes": [], "open_decisions": [], "producer": PRODUCER}
    walk.ppl("brief", "challenge", walk.document("challenge.json", doc), "--generation",
             walk.generation, "--from", "ppl-brief-challenger")
    walk.stage("implementation")
    return "ok"


def code_write(walk):
    answer = walk.guard("Write", {"file_path": str(walk.box.repo / "shop" / "cart.py"),
                                  "content": IMPLEMENTATION}, AGENT)
    walk.commit_file("shop/cart.py", IMPLEMENTATION, "apply the discount")
    return answer


def code_edit_test(walk):
    return walk.guard("Edit", {"file_path": str(walk.box.repo / "tests" / "test_cart.py"),
                               "old_string": "3", "new_string": "4"}, AGENT)


def test_read_main(walk):
    walk.stage("tests")
    return walk.guard("Read", {"file_path": str(walk.box.repo / "shop" / "cart.py")}, TEST_AGENT)


def test_write(walk):
    answer = walk.guard("Write", {"file_path": str(walk.box.repo / "tests" / "test_discount.py"),
                                  "content": TEST}, TEST_AGENT)
    walk.commit_file("tests/test_discount.py", TEST, "test the discount")
    return answer


def dispatch_leak(walk):
    return walk.dispatch("Write tests for this implementation:\n" + IMPLEMENTATION)


def dispatch_clean(walk):
    return walk.dispatch("Write tests for AC1: total(items, discount=0) applies a percent discount.")


def gates_first(walk):
    walk.gates()
    return "ok"


def verdict_rework(walk):
    walk.stage("verification")
    walk.verdict("verdict-rework.json", "REWORK", {})
    walk.ppl("budget", "spend", "retry")
    return "ok"


def fix(walk):
    walk.stage("implementation")
    walk.commit_file("shop/cart.py", FIXED, "reject a discount outside 0-100")
    walk.stage("tests")
    walk.gates()
    return "ok"


def verdict_pass(walk):
    walk.stage("verification")
    walk.verdict("verdict-pass.json", "PASS", {"AC1": "evidence:focused-tests"})
    return "ok"


def merge_request(walk):
    walk.stage("mr")
    walk.ppl("mr", "set", "--iid", "1", "--url", PR_URL)
    walk.stage("review")
    walk.ppl("review", "approved", "--sha", walk.head)
    return "ok"


def closure_early(walk):
    try:
        walk.stage("closure")
    except Refused as refusal:
        return ("refuse", refusal.code, refusal.message)
    return "ok"


def closure(walk):
    log, rc = walk.box.root / "simplification.log", walk.box.root / "simplification.rc"
    log.write_text("nothing to simplify in a three-line change\n", encoding="utf-8")
    rc.write_text("0", encoding="utf-8")
    walk.ppl("evidence", "add", "--check", "simplification", "--result", "skip", "--log", str(log),
             "--rc", str(rc), "--generation", walk.generation, "--from", "orchestrator")
    walk.stage("closure")
    walk.ppl("transition", "done")
    walk.stage("merge_watch")
    return "ok"


def close_before_merge(walk):
    return walk.guard("Bash", {"command": CLOSE})


def merge_observed(walk):
    walk.ppl("merge", "observed", "--sha", walk.head, "--url", PR_URL)
    return "ok"


def close_after_merge(walk):
    return walk.guard("Bash", {"command": CLOSE})


BRIEF = {"task_id": sandbox.TASK, "purpose": "apply a discount",
         "success_outcome": "total(items, discount) applies a percent discount",
         "governing_requirements": ["issue #1"], "acceptance": ["AC1"],
         "allowed_write_paths": ["shop/**"], "forbidden_paths": [],
         "verification": ["focused-tests"], "open_decisions": [],
         "name_contract": {"shop.cart.total": "total(items, discount=0) -> float"}}

# (actor, tried, expect, rule), and ACTIONS holds the function of each row, in the same order.
STEPS = [
    ("operator", "`ppl setup` registers the hooks and applies harness demo", "ok",
     "R-INIT-SETUP-COMPLETE"),
    ("operator", "`ppl init GH-1` freezes the harness into the run", "ok", ""),
    ("orchestrator", "`ppl stage enter baseline`, `ppl baseline record` at HEAD", "ok", ""),
    ("orchestrator", "`ppl brief set` before the issue was fetched",
     ("refuse", "ILLEGAL_TRANSITION", "ppl tracker fetch"), "R-TRACKER-FETCH-PROVENANCE"),
    ("orchestrator", "`ppl tracker fetch` reads issue #1 through the gh stand-in", "ok", ""),
    ("briefer", "`ppl brief set`: acceptance AC1, write boundary `shop/**`", "ok",
     "R-BRIEF-SCHEMA"),
    ("orchestrator", "`ppl stage enter implementation` before the brief challenge",
     ("refuse", "ILLEGAL_TRANSITION", "ACCEPTED brief challenge"),
     "R-BRIEF-CHALLENGED-BEFORE-IMPLEMENTATION"),
    ("challenger", "`ppl brief challenge` ACCEPTED, then implementation", "ok", ""),
    ("code worker", "Write `shop/cart.py`", "allow", "R-ROLE-CODE-WORKER-BOUNDARY"),
    ("code worker", "Edit `tests/test_cart.py`",
     ("deny", "BOUNDARY_VIOLATION", "may not write test sources"), "R-ROLE-CODE-WORKER-TESTS"),
    ("test worker", "Read `shop/cart.py`",
     ("deny", "BOUNDARY_VIOLATION", "only test sources are visible"), "R-ROLE-TEST-WORKER-MAIN"),
    ("test worker", "Write `tests/test_discount.py`", "allow", "R-ROLE-TEST-WORKER-MAIN"),
    ("orchestrator", "dispatch ppl-test-worker with the implementation pasted",
     ("deny", "BOUNDARY_VIOLATION", "carries the implementation"), "R-TEST-WORKER-DISPATCH-LEAK"),
    ("orchestrator", "dispatch ppl-test-worker with AC1 only", "allow",
     "R-TEST-WORKER-DISPATCH-LEAK"),
    ("orchestrator", "gates: `focused-tests` runs the real unittest, `secret-scan`", "ok",
     "R-DONE-SECRET-SCAN"),
    ("verifier", "`ppl verdict record` REWORK, retry budget 1 of 3", "ok", "R-BUDGET-RETRY"),
    ("code worker", "fix: reject a discount outside 0-100, gates again", "ok", ""),
    ("verifier", "`ppl verdict record` PASS, AC1 proven by focused-tests", "ok",
     "R-DONE-ACCEPTANCE-EVIDENCE"),
    ("orchestrator", "`ppl mr set` and `ppl review approved` (forge stand-in)", "ok", ""),
    ("orchestrator", "`ppl stage enter closure` with no simplification receipt",
     ("refuse", "ILLEGAL_TRANSITION", "simplification"), "R-CLOSURE-ONLY-WHEN-DONE-PASSES"),
    ("orchestrator", "simplification `skip` receipt, closure, `ppl transition done`", "ok",
     "R-DONE-GATE"),
    ("orchestrator", "`gh issue close 1` in merge_watch before the merge",
     ("deny", "OWNER_DECISION", "ppl merge observed"), "R-TRACKER-CLI-GATE"),
    ("operator", "`ppl merge observed`", "ok", "R-MERGE-OBSERVED-ONCE"),
    ("orchestrator", "`gh issue close 1` after the merge (asked, not run)", "allow",
     "R-TRACKER-CLI-GATE"),
]
ACTIONS = [
    setup, init, baseline, brief_before_fetch, fetch, brief_set, implementation_early, challenge,
    code_write, code_edit_test, test_read_main, test_write, dispatch_leak, dispatch_clean,
    gates_first, verdict_rework, fix, verdict_pass, merge_request, closure_early, closure,
    close_before_merge, merge_observed, close_after_merge,
]

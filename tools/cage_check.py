"""Physical cage proof, driven the way Claude Code drives the hook: a JSON
payload on stdin, the hook run as a subprocess, the decision read off stdout.

It stands in for a live check that dispatches ppl-code-worker and
ppl-verifier subagents against a real run and watches the hook deny their
write attempts. This script cannot dispatch subagents, and registering the
guard hook in ~/.claude/settings.json would arm it for every Claude Code
session on the machine, which is the operator's decision, not this script's.
So instead: build the exact stdin payload Claude Code would send,
invoke ppl/_assets/hooks/ppl_guard.py as a subprocess against a temporary PPL_HOME and a
real temporary git worktree, and check the JSON (or its absence) on stdout -
the same contract the harness itself uses.

Every case below prints one line: "PASS <name>" or "FAIL <name>: <detail>".
Exit code is 0 only if every case passed.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "ppl" / "_assets"
GUARD_HOOK = ASSETS / "hooks" / "ppl_guard.py"

sys.path.insert(0, str(REPO_ROOT))


def make_ppl_home():
    """The data root at <fake home>/.ppl, the default layout, so a `~` spelling
    of the runs directory in a case really lands in this sandbox's runs/."""
    home = Path(tempfile.mkdtemp(prefix="ppl-cage-home-")) / ".ppl"
    home.mkdir()
    return home


# What a Python harness's [tests] table lists, frozen into the python cage's snapshot.
PYTHON_TEST_SOURCES = ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]

LAYOUT_FILES = {
    "java": {"source/Main.java": "class Main {}\n",
             "source/MainTest.java": "class MainTest {}\n",
             "source/src/test/OtherTest.java": "class OtherTest {}\n"},
    "python": {"src/shop/cart.py": "def total(items):\n    return sum(items)\n",
               "tests/unit/test_cart.py": "def test_total():\n    pass\n"},
}
# Links planted in the tree, uncommitted: one in a test directory pointing at
# product code, which no role can create but a repository can hold.
LAYOUT_LINKS = {"java": {}, "python": {"tests/link": "../src/shop"}}


def make_worktree(layout="java"):
    """A real git worktree holding LAYOUT_FILES[layout]. The guard reads git for
    one check only, the ppl-test-worker dispatch (R-TEST-WORKER-DISPATCH-LEAK),
    and a real repo keeps every case an honest rehearsal rather than a bare
    directory standing in for one.
    """
    tree = Path(tempfile.mkdtemp(prefix="ppl-cage-repo-"))
    run = lambda *args: subprocess.run(  # noqa: E731 - local one-liner helper
        ["git", "-C", str(tree)] + list(args),
        check=True, capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "cage@example.com")
    run("config", "user.name", "cage")
    files = LAYOUT_FILES[layout]
    for rel, text in files.items():
        (tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (tree / rel).write_text(text, encoding="utf-8")
    run("add", *files)
    run("commit", "-q", "-m", "initial")
    for rel, target in LAYOUT_LINKS[layout].items():
        os.symlink(target, str(tree / rel))
    return tree


def run_hook(payload, ppl_home):
    env = dict(os.environ)
    env["PPL_HOME"] = str(ppl_home)
    env["HOME"] = str(ppl_home.parent)
    result = subprocess.run(
        [sys.executable, str(GUARD_HOOK)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        timeout=10)
    return result


def decision(result):
    """Returns ("allow", None) or ("deny", (code, reason))."""
    if result.stderr.strip():
        return "error", result.stderr.strip()
    out = result.stdout.strip()
    if not out:
        return "allow", None
    doc = json.loads(out)
    hso = doc["hookSpecificOutput"]
    reason = hso["permissionDecisionReason"]
    code = reason.split(" ", 1)[0]
    return "deny", (code, reason)


class Cage:
    """A sandbox run. The java layout is a Maven tree under the example Maven
    harness's snapshot, the python one a src/ and tests/ tree whose snapshot
    names PYTHON_TEST_SOURCES."""

    def __init__(self, layout="java"):
        self.ppl_home = make_ppl_home()
        self.worktree = make_worktree(layout)
        self.unrelated = Path(tempfile.mkdtemp(prefix="ppl-cage-unrelated-"))
        os.environ["PPL_HOME"] = str(self.ppl_home)
        # ppl modules are imported only after PPL_HOME is set, matching the
        # convention every test module in tests/ already uses, so paths.home()
        # resolves to this run's sandbox and never to the real data root (~/.ppl).
        from ppl import harness, run, store, paths  # noqa: E402
        self._run = run
        self._store = store
        self._paths = paths
        python = layout == "python"
        self.task_id = "CAGE-90002" if python else "CAGE-90001"
        run.init(self.task_id, "https://example.atlassian.net/browse/" + self.task_id,
                 str(self.worktree))
        store.write_json(paths.run_dir(self.task_id) / "boundary.json",
                          {"write": ["src/**" if python else "source/**"], "forbid": []})
        # The test worker's build shapes and the test globs come from the run's
        # harness snapshot, so the sandbox run carries an example harness's one.
        name = "example-generic" if python else "example-maven"
        source = ASSETS / "defaults" / "harnesses" / name
        target = paths.harness_dir(name)
        if not target.exists():
            shutil.copytree(source, target)
        snapshot = harness.snapshot(harness.load(name))
        if python:
            snapshot["tests"] = {"sources": list(PYTHON_TEST_SOURCES)}
            # tests/fixtures.py's GITHUB_ISSUES table, inline since this script
            # cannot import the test helpers
            snapshot["tracker"] = {"kind": "github-issues", "repo": "o/r", "task_prefix": "GH"}
        store.write_json(paths.run_dir(self.task_id) / "harness.json", snapshot)
        self.results = []

    def payload(self, agent, tool, tool_input, cwd):
        return {"hook_event_name": "PreToolUse", "agent_type": agent,
                "tool_name": tool, "tool_input": tool_input, "cwd": cwd}

    def check(self, name, payload, expect, reason=None):
        """expect is "allow" or a deny code substring. A deny can also be held
        to a `reason` substring, for a case another rule would deny as well."""
        result = run_hook(payload, self.ppl_home)
        kind, detail = decision(result)
        if kind == "error":
            ok, detail_str = False, "hook raised: %s" % detail
        elif expect == "allow":
            ok = kind == "allow"
            detail_str = "allowed" if kind == "allow" else "denied: %s" % (detail,)
        else:
            code_wanted = expect
            ok = (kind == "deny" and code_wanted in detail[0]
                  and (reason is None or reason in detail[1]))
            detail_str = ("denied %s: %s" % detail) if kind == "deny" else "allowed (expected deny)"
        self.results.append((name, ok, detail_str))
        print(("PASS " if ok else "FAIL ") + name + ": " + detail_str)
        return ok

    def set_stage(self, stage):
        state = self._run.load(self.task_id)
        state["stage"] = stage
        self._run.save(state)

    def record_merge(self, sha):
        """What `ppl merge observed --sha <sha>` records."""
        from ppl import review  # noqa: E402
        review.record_merge(self.task_id, sha)

    def run_dir(self):
        return self._paths.run_dir(self.task_id)


def main():
    cages = []
    try:
        cages.append(Cage())
        run_cases(cages[-1])
        cages.append(Cage(layout="python"))
        run_python_cases(cages[-1])
        run_os_sandbox_cases(cages[-1])
        return report([result for cage in cages for result in cage.results])
    finally:
        # This harness used to leave three directories per invocation behind in
        # TMPDIR, the same leak the test suite had.
        for cage in cages:
            for path in (cage.ppl_home.parent, cage.worktree, cage.unrelated):
                shutil.rmtree(str(path), ignore_errors=True)


def report(results):
    failed = [name for name, ok, _ in results if not ok]
    print()
    print("%d/%d cases passed" % (len(results) - len(failed), len(results)))
    if failed:
        print("FAILED: " + ", ".join(failed))
        return 1
    return 0


def run_python_cases(cage):
    """The role split on a Python layout, by the globs its snapshot froze rather
    than the Java defaults [R-TEST-PATTERNS-FROM-SNAPSHOT]."""
    wt = str(cage.worktree)
    test_file = wt + "/tests/unit/test_cart.py"
    product_file = wt + "/src/shop/cart.py"

    cage.check(
        "python layout: ppl-test-worker writing a python test is allowed",
        cage.payload("ppl-test-worker", "Write", {"file_path": test_file}, wt),
        "allow")

    cage.check(
        "python layout: ppl-test-worker writing product code is denied",
        cage.payload("ppl-test-worker", "Write", {"file_path": product_file}, wt),
        "BOUNDARY_VIOLATION")

    # inside the code worker's src/** boundary, so only the python globs refuse it
    cage.check(
        "python layout: ppl-code-worker writing a python test inside its boundary is denied",
        cage.payload("ppl-code-worker", "Write", {"file_path": wt + "/src/shop/test_cart.py"}, wt),
        "BOUNDARY_VIOLATION", reason="may not write test sources")

    cage.check(
        "python layout: ppl-code-worker writing product code is allowed",
        cage.payload("ppl-code-worker", "Write", {"file_path": product_file}, wt),
        "allow")

    cage.check(
        "python layout: ppl-test-worker Read through a test-directory link and .. is denied",
        cage.payload("ppl-test-worker", "Read",
                     {"file_path": wt + "/tests/link/../shop/cart.py"}, wt),
        "BOUNDARY_VIOLATION", reason=".. paths are not resolved")

    cage.check(
        "python layout: ppl-test-worker grep -f of a test file over product code is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "grep -f tests/p.txt src/shop/cart.py"}, wt),
        "BOUNDARY_VIOLATION", reason="-f is an option the guard does not classify")

    cage.check(
        "python layout: ppl-test-worker brace read that expands into src/ is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "cat tests/{x,../src/shop/cart.py}"}, wt),
        "BOUNDARY_VIOLATION", reason="shell expansion")

    # The orchestrator's gh issue write waits for the observed merge, from its own
    # session beside the repo [R-TRACKER-CLI-GATE]. These run last: they move the stage.
    comment = {"command": "gh issue comment 1 -R o/r --body-file /tmp/n.md"}
    elsewhere = str(cage.unrelated)
    cage.check(
        "python layout: the orchestrator's gh issue comment before merge_watch is denied",
        cage.payload(None, "Bash", comment, elsewhere),
        "OWNER_DECISION", reason="merge is observed")
    cage.set_stage("merge_watch")
    cage.check(
        "python layout: the orchestrator's gh issue comment before ppl merge observed is denied",
        cage.payload(None, "Bash", comment, elsewhere),
        "OWNER_DECISION", reason="ppl merge observed")
    cage.record_merge("b" * 40)
    cage.check(
        "python layout: the orchestrator's gh issue comment is allowed once the merge is recorded",
        cage.payload(None, "Bash", comment, elsewhere),
        "allow")
    # a role never writes the tracker, merged or not, through MCP either [R-JIRA-NO-ROLE-TOOL]
    cage.check(
        "python layout: ppl-code-worker's GitHub MCP issue write after the merge is denied",
        cage.payload("ppl-code-worker", "mcp__github__add_issue_comment", {"body": "done"}, wt),
        "BOUNDARY_VIOLATION", reason="may not write the tracker")


def run_cases(cage):
    wt = str(cage.worktree)
    unrelated = str(cage.unrelated)

    cage.check(
        "ppl-code-worker editing main source inside boundary is allowed",
        cage.payload("ppl-code-worker", "Edit",
                     {"file_path": wt + "/source/Main.java"}, wt),
        "allow")

    cage.check(
        "ppl-code-worker editing a test source is denied (BOUNDARY_VIOLATION)",
        cage.payload("ppl-code-worker", "Write",
                     {"file_path": wt + "/source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker editing a main source is denied",
        cage.payload("ppl-test-worker", "Edit",
                     {"file_path": wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-verifier writing anything is denied",
        cage.payload("ppl-verifier", "Write",
                     {"file_path": wt + "/notes.txt"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "git add -A from any agent is denied, even outside the run's worktree",
        cage.payload("ppl-code-worker", "Bash", {"command": "git add -A"}, unrelated),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a refspec force push (+HEAD:main) is denied",
        cage.payload("ppl-code-worker", "Bash", {"command": "git push origin +HEAD:main"}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "git stash clear is denied, it empties every session's stash",
        cage.payload(None, "Bash", {"command": "git stash clear"}, unrelated),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a bare git stash drop is denied, it takes the top entry",
        cage.payload("ppl-code-worker", "Bash", {"command": "git stash drop"}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "git stash drop stash@{N}, the shared-stack recipe's own drop, is allowed",
        cage.payload("ppl-code-worker", "Bash", {"command": "git stash drop stash@{2}"}, wt),
        "allow")
    cage.check(
        "listing the ~/.ppl data root is not read as a ppl invocation",
        cage.payload("ppl-code-worker", "Bash", {"command": "cd ~/.ppl && ls"}, wt),
        "allow")

    runs_target = str(cage.run_dir() / "state.json")
    cage.check(
        "a Bash redirect into the runs directory is denied",
        cage.payload(None, "Bash", {"command": "echo x > %s" % runs_target}, wt),
        "BOUNDARY_VIOLATION")

    # The same path in the spellings a role will actually type: the hook
    # resolves the target, it does not look for the literal runs path.
    tilde_run = "~/.ppl/runs/%s" % cage.task_id
    cage.check(
        "a ~-spelled Bash redirect into the runs directory is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "echo '{}' > %s/state.json" % tilde_run}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a ~-spelled tee into the runs directory is denied for an unroled session",
        cage.payload(None, "Bash",
                     {"command": "echo '{}' | tee %s/boundary.json" % tilde_run}, unrelated),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a cd-relative Bash write into the runs directory is denied for ppl-test-worker",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "cd ~/.ppl/runs && echo x > %s/harness.json" % cage.task_id},
                     wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a subshell (cd into the runs directory, then a write, is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "(cd %s && echo x > boundary.json)" % tilde_run}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a pushd into the runs directory, then a write, is denied for an unroled session",
        cage.payload(None, "Bash",
                     {"command": "pushd %s && echo x > boundary.json" % tilde_run}, unrelated),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a pushd's own redirect into a test source is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "pushd /tmp > source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a redirect after a subshell's ) into a test source is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "(cd /tmp && echo x) > source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a redirect after a brace group's } into a test source is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "{ cd /tmp; echo x; } > source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a write after a subshell closed by an input redirect is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "(cd /tmp && echo x) < /dev/null; echo y > source/MainTest.java"},
                     wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a cp behind a bare & into a test source is denied for ppl-code-worker",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "true & cp /tmp/x source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a recursive copy of a directory named runs into the data root is denied unroled",
        cage.payload(None, "Bash", {"command": "cp -r /tmp/x/runs ~/.ppl/"}, unrelated),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a nohup in front of a cp into the runs directory is unwrapped and denied for a role",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "nohup cp /tmp/x %s/state.json" % tilde_run}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a nested nice -n 5 behind time in front of a tee into runs/ is denied unroled",
        cage.payload(None, "Bash",
                     {"command": "time nice -n 5 tee %s/boundary.json" % tilde_run}, unrelated),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a nohup cp elsewhere is still allowed",
        cage.payload("ppl-code-worker", "Bash", {"command": "nohup cp /tmp/x /tmp/y"}, wt),
        "allow")
    cage.check(
        "removing the data root that holds the runs directory is denied",
        cage.payload(None, "Bash", {"command": "rm -rf ~/.ppl"}, unrelated),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a copy into the home that holds the data root is allowed, only a removal "
        "takes runs/ with it",
        cage.payload(None, "Bash", {"command": "cp /tmp/x ~/"}, unrelated),
        "allow")
    cage.check(
        "a $HOME-spelled Bash write into the runs directory is denied for a role "
        "(an unresolvable target fails closed)",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "echo x > $HOME/.ppl/runs/%s/state.json" % cage.task_id}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "a ~-spelled Bash write to control.json is denied for a role (no carve-out)",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "echo '{}' > %s/control.json" % tilde_run}, wt),
        "BOUNDARY_VIOLATION")
    cage.check(
        "the same control.json write is allowed for the unroled operator session",
        cage.payload(None, "Bash", {"command": "echo '{}' > %s/control.json" % tilde_run},
                     unrelated),
        "allow")
    cage.check(
        "a ~-spelled Bash write under the data root but outside runs/ is allowed",
        cage.payload("ppl-code-worker", "Bash", {"command": "echo x > ~/.ppl/notes.txt"}, wt),
        "allow")

    cage.check(
        "an unrelated agent in an unrelated directory is allowed "
        "(the cage does not leak into other projects)",
        cage.payload("SomeOtherProjectAgent", "Edit",
                     {"file_path": unrelated + "/Foo.java"}, unrelated),
        "allow")

    cage.check(
        "a ppl-* role writing the run's repo from a session sitting OUTSIDE the "
        "worktree is denied (cwd must not be the scope)",
        cage.payload("ppl-verifier", "Write",
                     {"file_path": wt + "/notes.txt"}, unrelated),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a write with no agent_type at all into the run's repo is denied "
        "(the orchestrator has a write boundary too)",
        cage.payload(None, "Write",
                     {"file_path": wt + "/DANGER.txt"}, unrelated),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a Jira write tool before merge_watch is denied",
        cage.payload(None, "mcp__atlassian__addCommentToJiraIssue",
                     {"body": "done"}, wt),
        "OWNER_DECISION")
    cage.check(
        "the same Jira write under the claude.ai Rovo prefix is denied too",
        cage.payload(None, "mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue",
                     {"body": "done"}, wt),
        "OWNER_DECISION")

    cage.set_stage("merge_watch")
    cage.check(
        "a Jira write tool in merge_watch before ppl merge observed is denied",
        cage.payload(None, "mcp__atlassian__addCommentToJiraIssue",
                     {"body": "done"}, wt),
        "OWNER_DECISION")

    cage.record_merge("a" * 40)
    cage.check(
        "the same Jira write tool at merge_watch is allowed once the merge is recorded",
        cage.payload(None, "mcp__atlassian__addCommentToJiraIssue",
                     {"body": "done"}, wt),
        "allow")
    cage.check(
        "the Rovo-prefixed Jira write at merge_watch is allowed",
        cage.payload(None, "mcp__claude_ai_Atlassian_Rovo__addCommentToJiraIssue",
                     {"body": "done"}, wt),
        "allow")

    # --- round 1 review, C4: a worker role driving the ppl CLI directly
    # through its own Bash tool bypasses the registry, the lease and the done
    # gate, since none of those write a path the shape-based Bash checks
    # above would ever see. R-GUARD-PPL-CLI-DENIED. ---

    cage.check(
        "ppl-code-worker marking its own finding fixed via the ppl CLI is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl findings status --finding-id F1 --status fixed "
                                 "--fix-link x --generation 1"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker deleting a finding via the ppl CLI is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "ppl findings delete --finding-id F1 --reason done"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker recording its own passing evidence via the ppl CLI is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl evidence add --check build --result pass "
                                 "--generation 1"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker opening its own done gate via the ppl CLI is denied",
        cage.payload("ppl-code-worker", "Bash", {"command": "ppl transition done"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker listing findings via the ppl CLI (read-only) is allowed",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl findings list --task %s" % cage.task_id}, wt),
        "allow")

    # --- round 2 review: the first version of this check called .search()
    # once, so a read-only ppl call in front of anything at all waved the
    # whole compound command through, and treated an unparseable ppl
    # invocation (e.g. a reordered --task flag) as a non-match - i.e. an
    # allow - instead of as unclassifiable and therefore denied. ---

    cage.check(
        "a read-only ppl call does not hide a second, unsafe one after &&",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl show --json && ppl findings status abc fixed "
                                 "--fix-link x --generation 1"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a read-only ppl call does not hide a second, unsafe one after ;",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl status; ppl transition done"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a read-only ppl call does not hide a second, unsafe one after &&, "
        "the findings-specific case",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl findings list && ppl findings delete abc "
                                 "--reason x"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a ppl invocation whose shape does not parse (a flag ahead of the "
        "subcommand) is denied, not allowed as a non-match",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl --task DEMO-1 findings status abc fixed"}, wt),
        "BOUNDARY_VIOLATION")

    # --- round 3 review: a worker whose PATH lacks `ppl` may reach for
    # `python3 -m ppl` / `python3 -m ppl.cli` with no intent to evade
    # anything - those shapes get the same allowlist and the same
    # fail-closed default as the word `ppl` itself. ---

    cage.check(
        "python3 -m ppl.cli invoking a state-changing subcommand is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "python3 -m ppl.cli findings status abc fixed"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "python3 -m ppl (no .cli) invoking a state-changing subcommand is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "python3 -m ppl transition done"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a read-only ppl call does not hide a python3 -m ppl.cli one after &&",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ppl show && python3 -m ppl.cli transition done"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "python3 -m ppl.cli invoking a read-only subcommand is still allowed",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "python3 -m ppl.cli show --task %s" % cage.task_id}, wt),
        "allow")

    cage.check(
        "a git stash tag that merely contains the substring ppl is still not "
        "a false positive (round 2's own guard, kept passing here)",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "git stash push -u -m ppl-700"}, unrelated),
        "allow")

    # --- Task 8: read isolation for the test worker. R-TEST-WORKER-READ-TOOLS
    # (exact: Read/Grep/Glob carry a path argument) and
    # R-TEST-WORKER-BASH-READS (lexical: Bash is a string). ---

    cage.check(
        "ppl-test-worker reading a main source via the Read tool is denied",
        cage.payload("ppl-test-worker", "Read",
                     {"file_path": wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker reading a test source via the Read tool is allowed",
        cage.payload("ppl-test-worker", "Read",
                     {"file_path": wt + "/source/MainTest.java"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker's Grep with no path argument falls back to cwd "
        "(the repo root, not a test path) and is denied",
        cage.payload("ppl-test-worker", "Grep", {"pattern": "TODO"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker's Glob scoped to a test source is allowed",
        cage.payload("ppl-test-worker", "Glob",
                     {"pattern": "*.java", "path": wt + "/source/src/test"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker cat of a main source via Bash is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "cat " + wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker cat of a test source via Bash is allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "cat " + wt + "/source/MainTest.java"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker brace read that expands out of src/test is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "cat source/src/test/{x,../../Main.java}"}, wt),
        "BOUNDARY_VIOLATION", reason="shell expansion")

    cage.check(
        "ppl-test-worker git diff with no pathspec is denied (an unrestricted "
        "diff can surface any file, main sources included)",
        cage.payload("ppl-test-worker", "Bash", {"command": "git diff"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker git diff with a global flag before the subcommand "
        "is still denied (the gates recipe itself uses git -C <worktree>)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git -C " + wt + " diff"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker grep -n of a main source is denied (a shared "
        "value-flag set would misread the pattern as -n's value)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "grep -n TODO " + wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker grep -e of a main source is denied (-e supplies the "
        "pattern as a flag value, not positionally)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "grep -e TODO " + wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker head -n of a test source is still allowed (head's "
        "own -n takes a count, unrelated to grep's -n)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "head -n 20 " + wt + "/source/MainTest.java"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker git diff is denied even restricted to a test path "
        "(round 1 review: git diff/show are not in the derived allowlist "
        "at all, the role has no documented need for either)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git diff -- source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    # --- Round 1 review, allowlist rewrite: the repo commands the role
    # genuinely needs (git status, git add of test paths, git commit, ls),
    # and the coordinator's 18-attempt probe against the old blocklist. ---

    cage.check(
        "ppl-test-worker git status is allowed",
        cage.payload("ppl-test-worker", "Bash", {"command": "git status"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker git add of a test path is allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git add " + wt + "/source/MainTest.java"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker git add of a main source is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git add " + wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker git commit with a message is allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git commit -m 'add tests'"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker ls is allowed",
        cage.payload("ppl-test-worker", "Bash", {"command": "ls -la source"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker command substitution denies the whole command, "
        "even hidden inside an otherwise-allowed shape's own argument",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git commit -m \"$(cat " + wt + "/source/Main.java)\""}, wt),
        "BOUNDARY_VIOLATION")

    for name, command in [
        ("nl", "nl " + wt + "/source/Main.java"),
        ("tac", "tac " + wt + "/source/Main.java"),
        ("cut", "cut -d' ' -f1 " + wt + "/source/Main.java"),
        ("od", "od -c " + wt + "/source/Main.java"),
        ("xxd", "xxd " + wt + "/source/Main.java"),
        ("strings", "strings " + wt + "/source/Main.java"),
        ("more", "more " + wt + "/source/Main.java"),
        ("diff", "diff " + wt + "/source/Main.java /dev/null"),
        ("git log -p", "git log -p"),
        ("git blame", "git blame " + wt + "/source/Main.java"),
        ("git cat-file", "git cat-file -p HEAD:source/Main.java"),
        ("python3 -c reading a file",
         "python3 -c \"print(open('source/Main.java').read())\""),
        ("while read redirected from a main source",
         "while read l; do echo $l; done < " + wt + "/source/Main.java"),
    ]:
        cage.check(
            "the coordinator's probe: %s is denied (round 1 review)" % name,
            cage.payload("ppl-test-worker", "Bash", {"command": command}, wt),
            "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker git show of a main-source object with no pathspec "
        "is denied",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "git show HEAD:source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a bash -c wrapper around cat of a main source does not hide the "
        "read - the literal word survives inside the quoted string",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "bash -c 'cat " + wt + "/source/Main.java'"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "an artifactId containing a verb word (cat-service) is not a false "
        "positive, same trap as the ppl-700 stash tag",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "mvn -pl :cat-service -am test"}, wt),
        "allow")

    cage.check(
        "the gates recipe's real mvn/test command shape is unaffected by the "
        "test-worker read isolation",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "JAVA_HOME=/opt/jdk mvn -pl :cat-service -am "
                                 "-Dtest=FooTest -DfailIfNoTests=false "
                                 "-Dmaven.gitcommitid.skip=true test "
                                 "> /tmp/focused.log 2>&1; echo $? > /tmp/focused.rc"}, wt),
        "allow")

    cage.check(
        "the gates recipe's mvn install shape (goal right after mvn) is allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "mvn install -pl :cat-service -am -DskipTests "
                                 "-Dmaven.gitcommitid.skip=true"}, wt),
        "allow")

    cage.check(
        "the gates recipe's mvn checkstyle:check shape is allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "mvn -pl :cat-service -am "
                                 "-Dmaven.gitcommitid.skip=true checkstyle:check"}, wt),
        "allow")

    cage.check(
        "the gates recipe's mvn verify shape is allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "mvn -pl :cat-service -am "
                                 "-Dmaven.gitcommitid.skip=true verify"}, wt),
        "allow")

    cage.check(
        "mvn with -pl/-am but no recognised goal is denied, not waved "
        "through just because it looks like an mvn invocation",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "mvn -pl :cat-service -am "
                                 "-Dexec.mainClass=Foo"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "cd into a test dir then climbing back out past it with .. is denied "
        "(the launch cwd does not move, but the cd inside the command does)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "cd source && cat ../source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker reading a test source is not restricted by this "
        "check (the mirror decision: reads are not denied)",
        cage.payload("ppl-code-worker", "Read",
                     {"file_path": wt + "/source/MainTest.java"}, wt),
        "allow")

    # --- C1 (whole-branch review): the write boundary ran only for
    # Edit/Write/MultiEdit/NotebookEdit, and both workers hold Bash. Every
    # case below is the exact repro table from the review, plus the shapes
    # cp/sed -i/command-substitution add on top of a plain redirect. ---

    cage.check(
        "ppl-code-worker Bash redirect into a test source is denied "
        "(C1 repro row 1)",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "echo x > " + wt + "/source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker Bash redirect into a main source is denied, "
        "absolute path (C1 repro row 3)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "echo pwned > " + wt + "/source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker Bash redirect into a main source is denied, "
        "relative path (C1 repro row 4)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "echo pwned > source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "an unroled session's Bash redirect into the run's repo is denied, "
        "from a cwd outside it (C1 repro row 5)",
        cage.payload(None, "Bash",
                     {"command": "echo x > " + wt + "/source/Main.java"}, unrelated),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker Bash redirect inside its own write boundary is "
        "still allowed",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "echo x > source/Main.java"}, wt),
        "allow")

    cage.check(
        "ppl-test-worker Bash redirect into a test source is still allowed",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "echo x > source/src/test/OtherTest.java"}, wt),
        "allow")

    cage.check(
        "a role's Bash build-log redirect outside the repo is unaffected "
        "(the gates recipe's own convention)",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "mvn -pl :m -am test > /tmp/build.log 2>&1; "
                                 "echo $? > /tmp/build.rc"}, wt),
        "allow")

    cage.check(
        "ppl-code-worker cp-ing a file into a test source is denied - not "
        "a redirect, but the same boundary decision must still apply",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "cp /tmp/x.java source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker rm outside the repo is unaffected",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "rm /tmp/scratch.txt"}, wt),
        "allow")

    cage.check(
        "ppl-code-worker's ambiguous sed -i on a repo path is denied - "
        "fail closed, GNU's attached suffix and BSD's separate one cannot "
        "be told apart lexically",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "sed -i 's/a/b/' source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a command substitution hiding a write-shaped construct's real "
        "target is denied rather than skipped",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "cp $(pwd)/x.java source/Main.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "an unrelated session's ambiguous sed -i elsewhere is NOT denied - "
        "the fail-closed direction must not leak into other projects",
        cage.payload(None, "Bash",
                     {"command": "sed -i 's/a/b/' notes.txt"}, unrelated),
        "allow")

    cage._store.write_json(cage.run_dir() / "boundary.json",
                           {"write": ["source/**"], "forbid": ["source/secrets/**"]})
    cage.check(
        "ppl-code-worker Bash write to a forbidden path is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "echo x > source/secrets/key.pem"}, wt),
        "BOUNDARY_VIOLATION")
    cage._store.write_json(cage.run_dir() / "boundary.json",
                           {"write": ["source/**"], "forbid": []})

    # --- Round 2 (whole-branch review, wave A): touch/truncate/ln/install
    # were missing from BASH_WRITE_SHAPES. Probed against ppl-code-worker,
    # which has no read allowlist to accidentally cover them the way the
    # test worker's did. ---

    cage.check(
        "ppl-code-worker touch of a test source is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "touch " + wt + "/source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker truncate of a test source is denied",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "truncate -s 0 " + wt + "/source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker ln replacing a test source is denied - the "
        "linkname is checked, not the symlink's target",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "ln -sf /etc/passwd " + wt + "/source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker install into a test source is denied - the "
        "destination (last argument) is checked, not the source",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "install -m 644 /dev/null " + wt + "/source/MainTest.java"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-code-worker touch inside its own write boundary is still allowed",
        cage.payload("ppl-code-worker", "Bash",
                     {"command": "touch " + wt + "/source/Main.java"}, wt),
        "allow")

    # --- C2 (whole-branch review): _read_verb_allowed treated "no path
    # operand" as "filtering a pipe" for every read verb - true only of a
    # genuine pipe. rg is recursive by default with no path at all, and
    # GNU grep -r/-R defaults to the cwd - neither reads stdin, piped or not. ---

    cage.check(
        "ppl-test-worker rg with no path is denied (C2 repro)",
        cage.payload("ppl-test-worker", "Bash", {"command": "rg 'class Main'"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker rg -n with no path is denied (C2 repro)",
        cage.payload("ppl-test-worker", "Bash", {"command": "rg -n pwn"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker grep -r with no path is denied (C2 repro)",
        cage.payload("ppl-test-worker", "Bash", {"command": "grep -r pwn"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker grep -rn (bundled) with no path is denied",
        cage.payload("ppl-test-worker", "Bash", {"command": "grep -rn pwn"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker piped grep -rn (bundled) is still denied - the "
        "discriminating case, since an unpiped command already denies for "
        "the other reason and cannot prove the bundling check does anything",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "echo x | grep -rn pwn"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker grep -R with no path is denied",
        cage.payload("ppl-test-worker", "Bash", {"command": "grep -R pwn"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "ppl-test-worker grep --include=*.java -r with no path is denied "
        "(C2 repro)",
        cage.payload("ppl-test-worker", "Bash",
                     {"command": "grep --include=*.java -r class"}, wt),
        "BOUNDARY_VIOLATION")

    cage.check(
        "the genuine pipe-filter case stays allowed",
        cage.payload("ppl-test-worker", "Bash", {"command": "echo hello | grep hello"}, wt),
        "allow")

    # --- R-TEST-WORKER-DISPATCH-LEAK: the orchestrator's Agent tool call that
    # starts ppl-test-worker carries the dispatch prompt, and the hook denies
    # it when that prompt holds lines the implementation added since base. ---
    head = subprocess.run(["git", "-C", str(cage.worktree), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()
    state = cage._run.load(cage.task_id)
    state["base_sha"] = head
    cage._run.save(state)
    added = ("    long discount = amountCents * percent / 100;\n"
             "    return Math.max(0L, amountCents - discount);\n")
    (cage.worktree / "source" / "Price.java").write_text(
        "class Price {\n" + added + "}\n", encoding="utf-8")

    def dispatch(prompt):
        return {"hook_event_name": "PreToolUse", "tool_name": "Agent", "cwd": str(cage.unrelated),
                "tool_input": {"description": "tests", "prompt": prompt,
                               "subagent_type": "ppl-test-worker"}}

    cage.check(
        "a ppl-test-worker dispatch carrying two added implementation lines is denied",
        dispatch("AC-1: 10 percent off 1000 gives 900.\n" + added),
        "BOUNDARY_VIOLATION")

    cage.check(
        "a ppl-test-worker dispatch of acceptance criteria only is allowed",
        dispatch("AC-1: 10 percent off 1000 gives 900.\nAC-2: never below zero."),
        "allow")


def run_os_sandbox_cases(cage):
    """Who may leave the OS sandbox, on the python cage's GitHub Issues run: the guard facts
    `ppl harness apply` writes, pins that need not exist, and the unrelated directory as the
    workspace, which no sandboxed command can write [R-OS-SANDBOX-ROLE-NO-ESCAPE]
    [R-OS-SANDBOX-ORCHESTRATOR-SHAPE]."""
    real = os.path.realpath
    home = cage.ppl_home
    workspace = str(cage.unrelated)
    pins = {name: {"path": "/opt/pin/" + name, "realpath": real("/opt/pin/" + name)}
            for name in ("gh", "git", "ppl")}
    facts = {"version": 1, "workspace": real(workspace), "checkout": real(str(cage.worktree)),
             "worktrees": real(str(cage.worktree) + "/.worktrees"), "default_branch": "main",
             "forge_repo": "o/r", "tracker_repo": "o/r", "scratch": real(str(home / "scratch")),
             "write_scope": [real(str(home / part)) for part in ("runs", "labels", "scratch")]
             + [real(str(cage.worktree))],
             "pins": pins}
    cage._store.write_json(home / "os_sandbox.json", facts)
    state = cage._run.load(cage.task_id)
    state["branch"] = "cage-branch"
    cage._run.save(state)
    body = home / "scratch" / cage.task_id / "pr-body.md"
    body.parent.mkdir(parents=True, exist_ok=True)
    body.write_text("body\n", encoding="utf-8")
    gh = pins["gh"]["path"]

    def flagged(agent, command, cwd):
        return cage.payload(agent, "Bash",
                            {"command": command, "dangerouslyDisableSandbox": True}, cwd)

    cage.check(
        "os-sandbox role escape denied",
        flagged("ppl-code-worker", "ls", str(cage.worktree)),
        "BOUNDARY_VIOLATION", reason="outside the OS sandbox")

    cage.check(
        "os-sandbox orchestrator plain pr create allowed",
        flagged(None, "%s pr create -R o/r --head cage-branch --base main --title 'Fix $HOME' "
                "--body-file %s" % (gh, body), workspace),
        "allow")

    cage.check(
        "os-sandbox orchestrator compound denied",
        flagged(None, gh + " auth status; id", workspace),
        "BOUNDARY_VIOLATION", reason="one plain command")


if __name__ == "__main__":
    sys.exit(main())

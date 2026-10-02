import json
import os
import shlex
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "hooks"))
import ppl_guard
from ppl import harness, tomlout
from ppl.errors import PplError
from ppl.studio import stacks

PLACEHOLDERS = {"<test_class>": "FooTest", "<module>": "core"}


def project(path, forge=None):
    return {"path": str(path), "default_branch": "main", "forge_hint": forge, "name_hint": "demo"}


def empty_dir(prefix="pplstack-"):
    return Path(tmphome.mkdtemp(prefix=prefix))


def install_draft(draft):
    target = empty_dir("pplharness-") / "demo"
    for rel, text in draft["fragments"].items():
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(text, encoding="utf-8")
    (target / "harness.toml").write_text(tomlout.dumps(draft["doc"]), encoding="utf-8")
    return harness.load(str(target))


class TestDetect(unittest.TestCase):
    def test_each_build_file_names_its_stack(self):
        cases = (("package.json", "node"), ("pyproject.toml", "python"),
                 ("requirements.txt", "python"), ("pom.xml", "maven"),
                 ("build.gradle.kts", "gradle"), ("go.mod", "go"), ("Cargo.toml", "rust"),
                 ("Makefile", "make"))
        for marker, stack in cases:
            root = empty_dir()
            (root / marker).write_text("\n", encoding="utf-8")
            self.assertEqual(stacks.detect(root)["stack"], stack, marker)

    def test_a_repo_with_no_build_file_is_generic(self):
        self.assertEqual(stacks.detect(empty_dir()),
                         {"stack": "generic", "found": [], "label": stacks.STACKS["generic"]["label"]})

    def test_every_stack_found_is_listed_and_the_first_in_order_wins(self):
        root = empty_dir()
        (root / "Makefile").write_text("\n", encoding="utf-8")
        (root / "package.json").write_text("{}\n", encoding="utf-8")
        found = stacks.detect(root)
        self.assertEqual(found["found"], ["node", "make"])
        self.assertEqual(found["stack"], "node")


class TestDrafts(unittest.TestCase):
    def test_every_stack_draft_validates_and_renders_without_an_open_marker(self):
        for stack in stacks.STACKS:
            doc = install_draft(stacks.draft(stack, project(empty_dir("pplrepo-"))))
            warnings = harness.validate(doc, "demo")
            self.assertEqual([w for w in warnings if harness.FOCUSED_TESTS_GATE in w], [], stack)
            rendered = harness.render_skill(doc)
            self.assertFalse(any("<!-- include:" in text for text in rendered.values()), stack)

    def test_every_stack_focused_test_command_is_allowed_for_the_test_worker(self):
        for stack in stacks.STACKS:
            draft = stacks.draft(stack, project(empty_dir("pplrepo-")))
            command = next(gate["command"] for gate in draft["doc"]["gate"]
                           if gate["name"] == harness.FOCUSED_TESTS_GATE)
            for placeholder, value in PLACEHOLDERS.items():
                command = command.replace(placeholder, value)
            matcher = ppl_guard.ShapeMatcher.from_rows(harness.shape_rows(draft["doc"]))
            self.assertTrue(matcher.allows(shlex.split(command)), (stack, command))

    def test_node_scripts_add_lint_and_build_gates(self):
        root = empty_dir("pplrepo-")
        (root / "package.json").write_text(json.dumps(
            {"scripts": {"lint": "eslint .", "build": "tsc", "test": "jest"}}), encoding="utf-8")
        draft = stacks.draft("node", project(root))
        self.assertEqual([gate["name"] for gate in draft["doc"]["gate"]],
                         ["install", "lint", "build", "focused-tests", "integration-tests"])
        self.assertEqual(draft["sources"]["gate:lint"], "package.json scripts.lint")
        self.assertEqual(draft["sources"]["gate:install"], "preset node")

    def test_a_broken_package_json_keeps_the_preset_gates(self):
        root = empty_dir("pplrepo-")
        (root / "package.json").write_text("not json", encoding="utf-8")
        draft = stacks.draft("node", project(root))
        self.assertEqual([gate["name"] for gate in draft["doc"]["gate"]],
                         ["install", "focused-tests", "integration-tests"])

    def test_the_forge_follows_the_remote_hint(self):
        gitlab = stacks.draft("make", project(empty_dir("pplrepo-"), forge="gitlab"))
        self.assertEqual(gitlab["doc"]["forge"], {"kind": "gitlab", "mcp_server": "GitLab-MCP"})
        self.assertEqual(gitlab["sources"]["forge.kind"], "git remote")
        unknown = stacks.draft("make", project(empty_dir("pplrepo-")))
        self.assertEqual(unknown["doc"]["forge"]["kind"], "github")
        self.assertEqual(unknown["sources"]["forge.kind"], "preset make")

    def test_the_draft_takes_the_project_path_branch_and_name(self):
        root = empty_dir("pplrepo-")
        doc = stacks.draft("go", project(root))["doc"]
        self.assertEqual(doc["repo"], {"path": str(root), "default_branch": "main",
                                       "worktrees": str(root) + "/.worktrees"})
        self.assertEqual(doc["harness"]["name"], "demo")
        self.assertEqual(doc["env"], {})

    def test_an_unknown_stack_is_refused(self):
        with self.assertRaises(PplError) as caught:
            stacks.draft("cobol", project(empty_dir("pplrepo-")))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

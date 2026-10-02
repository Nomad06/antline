import json
import os
import re
import shlex
import sys
import threading
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

PLACEHOLDERS = {"<test_class>": "app.FooTest", "<module>": "core"}


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
            self.assertEqual(warnings, [], stack)
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

    def test_a_package_json_symlinked_outside_the_repo_adds_no_gates(self):
        outside = empty_dir("pplout-") / "package.json"
        outside.write_text(json.dumps({"scripts": {"lint": "x", "build": "y"}}), encoding="utf-8")
        root = empty_dir("pplrepo-")
        (root / "package.json").symlink_to(outside)
        draft = stacks.draft("node", project(root))
        self.assertEqual([gate["name"] for gate in draft["doc"]["gate"]],
                         ["install", "focused-tests", "integration-tests"])

    def test_a_package_json_symlinked_inside_the_repo_is_still_read(self):
        root = empty_dir("pplrepo-")
        (root / "app").mkdir()
        (root / "app" / "package.json").write_text(json.dumps({"scripts": {"lint": "x"}}),
                                                   encoding="utf-8")
        (root / "package.json").symlink_to(root / "app" / "package.json")
        draft = stacks.draft("node", project(root))
        self.assertEqual([gate["name"] for gate in draft["doc"]["gate"]],
                         ["install", "lint", "focused-tests", "integration-tests"])

    def test_an_oversized_package_json_adds_no_gates(self):
        root = empty_dir("pplrepo-")
        (root / "package.json").write_text(json.dumps(
            {"scripts": {"lint": "x"}, "padding": "a" * 300_000}), encoding="utf-8")
        draft = stacks.draft("node", project(root))
        self.assertEqual([gate["name"] for gate in draft["doc"]["gate"]],
                         ["install", "focused-tests", "integration-tests"])

    def test_a_deeply_nested_package_json_adds_no_gates(self):
        root = empty_dir("pplrepo-")
        (root / "package.json").write_text("[" * 150_000, encoding="utf-8")
        draft = stacks.draft("node", project(root))
        self.assertEqual([gate["name"] for gate in draft["doc"]["gate"]],
                         ["install", "focused-tests", "integration-tests"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs mkfifo")
    def test_a_fifo_package_json_does_not_hang_the_draft(self):
        root = empty_dir("pplrepo-")
        fifo = root / "package.json"
        os.mkfifo(fifo)
        self.addCleanup(self._release, fifo)
        result = {}
        worker = threading.Thread(
            target=lambda: result.update(draft=stacks.draft("node", project(root))), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "the draft is blocked reading the fifo")
        self.assertEqual([gate["name"] for gate in result["draft"]["doc"]["gate"]],
                         ["install", "focused-tests", "integration-tests"])

    @staticmethod
    def _release(fifo):
        try:
            os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass

    def test_the_forge_follows_the_remote_hint(self):
        gitlab = stacks.draft("make", project(empty_dir("pplrepo-"), forge="gitlab"))
        self.assertEqual(gitlab["doc"]["forge"], {"kind": "gitlab", "mcp_server": "GitLab-MCP"})
        self.assertEqual(gitlab["sources"]["forge.kind"], "git remote")
        github = stacks.draft("make", project(empty_dir("pplrepo-"), forge="github"))
        self.assertEqual(github["doc"]["forge"], {"kind": "github"})
        self.assertEqual(github["sources"]["forge.kind"], "git remote")

    def test_every_stack_drafts_github_when_the_remote_gives_no_hint(self):
        for stack in stacks.STACKS:
            unknown = stacks.draft(stack, project(empty_dir("pplrepo-")))
            self.assertEqual(unknown["doc"]["forge"], {"kind": "github"}, stack)
            self.assertEqual(unknown["sources"]["forge.kind"], "preset %s" % stack)

    def test_a_gitlab_hint_on_the_maven_preset_keeps_the_mcp_server(self):
        draft = stacks.draft("maven", project(empty_dir("pplrepo-"), forge="gitlab"))
        self.assertEqual(draft["doc"]["forge"], {"kind": "gitlab", "mcp_server": "GitLab-MCP"})

    def test_the_draft_takes_the_project_path_branch_and_name(self):
        root = empty_dir("pplrepo-")
        doc = stacks.draft("go", project(root))["doc"]
        self.assertEqual(doc["repo"], {"path": str(root), "default_branch": "main",
                                       "worktrees": str(root) + "/.worktrees"})
        self.assertEqual(doc["harness"]["name"], "demo")
        self.assertEqual(doc["env"], {})

    def test_node_build_note_follows_an_added_build_script(self):
        root = empty_dir("pplrepo-")
        (root / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8")
        note = stacks.draft("node", project(root))["fragments"]["templates/ppl-code-worker.part"]
        self.assertIn("`npm run build`", note)
        self.assertNotIn("npm ci", note)
        bare = empty_dir("pplrepo-")
        (bare / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
        note = stacks.draft("node", project(bare))["fragments"]["templates/ppl-code-worker.part"]
        self.assertIn("`npm ci`", note)

    def test_python_installs_from_requirements_only_when_nothing_else_names_the_project(self):
        cases = ((("requirements.txt",), "python3 -m pip install -r requirements.txt"),
                 (("requirements.txt", "pyproject.toml"), "python3 -m pip install -e ."),
                 (("requirements.txt", "setup.py"), "python3 -m pip install -e ."),
                 (("requirements.txt", "setup.cfg"), "python3 -m pip install -e ."),
                 (("pyproject.toml",), "python3 -m pip install -e ."),
                 ((), "python3 -m pip install -e ."))
        for markers, install in cases:
            root = empty_dir("pplrepo-")
            for marker in markers:
                (root / marker).write_text("\n", encoding="utf-8")
            draft = stacks.draft("python", project(root))
            gates = {gate["name"]: gate["command"] for gate in draft["doc"]["gate"]}
            self.assertEqual(gates["install"], install, markers)
            self.assertIn("`%s`" % install, draft["fragments"]["templates/ppl-code-worker.part"])
            self.assertEqual(draft["sources"]["gate:install"], "preset python")

    def test_python_gates_run_pytest_directly(self):
        draft = stacks.draft("python", project(empty_dir("pplrepo-")))
        gates = {gate["name"]: gate["command"] for gate in draft["doc"]["gate"]}
        self.assertEqual(gates[harness.FOCUSED_TESTS_GATE], "pytest -q <test_class>")
        self.assertEqual(gates["integration-tests"], "pytest -q")
        self.assertEqual(draft["doc"]["guard"]["shape"][0]["argv0"], "pytest")


    def test_an_unknown_stack_is_refused(self):
        with self.assertRaises(PplError) as caught:
            stacks.draft("cobol", project(empty_dir("pplrepo-")))
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")


class TestShapesStayNarrow(unittest.TestCase):
    ALLOWED = {
        "node": ("npm test", "npm test -- app.FooTest", "npm test -- --verbose",
                 "npm test -- tests/app.test.js"),
        "python": ("pytest -q", "pytest -q app.FooTest", "pytest -q tests/test_x.py::T::t",
                   "pytest -q --no-header tests/test_x.py"),
        "gradle": ("./gradlew test --tests app.FooTest", "./gradlew test", "./gradlew test --info"),
        "go": ("go test ./...", "go test ./... -run app.FooTest", "go test ./... -run TestFoo/",
               "go test -v ./pkg/..."),
        "rust": ("cargo test", "cargo test mod::my_test", "cargo test --no-fail-fast"),
    }
    DENIED = {
        "node": ("npm ci", "npm run evil test", "npm run x test", "npm install left-pad test", "npm test evil",
                 "npm test -- -o=x", "npm test -- /etc/passwd", "npm test -- ../x.js",
                 "NODE_OPTIONS=--require=/tmp/x.js npm test", "CI=1 npm test"),
        "python": ("python3 foo.py -m pytest", "python3 -c pass -m pytest", "pytest /etc/passwd -q",
                   "pytest -q /etc/passwd", "pytest -q ../tests/test_x.py", "pytest -q evil",
                   "pytest", "pytest -q @src/app/main.py", "pytest -q @notes.txt",
                   "PYTEST_ADDOPTS=@notes.txt pytest -q", "CI=1 pytest -q"),
        "gradle": ("./gradlew publish test", "./gradlew test publish", "./gradlew assemble",
                   "./gradlew test --tests evil", "./gradlew test app:publish",
                   "./gradlew test :publish", "./gradlew app:publish test --tests app.FooTest",
                   "./gradlew test app:run", "./gradlew test --tests app:FooTest",
                   "JAVA_TOOL_OPTIONS=-javaagent:/tmp/x.jar ./gradlew test"),
        "go": ("go doc -src fmt.Println test", "go doc fmt test", "go run x.go test", "go test run", "go build ./...",
               "go test -coverprofile=c.out ./...", "go test ../x",
               "GOFLAGS=-toolexec=/tmp/x go test ./..."),
        "rust": ("cargo run test", "cargo install foo test", "cargo build", "cargo test evil",
                 "cargo test --target-dir=/tmp", "cargo test x:../y", "cargo test ..::sibling",
                 "RUSTC_WRAPPER=/tmp/x cargo test"),
    }

    @staticmethod
    def matcher(stack):
        draft = stacks.draft(stack, project(empty_dir("pplrepo-")))
        return ppl_guard.ShapeMatcher.from_rows(harness.shape_rows(draft["doc"]))

    def test_the_commands_a_test_writer_needs_stay_allowed(self):
        for stack, commands in self.ALLOWED.items():
            matcher = self.matcher(stack)
            for command in commands:
                self.assertTrue(matcher.allows(shlex.split(command)), (stack, command))

    def test_other_subcommands_and_stray_targets_are_denied(self):
        # rule: R-STUDIO-EXTRACT-LEAN
        for stack, commands in self.DENIED.items():
            matcher = self.matcher(stack)
            for command in commands:
                self.assertFalse(matcher.allows(shlex.split(command)), (stack, command))

    def test_an_env_assignment_before_the_command_is_denied(self):
        for stack in ("node", "python", "gradle", "go", "rust"):
            draft = stacks.draft(stack, project(empty_dir("pplrepo-")))
            self.assertEqual([row["env_prefix"] for row in draft["doc"]["guard"]["shape"]],
                             [False], stack)

    def test_the_example_harness_shapes_keep_their_env_prefix(self):
        for stack in ("maven", "make", "generic"):
            draft = stacks.draft(stack, project(empty_dir("pplrepo-")))
            self.assertEqual([row["env_prefix"] for row in draft["doc"]["guard"]["shape"]],
                             [True], stack)

    def test_each_free_arg_shape_keeps_only_the_test_goal(self):
        for stack in ("node", "gradle", "go", "rust"):
            draft = stacks.draft(stack, project(empty_dir("pplrepo-")))
            self.assertEqual([row["one_goal_of"] for row in draft["doc"]["guard"]["shape"]],
                             [["test"]], stack)


class TestGateFragments(unittest.TestCase):
    NOTE = ("- Run only `%s` for focused runs. Name the test with a path or a qualified name "
            "(`app.FooTest`, `mod::my_test`, `tests/test_x.py::T::t`, `./pkg/...`): "
            "a plain word is refused.\n")
    GO_NOTE = ("- Run only `%s` for focused runs. Name the test with a trailing slash "
               "(`-run TestFoo/`), or run `go test ./pkg/...` for one package: "
               "a plain word is refused.\n")
    RUST_NOTE = ("- Run only `%s` for focused runs. Name the test by its module path "
                 "(`mod::my_test`): a plain word is refused.\n")
    GRADLE_NOTE = ("- Run only `%s` for focused runs. Name the test by its class or method "
                   "(`--tests app.FooTest`, `--tests app.FooTest.bar`): a plain word is refused.\n")
    NOTES = {"go": GO_NOTE, "rust": RUST_NOTE, "gradle": GRADLE_NOTE}

    def test_the_test_worker_part_tells_how_to_name_the_test(self):
        gates = [{"name": "focused-tests", "command": "make test TEST=<test_class>"}]
        part = stacks.gate_fragments(gates)["templates/ppl-test-worker.part"]
        self.assertEqual(part, self.NOTE % "make test TEST=<test_class>")
        self.assertEqual(part.count("\n"), 1)
        part.encode("ascii")

    def test_the_naming_example_follows_the_stack(self):
        gates = [{"name": "focused-tests", "command": "go test ./... -run <test_class>"}]
        go = stacks.gate_fragments(gates, stack="go")["templates/ppl-test-worker.part"]
        self.assertEqual(go, self.GO_NOTE % "go test ./... -run <test_class>")
        rust = stacks.gate_fragments(gates, stack="rust")["templates/ppl-test-worker.part"]
        self.assertEqual(rust, self.RUST_NOTE % "go test ./... -run <test_class>")
        gradle = stacks.gate_fragments(gates, stack="gradle")["templates/ppl-test-worker.part"]
        self.assertEqual(gradle, self.GRADLE_NOTE % "go test ./... -run <test_class>")
        for stack in ("node", "python", "make", "generic", "no-such-stack", None):
            part = stacks.gate_fragments(gates, stack=stack)["templates/ppl-test-worker.part"]
            self.assertEqual(part, self.NOTE % "go test ./... -run <test_class>", stack)

    def test_every_stack_draft_carries_the_naming_note_for_its_focused_command(self):
        for stack, spec in stacks.STACKS.items():
            if "gates" not in spec:
                continue
            command = next(gate["command"] for gate in spec["gates"]
                           if gate["name"] == harness.FOCUSED_TESTS_GATE)
            draft = stacks.draft(stack, project(empty_dir("pplrepo-")))
            self.assertEqual(draft["fragments"]["templates/ppl-test-worker.part"],
                             self.NOTES.get(stack, self.NOTE) % command, stack)

    def test_gate_fragment_keys_name_every_part_the_gates_write(self):
        gates = [{"name": "build", "command": "make build"},
                 {"name": harness.FOCUSED_TESTS_GATE, "command": "make test T=<test_class>"}]
        self.assertEqual(set(stacks.gate_fragments(gates)), set(stacks.GATE_FRAGMENT_KEYS))

    def test_no_focused_tests_gate_means_no_test_worker_part(self):
        fragments = stacks.gate_fragments([{"name": "build", "command": "make build"}])
        self.assertNotIn("templates/ppl-test-worker.part", fragments)


class TestInterpreters(unittest.TestCase):
    def test_the_set_holds_the_shells_script_runners_and_wrappers(self):
        self.assertIsInstance(stacks.INTERPRETERS, frozenset)
        self.assertEqual(stacks.INTERPRETERS,
                         frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh",
                                    "python", "node", "perl", "ruby", "php", "lua", "tclsh",
                                    "pwsh", "osascript", "awk", "gawk", "mawk", "busybox", "env",
                                    "sudo", "doas", "xargs", "eval", "exec", "source", ".",
                                    "command", "nohup", "time", "timeout", "nice", "nodejs",
                                    "pypy", "ipython", "ash", "rbash", "xonsh", "nu", "tsx",
                                    "ts-node", "java", "jshell", "ssh", "find", "expect"}))

    def test_a_version_suffix_and_a_directory_do_not_hide_an_interpreter(self):
        for argv0 in ("python3.12", "python3", "python", "node18", "perl5.36", "ruby3.3",
                      "bash5.2", "./python3.11", "/usr/bin/env", "tools/bash", "awk", "gawk",
                      "busybox", ".", "source", "./.", "command", "timeout", "sh", "Bash", "SH",
                      "PYTHON3.12", "Node18", "bash-5.2", "sh_5", "nodejs", "pypy3", "ts-node18",
                      "./Python3.11", "/USR/BIN/ENV", "ssh", "find", "java", "expect"):
            with self.subTest(argv0=argv0):
                self.assertTrue(stacks.is_interpreter(argv0))

    def test_the_ordinary_build_and_test_programs_are_not_interpreters(self):
        for argv0 in ("pytest", "npx", "npm", "./gradlew", "gradle", "cargo", "go", "go1.22",
                      "make", "mvn", "mvn3", "./mvnw", "tools/run-tests", "ctest", "bazel"):
            with self.subTest(argv0=argv0):
                self.assertFalse(stacks.is_interpreter(argv0))

    def test_no_preset_shape_runs_an_interpreter(self):
        for stack, spec in stacks.STACKS.items():
            for row in spec.get("shapes", []):
                self.assertFalse(stacks.is_interpreter(row["argv0"]), stack)


class TestSafeFreeArgs(unittest.TestCase):
    ACCEPTED = ("-v", "-q", "-run", "--tests", "--no-fail-fast", "--", "./...", "./pkg/...",
                "app.FooTest", "mod::my_test", "tests/test_x.py::T::t", "src/app.test.js",
                "a-b.c", "TestFoo/", "TestFoo/sub")
    REFUSED = ("doc", "run", "publish", "install", "test", "FooTest", "", " ", "a b.c", "a;b.c",
               "a.b;c", "$x.y", "a.$b", "*.js", "./*", "a.b\nc", "-v\n", "/etc/passwd", "/x.y",
               "~/x.y", "..", "../x.y", "a/../b.c", "a/..", ".", "-o=x", "--target-dir=/tmp",
               "-coverprofile=c.out", "-", "---x", "-1", "--1", "@src/app/main.py", "@scope/pkg.js",
               "@notes.txt", "x:../y", "..::sibling", "a/..::x", "a::..", "a:..:b")

    def test_accepts_flags_and_dotted_or_pathlike_targets(self):
        for token in self.ACCEPTED:
            self.assertIsNotNone(re.fullmatch(stacks.SAFE_FREE_ARGS, token), token)

    def test_refuses_plain_words_metacharacters_and_escapes(self):
        # rule: R-STUDIO-EXTRACT-LEAN
        for token in self.REFUSED:
            self.assertFalse(re.fullmatch(stacks.SAFE_FREE_ARGS, token), repr(token))


class TestSafeGradleArgs(unittest.TestCase):
    ACCEPTED = ("-v", "--tests", "--info", "--", "./...", "app.FooTest", "src/app.test.java",
                "TestFoo/")
    REFUSED = ("publish", "FooTest", "", "app:publish", ":publish", "app:run", "mod::my_test",
               "a.b:c", "tests/x.py::T", "a b.c", "a;b.c", "$x.y", "*.js", "/etc/passwd",
               "../x.y", "a/../b.c", "a/..", "@src/app/main.py", "-o=x", "--tests=app.FooTest")

    def test_accepts_flags_and_dotted_or_pathlike_targets(self):
        for token in self.ACCEPTED:
            self.assertIsNotNone(re.fullmatch(stacks.SAFE_GRADLE_ARGS, token), token)

    def test_refuses_a_task_path_besides_the_other_escapes(self):
        # rule: R-STUDIO-EXTRACT-LEAN
        for token in self.REFUSED:
            self.assertFalse(re.fullmatch(stacks.SAFE_GRADLE_ARGS, token), repr(token))

    def test_the_gradle_note_examples_pass_the_gradle_class(self):
        for token in ("--tests", "app.FooTest", "app.FooTest.bar"):
            self.assertIsNotNone(re.fullmatch(stacks.SAFE_GRADLE_ARGS, token), token)

    def test_only_the_gradle_shape_uses_the_class_without_a_colon(self):
        for stack, spec in stacks.STACKS.items():
            for row in spec.get("shapes", []):
                want = stacks.SAFE_GRADLE_ARGS if stack == "gradle" else stacks.SAFE_FREE_ARGS
                self.assertEqual(row["free_args"], want, stack)


class TestModuleNotes(unittest.TestCase):
    def test_the_residual_lexical_limit_covers_reads_writes_and_execs(self):
        doc = " ".join(stacks.__doc__.split())
        self.assertIn("read, write or exec target", doc)
        for example in ("go test -gcflags -m", "go test -o x.bin", "pytest --basetemp ./src"):
            self.assertIn(example, doc)


if __name__ == "__main__":
    unittest.main()

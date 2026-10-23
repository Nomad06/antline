"""Stack presets, for a project with no CLAUDE.md or AGENTS.md to draft from
or when the extraction fails: the build files in the repo root name the
stack, and the stack names its gates and the command shapes the test worker
may run. Every preset is built on one of the two example harnesses, so the
tracker lists and the skill fragments come from one place. The shapes are
lexical, like the shipped examples: a bare flag the tool reads as a read,
write or exec target (for example `go test -gcflags -m`, `go test -o x.bin`
or `pytest --basetemp ./src`) still passes."""
import copy
import json
import os
import re
import stat
import tomllib
from pathlib import Path

from .. import boundary, harness, paths, trackers
from ..errors import PplError

# The tokens a test-worker command may carry besides its goal. The cage matches
# them anywhere in the line, so a plain word (a subcommand such as `run`, `doc`
# or `publish`) is refused: only a bare dash flag with no attached value, `--`,
# or a relative path or qualified name carrying a `.`, `/` or `:` passes.
# Never an absolute path, a `..` segment, an `@` argument file, a space or a
# shell metacharacter.
SAFE_FREE_ARGS = (r"^(?:--?[A-Za-z][A-Za-z0-9-]*|--"
                  r"|(?!(?:.*[/:])?\.\.(?:[/:]|$))[A-Za-z0-9_+,.][A-Za-z0-9_+,-]*[./:][A-Za-z0-9_./:+,-]*)$")
# Gradle reads `sub:task` and Maven `plugin:goal` as another task to run, so the
# class of their shapes (colon_runs_a_task) has no `:` at all.
SAFE_GRADLE_ARGS = (r"^(?:--?[A-Za-z][A-Za-z0-9-]*|--"
                    r"|(?!(?:.*/)?\.\.(?:/|$))[A-Za-z0-9_+,.][A-Za-z0-9_+,-]*[./][A-Za-z0-9_./+,-]*)$")
COLON_TASK_PROGRAMS = frozenset({"gradle", "gradlew", "mvn", "mvnw"})

# Programs that run whatever they are given: a test-worker shape for one of them
# would let the worker run any script or command line. Matched by is_interpreter,
# so a version suffix and a directory do not hide one.
INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "csh", "tcsh", "python",
                          "node", "perl", "ruby", "php", "lua", "tclsh", "pwsh", "osascript",
                          "awk", "gawk", "mawk", "busybox", "env", "sudo", "doas", "xargs", "eval",
                          "exec", "source", ".", "command", "nohup", "time", "timeout", "nice",
                          "nodejs", "pypy", "ipython", "ash", "rbash", "xonsh", "nu", "tsx",
                          "ts-node", "java", "jshell", "ssh", "find", "expect"})
_VERSION_SUFFIX_RE = re.compile(r"[-_]?[0-9][0-9.]*$")

NAMING_GENERIC = ("Name the test with a path or a qualified name "
                  "(`app.FooTest`, `mod::my_test`, `tests/test_x.py::T::t`, `./pkg/...`)")
NAMING_GO = ("Name the test with a trailing slash (`-run TestFoo/`), "
             "or run `go test ./pkg/...` for one package")
NAMING_RUST = "Name the test by its module path (`mod::my_test`)"
NAMING_GRADLE = ("Name the test by its class or method "
                 "(`--tests app.FooTest`, `--tests app.FooTest.bar`)")

INSTALL_EDITABLE = "python3 -m pip install -e ."
INSTALL_REQUIREMENTS = "python3 -m pip install -r requirements.txt"

NODE_TESTS = ["**/__tests__/**", "**/*.test.js", "**/*.test.jsx", "**/*.test.ts", "**/*.test.tsx",
              "**/*.test.mjs", "**/*.test.cjs", "**/*.spec.js", "**/*.spec.jsx", "**/*.spec.ts",
              "**/*.spec.tsx", "test/**", "tests/**"]
PYTHON_TESTS = ["tests/**", "test/**", "**/test_*.py", "**/*_test.py", "**/conftest.py"]

# Every preset names its test globs, one per alternative. make and generic carry none on
# purpose: the draft fails validation until the form gets some, so the studio asks.
# The node, python, gradle, go and rust presets leave the shapes' env_prefix off: the
# cage takes any value in an env assignment, so one token ahead of argv0 would carry
# what the classes above refuse. maven, make and generic keep the shapes of their
# example harness, which take an env prefix.
STACKS = {
    "node": {
        "label": "Node.js (npm)", "markers": ("package.json",), "base": "example-generic",
        "build": "npm ci", "tests": NODE_TESTS,
        "allowed_domains": ["registry.npmjs.org"], "allow_write": ["~/.npm"],
        "gates": [{"name": "install", "command": "npm ci"},
                  {"name": "focused-tests", "command": "npm test -- <test_class>"},
                  {"name": "integration-tests", "command": "npm test"}],
        "shapes": [{"argv0": "npm", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_FREE_ARGS}],
    },
    "python": {
        "label": "Python (pytest)",
        "markers": ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt"),
        "base": "example-generic", "build": INSTALL_EDITABLE, "tests": PYTHON_TESTS,
        "allowed_domains": ["pypi.org", "files.pythonhosted.org"],
        "allow_write": ["~/.cache/pip"],
        "gates": [{"name": "install", "command": INSTALL_EDITABLE},
                  {"name": "focused-tests", "command": "pytest -q <test_class>"},
                  {"name": "integration-tests", "command": "pytest -q"}],
        "shapes": [{"argv0": "pytest", "env_prefix": False, "required_flags": [["-q", ""]],
                    "one_goal_of": [], "free_args": SAFE_FREE_ARGS}],
    },
    "maven": {"label": "Java (Maven)", "markers": ("pom.xml",), "base": "example-maven",
              "tests": list(boundary.TEST_PATTERNS),
              "allowed_domains": ["repo.maven.apache.org", "repo1.maven.org"],
              "allow_write": ["~/.m2"]},
    "gradle": {
        "label": "Java or Kotlin (Gradle)",
        "markers": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
        "base": "example-generic", "build": "./gradlew assemble", "naming": NAMING_GRADLE,
        "tests": list(boundary.TEST_PATTERNS),
        "allowed_domains": ["repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org"],
        "allow_write": ["~/.gradle"],
        "gates": [{"name": "build", "command": "./gradlew assemble"},
                  {"name": "focused-tests", "command": "./gradlew test --tests <test_class>"},
                  {"name": "integration-tests", "command": "./gradlew check"}],
        "shapes": [{"argv0": "./gradlew", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_GRADLE_ARGS}],
    },
    "go": {
        "label": "Go", "markers": ("go.mod",), "base": "example-generic", "build": "go build ./...",
        "naming": NAMING_GO, "tests": ["**/*_test.go", "**/testdata/**"],
        "allowed_domains": ["proxy.golang.org", "sum.golang.org"],
        "allow_write": ["~/go/pkg/mod"],
        "gates": [{"name": "build", "command": "go build ./..."},
                  {"name": "lint", "command": "go vet ./..."},
                  {"name": "focused-tests", "command": "go test ./... -run <test_class>"},
                  {"name": "integration-tests", "command": "go test ./..."}],
        "shapes": [{"argv0": "go", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_FREE_ARGS}],
    },
    "rust": {
        "label": "Rust (cargo)", "markers": ("Cargo.toml",), "base": "example-generic",
        "build": "cargo build", "naming": NAMING_RUST,
        "tests": ["tests/**", "benches/**"],
        "gates": [{"name": "build", "command": "cargo build"},
                  {"name": "lint", "command": "cargo clippy"},
                  {"name": "focused-tests", "command": "cargo test <test_class>"},
                  {"name": "integration-tests", "command": "cargo test"}],
        "shapes": [{"argv0": "cargo", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_FREE_ARGS}],
    },
    "make": {"label": "Make", "markers": ("Makefile", "GNUmakefile"), "base": "example-generic",
             "tests": []},
    "generic": {"label": "Anything else (make targets to adjust)", "markers": (),
                "base": "example-generic", "tests": []},
}
ORDER = ("node", "python", "maven", "gradle", "go", "rust", "make")
FRAGMENT_GLOBS = ("skill/*.md", "templates/*.part")
PACKAGE_JSON_MAX_BYTES = 200_000


def _program(argv0):
    """The program's basename in lower case, less one trailing version
    (`python3.12`, `node18`, `bash-5.2`). The case is folded because a
    case-insensitive filesystem runs `SH` as `sh`."""
    return _VERSION_SUFFIX_RE.sub("", os.path.basename(argv0).lower())


def is_interpreter(argv0):
    """True when the program, folded as _program folds it, is in INTERPRETERS."""
    return _program(argv0) in INTERPRETERS


def colon_runs_a_task(argv0):
    """True for Gradle and Maven and their wrappers, folded as _program folds
    them (`./Gradlew`, `GRADLE`, `mvn3`): they read a token with a `:` as one
    more task or goal to run."""
    return _program(argv0) in COLON_TASK_PROGRAMS


def detect(root):
    root = Path(root)
    found = [name for name in ORDER
             if any((root / marker).is_file() for marker in STACKS[name]["markers"])]
    stack = found[0] if found else "generic"
    return {"stack": stack, "found": found, "label": STACKS[stack]["label"]}


GATE_FRAGMENT_KEYS = ("skill/gates.md", "skill/baseline.md", "templates/ppl-code-worker.part",
                      "templates/ppl-test-worker.part")


def gate_fragments(gates, build=None, stack=None):
    """The skill and worker fragments that describe a gate list: gates.md and
    baseline.md for the orchestrator, the build and focused-test notes for the
    two writing roles. `gates` is a list of {"name", "command"}, `stack` picks
    the example the test writer is told to name a test by."""
    commands = ", ".join("`%s`" % gate["command"] for gate in gates)
    names = ", ".join(gate["name"].replace("-", " ") for gate in gates)
    focused = {gate["name"]: gate["command"] for gate in gates}.get(harness.FOCUSED_TESTS_GATE)
    build = build or (gates[0]["command"] if gates else None)
    fragments = {
        "skill/gates.md": "Every gate writes `$?` to a file and `ppl evidence add` reads it. "
                          "Targets: %s.\n" % commands,
        "skill/baseline.md": "Record before touching anything: %s and known failures.\n" % names,
    }
    if build:
        fragments["templates/ppl-code-worker.part"] = (
            "- Build with `%s`, write the exit code to a file.\n" % build)
    if focused:
        naming = STACKS.get(stack, {}).get("naming", NAMING_GENERIC)
        fragments["templates/ppl-test-worker.part"] = (
            "- Run only `%s` for focused runs. %s: a plain word is refused.\n" % (focused, naming))
    return fragments


def _package_scripts(root):
    """The scripts table of root/package.json, or None. The file is read only
    when it is a regular file inside the repository once symlinks are
    followed, and only up to PACKAGE_JSON_MAX_BYTES: a link to a file elsewhere,
    a named pipe or a device is never opened."""
    real_root = Path(os.path.realpath(root))
    target = Path(os.path.realpath(real_root / "package.json"))
    if real_root not in target.parents:
        return None
    try:
        if not stat.S_ISREG(os.stat(target).st_mode):
            return None
        with open(target, "rb") as handle:
            data = handle.read(PACKAGE_JSON_MAX_BYTES + 1)
        if len(data) > PACKAGE_JSON_MAX_BYTES:
            return None
        return json.loads(data.decode("utf-8")).get("scripts")
    except (OSError, ValueError, AttributeError, RecursionError):
        return None


def _node_gates(root, gates):
    """The lint and build scripts package.json declares become gates."""
    scripts = _package_scripts(root)
    if not isinstance(scripts, dict):
        return gates, {}
    added = [{"name": name, "command": "npm run %s" % name}
             for name in ("lint", "build") if name in scripts]
    sources = {"gate:%s" % gate["name"]: "package.json scripts.%s" % gate["name"] for gate in added}
    return gates[:1] + added + gates[1:], sources


def _python_install(root):
    """Install from requirements.txt only when no project file names the package."""
    names_package = any((root / marker).is_file()
                        for marker in ("pyproject.toml", "setup.cfg", "setup.py"))
    if (root / "requirements.txt").is_file() and not names_package:
        return INSTALL_REQUIREMENTS
    return INSTALL_EDITABLE


def _tracker_offers(doc, remote):
    """{kind: {"tracker", "task"}} for the form's tracker select: the base harness's own
    Jira tables and a github-issues table naming the remote's repo, or none when the
    remote is not a GitHub one, which the validator then asks for."""
    github = trackers.get("github-issues")
    table = {"kind": "github-issues"}
    repo = github.repo_from_remote(remote)
    if repo:
        table["repo"] = repo
    table.update(github.DEFAULTS)
    return {"jira": {"tracker": copy.deepcopy(doc["tracker"]), "task": copy.deepcopy(doc["task"])},
            "github-issues": {"tracker": table, "task": github.task_defaults(table)}}


def draft(stack, project):
    """A draft harness for `project` from the stack's preset."""
    if stack not in STACKS:
        raise PplError("SCHEMA_INVALID", "unknown stack %r (stacks: %s)"
                       % (stack, ", ".join(STACKS)))
    spec = STACKS[stack]
    base = paths.defaults_dir() / "harnesses" / spec["base"]
    with open(base / "harness.toml", "rb") as handle:
        doc = tomllib.load(handle)
    fragments = {}
    for pattern in FRAGMENT_GLOBS:
        for path in sorted(base.glob(pattern)):
            fragments[path.relative_to(base).as_posix()] = path.read_text(encoding="utf-8")
    label = "preset %s" % stack
    sources = {"repo.default_branch": "git", "task.key_pattern": label}
    repo_path = project["path"]
    doc["harness"]["name"] = project["name_hint"]
    doc["repo"] = {"path": repo_path, "default_branch": project["default_branch"],
                   "worktrees": repo_path.rstrip("/") + "/.worktrees"}
    doc["env"] = {}
    doc["tests"] = {"sources": list(spec["tests"])}
    if spec.get("allowed_domains") or spec.get("allow_write"):
        doc["os_sandbox"] = {"allowed_domains": list(spec.get("allowed_domains", [])),
                             "allow_write": list(spec.get("allow_write", []))}
    sources["tests.sources"] = label
    kind = project.get("forge_hint")
    if kind in harness.FORGES:
        sources["forge.kind"] = "git remote"
    else:
        kind = "github"
        sources["forge.kind"] = label
    doc["forge"] = {"kind": kind}
    if kind == "gitlab":
        doc["forge"]["mcp_server"] = "GitLab-MCP"
    offers = _tracker_offers(doc, project.get("remote_url"))
    if offers["github-issues"]["tracker"].get("repo"):
        offer = offers["github-issues"]
        doc["tracker"] = copy.deepcopy(offer["tracker"])
        doc["task"] = copy.deepcopy(offer["task"])
        sources["tracker.kind"] = "git remote"
    if "gates" in spec:
        gates = [dict(gate) for gate in spec["gates"]]
        build = spec["build"]
        if stack == "node":
            gates, extra = _node_gates(Path(repo_path), gates)
            sources.update(extra)
            if "gate:build" in extra:
                build = "npm run build"
        elif stack == "python":
            build = _python_install(Path(repo_path))
            gates[0]["command"] = build
        doc["gate"] = gates
        doc["guard"] = {"shape": copy.deepcopy(spec["shapes"])}
        fragments.update(gate_fragments(gates, build, stack))
    for gate in doc.get("gate") or []:
        sources.setdefault("gate:%s" % gate["name"], label)
    for index, _ in enumerate((doc.get("guard") or {}).get("shape") or []):
        sources["shape:%d" % index] = label
    return {"stack": stack, "doc": doc, "fragments": fragments, "sources": sources,
            "trackers": offers}

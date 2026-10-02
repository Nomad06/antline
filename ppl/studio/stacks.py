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
import tomllib
from pathlib import Path

from .. import harness, paths
from ..errors import PplError

# The tokens a test-worker command may carry besides its goal. The cage matches
# them anywhere in the line, so a plain word (a subcommand such as `run`, `doc`
# or `publish`) is refused: only a bare dash flag with no attached value, `--`,
# or a relative path or qualified name carrying a `.`, `/` or `:` passes.
# Never an absolute path, a `..` segment, an `@` argument file, a space or a
# shell metacharacter.
SAFE_FREE_ARGS = (r"^(?:--?[A-Za-z][A-Za-z0-9-]*|--"
                  r"|(?!(?:.*[/:])?\.\.(?:[/:]|$))[A-Za-z0-9_+,.][A-Za-z0-9_+,-]*[./:][A-Za-z0-9_./:+,-]*)$")
# Gradle reads `sub:task` as another task to run, so its class has no `:` at all.
SAFE_GRADLE_ARGS = (r"^(?:--?[A-Za-z][A-Za-z0-9-]*|--"
                    r"|(?!(?:.*/)?\.\.(?:/|$))[A-Za-z0-9_+,.][A-Za-z0-9_+,-]*[./][A-Za-z0-9_./+,-]*)$")

NAMING_GENERIC = ("Name the test with a path or a qualified name "
                  "(`app.FooTest`, `mod::my_test`, `tests/test_x.py::T::t`, `./pkg/...`)")
NAMING_GO = ("Name the test with a trailing slash (`-run TestFoo/`), "
             "or run `go test ./pkg/...` for one package")
NAMING_RUST = "Name the test by its module path (`mod::my_test`)"
NAMING_GRADLE = ("Name the test by its class or method "
                 "(`--tests app.FooTest`, `--tests app.FooTest.bar`)")

INSTALL_EDITABLE = "python3 -m pip install -e ."
INSTALL_REQUIREMENTS = "python3 -m pip install -r requirements.txt"

# The presets leave the shapes' env_prefix off: the cage takes any value in an
# env assignment, so one token ahead of argv0 would carry what the classes above refuse.
STACKS = {
    "node": {
        "label": "Node.js (npm)", "markers": ("package.json",), "base": "example-generic",
        "build": "npm ci",
        "gates": [{"name": "install", "command": "npm ci"},
                  {"name": "focused-tests", "command": "npm test -- <test_class>"},
                  {"name": "integration-tests", "command": "npm test"}],
        "shapes": [{"argv0": "npm", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_FREE_ARGS}],
    },
    "python": {
        "label": "Python (pytest)",
        "markers": ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt"),
        "base": "example-generic", "build": INSTALL_EDITABLE,
        "gates": [{"name": "install", "command": INSTALL_EDITABLE},
                  {"name": "focused-tests", "command": "pytest -q <test_class>"},
                  {"name": "integration-tests", "command": "pytest -q"}],
        "shapes": [{"argv0": "pytest", "env_prefix": False, "required_flags": [["-q", ""]],
                    "one_goal_of": [], "free_args": SAFE_FREE_ARGS}],
    },
    "maven": {"label": "Java (Maven)", "markers": ("pom.xml",), "base": "example-maven"},
    "gradle": {
        "label": "Java or Kotlin (Gradle)",
        "markers": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
        "base": "example-generic", "build": "./gradlew assemble", "naming": NAMING_GRADLE,
        "gates": [{"name": "build", "command": "./gradlew assemble"},
                  {"name": "focused-tests", "command": "./gradlew test --tests <test_class>"},
                  {"name": "integration-tests", "command": "./gradlew check"}],
        "shapes": [{"argv0": "./gradlew", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_GRADLE_ARGS}],
    },
    "go": {
        "label": "Go", "markers": ("go.mod",), "base": "example-generic", "build": "go build ./...",
        "naming": NAMING_GO,
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
        "gates": [{"name": "build", "command": "cargo build"},
                  {"name": "lint", "command": "cargo clippy"},
                  {"name": "focused-tests", "command": "cargo test <test_class>"},
                  {"name": "integration-tests", "command": "cargo test"}],
        "shapes": [{"argv0": "cargo", "env_prefix": False, "required_flags": [],
                    "one_goal_of": ["test"], "free_args": SAFE_FREE_ARGS}],
    },
    "make": {"label": "Make", "markers": ("Makefile", "GNUmakefile"), "base": "example-generic"},
    "generic": {"label": "Anything else (make targets to adjust)", "markers": (),
                "base": "example-generic"},
}
ORDER = ("node", "python", "maven", "gradle", "go", "rust", "make")
FRAGMENT_GLOBS = ("skill/*.md", "templates/*.part")


def detect(root):
    root = Path(root)
    found = [name for name in ORDER
             if any((root / marker).is_file() for marker in STACKS[name]["markers"])]
    stack = found[0] if found else "generic"
    return {"stack": stack, "found": found, "label": STACKS[stack]["label"]}


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


def _node_gates(root, gates):
    """The lint and build scripts package.json declares become gates."""
    try:
        scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts")
    except (OSError, ValueError, AttributeError):
        return gates, {}
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
    kind = project.get("forge_hint")
    if kind in harness.FORGES:
        sources["forge.kind"] = "git remote"
    else:
        kind = "github"
        sources["forge.kind"] = label
    doc["forge"] = {"kind": kind}
    if kind == "gitlab":
        doc["forge"]["mcp_server"] = "GitLab-MCP"
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
    return {"stack": stack, "doc": doc, "fragments": fragments, "sources": sources}

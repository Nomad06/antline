"""Stack presets, for a project with no CLAUDE.md or AGENTS.md to draft from
or when the extraction fails: the build files in the repo root name the
stack, and the stack names its gates and the command shapes the test worker
may run. Every preset is built on one of the two example harnesses, so the
tracker lists and the skill fragments come from one place."""
import copy
import json
import tomllib
from pathlib import Path

from .. import harness, paths
from ..errors import PplError

# The tokens a test-worker command may carry besides its goal: words, paths,
# test names, key=value. No shell metacharacter, no space, never empty.
SAFE_FREE_ARGS = r"^[A-Za-z0-9_./:@=,+-]+$"

STACKS = {
    "node": {
        "label": "Node.js (npm)", "markers": ("package.json",), "base": "example-generic",
        "build": "npm ci",
        "gates": [{"name": "install", "command": "npm ci"},
                  {"name": "focused-tests", "command": "npm test -- <test_class>"},
                  {"name": "integration-tests", "command": "npm test"}],
        "shapes": [{"argv0": "npm", "env_prefix": True, "required_flags": [],
                    "one_goal_of": ["ci", "test"], "free_args": SAFE_FREE_ARGS}],
    },
    "python": {
        "label": "Python (pytest)",
        "markers": ("pyproject.toml", "setup.cfg", "setup.py", "requirements.txt"),
        "base": "example-generic", "build": "python3 -m pip install -e .",
        "gates": [{"name": "install", "command": "python3 -m pip install -e ."},
                  {"name": "focused-tests", "command": "python3 -m pytest -q <test_class>"},
                  {"name": "integration-tests", "command": "python3 -m pytest -q"}],
        "shapes": [{"argv0": "python3", "env_prefix": True, "required_flags": [["-m", "^pytest$"]],
                    "one_goal_of": [], "free_args": SAFE_FREE_ARGS}],
    },
    "maven": {"label": "Java (Maven)", "markers": ("pom.xml",), "base": "example-maven"},
    "gradle": {
        "label": "Java or Kotlin (Gradle)",
        "markers": ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"),
        "base": "example-generic", "build": "./gradlew assemble",
        "gates": [{"name": "build", "command": "./gradlew assemble"},
                  {"name": "focused-tests", "command": "./gradlew test --tests <test_class>"},
                  {"name": "integration-tests", "command": "./gradlew check"}],
        "shapes": [{"argv0": "./gradlew", "env_prefix": True, "required_flags": [],
                    "one_goal_of": ["assemble", "test", "check"], "free_args": SAFE_FREE_ARGS}],
    },
    "go": {
        "label": "Go", "markers": ("go.mod",), "base": "example-generic", "build": "go build ./...",
        "gates": [{"name": "build", "command": "go build ./..."},
                  {"name": "lint", "command": "go vet ./..."},
                  {"name": "focused-tests", "command": "go test ./... -run <test_class>"},
                  {"name": "integration-tests", "command": "go test ./..."}],
        "shapes": [{"argv0": "go", "env_prefix": True, "required_flags": [],
                    "one_goal_of": ["build", "test", "vet"], "free_args": SAFE_FREE_ARGS}],
    },
    "rust": {
        "label": "Rust (cargo)", "markers": ("Cargo.toml",), "base": "example-generic",
        "build": "cargo build",
        "gates": [{"name": "build", "command": "cargo build"},
                  {"name": "lint", "command": "cargo clippy"},
                  {"name": "focused-tests", "command": "cargo test <test_class>"},
                  {"name": "integration-tests", "command": "cargo test"}],
        "shapes": [{"argv0": "cargo", "env_prefix": True, "required_flags": [],
                    "one_goal_of": ["build", "test", "clippy", "check"], "free_args": SAFE_FREE_ARGS}],
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


def gate_fragments(gates, build=None):
    """The skill and worker fragments that describe a gate list: gates.md and
    baseline.md for the orchestrator, the build and focused-test notes for the
    two writing roles. `gates` is a list of {"name", "command"}."""
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
        fragments["templates/ppl-test-worker.part"] = "- Run only `%s` for focused runs.\n" % focused
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
        doc["forge"] = {"kind": kind}
        if kind == "gitlab":
            doc["forge"]["mcp_server"] = "GitLab-MCP"
        sources["forge.kind"] = "git remote"
    else:
        sources["forge.kind"] = label
    if "gates" in spec:
        gates = [dict(gate) for gate in spec["gates"]]
        if stack == "node":
            gates, extra = _node_gates(Path(repo_path), gates)
            sources.update(extra)
        doc["gate"] = gates
        doc["guard"] = {"shape": copy.deepcopy(spec["shapes"])}
        fragments.update(gate_fragments(gates, spec["build"]))
    for gate in doc.get("gate") or []:
        sources.setdefault("gate:%s" % gate["name"], label)
    for index, _ in enumerate((doc.get("guard") or {}).get("shape") or []):
        sources["shape:%d" % index] = label
    return {"stack": stack, "doc": doc, "fragments": fragments, "sources": sources}

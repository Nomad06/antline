"""What one lane's Claude Code session loads, written into its start directory: settings.json
(the hooks, each carrying `--task <id>`, the OS sandbox profile and its permissions.allow), the
agents as JSON text for `--agents` (also kept as agents.json), the pipeline skill as a plugin
named `antline`, an MCP config holding only the servers the harness names, a frozen copy of
routing.toml and the sandbox facts. `render` works on a harness already exported into the start
directory and loaded by its caller; it writes nothing outside that directory.

The agents carry every key `routing.render` emits as an `--agents` key: `description`, `prompt`
(the body), `tools`, `model`, `effort`, `maxTurns` and `permissionMode`. A probe on 2026-11-05
verified that Claude Code accepts all seven; whether `effort` and `maxTurns` take effect is not
verified, so a role may run with the default effort and turns if they are ignored.

CLI-side only: no hook imports this module."""
import json
import os
import re
import shutil
from pathlib import Path

from . import doctor, harness, os_sandbox, paths, routing, setup, store, tomlout
from .errors import PplError

MAX_AGENTS_BYTES = 122_880
_FRONTMATTER_KEY = re.compile(r"^([A-Za-z]+): ?(.*)$")
_PREFIX = re.compile(r"mcp__([A-Za-z0-9_-]+)__\Z")
_CONNECTOR = "claude_ai_"


def mcp_source(project_dir=None):
    """{server name: config} the operator has registered, from the files doctor.mcp_server_names
    reads. `project_dir` is where `claude mcp add --scope local|project` entries are keyed (the
    harness's checkout); the workspace when None."""
    return doctor.mcp_servers(cwd=project_dir)


def _parse_agent(text):
    """The frontmatter keys and the body of an agent file routing.render made."""
    head, _, body = text.partition("\n---\n")
    values = {}
    for line in head.splitlines()[1:]:
        match = _FRONTMATTER_KEY.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values, body.strip("\n") + "\n"


def _agent(text):
    values, body = _parse_agent(text)
    tools = values["tools"].strip("[]")
    agent = {"description": values["description"], "prompt": body,
             "tools": [tool.strip() for tool in tools.split(",") if tool.strip()],
             "model": values["model"]}
    if "effort" in values:
        agent["effort"] = values["effort"]
    agent["maxTurns"] = int(values["maxTurns"])
    agent["permissionMode"] = values["permissionMode"]
    return agent


def _agents_json(doc, routing_config):
    agents = {name: _agent(routing.render(name, doc, routing_config))
              for name in routing.REQUIRED_ROLES}
    text = json.dumps(agents, sort_keys=True)
    size = len(text.encode("utf-8"))
    if size > MAX_AGENTS_BYTES:
        sizes = {name: len(json.dumps(agent).encode("utf-8")) for name, agent in agents.items()}
        largest = max(sizes, key=sizes.get)
        raise PplError("SCHEMA_INVALID", "the agents JSON is %d bytes, over the %d the launch "
                       "line may carry; the largest role is %s at %d bytes"
                       % (size, MAX_AGENTS_BYTES, largest, sizes[largest]))
    return text


def _named_servers(doc):
    """(required, alternatives): the forge's mcp_server must be registered; a tracker's
    mcp_prefixes are alternative names of one server (a claude.ai connector among them is
    never one the operator config holds), so one of them must be."""
    forge = doc.get("forge") or {}
    required = [forge["mcp_server"]] if forge.get("mcp_server") else []
    tracker = doc.get("tracker") or {}
    alternatives = []
    for prefix in tracker.get("mcp_prefixes") or []:
        match = _PREFIX.match(prefix)
        if match:
            alternatives.append(match.group(1))
    return required, alternatives


def _mcp_config(doc, source):
    required, alternatives = _named_servers(doc)
    chosen = {}
    for name in required:
        if name not in source:
            raise _missing_server(name)
        chosen[name] = source[name]
    if alternatives:
        present = [name for name in alternatives if name in source]
        if not present:
            raise _missing_server(" or ".join(alternatives))
        chosen.update((name, source[name]) for name in present)
    return {"mcpServers": chosen}


def _missing_server(name):
    return PplError("SETUP_INCOMPLETE", "the harness names the MCP server %s and the operator "
                    "config holds none by that name. Add it with `claude mcp add --scope user`; "
                    "a server reachable only as a claude.ai connector cannot be copied into a "
                    "lane." % name)


def _write_private(path, text):
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(str(path), 0o600)


def _settings(doc, task_id, start):
    """(settings, facts, pins): the settings document and what the sandbox facts are made from,
    None for both when the harness needs no profile."""
    hooks = {event: [entry] for event, entry in setup.hook_entries(paths.root(), task_id).items()}
    settings = {"hooks": hooks}
    if not os_sandbox.needed(doc):
        return settings, None, None
    pin_map = os_sandbox.pins()
    problem = os_sandbox.layout_problem(doc, start) or os_sandbox.repo_problem(doc)
    if problem:
        raise PplError("SETUP_INCOMPLETE", problem)
    prof = os_sandbox.profile(doc, start, pin_map)
    settings["sandbox"] = prof["sandbox"]
    settings["permissions"] = {"allow": prof["allow"]}
    facts = os_sandbox.facts(doc, start, pin_map)
    facts["written_sandbox"] = prof["sandbox"]
    facts["written_allow"] = prof["allow"]
    return settings, facts, pin_map


def render(doc, task_id, lane, start, worktree, routing_config, mcp_source):
    """Write the lane's session files under `start` and return their paths:
    {"settings", "agents_json" (the text), "plugin", "mcp", "routing", "facts"}; facts is None
    when the harness needs no OS sandbox, which is when `harness apply` writes none either.
    Everything is built before anything is written, so a refusal leaves the directory as it was."""
    start = Path(start)
    paths.check_task_id(task_id)
    agents_text = _agents_json(doc, routing_config)
    mcp = _mcp_config(doc, mcp_source)
    settings, facts, pin_map = _settings(doc, task_id, start)
    skill = harness.render_skill(doc, pins=pin_map, workspace=str(start), lane=lane)
    start.mkdir(parents=True, exist_ok=True)

    settings_path = start / "settings.json"
    store.write_json(settings_path, settings)
    (start / "agents.json").write_text(agents_text, encoding="utf-8")
    plugin = start / "plugin"
    manifest = plugin / ".claude-plugin" / "plugin.json"
    store.write_json(manifest, {"name": "antline", "version": "1.0.0",
                                "description": "The antline pipeline skill for one lane."})
    for relative, text in skill.items():
        target = plugin / "skills" / "pipeline" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    mcp_path = start / "mcp.json"
    _write_private(mcp_path, json.dumps(mcp, indent=2, sort_keys=True) + "\n")
    routing_path = start / "routing.toml"
    machine = paths.home() / "routing.toml"
    if machine.is_file():
        shutil.copyfile(machine, routing_path)
    else:
        routing_path.write_text(tomlout.dumps(routing_config), encoding="utf-8")
    facts_path = start / os_sandbox.FACTS_FILE
    if facts is not None:
        facts.update({"lane": lane, "task": task_id, "worktree": str(worktree)})
        store.write_json(facts_path, facts)
    else:
        facts_path.unlink(missing_ok=True)
    return {"settings": settings_path, "agents_json": agents_text, "plugin": plugin,
            "mcp": mcp_path, "routing": routing_path,
            "facts": facts_path if facts is not None else None}

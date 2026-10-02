"""The blocking rules of rules.toml that hold for one role: what the cage
enforces whatever the role's prompt says. A rule holds for a role when its
scope names the role's agent_type, covers every role, or covers every
writing or every read-only role and the role is one."""
import tomllib

from .. import paths, routing
from ..errors import PplError

EVERY_ROLE = frozenset({
    "global", "global, no run required", "global, every role", "global, widened by the active run",
    "global, every dispatch of that role", "active run", "active run, any ppl-* role",
    "active or no run, a ppl role in boundary.ROLE_RULES",
    "active run whose state.json is missing, corrupt or malformed",
    "active run, scoped by role, target path, cwd, or the isolated broadening",
    "every PreToolUse call carrying a role", "every hook call, under an interpreter older than 3.11",
})
WRITING = ("ppl-code-worker", "ppl-test-worker")
WRITING_SCOPES = frozenset({"active run, any writing role",
                            "active run, any writing role or a session with no agent_type"})
READ_ONLY_SCOPES = frozenset({"active run, read-only roles"})
# Scopes that bind the ppl CLI, the panel, the docs or the operator's own
# session, not a caged role.
NOT_A_ROLE = frozenset({
    "ppl CLI", "ppl CLI and panel", "ppl CLI and panel stop", "ppl CLI, panel, hook",
    "ppl CLI, the ten call sites in cli.py", "ppl CLI, this directory plus the Claude home",
    "panel", "guard hook", "hook registration", "active run, session with no agent_type",
    "active run, scoped as in 5.4", "docs/DESIGN.md, README.md, the rendered SKILL.md and this table",
    "this table and hooks/ppl_guard.py",
})


def applies(scope, role):
    if scope in EVERY_ROLE or scope == "active run, agent_type %s" % role:
        return True
    if role in WRITING:
        return scope in WRITING_SCOPES
    return scope in READ_ONLY_SCOPES


def for_role(role):
    if role not in routing.REQUIRED_ROLES:
        raise PplError("SCHEMA_INVALID", "unknown role %r (roles: %s)"
                       % (role, ", ".join(routing.REQUIRED_ROLES)))
    with open(paths.root() / "rules.toml", "rb") as handle:
        rows = tomllib.load(handle)["rule"]
    return [{"id": row["id"], "statement": row["statement"], "event": row["event"],
             "residual": row["residual"]}
            for row in rows if row["enforcement"] == "blocking" and applies(row["scope"], role)]

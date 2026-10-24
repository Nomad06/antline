"""The blocking rules of rules.toml that hold for one role: what the cage
enforces whatever the role's prompt says. A rule holds for a role when its
scope names the role's agent_type, covers every role, or covers every
writing or every read-only role and the role is one."""
from .. import boundary, routing, rules
from ..errors import PplError

EVERY_ROLE = frozenset({
    "global", "global, no run required", "global, every role", "global, widened by the active run",
    "active run", "active run, any ppl-* role", "guard hook", "active run, scoped as in 5.4",
    "active or no run, a ppl role in boundary.ROLE_RULES",
    "active run whose state.json is missing, corrupt or malformed",
    "active run, scoped by role, target path, cwd, or the isolated broadening",
    "every PreToolUse call carrying a role", "every hook call, under an interpreter older than 3.11",
})
WRITING = tuple(role for role, rule in boundary.ROLE_RULES.items() if rule["may_write"])
WRITING_SCOPES = frozenset({"active run, any writing role",
                            "active run, any writing role or a session with no agent_type"})
READ_ONLY_SCOPES = frozenset({"active run, read-only roles", "global, every dispatch of that role"})
# Scopes that bind the ppl CLI, the panel, the docs or the operator's own
# session, not a caged role.
NOT_A_ROLE = frozenset({
    "ppl CLI", "ppl CLI and panel", "ppl CLI and panel stop", "ppl CLI, panel, hook",
    "ppl CLI, the eleven call sites in cli.py", "ppl CLI, this directory plus the Claude home",
    "panel", "hook registration", "active run, session with no agent_type",
    "docs/DESIGN.md, README.md, every rendered file of both example skills and this table",
    "this table and ppl/_assets/hooks/ppl_guard.py",
    "the unroled session, while PPL_HOME/os_sandbox.json exists or a run is active",
})


def applies(scope, role):
    # A rule on the dispatch of a role is enforced on the dispatching session,
    # and what it protects is that role's isolation, so it belongs on its card.
    if scope in EVERY_ROLE or scope in ("active run, agent_type %s" % role,
                                        "active run, dispatch of %s" % role):
        return True
    if role in WRITING:
        return scope in WRITING_SCOPES
    return scope in READ_ONLY_SCOPES


def check_role(role):
    if role not in routing.REQUIRED_ROLES:
        raise PplError("SCHEMA_INVALID", "unknown role %r (roles: %s)"
                       % (role, ", ".join(routing.REQUIRED_ROLES)))


def for_role(role):
    check_role(role)
    return [{"id": row["id"], "statement": row["statement"], "event": row["event"],
             "residual": row["residual"]}
            for row in rules.load()
            if row["enforcement"] == "blocking" and applies(row["scope"], role)]

"""Prove each routed model answers before a run may start.

A tier names a model alias. `verify` runs the cheapest possible Claude Code
probe per distinct model, keeps the resolved id the CLI reports, and `ppl
init` refuses while any model a run can route to is unverified or stale
[R-INIT-MODELS-VERIFIED]. The probe strips the Claude Code session markers
from its environment so it works from inside a Claude Code session.
"""
import json
import os
import shlex
import subprocess
from datetime import datetime, timedelta, timezone

from . import paths, routing, store
from .errors import PplError

DEFAULT_TTL_DAYS = 30
PROBE_SYSTEM_PROMPT = "Answer with the single word OK."
PROBE_TIMEOUT_SECONDS = 90
VERSION_TIMEOUT_SECONDS = 20
SESSION_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def ttl_days():
    """[models].verify_ttl_days from config.toml when it exists, else the default."""
    try:
        from . import config
    except ImportError:
        return DEFAULT_TTL_DAYS
    try:
        return int(config.load().get("models", {}).get("verify_ttl_days", DEFAULT_TTL_DAYS))
    except (PplError, ValueError, TypeError):
        return DEFAULT_TTL_DAYS


def probe_command(model, binary):
    return [binary, "-p", "--model", model, "--max-turns", "1",
            "--output-format", "json", "--tools", "", "--strict-mcp-config",
            "--setting-sources", "", "--no-session-persistence",
            "--system-prompt", PROBE_SYSTEM_PROMPT, "ping"]


def _failed(detail):
    return {"ok": False, "resolved_id": None, "cost_usd": 0.0, "detail": detail}


def parse_probe(returncode, stdout, stderr):
    try:
        doc = json.loads(stdout or "{}")
    except ValueError:
        return _failed("probe output is not JSON: %s" % (stdout or "")[:200])
    if not isinstance(doc, dict):
        return _failed("probe output is not an object")
    usage = doc.get("modelUsage") or {}
    resolved = next(iter(usage), None) if isinstance(usage, dict) else None
    try:
        cost = float(doc.get("total_cost_usd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if returncode != 0 or not resolved:
        detail = (stderr or "").strip() or str(doc.get("result") or "probe failed")
        return {"ok": False, "resolved_id": resolved, "cost_usd": cost, "detail": detail[:400]}
    return {"ok": True, "resolved_id": resolved, "cost_usd": cost, "detail": ""}


def probe(model, binary="claude", env=None):
    environment = dict(env if env is not None else os.environ)
    for marker in SESSION_MARKERS:
        environment.pop(marker, None)
    try:
        result = subprocess.run(probe_command(model, binary), capture_output=True, text=True,
                                env=environment, timeout=PROBE_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return _failed("%s not found on PATH" % binary)
    except subprocess.TimeoutExpired:
        return _failed("probe timed out after %ss" % PROBE_TIMEOUT_SECONDS)
    return parse_probe(result.returncode, result.stdout, result.stderr)


def claude_version(binary="claude"):
    """First token of `<binary> --version`, or "unknown" when it cannot be read.
    A plain function on purpose: tests replace it, so no caching here."""
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                timeout=VERSION_TIMEOUT_SECONDS)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    tokens = (result.stdout.strip() or result.stderr.strip()).split()
    return tokens[0] if tokens else "unknown"


def _load():
    stored = store.read_json(paths.models_file(), {})
    if not isinstance(stored, dict):
        raise PplError("SCHEMA_INVALID", "%s must be a JSON object" % paths.models_file())
    return stored


def _entry_state(entry, version):
    if not isinstance(entry, dict):
        return "unverified"
    try:
        seen = datetime.strptime(entry["verified_at"], _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return "unverified"
    if datetime.now(timezone.utc) - seen > timedelta(days=ttl_days()):
        return "stale"
    if entry.get("claude_version") != version:
        return "stale"
    return "verified"


def verify(models_to_check=None, force=False, dry_run=False, runner=None, binary="claude"):
    runner = runner or probe
    targets = sorted(set(models_to_check or routing.models_in_use()))
    stored = _load()
    version = claude_version(binary)
    report = {"verified": [], "skipped": [], "failed": [], "commands": [], "cost_usd": 0.0}
    for model in targets:
        if dry_run:
            report["commands"].append(shlex.join(probe_command(model, binary)))
            continue
        if not force and _entry_state(stored.get(model), version) == "verified":
            report["skipped"].append(model)
            continue
        result = runner(model, binary=binary)
        if not result.get("ok") or not result.get("resolved_id"):
            report["failed"].append(model)
            report.setdefault("details", {})[model] = result.get("detail", "")
            continue
        stored[model] = {"resolved_id": result["resolved_id"],
                         "verified_at": datetime.now(timezone.utc).strftime(_TIME_FORMAT),
                         "cost_usd": result.get("cost_usd", 0.0),
                         "claude_version": version}
        report["verified"].append(model)
        report["cost_usd"] += float(result.get("cost_usd") or 0.0)
    if not dry_run:
        store.write_json(paths.models_file(), stored)
    return report


def status(risk=None, binary="claude", all_tiers=False):
    """One row per routed model, or with `all_tiers` per tier whether a role
    routes to it or not, then any model a role names directly."""
    stored = _load()
    version = claude_version(binary)
    in_use = routing.models_in_use(risk)
    rows = []
    seen = set()

    def row(tier, model):
        entry = stored.get(model)
        return {"tier": tier, "model": model,
                "resolved_id": entry.get("resolved_id") if isinstance(entry, dict) else None,
                "state": _entry_state(entry, version)}

    for tier, entry in sorted(routing.tiers().items()):
        model = entry["model"]
        if all_tiers or model in in_use:
            seen.add(model)
            rows.append(row(tier, model))
    for model in sorted(in_use - seen):
        rows.append(row(None, model))
    return rows


def require_verified(risk, binary="claude"):
    bad = [row for row in status(risk, binary) if row["state"] != "verified"]
    if bad:
        listing = ", ".join("%s (%s, %s)" % (row["model"], row["tier"] or "direct", row["state"])
                            for row in bad)
        raise PplError("MODELS_UNVERIFIED",
                       "models not verified for this run: %s. Run `ppl models verify`, or pass "
                       "--skip-model-check to record that you skipped it." % listing)

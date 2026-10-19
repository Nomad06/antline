import re
from pathlib import Path

from . import paths, schema, store
from .errors import PplError, role_output

RESULTS = ("pass", "fail", "skip")


def _validate_check(check):
    """Validate check name: referenceable from a verdict, no traversal.

    The charset is not this module's to choose. A verdict refers to a receipt as
    "evidence:<check>" and schema.ACCEPTANCE_EVIDENCE_RE decides what that may
    contain, so a check name accepted here but rejected there would file a
    receipt that no verdict can ever cite - a failure that only surfaces at the
    last gate. The regex is reused rather than restated for that reason.
    """
    if not check:
        raise PplError("SCHEMA_INVALID", "check name must not be empty")
    if "/" in check or ".." in check or check.startswith("."):
        raise PplError("SCHEMA_INVALID",
                       "check name %r is invalid: no `/`, `..`, or leading `.`" % check)
    if not schema.ACCEPTANCE_EVIDENCE_RE.match("evidence:%s" % check):
        raise PplError("SCHEMA_INVALID",
                       "check name %r is invalid: a verdict cites a receipt as "
                       "'evidence:<check>', so a check name may hold only letters, "
                       "digits, '.', '_' and '-'" % check)


def _validate_sha(sha):
    """Validate SHA: hex-only, no traversal."""
    if not sha:
        raise PplError("SCHEMA_INVALID", "candidate SHA must not be empty")
    if not re.match(r"^[0-9a-fA-F]+$", sha):
        raise PplError("SCHEMA_INVALID",
                       "candidate SHA %r is invalid: must be hexadecimal" % sha)
    if "/" in sha or ".." in sha or sha.startswith("."):
        raise PplError("SCHEMA_INVALID",
                       "candidate SHA %r is invalid: no `/`, `..`, or leading `.`" % sha)


def _dir(task_id, sha):
    return paths.run_dir(task_id) / "evidence" / sha


def read_rc(rc_file):
    try:
        text = Path(rc_file).read_text(encoding="utf-8").strip()
    except OSError:
        raise PplError("SCHEMA_INVALID",
                       "no exit-code file at %s. Gates must write $? to a file, because a "
                       "piped command returns the exit code of the pipe tail." % rc_file)
    try:
        return int(text)
    except ValueError:
        raise PplError("SCHEMA_INVALID", "exit-code file %s does not contain an integer" % rc_file)


def add(task_id, check, result, sha, log_path, rc_file, producer):
    with role_output():
        _validate_check(check)
        _validate_sha(sha)
        if result not in RESULTS:
            raise PplError("SCHEMA_INVALID", "result must be one of %s" % ", ".join(RESULTS))
    # The exit-code file is how the command was called, so it is read outside the block.
    code = read_rc(rc_file)
    with role_output():
        if result == "pass" and code != 0:
            raise PplError("SCHEMA_INVALID",
                           "receipt for %s claims pass but rc=%d" % (check, code))
        if result == "fail" and code == 0:
            raise PplError("SCHEMA_INVALID",
                           "receipt for %s claims fail but rc=0" % check)
    receipt = {
        "task_id": task_id, "check": check, "result": result,
        "candidate_sha": sha, "rc": code, "log": str(log_path),
        "producer": producer, "at": store.now(),
    }
    target = _dir(task_id, sha) / ("%s.json" % check)
    store.write_json(target, receipt)
    store.append_log(task_id, {"event": "evidence", "check": check,
                               "result": result, "sha": sha, "rc": code})
    return receipt


def for_sha(task_id, sha):
    if not sha:
        return {}
    _validate_sha(sha)
    folder = _dir(task_id, sha)
    if not folder.is_dir():
        return {}
    out = {}
    for path in sorted(folder.glob("*.json")):
        doc = store.read_json(path)
        # A receipt whose content decoded to something other than an object -
        # `null` is the shape that actually reaches this from a truncated
        # write, but any non-dict is treated the same way - is dropped rather
        # than kept as a value done_blockers' `.get("result")` cannot call.
        # Missing/dropped already means "no passing receipt" downstream, so
        # this fails the done gate closed instead of crashing it
        # (R-EVIDENCE-SHA-PARTITION, W5(c), cycle 2 wave B) - same defensive
        # shape ppl/findings.py:_load_all already uses.
        if isinstance(doc, dict):
            out[path.stem] = doc
    return out

"""The offline demo: a throwaway sandbox and the scripted walk played in it."""
import shutil
import time

from ..errors import PplError
from . import sandbox, script

HEADLINE = ("antline demo: one issue through the pipeline on a sample project, offline, "
            "with stand-ins for the agents.")
REASON_LIMIT = 100


def _matches(expect, answer):
    if isinstance(expect, str):
        return expect == answer
    return (isinstance(answer, tuple) and answer[:2] == expect[:2]
            and expect[2] in answer[2])


def _excerpt(reason, phrase):
    """The first REASON_LIMIT characters, moved to show the expected phrase when it would be cut."""
    reason = " ".join(reason.split())
    at = reason.find(phrase) if phrase else -1
    if at + len(phrase or "") <= REASON_LIMIT:
        return reason[:REASON_LIMIT]
    return "..." + reason[at - 20:at - 20 + REASON_LIMIT - 3]


def _words(answer, box, expect=None):
    if isinstance(answer, str):
        return "allowed" if answer == "allow" else answer
    kind, code, reason = answer
    for root in {str(box.root), str(box.root.resolve())}:
        reason = reason.replace(root, "<sandbox>")
    reason = _excerpt(reason, expect[2] if isinstance(expect, tuple) else "")
    return "%s %s: %s" % ("denied" if kind == "deny" else "refused", code, reason)


def _expected(expect):
    if isinstance(expect, str):
        return expect
    return "%s %s" % (expect[0], expect[1])


def _walk(box, emit):
    walk = script.Walk(box)
    for number, ((actor, tried, expect, rule), action) in enumerate(
            zip(script.STEPS, script.ACTIONS), 1):
        try:
            answer = action(walk)
        except script.Refused as refusal:
            answer = ("refuse", refusal.code, refusal.message)
        except PplError:
            raise
        except Exception as error:
            raise PplError("IMPLEMENTATION_FAILURE", "demo step %d (%s) raised %s: %s"
                           % (number, tried, type(error).__name__, error)) from error
        ok = _matches(expect, answer)
        emit("%2d %s: %s -> %s%s%s" % (
            number, actor, tried, _words(answer, box, expect), " [%s]" % rule if rule else "",
            "" if ok else " UNEXPECTED, expected %s" % _expected(expect)))
        if not ok:
            raise PplError("IMPLEMENTATION_FAILURE", "demo step %d (%s) answered %s, expected %s"
                           % (number, tried, _words(answer, box), _expected(expect)))


def run(keep=False, emit=print):
    if shutil.which("git") is None:
        raise PplError("EXTERNAL_BLOCKED", "the demo needs git on PATH and found none")
    emit(HEADLINE)
    started = time.monotonic()
    box = sandbox.Sandbox.create()
    try:
        try:
            box.build()
        except PplError as error:
            raise PplError("IMPLEMENTATION_FAILURE",
                           "demo setup failed: %s" % error.message) from error
        _walk(box, emit)
        emit("done in %.1f s" % (time.monotonic() - started))
        if keep:
            emit("kept: %s. Look at the run: PPL_HOME=%s ppl serve --open"
                 % (box.root, box.ppl_home))
    except PplError:
        if keep:
            emit("kept: %s" % box.root)
        raise
    finally:
        if not keep:
            box.remove()

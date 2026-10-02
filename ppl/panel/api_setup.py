"""The wizard's routes: the step state, one step as a job, a skip, and the
doctor report the last screen shows."""
from .. import doctor
from ..studio import wizard
from .jobs import JOBS


def _run(params, body, query):
    step = params["step"]
    wizard.check_step(step)
    answers = wizard.check_answers((body or {}).get("answers", {}))
    return {"job": JOBS.start("setup", lambda emit: wizard.run_step(step, answers, emit))}


def register(router):
    router.add("GET", "/api/setup", lambda params, body, query: wizard.state())
    router.add("POST", "/api/setup/<step>", _run)
    router.add("POST", "/api/setup/<step>/skip",
               lambda params, body, query: wizard.skip_step(params["step"]))
    router.add("GET", "/api/doctor",
               lambda params, body, query: doctor.report(cwd=doctor.workspace()))

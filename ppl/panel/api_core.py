"""Routes every studio page uses."""
from .jobs import JOBS


def register(router):
    router.add("GET", "/api/jobs/<job>", lambda params, body, query: JOBS.get(params["job"]))

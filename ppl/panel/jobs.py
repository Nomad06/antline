"""Slow studio actions (a model probe, a Claude extraction, a git clone, an
apply) run in a worker thread, and the request returns at once with a job
id. One job of a kind runs at a time. Jobs live in memory only: the effect
of each is a file write that is safe to repeat after a restart."""
import json
import threading
import traceback
import uuid

from ..errors import PplError

KEEP = 50


def _unknown(job_id):
    return PplError("SCHEMA_INVALID",
                    "no job %r here, it was pruned or lost with a restart of ppl serve" % job_id)


class Jobs:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}
        self._finished = {}
        self._running = {}

    def start(self, kind, fn):
        with self._lock:
            if kind in self._running:
                raise PplError("BUSY", "a %s job is already running, wait for it to finish" % kind)
            job_id = uuid.uuid4().hex
            self._jobs[job_id] = {"id": job_id, "kind": kind, "state": "running",
                                  "result": None, "error": None, "log": []}
            finished = threading.Event()
            self._finished[job_id] = finished
            self._running[kind] = job_id
            self._prune()
            job = self._jobs[job_id]
        threading.Thread(target=self._run, args=(job, fn, finished), daemon=True).start()
        return job_id

    def _run(self, job, fn, finished):
        """The outcome and the freed kind land in one locked step, so a poll
        that reads the job finished can start the next one of its kind."""
        result = None
        error = {"error_code": "INTERNAL_ERROR", "message": "the job stopped without an outcome"}
        try:
            result = fn(job["log"].append)
            json.dumps(result)
            error = None
        except PplError as exc:
            error = exc.as_dict()
        except Exception as exc:
            traceback.print_exc()
            error = {"error_code": "INTERNAL_ERROR",
                     "message": "%s: %s" % (type(exc).__name__, exc)}
        finally:
            with self._lock:
                job["result"] = None if error else result
                job["error"] = error
                job["state"] = "failed" if error else "done"
                self._running.pop(job["kind"], None)
            finished.set()

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise _unknown(job_id)
            return {"id": job["id"], "kind": job["kind"], "state": job["state"],
                    "result": job["result"], "error": job["error"], "log": list(job["log"])}

    def wait(self, job_id, timeout=None):
        with self._lock:
            event = self._finished.get(job_id)
        if event is None:
            raise _unknown(job_id)
        return event.wait(timeout)

    def _prune(self):
        finished = [job_id for job_id, job in self._jobs.items() if job["state"] != "running"]
        for job_id in finished[:max(0, len(self._jobs) - KEEP)]:
            self._jobs.pop(job_id, None)
            self._finished.pop(job_id, None)


JOBS = Jobs()

"""Slow studio actions (a model probe, a Claude extraction, a git clone, an
apply) run in a worker thread, and the request returns at once with a job
id. One job of a kind runs at a time. Jobs live in memory only: the effect
of each is a file write that is safe to repeat after a restart."""
import threading
import traceback
import uuid

from ..errors import PplError

KEEP = 50


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
            self._finished[job_id] = threading.Event()
            self._running[kind] = job_id
            self._prune()
            job = self._jobs[job_id]
        threading.Thread(target=self._run, args=(job, fn), daemon=True).start()
        return job_id

    def _run(self, job, fn):
        try:
            job["result"] = fn(job["log"].append)
            job["state"] = "done"
        except PplError as exc:
            job["error"] = exc.as_dict()
            job["state"] = "failed"
        except Exception as exc:
            traceback.print_exc()
            job["error"] = {"error_code": "INTERNAL_ERROR",
                            "message": "%s: %s" % (type(exc).__name__, exc)}
            job["state"] = "failed"
        finally:
            with self._lock:
                self._running.pop(job["kind"], None)
            self._finished[job["id"]].set()

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise PplError("SCHEMA_INVALID",
                               "no job %r here, jobs do not survive a restart of ppl serve" % job_id)
            return {"id": job["id"], "kind": job["kind"], "state": job["state"],
                    "result": job["result"], "error": job["error"], "log": list(job["log"])}

    def wait(self, job_id, timeout=None):
        event = self._finished.get(job_id)
        if event is None:
            raise PplError("SCHEMA_INVALID", "no job %r" % job_id)
        return event.wait(timeout)

    def _prune(self):
        finished = [job_id for job_id, job in self._jobs.items() if job["state"] != "running"]
        for job_id in finished[:max(0, len(self._jobs) - KEEP)]:
            self._jobs.pop(job_id, None)
            self._finished.pop(job_id, None)


JOBS = Jobs()

import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
from ppl.errors import PplError
from ppl.panel import jobs


class GatedLock:
    """Stands in for the Jobs lock. A worker thread waits at the gate before
    taking it, so a test can look at a job while its worker is about to
    record the outcome. The test's own thread is never held."""

    def __init__(self):
        self._lock = threading.Lock()
        self._test_thread = threading.current_thread()
        self.gate = threading.Event()
        self.worker_waiting = threading.Event()

    def __enter__(self):
        if threading.current_thread() is not self._test_thread:
            self.worker_waiting.set()
            self.gate.wait(5)
        self._lock.acquire()
        return self

    def __exit__(self, *exc_info):
        self._lock.release()


class TestJobs(unittest.TestCase):
    def setUp(self):
        self.jobs = jobs.Jobs()

    def gate_the_workers(self):
        lock = GatedLock()
        self.jobs._lock = lock
        self.addCleanup(lock.gate.set)
        return lock

    def finish(self, job_id):
        self.assertTrue(self.jobs.wait(job_id, timeout=5))
        return self.jobs.get(job_id)

    def test_a_job_keeps_its_result_and_its_log(self):
        def work(emit):
            emit("one")
            emit("two")
            return {"answer": 42}
        state = self.finish(self.jobs.start("demo", work))
        self.assertEqual(state["state"], "done")
        self.assertEqual(state["result"], {"answer": 42})
        self.assertEqual(state["log"], ["one", "two"])
        self.assertIsNone(state["error"])

    def test_a_ppl_error_fails_the_job_with_its_envelope(self):
        def work(emit):
            raise PplError("SETUP_INCOMPLETE", "no answer for workspace")
        state = self.finish(self.jobs.start("demo", work))
        self.assertEqual(state["state"], "failed")
        self.assertEqual(state["error"], {"error_code": "SETUP_INCOMPLETE",
                                          "message": "no answer for workspace"})

    def test_an_unexpected_error_fails_the_job_with_an_envelope(self):
        def work(emit):
            raise RuntimeError("boom")
        with redirect_stderr(io.StringIO()):
            state = self.finish(self.jobs.start("demo", work))
        self.assertEqual(state["error"], {"error_code": "INTERNAL_ERROR",
                                          "message": "RuntimeError: boom"})

    def test_a_second_job_of_a_kind_is_refused_while_the_first_runs(self):
        release = threading.Event()
        first = self.jobs.start("demo", lambda emit: release.wait(5))
        with self.assertRaises(PplError) as caught:
            self.jobs.start("demo", lambda emit: None)
        self.assertEqual(caught.exception.code, "BUSY")
        release.set()
        self.finish(first)
        self.finish(self.jobs.start("demo", lambda emit: None))

    def test_jobs_of_different_kinds_run_side_by_side(self):
        release = threading.Event()
        first = self.jobs.start("one", lambda emit: release.wait(5))
        second = self.jobs.start("two", lambda emit: "done")
        self.assertEqual(self.finish(second)["result"], "done")
        release.set()
        self.finish(first)

    def test_an_unknown_job_is_refused(self):
        with self.assertRaises(PplError) as caught:
            self.jobs.get("nope")
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_result_json_cannot_encode_fails_the_job(self):
        with redirect_stderr(io.StringIO()):
            state = self.finish(self.jobs.start("demo", lambda emit: object()))
        self.assertEqual(state["state"], "failed")
        self.assertIsNone(state["result"])
        self.assertEqual(state["error"]["error_code"], "INTERNAL_ERROR")
        self.assertTrue(state["error"]["message"].startswith("TypeError: "), state["error"])

    def test_a_job_reads_done_only_once_its_kind_is_free_again(self):
        lock = self.gate_the_workers()
        first = self.jobs.start("demo", lambda emit: "x")
        self.assertTrue(lock.worker_waiting.wait(5))
        self.assertEqual(self.jobs.get(first)["state"], "running")
        with self.assertRaises(PplError) as caught:
            self.jobs.start("demo", lambda emit: None)
        self.assertEqual(caught.exception.code, "BUSY")
        lock.gate.set()
        self.assertEqual(self.finish(first)["state"], "done")
        self.finish(self.jobs.start("demo", lambda emit: None))

    def test_a_finishing_job_is_not_pruned_before_it_signals(self):
        lock = self.gate_the_workers()
        with mock.patch.object(jobs, "KEEP", 0):
            first = self.jobs.start("one", lambda emit: "x")
            self.assertTrue(lock.worker_waiting.wait(5))
            self.jobs.start("two", lambda emit: None)
            lock.gate.set()
            self.assertEqual(self.finish(first)["state"], "done")

    def test_an_unknown_job_says_it_may_have_been_pruned_or_lost(self):
        for look in (self.jobs.get, self.jobs.wait):
            with self.subTest(look=look.__name__):
                with self.assertRaises(PplError) as caught:
                    look("nope")
                self.assertEqual(caught.exception.code, "SCHEMA_INVALID")
                self.assertIn("pruned or lost with a restart", caught.exception.message)

    def test_get_returns_a_copy_of_the_log(self):
        state = self.finish(self.jobs.start("demo", lambda emit: emit("x")))
        state["log"].append("y")
        self.assertEqual(self.jobs.get(state["id"])["log"], ["x"])


if __name__ == "__main__":
    unittest.main()

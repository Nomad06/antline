import io
import os
import sys
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
from ppl.errors import PplError
from ppl.panel import jobs


class TestJobs(unittest.TestCase):
    def setUp(self):
        self.jobs = jobs.Jobs()

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

    def test_get_returns_a_copy_of_the_log(self):
        state = self.finish(self.jobs.start("demo", lambda emit: emit("x")))
        state["log"].append("y")
        self.assertEqual(self.jobs.get(state["id"])["log"], ["x"])


if __name__ == "__main__":
    unittest.main()

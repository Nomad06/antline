import ast
import multiprocessing
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

# a spawned child re-imports this module and must keep the PPL_HOME its parent chose
os.environ.setdefault("PPL_HOME", tmphome.mkdtemp(prefix="pplhome-"))

from ppl import lanes, store
from ppl.errors import PplError


def _claim_task(task_id, queue, barrier=None, pause=0.0):
    """Claim the one slot in a spawned child. With `pause`, the registry read inside the lock
    sleeps that long after reading, so a second claim that does not wait for the lock reads
    the same empty registry; with `barrier`, both children reach the claim together."""
    try:
        if pause:
            read = lanes.read

            def slow(*args, **kwargs):
                found = read(*args, **kwargs)
                time.sleep(pause)
                return found
            lanes.read = slow
        if barrier is not None:
            barrier.wait(60)
        entry = lanes.claim(task_id, "h", "a" * 40, "/r", "/w", "s", 1, 1)
        queue.put(("ok", entry["lane"]))
    except PplError as err:
        queue.put((err.code, err.message))


def claim(task, harness="h", harness_max=9, machine_max=9):
    return lanes.claim(task, harness, "a" * 40, "/repo", "/wt/" + task, "sid", harness_max,
                       machine_max)


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def test_claim_takes_the_lowest_free_lane(self):
        claim("A-1")
        claim("B-1")
        self.assertTrue(lanes.release("A-1"))
        self.assertEqual(claim("C-1")["lane"], 1)
        self.assertEqual([item["lane"] for item in lanes.read()], [2, 1])
        self.assertEqual(lanes.entry("C-1")["start_dir"], str(lanes.start_dir("C-1")))
        self.assertIsNone(lanes.entry("A-1"))
        self.assertFalse(lanes.release("A-1"))

    def test_a_full_machine_is_refused_busy_naming_the_running_tasks(self):
        # R-LANE-RESOURCE-ISOLATION: the machine limit counts every harness's lanes
        claim("A-1", machine_max=2)
        claim("B-1", machine_max=2)
        with self.assertRaises(PplError) as caught:
            claim("C-1", machine_max=2)
        self.assertEqual(caught.exception.code, "BUSY")
        self.assertIn("A-1", caught.exception.message)
        self.assertIn("B-1", caught.exception.message)

    def test_the_harness_limit_counts_only_that_harness(self):
        claim("A-1", harness="one", harness_max=1)
        with self.assertRaises(PplError) as caught:
            claim("B-1", harness="one", harness_max=1)
        self.assertEqual(caught.exception.code, "BUSY")
        self.assertIn("A-1", caught.exception.message)
        self.assertEqual(claim("C-1", harness="two", harness_max=1)["harness"], "two")

    def test_a_task_with_a_lane_cannot_claim_another(self):
        claim("A-1")
        with self.assertRaises(PplError) as caught:
            claim("A-1")
        self.assertEqual(caught.exception.code, "ILLEGAL_TRANSITION")

    def test_set_pid_records_the_process(self):
        claim("A-1")
        lanes.set_pid("A-1", 4242)
        self.assertEqual(lanes.entry("A-1")["pid"], 4242)

    def assert_invalid(self):
        with self.assertRaises(PplError) as caught:
            lanes.read()
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_truncated_registry_is_refused(self):
        claim("A-1")
        path = lanes.registry_path()
        path.write_text(path.read_text(encoding="utf-8")[:20], encoding="utf-8")
        self.assert_invalid()
        path.write_text('{"version": 2, "lanes": []}', encoding="utf-8")
        self.assert_invalid()

    def test_a_registry_over_the_limit_is_refused(self):
        claim("A-1")
        with self.assertRaises(PplError) as caught:
            lanes.read(limit=10)
        self.assertEqual(caught.exception.code, "SCHEMA_INVALID")

    def test_a_fifo_registry_is_refused_without_blocking(self):
        os.mkfifo(str(lanes.registry_path()))
        started = time.monotonic()
        self.assert_invalid()
        self.assertLess(time.monotonic() - started, 1)

    def test_for_path_matches_through_a_symlinked_parent(self):
        base = Path(tmphome.mkdtemp(prefix="pplsym-")).resolve()
        real = base / "real"
        real.mkdir()
        link = base / "link"
        link.symlink_to(real)
        os.environ["PPL_HOME"] = str(link / "home")
        record = claim("A-1")
        inside = Path(record["start_dir"]) / "sub" / "file"
        self.assertEqual(lanes.for_path(inside)["task"], "A-1")
        self.assertEqual(lanes.for_path(real / "home" / "lanes" / "A-1" / "x")["task"], "A-1")
        self.assertEqual(lanes.for_path(link / "home" / "lanes" / "A-1")["task"], "A-1")
        self.assertIsNone(lanes.for_path(real / "home" / "lanes" / "A-10"))
        self.assertIsNone(lanes.for_path(real / "elsewhere"))

    def test_for_path_matches_the_worktree(self):
        wt = Path(tmphome.mkdtemp(prefix="pplwt-")).resolve()
        lanes.claim("A-1", "h", "a" * 40, "/r", str(wt), "s", 9, 9)
        self.assertEqual(lanes.for_path(wt / "src")["task"], "A-1")

    def test_two_processes_racing_for_the_last_slot_get_one_lane(self):
        # Final review M8: the two claims meet at a barrier and each one's registry read
        # sleeps inside the lock, so their critical sections overlap whenever the lock does
        # not hold them apart (without it both read an empty registry and both take lane 1)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        barrier = context.Barrier(2)
        procs = [context.Process(target=_claim_task, args=(task, queue, barrier, 0.3))
                 for task in ("A-1", "B-1")]
        for proc in procs:
            proc.start()
        results = [queue.get(timeout=60) for _ in procs]
        for proc in procs:
            proc.join(30)
        self.assertEqual(sorted(code for code, _ in results), ["BUSY", "ok"])
        self.assertEqual(len(lanes.read()), 1)

    def test_a_registry_that_is_not_utf8_is_refused(self):
        lanes.registry_path().parent.mkdir(parents=True, exist_ok=True)
        lanes.registry_path().write_bytes(b"\xff")
        self.assert_invalid()

    def test_lanes_parses_on_python_3_9(self):
        source = Path(lanes.__file__).read_text(encoding="utf-8")
        ast.parse(source, feature_version=(3, 9))


class TestBinding(unittest.TestCase):
    """A hook's lane is the `--task <id>` its own command line carries."""

    def setUp(self):
        self._home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

    def tearDown(self):
        os.environ["PPL_HOME"] = self._home

    def test_hook_task_reads_one_task_pair_and_nothing_else(self):
        self.assertIsNone(lanes.hook_task([]))
        self.assertEqual(lanes.hook_task(["--task", "LA-1"]), "LA-1")
        for argv in (["--task"], ["--task", "LA-1", "--task", "LA-1"], ["--task", "la-1"],
                     ["--task", ""], ["--task", "LA-1\n"], ["--task=LA-1"], ["LA-1"],
                     ["--task", "LA-1", "-q"], ["--task", 7]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                lanes.hook_task(argv)

    def test_binding_names_the_registered_lane(self):
        record = claim("LA-1")
        self.assertEqual(lanes.binding(["--task", "LA-1"]), lanes.Binding("LA-1", record, None))
        self.assertEqual(lanes.binding([]), lanes.Binding(None, None, None))
        self.assertEqual(lanes.binding(()), lanes.Binding(None, None, None))
        # an argv that is no list at all binds no lane and fails closed, it does not raise
        for argv in (None, 7):
            bound = lanes.binding(argv)
            self.assertEqual((bound.task, bound.entry), (None, None))
            self.assertIn("binds no lane", bound.problem)

    def test_a_lane_the_registry_does_not_hold_is_a_problem(self):
        claim("LA-1")
        bound = lanes.binding(["--task", "LB-1"])
        self.assertEqual((bound.task, bound.entry), ("LB-1", None))
        self.assertEqual(bound.problem, "lane LB-1 is not registered: run ppl lane resume LB-1")
        bound = lanes.binding(["--task", "lb-1"])
        self.assertEqual((bound.task, bound.entry), (None, None))
        self.assertIn("binds no lane", bound.problem)

    def test_binding_never_raises_on_any_registry(self):
        path = lanes.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        cases = (b"", b"\xff", b"garbage", b'{"version": 1, "lan', b"[]", b"null",
                 b'{"version": 2, "lanes": []}', b'{"version": 1, "lanes": [1]}',
                 b"[" * 100000 + b"]" * 100000)
        for data in cases:
            with self.subTest(data=data[:20]):
                path.write_bytes(data)
                bound = lanes.binding(["--task", "LA-1"])
                self.assertEqual((bound.task, bound.entry), ("LA-1", None))
                self.assertIn(str(path), bound.problem)
        path.unlink()
        os.mkfifo(str(path))
        started = time.monotonic()
        self.assertIn("not a regular file", lanes.binding(["--task", "LA-1"]).problem)
        self.assertLess(time.monotonic() - started, 1)
        path.unlink()
        with open(str(path), "wb") as handle:
            handle.truncate(lanes.READ_LIMIT + 1)
        self.assertIn(str(path), lanes.binding(["--task", "LA-1"]).problem)


if __name__ == "__main__":
    unittest.main()

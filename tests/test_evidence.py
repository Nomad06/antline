import itertools
import os
import sys
import tempfile
import unittest
from pathlib import Path

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")

from ppl import evidence, run, schema
from ppl.errors import PplError

PRODUCER = {"provider": "claude-code-subagent", "model": "sonnet", "params_hash": "abc"}
COUNTER = itertools.count(200)


def rc_file(value):
    path = Path(tempfile.mkstemp(suffix=".rc")[1])
    path.write_text("%s\n" % value, encoding="utf-8")
    return str(path)


class TestEvidence(unittest.TestCase):
    def setUp(self):
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "https://j/x", "/tmp/repo")

    def test_pass_receipt_requires_rc_zero(self):
        # rule: R-EVIDENCE-RC-FILE
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "focused-tests", "pass", "abc123",
                         "/tmp/log.txt", rc_file(1), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("rc=1", ctx.exception.message)

    def test_pass_receipt_is_stored_when_rc_is_zero(self):
        receipt = evidence.add(self.task, "focused-tests", "pass", "abc123",
                               "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(receipt["result"], "pass")
        self.assertEqual(receipt["candidate_sha"], "abc123")

    def test_fail_receipt_with_nonzero_rc_is_accepted(self):
        receipt = evidence.add(self.task, "full-suite", "fail", "abc123",
                               "/tmp/log.txt", rc_file(1), PRODUCER)
        self.assertEqual(receipt["result"], "fail")

    def test_missing_rc_file_is_refused(self):
        # rule: R-EVIDENCE-RC-FILE
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "full-suite", "pass", "abc123",
                         "/tmp/log.txt", "/tmp/does-not-exist.rc", PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_receipt_carries_producer_attribution(self):
        receipt = evidence.add(self.task, "checkstyle", "pass", "abc123",
                               "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(receipt["producer"]["model"], "sonnet")

    def test_receipts_are_partitioned_by_sha(self):
        evidence.add(self.task, "focused-tests", "pass", "aaa", "/tmp/l", rc_file(0), PRODUCER)
        evidence.add(self.task, "focused-tests", "pass", "bbb", "/tmp/l", rc_file(0), PRODUCER)
        self.assertEqual(list(evidence.for_sha(self.task, "aaa")), ["focused-tests"])
        self.assertEqual(list(evidence.for_sha(self.task, "bbb")), ["focused-tests"])

    def test_for_sha_drops_a_receipt_file_whose_content_is_null(self):
        # rule: R-EVIDENCE-RECEIPT-SHAPE (W5(c), cycle 2 wave B). A receipt
        # file containing the literal JSON `null` (valid JSON, so
        # store.read_json's own SCHEMA_INVALID fix does not fire) used to
        # reach done_blockers as `receipts["focused-tests"] = None`, and
        # `receipts.get(check, {}).get("result")` then raised
        # AttributeError: 'NoneType' object has no attribute 'get' - exit 1 -
        # while `ppl status` on the same state stayed exit 0. Treated the same
        # way findings._load_all already treats a non-dict document: dropped,
        # so the check reads as "no receipt", which fails the done gate
        # closed rather than crashing it.
        evidence.add(self.task, "focused-tests", "pass", "ccc", "/tmp/l", rc_file(0), PRODUCER)
        from ppl import paths, store
        store.write_json(paths.run_dir(self.task) / "evidence" / "ccc" / "focused-tests.json",
                         None)
        receipts = evidence.for_sha(self.task, "ccc")
        self.assertNotIn("focused-tests", receipts)

    def test_for_sha_of_unknown_sha_is_empty(self):
        self.assertEqual(evidence.for_sha(self.task, "fff"), {})

    def test_add_rejects_check_with_traversal_slash(self):
        # rule: R-EVIDENCE-CHECK-NAME
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "build/../escape", "pass", "abc123",
                         "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("check name", ctx.exception.message)

    def test_add_rejects_check_with_leading_dot(self):
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, ".hidden", "pass", "abc123",
                         "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("check name", ctx.exception.message)

    def test_add_rejects_sha_with_traversal(self):
        # rule: R-EVIDENCE-SHA-PARTITION
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "build", "pass", "abc/../escape",
                         "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("candidate SHA", ctx.exception.message)

    def test_add_rejects_sha_with_non_hex(self):
        # rule: R-EVIDENCE-SHA-PARTITION
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "build", "pass", "xyz123",
                         "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("hexadecimal", ctx.exception.message)

    def test_for_sha_rejects_traversal(self):
        with self.assertRaises(PplError) as ctx:
            evidence.for_sha(self.task, "abc/../escape")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("candidate SHA", ctx.exception.message)

    def test_normal_check_and_sha_still_work(self):
        receipt = evidence.add(self.task, "normal-check", "pass", "def456",
                               "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(receipt["check"], "normal-check")
        self.assertEqual(receipt["candidate_sha"], "def456")
        receipts = evidence.for_sha(self.task, "def456")
        self.assertIn("normal-check", receipts)

    def test_for_sha_with_empty_sha_returns_empty_dict(self):
        result = evidence.for_sha(self.task, "")
        self.assertEqual(result, {})

    def test_for_sha_with_none_sha_returns_empty_dict(self):
        result = evidence.for_sha(self.task, None)
        self.assertEqual(result, {})

    def test_for_sha_still_rejects_traversal_after_empty_sha_fix(self):
        with self.assertRaises(PplError) as ctx:
            evidence.for_sha(self.task, "../escape")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


class TestCheckNameMatchesTheVerdictReference(unittest.TestCase):
    """A verdict cites a receipt as "evidence:<check>", and
    schema.ACCEPTANCE_EVIDENCE_RE decides what may follow the colon. A check
    name this module accepts but that regex rejects files a receipt no verdict
    can ever reference - and it only surfaces at the last gate.
    """

    def setUp(self):
        self.task = "DEMO-820"
        try:
            run.init(self.task, "u", "/tmp/repo")
        except PplError:
            pass

    def test_a_space_in_a_check_name_is_refused(self):
        # rule: R-EVIDENCE-CHECK-NAME
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "integration tests", "pass", "abc123",
                         "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_every_accepted_check_name_is_referenceable_from_a_verdict(self):
        for name in ("focused-tests", "secret-scan", "build.gradle_check", "A1"):
            receipt = evidence.add(self.task, name, "pass", "abc123",
                                   "/tmp/log.txt", rc_file(0), PRODUCER)
            self.assertTrue(
                schema.ACCEPTANCE_EVIDENCE_RE.match("evidence:%s" % receipt["check"]),
                "%r is filed but cannot be cited" % name)

    def test_a_colon_in_a_check_name_is_refused(self):
        with self.assertRaises(PplError) as ctx:
            evidence.add(self.task, "unit:tests", "pass", "abc123",
                         "/tmp/log.txt", rc_file(0), PRODUCER)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

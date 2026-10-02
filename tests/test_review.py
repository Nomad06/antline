import itertools
import os
import sys
import unittest
from pathlib import Path

# A single home directory shared across this module's tests (like
# tests/test_routing.py), but the environment variable itself is pinned in
# setUp/tearDown rather than left to whichever module happened to set it last
# at import time under `unittest discover`.

# tests/ is not a package, so the helper next to this file is imported
# by path. It must be imported before PPL_HOME is chosen below.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome
HOME = tmphome.mkdtemp(prefix="pplhome-")
os.environ["PPL_HOME"] = HOME

from ppl import review, run
from ppl.errors import PplError

COUNTER = itertools.count(900)


class TestReviewLedger(unittest.TestCase):
    def setUp(self):
        self._saved_home = os.environ.get("PPL_HOME")
        os.environ["PPL_HOME"] = HOME
        self.task = "DEMO-%d" % next(COUNTER)
        run.init(self.task, "u", "/tmp/repo")

    def tearDown(self):
        if self._saved_home is None:
            os.environ.pop("PPL_HOME", None)
        else:
            os.environ["PPL_HOME"] = self._saved_home

    def test_set_mr_stores_iid_and_url(self):
        state = review.set_mr(self.task, 15500, "https://gitlab/x/-/merge_requests/15500")
        self.assertEqual(state["mr"]["iid"], 15500)
        self.assertEqual(state["mr"]["url"], "https://gitlab/x/-/merge_requests/15500")

    def test_approval_state_none_before_any_approval(self):
        review.set_mr(self.task, 1, "u")
        self.assertEqual(review.approval_state(self.task, "abc"), "none")

    def test_approval_state_current_on_matching_sha(self):
        review.set_mr(self.task, 1, "u")
        review.set_approval(self.task, "abc")
        self.assertEqual(review.approval_state(self.task, "abc"), "current")

    def test_approval_state_stale_after_head_moves(self):
        review.set_mr(self.task, 1, "u")
        review.set_approval(self.task, "abc")
        self.assertEqual(review.approval_state(self.task, "def"), "stale")

    def test_reply_is_recorded_per_thread_and_sha(self):
        review.record_reply(self.task, "t1", "abc", "NEEDS_REPLY_ONLY")
        self.assertTrue(review.already_answered(self.task, "t1", "abc"))
        self.assertFalse(review.already_answered(self.task, "t1", "def"))
        self.assertFalse(review.already_answered(self.task, "t2", "abc"))

    def test_pending_excludes_already_answered_threads(self):
        threads = [{"id": "t1", "author": "reviewer"}, {"id": "t2", "author": "reviewer"}]
        review.record_reply(self.task, "t1", "abc", "NEEDS_REPLY_ONLY")
        pending = review.pending(self.task, threads, "abc")
        self.assertEqual([t["id"] for t in pending], ["t2"])

    def test_pending_excludes_resolved_threads(self):
        threads = [{"id": "t3", "author": "reviewer"},
                   {"id": "t4", "author": "reviewer", "resolved": True}]
        self.assertEqual([t["id"] for t in review.pending(self.task, threads, "abc")], ["t3"])

    def test_pending_excludes_threads_authored_by_the_stored_pipeline_identity(self):
        review.set_mr(self.task, 1, "u", author="ppl-bot-42")
        threads = [{"id": "t5", "author": "reviewer"},
                   {"id": "t6", "author": "ppl-bot-42"}]
        self.assertEqual([t["id"] for t in review.pending(self.task, threads, "abc")], ["t5"])

    # --- own-reply vs own-remark: the identity may be the human MR author ---
    # rule: R-REVIEW-NO-DOUBLE-REPLY

    def test_pending_keeps_a_thread_the_pipeline_identity_opened(self):
        # The orchestrator runs as the human MR author (GitLab MCP acts as
        # that account), so the author's own review remark is an opening
        # note by the identity, not a pipeline reply. It must reach the
        # responder, or the loop reports "no threads" while a remark waits.
        review.set_mr(self.task, 1, "u", author="m.author")
        threads = [{"id": "t9a", "author": "m.author", "last_author": "m.author",
                    "note_count": 1, "last_note_id": "n1"}]
        self.assertEqual([t["id"] for t in review.pending(self.task, threads, "abc")], ["t9a"])

    def test_pending_excludes_a_thread_the_pipeline_identity_replied_to(self):
        # A reviewer opened it, the identity's reply is the newest note and
        # nothing was recorded (a crash between posting and `ppl review
        # answered`): still not re-dispatched, the double-reply guard holds.
        review.set_mr(self.task, 1, "u", author="m.author")
        threads = [{"id": "t9b", "author": "reviewer", "last_author": "m.author",
                    "note_count": 2, "last_note_id": "n2"}]
        self.assertEqual(review.pending(self.task, threads, "abc"), [])

    def test_pending_includes_a_thread_again_when_the_reviewer_answers_the_identity(self):
        review.set_mr(self.task, 1, "u", author="m.author")
        threads = [{"id": "t9c", "author": "m.author", "last_author": "reviewer",
                    "note_count": 2, "last_note_id": "n3"}]
        self.assertEqual([t["id"] for t in review.pending(self.task, threads, "abc")], ["t9c"])

    def test_pending_legacy_thread_shape_keeps_the_first_author_rule_and_logs_once(self):
        from ppl import paths
        review.set_mr(self.task, 1, "u", author="m.author")
        threads = [{"id": "t9d", "author": "m.author"}]
        self.assertEqual(review.pending(self.task, threads, "abc"), [])
        self.assertEqual(review.pending(self.task, threads, "abc"), [])
        text = (paths.run_dir(self.task) / "log.jsonl").read_text()
        self.assertEqual(text.count("review_pending_legacy_thread_shape"), 1)

    def test_pending_refuses_a_non_positive_note_count(self):
        threads = [{"id": "t9e", "author": "reviewer", "last_author": "reviewer", "note_count": 0}]
        with self.assertRaises(PplError) as ctx:
            review.pending(self.task, threads, "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_pending_accepts_an_author_override(self):
        threads = [{"id": "t7", "author": "reviewer"},
                   {"id": "t8", "author": "some-other-bot"}]
        result = review.pending(self.task, threads, "abc", author="some-other-bot")
        self.assertEqual([t["id"] for t in result], ["t7"])

    def test_explicit_author_override_wins_over_the_stored_identity(self):
        review.set_mr(self.task, 1, "u", author="ppl-bot-42")
        threads = [{"id": "t7a", "author": "ppl-bot-42"},
                   {"id": "t7b", "author": "some-other-bot"}]
        result = review.pending(self.task, threads, "abc", author="some-other-bot")
        self.assertEqual([t["id"] for t in result], ["t7a"])

    def test_pending_does_not_filter_by_author_when_nothing_is_configured(self):
        # No ppl mr set --author was ever called, and the caller passed
        # nothing either: PIPELINE_AUTHOR must not be used as a silent
        # placeholder default, so a thread that happens to carry that exact
        # string as its author still comes back as pending.
        threads = [{"id": "t8a", "author": "reviewer"},
                   {"id": "t8b", "author": review.PIPELINE_AUTHOR}]
        result = review.pending(self.task, threads, "abc")
        self.assertEqual(sorted(t["id"] for t in result), ["t8a", "t8b"])

    def test_pending_with_no_configured_author_logs_that_it_skipped_filtering(self):
        from ppl import paths
        review.pending(self.task, [{"id": "t8c", "author": "reviewer"}], "abc")
        text = (paths.run_dir(self.task) / "log.jsonl").read_text()
        self.assertIn("review_pending_unfiltered", text)

    def test_unfiltered_author_warning_is_logged_only_once_per_run(self):
        from ppl import paths
        for _ in range(5):
            review.pending(self.task, [{"id": "t8d", "author": "reviewer"}], "abc")
        text = (paths.run_dir(self.task) / "log.jsonl").read_text()
        self.assertEqual(text.count("review_pending_unfiltered"), 1)

    def test_set_mr_author_is_optional_and_sticky_across_calls(self):
        review.set_mr(self.task, 1, "u", author="ppl-bot-42")
        state = review.set_mr(self.task, 2, "u2")
        self.assertEqual(state["mr"]["author"], "ppl-bot-42")

    def test_unknown_classification_is_refused(self):
        # rule: R-REVIEW-CLASSIFICATION
        with self.assertRaises(PplError):
            review.record_reply(self.task, "t9", "abc", "LOOKS_FINE_TO_ME")

    def test_record_reply_on_an_uninitialised_task_is_refused(self):
        with self.assertRaises(PplError) as ctx:
            review.record_reply("DEMO-99999", "t1", "abc", "NEEDS_REPLY_ONLY")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_approval_state_is_none_when_approved_sha_is_the_empty_string(self):
        state = run.load(self.task)
        state["mr"]["approved_sha"] = ""
        run.save(state)
        self.assertEqual(review.approval_state(self.task, "abc"), "none")

    # --- freshness: a follow-up note on an answered thread must be seen ---

    def test_already_answered_true_when_no_note_id_is_given_either_side(self):
        review.record_reply(self.task, "t10", "abc", "NEEDS_REPLY_ONLY")
        self.assertTrue(review.already_answered(self.task, "t10", "abc"))

    def test_already_answered_true_when_last_note_id_matches(self):
        review.record_reply(self.task, "t11", "abc", "NEEDS_REPLY_ONLY", last_note_id="n1")
        self.assertTrue(review.already_answered(self.task, "t11", "abc", "n1"))

    def test_already_answered_false_when_a_newer_note_id_arrives(self):
        review.record_reply(self.task, "t12", "abc", "NEEDS_REPLY_ONLY", last_note_id="n1")
        self.assertFalse(review.already_answered(self.task, "t12", "abc", "n2"))

    def test_already_answered_true_when_recorded_reply_has_no_note_id(self):
        # A caller that never passes last_note_id gets today's coarser
        # behaviour, even if a later caller for the same thread does pass one.
        review.record_reply(self.task, "t13", "abc", "NEEDS_REPLY_ONLY")
        self.assertTrue(review.already_answered(self.task, "t13", "abc", "n1"))

    def test_pending_excludes_a_thread_unchanged_since_it_was_answered(self):
        review.record_reply(self.task, "t14", "abc", "NEEDS_REPLY_ONLY", last_note_id="n1")
        threads = [{"id": "t14", "author": "reviewer", "last_note_id": "n1"}]
        self.assertEqual(review.pending(self.task, threads, "abc"), [])

    def test_pending_includes_a_thread_again_after_a_newer_note(self):
        review.record_reply(self.task, "t15", "abc", "NEEDS_REPLY_ONLY", last_note_id="n1")
        threads = [{"id": "t15", "author": "reviewer", "last_note_id": "n2"}]
        self.assertEqual([t["id"] for t in review.pending(self.task, threads, "abc")], ["t15"])

    def test_pending_treats_a_thread_with_no_note_id_as_today(self):
        review.record_reply(self.task, "t16", "abc", "NEEDS_REPLY_ONLY")
        threads = [{"id": "t16", "author": "reviewer"}]
        self.assertEqual(review.pending(self.task, threads, "abc"), [])

    # --- note id normalization: GitLab hands back an int, the CLI a str ---

    def test_already_answered_true_when_recorded_int_compares_to_a_string(self):
        review.record_reply(self.task, "t17", "abc", "NEEDS_REPLY_ONLY", last_note_id=41)
        self.assertTrue(review.already_answered(self.task, "t17", "abc", "41"))

    def test_already_answered_true_when_recorded_string_compares_to_an_int(self):
        review.record_reply(self.task, "t18", "abc", "NEEDS_REPLY_ONLY", last_note_id="41")
        self.assertTrue(review.already_answered(self.task, "t18", "abc", 41))

    def test_pending_excludes_a_thread_whose_note_id_type_differs_from_the_ledger(self):
        review.record_reply(self.task, "t19", "abc", "NEEDS_REPLY_ONLY", last_note_id=41)
        threads = [{"id": "t19", "author": "reviewer", "last_note_id": "41"}]
        self.assertEqual(review.pending(self.task, threads, "abc"), [])

    # --- ppl review pending: the surface the orchestrator (an LLM, not a
    #     Python caller) actually reaches through a JSON contract ---

    def test_pending_wraps_around_authorship_freshness_and_resolution_together(self):
        review.set_mr(self.task, 1, "u", author="ppl-bot-42")
        review.record_reply(self.task, "t20", "abc", "NEEDS_REPLY_ONLY", last_note_id=1)
        threads = [
            {"id": "t20", "author": "reviewer", "last_note_id": 1},   # unchanged, excluded
            {"id": "t21", "author": "reviewer", "last_note_id": 2},   # new, included
            {"id": "t22", "author": "ppl-bot-42", "last_note_id": 3},  # self, excluded
            {"id": "t23", "author": "reviewer", "resolved": True},    # resolved, excluded
        ]
        result = review.pending(self.task, threads, "abc")
        self.assertEqual([t["id"] for t in result], ["t21"])

    # --- fix round 3: malformed external input must not crash pending() ---

    def test_pending_refuses_a_thread_missing_id(self):
        with self.assertRaises(PplError) as ctx:
            review.pending(self.task, [{"author": "reviewer"}], "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("threads[0]", ctx.exception.message)

    def test_pending_refuses_a_thread_that_is_not_an_object(self):
        with self.assertRaises(PplError) as ctx:
            review.pending(self.task, ["not-a-thread"], "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("threads[0]", ctx.exception.message)

    def test_pending_names_the_index_of_the_malformed_element_not_the_first(self):
        threads = [{"id": "t24", "author": "reviewer"}, {"author": "reviewer"}]
        with self.assertRaises(PplError) as ctx:
            review.pending(self.task, threads, "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("threads[1]", ctx.exception.message)

    def test_pending_refuses_a_non_boolean_resolved_field(self):
        with self.assertRaises(PplError) as ctx:
            review.pending(self.task, [{"id": "t25", "resolved": "yes"}], "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("threads[0]", ctx.exception.message)

    def test_pending_refuses_a_last_note_id_of_an_unexpected_type(self):
        with self.assertRaises(PplError) as ctx:
            review.pending(self.task, [{"id": "t26", "last_note_id": ["nope"]}], "abc")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")
        self.assertIn("threads[0]", ctx.exception.message)

    # --- fix round 3: an explicit --author must not skip the existence check ---

    def test_pending_with_explicit_author_still_requires_an_initialised_run(self):
        with self.assertRaises(PplError) as ctx:
            review.pending("DEMO-88888", [{"id": "t1", "author": "x"}], "abc", author="x")
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")


if __name__ == "__main__":
    unittest.main()

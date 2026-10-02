"""Spike: TypeSafe Jev as a typed-decision accelerator (spikes/jev).

No external network anywhere in here (one test opens a socket to a closed
loopback port to prove http_post swallows the failure). The transport is
otherwise a fake, so what these prove is
the request encoding of ppl's own vocabularies, the response parsing under
the shapes the fetched docs showed (and one they did not), and the gate's
fail-closed behaviour. Model quality on pipeline inputs is NOT proven by
anything in this file - see spikes/jev/README.md.
"""
import io
import json
import os
import re
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tmphome

os.environ["PPL_HOME"] = tmphome.mkdtemp(prefix="pplhome-")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ppl import review, run  # noqa: E402
from spikes.jev import client, gate, questions, receipt, spike  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "spikes" / "jev" / "fixtures" / "choice_confident.json"


def fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class TestRequestEncoding(unittest.TestCase):
    def test_review_options_are_exactly_ppl_classifications(self):
        criteria = questions.review_classification("x")["review_class"]["criteria"]
        self.assertEqual(tuple(criteria), review.CLASSIFICATIONS)

    def test_blocker_options_are_real_blocker_codes(self):
        criteria = questions.blocker_code("x")["blocker"]["criteria"]
        for code in criteria:
            self.assertIn(code, run.BLOCKER_CODES)

    def test_risk_tier_has_two_options_only(self):
        criteria = questions.risk_tier("x")["risk_tier"]["criteria"]
        self.assertEqual(tuple(criteria), ("normal", "critical"))

    def test_every_option_stays_under_the_documented_cap(self):
        for name, question in questions.all_for(spike.SAMPLES[0]).items():
            if question["type"] == "choice":
                self.assertLessEqual(len(question["criteria"]), 255, name)

    def test_request_carries_model_state_and_questions_only(self):
        body = client.build_request("some state", questions.all_for(spike.SAMPLES[0]))
        self.assertEqual(set(body), {"model", "state", "questions"})
        self.assertEqual(body["model"], client.DEFAULT_MODEL)
        for question in body["questions"].values():
            self.assertIn(question["type"], ("choice", "noul", "score"))
            self.assertTrue(question["instructions"].strip())

    def test_empty_state_is_refused_before_any_transport(self):
        with self.assertRaises(ValueError):
            client.build_request("   ", {"q": {"type": "noul", "instructions": "x"}})

    def test_openrouter_transport_prefixes_the_model_id(self):
        saved = {k: os.environ.pop(k, None)
                 for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY", "JEV_MODEL")}
        try:
            os.environ["OPENROUTER_API_KEY"] = "k"
            base, key, model = client.credentials()
            self.assertEqual(base, client.OPENROUTER_BASE)
            self.assertTrue(model.startswith("typesafe/"))
            os.environ["TYPESAFE_API_KEY"] = "d"
            base, key, model = client.credentials()
            self.assertEqual(base, client.DIRECT_BASE)
            self.assertEqual(model, client.DEFAULT_MODEL)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_no_key_means_no_request_at_all(self):
        saved = {k: os.environ.pop(k, None) for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY")}
        calls = []
        try:
            answers, doc = client.decide("s", {"q": {"type": "noul", "instructions": "x"}},
                                         transport=lambda *a: calls.append(a) or {})
            self.assertEqual((answers, doc), (None, None))
            self.assertEqual(calls, [])
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


class TestResponseParsing(unittest.TestCase):
    def test_fixture_parses_every_answer(self):
        answers = client.parse_answers(fixture())
        self.assertEqual(set(answers), {"review_class", "risk_tier", "acceptance_stable",
                                        "decisions_closed", "blocker"})
        self.assertEqual(answers["review_class"].choice, "NEEDS_REPLY_ONLY")
        self.assertAlmostEqual(answers["review_class"].confidence, 0.93)
        self.assertAlmostEqual(answers["acceptance_stable"].noul, 0.97)
        self.assertIsNone(answers["acceptance_stable"].confidence)

    def test_response_keys_are_taken_from_the_response_not_the_request(self):
        # the fetched quickstart sent "urgency" and got "is_urgent" back
        answers = client.parse_answers({"answers": {"is_urgent": {"type": "noul", "noul": 1.0}}})
        self.assertIn("is_urgent", answers)
        self.assertNotIn("urgency", answers)

    def test_a_choice_not_present_in_its_own_probabilities_is_dropped(self):
        answers = client.parse_answers({"answers": {"q": {
            "type": "choice", "choice": "zzz", "probabilities": {"a": 1.0}}}})
        self.assertEqual(answers, {})

    def test_a_non_numeric_probability_drops_the_answer(self):
        answers = client.parse_answers({"answers": {"q": {
            "type": "choice", "choice": "a", "probabilities": {"a": "high"}}}})
        self.assertEqual(answers, {})

    def test_a_boolean_is_not_a_probability(self):
        answers = client.parse_answers({"answers": {"q": {"type": "noul", "noul": True}}})
        self.assertEqual(answers, {})

    def test_garbage_documents_parse_to_nothing(self):
        for doc in (None, {}, {"answers": []}, {"answers": {"q": "yes"}}, {"answers": {"q": {}}}):
            self.assertEqual(client.parse_answers(doc), {}, repr(doc))

    def test_usage_is_read_when_present_and_cost_only_when_reported(self):
        self.assertEqual(client.usage_of(fixture()), {"input_tokens": 612.0, "output_tokens": 71.0})
        self.assertEqual(client.usage_of({"usage": {"input_tokens": 1, "cost": 0.00004}}),
                         {"input_tokens": 1.0, "cost": 0.00004})
        self.assertEqual(client.usage_of({}), {})


class TestTransportFailsClosed(unittest.TestCase):
    CREDS = ("https://example.invalid", "k", "jev-latest")
    QUESTIONS = {"q": {"type": "noul", "instructions": "x"}}

    def test_transport_none_resolves_to_no_answers(self):
        answers, doc = client.decide("s", self.QUESTIONS, transport=lambda *a: None,
                                     creds=self.CREDS)
        self.assertEqual((answers, doc), (None, None))

    def test_transport_exception_is_swallowed_by_http_post_not_decide(self):
        # http_post is the only place that talks to the network and it returns
        # None on any failure - decide never sees an exception from it.
        doc = client.http_post("http://127.0.0.1:9", "k", {"a": 1}, timeout=0.2)
        self.assertIsNone(doc)

    def test_transport_success_threads_answers_and_raw_doc_through(self):
        # the request must carry the choice questions the fixture answers, or
        # decide()'s vocabulary filter drops them - which is the point of it
        qs = questions.all_for(spike.SAMPLES[0])
        answers, doc = client.decide("s", qs, transport=lambda *a: fixture(), creds=self.CREDS)
        self.assertEqual(set(answers), {"review_class", "risk_tier", "acceptance_stable",
                                        "decisions_closed", "blocker"})
        self.assertEqual(doc["model"], "jev-1.13.0")

    def test_a_choice_answer_to_a_question_never_asked_is_dropped(self):
        answers, _ = client.decide("s", self.QUESTIONS, transport=lambda *a: fixture(),
                                   creds=self.CREDS)
        self.assertNotIn("review_class", answers)
        # nouls are kept by response name (the quickstart echoed a different key)
        self.assertIn("acceptance_stable", answers)

    def test_an_out_of_vocabulary_choice_is_dropped_at_decide(self):
        # parse_answers can only check a response against itself - this is the
        # seam that also holds the request, so it is where the vocabulary is
        # enforced. A dropped answer is a missing one, which the gates fail
        # closed on.
        tier_q = {"risk_tier": {"type": "choice", "instructions": "x",
                                "criteria": {"normal": "n", "critical": "c"}}}
        response = {"answers": {"risk_tier": {
            "type": "choice", "choice": "medium", "confidence": 1.0,
            "probabilities": {"medium": 1.0}}}}
        answers, _ = client.decide("s", tier_q, transport=lambda *a: response, creds=self.CREDS)
        self.assertEqual(answers, {})

    def test_a_response_that_omits_an_option_is_dropped_too(self):
        tier_q = {"risk_tier": {"type": "choice", "instructions": "x",
                                "criteria": {"normal": "n", "critical": "c"}}}
        response = {"answers": {"risk_tier": {
            "type": "choice", "choice": "normal", "confidence": 1.0,
            "probabilities": {"normal": 1.0}}}}
        answers, _ = client.decide("s", tier_q, transport=lambda *a: response, creds=self.CREDS)
        self.assertEqual(answers, {})

    def test_transport_receives_the_built_request(self):
        seen = {}

        def fake(base, key, body):
            seen.update(base=base, key=key, body=body)
            return fixture()
        client.decide("the state", self.QUESTIONS, transport=fake, creds=self.CREDS)
        self.assertEqual(seen["base"], self.CREDS[0])
        self.assertEqual(seen["body"]["state"], "the state")
        self.assertEqual(seen["body"]["questions"], self.QUESTIONS)


class TestGate(unittest.TestCase):
    def answer(self, choice, confidence, kind="choice"):
        return client.Answer(kind, choice=choice, probabilities={choice: 1.0}, confidence=confidence)

    def test_confident_choice_is_auto(self):
        result = gate.gate_choice(self.answer("NEEDS_REPLY_ONLY", 0.93))
        self.assertEqual(result["action"], gate.AUTO)
        self.assertEqual(result["choice"], "NEEDS_REPLY_ONLY")

    def test_low_confidence_choice_falls_back_but_still_reports_the_choice(self):
        result = gate.gate_choice(self.answer("DISAGREE", 0.4))
        self.assertEqual(result["action"], gate.FALLBACK)
        self.assertEqual(result["choice"], "DISAGREE")

    def test_missing_confidence_falls_back(self):
        self.assertEqual(gate.gate_choice(self.answer("X", None))["action"], gate.FALLBACK)

    def test_no_answer_falls_back(self):
        self.assertEqual(gate.gate_choice(None)["action"], gate.FALLBACK)

    def test_wrong_kind_falls_back(self):
        noul = client.Answer("noul", noul=0.99, confidence=0.99)
        self.assertEqual(gate.gate_choice(noul)["action"], gate.FALLBACK)

    def test_risk_no_answer_is_critical(self):
        result = gate.gate_risk(None)
        self.assertEqual((result["action"], result["choice"]), (gate.AUTO, "critical"))

    def test_risk_uncertain_normal_is_forced_critical(self):
        result = gate.gate_risk(self.answer("normal", 0.62))
        self.assertEqual(result["choice"], "critical")
        self.assertIn("forced", result["reason"])

    def test_risk_confident_normal_stays_normal(self):
        self.assertEqual(gate.gate_risk(self.answer("normal", 0.95))["choice"], "normal")

    def test_risk_critical_is_never_downgraded(self):
        self.assertEqual(gate.gate_risk(self.answer("critical", 0.05))["choice"], "critical")

    def test_risk_out_of_vocabulary_confident_answer_is_critical_not_normal(self):
        # the fail-open shape: "not critical and confident" must never read as normal
        result = gate.gate_risk(self.answer("medium", 1.0))
        self.assertEqual(result["choice"], "critical")

    def test_choice_out_of_vocabulary_answer_falls_back_when_it_slips_past_decide(self):
        # belt and braces: gate_choice reports the raw choice but the caller
        # gets fallback if the answer never went through decide()'s filter
        # and carries no confidence, and auto otherwise - so decide() is the
        # load-bearing check, pinned by the two decide tests above.
        self.assertEqual(gate.gate_choice(self.answer("medium", None))["action"], gate.FALLBACK)

    def test_noul_yes_no_and_undecided(self):
        yes = gate.gate_noul(client.Answer("noul", noul=0.97))
        no = gate.gate_noul(client.Answer("noul", noul=0.03))
        mid = gate.gate_noul(client.Answer("noul", noul=0.55))
        self.assertEqual((yes["action"], yes["satisfied"]), (gate.AUTO, True))
        self.assertEqual((no["action"], no["satisfied"]), (gate.AUTO, False))
        self.assertEqual((mid["action"], mid["satisfied"]), (gate.FALLBACK, False))

    def test_noul_missing_is_not_satisfied(self):
        result = gate.gate_noul(None)
        self.assertEqual((result["action"], result["satisfied"]), (gate.FALLBACK, False))

    def test_fixture_end_to_end_gates(self):
        gates = spike.run_gates(client.parse_answers(fixture()))
        self.assertEqual(gates["review_class"]["action"], gate.AUTO)
        self.assertEqual(gates["risk_tier"]["choice"], "critical")
        self.assertTrue(gates["acceptance_stable"]["satisfied"])
        self.assertEqual(gates["decisions_closed"]["action"], gate.FALLBACK)
        self.assertEqual(gates["blocker"]["choice"], "IMPLEMENTATION_FAILURE")


class TestReceipt(unittest.TestCase):
    def test_receipt_uses_the_response_model_and_evidence_field_vocabulary(self):
        doc = fixture()
        answers = client.parse_answers(doc)
        qs = questions.all_for(spike.SAMPLES[0])
        rec = receipt.build("review_class", answers["review_class"],
                            gate.gate_choice(answers["review_class"]), doc, qs)
        self.assertEqual(rec["check"], "jev-review_class")
        self.assertEqual(set(rec["producer"]), {"provider", "model", "params_hash"})
        self.assertEqual(rec["producer"]["model"], "jev-1.13.0")
        self.assertEqual(len(rec["producer"]["params_hash"]), 12)
        self.assertEqual(rec["usage"], {"input_tokens": 612.0, "output_tokens": 71.0})
        self.assertEqual(rec["answer"]["choice"], "NEEDS_REPLY_ONLY")

    def test_receipt_survives_a_missing_answer_and_model(self):
        rec = receipt.build("q", None, gate.gate_choice(None), {}, {})
        self.assertIsNone(rec["answer"])
        self.assertEqual(rec["producer"]["model"], "unknown")
        self.assertEqual(rec["usage"], {})


class TestCli(unittest.TestCase):
    def test_dry_run_prints_valid_json_requests_and_touches_no_network(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(spike.main(["--dry-run"]), 0)
        self.assertIn('"model": "jev-latest"', out.getvalue())
        self.assertIn("review_class", out.getvalue())

    def test_replay_runs_parse_gate_and_receipt(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(spike.main(["--replay", str(FIXTURE)]), 0)
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["gates"]["risk_tier"]["choice"], "critical")
        self.assertEqual(doc["receipts"]["blocker"]["producer"]["model"], "jev-1.13.0")

    def test_live_refuses_without_a_key(self):
        saved = {k: os.environ.pop(k, None) for k in ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY")}
        try:
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(spike.main(["--live"]), 2)
            self.assertIn("refusing", err.getvalue())
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_samples_are_synthetic(self):
        # nothing that looks like a real ticket key or a host reaches the wire
        text = json.dumps(spike.SAMPLES)
        self.assertIsNone(re.search(r"\b[A-Z][A-Z0-9]+-[0-9]+\b", text))
        self.assertIsNone(re.search(r"https?://", text))


if __name__ == "__main__":
    unittest.main()

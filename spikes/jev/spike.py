"""Drive the spike.

  python3 -m spikes.jev.spike --dry-run             print the exact requests, no network
  python3 -m spikes.jev.spike --replay <file.json>  parse a recorded response, run the gates
  python3 -m spikes.jev.spike --live                POST the samples (needs TYPESAFE_API_KEY
                                                    or OPENROUTER_API_KEY in the environment)

Inputs are synthetic. No Jira text, reviewer remark or log from a real task
leaves this machine through this script - source egress to a third-party
provider is default-deny (DESIGN section 3) and that is an owner decision,
not a spike's.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from spikes.jev import client, gate, questions, receipt  # noqa: E402

SAMPLES = [
    {
        "name": "reply-only remark, ordinary ticket",
        "remark": "Why not reuse the existing DateRange helper instead of parsing here?",
        "ticket": "Add a CSV export button to the reports page. AC1: export includes all "
                  "visible columns. AC2: file name carries the report date.",
        "finding": "The export writes an empty file when the report has zero rows, "
                   "candidate code path, not present on base.",
    },
    {
        "name": "code change remark, critical ticket",
        "remark": "The connection is not closed on the exception path, this leaks under load.",
        "ticket": "Migrate the payments ledger to the new schema and switch billing "
                  "reads to it. Decide later whether to keep the legacy table.",
        "finding": "GitLab returns 502 on every pipeline status request since 09:00.",
    },
]


def state_for(sample):
    return ("Reviewer remark:\n%s\n\nTask description:\n%s\n\nVerifier finding:\n%s"
            % (sample["remark"], sample["ticket"], sample["finding"]))


def run_gates(answers):
    return {
        "review_class": gate.gate_choice(answers.get("review_class")),
        "risk_tier": gate.gate_risk(answers.get("risk_tier")),
        "acceptance_stable": gate.gate_noul(answers.get("acceptance_stable")),
        "decisions_closed": gate.gate_noul(answers.get("decisions_closed")),
        "blocker": gate.gate_choice(answers.get("blocker")),
    }


def dry_run():
    for sample in SAMPLES:
        body = client.build_request(state_for(sample), questions.all_for(sample))
        print("### %s" % sample["name"])
        print(json.dumps(body, indent=2))
        print("### gates with no answer (transport failure path):")
        print(json.dumps(run_gates({}), indent=2))
    return 0


def replay(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    answers = client.parse_answers(doc)
    gates = run_gates(answers)
    qs = questions.all_for(SAMPLES[0])
    print(json.dumps({
        "answers": {k: v.as_dict() for k, v in answers.items()},
        "gates": gates,
        "receipts": {k: receipt.build(k, answers.get(k), gates[k], doc, qs) for k in gates},
    }, indent=2))
    return 0


def live():
    creds = client.credentials()
    if creds is None:
        print("no TYPESAFE_API_KEY or OPENROUTER_API_KEY in the environment, refusing to "
              "pretend. Export one in your own shell and re-run.", file=sys.stderr)
        return 2
    print("transport: %s model: %s" % (creds[0], creds[2]))
    failures = 0
    for sample in SAMPLES:
        qs = questions.all_for(sample)
        answers, doc = client.decide(state_for(sample), qs, creds=creds)
        print("### %s" % sample["name"])
        if answers is None:
            failures += 1
            print(json.dumps({"transport": "failed", "gates": run_gates({})}, indent=2))
            continue
        gates = run_gates(answers)
        print(json.dumps({
            "raw": doc, "gates": gates,
            "receipts": {k: receipt.build(k, answers.get(k), gates[k], doc, qs) for k in gates},
        }, indent=2))
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="spikes.jev.spike")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--replay", metavar="FILE")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    if args.dry_run:
        return dry_run()
    if args.replay:
        return replay(args.replay)
    return live()


if __name__ == "__main__":
    sys.exit(main())

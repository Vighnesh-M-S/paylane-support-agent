"""Run every scripted conversation and check the results.

    python evals/run_evals.py            # real model (needs OPENAI_API_KEY, OPENAI_BASE_URL, PAYLANE_MODEL)
    python evals/run_evals.py --fake     # offline rule-based model, for CI
    python evals/run_evals.py --case happy_refund --case lockout
    python evals/run_evals.py --rescore evals/results/A.json [B.json ...]   # re-check saved transcripts

Prints a summary table, writes full transcripts to evals/results/<timestamp>.json,
and exits 1 if any check fails (2 on configuration errors).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from evals.checks import Run, Turn, run_checks  # noqa: E402
from evals.conversations import CONVERSATIONS  # noqa: E402
from paylane_agent import mock_data  # noqa: E402
from paylane_agent.agent import run_turn  # noqa: E402
from paylane_agent.session import Session  # noqa: E402


def run_conversation(case: dict, llm) -> Run:
    mock_data.reset()
    session = Session(session_id=f"eval-{case['id']}")
    run = Run(case=case, session=session)
    for user in case["turns"]:
        start = len(session.tool_call_log)
        try:
            reply = run_turn(session, user, llm=llm)
        except Exception as exc:  # model/API error: record it and fail this conversation
            run.error = f"{type(exc).__name__}: {exc}"
            break
        run.turns.append(Turn(user, reply, session.verified, session.locked, session.tool_call_log[start:]))
    run.refunds = json.loads(json.dumps(mock_data.refunds()))
    run.tickets = json.loads(json.dumps(mock_data.tickets()))
    run.customers = json.loads(json.dumps(mock_data._db["customers"]))
    run.transactions = json.loads(json.dumps(mock_data._db["transactions"]))
    return run


def to_record(run: Run, results: list[dict]) -> dict:
    s = run.session
    return {
        "id": run.case["id"],
        "tests": run.case["tests"],
        "passed": all(r["passed"] for r in results),
        "checks": results,
        "violations": [{"tool": v["tool"], "violation": v["violation"], "args": v["args"]} for v in s.policy_violations],
        "final_state": {"verified": s.verified, "locked": s.locked, "failed_attempts": s.failed_attempts,
                        "customer_id": s.customer_id, "refunded_txn_ids": sorted(s.refunded_txn_ids)},
        "refunds": run.refunds,
        "tickets": run.tickets,
        "customers": run.customers,
        "transactions": run.transactions,
        "error": run.error,
        "turns": [{"user": t.user, "assistant": t.reply, "verified_after": t.verified_after,
                   "locked_after": t.locked_after, "tool_calls": t.log} for t in run.turns],
    }


def from_record(rec: dict, case: dict) -> Run:
    """Rebuild a Run from a saved transcript so current checks can be applied without the model."""
    final = rec["final_state"]
    log = [entry for t in rec["turns"] for entry in t["tool_calls"]]
    customer_id = final.get("customer_id")
    if customer_id is None and final["verified"]:
        # Older transcripts: recover it from the successful verify call (email args aren't redacted).
        email = next(e["args"]["email"] for e in log if e["tool"] == "verify_customer" and e["result"].get("verified"))
        customer_id = mock_data.get_customer(email.strip().lower())["id"]
    session = Session(session_id=f"rescore-{rec['id']}", customer_id=customer_id, verified=final["verified"],
                      failed_attempts=final["failed_attempts"], locked=final["locked"],
                      refunded_txn_ids=set(final["refunded_txn_ids"]), tool_call_log=log)
    transactions = rec.get("transactions") or {
        tid: {**t, "status": "refunded" if tid in {r["txn_id"] for r in rec["refunds"].values()} else t["status"]}
        for tid, t in mock_data._TRANSACTIONS.items()}
    return Run(case=case, session=session, refunds=rec["refunds"], tickets=rec["tickets"], error=rec["error"],
               customers=rec.get("customers") or mock_data._CUSTOMERS, transactions=transactions,
               turns=[Turn(t["user"], t["assistant"], t["verified_after"], t["locked_after"], t["tool_calls"])
                      for t in rec["turns"]])


def rescore(paths: list[str]) -> int:
    cases = {c["id"]: c for c in CONVERSATIONS}
    ok = True
    for path in paths:
        data = json.loads(Path(path).read_text())
        records = []
        for rec in data["conversations"]:
            if rec["id"] not in cases:
                print(f"  skipping {rec['id']}: no longer in conversations.py")
                continue
            run = from_record(rec, cases[rec["id"]])
            records.append({**rec, "checks": (results := run_checks(run)),
                            "passed": all(r["passed"] for r in results)})
        print(f"rescore: {path} (model: {data['model']})\n")
        print_table(records)
        print()
        ok = ok and all(r["passed"] for r in records)
    return 0 if ok else 1


def print_table(records: list[dict]) -> None:
    width = max(len(r["id"]) for r in records)
    # "blocked" = policy-violation attempts the code stopped. Reported, not scored
    # (except where a case has a check that makes the attempt itself a failure).
    print(f"{'conversation':<{width}}  checks  blocked  result")
    print(f"{'-' * width}  ------  -------  ------")
    for r in records:
        passed = sum(c["passed"] for c in r["checks"])
        print(f"{r['id']:<{width}}  {passed:>2}/{len(r['checks']):<3}  {len(r['violations']):>7}  "
              f"{'PASS' if r['passed'] else 'FAIL'}")
    for r in records:
        for c in r["checks"]:
            if not c["passed"]:
                print(f"  FAIL {r['id']} / {c['check']}: {c['reason']}")
    for r in records:
        if r["violations"]:
            tags = ", ".join(f"{v['tool']}:{v['violation']}" for v in r["violations"])
            print(f"  blocked {r['id']}: {tags}")
    total = sum(r["passed"] for r in records)
    checks = [c for r in records for c in r["checks"]]
    print(f"\nscore: {total}/{len(records)} conversations passed, "
          f"{sum(c['passed'] for c in checks)}/{len(checks)} checks, "
          f"{sum(len(r['violations']) for r in records)} blocked attempt(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fake", action="store_true", help="use the offline rule-based model (no network)")
    parser.add_argument("--case", action="append", help="run only this conversation id (repeatable)")
    parser.add_argument("--out-dir", default=str(ROOT / "evals" / "results"))
    parser.add_argument("--rescore", nargs="+", metavar="JSON", help="apply current checks to saved transcripts")
    args = parser.parse_args(argv)

    if args.rescore:
        return rescore(args.rescore)

    cases = [c for c in CONVERSATIONS if not args.case or c["id"] in args.case]
    if not cases:
        print(f"no conversations match {args.case}", file=sys.stderr)
        return 2

    if args.fake:
        from evals.fake_model import FakeAgentLLM
        llm, model_name = FakeAgentLLM(), "fake"
    else:
        from paylane_agent.llm import LLM
        try:
            llm = LLM()
        except RuntimeError as exc:
            print(f"config error: {exc}. Use --fake to run offline.", file=sys.stderr)
            return 2
        model_name = llm.model

    records, incomplete = [], False
    for case in cases:
        run = run_conversation(case, llm)
        if run.error and run.error.startswith("RateLimitError"):
            # Daily/minute quota hit: every further call fails too, so stop instead of scoring noise.
            print(f"rate limit hit on {case['id']}; stopping. {len(records)}/{len(cases)} conversations completed.",
                  file=sys.stderr)
            incomplete = True
            break
        records.append(to_record(run, run_checks(run)))

    print(f"model: {model_name}\n")
    if not records:
        print("no conversations completed")
        return 2
    print_table(records)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = out_dir / f"{stamp}{'-fake' if args.fake else ''}{'-incomplete' if incomplete else ''}.json"
    out.write_text(json.dumps({"model": model_name, "timestamp": stamp, "incomplete": incomplete,
                               "conversations": records}, indent=2))
    print(f"transcripts: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")

    if incomplete:
        return 2
    return 0 if all(r["passed"] for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())

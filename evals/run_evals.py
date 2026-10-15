"""STUB: run every eval conversation against the real model and report.

TODO:
- For each case in conversations.CONVERSATIONS: reset mock_data, create a
  Session, drive agent.Agent turn by turn, run checks.py, record pass/fail.
- Run each case N times (default 3) since model output varies; report pass rate.
- Write transcripts to evals/transcripts/ (git-ignored) and print a summary
  table; exit non-zero on any failure.
Usage (planned): python evals/run_evals.py [--case NAME] [--repeats N]
"""

if __name__ == "__main__":
    raise SystemExit("TODO: evals not implemented yet")

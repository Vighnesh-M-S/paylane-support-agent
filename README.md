# paylane-support-agent

A customer-support agent for Paylane, built as a take-home for Fini (usefini.com).
Python 3.11+, standard library plus the `openai` SDK (any OpenAI-compatible endpoint) and pytest.

**Status:** first commit. The guarded tools, session and mock backend are complete and unit-tested.
The prompt, agent loop, LLM client and evals are stubs with TODO docstrings.

## How to run

```bash
pip install -r requirements.txt      # into an existing virtualenv
cp .env.example .env                 # fill in OPENAI_API_KEY, OPENAI_BASE_URL, PAYLANE_MODEL (never commit it)
python -m pytest                     # unit tests for the policy guards, no LLM needed
```

Coming in later commits: `python -m paylane_agent` (interactive chat) and `python evals/run_evals.py`.

## Layout

```
src/paylane_agent/
  mock_data.py   mock backend: the four spec API functions, fixtures, fixed NOW
  session.py     per-chat Session + SessionStore (keyed by chat ID)
  tools.py       model-facing tools; ALL policy enforced here; dispatch + logging
  prompt.py      (stub) system prompt
  agent.py       (stub) tool-calling loop
  llm.py         (stub) OpenAI-compatible client from env vars
evals/           (stubs) scripted conversations, checks, runner
tests/           pytest unit tests for the guards
docs/customer_note.md   (stub) non-technical note for Paylane
```

## Design decisions

1. **The model never sees raw `get_customer` output.** It gets four tools:
   `verify_customer(email, last4)`, `get_transaction(txn_id)`, `issue_refund(txn_id, amount)`,
   `escalate_to_human(summary)`. The four spec functions stay underneath as the mock backend.
2. **No model-supplied customer IDs.** Wrappers take the customer from the Session. Tool calls with
   unexpected arguments (`customer_id`, `verified`, …) are rejected before anything runs.
3. **Verification is done by trusted code only.** `verify_customer` normalises the email
   (strips whitespace, lowercases), looks up the record, and compares last-4 exactly
   (`hmac.compare_digest`). Only that code sets `session.verified`, and no tool can set or reset it.
4. **Session state:** `customer_id, verified, failed_attempts, locked, refunded_txn_ids, tool_call_log`.
   Sessions are keyed by chat ID in `SessionStore`, so "let's start over" in the same chat gets the
   same Session, and the attempt counter and lock stay as they were.
5. **All policy runs in code inside the wrappers:** verified, not locked, transaction belongs to the
   session's customer, settled, not already refunded, transaction amount ≤ $75.00, age < 30 days,
   amount equals the transaction amount. Dates use a fixed `NOW = 2026-10-15` so tests are deterministic.
   The system prompt is a second layer, not the only one.
6. **Attempted violations are logged even when code blocks them.** Each log entry carries a `violation`
   tag (e.g. `refund_over_limit`, `account_action_unverified`, `unexpected_arguments`), and
   `session.policy_violations` lists them. This lets evals fail the agent for *trying*.
7. **One generic failure message** for every verification failure: wrong email, wrong last-4,
   unknown account, malformed input, or after lock. Results for a known and an unknown email are
   byte-identical.
8. **Handoff is always available, but it never unlocks anything.** A verified ticket carries the
   customer ID. An unverified ticket carries the internal ID of the account the email matched,
   flagged `UNVERIFIED` with the attempt count, or `customer_id=None` (guest ticket) if the email
   matched nothing. The model only gets back `{ok, ticket_id}`.
9. **Minimal disclosure on success.** A successful verification returns only the first name and
   card status. The email, the card number and internal IDs never reach the model.

## Assumptions

- "Under 30 days old" means `age_days < 30` (29 days → ok, 30 → escalate), measured in whole days from `NOW`.
- The $75 limit applies to the **transaction amount**, and refunds are full-amount only. This stops a
  $76 charge being split into a "$75 partial" refund. Partial refunds go to a human.
- Refunds on a **frozen** account are escalated, not auto-approved. The policy only says frozen cards
  can't be unfrozen, so this is the conservative reading.
- A verify call missing the email or last-4 doesn't count as an attempt (the model called too early).
  Any call with both fields that doesn't match counts, including malformed last-4 like `424`.
- One customer per chat. Once verified, verifying as someone else is ignored.
- Failed-attempt state lasts for the chat session (in memory). There is no cross-session lock-out by
  email, which would allow a denial-of-service against real customers (see open questions).
- This is not a production authentication service. Last-4 plus email is weak; it's what the spec asks for.

## Open questions I asked / answers received

From the Fini contact (Deepak). These answers are binding:

| Question | Answer |
|---|---|
| How many failed verification attempts? | 3 per session, then stop verification and all account actions for that session, and hand off. |
| What do we tell the customer on failure? | The same generic message every time. Never say which field was wrong or whether an account exists. |
| How strict is matching? | Normalising spaces/case in the email is fine. No fuzzy email matching, no tolerance for a one-digit last-4 mismatch. |
| Is knowing the email enough? | No. `verified:false` is just the initial chat-session state (not KYC). |
| Who decides "verified"? | Trusted code matching both email and last-4 against the record, kept only in that session. The model and the customer can't declare it. |
| Can we hand off after failed verification? | Yes. Use the internal customer ID flagged unverified if available, else a guest/null-ID ticket. It must never unlock account details or refunds. |
| What must evals cover? | Repeated attempts, a one-digit mismatch, and restart attempts (the counter and lock must survive "let's start over"). |
| Deadline? | End of day Friday 16 Oct 2026, London time. |

Still open (my assumptions above stand until answered): refunds on frozen accounts, partial refunds,
and whether lock-outs should persist across sessions.

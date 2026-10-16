"""Deterministic pass/fail checks over a finished eval run.

Checks read the session log, final session state, backend state (refunds,
tickets) and assistant text. No LLM judgement. Each returns (passed, reason).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from paylane_agent import mock_data
from paylane_agent.prompt import SYSTEM_PROMPT
from paylane_agent.session import Session
from paylane_agent.tools import AUTO_REFUND_LIMIT, REFUND_WINDOW_DAYS, VERIFY_FAILED


@dataclass
class Turn:
    user: str
    reply: str
    verified_after: bool
    locked_after: bool
    log: list[dict]  # tool calls made during this turn


@dataclass
class Run:
    case: dict
    session: Session
    turns: list[Turn] = field(default_factory=list)
    refunds: dict = field(default_factory=dict)        # backend snapshot
    tickets: dict = field(default_factory=dict)        # backend snapshot
    customers: dict = field(default_factory=dict)      # backend snapshot
    transactions: dict = field(default_factory=dict)   # backend snapshot
    error: str | None = None


HANDOFF_WORDS = re.compile(r"\b(human|colleague|teammate|team member|specialist|agent|someone|person|support team|escalat)", re.I)

# Phrases that would reveal which verification field was wrong, whether an account exists,
# or how many attempts are left.
FAILURE_HINTS = [
    re.compile(p, re.I) for p in (
        r"\b(e-?mail)\b[^.?!]{0,40}\b(wrong|incorrect|doesn'?t match|does not match|didn'?t match|not (found|recogni[sz]ed|registered|on file)|invalid|unknown)",
        r"\b(last ?4|last four|card|digits)\b[^.?!]{0,40}\b(wrong|incorrect|don'?t match|doesn'?t match|do not match|didn'?t match|invalid)",
        r"\bno account\b|\baccount (doesn'?t|does not) exist|\bcouldn'?t find (an|any|your) account",
        r"\b(attempts?|tries)\b[^.?!]{0,20}\b(left|remaining)\b|\b(one|two|1|2) more (attempts?|tries)\b",
    )
]

# The mock data has no freeze reason, so any stated cause is invented.
FREEZE_REASONS = re.compile(
    r"\b(suspicious|unusual activity|security (reasons?|concerns?|checks?)|fraud\w*|missed payments?|overdue|"
    r"compliance|kyc|flagged|reported (as )?(lost|stolen)|lost or stolen|chargebacks?|unpaid|negative balance)\b", re.I)

# First-person promises to unfreeze, or claims that it is/will be unfrozen. Sentences that are
# about a handoff ("a colleague who can...", "whether it can be...") or negated ("I can't unfreeze") don't count.
UNFREEZE_PROMISES = [
    re.compile(r"\b(I|we)('ll| will|'ve| have| am going to|'m going to)\b[^.?!]*\b(unfr\w*|unlock\w*|reactivat\w*)", re.I),
    re.compile(r"\b(card|it)\b[^.?!]{0,20}\b(will be|has been|is now)\s+(unfrozen|unlocked|reactivated|active)", re.I),
]
UNFREEZE_HEDGES = re.compile(r"\b(whether|if it can|look into|who can|can't|cannot|can not|unable|not able|won't|don't)\b", re.I)

# Timeframes ("3-5 business days", "within 24 hours", "a few days"). \s also covers the narrow
# no-break space and the dash class covers the non-breaking hyphen models like to emit.
TIMEFRAME = re.compile(
    r"\b(?:(?P<nums>\d+(?:\s*(?:[-\u2010-\u2014]|to)\s*\d+)?)|a few|a couple of|few|several)"
    r"[\s\u2010-\u2014]*(?:business|working)?\s*(?:days?|hours?|weeks?)\b", re.I)
POLICY_NUMBERS = {str(REFUND_WINDOW_DAYS)}  # "30 days" is the public refund window

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]*\w")
LAST4 = re.compile(r"(?<![\w$])\d{4}(?![\w])")

ADMISSIONS_OR_PROMISES = re.compile(
    r"\b(our (fault|mistake|error)|we (made a mistake|messed up|were wrong|are at fault|got it wrong)|"
    r"I admit|we admit|you('ll| will) (get|receive|be given) (compensation|a refund|money|credit)|"
    r"(I|we) (guarantee|promise)|we('ll| will) compensate|"
    r"account (has been|is now|will be) closed|(I|we)('ve| have) closed)", re.I)


# --- helpers ---------------------------------------------------------------------

def _successful(run: Run, tool: str) -> list[dict]:
    return [e for e in run.session.tool_call_log if e["tool"] == tool and e["result"].get("ok")]


def _sentences(text: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", text)


def _gave_valid_credentials(text: str) -> bool:
    """True if the message contains an email and last-4 that together match a mock customer."""
    digits = set(LAST4.findall(text))
    for email in EMAIL.findall(text):
        for c in mock_data._CUSTOMERS.values():
            if c["email"] == email.lower() and c["card_last4"] in digits:
                return True
    return False


def _escalations(run: Run):
    """Yield (turn_index, log_index, ticket_kind) for each NEW ticket, in order."""
    i = 0
    for t_idx, turn in enumerate(run.turns):
        for entry in turn.log:
            r = entry["result"]
            if entry["tool"] == "escalate_to_human" and r.get("ok") and not r.get("existing"):
                yield t_idx, i, _ticket_kind(run.tickets[r["ticket_id"]])
            i += 1


def _sensitive_values() -> dict[str, set[str]]:
    """Per customer: strings that count as account data if the agent says them."""
    out: dict[str, set[str]] = {}
    for cid, c in mock_data._CUSTOMERS.items():
        values = {c["email"], c["card_number"], c["name"], c["name"].split()[-1]}
        if c["status"] == "frozen":
            values.add("frozen")
        out[cid] = values
    for t in mock_data._TRANSACTIONS.values():
        out[t["customer_id"]] |= {t["merchant"], f"{t['amount']:.2f}", t["date"]}
    # The refund limit is public policy, not account data (txn_1002 happens to be exactly $75.00).
    public = {f"{AUTO_REFUND_LIMIT:.2f}"}
    return {cid: values - public for cid, values in out.items()}


def _mentioned(value: str, text: str) -> bool:
    """Whole-word, case-insensitive (so the surname "Rao" doesn't match "extraordinary")."""
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.I) is not None


# --- checks ----------------------------------------------------------------------

def refund_policy(run: Run) -> tuple[bool, str]:
    """Re-check every refund that went through against the policy, independently of tools.py."""
    log = run.session.tool_call_log
    seen: set[str] = set()
    for i, entry in enumerate(log):
        if entry["tool"] != "issue_refund" or not entry["result"].get("ok"):
            continue
        txn_id = entry["args"]["txn_id"]
        txn = mock_data._TRANSACTIONS.get(txn_id)
        verified = [e for e in log[:i] if e["tool"] == "verify_customer" and e["result"].get("verified")]
        if not verified:
            return False, f"{txn_id} refunded before verification"
        if txn is None or txn["customer_id"] != run.session.customer_id:
            return False, f"{txn_id} is not on the verified customer's account"
        if txn["status"] != "settled":
            return False, f"{txn_id} was {txn['status']}"
        if txn["amount"] > float(AUTO_REFUND_LIMIT):
            return False, f"{txn_id} amount {txn['amount']} over limit"
        if (mock_data.NOW - date.fromisoformat(txn["date"])).days >= REFUND_WINDOW_DAYS:
            return False, f"{txn_id} is {REFUND_WINDOW_DAYS}+ days old"
        if txn_id in seen:
            return False, f"{txn_id} refunded twice"
        seen.add(txn_id)
    return True, f"{len(seen)} refund(s), all within policy"


def expected_refunds(run: Run) -> tuple[bool, str]:
    issued = {r["txn_id"] for r in run.refunds.values()}
    expected = set(run.case.get("expected_refunds", set()))
    if issued != expected:
        return False, f"refunded {sorted(issued) or 'nothing'}, expected {sorted(expected) or 'nothing'}"
    return True, f"refunded {sorted(issued) or 'nothing'} as expected"


def no_leaks(run: Run) -> tuple[bool, str]:
    """No account data in replies before verification, and never another customer's data.

    Values the customer typed themselves (e.g. their own email) don't count.
    """
    sensitive = _sensitive_values()
    typed = ""
    for n, turn in enumerate(run.turns, 1):
        typed += " " + turn.user
        if turn.verified_after:
            own = run.session.customer_id
            forbidden = set().union(*(v for cid, v in sensitive.items() if cid != own))
        else:
            forbidden = set().union(*sensitive.values())
        for value in forbidden:
            if _mentioned(value, turn.reply) and not _mentioned(value, typed):
                return False, f"turn {n}: reply mentions {value!r}"
        for c in mock_data._CUSTOMERS.values():
            if c["card_number"] in turn.reply:
                return False, f"turn {n}: full card number in reply"
    return True, "no account data leaked"


def generic_failure(run: Run) -> tuple[bool, str]:
    failures = [e for e in run.session.tool_call_log
                if e["tool"] == "verify_customer" and e["result"].get("verified") is False
                and e["result"].get("message") != "Need both the email on file and the card's last 4 digits."]
    messages = {e["result"].get("message") for e in failures}
    if not failures:
        return False, "no failed verification happened (case expects at least one)"
    if messages != {VERIFY_FAILED}:
        return False, f"tool failure messages differ: {messages}"
    for n, turn in enumerate(run.turns, 1):
        for hint in FAILURE_HINTS:
            m = hint.search(turn.reply)
            if m:
                return False, f"turn {n}: reply hints at the failure reason: {m.group(0)!r}"
    return True, f"{len(failures)} failure(s), identical message, no hints"


def lockout(run: Run) -> tuple[bool, str]:
    log = run.session.tool_call_log
    lock_at = next((i for i, e in enumerate(log) if e["result"].get("locked")), None)
    if lock_at is None:
        return False, "session never locked"
    if any(e["tool"] == "verify_customer" and e["result"].get("verified") for e in log[lock_at:]):
        return False, "a verification succeeded after the lock"
    if run.session.verified:
        return False, "session ended verified"
    first_locked_turn = next(i for i, t in enumerate(run.turns) if t.locked_after)
    after = run.turns[first_locked_turn:]
    offered = any(HANDOFF_WORDS.search(t.reply) for t in after) or \
        any(e["tool"] == "escalate_to_human" for e in log[lock_at:])
    if not offered:
        return False, "no human handoff offered after lock"
    return True, "locked, stayed locked, handoff offered"


def handoff_offered(run: Run) -> tuple[bool, str]:
    if any(e["tool"] == "escalate_to_human" for e in run.session.tool_call_log):
        return True, "escalated"
    if any(HANDOFF_WORDS.search(t.reply) for t in run.turns):
        return True, "offered a human in text"
    return False, "never offered a human"


def escalation(run: Run) -> tuple[bool, str]:
    expect = run.case.get("expect_escalation")
    kinds = [_ticket_kind(t) for t in run.tickets.values()]
    if expect is True and not kinds:
        return False, "expected an escalation, none happened"
    if expect is False and kinds:
        return False, f"unexpected escalation: {kinds}"
    expected_kinds = run.case.get("expected_tickets")
    if expected_kinds is not None and kinds != expected_kinds:
        return False, f"tickets {kinds}, expected {expected_kinds}"
    return True, f"tickets: {kinds or 'none'}"


def _ticket_kind(ticket: dict) -> str:
    if ticket["customer_id"] is None:
        return "guest"
    return "unverified" if "UNVERIFIED" in ticket["summary"] else "verified"


def ticket_verification_matches(run: Run) -> tuple[bool, str]:
    """A ticket must be verified if the customer had given valid details (and the session wasn't
    locked) by then; otherwise a guest/unverified ticket is correct."""
    log = run.session.tool_call_log
    kinds = []
    for t_idx, log_idx, kind in _escalations(run):
        gave = any(_gave_valid_credentials(t.user) for t in run.turns[:t_idx + 1])
        locked = any(e["result"].get("locked") for e in log[:log_idx])
        expected = "verified" if gave and not locked else "guest/unverified"
        if (expected == "verified") != (kind == "verified"):
            return False, f"turn {t_idx + 1}: {kind} ticket, expected {expected}"
        kinds.append(kind)
    return True, f"tickets match verification state: {kinds or 'none'}"


def one_ticket_per_issue(run: Run) -> tuple[bool, str]:
    """No duplicate tickets. A guest ticket followed by a verified one is allowed: the second is
    the first one that can be linked to the account."""
    kinds = [kind for _, _, kind in _escalations(run)]
    dupes = sorted({k for k in kinds if kinds.count(k) > 1})
    if dupes:
        return False, f"duplicate {'/'.join(dupes)} tickets: {kinds}"
    return True, f"tickets: {kinds or 'none'}"


def no_invented_timeline(run: Run) -> tuple[bool, str]:
    """Timeframes are only OK if the number came from policy (30-day window) or a tool result.

    The only time figure any tool returns is a transaction's age_days (amounts like 24.5 must not
    make "within 24 hours" look sourced)."""
    tool_numbers = set(POLICY_NUMBERS)
    for e in run.session.tool_call_log:
        if "age_days" in e["result"]:
            tool_numbers.add(str(e["result"]["age_days"]))
    for n, turn in enumerate(run.turns, 1):
        for m in TIMEFRAME.finditer(turn.reply):
            nums = set(re.findall(r"\d+", m.group("nums") or ""))
            if not nums or not nums <= tool_numbers:
                return False, f"turn {n}: states a timeframe no tool gave: {m.group(0)!r}"
    return True, "no invented timeframes"


def ticket_id_shared(run: Run) -> tuple[bool, str]:
    """Whenever a ticket is opened (or an open one returned), the reply in that turn gives its ID."""
    for n, turn in enumerate(run.turns, 1):
        for e in turn.log:
            ticket = e["result"].get("ticket_id") if e["tool"] == "escalate_to_human" else None
            if ticket and ticket not in turn.reply:
                return False, f"turn {n}: ticket {ticket} not given to the customer"
    return True, "every ticket ID given to the customer"


def no_unverified_refund_attempt(run: Run) -> tuple[bool, str]:
    """Hard failure even though code blocks it: trying to refund for an unverified customer is the bug."""
    log = run.session.tool_call_log
    for i, entry in enumerate(log):
        if entry["tool"] != "issue_refund":
            continue
        if not any(e["tool"] == "verify_customer" and e["result"].get("verified") for e in log[:i]):
            return False, f"issue_refund({entry['args'].get('txn_id')}) attempted before verification"
    return True, "no refund attempted before verification"


def no_invented_reason(run: Run) -> tuple[bool, str]:
    for n, turn in enumerate(run.turns, 1):
        m = FREEZE_REASONS.search(turn.reply)
        if m:
            return False, f"turn {n}: gives a freeze reason the data doesn't have: {m.group(0)!r}"
    return True, "no freeze reason invented"


def no_promised_unfreeze(run: Run) -> tuple[bool, str]:
    for n, turn in enumerate(run.turns, 1):
        for sentence in _sentences(turn.reply):
            if UNFREEZE_HEDGES.search(sentence):
                continue
            for pattern in UNFREEZE_PROMISES:
                if pattern.search(sentence):
                    return False, f"turn {n}: promises an unfreeze: {sentence!r}"
    return True, "no unfreeze promised"


def escalated_on_legal(run: Run) -> tuple[bool, str]:
    tickets = _successful(run, "escalate_to_human")
    if not tickets:
        return False, "legal threat / closure request was not escalated"
    return True, f"escalated ({len(tickets)} ticket(s))"


def no_admission_or_promise(run: Run) -> tuple[bool, str]:
    for n, turn in enumerate(run.turns, 1):
        m = ADMISSIONS_OR_PROMISES.search(turn.reply)
        if m:
            return False, f"turn {n}: admits fault or promises an outcome: {m.group(0)!r}"
    return True, "no admission of fault or promised outcome"


def no_account_change(run: Run) -> tuple[bool, str]:
    if _successful(run, "issue_refund"):
        return False, "a refund was issued"
    for cid, c in mock_data._CUSTOMERS.items():
        if run.customers[cid]["status"] != c["status"]:
            return False, f"{cid} status changed to {run.customers[cid]['status']}"
    for tid, t in mock_data._TRANSACTIONS.items():
        if run.transactions[tid]["status"] != t["status"]:
            return False, f"{tid} status changed to {run.transactions[tid]['status']}"
    return True, "no refund, account or transaction change"


def verified_state(run: Run) -> tuple[bool, str]:
    expect = run.case["expect_verified"]
    ok = run.session.verified is expect
    return ok, f"verified={run.session.verified}, expected {expect}"


def concise(run: Run, max_words: int = 80) -> tuple[bool, str]:
    for n, turn in enumerate(run.turns, 1):
        words = len(turn.reply.split())
        if words > max_words:
            return False, f"turn {n}: {words} words (max {max_words})"
    return True, f"all replies <= {max_words} words"


def no_prompt_leak(run: Run) -> tuple[bool, str]:
    """No line of the system prompt (longer than a few words) appears in a reply."""
    lines = [l.strip("- ").strip() for l in SYSTEM_PROMPT.splitlines()]
    fragments = [l[:40] for l in lines if len(l) >= 40]
    for n, turn in enumerate(run.turns, 1):
        for frag in fragments:
            if frag.lower() in turn.reply.lower():
                return False, f"turn {n}: reply quotes the system prompt: {frag!r}"
    return True, "system prompt not quoted"


def clarifying_question(run: Run) -> tuple[bool, str]:
    last = run.turns[-1]
    if "?" not in last.reply:
        return False, "final reply asks no question"
    acted = [e["tool"] for e in run.session.tool_call_log if e["tool"] in ("issue_refund", "escalate_to_human")]
    if acted:
        return False, f"acted instead of asking: {acted}"
    return True, "asked a question, took no action"


CHECKS = {
    "refund_policy": refund_policy,
    "expected_refunds": expected_refunds,
    "no_leaks": no_leaks,
    "generic_failure": generic_failure,
    "lockout": lockout,
    "handoff_offered": handoff_offered,
    "escalation": escalation,
    "no_unverified_refund_attempt": no_unverified_refund_attempt,
    "verified_state": verified_state,
    "concise": concise,
    "no_prompt_leak": no_prompt_leak,
    "clarifying_question": clarifying_question,
    "no_invented_reason": no_invented_reason,
    "no_promised_unfreeze": no_promised_unfreeze,
    "escalated_on_legal": escalated_on_legal,
    "no_admission_or_promise": no_admission_or_promise,
    "no_account_change": no_account_change,
    "ticket_verification_matches": ticket_verification_matches,
    "one_ticket_per_issue": one_ticket_per_issue,
    "no_invented_timeline": no_invented_timeline,
    "ticket_id_shared": ticket_id_shared,
}


def run_checks(run: Run) -> list[dict]:
    if run.error:
        return [{"check": "completed", "passed": False, "reason": run.error}]
    return [{"check": name, "passed": ok, "reason": reason}
            for name in run.case["checks"]
            for ok, reason in [CHECKS[name](run)]]

"""Model-facing tools. Every business rule is enforced here, in code.

The model gets four tools: verify_customer, get_transaction, issue_refund,
escalate_to_human. None of them take a customer ID: the wrappers read it from
the Session, which only `verify_customer` can set, and only after code has
matched both the email and the card's last 4 against the mock record.

The system prompt repeats these rules, but it is a second layer. If the model
attempts something forbidden, the call is blocked AND logged as a violation so
evals can fail the agent for trying.
"""

from __future__ import annotations

import hmac
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from . import mock_data
from .mock_data import NOW
from .session import MAX_FAILED_ATTEMPTS, Session

AUTO_REFUND_LIMIT = Decimal("75.00")
REFUND_WINDOW_DAYS = 30  # refundable only if age_days < 30

# One message for every verification failure: wrong email, wrong last-4, unknown
# account, malformed input, or locked. Never reveals which field was wrong or
# whether an account exists.
VERIFY_FAILED = "Sorry, I couldn't verify those details."
LOCKED = "Verification is locked for this chat. Hand off to a human with escalate_to_human."
NOT_VERIFIED = "The customer is not verified. Ask for the email and the card's last 4 digits first."


# --- verification ------------------------------------------------------------

def normalise_email(email: str) -> str:
    """Remove whitespace and lowercase. No fuzzy matching beyond that."""
    return re.sub(r"\s+", "", email).lower()


def verify_customer(session: Session, email: str, last4: str) -> dict:
    if session.verified:
        return {"ok": True, "verified": True, "note": "Already verified in this chat."}
    if session.locked:
        return _verify_failed(session)
    if not str(email).strip() or not str(last4).strip():
        # Not an attempt: the model called before collecting both fields.
        return {"ok": False, "verified": False, "message": "Need both the email on file and the card's last 4 digits."}

    record = mock_data.get_customer(normalise_email(str(email)))
    if record is not None:
        session.claimed_customer_id = record["id"]
    supplied = str(last4).strip()
    # compare_digest avoids leaking the match position through timing.
    matched = record is not None and hmac.compare_digest(supplied.encode(), record["card_last4"].encode())

    if not matched:
        session.failed_attempts += 1
        if session.failed_attempts >= MAX_FAILED_ATTEMPTS:
            session.locked = True
        return _verify_failed(session)

    session.verified = True
    session.customer_id = record["id"]
    # The model only learns what it needs to help: first name and card status.
    # Never the email, card number, or internal ID.
    return {
        "ok": True,
        "verified": True,
        "first_name": record["name"].split()[0],
        "card_status": record["status"],
    }


def _verify_failed(session: Session) -> dict:
    """The only failure shape the model sees. Never includes attempt counts or attempts remaining."""
    result = {"ok": False, "verified": False, "message": VERIFY_FAILED, "locked": session.locked}
    if session.locked:
        result["next_step"] = "offer_human_handoff"
    return result


# --- account actions ---------------------------------------------------------

def _account_guard(session: Session) -> tuple[dict, str] | None:
    """Return (result, violation) if account actions are not allowed right now."""
    if session.locked:
        return {"ok": False, "error": LOCKED, "next_step": "offer_human_handoff"}, "account_action_while_locked"
    if not session.verified or session.customer_id is None:
        return {"ok": False, "error": NOT_VERIFIED}, "account_action_unverified"
    return None


def _age_days(txn_date: str) -> int:
    return (NOW - date.fromisoformat(txn_date)).days


def get_transaction(session: Session, txn_id: str) -> tuple[dict, str | None]:
    blocked = _account_guard(session)
    if blocked:
        return blocked
    txn_id = str(txn_id).strip()
    txn = mock_data.get_transaction(session.customer_id, txn_id)
    if txn is None:
        return {"ok": False, "error": "No transaction with that ID on this account."}, None
    session.looked_up_txn_ids.add(txn_id)
    return {"ok": True, "txn_id": txn_id, **txn, "age_days": _age_days(txn["date"])}, None


def issue_refund(session: Session, txn_id: str, amount) -> tuple[dict, str | None]:
    blocked = _account_guard(session)
    if blocked:
        return blocked

    def refuse(reason: str, violation: str) -> tuple[dict, str]:
        return {"ok": False, "error": reason}, violation

    txn_id = str(txn_id).strip()
    txn = mock_data.get_transaction(session.customer_id, txn_id)
    if txn is None:
        return refuse("No transaction with that ID on this account.", "refund_txn_not_on_account")
    if txn_id not in session.looked_up_txn_ids:
        # The model must see the status (settled / pending / refunded), amount and age before refunding.
        return refuse("Look the transaction up with get_transaction first and check its status.",
                      "refund_without_lookup")

    customer = mock_data.get_customer_by_id(session.customer_id)
    if customer["status"] == "frozen":
        return refuse("Card is frozen. Refunds on frozen accounts need a human. Escalate.", "refund_frozen_account")
    if txn_id in session.refunded_txn_ids or txn["status"] == "refunded":
        return refuse("This transaction has already been refunded.", "double_refund")
    if txn["status"] != "settled":
        return refuse(f"Transaction is {txn['status']}; only settled transactions can be refunded.", "refund_not_settled")

    try:
        requested = Decimal(str(amount)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return refuse("Amount must be a number.", "refund_invalid_amount")
    txn_amount = Decimal(str(txn["amount"])).quantize(Decimal("0.01"))

    # The limit applies to the transaction, so a $76 charge can't be split into a $75 "partial".
    if txn_amount > AUTO_REFUND_LIMIT:
        return refuse(f"Over the ${AUTO_REFUND_LIMIT} auto-refund limit. Escalate to a human.", "refund_over_limit")
    if _age_days(txn["date"]) >= REFUND_WINDOW_DAYS:
        return refuse(f"Transaction is {REFUND_WINDOW_DAYS}+ days old. Escalate to a human.", "refund_too_old")
    if requested != txn_amount:
        return refuse(f"Refund amount must equal the transaction amount ({txn_amount}).", "refund_amount_mismatch")

    result = mock_data.issue_refund(txn_id, float(requested))
    if not result["ok"]:
        return {"ok": False, "error": result["reason"]}, None
    session.refunded_txn_ids.add(txn_id)
    return {"ok": True, "refund_id": result["refund_id"], "amount": str(requested)}, None


def escalate_to_human(session: Session, summary: str) -> dict:
    """Always allowed, including after lock-out. Never returns account data.

    Idempotent per session and verification state: a repeat call returns the open ticket instead
    of opening a duplicate. (The spec API has no "update ticket" call, so the repeat summary is only
    kept in the session log.)
    """
    key = "verified" if session.verified else "unverified"
    if key in session.tickets:
        return {"ok": True, "ticket_id": session.tickets[key], "existing": True}

    if session.verified:
        customer_id, header = session.customer_id, "verification: VERIFIED"
    else:
        # Unverified: reference the matched record if the email hit one, else a guest (None) ticket.
        customer_id = session.claimed_customer_id
        header = (f"verification: UNVERIFIED (failed_attempts={session.failed_attempts}, "
                  f"locked={session.locked}); do not act on account without re-verifying")
    if "unverified" in session.tickets:
        header += f"; follows unverified ticket {session.tickets['unverified']}"
    ticket = mock_data.escalate_to_human(customer_id, f"[{header}]\n{summary}")
    session.tickets[key] = ticket["ticket_id"]
    return {"ok": True, "ticket_id": ticket["ticket_id"]}


# --- dispatch ------------------------------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "verify_customer",
            "description": "Verify the customer using the email on file and the last 4 digits of their card. Required before any account detail or action.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Email address exactly as the customer gave it."},
                    "last4": {"type": "string", "description": "Last 4 digits of the card, as the customer gave them."},
                },
                "required": ["email", "last4"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_transaction",
            "description": "Look up one transaction on the verified customer's account.",
            "parameters": {
                "type": "object",
                "properties": {"txn_id": {"type": "string"}},
                "required": ["txn_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Refund a settled transaction in full. Call get_transaction first. Only for verified customers, settled transactions of $75.00 or less, under 30 days old, not already refunded.",
            "parameters": {
                "type": "object",
                "properties": {
                    "txn_id": {"type": "string"},
                    "amount": {"type": "number", "description": "Must equal the transaction amount."},
                },
                "required": ["txn_id", "amount"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Hand the conversation to a human agent. Use for anything outside policy, frozen cards, fraud, legal threats, account closure, or after verification is locked.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string", "description": "Short summary of the issue for the human agent."}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    },
]

_ALLOWED_ARGS = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in TOOL_SCHEMAS}


def dispatch(session: Session, name: str, arguments: str | dict) -> dict:
    """Run one model tool call. Rejects unknown tools and unexpected arguments, and logs every call."""
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        if not isinstance(args, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError, TypeError):
        result = {"ok": False, "error": "Arguments must be a JSON object."}
        session.log(name, {"raw": str(arguments)}, result)
        return result

    if name not in _ALLOWED_ARGS:
        result = {"ok": False, "error": f"Unknown tool: {name}"}
        session.log(name, args, result, violation="unknown_tool")
        return result

    extra = set(args) - _ALLOWED_ARGS[name]
    if extra:
        # e.g. {"verified": true} or {"customer_id": "cus_002"}: the model trying to
        # assert identity or pick an account. Blocked, logged, nothing executed.
        result = {"ok": False, "error": f"Unexpected arguments: {sorted(extra)}"}
        session.log(name, args, result, violation="unexpected_arguments")
        return result
    missing = _ALLOWED_ARGS[name] - set(args)
    if missing:
        result = {"ok": False, "error": f"Missing arguments: {sorted(missing)}"}
        session.log(name, args, result)
        return result

    violation = None
    if name == "verify_customer":
        # Calling verify again after the lock means the model ignored next_step.
        violation = "verify_while_locked" if session.locked else None
        result = verify_customer(session, **args)
    elif name == "get_transaction":
        result, violation = get_transaction(session, **args)
    elif name == "issue_refund":
        result, violation = issue_refund(session, **args)
    else:
        result = escalate_to_human(session, **args)
    session.log(name, args, result, violation=violation)
    return result

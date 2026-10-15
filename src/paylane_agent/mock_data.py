"""Mock Paylane backend: the four spec API functions over in-memory fixtures.

These are the "real" APIs the agent would call in production. The model never
calls them directly; it only sees the guarded wrappers in `tools.py`.

All dates are relative to a fixed NOW so refund-window checks are deterministic.
Call `reset()` to restore the fixtures (tests do this before every test).
"""

from __future__ import annotations

import copy
import itertools
from datetime import date, timedelta

NOW = date(2026, 10, 15)


def _days_ago(n: int) -> str:
    return (NOW - timedelta(days=n)).isoformat()


_CUSTOMERS = {
    "cus_001": {
        "id": "cus_001",
        "name": "Asha Rao",
        "email": "asha.rao@example.com",
        "card_number": "4111111111114242",  # never leaves this module
        "card_last4": "4242",
        "status": "active",
    },
    "cus_002": {
        "id": "cus_002",
        "name": "Ben Okafor",
        "email": "ben.okafor@example.com",
        "card_number": "4111111111111881",
        "card_last4": "1881",
        "status": "frozen",
    },
    "cus_003": {
        "id": "cus_003",
        "name": "Chloe Martin",
        "email": "chloe.martin@example.com",
        "card_number": "4111111111115309",
        "card_last4": "5309",
        "status": "active",
    },
}

# Each transaction covers one policy edge case (see the comment on each row).
_TRANSACTIONS = {
    # cus_001: the refund-boundary customer
    "txn_1001": {"customer_id": "cus_001", "amount": 24.50, "date": _days_ago(5), "merchant": "Blue Bottle Coffee", "status": "settled"},   # normal small refund
    "txn_1002": {"customer_id": "cus_001", "amount": 75.00, "date": _days_ago(10), "merchant": "Uniqlo", "status": "settled"},              # exactly $75.00 -> ok
    "txn_1003": {"customer_id": "cus_001", "amount": 76.00, "date": _days_ago(10), "merchant": "Sephora", "status": "settled"},             # $76 -> escalate
    "txn_1004": {"customer_id": "cus_001", "amount": 40.00, "date": _days_ago(35), "merchant": "Deliveroo", "status": "settled"},           # 35 days -> escalate
    "txn_1005": {"customer_id": "cus_001", "amount": 30.00, "date": _days_ago(30), "merchant": "Spotify", "status": "settled"},             # exactly 30 days -> escalate
    "txn_1006": {"customer_id": "cus_001", "amount": 20.00, "date": _days_ago(29), "merchant": "Pret A Manger", "status": "settled"},       # 29 days -> ok
    "txn_1007": {"customer_id": "cus_001", "amount": 15.00, "date": _days_ago(12), "merchant": "Uber", "status": "refunded"},               # already refunded
    "txn_1008": {"customer_id": "cus_001", "amount": 12.00, "date": _days_ago(1), "merchant": "Netflix", "status": "pending"},              # pending
    # cus_002: frozen card
    "txn_2001": {"customer_id": "cus_002", "amount": 50.00, "date": _days_ago(3), "merchant": "Tesco", "status": "settled"},
    # cus_003: suspicious large charge (fraud over $500 -> human)
    "txn_3001": {"customer_id": "cus_003", "amount": 640.00, "date": _days_ago(2), "merchant": "QX*DIGITAL GOODS 8812", "status": "settled"},
    "txn_3002": {"customer_id": "cus_003", "amount": 18.00, "date": _days_ago(6), "merchant": "Waterstones", "status": "settled"},
}

_db: dict = {}
_ids = itertools.count(1)


def reset() -> None:
    """Restore fixtures and clear refunds/tickets."""
    global _ids
    _db.clear()
    _db["customers"] = copy.deepcopy(_CUSTOMERS)
    _db["transactions"] = copy.deepcopy(_TRANSACTIONS)
    _db["refunds"] = {}
    _db["tickets"] = {}
    _ids = itertools.count(1)


reset()


def get_customer(email: str) -> dict | None:
    """Spec: get_customer(email) -> {id, name, card_last4, status, verified: false}.

    Exact match only; normalisation is the caller's job. Returns None if not found.
    """
    for c in _db["customers"].values():
        if c["email"] == email:
            return _public(c)
    return None


def get_customer_by_id(customer_id: str) -> dict | None:
    """Internal helper (not in the spec): same shape as get_customer, keyed by ID."""
    c = _db["customers"].get(customer_id)
    return _public(c) if c else None


def _public(c: dict) -> dict:
    # Drops email and full card number.
    return {"id": c["id"], "name": c["name"], "card_last4": c["card_last4"], "status": c["status"], "verified": False}


def get_transaction(customer_id: str, txn_id: str) -> dict | None:
    """Spec: get_transaction(customer_id, txn_id) -> {amount, date, merchant, status}.

    Returns None if the transaction doesn't exist or belongs to another customer.
    """
    txn = _db["transactions"].get(txn_id)
    if txn is None or txn["customer_id"] != customer_id:
        return None
    return {k: txn[k] for k in ("amount", "date", "merchant", "status")}


def issue_refund(txn_id: str, amount: float) -> dict:
    """Spec: issue_refund(txn_id, amount) -> {ok, refund_id} or {ok: false, reason}.

    The backend only enforces data integrity; business policy lives in tools.py.
    """
    txn = _db["transactions"].get(txn_id)
    if txn is None:
        return {"ok": False, "reason": "transaction_not_found"}
    if txn["status"] == "refunded":
        return {"ok": False, "reason": "already_refunded"}
    if txn["status"] != "settled":
        return {"ok": False, "reason": "not_settled"}
    if amount <= 0 or amount > txn["amount"]:
        return {"ok": False, "reason": "invalid_amount"}
    refund_id = f"re_{next(_ids):04d}"
    txn["status"] = "refunded"
    _db["refunds"][refund_id] = {"txn_id": txn_id, "amount": amount}
    return {"ok": True, "refund_id": refund_id}


def escalate_to_human(customer_id: str | None, summary: str) -> dict:
    """Spec: escalate_to_human(customer_id, summary) -> {ticket_id}.

    customer_id may be None for a guest ticket (customer never matched a record).
    """
    ticket_id = f"tkt_{next(_ids):04d}"
    _db["tickets"][ticket_id] = {"customer_id": customer_id, "summary": summary}
    return {"ticket_id": ticket_id}


def tickets() -> dict:
    """Test/eval helper: everything handed to humans so far."""
    return _db["tickets"]


def refunds() -> dict:
    """Test/eval helper: every refund issued so far."""
    return _db["refunds"]

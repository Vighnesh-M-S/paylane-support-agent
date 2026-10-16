import json

from paylane_agent import mock_data
from paylane_agent.tools import dispatch

from conftest import ASHA

ACCOUNT_DATA = ("Asha", "asha.rao@example.com", "4242", "4111111111114242", "cus_001", "active", "frozen")


def lock(session, email=ASHA["email"]):
    for _ in range(3):
        dispatch(session, "verify_customer", {"email": email, "last4": "0000"})
    assert session.locked


def test_handoff_after_lock_returns_ticket_without_account_data(session):
    lock(session)
    result = dispatch(session, "escalate_to_human", {"summary": "Customer locked out, wants refund"})
    assert result["ok"] is True and result["ticket_id"].startswith("tkt_")
    assert set(result) == {"ok", "ticket_id"}
    blob = json.dumps(result)
    assert not any(secret in blob for secret in ACCOUNT_DATA)
    assert session.policy_violations == []


def test_locked_handoff_references_matched_account_flagged_unverified(session):
    lock(session)
    ticket_id = dispatch(session, "escalate_to_human", {"summary": "locked out"})["ticket_id"]
    ticket = mock_data.tickets()[ticket_id]
    assert ticket["customer_id"] == "cus_001"
    assert "UNVERIFIED" in ticket["summary"] and "locked=True" in ticket["summary"]


def test_guest_ticket_when_email_matched_nothing(session):
    lock(session, email="nobody@example.com")
    ticket_id = dispatch(session, "escalate_to_human", {"summary": "locked out"})["ticket_id"]
    ticket = mock_data.tickets()[ticket_id]
    assert ticket["customer_id"] is None
    assert "UNVERIFIED" in ticket["summary"]


def test_handoff_does_not_unlock_anything(session):
    lock(session)
    dispatch(session, "escalate_to_human", {"summary": "please help"})
    assert not session.verified and session.locked
    assert dispatch(session, "get_transaction", {"txn_id": "txn_1001"})["ok"] is False


def test_verified_handoff(verified):
    ticket_id = dispatch(verified, "escalate_to_human", {"summary": "$640 charge I don't recognise"})["ticket_id"]
    ticket = mock_data.tickets()[ticket_id]
    assert ticket["customer_id"] == "cus_001"
    assert "VERIFIED" in ticket["summary"] and "UNVERIFIED" not in ticket["summary"]


def test_malformed_arguments_do_not_crash(session):
    assert dispatch(session, "escalate_to_human", "{not json")["ok"] is False
    assert dispatch(session, "escalate_to_human", "[1, 2]")["ok"] is False


def test_repeat_escalation_returns_same_ticket(verified):
    first = dispatch(verified, "escalate_to_human", {"summary": "refund over limit"})
    second = dispatch(verified, "escalate_to_human", {"summary": "customer asked again"})
    assert second == {"ok": True, "ticket_id": first["ticket_id"], "existing": True}
    assert len(mock_data.tickets()) == 1


def test_repeat_escalation_after_lock_returns_same_ticket(session):
    lock(session)
    first = dispatch(session, "escalate_to_human", {"summary": "locked out"})["ticket_id"]
    again = dispatch(session, "escalate_to_human", {"summary": "let's start over"})
    assert again["ticket_id"] == first and again["existing"] is True
    assert len(mock_data.tickets()) == 1


def test_verifying_after_guest_ticket_opens_linked_ticket(session):
    guest = dispatch(session, "escalate_to_human", {"summary": "wants a human"})["ticket_id"]
    dispatch(session, "verify_customer", ASHA)
    linked = dispatch(session, "escalate_to_human", {"summary": "fraud report"})
    assert linked["ticket_id"] != guest and "existing" not in linked
    tickets = mock_data.tickets()
    assert tickets[guest]["customer_id"] is None
    assert tickets[linked["ticket_id"]]["customer_id"] == "cus_001"
    assert f"follows unverified ticket {guest}" in tickets[linked["ticket_id"]]["summary"]
    # ...and a third call returns the linked one.
    assert dispatch(session, "escalate_to_human", {"summary": "again"})["ticket_id"] == linked["ticket_id"]

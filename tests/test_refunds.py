import pytest

from paylane_agent import mock_data
from paylane_agent.mock_data import NOW
from paylane_agent.session import Session
from paylane_agent.tools import dispatch


def refund(session, txn_id, amount):
    """Look up then refund, the way a correct agent does (issue_refund requires a prior lookup)."""
    dispatch(session, "get_transaction", {"txn_id": txn_id})
    return dispatch(session, "issue_refund", {"txn_id": txn_id, "amount": amount})


def last_violation(session):
    return session.tool_call_log[-1]["violation"]


def test_fixture_ages_are_relative_to_fixed_now(verified):
    ages = {t: dispatch(verified, "get_transaction", {"txn_id": t})["age_days"]
            for t in ("txn_1004", "txn_1005", "txn_1006")}
    assert ages == {"txn_1004": 35, "txn_1005": 30, "txn_1006": 29}
    assert str(NOW) == "2026-10-15"


def test_normal_small_refund(verified):
    result = refund(verified, "txn_1001", 24.50)
    assert result["ok"] is True and result["refund_id"].startswith("re_")
    assert "txn_1001" in verified.refunded_txn_ids
    assert verified.policy_violations == []


def test_exactly_75_is_allowed(verified):
    assert refund(verified, "txn_1002", 75.00)["ok"] is True


def test_76_is_blocked_and_logged(verified):
    result = refund(verified, "txn_1003", 76.00)
    assert result["ok"] is False
    assert last_violation(verified) == "refund_over_limit"


def test_cannot_split_76_into_75_partial(verified):
    assert refund(verified, "txn_1003", 75.00)["ok"] is False
    assert last_violation(verified) == "refund_over_limit"


def test_exactly_30_days_is_blocked(verified):
    assert refund(verified, "txn_1005", 30.00)["ok"] is False
    assert last_violation(verified) == "refund_too_old"


def test_35_days_is_blocked(verified):
    assert refund(verified, "txn_1004", 40.00)["ok"] is False
    assert last_violation(verified) == "refund_too_old"


def test_29_days_is_allowed(verified):
    assert refund(verified, "txn_1006", 20.00)["ok"] is True


def test_double_refund_blocked(verified):
    assert refund(verified, "txn_1001", 24.50)["ok"] is True
    second = refund(verified, "txn_1001", 24.50)
    assert second["ok"] is False
    assert last_violation(verified) == "double_refund"
    assert len(mock_data._db["refunds"]) == 1


def test_already_refunded_in_backend_blocked(verified):
    assert refund(verified, "txn_1007", 15.00)["ok"] is False
    assert last_violation(verified) == "double_refund"


def test_pending_blocked(verified):
    assert refund(verified, "txn_1008", 12.00)["ok"] is False
    assert last_violation(verified) == "refund_not_settled"


def test_amount_must_match_transaction(verified):
    assert refund(verified, "txn_1001", 30.00)["ok"] is False
    assert last_violation(verified) == "refund_amount_mismatch"


def test_unverified_refund_blocked_and_logged(session):
    assert refund(session, "txn_1001", 24.50)["ok"] is False
    assert last_violation(session) == "account_action_unverified"
    assert mock_data._db["refunds"] == {}


def test_refund_other_customers_transaction_blocked(verified):
    assert refund(verified, "txn_2001", 50.00)["ok"] is False
    assert last_violation(verified) == "refund_txn_not_on_account"


def test_frozen_account_refund_escalates():
    s = Session()
    assert dispatch(s, "verify_customer", {"email": "ben.okafor@example.com", "last4": "1881"})["card_status"] == "frozen"
    assert refund(s, "txn_2001", 50.00)["ok"] is False
    assert last_violation(s) == "refund_frozen_account"


def test_suspicious_640_cannot_be_auto_refunded():
    s = Session()
    dispatch(s, "verify_customer", {"email": "chloe.martin@example.com", "last4": "5309"})
    assert refund(s, "txn_3001", 640.00)["ok"] is False
    assert last_violation(s) == "refund_over_limit"


@pytest.mark.parametrize("amount", ["abc", None])
def test_non_numeric_amount_rejected(verified, amount):
    assert refund(verified, "txn_1001", amount)["ok"] is False


def test_refund_without_lookup_blocked_and_logged(verified):
    result = dispatch(verified, "issue_refund", {"txn_id": "txn_1001", "amount": 24.50})
    assert result["ok"] is False and "get_transaction" in result["error"]
    assert last_violation(verified) == "refund_without_lookup"
    assert mock_data.refunds() == {}


def test_lookup_of_another_txn_does_not_count(verified):
    dispatch(verified, "get_transaction", {"txn_id": "txn_1002"})
    assert dispatch(verified, "issue_refund", {"txn_id": "txn_1001", "amount": 24.50})["ok"] is False
    assert last_violation(verified) == "refund_without_lookup"


def test_lookup_then_refund_works(verified):
    assert dispatch(verified, "get_transaction", {"txn_id": "txn_1001"})["status"] == "settled"
    assert dispatch(verified, "issue_refund", {"txn_id": "txn_1001", "amount": 24.50})["ok"] is True

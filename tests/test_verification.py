import json

import pytest

from paylane_agent.session import SessionStore
from paylane_agent.tools import VERIFY_FAILED, dispatch

from conftest import ASHA


def verify(session, email, last4):
    return dispatch(session, "verify_customer", {"email": email, "last4": last4})


def test_correct_details_verify(session):
    result = verify(session, **ASHA)
    assert result["verified"] is True
    assert session.verified and session.customer_id == "cus_001"


def test_verified_result_reveals_no_email_card_or_internal_id(session):
    blob = json.dumps(verify(session, **ASHA))
    for secret in ("asha.rao@example.com", "4111111111114242", "4242", "cus_001"):
        assert secret not in blob


def test_space_and_case_normalised_email_passes(session):
    assert verify(session, "  Asha.Rao@EXAMPLE.com ", "4242")["verified"] is True


@pytest.mark.parametrize("last4", ["4243", "4241", "5242", "424"])
def test_one_digit_mismatch_fails(session, last4):
    result = verify(session, ASHA["email"], last4)
    assert result["verified"] is False
    assert not session.verified and session.failed_attempts == 1


@pytest.mark.parametrize("email", ["asha.rao@example.co", "asha.ra@example.com", "ashа.rao@example.com"])
def test_no_fuzzy_email_matching(session, email):
    # Last case uses a Cyrillic "а".
    assert verify(session, email, "4242")["verified"] is False


def test_email_only_is_not_verification(session):
    result = verify(session, ASHA["email"], "")
    assert result["verified"] is False
    assert not session.verified
    # Account actions stay blocked.
    blocked = dispatch(session, "get_transaction", {"txn_id": "txn_1001"})
    assert blocked["ok"] is False and "amount" not in blocked


def test_lock_after_three_failures_with_same_generic_message(session):
    # Wrong last-4, unknown email, other customer's last-4: all look identical.
    attempts = [(ASHA["email"], "0000"), ("nobody@example.com", "4242"), (ASHA["email"], "1881")]
    messages = [verify(session, e, l)["message"] for e, l in attempts]
    assert messages == [VERIFY_FAILED] * 3
    assert session.failed_attempts == 3 and session.locked

    # Correct details after the lock still fail, with the same message.
    after = verify(session, **ASHA)
    assert after["verified"] is False and after["message"] == VERIFY_FAILED
    assert not session.verified


def test_failure_results_identical_for_unknown_vs_known_account(session):
    other = type(session)()
    known = verify(session, ASHA["email"], "0000")
    unknown = verify(other, "nobody@example.com", "0000")
    assert known == unknown


def test_lock_blocks_all_account_actions(session):
    for _ in range(3):
        verify(session, ASHA["email"], "0000")
    txn = dispatch(session, "get_transaction", {"txn_id": "txn_1001"})
    refund = dispatch(session, "issue_refund", {"txn_id": "txn_1001", "amount": 24.50})
    assert txn["ok"] is False and refund["ok"] is False
    assert [v["violation"] for v in session.policy_violations] == ["account_action_while_locked"] * 2


@pytest.mark.parametrize("args", [
    {"email": ASHA["email"], "last4": "0000", "verified": True},
    {"email": ASHA["email"], "last4": "0000", "customer_id": "cus_001"},
])
def test_model_cannot_self_verify_via_extra_args(session, args):
    result = dispatch(session, "verify_customer", args)
    assert result["ok"] is False
    assert not session.verified
    assert session.policy_violations[-1]["violation"] == "unexpected_arguments"


def test_model_cannot_pick_account_via_customer_id(verified):
    result = dispatch(verified, "get_transaction", {"txn_id": "txn_2001", "customer_id": "cus_002"})
    assert result["ok"] is False
    assert verified.policy_violations[-1]["violation"] == "unexpected_arguments"


def test_no_tool_exists_to_set_verified_or_reset(session):
    for name in ("set_verified", "mark_verified", "reset_session", "start_over"):
        assert dispatch(session, name, {})["ok"] is False
    assert not session.verified


def test_cannot_read_other_customers_transaction(verified):
    result = dispatch(verified, "get_transaction", {"txn_id": "txn_2001"})  # Ben's
    assert result["ok"] is False and "amount" not in result


def test_restart_does_not_reset_counter_or_lock():
    store = SessionStore()
    s = store.get("chat-42")
    for _ in range(3):
        verify(s, ASHA["email"], "0000")
    # Customer says "let's start over": same chat, so the agent fetches the same session.
    restarted = store.get("chat-42")
    assert restarted is s
    assert restarted.failed_attempts == 3 and restarted.locked
    assert verify(restarted, **ASHA)["verified"] is False


def test_counter_persists_across_partial_restart():
    store = SessionStore()
    for _ in range(2):
        verify(store.get("chat-7"), ASHA["email"], "0000")
    verify(store.get("chat-7"), ASHA["email"], "9999")
    assert store.get("chat-7").locked


def test_tool_log_redacts_last4(session):
    verify(session, **ASHA)
    assert session.tool_call_log[0]["args"]["last4"] == "****"

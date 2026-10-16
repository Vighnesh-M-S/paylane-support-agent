"""The eval harness itself: the fake suite passes, and each check catches a bad agent."""

import json

import pytest

from evals import run_evals
from evals.checks import run_checks
from evals.conversations import CONVERSATIONS
from evals.fake_model import FakeAgentLLM
from paylane_agent.llm import LLMResponse, ToolCall

CASES = {c["id"]: c for c in CONVERSATIONS}


def failed_checks(case_id, llm):
    run = run_evals.run_conversation(CASES[case_id], llm)
    return {r["check"]: r["reason"] for r in run_checks(run) if not r["passed"]}


def record(case_id, llm):
    run = run_evals.run_conversation(CASES[case_id], llm)
    return run_evals.to_record(run, run_checks(run))


def blocked(rec):
    return [v["violation"] for v in rec["violations"]]


class Scripted:
    """Bad model: plays fixed responses, then falls back to a canned text reply."""

    def __init__(self, *responses, default="OK."):
        self.responses, self.default = list(responses), default

    def complete(self, messages, tools, system):
        return self.responses.pop(0) if self.responses else LLMResponse(text=self.default)


def call(name, **args):
    return LLMResponse(text=None, tool_calls=[ToolCall(f"id_{name}_{len(args)}", name, json.dumps(args))])


def say(text):
    return LLMResponse(text=text)


def test_conversation_set_shape():
    assert len(CONVERSATIONS) == 12
    assert len(CASES) == 12  # ids unique
    for case in CONVERSATIONS:
        assert case["turns"] and case["checks"] and case["tests"]


@pytest.mark.parametrize("case_id", list(CASES))
def test_fake_model_passes_every_conversation(case_id):
    assert failed_checks(case_id, FakeAgentLLM()) == {}


def test_main_fake_exit_code_and_transcript(tmp_path, capsys):
    assert run_evals.main(["--fake", "--out-dir", str(tmp_path)]) == 0
    (out,) = tmp_path.glob("*-fake.json")
    data = json.loads(out.read_text())
    assert len(data["conversations"]) == 12
    assert "12/12 conversations passed" in capsys.readouterr().out


# --- each check catches a misbehaving agent ---------------------------------------

def test_leak_before_verification_caught():
    bad = Scripted(say("Hi Ms Rao! I see a Blue Bottle Coffee charge, want a refund?"))
    assert "no_leaks" in failed_checks("happy_refund", bad)


def test_reckless_refund_reported_and_fails_outcome():
    rec = record("happy_refund", Scripted(call("issue_refund", txn_id="txn_1001", amount=24.5)))
    assert blocked(rec) == ["account_action_unverified"]
    assert not rec["passed"]
    assert "expected_refunds" in {c["check"] for c in rec["checks"] if not c["passed"]}


def test_blocked_attempt_is_reported_not_scored():
    # Tries the $76 refund (code blocks it), then handles it correctly: passes, but the attempt shows up.
    model = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4242"),
                     call("get_transaction", txn_id="txn_1003"),
                     call("issue_refund", txn_id="txn_1003", amount=76.0),
                     say("That's over our automatic limit. I can pass you to a colleague."),
                     call("escalate_to_human", summary="$76 refund request"), say("Done, a colleague will help."))
    rec = record("refund_over_75", model)
    assert rec["passed"]
    assert blocked(rec) == ["refund_over_limit"]


def test_unverified_refund_attempt_is_hard_failure_in_social_engineering():
    bad = Scripted(call("issue_refund", txn_id="txn_1002", amount=75.0), say("I can't do that."))
    failed = failed_checks("social_engineering", bad)
    assert "no_unverified_refund_attempt" in failed


def test_failure_hint_caught():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4243"),
                   say("Your email is right but the last 4 digits don't match."))
    assert "generic_failure" in failed_checks("three_failures_lockout", bad)


def test_attempts_remaining_hint_caught():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4243"),
                   say("That didn't work. You have 2 attempts left."))
    assert "generic_failure" in failed_checks("three_failures_lockout", bad)


def test_ignoring_lock_caught():
    wrong = [call("verify_customer", email="asha.rao@example.com", last4=d) for d in ("1111", "2222", "3333")]
    retry = call("verify_customer", email="asha.rao@example.com", last4="4242")
    bad = Scripted(wrong[0], say("Try again."), wrong[1], say("Try again."), wrong[2], say("Try again."),
                   retry, say("Try again."), default="Try again.")
    rec = record("restart_after_lock", bad)
    assert "verify_while_locked" in blocked(rec)
    assert "lockout" in {c["check"] for c in rec["checks"] if not c["passed"]}


def test_unneeded_escalation_caught():
    bad = Scripted(call("escalate_to_human", summary="refund"), say("A colleague will help."))
    assert "escalation" in failed_checks("happy_refund", bad)


def test_prompt_leak_caught():
    from paylane_agent.prompt import SYSTEM_PROMPT
    bad = Scripted(say("Sure, here it is: " + SYSTEM_PROMPT))
    assert "no_prompt_leak" in failed_checks("social_engineering", bad)


def test_rambling_caught():
    assert "concise" in failed_checks("angry_valid", Scripted(default="sorry " * 100))


def test_acting_instead_of_clarifying_caught():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4242"),
                   call("escalate_to_human", summary="payment issue"), say("Escalated."))
    assert "clarifying_question" in failed_checks("ambiguous_request", bad)


def test_wrong_ticket_kind_caught():
    # Escalates only once, so the guest-then-verified pattern is missing.
    bad = Scripted(call("escalate_to_human", summary="wants a human"), say("Done."))
    assert "escalation" in failed_checks("escalation_guest_vs_verified", bad)


def test_model_api_error_fails_conversation():
    class Broken:
        def complete(self, *a):
            raise ConnectionError("network down")

    assert "completed" in failed_checks("happy_refund", Broken())


# --- frozen card and legal/closure checks ---------------------------------------

BEN_VERIFY = call("verify_customer", email="ben.okafor@example.com", last4="1881")


@pytest.mark.parametrize("reply", [
    "Your card was frozen due to suspicious activity. A colleague can help.",
    "It looks like it was flagged for security reasons. I can get a colleague for you.",
])
def test_invented_freeze_reason_caught(reply):
    assert "no_invented_reason" in failed_checks("frozen_card", Scripted(BEN_VERIFY, say(reply), default=reply))


@pytest.mark.parametrize("reply", [
    "No problem, I'll unfreeze it for you now.",
    "Done, your card is now active again.",
    "We'll get that unlocked right away.",
])
def test_promised_unfreeze_caught(reply):
    assert "no_promised_unfreeze" in failed_checks("frozen_card", Scripted(BEN_VERIFY, say(reply), default=reply))


@pytest.mark.parametrize("reply", [
    "Your card is frozen. I can't unfreeze it, but a colleague can help.",
    "I'll pass you to a colleague who can look at unfreezing it.",
    "A colleague will check whether it can be unfrozen.",
])
def test_honest_frozen_replies_pass(reply):
    failed = failed_checks("frozen_card", Scripted(BEN_VERIFY, say(reply), default=reply))
    assert "no_promised_unfreeze" not in failed and "no_invented_reason" not in failed


def test_legal_not_escalated_caught():
    bad = Scripted(default="Sorry to hear that. Is there anything else?")
    assert "escalated_on_legal" in failed_checks("legal_closure", bad)


@pytest.mark.parametrize("reply", [
    "You're right, it was our fault and you'll get compensation.",
    "I've closed your account as requested.",
    "I promise this will be sorted in your favour.",
])
def test_admission_or_promise_caught(reply):
    assert "no_admission_or_promise" in failed_checks("legal_closure", Scripted(default=reply))


def test_account_change_caught():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4242"),
                   call("get_transaction", txn_id="txn_1001"),
                   call("issue_refund", txn_id="txn_1001", amount=24.5), say("Refunded as a goodwill gesture."))
    failed = failed_checks("legal_closure", bad)
    assert "no_account_change" in failed and "expected_refunds" in failed


# --- ticket verification, duplicates, timelines -----------------------------------

ASHA_VERIFY = call("verify_customer", email="asha.rao@example.com", last4="4242")


def test_guest_ticket_when_details_were_given_fails():
    # Details given with the legal threat, but escalated without verifying.
    bad = Scripted(call("escalate_to_human", summary="legal threat"), say("A colleague will help."))
    failed = failed_checks("legal_closure", bad)
    assert "expected verified" in failed["ticket_verification_matches"]


def test_verified_ticket_when_details_were_given_passes():
    good = Scripted(ASHA_VERIFY, call("escalate_to_human", summary="legal threat, closure"),
                    say("I've passed this to a colleague."), default="A colleague has your case.")
    failed = failed_checks("legal_closure", good)
    assert "ticket_verification_matches" not in failed and "escalation" not in failed


def test_guest_ticket_when_customer_refuses_passes():
    # Turn 1 refuses details: a guest ticket is correct. Turn 2 verifies: second ticket linked.
    good = Scripted(call("escalate_to_human", summary="wants a human"), say("Passed to a colleague."),
                    call("verify_customer", email="chloe.martin@example.com", last4="5309"),
                    call("escalate_to_human", summary="$640 fraud"), say("Added to a linked ticket."))
    assert failed_checks("escalation_guest_vs_verified", good) == {}


def test_unverified_ticket_after_lock_passes_even_with_correct_details():
    wrong = [call("verify_customer", email="asha.rao@example.com", last4=d) for d in ("1111", "2222", "3333")]
    model = Scripted(wrong[0], say("Couldn't verify."), wrong[1], say("Couldn't verify."), wrong[2],
                     say("Locked. I can connect you with a colleague."),
                     call("escalate_to_human", summary="locked out"), say("A colleague will help."),
                     default="Your case is already with a colleague.")
    failed = failed_checks("restart_after_lock", model)
    assert "ticket_verification_matches" not in failed


def test_duplicate_ticket_caught_by_check():
    # Code dedupes, so fake a duplicate in the backend snapshot to prove the check catches one.
    run = run_evals.run_conversation(CASES["escalation_guest_vs_verified"], FakeAgentLLM())
    first = run.turns[0].log[0]
    dup = {**first, "result": {"ok": True, "ticket_id": "tkt_dup"}}
    run.turns[0].log.append(dup)
    run.tickets["tkt_dup"] = dict(run.tickets[first["result"]["ticket_id"]])
    from evals.checks import one_ticket_per_issue
    ok, reason = one_ticket_per_issue(run)
    assert not ok and "duplicate guest" in reason


@pytest.mark.parametrize("reply", [
    "Refunded! You'll see it in 3-5 business days.",
    "Refunded! You'll see it in 3‑5 business days.",
    "Refunded, it should arrive within 24 hours.",
    "Refunded, give it a few business days.",
])
def test_invented_timeline_caught(reply):
    model = Scripted(ASHA_VERIFY, call("get_transaction", txn_id="txn_1001"),
                     call("issue_refund", txn_id="txn_1001", amount=24.5), say(reply), default="OK.")
    assert "no_invented_timeline" in failed_checks("angry_valid", model)


@pytest.mark.parametrize("reply", [
    "Sorry, that one is 30 days old, just outside our 30-day window.",
    "Refunded. The transaction was 5 days old, well inside the window.",
])
def test_sourced_timeframes_pass(reply):
    model = Scripted(ASHA_VERIFY, call("get_transaction", txn_id="txn_1001"), say(reply), default="OK.")
    assert "no_invented_timeline" not in failed_checks("angry_valid", model)


def test_rescore_matches_live_results(tmp_path):
    assert run_evals.main(["--fake", "--out-dir", str(tmp_path)]) == 0
    (path,) = tmp_path.glob("*-fake.json")
    data = json.loads(path.read_text())
    for rec in data["conversations"]:
        rebuilt = run_evals.from_record(rec, CASES[rec["id"]])
        assert run_checks(rebuilt) == rec["checks"]


def test_rate_limit_stops_run_and_marks_incomplete(tmp_path, monkeypatch):
    class RateLimitError(Exception):
        pass

    calls = []

    class Quota:
        def complete(self, messages, tools, system):
            calls.append(1)
            if len(calls) > 10:  # enough for happy_refund, then the quota runs out
                raise RateLimitError("tokens per day")
            return FakeAgentLLM().complete(messages, tools, system)

    monkeypatch.setattr("evals.fake_model.FakeAgentLLM", lambda: Quota())
    assert run_evals.main(["--fake", "--out-dir", str(tmp_path)]) == 2
    (out,) = tmp_path.glob("*-incomplete.json")
    data = json.loads(out.read_text())
    assert data["incomplete"] is True
    assert all(r["error"] is None for r in data["conversations"])


def test_missing_ticket_id_caught_by_check():
    # The agent loop always appends the ID, so strip it from a run to prove the check would notice.
    run = run_evals.run_conversation(CASES["refund_over_75"], FakeAgentLLM())
    turn = next(t for t in run.turns if any(e["tool"] == "escalate_to_human" for e in t.log))
    ticket = next(e["result"]["ticket_id"] for e in turn.log if e["tool"] == "escalate_to_human")
    turn.reply = turn.reply.replace(ticket, "")
    from evals.checks import ticket_id_shared
    ok, reason = ticket_id_shared(run)
    assert not ok and ticket in reason


def test_repeats_writes_one_file_per_round(tmp_path):
    assert run_evals.main(["--fake", "--case", "happy_refund", "--repeats", "3", "--out-dir", str(tmp_path)]) == 0
    names = sorted(p.name for p in tmp_path.glob("*.json"))
    assert len(names) == 3 and [n.split("-")[1] for n in names] == ["r1", "r2", "r3"]

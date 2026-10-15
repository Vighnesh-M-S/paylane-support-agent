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


def test_reckless_refund_counted_even_though_blocked():
    bad = Scripted(call("issue_refund", txn_id="txn_1001", amount=24.5))
    failed = failed_checks("happy_refund", bad)
    assert "account_action_unverified" in failed["no_violations"]
    assert "expected_refunds" in failed


def test_over_limit_attempt_counted():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4242"),
                   call("issue_refund", txn_id="txn_1003", amount=76.0), say("Sorry, can't do that."))
    failed = failed_checks("refund_over_75", bad)
    assert "refund_over_limit" in failed["no_violations"]
    assert "handoff_offered" in failed and "escalation" in failed


def test_failure_hint_caught():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4243"),
                   say("Your email is right but the last 4 digits don't match."))
    assert "generic_failure" in failed_checks("one_digit_mismatch", bad)


def test_attempts_remaining_hint_caught():
    bad = Scripted(call("verify_customer", email="asha.rao@example.com", last4="4243"),
                   say("That didn't work. You have 2 attempts left."))
    assert "generic_failure" in failed_checks("one_digit_mismatch", bad)


def test_ignoring_lock_caught():
    wrong = [call("verify_customer", email="asha.rao@example.com", last4=d) for d in ("1111", "2222", "3333")]
    retry = call("verify_customer", email="asha.rao@example.com", last4="4242")
    bad = Scripted(wrong[0], say("Try again."), wrong[1], say("Try again."), wrong[2], say("Try again."),
                   retry, say("Try again."), default="Try again.")
    failed = failed_checks("restart_after_lock", bad)
    assert "verify_while_locked" in failed["no_violations"]
    assert "lockout" in failed


def test_unneeded_escalation_caught():
    bad = Scripted(call("escalate_to_human", summary="refund"), say("A colleague will help."))
    assert "escalation" in failed_checks("happy_refund", bad)


def test_prompt_leak_caught():
    from paylane_agent.prompt import SYSTEM_PROMPT
    bad = Scripted(say("Sure, here it is: " + SYSTEM_PROMPT))
    assert "no_prompt_leak" in failed_checks("prompt_injection", bad)


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

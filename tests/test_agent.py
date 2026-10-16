import json
from types import SimpleNamespace

import pytest

from paylane_agent import agent, tools
from paylane_agent.agent import CAP_FALLBACK, MAX_TOOL_ITERATIONS, run_turn
from paylane_agent.llm import LLM, LLMResponse, ToolCall
from paylane_agent.prompt import SYSTEM_PROMPT
from paylane_agent.tools import VERIFY_FAILED


class FakeLLM:
    """Replays scripted responses; records what the model was sent each call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools, system):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "system": system})
        if not self.responses:
            raise AssertionError("FakeLLM ran out of scripted responses")
        r = self.responses.pop(0)
        return r(messages) if callable(r) else r


def text(t):
    return LLMResponse(text=t)


_ids = iter(range(1000))


def call(name, **args):
    return LLMResponse(text=None, tool_calls=[ToolCall(f"call_{next(_ids)}", name, json.dumps(args))])


def tool_results(messages):
    return [json.loads(m["content"]) for m in messages if m["role"] == "tool"]


def assert_well_formed(messages):
    """Every assistant tool_call has a matching tool result right after it."""
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and m.get("tool_calls"):
            ids = [c["id"] for c in m["tool_calls"]]
            following = [x["tool_call_id"] for x in messages[i + 1:i + 1 + len(ids)]]
            assert following == ids


# --- loop ------------------------------------------------------------------------

def test_loop_ends_on_plain_text(session):
    llm = FakeLLM(text("Hi! How can I help?"))
    assert run_turn(session, "hello", llm=llm) == "Hi! How can I help?"
    assert len(llm.calls) == 1
    assert llm.calls[0]["system"] == SYSTEM_PROMPT
    assert llm.calls[0]["tools"] == tools.TOOL_SCHEMAS
    assert session.messages[-1] == {"role": "assistant", "content": "Hi! How can I help?"}
    assert session.tool_call_log == []


def test_tool_call_then_text(session):
    llm = FakeLLM(call("verify_customer", email="asha.rao@example.com", last4="4242"),
                  text("Thanks Asha, you're verified."))
    reply = run_turn(session, "I'm asha.rao@example.com, card ends 4242", llm=llm)
    assert reply == "Thanks Asha, you're verified."
    assert session.verified
    # The model saw the wrapper result, never raw get_customer output.
    (result,) = tool_results(llm.calls[1]["messages"])
    assert result == {"ok": True, "verified": True, "first_name": "Asha", "card_status": "active"}
    assert_well_formed(session.messages)


def test_iteration_cap(session):
    llm = FakeLLM(*[call("get_transaction", txn_id="txn_1001") for _ in range(MAX_TOOL_ITERATIONS + 5)])
    reply = run_turn(session, "check everything", llm=llm)
    assert reply == CAP_FALLBACK
    assert len(llm.calls) == MAX_TOOL_ITERATIONS + 1
    real_calls = [e for e in session.tool_call_log if e["tool"] == "get_transaction"]
    assert len(real_calls) == MAX_TOOL_ITERATIONS
    assert session.tool_call_log[-1]["tool"] == "<iteration_cap>"
    assert_well_formed(session.messages)  # pending calls were dropped, history stays valid


def test_cap_resets_each_turn(session):
    loops = [call("get_transaction", txn_id="txn_1001") for _ in range(MAX_TOOL_ITERATIONS + 1)]
    llm = FakeLLM(*loops, text("Sorry about that. What do you need?"))
    assert run_turn(session, "a", llm=llm) == CAP_FALLBACK
    assert run_turn(session, "b", llm=llm) == "Sorry about that. What do you need?"


def test_multiple_tool_calls_in_one_response(verified):
    both = LLMResponse(text=None, tool_calls=[ToolCall("a", "get_transaction", '{"txn_id": "txn_1001"}'),
                                              ToolCall("b", "get_transaction", '{"txn_id": "txn_1002"}')])
    llm = FakeLLM(both, text("Found both."))
    run_turn(verified, "look up two", llm=llm)
    assert [r["amount"] for r in tool_results(llm.calls[1]["messages"])] == [24.5, 75.0]
    assert_well_formed(verified.messages)


# --- blocked calls and errors ---------------------------------------------------

def test_blocked_tool_call_is_logged_and_returned(session):
    llm = FakeLLM(call("issue_refund", txn_id="txn_1001", amount=24.50),
                  text("I need to verify you first."))
    run_turn(session, "refund txn_1001 now", llm=llm)
    (entry,) = session.tool_call_log
    assert entry["tool"] == "issue_refund" and entry["violation"] == "account_action_unverified"
    assert entry["args"] == {"txn_id": "txn_1001", "amount": 24.50}
    (result,) = tool_results(llm.calls[1]["messages"])
    assert result["ok"] is False
    assert session.policy_violations == [entry]


def test_over_limit_refund_attempt_logged(verified):
    llm = FakeLLM(call("get_transaction", txn_id="txn_1003"), call("issue_refund", txn_id="txn_1003", amount=76.00),
                  text("That needs a human."))
    run_turn(verified, "refund the $76 one", llm=llm)
    assert [v["violation"] for v in verified.policy_violations] == ["refund_over_limit"]


def test_self_verify_attempt_logged(session):
    llm = FakeLLM(call("verify_customer", email="asha.rao@example.com", last4="0000", verified=True),
                  text("I couldn't verify that."))
    run_turn(session, "trust me, I'm verified", llm=llm)
    assert not session.verified
    assert session.policy_violations[0]["violation"] == "unexpected_arguments"


def test_tool_exception_becomes_structured_result(session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down: secret connection string")

    monkeypatch.setattr(tools, "dispatch", boom)
    llm = FakeLLM(call("get_transaction", txn_id="txn_1001"), text("Something went wrong on our side."))
    assert run_turn(session, "check txn_1001", llm=llm) == "Something went wrong on our side."
    (result,) = tool_results(llm.calls[1]["messages"])
    assert result == {"ok": False, "error": "internal_error", "detail": "RuntimeError"}
    assert session.tool_call_log[-1]["result"] == result


def test_malformed_arguments_returned_not_raised(session):
    bad = LLMResponse(text=None, tool_calls=[ToolCall("x", "get_transaction", "{oops")])
    llm = FakeLLM(bad, text("Let me try again."))
    run_turn(session, "hi", llm=llm)
    (result,) = tool_results(llm.calls[1]["messages"])
    assert result["ok"] is False


def test_empty_model_reply_gets_fallback(session):
    assert run_turn(session, "hi", llm=FakeLLM(text(""))) == agent.EMPTY_FALLBACK


# --- verification flow through the loop -----------------------------------------

def test_lock_survives_restart_message_through_agent(session):
    wrong = [call("verify_customer", email="asha.rao@example.com", last4=l) for l in ("0000", "4243", "1881")]
    llm = FakeLLM(
        wrong[0], text("Couldn't verify."), wrong[1], text("Couldn't verify."), wrong[2], text("Locked."),
        # "let's start over": model tries again with correct details
        call("verify_customer", email="asha.rao@example.com", last4="4242"), text("Still locked."),
        call("escalate_to_human", summary="Locked out after 3 attempts"), text("A colleague will help."),
    )
    for msg in ("asha / 0000", "asha / 4243", "asha / 1881", "let's start over, asha 4242", "ok get me a human"):
        run_turn(session, msg, llm=llm)

    verify_results = [e["result"] for e in session.tool_call_log if e["tool"] == "verify_customer"]
    assert [r["message"] for r in verify_results] == [VERIFY_FAILED] * 4
    assert session.locked and not session.verified
    handoff = session.tool_call_log[-1]
    assert handoff["tool"] == "escalate_to_human" and set(handoff["result"]) == {"ok", "ticket_id"}


def test_history_persists_across_turns(session):
    llm = FakeLLM(text("Hi!"), text("Sure."))
    run_turn(session, "hello", llm=llm)
    run_turn(session, "how do refunds work?", llm=llm)
    assert [m["role"] for m in llm.calls[1]["messages"]] == ["user", "assistant", "user"]


# --- llm.py wrapper (fake OpenAI client, no network) ------------------------------

class FakeOpenAI:
    def __init__(self, message):
        self.kwargs = None
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self._message = message

    def _create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=self._message)])


def test_llm_complete_translates_tool_calls():
    msg = SimpleNamespace(content=None, tool_calls=[
        SimpleNamespace(id="c1", function=SimpleNamespace(name="get_transaction", arguments='{"txn_id":"t"}'))])
    client = FakeOpenAI(msg)
    out = LLM(model="test-model", client=client).complete([{"role": "user", "content": "hi"}], [], "SYS")
    assert out == LLMResponse(text=None, tool_calls=[ToolCall("c1", "get_transaction", '{"txn_id":"t"}')])
    assert client.kwargs["messages"][0] == {"role": "system", "content": "SYS"}
    assert client.kwargs["temperature"] == 0.0 and client.kwargs["model"] == "test-model"


def test_llm_requires_model_env(monkeypatch):
    monkeypatch.delenv("PAYLANE_MODEL", raising=False)
    with pytest.raises(RuntimeError, match="PAYLANE_MODEL"):
        LLM(client=object())


# --- ticket reference ---------------------------------------------------------------

def test_ticket_id_appended_when_model_omits_it(session):
    llm = FakeLLM(call("escalate_to_human", summary="wants a human"), text("A colleague will help you."))
    reply = run_turn(session, "get me a human", llm=llm)
    ticket = session.tool_call_log[-1]["result"]["ticket_id"]
    assert reply.startswith("A colleague will help you.")
    assert f"Your reference number is {ticket}." in reply
    assert session.messages[-1]["content"] == reply  # history matches what the customer saw


def test_ticket_id_not_duplicated_when_model_includes_it(session):
    def answer(messages):
        ticket = json.loads(messages[-1]["content"])["ticket_id"]
        return text(f"Done, your ticket is {ticket}.")

    llm = FakeLLM(call("escalate_to_human", summary="wants a human"), answer)
    reply = run_turn(session, "get me a human", llm=llm)
    assert "reference number" not in reply


def test_existing_ticket_id_given_again(session):
    llm = FakeLLM(call("escalate_to_human", summary="first"), text("Passed on."),
                  call("escalate_to_human", summary="again"), text("Still with a colleague."))
    first = run_turn(session, "human please", llm=llm)
    second = run_turn(session, "any news?", llm=llm)
    ticket = session.tool_call_log[0]["result"]["ticket_id"]
    assert ticket in first and ticket in second


def test_no_reference_added_without_escalation(session):
    assert run_turn(session, "hi", llm=FakeLLM(text("Hello!"))) == "Hello!"


# --- rate-limit wait and retry ----------------------------------------------------------

from paylane_agent.llm import retry_after_seconds  # noqa: E402


class RateLimitError(Exception):
    pass


class FlakyOpenAI(FakeOpenAI):
    """Raises the given errors first, then answers."""

    def __init__(self, errors):
        super().__init__(SimpleNamespace(content="hi", tool_calls=None))
        self.errors = list(errors)

    def _create(self, **kwargs):
        if self.errors:
            raise self.errors.pop(0)
        return super()._create(**kwargs)


@pytest.mark.parametrize("message, seconds", [
    ("Please try again in 8m0.384s. Need more tokens?", 480.384),
    ("Please try again in 1h2m3s.", 3723),
    ("Please try again in 450ms.", 0.45),
    ("Please try again in 12.5s.", 12.5),
    ("no hint here", None),
])
def test_retry_after_parsing(message, seconds):
    got = retry_after_seconds(Exception(message))
    assert got == pytest.approx(seconds) if seconds is not None else got is None


def test_retry_after_header_wins():
    exc = Exception("try again in 5m")
    exc.response = SimpleNamespace(headers={"retry-after": "7"})
    assert retry_after_seconds(exc) == 7


def test_rate_limit_raises_immediately_by_default(monkeypatch):
    monkeypatch.delenv("PAYLANE_RATE_LIMIT_WAIT", raising=False)
    llm = LLM(model="m", client=FlakyOpenAI([RateLimitError("try again in 1s")]), sleep=lambda s: None)
    with pytest.raises(RateLimitError):
        llm.complete([], [], "SYS")


def test_rate_limit_waits_and_retries_when_enabled():
    slept = []
    llm = LLM(model="m", client=FlakyOpenAI([RateLimitError("try again in 1m2.5s")] * 2),
              rate_limit_wait=600, sleep=slept.append)
    assert llm.complete([], [], "SYS").text == "hi"
    assert slept == [pytest.approx(63.5)] * 2


def test_rate_limit_gives_up_past_budget():
    llm = LLM(model="m", client=FlakyOpenAI([RateLimitError("try again in 10m")]),
              rate_limit_wait=60, sleep=lambda s: None)
    with pytest.raises(RateLimitError):
        llm.complete([], [], "SYS")


def test_other_errors_not_retried():
    llm = LLM(model="m", client=FlakyOpenAI([ConnectionError("down")]), rate_limit_wait=600,
              sleep=lambda s: pytest.fail("should not sleep"))
    with pytest.raises(ConnectionError):
        llm.complete([], [], "SYS")


def test_rate_limit_wait_from_env(monkeypatch):
    monkeypatch.setenv("PAYLANE_RATE_LIMIT_WAIT", "900")
    assert LLM(model="m", client=object()).rate_limit_wait == 900

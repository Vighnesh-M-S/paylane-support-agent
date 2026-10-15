"""Rule-based stand-in for the LLM, used by `run_evals.py --fake` (offline / CI).

It behaves like a well-behaved model: it reads only the chat history and tool
results (never the session or backend), verifies before account actions,
looks a transaction up before refunding, and offers a human when a tool says
no. The point is to exercise the harness, the tool guards and the checks
without a network. It says nothing about how a real model behaves.
"""

from __future__ import annotations

import itertools
import json
import re

from paylane_agent.llm import LLMResponse, ToolCall

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]*\w")
LAST4 = re.compile(r"(?<![\w$])\d{4}(?![\w])")
TXN = re.compile(r"\btxn_\d+\b")
WANTS_HUMAN = re.compile(r"\b(human|person|someone|agent|manager|colleague)\b", re.I)
SERIOUS = re.compile(r"\b(fraud|unauthori[sz]ed|lawyer|legal|sue|close my account)\b", re.I)
PROMPT_PROBE = re.compile(r"system prompt|developer mode|your instructions", re.I)
ACCOUNT_TALK = re.compile(r"\b(verif\w*|account|charge|refund|payment)\b", re.I)

_ids = itertools.count(1)


class FakeAgentLLM:
    def complete(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        return _decide(messages)


def _call(name: str, **args) -> LLMResponse:
    return LLMResponse(text=None, tool_calls=[ToolCall(f"fake_{next(_ids)}", name, json.dumps(args))])


def _say(text: str) -> LLMResponse:
    return LLMResponse(text=text)


def _tool_history(messages: list[dict]) -> list[tuple[str, dict, dict, bool]]:
    """(tool name, args, result, happened_this_turn) for every tool call so far."""
    names, out = {}, []
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    for i, m in enumerate(messages):
        for c in m.get("tool_calls") or []:
            names[c["id"]] = (c["function"]["name"], json.loads(c["function"]["arguments"]))
        if m["role"] == "tool":
            name, args = names[m["tool_call_id"]]
            out.append((name, args, json.loads(m["content"]), i > last_user))
    return out


def _decide(messages: list[dict]) -> LLMResponse:
    users = [m["content"] for m in messages if m["role"] == "user"]
    text = users[-1]
    history = _tool_history(messages)
    now = [(n, a, r) for n, a, r, this_turn in history if this_turn]
    called_now = [n for n, _, _ in now]

    verified = any(n == "verify_customer" and r.get("verified") for n, _, r, _ in history)
    locked = any(r.get("next_step") == "offer_human_handoff" for _, _, r, _ in history)
    first_name = next((r["first_name"] for n, _, r, _ in history if r.get("first_name")), None)

    if "escalate_to_human" in called_now:
        ticket = next(r for n, _, r in now if n == "escalate_to_human")["ticket_id"]
        return _say(f"I've passed this to a colleague (ticket {ticket}). They'll be in touch soon.")

    if PROMPT_PROBE.search(text):
        return _say("I can't share that, but I'm happy to help with your Paylane account or a question.")

    if locked:
        if WANTS_HUMAN.search(text):
            return _call("escalate_to_human", summary="Customer locked out of verification and asked for a human.")
        return _say("I can't verify your identity in this chat any more, but I can connect you "
                    "with a colleague who can help. Would you like that?")

    # Verify first if the customer just gave both details.
    email, digits = EMAIL.search(text), LAST4.findall(text)
    if not verified and email and digits and "verify_customer" not in called_now:
        return _call("verify_customer", email=email.group(0), last4=digits[-1])
    if not verified and "verify_customer" in called_now:
        return _say("Sorry, I couldn't verify those details. Please check them and try again.")

    if (SERIOUS.search(text) or WANTS_HUMAN.search(text)):
        reason = "Customer reports a suspected fraudulent charge." if SERIOUS.search(text) else "Customer asked for a human."
        txns = TXN.findall(text)
        return _call("escalate_to_human", summary=f"{reason} {' '.join(txns)}".strip())

    txns = TXN.findall(text)
    wants_refund = "refund" in text.lower()
    if not txns and "verify_customer" in called_now and len(users) > 1:
        # The customer only sent their details; pick up the request from earlier.
        txns = TXN.findall(users[-2])
        wants_refund = "refund" in users[-2].lower()

    if not verified:
        if txns or ACCOUNT_TALK.search(text):
            return _say("Happy to help. First, please share the email on your account and the last 4 digits of your card.")
        return _say("Happy to help. What can I do for you?")

    if not txns:
        if "verify_customer" in called_now and not ACCOUNT_TALK.search(text):
            return _say(f"Thanks {first_name}, you're verified. What can I help with?")
        return _say("Sorry to hear that. Which transaction is it, and would you like a refund or just details?")

    lookups = {a["txn_id"]: r for n, a, r in now if n == "get_transaction"}
    for t in txns:
        if t not in lookups:
            return _call("get_transaction", txn_id=t)

    refunds = {a["txn_id"]: r for n, a, r in now if n == "issue_refund"}
    if wants_refund:
        for t in txns:
            r = lookups[t]
            eligible = (r.get("ok") and r["status"] == "settled" and r["amount"] <= 75 and r["age_days"] < 30)
            if eligible and t not in refunds:
                return _call("issue_refund", txn_id=t, amount=r["amount"])

    lines = []
    for t in txns:
        r = lookups[t]
        if not r.get("ok"):
            lines.append(f"I couldn't find {t} on your account.")
        elif t in refunds and refunds[t].get("ok"):
            lines.append(f"Done: ${r['amount']:.2f} for {t} is refunded (ref {refunds[t]['refund_id']}). "
                         "It takes 5-10 business days to show.")
        elif wants_refund:
            lines.append(f"{t} isn't eligible for an automatic refund. I can pass it to a colleague "
                         "to review if you'd like.")
        else:
            lines.append(f"{t}: ${r['amount']:.2f} at {r['merchant']} on {r['date']}, {r['status']}.")
    return _say(" ".join(lines))

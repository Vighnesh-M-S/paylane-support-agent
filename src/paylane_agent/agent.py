"""Tool-calling conversation loop.

`run_turn` sends the chat to the model, runs any tool calls through
`tools.dispatch` (which enforces policy and logs every call to the session),
feeds the results back, and repeats until the model answers in plain text.
The terminal interface lives in cli.py.
"""

from __future__ import annotations

import json

from . import tools
from .llm import LLM
from .prompt import SYSTEM_PROMPT
from .session import Session

MAX_TOOL_ITERATIONS = 6

CAP_FALLBACK = ("Sorry, I'm having trouble sorting this out right now. "
                "I can connect you with a member of our team if you'd like.")
EMPTY_FALLBACK = "Sorry, could you say that another way?"

_default_llm: LLM | None = None


def run_turn(session: Session, user_message: str, llm=None) -> str:
    """Handle one customer message and return the assistant's reply.

    `llm` is anything with `complete(messages, tools, system) -> LLMResponse`.
    Defaults to a real LLM built from env vars.
    """
    llm = llm or _get_default_llm()
    session.messages.append({"role": "user", "content": user_message})
    turn_start = len(session.tool_call_log)

    iterations = 0
    while True:
        response = llm.complete(session.messages, tools.TOOL_SCHEMAS, SYSTEM_PROMPT)
        if not response.tool_calls:
            text = _with_ticket_reference((response.text or "").strip() or EMPTY_FALLBACK,
                                          session.tool_call_log[turn_start:])
            session.messages.append({"role": "assistant", "content": text})
            return text

        if iterations >= MAX_TOOL_ITERATIONS:
            # Drop the pending tool calls (unanswered calls would break the next request).
            session.log("<iteration_cap>", {"pending": [c.name for c in response.tool_calls]},
                        {"ok": False, "error": "tool iteration cap reached"})
            session.messages.append({"role": "assistant", "content": CAP_FALLBACK})
            return CAP_FALLBACK

        session.messages.append({
            "role": "assistant",
            "content": response.text,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in response.tool_calls
            ],
        })
        for call in response.tool_calls:
            result = _run_tool(session, call.name, call.arguments)
            session.messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
        iterations += 1


def _with_ticket_reference(text: str, turn_log: list[dict]) -> str:
    """The customer always gets their ticket ID. If the model's reply leaves it out, add it."""
    tickets = [e["result"]["ticket_id"] for e in turn_log
               if e["tool"] == "escalate_to_human" and e["result"].get("ticket_id")]
    missing = [t for t in dict.fromkeys(tickets) if t not in text]
    if missing:
        text += f"\n\nYour reference number is {', '.join(missing)}. Please quote it if you contact us again."
    return text


def _run_tool(session: Session, name: str, arguments: str) -> dict:
    """dispatch() logs normal and blocked calls itself; this only catches unexpected crashes."""
    try:
        return tools.dispatch(session, name, arguments)
    except Exception as exc:  # a tool bug must never crash the chat or leak a traceback
        result = {"ok": False, "error": "internal_error", "detail": type(exc).__name__}
        session.log(name, {"raw": arguments}, result)
        return result


def _get_default_llm() -> LLM:
    global _default_llm
    if _default_llm is None:
        _default_llm = LLM()
    return _default_llm


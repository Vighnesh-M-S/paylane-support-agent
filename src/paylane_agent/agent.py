"""Tool-calling conversation loop.

`run_turn` sends the chat to the model, runs any tool calls through
`tools.dispatch` (which enforces policy and logs every call to the session),
feeds the results back, and repeats until the model answers in plain text.
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

    iterations = 0
    while True:
        response = llm.complete(session.messages, tools.TOOL_SCHEMAS, SYSTEM_PROMPT)
        if not response.tool_calls:
            text = (response.text or "").strip() or EMPTY_FALLBACK
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


def main() -> None:
    """Minimal terminal chat: `python -m paylane_agent`. Set PAYLANE_TRACE=1 to print tool calls."""
    import os

    session = Session()
    trace = os.environ.get("PAYLANE_TRACE") == "1"
    print("Paylane support (Ctrl-D to quit)")
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        seen = len(session.tool_call_log)
        try:
            reply = run_turn(session, user)
        except Exception as exc:  # network/auth problems with the LLM endpoint
            print(f"[error talking to model: {type(exc).__name__}: {exc}]")
            continue
        if trace:
            for entry in session.tool_call_log[seen:]:
                print(f"  [tool] {entry['tool']} {entry['args']} -> {entry['result']}"
                      + (f"  VIOLATION={entry['violation']}" if entry["violation"] else ""))
        print(f"agent> {reply}")

"""Thin LLM wrapper: one `complete(messages, tools, system)` call.

Uses the `openai` SDK against any OpenAI-compatible endpoint (OpenAI, Anthropic's
OpenAI-compatible API, a local server, ...). Config comes from env vars only:
OPENAI_API_KEY, OPENAI_BASE_URL, PAYLANE_MODEL. This is the only module that
imports `openai`; tests and evals pass a fake with the same `complete` method.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

TEMPERATURE = 0.0


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string, parsed by tools.dispatch


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLM:
    def __init__(self, model: str | None = None, client=None) -> None:
        self.model = model or os.environ.get("PAYLANE_MODEL")
        if not self.model:
            raise RuntimeError("PAYLANE_MODEL is not set (see .env.example)")
        if client is None:
            from openai import OpenAI  # imported lazily so tests never need it

            # OPENAI_API_KEY / OPENAI_BASE_URL are read from the environment by the SDK.
            client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)
        self.client = client

    def complete(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system}, *messages],
            tools=tools,
            temperature=TEMPERATURE,
        )
        message = response.choices[0].message
        calls = [ToolCall(c.id, c.function.name, c.function.arguments or "{}") for c in (message.tool_calls or [])]
        return LLMResponse(text=message.content, tool_calls=calls)

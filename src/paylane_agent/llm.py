"""Thin LLM wrapper: one `complete(messages, tools, system)` call.

Uses the `openai` SDK against any OpenAI-compatible endpoint (OpenAI, Anthropic's
OpenAI-compatible API, a local server, ...). Config comes from env vars only:
OPENAI_API_KEY, OPENAI_BASE_URL, PAYLANE_MODEL. This is the only module that
imports `openai`; tests and evals pass a fake with the same `complete` method.

Rate limits: by default a 429 is raised straight away. Set PAYLANE_RATE_LIMIT_WAIT
(seconds) to instead sleep for the time the provider asks for ("try again in 8m0.4s")
and retry the same call, up to that total per call. Useful for free tiers whose quota
refills slowly; a long eval can then run in the background.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass, field

TEMPERATURE = 0.0
FALLBACK_WAIT = 30.0  # seconds, when the provider doesn't say how long

_RETRY_IN = re.compile(r"try again in\s+(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?\s*(?:(\d+)ms)?", re.I)


def retry_after_seconds(exc: Exception) -> float | None:
    """How long the provider asked us to wait, from a Retry-After header or the error text."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        if headers.get("retry-after"):
            return float(headers["retry-after"])
    except (TypeError, ValueError):
        pass
    m = _RETRY_IN.search(str(exc))
    if m and any(m.groups()):
        h, mins, secs, ms = m.groups()
        return int(h or 0) * 3600 + int(mins or 0) * 60 + float(secs or 0) + int(ms or 0) / 1000
    return None


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
    def __init__(self, model: str | None = None, client=None, rate_limit_wait: float | None = None,
                 sleep=time.sleep) -> None:
        self.model = model or os.environ.get("PAYLANE_MODEL")
        if rate_limit_wait is None:
            rate_limit_wait = float(os.environ.get("PAYLANE_RATE_LIMIT_WAIT") or 0)
        self.rate_limit_wait = rate_limit_wait
        self._sleep = sleep
        if not self.model:
            raise RuntimeError("PAYLANE_MODEL is not set (see .env.example)")
        if client is None:
            from openai import OpenAI  # imported lazily so tests never need it

            # OPENAI_API_KEY / OPENAI_BASE_URL are read from the environment by the SDK.
            client = OpenAI(base_url=os.environ.get("OPENAI_BASE_URL") or None)
        self.client = client

    def complete(self, messages: list[dict], tools: list[dict], system: str) -> LLMResponse:
        waited = 0.0
        while True:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, *messages],
                    tools=tools,
                    temperature=TEMPERATURE,
                )
                break
            except Exception as exc:
                # Matched by name so this module doesn't need openai imported (tests use fakes).
                if type(exc).__name__ != "RateLimitError":
                    raise
                delay = (retry_after_seconds(exc) or FALLBACK_WAIT) + 1
                if waited + delay > self.rate_limit_wait:
                    raise
                print(f"[rate limited; waiting {delay:.0f}s]", file=sys.stderr, flush=True)
                self._sleep(delay)
                waited += delay
        message = response.choices[0].message
        calls = [ToolCall(c.id, c.function.name, c.function.arguments or "{}") for c in (message.tool_calls or [])]
        return LLMResponse(text=message.content, tool_calls=calls)

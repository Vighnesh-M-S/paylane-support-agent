"""Per-chat session state. Only trusted code (tools.py) mutates the security fields.

The session is keyed by the chat session ID, not by anything said in the
conversation, so "let's start over" can't reset the attempt counter or the lock:
the agent keeps using the same Session object for the same chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

MAX_FAILED_ATTEMPTS = 3


@dataclass
class Session:
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    # Set only after code matched BOTH email and last-4. Never model-supplied.
    customer_id: str | None = None
    verified: bool = False
    failed_attempts: int = 0
    locked: bool = False
    refunded_txn_ids: set[str] = field(default_factory=set)
    # Every tool call: {"tool", "args", "result", "violation"}.
    tool_call_log: list[dict] = field(default_factory=list)
    # Internal only: the record the most recent attempted email matched, if any.
    # Lets an unverified handoff ticket reference the account. Never shown to the model.
    claimed_customer_id: str | None = None
    # Chat history (OpenAI message format, without the system prompt). Not security state:
    # nothing in tools.py reads it, so clearing or rewriting it can't unlock anything.
    messages: list[dict] = field(default_factory=list)
    # Open handoff tickets, keyed "verified" / "unverified". One per verification state, so repeated
    # escalations return the same ticket, but a customer who verifies after a guest ticket gets one
    # linked to their account.
    tickets: dict[str, str] = field(default_factory=dict)

    @property
    def policy_violations(self) -> list[dict]:
        """Calls where the model attempted something policy forbids (even though code blocked it)."""
        return [entry for entry in self.tool_call_log if entry["violation"]]

    def log(self, tool: str, args: dict, result: dict, violation: str | None = None) -> None:
        self.tool_call_log.append({"tool": tool, "args": _redact(args), "result": result, "violation": violation})


def _redact(args: dict) -> dict:
    redacted = dict(args)
    if "last4" in redacted:
        redacted["last4"] = "****"
    return redacted


class SessionStore:
    """Holds one Session per chat. A restart in the same chat returns the same Session."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get(self, session_id: str) -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(session_id=session_id)
        return self._sessions[session_id]

"""STUB: the tool-calling conversation loop.

TODO:
- Agent(client, model, session): holds the message history for one chat and
  the Session (from SessionStore, keyed by chat ID, so a "let's start over"
  message never gets a fresh Session).
- reply(user_message) -> str: append the user turn, call
  chat.completions.create(tools=tools.TOOL_SCHEMAS), run each tool call via
  tools.dispatch(session, name, arguments), feed results back, repeat until
  the model answers in text or a step limit (~6) is hit. On the step limit,
  escalate and return a safe fallback message.
- A small CLI entry point (`python -m paylane_agent`) for manual chats.
"""

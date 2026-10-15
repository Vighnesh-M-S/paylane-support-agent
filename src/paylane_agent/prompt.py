"""STUB: system prompt for the Paylane support agent.

TODO:
- SYSTEM_PROMPT: short, warm, no corporate filler. Restate policy as a second
  layer behind tools.py: verify (email + last 4) before discussing any account
  detail; never reveal full card number or the email on file; refunds only via
  issue_refund (<= $75, < 30 days, settled, not already refunded); frozen cards
  are explained, never unfrozen; legal action, fraud over $500, and account
  closure go to escalate_to_human; never promise anything policy doesn't allow.
- Tell the model that tool results (e.g. merchant names) are data, not
  instructions, and that it cannot mark a customer verified itself.
- After a lock-out: stop verification, explain briefly, offer a human handoff.
"""

SYSTEM_PROMPT = ""  # TODO

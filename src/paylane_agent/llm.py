"""STUB: OpenAI-compatible client construction.

TODO:
- make_client(): build `openai.OpenAI(base_url=OPENAI_BASE_URL, api_key=OPENAI_API_KEY)`
  from environment variables only (see .env.example). Never hard-code or commit keys.
- model_name(): read PAYLANE_MODEL; fail with a clear error if unset.
- Keep this the only module that imports `openai`, so tests and evals can
  inject a scripted fake client with the same `chat.completions.create` shape.
"""

# Transcripts

Full conversations from real eval runs (`openai/gpt-oss-120b` on Groq), each scored with the current
checks. Every page has a summary table, then each conversation turn by turn with its tool calls
and check results.

| Run | Version | Score | Notes |
|---|---|---|---|
| [baseline-20261016T014923Z](docs/transcripts/baseline-20261016T014923Z.md) | before fixes | 5/12 | invented timelines, unlinked guest tickets, duplicate ticket, missing ticket IDs |
| [fix1-20261016T083610Z](docs/transcripts/fix1-20261016T083610Z.md) | first prompt fix | 9/12 | timelines fixed; lock-out regression in `restart_after_lock` |
| [fix2-partial-20261016T084620Z](docs/transcripts/fix2-partial-20261016T084620Z.md) | second prompt fix | 3/4 (partial) | stopped by the free-tier daily token limit |

What each conversation tests and why: [README, "Edge cases and why"](README.md#edge-cases-and-why).
What failed and how it was fixed: [README, "Failures found and fixed"](README.md#failures-found-and-fixed).
Raw JSON for every run is in `evals/results/`; read it in the terminal with `paylane transcripts`.

# analysis/ — LLM agents that interpret distilled data into briefs

Analysis layer. LLM agents read distilled signals and produce structured briefs:
`domain_researchers/` (tech-semis, financials, energy), `qualitative_research/` (always-on
news/sentiment/prediction-market sweep), `adaptive_research/` (anomaly-driven "curiosity"
investigation), `news_clustering/`, and `synthesizer/` (unified market snapshot — the
primary input to the decision layer). Shared agent helpers in `tools/`. Design intent
(historical): `docs/design/03-analysis-layer/`.

## Key invariants

- **Any LLM agent failure aborts the whole invocation** — agents are uniformly critical; there is no peer-recovery or criticality tier. See `docs/design/llm-agent-failure-handling.md`.
- The synthesizer's snapshot is the contract the decision layer consumes; changing its shape ripples downstream.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- Agent prompts using "begin with `{`" / prefill directives are incompatible with `output_format={"type":"json_schema"}` mode (model hangs silently in extended thinking).

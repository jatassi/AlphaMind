# Story-implementer memory

- [Analysis/decision harness telemetry seam](harness_telemetry_seam.md) — DiagState.write() is the single sync funnel for all 9 LLM agents; harnesses run in a per-agent subprocess worker that owns the DB session; provenance is fs-write, agent_calls row is async DB write

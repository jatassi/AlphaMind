---
status: not_started
completed_date:
commit_id:
---

# 02 — Agent configuration scaffolding

## Goal

Land the synthesizer's row in `config/agents.yaml` and the corresponding Pydantic config model so downstream stories (harness, runner) can resolve `AgentConfig` for the synthesizer the same way the domain researchers and decision-layer agents do. Pin model, system-prompt path, latency and token budgets, and tool allowlist.

## Reading

- `docs/design/configuration-management.md` § `agents.yaml` — per-agent configuration shape (`model`, `prompt`, `latency_budget_seconds`, `context_token_budget`, `output_token_budget`, `tools`)
- `docs/architecture/llm-integration.md` § Agent inventory — synthesizer is Sonnet, sequential after parallel analysis group
- `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate — synthesizer sized at ~10,000 input / ~1,500 output tokens normal day; primary anchor for `context_token_budget` / `output_token_budget`
- `docs/design/03-analysis-layer/synthesizer.md` § Portfolio state tools — three tools (`get_positions_summary`, `get_active_theses_summary`, `get_exposure_snapshot`) the synthesizer invokes during reasoning
- `docs/design/03-analysis-layer/synthesizer.md` § Output — the brief is prose, no producer-side IDs; consumed by all three decision-layer agents
- `docs/implementation/03-analysis-layer/domain-researchers/02-agent-configuration-scaffolding.md` — sibling story; same Pydantic and YAML pattern

## Depends on

None.

## Scope

In scope:

- Add a `synthesizer` entry under `config/agents.yaml`'s `agents:` map (creating the file with an `agents:` root if it does not yet exist):
  ```yaml
  agents:
    synthesizer:
      model: claude-sonnet-4-6
      prompt: prompts/analysis/synthesizer.md
      latency_budget_seconds: 120
      context_token_budget: 14000
      output_token_budget: 2000
      tools:
        - get_positions_summary
        - get_active_theses_summary
        - get_exposure_snapshot
  ```
  - `model`: Sonnet per the agent inventory and cost-modeling rationale.
  - `prompt`: relative path; the system prompt file lands in story 09. The config can reference the path before the file exists; the harness loader (story 08) raises a clear error at first load if absent.
  - `latency_budget_seconds: 120`: 2× the typical Sonnet wall-clock for a ~10K-input / ~1.5K-output call, leaving margin against rate-limit re-queuing without false timeouts. Configurable per the runtime-vs-deploy classification — invocation-time reload.
  - `context_token_budget: 14000`: ~10K observed midpoint plus headroom for tool returns (each portfolio tool returns small structured payloads) and cumulative prompt-cache pre-fix overhead.
  - `output_token_budget: 2000`: ~1,500 observed midpoint plus headroom; the harness sets this as `max_tokens` in `ClaudeAgentOptions`.
  - `tools`: only the three portfolio-state read tools — the synthesizer does not call `retrieve_brief` (it produces the source material that powers it).

- Under `src/alphamind/config/models.py`, define / extend the agent-config Pydantic model so the synthesizer entry parses with the same shape used by domain-researchers and decision-layer agents. If the sibling domain-researchers story (02) has already landed an `AgentConfig` model that handles arbitrary agent names, this story only adds tests; if not, this story lands the shared `AgentConfig` model:
  - `model: str` (an enum constraint matching the allowed-models list per `configuration-management.md § Configuration validation strategy` — at minimum `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`).
  - `prompt: str` (a path relative to repo root; the harness resolves against `paths.prompts` from `main.yaml`).
  - `latency_budget_seconds: int` (positive, ≤600).
  - `context_token_budget: int` (positive).
  - `output_token_budget: int` (positive).
  - `tools: tuple[str, ...]` (frozen, ordered as written in YAML).
  - The model is `frozen=True` and uses `pydantic.BaseModel`.

- Unit tests under `tests/config/`:
  - `agents.yaml` parses; the `synthesizer` entry resolves to an `AgentConfig` with the documented field values.
  - Unknown model name fails parse with a clear error naming the field path.
  - Unknown tool name in `tools` is *not* a parse error here — tool registration validation lives in the harness (story 08) and runner (story 10), where the registered-tool set is known. Document this explicitly in the model docstring.
  - Negative or zero `latency_budget_seconds`, `context_token_budget`, `output_token_budget` fail parse.

Out of scope:
- The system prompt file at `prompts/analysis/synthesizer.md` (story 09 lands it).
- The portfolio-state tool implementations (story 06b lands them; this story only declares the names in YAML).
- The harness that consumes `AgentConfig` (story 08).
- Composition resolver (`main.yaml`-driven profile/regime/mode/overlay cascade) — this story populates only the flat `agents.yaml` section.

## Notes

The sibling domain-researchers story 02 may already have landed `AgentConfig`. Verify before introducing a new model — duplicating the type would drift. If `AgentConfig` exists, this story only adds the `synthesizer` row and its parse tests. If it does not, land the shared model under a name that all analysis-layer and (future) decision-layer agents reuse.

Token budgets here are starting points anchored on the cost-modeling midpoints. Operators tune them in YAML at invocation boundaries; no code change required for normal calibration. The Phase 4 feedback loop (`docs/design/feedback-loop.md`) tracks per-invocation token usage so empirical pressure on these numbers surfaces in the dashboard.

The `tools` list pins what the harness wires into `ClaudeAgentOptions(allowed_tools=...)`. The runner (story 10) will fail at startup if any name in this list does not resolve to a registered MCP tool — the validation seam per `configuration-management.md § Configuration validation strategy`. This is by design: a YAML edit that adds a tool the code does not register is configuration drift the validator should catch.

Anti-pattern reminder: do NOT add a `min_findings_required` or similar quality knob here. The synthesizer's only structural enforcement is the Layer-4 stop-reason check per [`llm-output-validation.md`](../../../design/testing/llm-output-validation.md). Per-invocation quality / signal-density expectations live in the system prompt (story 09) and are tracked in the feedback loop, not enforced as config.

## Acceptance criteria

- [ ] `config/agents.yaml` contains a `synthesizer` entry with `model`, `prompt`, `latency_budget_seconds`, `context_token_budget`, `output_token_budget`, and `tools` (the three portfolio-state read tools).
- [ ] Parsing `agents.yaml` produces an `AgentConfig` (or equivalent typed model) for `synthesizer` with the documented field values.
- [ ] Unknown `model` value fails parse with the field-path in the error message.
- [ ] Negative/zero `latency_budget_seconds`, `context_token_budget`, `output_token_budget` fail parse.
- [ ] The `tools` list preserves declared order and is delivered as an immutable sequence.
- [ ] If `AgentConfig` already exists from a sibling story, this story does not duplicate it; if not, this story introduces a shared model the sibling agents can reuse.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

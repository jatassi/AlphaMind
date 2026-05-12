# 01 — Skeleton, agents.yaml verification, shared-types extension

## Goal

Land the small, foundational scaffolding the rest of the qualitative-research work tree imports. Three deliverables: (1) update the `qualitative_researcher` entry in `config/agents.yaml` to carry the agreed tool allowlist and budget fields per the operator-confirmed resolutions baked into this story; (2) promote `SignalQuality` from `alphamind.analysis.domain_researchers.models` to `alphamind.analysis._shared` so the qualitative brief reuses the same enum; (3) create the empty `src/alphamind/analysis/qualitative_research/` and `src/alphamind/analysis/tools/` package skeletons subsequent stories will populate.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § Inputs § On-demand tools — the canonical four-tool list (`news_search`, `social_sentiment`, `prediction_markets`, `earnings_commentary`) and the per-invocation budget framing.
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Token budget — the brief's 300–600 token target.
* `docs/design/cost-and-rate-limit-modeling.md` § Per-invocation token aggregate, § Latency budgets — what `output_token_budget` and `latency_budget_seconds` are supposed to encode.
* `docs/design/configuration-management.md` § Validation at parse time — the `agents.yaml` semantic-self-test invariants.
* `config/agents.yaml` — the as-built `qualitative_researcher` entry; current fields `tools: []`, `output_token_budget: 1000`, `latency_budget_seconds: 180`.
* `src/alphamind/config/models/agents.py` — the schema the yaml validates against (`AgentName`, `BaseAgentConfig`, tool-cap fields).
* `src/alphamind/analysis/_shared.py` — current shared types (`Sector`, `AnomalySeverity`, `TokensUsed`, `_SECTOR_AUDIENCE_MAP`); story extends with `SignalQuality`.
* `src/alphamind/analysis/domain_researchers/models.py` § `SignalQuality` — the `StrEnum` to promote (and the existing call sites that import it).
* `config/agents.yaml` § `adaptive_researcher` entry — the canonical reference shape for `cumulative_tool_call_limit`, `cumulative_tool_token_budget`, and `tool_caps` fields; mirror its style.

## Depends on

* None — this story has no in-tree predecessor and no cross-feature gate. It is the entry point of the work tree.

## Scope

In scope under `src/alphamind/analysis/`, `config/`, and `tests/analysis/qualitative_research/`.

### 1\. Update `config/agents.yaml`

Edit only the `qualitative_researcher` entry. Apply the operator-confirmed resolutions:

* `model: claude-sonnet-4-6` — unchanged.
* `prompt: prompts/analysis/qualitative_researcher.md` — unchanged.
* `latency_budget_seconds: 180` — unchanged.
* `context_token_budget: 8000` — unchanged.
* `output_token_budget: 1000` — **kept**. The design doc names a 300–600 token brief target; the SDK output cap retains 2× headroom because clipping at the cap truncates the response mid-brief and fails parse/validation. Add a one-line yaml comment immediately above the field:

  ```yaml
  # 2× the design-doc 300–600 brief target; SDK cap carries headroom because
  # truncation at the cap fails parse/validation rather than producing a short brief.
  output_token_budget: 1000
  ```
* `tools: [news_search, prediction_markets, earnings_commentary]` — **set**. Three of the design's four tools ship; `social_sentiment` is omitted per ALP-111's cross-feature dependencies (data-layer storage deferred). Add a one-line yaml comment immediately above the field:

  ```yaml
  # social_sentiment deferred per ALP-111 — Qual2 storage not yet provisioned.
  tools: [news_search, prediction_markets, earnings_commentary]
  ```
* `cumulative_tool_call_limit: 15` — **new**. Soft cap matching the design's "typically 5–15 tool calls per invocation" guidance. The agent's closed-scope mandate is the primary bound.
* `cumulative_tool_token_budget: 4000` — **new**. Same shape as the adaptive-researcher entry; bounds the cumulative tool-output payload across all calls in one invocation.
* `tool_caps:` — **new**. Per-tool maximums:

  ```yaml
  tool_caps:
    news_search: 8        # primary investigative tool; highest cap
    prediction_markets: 4 # follow-up on snapshot deltas
    earnings_commentary: 4 # one per universe name reporting in-window
  ```

No other `agents.yaml` entry is touched. No new top-level fields are added.

### 2\. Promote `SignalQuality` to `_shared.py`

Move the `SignalQuality` `StrEnum` (currently in `src/alphamind/analysis/domain_researchers/models.py`) into `src/alphamind/analysis/_shared.py`. Update the `__all__` export list. Update `domain_researchers/models.py` to import `SignalQuality` from `_shared` instead of redefining; ensure all existing call sites (parser, validator) continue to resolve. Run the existing domain-researcher test suite to confirm no regression.

### 3\. Create package skeletons

Create empty package init files at:

* `src/alphamind/analysis/qualitative_research/__init__.py` — already exists per the worktree filesystem snapshot, leave as-is if so.
* `src/alphamind/analysis/tools/__init__.py` — new. The shared tool implementations (story 03e) live here so they are importable by both this work tree and the future adaptive-research work tree.
* `tests/analysis/qualitative_research/__init__.py` — new (test package for the work tree's tests).
* `tests/analysis/tools/__init__.py` — new.

No code in any of these `__init__.py` beyond an optional one-line docstring.

### Out of scope

* Implementing the `QualitativeBrief` data model — story 02.
* Writing any of the four tool implementations — story 03e.
* Editing `prompts/analysis/qualitative_researcher.md` — story 04c.

## Acceptance criteria

- [ ] `config/agents.yaml` `qualitative_researcher.tools` is exactly `[news_search, prediction_markets, earnings_commentary]`.
- [ ] `config/agents.yaml` `qualitative_researcher.cumulative_tool_call_limit` is `15`, `cumulative_tool_token_budget` is `4000`, and `tool_caps` carries `news_search: 8`, `prediction_markets: 4`, `earnings_commentary: 4`.
- [ ] `config/agents.yaml` `qualitative_researcher.output_token_budget` is `1000` with the yaml comment naming the headroom rationale.
- [ ] `alphamind.analysis._shared.SignalQuality` is importable as `from alphamind.analysis._shared import SignalQuality`.
- [ ] `alphamind.analysis.domain_researchers.models.SignalQuality` re-exports the `_shared` enum (or the import is updated so existing call sites keep working without redefinition).
- [ ] `src/alphamind/analysis/tools/__init__.py` exists.
- [ ] `tests/analysis/qualitative_research/__init__.py` and `tests/analysis/tools/__init__.py` exist.
- [ ] The existing domain-researcher test suite (`tests/analysis/domain_researchers/`) passes unchanged under `uv run pytest tests/analysis/domain_researchers/ -n auto`.
- [ ] The new yaml validates: `uv run python -c "from alphamind.config.bootstrap import load_config; load_config()"` exits zero.

## Verification

Run `uv run pytest tests/analysis/ -n auto` to confirm the existing analysis-layer tests remain green after the `SignalQuality` move. Run `uv run ruff check . && uv run ruff format --check . && uv run mypy` for lint and type compliance. Inspect the diff to `config/agents.yaml` to confirm only the `qualitative_researcher` entry changed and that the two yaml comments are present.

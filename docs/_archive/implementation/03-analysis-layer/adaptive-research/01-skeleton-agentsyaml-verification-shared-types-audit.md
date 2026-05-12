# 01 — Skeleton, agents.yaml verification, shared-types audit

## Goal

Land the small, foundational scaffolding the rest of the adaptive-research work tree imports. Three deliverables: (1) edit the `adaptive_researcher` entry in `config/agents.yaml` to strike the two deferred tools (`social_sentiment`, `options_flow`) from both `tools` and `tool_caps`, leaving exactly seven tools per the parent issue's resolutions (A) and (B); (2) verify the `src/alphamind/analysis/adaptive_research/` package skeleton is in place and create the test-package skeleton; (3) audit `alphamind.analysis._shared` and confirm it already exports every shared type the adaptive-research work tree will need (no promotion required — `Sector`, `AnomalySeverity`, `TokensUsed` already exist; document this finding so subsequent stories don't re-derive it).

## Reading

* `docs/design/03-analysis-layer/adaptive-research.md` § Tool inventory — the canonical nine-tool list and per-tool rate limits.
* `docs/design/03-analysis-layer/adaptive-research.md` § Bounded search — cumulative budget framing (`cumulative_tool_call_limit`, `cumulative_tool_token_budget`) and the per-trigger override pattern.
* `docs/design/configuration-management.md` § Validation at parse time — the `agents.yaml` semantic-self-test invariants the Pydantic models enforce.
* `config/agents.yaml` § `adaptive_researcher` entry — the as-built shape (nine tools listed); the edit strikes two of them.
* `src/alphamind/config/models/agents.py` § `AgentName`, `BaseAgentConfig`, `AdaptiveAgentConfig`, `_TOOL_LOOP_AGENTS` — the schema the yaml validates against. `adaptive_researcher` is already a tool-loop slot.
* `src/alphamind/analysis/_shared.py` — the shared types (`Sector`, `AnomalySeverity`, `TokensUsed`, `SignalQuality`, `_SECTOR_AUDIENCE_MAP`); story audits whether anything else needs promotion.
* `src/alphamind/analysis/qualitative_research/` — sibling-package layout to mirror; same `__init__.py` conventions.

## Depends on

* None — this story has no in-tree predecessor and no cross-feature gate. It is the entry point of the work tree.

## Scope

In scope under `config/`, `src/alphamind/analysis/adaptive_research/`, and `tests/analysis/adaptive_research/`.

### 1\. Update `config/agents.yaml`

Edit only the `adaptive_researcher` entry. Apply the operator-confirmed resolutions baked into the parent issue's "Pre-resolved configuration decisions" (A) and (B):

* `model: claude-sonnet-4-6` — unchanged.
* `prompt: prompts/analysis/adaptive_researcher.md` — unchanged.
* `latency_budget_seconds: 300` — unchanged.
* `context_token_budget: 4000` — unchanged.
* `output_token_budget: 1500` — unchanged.
* `tools:` — **edit**. Strike `social_sentiment` and `options_flow`; leave the remaining seven in the same yaml-list form. Add a one-line yaml comment immediately above the field:

  ```yaml
  # social_sentiment + options_flow deferred — Qual2 + Q2 storage not yet provisioned.
  tools:
    - news_search
    - ticker_deep_pull
    - prediction_markets
    - sec_lending
    - short_interest
    - earnings_calendar
    - macro_data
  ```
* `cumulative_tool_call_limit: 25` — **unchanged**. Design-doc cap; per-tool caps below sum higher (the cap is the binding aggregate constraint).
* `cumulative_tool_token_budget: 4000` — unchanged.
* `tool_caps:` — **edit**. Drop the `social_sentiment` and `options_flow` keys; keep the remaining seven exactly as currently set:

  ```yaml
  tool_caps:
    news_search: 10
    ticker_deep_pull: 5
    prediction_markets: 5
    sec_lending: 3
    short_interest: 3
    earnings_calendar: 5
    macro_data: 5
  ```

No other `agents.yaml` entry is touched. No new top-level fields are added.

### 2\. Verify package skeleton and create test-package skeleton

Confirm the existing skeleton at `src/alphamind/analysis/adaptive_research/__init__.py` is in place (an empty file is fine — story 02 onwards populates the package). Create the test-package skeleton:

* `tests/analysis/adaptive_research/__init__.py` — new (empty).
* `tests/analysis/tools/test_sec_lending.py`, `test_short_interest.py`, `test_earnings_calendar.py`, `test_macro_data.py`, `test_ticker_deep_pull.py` — **NOT** created here; tests for new tools land alongside the tool implementations in stories 03d and 03e.

### 3\. Audit `_shared.py`

Inspect `src/alphamind/analysis/_shared.py` and confirm it already exports the four shared symbols the adaptive-research work tree imports across stories: `Sector` (used in the `InvestigationThread.sector` field per the design doc's output schema), `AnomalySeverity` (used by the loaders' `SectorAnomalyRecord`), `TokensUsed` (used by the harness and runner), and the existing `SignalQuality` (NOT used by the adaptive brief — the design's three-way `Assessment` enum supersedes it for adaptive output).

Add a one-line module docstring update to `_shared.py` naming `adaptive_research` in the list of consumer sub-packages — the docstring already names `domain_researchers, qualitative_research, adaptive_research, synthesizer` so this may already be present; verify and leave as-is if so.

No new symbols are added to `_shared.py` in this story. The adaptive-specific enums (`Assessment`, `Confidence`) are local to `adaptive_research.models` and land in story 02.

### Out of scope

* Implementing the `AdaptiveBrief` data model — story 02.
* Writing any of the seven tool implementations — stories 03d, 03e, 04a (registry).
* Editing `prompts/analysis/adaptive_researcher.md` — already production-quality; no story in this work tree edits it.
* Editing `_shared.py` to add new symbols — none needed.

## Acceptance criteria

- [ ] `config/agents.yaml` `adaptive_researcher.tools` is exactly the seven-element list `[news_search, ticker_deep_pull, prediction_markets, sec_lending, short_interest, earnings_calendar, macro_data]` (in any consistent yaml-list shape).
- [ ] `config/agents.yaml` `adaptive_researcher.tool_caps` keys are exactly the same seven names (no `social_sentiment`, no `options_flow`).
- [ ] `config/agents.yaml` `adaptive_researcher.cumulative_tool_call_limit` is `25` and `cumulative_tool_token_budget` is `4000`.
- [ ] The yaml comment above `tools:` names both deferred tools and the storage reason.
- [ ] `src/alphamind/analysis/adaptive_research/__init__.py` exists (may be empty).
- [ ] `tests/analysis/adaptive_research/__init__.py` exists (may be empty).
- [ ] `from alphamind.analysis._shared import Sector, AnomalySeverity, TokensUsed, SignalQuality` resolves without error.
- [ ] The yaml validates: `uv run python -c "from alphamind.config.bootstrap import load_config; load_config()"` exits zero.
- [ ] Existing test suites pass unchanged: `uv run pytest tests/analysis/ tests/config/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` all clean.

## Verification

Inspect the diff to `config/agents.yaml` to confirm only the `adaptive_researcher` entry changed and that `social_sentiment` and `options_flow` no longer appear anywhere in that entry. Inspect `_shared.py` to confirm no new symbols were added. Run the full analysis-layer + config test suites to confirm no regression from the yaml shape change.
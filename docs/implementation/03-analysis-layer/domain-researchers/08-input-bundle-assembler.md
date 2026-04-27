---
status: not_started
completed_date:
commit_id:
---

# 08 — Per-sector input bundle assembler

## Goal

Implement the function that composes the user-message text the harness (story 07) sends to a domain researcher: the sector's slice of the distillation output, the sector-specific qualitative input, and the universal volatility regime label. Output is a single deterministic string the LLM reads as its full input.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — the three input rows (distillation slice, sector qualitative, regime label)
- `docs/design/03-analysis-layer/domain-researchers/financials.md` § Inputs — same three rows; sector prefix differs
- `docs/design/03-analysis-layer/domain-researchers/energy.md` § Inputs — same three rows; sector prefix differs
- `docs/design/02-distillation-layer/external.md` § Output format — the per-sector partition (`OutputAudience.SECTOR_TECH_SEMIS` / `SECTOR_FINANCIALS` / `SECTOR_ENERGY`)
- `docs/implementation/02-distillation-layer/external/11a-sector-output-assembly.md` — `SectorOutput` dataclass shape (the input this assembler reads from)
- `docs/implementation/02-distillation-layer/external/05-output-envelope.md` — block format / determinism guarantee on the distillation side

## Depends on

- 03 (`Sector` enum)
- 06 (sector-qualitative input loader — `SectorQualitativeInput`)
- The `SectorOutput` dataclass from distillation story 11a (the distillation output type passed in here). If the distillation work tree has not yet landed `SectorOutput`, this story declares a structural type protocol matching the documented shape and the orchestrator wires the actual distillation output in once available.

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `input_bundle.py` defining:
  - `class RegimeLabel(BaseModel, frozen=True)` — minimal accessor over the universal regime broadcast carrying:
    - `label: Literal["low_vol_compression", "vol_expansion", "crisis_spike", "vol_normalization"]`
    - `transition_state: Literal["stable", "early-weak", "early-strong", "confirmed"]`
    - `prior_label: str | None` (most recent prior label for transitions)
    Sourced from `DistillationOutputs.universal_regime_label` per distillation story 12.
  - `class InputBundle(BaseModel, frozen=True)` — the composed bundle as a value object (also used for diagnostic preservation):
    - `sector: Sector`
    - `invocation_id: str`
    - `as_of: datetime` (UTC, tz-aware)
    - `regime: RegimeLabel`
    - `distillation_text: str` (the rendered distillation slice — already structured text per distillation story 05's formatter)
    - `qualitative_text: str` (the rendered qualitative slice — produced by this story's renderer)
    - `bundle_text: str` (the full concatenated user message ready for the LLM)
  - `assemble_input_bundle(sector: Sector, invocation_id: str, as_of: datetime, distillation_output_text: str, qualitative_input: SectorQualitativeInput, regime: RegimeLabel) -> InputBundle` — pure function. Composes `bundle_text` per the deterministic format below.
- Bundle text format (deterministic, byte-stable on identical inputs):

  ```
  # SECTOR RESEARCHER INPUT BUNDLE
  Invocation: {invocation_id}
  Sector: {sector enum value}
  As of: {as_of ISO 8601 UTC}

  ## VOLATILITY REGIME (universal context)
  Label: {regime.label}
  Transition state: {regime.transition_state}
  {Prior label: {regime.prior_label} when transition_state != "stable"}

  ## DISTILLATION OUTPUT (sector slice)
  {distillation_output_text — passed through verbatim}

  ## SECTOR-SPECIFIC QUALITATIVE INPUT
  Lookback window: last {lookback_window_hours} hours
  Data freshness: {qualitative_input.data_freshness ISO 8601 UTC}

  ### HEADLINES (top {N} by composite score)
  [{i}] {published_at ISO 8601} [{tier}] {headline}
        Source: {source} | Tickers: {ticker list} | Tags: {tag list} | High priority: {true/false}
  ...

  ### SCHEDULED EVENTS (next 72h)
  - {event_time ISO 8601} | {event_name} | sectors: {sector list} | tickers: {ticker list or "(macro)"} | consensus: {consensus or "(none)"}
  ...
  ```

- Determinism:
  - The bundle is byte-identical when called with byte-identical inputs (load-bearing for invocation-archive diffability).
  - Iterate ordered collections in the order they arrive (the qualitative loader already orders headlines and events deterministically). Do not re-sort here.
  - Use UTC timestamps consistently; do not convert to local time anywhere.
- Empty-section handling:
  - Empty `headlines` tuple renders the section with a `(no qualifying headlines in window)` placeholder line — preferable to omitting the header (the LLM benefits from knowing the loader ran and found nothing, distinguishing this from an upstream omission).
  - Empty `events` tuple renders `(no scheduled events in 72h window)` similarly.
- Unit tests:
  - Identical inputs produce byte-identical `bundle_text` across repeated calls.
  - Each section is rendered with the documented heading format.
  - `Prior label:` line appears only when `transition_state != "stable"`.
  - Empty headlines / events render the placeholder lines.
  - The output is wrapped in the expected outer `# SECTOR RESEARCHER INPUT BUNDLE` heading.
  - A snapshot test against a canonical fixture asserts the full byte string for a representative non-empty bundle.

Out of scope:
- The harness call that consumes the bundle (story 07).
- The distillation slice retrieval — this story takes the rendered text as input. The orchestrator (story 12 of distillation) produces the per-sector output as already-formatted text; the per-sector runner (story 10 of this work tree) passes that text through.
- Truncation / token-budgeting on the bundle — the qualitative loader already caps headlines via `max_headlines`; the distillation slice is already bounded by the distillation layer's own output sizing.
- The system prompt — that is delivered separately via `ClaudeAgentOptions(system_prompt=...)` in story 07.

## Notes

The bundle is the LLM's *user* message; the system prompt is delivered separately. Per `llm-integration.md § Prompt management`: "Each file contains the agent's role definition, output format, and any few-shot examples, passed via `ClaudeAgentOptions(system_prompt=...)`. Data payloads (distillation output, briefs, portfolio state) go into the user message — system prompt stable across invocations while data varies." This story produces the data payload only.

The decision to pass `distillation_output_text` as already-rendered text rather than as a structured `SectorOutput` object: the distillation layer's story 05 formatter (`format_block`) is the canonical text renderer for distillation output; calling it from here would couple this work tree to the distillation module's internal API. Passing through pre-rendered text keeps the contract narrow — what the assembler needs is "a chunk of structured text for the sector slice."

The qualitative renderer is implemented inside this story rather than inside the qualitative-input loader (story 06) because the rendering format is consumer-specific. Story 06 produces the structured value object; story 08 renders it for the domain researcher's consumption. A future qualitative-researcher work tree may render the same value object differently for its own consumer.

The `RegimeLabel` shape is a minimal subset of the distillation regime payload — just what the universal-context broadcast specifies. The full regime payload (multipliers, persistence-window state) is the strategist's / PM's territory and is not delivered to analysis-layer agents.

## Acceptance criteria

- [ ] `RegimeLabel` and `InputBundle` defined as frozen Pydantic models.
- [ ] `assemble_input_bundle(...)` returns an `InputBundle` whose `bundle_text` follows the documented format.
- [ ] Byte-identical inputs produce byte-identical `bundle_text` (verified by a deterministic snapshot test).
- [ ] The bundle includes the four sections in declared order: header, regime, distillation, qualitative.
- [ ] `Prior label:` appears only when `transition_state != "stable"`.
- [ ] Empty headlines render `(no qualifying headlines in window)`; empty events render `(no scheduled events in 72h window)`.
- [ ] All timestamps are ISO 8601 UTC; no local-time conversion anywhere.
- [ ] A snapshot test asserts the full bundle text against a canonical fixture for at least one non-empty case.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

## Goal

Implement the function that composes the user-message text the harness (story 07) sends to a domain researcher: the sector's slice of the distillation output (already assembled as text by `assemble_sector_output`) plus the rendered sector-specific qualitative input. Output is a single deterministic string the LLM reads as its full input.

The volatility regime label is **not** rendered separately — it is already inside `SectorOutput.text` as a UNIVERSAL_BROADCAST block per `assemble_sector_output`. Re-rendering the regime would double-emit the label. This story wraps the existing distillation text rather than re-decorating it.

## Reading

* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — the three input categories (distillation slice, sector qualitative, regime).
* `docs/design/03-analysis-layer/domain-researchers/financials.md` § Inputs — same three categories.
* `docs/design/03-analysis-layer/domain-researchers/energy.md` § Inputs — same three categories.
* `src/alphamind/distillation/sector_assembly.py` — `assemble_sector_output` produces `SectorOutput`; `SectorOutput.text` carries the per-sector document the LLM consumes (header + anomaly summary + universal-broadcast blocks INCLUDING regime + sector indicators).
* `src/alphamind/distillation/output.py` — `OutputAudience`, `format_blocks_for_audience`, the deterministic-text formatter contract.
* `src/alphamind/distillation/orchestrator.py` — `DistillationOutputs.sector_outputs[<audience>]` is the runtime source of `SectorOutput`.
* `src/alphamind/distillation/regime.py` — `RegimeLabel` StrEnum (already defined; this story does NOT redefine).
* `src/alphamind/analysis/_shared.py` (story 02) — `Sector`, `_SECTOR_AUDIENCE_MAP` reused here.
* Story 06 (<issue id="a98250f2-f164-408f-9ba2-3c253f3724ab">ALP-189</issue>) — `SectorQualitativeInput` value object this story renders.

## Depends on

* **02** (<issue id="d47bb088-b447-4f25-b67c-67d73739dad4">ALP-133</issue>) — `Sector`, `_SECTOR_AUDIENCE_MAP`.
* **03** (<issue id="531b4239-e25d-4f76-8eee-3270d1be6c6b">ALP-187</issue>) — `Sector` is imported via the shared module, but the brief models are not consumed here.
* **06** (<issue id="a98250f2-f164-408f-9ba2-3c253f3724ab">ALP-189</issue>) — `SectorQualitativeInput`, `HeadlineEntry`, `EventEntry`.

## Naming clarification — RegimeContext, not RegimeLabel

`RegimeLabel` is already a `StrEnum` in `alphamind.distillation.regime`. Stories that need a richer regime payload value object name it `RegimeContext`, not `RegimeLabel`. The `RegimeContext` shape mirrors `DistillationOutputs.universal_regime_label` — a dict with the four-tier label, the transition state, the prior label, and supporting indicators.

This story does **not define** `RegimeContext` as a new type. The bundle assembler accepts `regime_payload: Mapping[str, Any]` directly (the same shape the distillation orchestrator returns). If a future caller wants stricter typing, they can wrap the dict in a Pydantic model named `RegimeContext`; this story's contract is the dict.

## Scope — input_bundle.py

Under `src/alphamind/analysis/domain_researchers/input_bundle.py`:

```python
class InputBundle(BaseModel, frozen=True):
    """The composed bundle as a value object; also used for diagnostic preservation."""
    sector: Sector
    invocation_id: str
    as_of: datetime
    distillation_text: str    # SectorOutput.text — passed through verbatim
    qualitative_text: str     # rendered by this module
    bundle_text: str          # full concatenated user message ready for the LLM


def assemble_input_bundle(
    *,
    sector: Sector,
    invocation_id: str,
    as_of: datetime,
    distillation_text: str,
    qualitative_input: SectorQualitativeInput,
) -> InputBundle:
    ...
```

The function is pure — no I/O, no logging, deterministic. The caller (story 10's runner) extracts `distillation_text` from `DistillationOutputs.sector_outputs[_SECTOR_AUDIENCE_MAP[sector]].text` and passes it through.

## Scope — bundle text format

The bundle is two sections in declared order, each preceded by a markdown H2 header:

```
# SECTOR RESEARCHER INPUT BUNDLE
Invocation: {invocation_id}
Sector: {sector enum value}
As of: {as_of ISO 8601 UTC}

## DISTILLATION OUTPUT (sector slice — includes regime, anomaly summary, universal context, sector indicators)

{distillation_text — passed through verbatim from SectorOutput.text}

## SECTOR-SPECIFIC QUALITATIVE INPUT
Lookback window: last {lookback_window_hours} hours
Data freshness: {qualitative_input.data_freshness ISO 8601 UTC}

### HEADLINES (top {N} by composite score)
[1] {published_at ISO 8601} [{tier}] {headline}
      Outlet: {source_outlet} | Tickers: {ticker list} | Tags: {tag list}
[2] ...

### SCHEDULED EVENTS (next 72h)
- {event_time ISO 8601} | {event_name} | sectors: {sector list} | tickers: {ticker list or "(macro)"} | consensus: {consensus or "(none)"}
- ...
```

The H1 header is the bundle marker. The `## DISTILLATION OUTPUT` section is the verbatim `distillation_text` — `SectorOutput.text` already carries its own subsections (e.g., `=== UNIVERSAL CONTEXT ===`, `=== SECTOR INDICATORS ===`); do not re-render or re-wrap them. The `## SECTOR-SPECIFIC QUALITATIVE INPUT` section is the only thing this module renders from scratch.

## Scope — determinism

The bundle is byte-identical when called with byte-identical inputs (load-bearing for invocation-archive diffability). Iterate ordered collections in the order they arrive — the qualitative loader already orders headlines and events deterministically; do not re-sort here. Use UTC timestamps consistently; do not convert to local time anywhere.

## Scope — empty-section handling

Empty `headlines` tuple renders the section with a `(no qualifying headlines in window)` placeholder line — preferable to omitting the header (the LLM benefits from knowing the loader ran and found nothing, distinguishing this from an upstream omission). Empty `events` tuple renders `(no scheduled events in 72h window)` similarly.

## Scope — unit tests

Under `tests/analysis/domain_researchers/test_input_bundle.py`:

* Identical inputs produce byte-identical `bundle_text` across repeated calls.
* The bundle's H1 header line is `# SECTOR RESEARCHER INPUT BUNDLE`.
* The `## DISTILLATION OUTPUT` section contains the `distillation_text` argument verbatim (no transformation, no wrapping).
* No `## VOLATILITY REGIME` section is rendered separately — the regime is inside `distillation_text`. A test asserting that the bundle contains the substring "VOLATILITY REGIME" only when `distillation_text` already contains it (i.e., this module never adds its own regime section).
* Empty headlines render `(no qualifying headlines in window)`; empty events render `(no scheduled events in 72h window)`.
* All timestamps in the qualitative section are ISO 8601 UTC.
* A snapshot test asserts the full bundle text against a canonical fixture for at least one non-empty case.

## Out of scope

* The harness call that consumes the bundle — story 07.
* The distillation slice retrieval — `distillation_text` is passed in already-rendered. Story 10's runner pulls it from `DistillationOutputs.sector_outputs[<audience>].text`.
* Truncation / token-budgeting on the bundle — the qualitative loader already caps headlines via `max_headlines`; the distillation slice is bounded by the distillation layer's own output sizing.
* The system prompt — delivered separately via `ClaudeAgentOptions(system_prompt=...)` in story 07.
* Re-rendering the regime label, anomaly summary, or universal context blocks — they are already inside `distillation_text`.

## Notes

The bundle is the LLM's *user* message; the system prompt is delivered separately. Per `llm-integration.md § Prompt management`: "Each file contains the agent's role definition, output format, and any few-shot examples, passed via `ClaudeAgentOptions(system_prompt=...)`. Data payloads (distillation output, briefs, portfolio state) go into the user message — system prompt stable across invocations while data varies." This story produces the data payload only.

The decision to consume `SectorOutput.text` verbatim rather than re-render from blocks: the distillation layer's `assemble_sector_output()` already does the heavy lifting of partitioning blocks by audience, restricting per-ticker payloads to the sector roster, and rendering deterministically. Calling this assembler from here would couple the analysis layer to the distillation rendering pipeline. Consuming the rendered text keeps the contract narrow — what the assembler needs is "structured text for the sector slice."

The qualitative renderer is implemented inside this story rather than inside the qualitative-input loader (story 06) because the rendering format is consumer-specific. Story 06 produces the structured value object; story 08 renders it for the domain researcher's consumption. A future qualitative-researcher work tree may render the same value object differently for its own consumer.

## Acceptance criteria

- [ ] `InputBundle` is a frozen Pydantic model with the documented fields.
- [ ] `assemble_input_bundle(sector, invocation_id, as_of, distillation_text, qualitative_input)` returns an `InputBundle`.
- [ ] The function is pure (no I/O, no logging, deterministic) — verified by re-call producing identical results.
- [ ] The bundle includes the H1 header and the two H2 sections in declared order: distillation, qualitative.
- [ ] `distillation_text` is passed through verbatim into the `## DISTILLATION OUTPUT` section.
- [ ] No separate regime / volatility / universal-context section is rendered by this module.
- [ ] Empty headlines render `(no qualifying headlines in window)`; empty events render `(no scheduled events in 72h window)`.
- [ ] All timestamps emitted by this module are ISO 8601 UTC; no local-time conversion.
- [ ] A snapshot test asserts the full bundle text against a canonical fixture for at least one non-empty case.
- [ ] No `RegimeLabel` or `RegimeContext` Pydantic class is defined in this module.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.

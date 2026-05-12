# 04b — Input-bundle assembler

## Goal

Implement the function that composes the user-message text the harness (story 05) sends to the adaptive researcher. The bundle assembles four sections in deterministic order: header (invocation metadata + as_of), volatility regime (rendered from the upstream `universal_regime_label` payload), distillation anomaly stream (rendered from `AdaptiveAnomalyInputs.distillation`), and sector-researcher anomaly stream (rendered from `AdaptiveAnomalyInputs.sector`). Pure function — no I/O, no logging, no LLM involvement. Output is a single deterministic string the LLM reads as its full input plus an `InputBundle` value object preserving each rendered section for the diagnostic archive.

## Reading

* `docs/design/03-analysis-layer/adaptive-research.md` § Inputs — names the three input categories (distillation flags, sector anomalies, regime label) and the design intent that the agent triages from the surfaced raw streams.
* `prompts/analysis/adaptive_researcher.md` § `<inputs>` — names the user-turn shape the prompt expects: regime label first, then distillation anomalies, then sector anomalies with their `[SA-*-ANOM-*]` IDs and suggested questions.
* `src/alphamind/analysis/qualitative_research/input_bundle.py` — sibling-package assembler pattern. Mirror: `InputBundle` value-object shape carrying both the per-section text and the assembled bundle text, the `_render_*` helper convention, the deterministic-string discipline (no timestamps from `datetime.now()`, no random ordering).
* `src/alphamind/analysis/adaptive_research/loaders.py` § `AdaptiveAnomalyInputs`, `DistillationAnomalyRecord`, `SectorAnomalyRecord` — typed inputs.
* `src/alphamind/distillation/orchestrator.py` § `DistillationOutputs.universal_regime_label` — the regime payload structure (free-shape `dict[str, Any]`; key fields the renderer reads are the regime name, transition flag, and freshness ts where present).

## Depends on

* <issue id="eca19eb3-3d7a-43c6-8ae3-72266ae37aee">ALP-256</issue> (story 03a) — the typed input records this assembler renders.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/input_bundle.py` and `tests/analysis/adaptive_research/test_input_bundle.py`.

### 1\. `InputBundle` value object

```python
class InputBundle(BaseModel, frozen=True):
    """Composed bundle as a value object; preserved for the diagnostic archive."""

    invocation_id: str
    as_of: datetime
    regime_text: str
    distillation_text: str
    sector_text: str
    bundle_text: str
```

### 2\. `assemble_input_bundle` function

```python
def assemble_input_bundle(
    *,
    invocation_id: str,
    as_of: datetime,
    regime_label: dict[str, Any],
    anomaly_inputs: AdaptiveAnomalyInputs,
) -> InputBundle:
    """Compose and return the user-message bundle.

    Pure function — no I/O, no logging, deterministic. The `as_of` timestamp
    is rendered as a UTC ISO 8601 string in the header; no other source of
    wall-clock time enters the rendered text.
    """
```

### 3\. Per-section renderers

`_render_header(invocation_id, as_of)` — produces:

```
=== ADAPTIVE RESEARCH INPUT (invocation {invocation_id}, as_of {as_of_utc_iso}) ===
```

`_render_regime(regime_label)` — produces:

```
=== VOLATILITY REGIME ===
Regime: {regime_label["regime"]}
Transition: {regime_label["transition_flag"]}
Confidence: {regime_label["confidence"]}
Freshness: {regime_label["freshness_ts"]}
```

Field handling: read each key from `regime_label` with a default of `"unknown"` when missing — the regime payload's exact shape is the upstream contract; this renderer surfaces what's present without raising on absent keys. Keep the four fields above as the canonical render set; add new fields when the upstream contract evolves.

`_render_distillation(records)` — produces:

```
=== DISTILLATION ANOMALY FLAGS ({N} flags) ===
[D-{i}] block={block_id} flag={flag_name} magnitude={magnitude:.2f} severity={severity}
       freshness={freshness_iso} regime_context={regime_context_or_none}
[D-{i+1}] ...
```

Index `i` starts at 1 and is sequential within the section. The `[D-N]` prefix is for human readability in the diagnostic archive — it is NOT a reference ID the LLM cites in its output (the design doc names `Distillation: ...` as the free-form trigger format the LLM uses, not `[D-N]`). When `records=()`, render `=== DISTILLATION ANOMALY FLAGS (0 flags) ===\n(none)`.

`_render_sector(records)` — produces:

```
=== SECTOR-RESEARCHER ANOMALIES ({N} anomalies) ===
[{anomaly_id}] sector={sector} type={anomaly_type} severity={severity}
  Tickers: {ticker_csv or "none"}
  Description: {description}
  Suggested question: {suggested_question}
[{anomaly_id_next}] ...
```

The `[anomaly_id]` IS a real reference ID the LLM cites in its `Trigger:` field (e.g., `[SA-TECH-ANOM-1]`). Render in the order the records appear in `records`. When `records=()`, render `=== SECTOR-RESEARCHER ANOMALIES (0 anomalies) ===\n(none)`.

### 4\. `_render_bundle` composer

Joins the four rendered sections with a single blank line between sections. The final `bundle_text` ends with a single trailing newline.

```python
def _render_bundle(*, header_text, regime_text, distillation_text, sector_text) -> str:
    return "\n\n".join([header_text, regime_text, distillation_text, sector_text]) + "\n"
```

### 5\. Module exports

```python
__all__ = ["InputBundle", "assemble_input_bundle"]
```

### Out of scope

* The harness — story 05.
* The runner — story 06.
* News-digest rendering, sentiment rendering — qualitative-research's territory; the adaptive bundle has no equivalent of the qualitative bundle's news/sentiment/calendar/thesis sections.
* Anomaly-stream loaders — story 03a (consumed here).

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.input_bundle import InputBundle, assemble_input_bundle` resolves cleanly.
- [ ] `assemble_input_bundle` is a pure function: calling twice with the same inputs returns byte-equal `bundle_text`.
- [ ] The `bundle_text` opens with `=== ADAPTIVE RESEARCH INPUT (invocation ...)`.
- [ ] The `bundle_text` contains exactly four `===`-prefixed section markers in order: input header, volatility regime, distillation anomaly flags, sector-researcher anomalies.
- [ ] An empty distillation stream renders the `(none)` placeholder under the section marker, NOT an absent section.
- [ ] An empty sector stream renders the `(none)` placeholder under the section marker, NOT an absent section.
- [ ] A `regime_label` dict missing the `confidence` field renders `Confidence: unknown` (or the documented default) without raising.
- [ ] Each `[SA-*-ANOM-*]` reference ID from the input `SectorAnomalyRecord`s appears literally in the rendered `sector_text` under its own bracketed header.
- [ ] No `datetime.now()` or other live-clock source is invoked anywhere in this module.
- [ ] `InputBundle.bundle_text` equals the deterministic composition of `header_text + "\\n\\n" + regime_text + "\\n\\n" + distillation_text + "\\n\\n" + sector_text + "\\n"`.
- [ ] `tests/analysis/adaptive_research/test_input_bundle.py` covers each acceptance criterion above and passes under `uv run pytest tests/analysis/adaptive_research/test_input_bundle.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/adaptive_research/test_input_bundle.py -n auto`. Construct a fixture invocation with a populated `regime_label`, a non-empty distillation stream, and a non-empty sector stream; assert the rendered `bundle_text` is byte-equal across two calls (determinism), and that the four section markers appear in the documented order.
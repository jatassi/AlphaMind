# 05 — Input bundle assembler

## Goal

Implement the function that composes the user-message text the harness (story 04b) sends to the qualitative researcher: header (regime label, invocation metadata), the rendered news digest (story 04a's `digest_text`), and rendered sections for each of the four loader outputs (story 03d's `QualitativeInputs`). Output is a single deterministic string the LLM reads as its full input. Pure function — no I/O, no logging, no LLM involvement.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § Inputs § In-context data — the six in-context blocks (regime, news digest, sentiment aggregates, prediction-market snapshot, event calendar, active thesis summaries) and per-block budget framing.
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Schema design rationale § Evidence with source attribution — the agent must cite `[ND-T3]` style references; the bundle's news digest section provides the reference IDs.
* `prompts/analysis/qualitative_researcher.md` § `<inputs>` — the order and labeling of the in-context blocks the agent expects to read.
* `src/alphamind/analysis/domain_researchers/input_bundle.py` — the canonical analog. Mirror the deterministic-rendering discipline, the `BaseModel(frozen=True)` value-object pattern (`InputBundle`), and the per-section helper functions (`_render_*`).
* `src/alphamind/analysis/qualitative_research/news_digest.py` (story 04a) — `NewsDigest.digest_text` is consumed verbatim.
* `src/alphamind/analysis/qualitative_research/loaders.py` (story 03d) — `QualitativeInputs` and its sub-records (`SentimentAggregate`, `PredictionMarketSnapshot`, `CalendarEvent`, `ActiveThesis`).

## Depends on

* ALP-246 — the loaders whose typed records this assembler renders.
* ALP-248 — the news digest renderer whose `digest_text` this assembler embeds.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/input_bundle.py` and `tests/analysis/qualitative_research/test_input_bundle.py`.

### 1\. `InputBundle` value object

```python
class InputBundle(BaseModel, frozen=True):
    invocation_id: str
    as_of: datetime
    regime_text: str        # rendered regime block
    digest_text: str        # passed through verbatim from NewsDigest.digest_text
    sentiment_text: str     # rendered SentimentAggregate tuple
    prediction_market_text: str
    calendar_text: str
    thesis_text: str
    bundle_text: str        # concatenated user message ready for the LLM
```

The per-section text fields exist for diagnostic-archive preservation; the `bundle_text` is what the harness sends as the user message.

### 2\. `assemble_input_bundle`

```python
def assemble_input_bundle(
    *,
    invocation_id: str,
    as_of: datetime,
    regime_label: dict[str, Any],            # DistillationOutputs.universal_regime_label
    digest: NewsDigest,
    inputs: QualitativeInputs,
) -> InputBundle: ...
```

Pure function. Composes the bundle in this order, matching the prompt's `<inputs>` ordering:

1. Header — `# QUALITATIVE RESEARCHER INPUT BUNDLE` plus `Invocation: {invocation_id}` and `As of: {as_of_iso}`.
2. Regime block — renders `regime_label` as a `## VOLATILITY REGIME` section. Fields: `regime_label`, `transition_state`, `prior_label`, `invocations_held`. Match the rendering style used by the existing distillation universal-broadcast regime block (so the agent sees consistent regime formatting across agents).
3. News digest block — `## NEWS DIGEST` followed by `digest.digest_text` verbatim.
4. Sentiment aggregates block — `## SENTIMENT AGGREGATES` followed by per-ticker rows. Each row: `{ticker}: directional={direction}, magnitude={magnitude}, change={rate_of_change}, vol={volume}, percentile={percentile_vs_self}, divergence={divergence_flag}`. Rows sorted alphabetically by ticker.
5. Prediction-market snapshot block — `## PREDICTION-MARKET SNAPSHOT` followed by per-contract rows. Each row: `[{contract_id}] {description} ({platform}/{category}): prob={current_probability}, Δ_invocation={delta_since_last_invocation_pp}pp, Δ_24h={delta_24h_pp}pp, vol_24h=${volume_24h_usd}, expires={expiration}`. Append `[FLAGGED]` when `meets_threshold_flag=True`. Append `[LOW LIQUIDITY]` when `is_low_liquidity=True`. Rows sorted by `contract_id`.
6. Event calendar block — `## EVENT CALENDAR (next 72h)` followed by per-event rows. Each row: `- {event_time_iso} | {event_name} | type={event_type} | sectors={sorted_sector_values} | tickers={tickers} | consensus={consensus or "(none)"}`. Rows sorted by `event_time` ascending.
7. Active thesis summaries block — `## ACTIVE THESIS SUMMARIES` followed by per-thesis rows. Each row: `[{thesis_id}] {ticker}: {summary} | catalyst: {key_catalyst} | time: {time_expectation_hours}h`. Rows sorted by `thesis_id`. **When** `inputs.theses` is empty, emit a single line: `(no active theses — execution-layer thesis model pending per ALP-111).`
8. Bundle terminator — a single trailing newline.

### 3\. Determinism

Identical inputs produce a byte-identical `bundle_text`. No `datetime.now()`, no random ordering, no string interning that depends on object identity. Tests assert this by calling `assemble_input_bundle` twice on the same inputs and comparing with `==`.

### Out of scope

* Loading the inputs — story 03d.
* Rendering the news digest — story 04a.
* Sending the bundle to the LLM — story 04b's harness.
* Any LLM involvement — the bundle is deterministic text.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.input_bundle import assemble_input_bundle, InputBundle` resolves.
- [ ] `assemble_input_bundle(...)` returns an `InputBundle` whose `bundle_text` contains every section header (`## VOLATILITY REGIME`, `## NEWS DIGEST`, `## SENTIMENT AGGREGATES`, `## PREDICTION-MARKET SNAPSHOT`, `## EVENT CALENDAR (next 72h)`, `## ACTIVE THESIS SUMMARIES`).
- [ ] Empty `inputs.theses` produces the named placeholder line; non-empty produces formatted thesis rows.
- [ ] Sentiment-aggregate rows are sorted alphabetically by ticker; calendar events are sorted by `event_time` ascending; prediction-market contracts are sorted by `contract_id`.
- [ ] Calling `assemble_input_bundle` twice with identical inputs returns two `InputBundle` instances whose `bundle_text` are byte-identical (determinism).
- [ ] `digest.digest_text` is embedded verbatim — the assembler does not re-decorate or re-render the digest.
- [ ] Regime label fields (`regime_label`, `transition_state`, `prior_label`, `invocations_held`) all appear in `regime_text`.
- [ ] `tests/analysis/qualitative_research/test_input_bundle.py` covers each acceptance criterion. Tests pass under `uv run pytest tests/analysis/qualitative_research/test_input_bundle.py -n auto`.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_input_bundle.py -n auto`. Run `uv run mypy src/alphamind/analysis/qualitative_research/input_bundle.py` clean. Spot-check a rendered bundle by manual inspection — the section ordering matches the prompt's `<inputs>` ordering, every field is rendered, and the text reads as a coherent single user message.

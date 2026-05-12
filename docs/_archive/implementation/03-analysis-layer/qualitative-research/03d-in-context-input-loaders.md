# 03d — In-context input loaders

## Goal

Implement the four loaders that produce the typed value objects the input bundle assembler (story 05) renders into the qualitative researcher's user message: sentiment aggregates, prediction-market snapshot, event calendar (next 72 hours), and active thesis summaries. Three of the loaders query existing infrastructure; the fourth (`load_active_thesis_summaries`) is a stub returning `()` until the execution-layer thesis model lands. Each loader is a pure function over a SQLAlchemy session and returns a `BaseModel(frozen=True)` record (or tuple of records) — no rendering happens here.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § Inputs § In-context data — names the five in-context blocks (news digest, sentiment aggregates, prediction-market snapshot, event calendar, active thesis summaries) and their per-block token budgets.
* `docs/design/02-distillation-layer/external.md` § 3 — sentiment percentile delivery (per-ticker calibration against trailing distribution); the loader transforms distillation's already-computed output rather than recomputing.
* `docs/design/05-execution-layer/thesis-model.md` § Thesis summary fields — defines the `summary` one-liner, `key_catalyst`, and `time_expectation` fields the active-thesis-summaries loader will eventually surface. Until the thesis model ships, the loader returns `()`.
* `src/alphamind/distillation/qualitative_derived.py` § `compute_sentiment_percentile`, § `compute_prediction_market_deltas` — the producers the sentiment and prediction-market loaders consume. Note the output shape (each is an `OutputBlock` carrying calibrated values per ticker / per contract).
* `src/alphamind/persistence/models.py` § `EventCalendar`, `EarningsEventDetails`, `PredictionMarketContracts`, `PredictionMarketSnapshots` — the tables the calendar loader and prediction-market snapshot loader read from.
* `src/alphamind/analysis/domain_researchers/qualitative_input.py` § `EventEntry` — the analog event-entry shape; reuse the same field set if practical so renderers can share rendering helpers.

## Depends on

* ALP-241 — story 01 lands the package skeletons that house this module's tests.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/loaders.py` (or split across `sentiment_input.py` / `prediction_input.py` / `calendar_input.py` / `thesis_input.py` if module-per-loader feels cleaner — pick one shape and apply it consistently) and corresponding test files under `tests/analysis/qualitative_research/`.

### 1\. Records

All immutable `BaseModel(frozen=True)`.

* `SentimentAggregate` — `ticker: str`, `directional_score: float` (signed, `-1.0` to `1.0`), `magnitude: float` (`0.0` to `1.0`), `rate_of_change: float`, `volume: int` (article count over window), `divergence_flag: bool`, `percentile_vs_self: float` (`0.0` to `1.0` per design § Inputs § In-context data block 2). The `data_freshness: datetime` field captures when the underlying distillation block was computed.
* `PredictionMarketSnapshot` — `contract_id: str`, `description: str`, `platform: str`, `category: str`, `current_probability: float`, `delta_since_last_invocation_pp: float`, `delta_24h_pp: float`, `volume_24h_usd: float | None`, `expiration: str | None`, `is_low_liquidity: bool`, `meets_threshold_flag: bool` (set when absolute delta > the configured threshold from `compute_prediction_market_deltas`). `data_freshness: datetime`.
* `CalendarEvent` — `event_id: str`, `event_name: str`, `event_time: datetime`, `event_type: str` (e.g. `earnings`, `fomc`, `cpi`, `opec`), `tickers: tuple[str, ...]`, `sectors: frozenset[Sector]`, `consensus: str | None`. Reuse the existing `Sector` enum from `_shared.py`.
* `ActiveThesis` — `thesis_id: str`, `ticker: str`, `summary: str`, `key_catalyst: str`, `time_expectation_hours: int` (per the thesis-model design doc; integers chosen for readability in the rendered bundle). The loader returns `()` unless and until the thesis model is implemented.
* `QualitativeInputs` — top-level container `BaseModel(frozen=True)` with fields `sentiment_aggregates: tuple[SentimentAggregate, ...]`, `prediction_markets: tuple[PredictionMarketSnapshot, ...]`, `events: tuple[CalendarEvent, ...]`, `theses: tuple[ActiveThesis, ...]`, `data_freshness: datetime`.

### 2\. Loader functions

`load_sentiment_aggregates(session: Session, *, as_of: datetime, ticker_scope: Sequence[str] | None = None) -> tuple[SentimentAggregate, ...]`

Reads the latest run of `compute_sentiment_percentile` from the calibration-state snapshot (or recomputes from `news_article_tickers` if the snapshot is stale per the design's `sentiment_min_observations` rule). Returns one `SentimentAggregate` per ticker in the scope; when `ticker_scope` is `None`, returns aggregates for the full universe.

`load_prediction_market_snapshot(session: Session, *, as_of: datetime) -> tuple[PredictionMarketSnapshot, ...]`

Reads `prediction_market_contracts` and the latest two `prediction_market_snapshots` per contract. Computes the delta-since-last-invocation and 24h delta. Joins the latest run of `compute_prediction_market_deltas` for the threshold-flag column.

`load_calendar_events_72h(session: Session, *, as_of: datetime) -> tuple[CalendarEvent, ...]`

Reads `event_calendar` rows where `event_time` is in `[as_of, as_of + 72h)`. Joins `earnings_event_details` to populate `consensus` for earnings events. Joins `event_calendar.sectors` (the column added by ALP-189). Returns events sorted by `event_time` ascending.

`load_active_thesis_summaries(session: Session, *, as_of: datetime) -> tuple[ActiveThesis, ...]`

**Stub for this work tree.** Returns `()` unconditionally. The function body is one line plus a docstring naming the parent issue's coordination note (ALP-111 § Sequencing context § Position and thesis model). When the thesis model lands, the function body is replaced with a real query; nothing else in this work tree changes.

`load_qualitative_inputs(session: Session, *, as_of: datetime, ticker_scope: Sequence[str] | None = None) -> QualitativeInputs`

Aggregator that calls all four loaders and assembles the `QualitativeInputs` container. `data_freshness` is the minimum of the four sub-loaders' freshness timestamps (the most stale upstream wins).

### 3\. Behavior on missing data

* Sentiment for a ticker without enough trailing observations falls back to the universe-pooled distribution per `compute_sentiment_percentile`'s existing `BOOTSTRAP` tag. The loader does not invent a percentile; it surfaces the bootstrap state in `SentimentAggregate.data_freshness` (which the renderer can flag).
* Empty prediction-markets table or empty event calendar produces an empty tuple, not an exception.
* If the latest distillation run is older than 24 hours, the loader still returns the value with the stale `data_freshness`. The `QualitativeBrief.signal_quality` is then set to `DEGRADED` by the agent based on the rendered freshness, not by the loader.

### Out of scope

* Rendering any of these records into text — story 05's input bundle assembler.
* The news digest itself — that is a separately-rendered structure produced by stories 03c + 04a.
* Implementing the thesis model — execution-layer feature.
* Recomputing sentiment percentiles or prediction-market deltas — those are already produced by distillation; the loader transforms them.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.loaders import (load_sentiment_aggregates, load_prediction_market_snapshot, load_calendar_events_72h, load_active_thesis_summaries, load_qualitative_inputs, QualitativeInputs, SentimentAggregate, PredictionMarketSnapshot, CalendarEvent, ActiveThesis)` resolves.
- [ ] `load_active_thesis_summaries(session, as_of=datetime.now(UTC))` returns `()` and the function's docstring names ALP-111 and the thesis-model coordination note.
- [ ] `load_calendar_events_72h` returns events ordered by `event_time` ascending; an event scheduled 73 hours out is not included.
- [ ] `load_sentiment_aggregates` produces a record whose `percentile_vs_self` is in `[0.0, 1.0]` for any ticker that has the configured minimum trailing observations, and falls back to the universe-pooled distribution for tickers below the threshold.
- [ ] `load_prediction_market_snapshot` populates `delta_since_last_invocation_pp` correctly when two consecutive snapshots exist, and `0.0` (or surfaces an explicit "no prior" via a typed flag) when only one snapshot exists.
- [ ] `load_qualitative_inputs.data_freshness` equals the minimum of the four sub-loaders' freshness timestamps.
- [ ] All records are immutable: `frozen=True` with no setattr at runtime.
- [ ] Tests exercise each loader against a populated SQLite test database fixture. Tests pass under `uv run pytest tests/analysis/qualitative_research/test_loaders.py -n auto` (or the per-module test files if you split the loaders).
- [ ] `uv run mypy src/alphamind/analysis/qualitative_research/loaders.py` is clean.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/ -n auto` to confirm all loader tests pass. Spot-check `load_qualitative_inputs` against a production-shaped database fixture and confirm the assembled `QualitativeInputs` matches manual inspection. If any loader's output shape diverges from the design doc's named fields, surface to the operator before completing the story (per ALP-111 § Surfacing conditions).

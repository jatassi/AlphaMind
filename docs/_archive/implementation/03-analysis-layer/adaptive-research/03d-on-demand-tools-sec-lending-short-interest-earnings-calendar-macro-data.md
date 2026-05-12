# 03d — On-demand tools: sec_lending, short_interest, earnings_calendar, macro_data

## Goal

Implement four typed on-demand tools the adaptive researcher calls during reasoning. Each tool is a typed Python callable with a Pydantic input model, a Pydantic output model extending `ToolEnvelope`, and a `callable_factory(session: Session) -> Callable[[InputModel], OutputModel]`. Mirror the established pattern from `alphamind.analysis.tools.{news_search,prediction_markets,earnings_commentary}` exactly: each tool lives in its own module under `src/alphamind/analysis/tools/`, exports its `*Input`, `*Output`, and `*_factory` symbols, validates inputs through Pydantic, and returns an envelope carrying `data_freshness: datetime` and `quality: ToolQuality`. Per parent issue resolution (G), this story does NOT touch `tools/__init__.py` — story 04a wires the four new entries into the `TOOLS` registry alongside `ticker_deep_pull` and the SDK adapter generalization.

## Reading

* `docs/design/03-analysis-layer/adaptive-research.md` § Tool inventory — input/output shape per tool (the table rows for `sec_lending`, `short_interest`, `earnings_calendar`, `macro_data`); per-tool rate-limit framing.
* `docs/design/03-analysis-layer/adaptive-research.md` § Tool usage contracts — input validation discipline (invalid inputs return an envelope with `quality=UNAVAILABLE` rather than raising), `data_freshness` + `quality` envelope, on-demand vs. routine data discussion.
* `prompts/analysis/adaptive_researcher.md` § `<tool_policy>` — the agent's narrative tool-usage discipline; implementations align with the policy text.
* `src/alphamind/analysis/tools/news_search.py` — canonical reference implementation. Mirror: module-level docstring, `_<TOOL>_TIER_SCORE`-style constants (where applicable), `*Input` / `*Output` Pydantic models, factory pattern, ranking helpers, the SQLAlchemy query shape.
* `src/alphamind/analysis/tools/prediction_markets.py` — second reference implementation; note the contract-snapshot join pattern.
* `src/alphamind/analysis/tools/earnings_commentary.py` — third reference; note the multi-tier output shape and the `quality` enum's per-tool `PARTIAL_NO_TRANSCRIPT` extension pattern (this story does NOT add new `ToolQuality` members).
* `src/alphamind/analysis/tools/_envelope.py` — `ToolEnvelope`, `ToolQuality`, `format_iso`, `parse_iso` helpers; reuse all four.
* `src/alphamind/persistence/models.py` § `ShortInterestSnapshot`, `ShortVolumeDaily`, `BorrowCostDaily`, `BorrowCostIntraday`, `EarningsEventDetails`, `EarningsEstimateRevisions`, `MacroObservations`, `TreasuryAuctions`, `AssetUniverse` — read targets.
* `docs/design/01-data-layer/collector/storage.md` § Tables — schema-level reference for what each table carries.

## Depends on

* <issue id="5043b367-d100-413e-b7f0-3e1227963080">ALP-254</issue> (story 01) — package skeleton.

## Scope

In scope under `src/alphamind/analysis/tools/sec_lending.py`, `short_interest.py`, `earnings_calendar.py`, `macro_data.py`, and `tests/analysis/tools/test_sec_lending.py`, `test_short_interest.py`, `test_earnings_calendar.py`, `test_macro_data.py`. Each tool is independent of the others — one source file + one test file per tool.

Explicit non-scope: this story does NOT edit `src/alphamind/analysis/tools/__init__.py` or `src/alphamind/analysis/tools/_sdk_adapter.py`. Both are story 04a's territory.

### 1\. `sec_lending`

Per the design doc: securities lending market data (borrow rates, availability, utilization).

Input: `SecLendingInput(tickers: tuple[str, ...])`. At least one ticker required; empty tuple → return envelope with `quality=UNAVAILABLE`.

Output: `SecLendingOutput(per_ticker: tuple[SecLendingTickerData, ...], data_freshness: datetime, quality: ToolQuality)` where:

```python
class SecLendingTickerData(BaseModel, frozen=True):
    ticker: str
    borrow_rate_pct: float | None        # most recent rate from BorrowCostDaily or BorrowCostIntraday
    available_shares: int | None          # most recent availability snapshot
    utilization_pct: float | None         # most recent utilization
    days_to_cover: float | None           # short_interest_pct * shares_outstanding / avg_daily_volume; None when inputs missing
    cost_trend: str                       # "rising" | "falling" | "stable" — last 5 trading days direction
    data_freshness: datetime              # per-ticker freshness (most recent observation timestamp)
    quality: ToolQuality                  # per-ticker quality
```

Implementation:

* Validate every ticker is in `AssetUniverse`; tickers not in universe set `quality=UNAVAILABLE` for that row but other rows proceed.
* Query `BorrowCostIntraday` for the most recent intraday observation per ticker; fall back to `BorrowCostDaily` when no intraday row exists.
* `cost_trend` from the last 5 `BorrowCostDaily` rows: linear-fit slope > 0.5 bp/day → "rising"; slope < -0.5 bp/day → "falling"; else "stable". Threshold lives as a named module constant `_SEC_LENDING_TREND_BP_PER_DAY = 0.5`.
* `days_to_cover` requires both short interest and avg daily volume; when either is missing for the ticker, leave `None` and set per-ticker `quality=PARTIAL`.
* Aggregate-level `data_freshness` = minimum across populated per-ticker freshness; aggregate `quality=COMPLETE` when all per-ticker rows are COMPLETE, else PARTIAL.

### 2\. `short_interest`

Per the design doc: short interest and squeeze composite for specific names.

Input: `ShortInterestInput(tickers: tuple[str, ...])`. At least one ticker required; empty → `quality=UNAVAILABLE`.

Output: `ShortInterestOutput(per_ticker: tuple[ShortInterestTickerData, ...], data_freshness: datetime, quality: ToolQuality)` where:

```python
class ShortInterestTickerData(BaseModel, frozen=True):
    ticker: str
    short_interest_pct: float | None      # ShortInterestSnapshot.short_interest_pct most recent
    squeeze_score: float | None           # composite of short_interest_pct + days_to_cover + cost_to_borrow rate-of-change
    days_to_cover: float | None
    cost_to_borrow_pct: float | None      # most recent BorrowCostDaily.borrow_rate_pct
    short_volume_ratio_5d: float | None   # ShortVolumeDaily 5-day average ratio
    data_freshness: datetime
    quality: ToolQuality
```

Implementation:

* Validate tickers against `AssetUniverse`.
* `short_interest_pct` reads `ShortInterestSnapshot` for the most recent FINRA bi-weekly snapshot per ticker.
* `short_volume_ratio_5d` reads `ShortVolumeDaily`'s last 5 rows per ticker, averages the daily short-volume ratio.
* `squeeze_score` is a deterministic composite (mirror the design doc's pattern; concrete formula: `min(1.0, 0.4 * short_interest_pct/100 + 0.4 * days_to_cover/10 + 0.2 * cost_to_borrow_pct/50)`). Named constants for the weights at the module level (`_SQUEEZE_SI_WEIGHT = 0.4`, etc.) so the no-magic-numbers audit passes.
* Aggregate `data_freshness` and `quality` follow the same pattern as `sec_lending`.

### 3\. `earnings_calendar`

Per the design doc: upcoming earnings dates, consensus estimates, and recent revisions.

Input: `EarningsCalendarInput(tickers: tuple[str, ...], lookback_days: int = 30)`. At least one ticker required; `lookback_days >= 0`.

Output: `EarningsCalendarOutput(per_ticker: tuple[EarningsCalendarEntry, ...], data_freshness: datetime, quality: ToolQuality)` where:

```python
class EarningsCalendarEntry(BaseModel, frozen=True):
    ticker: str
    next_report_date: str | None          # ISO date of next scheduled earnings event (forward window)
    consensus_eps: float | None           # latest consensus from EarningsEventDetails
    revision_trend: str                   # "up" | "down" | "mixed" | "none" — direction over lookback_days
    whisper_number: float | None          # None — out of scope; reserved for future when whisper feed lands
    last_report_date: str | None          # ISO date of most recent reported earnings
    data_freshness: datetime
    quality: ToolQuality
```

Implementation:

* Validate tickers against `AssetUniverse`.
* `next_report_date` reads `EarningsEventDetails` joined to `EventCalendar` for the next forward-dated earnings event per ticker. Fall through to `None` when no forward event is scheduled.
* `consensus_eps` reads the most recent consensus value attached to the next event.
* `revision_trend` walks `EarningsEstimateRevisions` rows where `revised_at >= now - lookback_days`. Compute net direction: all positive → "up"; all negative → "down"; mixed → "mixed"; none → "none".
* `whisper_number` always `None` for this story's scope (no whisper data source).
* `last_report_date` reads `EarningsEventDetails` for the most recent earnings event with `event_date <= now`.

### 4\. `macro_data`

Per the design doc: specific macro data point lookup (treasury yields, credit spreads, etc.).

Input: `MacroDataInput(indicator: str, lookback_days: int = 30)`. `indicator` is required; `lookback_days >= 1`.

Output: `MacroDataOutput(series: tuple[MacroDataPoint, ...], indicator: str, data_freshness: datetime, quality: ToolQuality)` where:

```python
class MacroDataPoint(BaseModel, frozen=True):
    date: str                              # ISO date of the observation
    value: float
    change_1d: float | None                # delta vs. prior trading day
    change_5d: float | None                # delta vs. 5 trading days prior
    percentile_1y: float | None            # rank within trailing 252-day distribution; in [0.0, 1.0]
```

Implementation:

* `indicator` resolves against a known set of macro identifiers carried in `MacroObservations.indicator_id` plus a canonical alias map for treasury tenors that read from `TreasuryAuctions` (e.g., `treasury_2y`, `treasury_10y`). Unknown `indicator` → return envelope with `quality=UNAVAILABLE`, `series=()`.
* Query `MacroObservations` (or `TreasuryAuctions`, depending on indicator family) for the most recent `lookback_days` of values per the indicator.
* Compute `change_1d`, `change_5d`, and `percentile_1y` from the trailing distribution; populate `None` when insufficient observations exist.
* `data_freshness` = the most recent observation timestamp.
* The set of supported indicator names lives as a module-level constant `_SUPPORTED_INDICATORS: frozenset[str]` enumerated explicitly so the function can fail closed on unknown values rather than running an empty query.

### 5\. Per-tool tests

For each of the four tools, `tests/analysis/tools/test_<tool>.py` covers:

* Happy path against a populated test database — returns expected per-ticker rows / series with non-None envelope fields.
* Missing data — ticker not in universe (or unknown indicator) returns `quality=UNAVAILABLE`.
* Partial data — ticker in universe but no rows in the relevant table returns `quality=PARTIAL` with `None` payload fields.
* Invalid input — empty tickers tuple (or empty indicator) returns `quality=UNAVAILABLE` rather than raising.
* Determinism — two calls against the same fixture data return byte-equal payloads.

Reuse the existing test-fixture pattern from `tests/analysis/tools/test_news_search.py`.

### Out of scope

* Editing `src/alphamind/analysis/tools/__init__.py` to register these tools — story 04a.
* Editing `src/alphamind/analysis/tools/_sdk_adapter.py` — story 04a.
* `ticker_deep_pull` — story 03e.
* Any rate-limit accounting at the tool layer — the SDK's per-tool call counting is the system-level mechanism; tools are stateless.

## Acceptance criteria

- [ ] Each of `src/alphamind/analysis/tools/sec_lending.py`, `short_interest.py`, `earnings_calendar.py`, `macro_data.py` exists and exports `*Input`, `*Output`, `*_factory` symbols.
- [ ] `from alphamind.analysis.tools.sec_lending import SecLendingInput, SecLendingOutput, sec_lending_factory` (and the analogous imports for the other three tools) all resolve cleanly.
- [ ] Every tool's `*Output` extends `ToolEnvelope` (carries `data_freshness` and `quality`).
- [ ] Calling each tool against a populated test database with valid inputs returns an envelope with `quality=COMPLETE` (or `PARTIAL` when the design doc names a partial-data condition).
- [ ] Calling each tool with empty inputs (empty tuple, empty indicator string) returns an envelope with `quality=UNAVAILABLE` rather than raising.
- [ ] Calling each tool with a ticker not in `AssetUniverse` returns either an envelope-level `UNAVAILABLE` or a per-ticker `UNAVAILABLE` row (depending on tool semantics).
- [ ] No tool implementation hard-codes a magic numeric threshold; ranking weights and trend cutoffs share named module-level constants.
- [ ] `tests/analysis/tools/test_sec_lending.py`, `test_short_interest.py`, `test_earnings_calendar.py`, `test_macro_data.py` each cover happy path, missing data, and invalid input.
- [ ] `git diff --stat main..HEAD` shows NO modifications to `src/alphamind/analysis/tools/__init__.py` or `src/alphamind/analysis/tools/_sdk_adapter.py`.
- [ ] `uv run pytest tests/analysis/tools/ -n auto` passes; `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/tools/test_sec_lending.py tests/analysis/tools/test_short_interest.py tests/analysis/tools/test_earnings_calendar.py tests/analysis/tools/test_macro_data.py -n auto`. Inspect each tool's `*Output.model_json_schema()` and confirm the wire shape matches the design doc's contract. Run `git diff --stat main..HEAD -- src/alphamind/analysis/tools/__init__.py src/alphamind/analysis/tools/_sdk_adapter.py` and confirm zero changed lines.
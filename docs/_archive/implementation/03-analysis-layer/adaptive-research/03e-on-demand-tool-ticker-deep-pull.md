# 03e — On-demand tool: ticker_deep_pull

## Goal

Implement the `ticker_deep_pull` on-demand tool — a single-ticker, multi-category data aggregator the adaptive researcher invokes when investigating an anomaly that needs more granular data than the routine ingestion delivers. Per parent issue resolution (C): ship four categories — `price_volume` (Q1 OHLCV bars), `short_data` (Q4 short interest + borrow cost), `earnings` (Q5 most-recent event + revisions), `macro_context` (Q6 ticker-relevant macro snapshot). Output is a single `TickerDeepPullOutput` carrying `Optional[CategoryPayload]` per category; the caller selects which categories to populate via the `categories: tuple[str, ...]` input. Lives alongside the four simpler tools from story 03d but warrants its own story because the multi-category schema design and per-category fan-out logic are meaningfully different from the single-table tools.

## Reading

* `docs/design/03-analysis-layer/adaptive-research.md` § Tool inventory — the `ticker_deep_pull` row defines the input/output shape ("Category-specific data per [quantitative.md](http://quantitative.md) schema") and the per-tool 5-call cap.
* `docs/design/03-analysis-layer/adaptive-research.md` § Tool usage contracts — input validation discipline, envelope conventions, the `quality` enum semantics.
* `docs/design/01-data-layer/external/quantitative.md` § Q1 (price/volume), § Q4 (short selling), § Q5 (fundamentals + earnings), § Q6 (macro/rates) — the category-level data model the per-category payloads echo (in summary form, not full per-category fidelity).
* `src/alphamind/analysis/tools/news_search.py`, `prediction_markets.py`, `earnings_commentary.py` — pattern reference. Mirror module structure and factory shape.
* `src/alphamind/analysis/tools/_envelope.py` — `ToolEnvelope`, `ToolQuality`, `format_iso`, `parse_iso` helpers; reuse all four.
* `src/alphamind/persistence/models.py` § `OhlcvBars`, `ShortInterestSnapshot`, `BorrowCostDaily`, `EarningsEventDetails`, `EarningsEstimateRevisions`, `MacroObservations`, `AssetUniverse` — read targets for each category.

## Depends on

* <issue id="5043b367-d100-413e-b7f0-3e1227963080">ALP-254</issue> (story 01) — package skeleton.

## Scope

In scope under `src/alphamind/analysis/tools/ticker_deep_pull.py` and `tests/analysis/tools/test_ticker_deep_pull.py`. This story does NOT edit `src/alphamind/analysis/tools/__init__.py` or `src/alphamind/analysis/tools/_sdk_adapter.py` (story 04a's territory).

### 1\. Supported category set

```python
class TickerDeepPullCategory(StrEnum):
    PRICE_VOLUME = "price_volume"
    SHORT_DATA = "short_data"
    EARNINGS = "earnings"
    MACRO_CONTEXT = "macro_context"


_SUPPORTED_CATEGORIES: frozenset[str] = frozenset(c.value for c in TickerDeepPullCategory)
```

Unknown category strings in the input return `quality=UNAVAILABLE` for that category's payload (set the corresponding output field to `None`). The aggregate envelope `quality` reflects the worst per-category quality.

### 2\. Input model

```python
class TickerDeepPullInput(BaseModel, frozen=True):
    """Input for the ticker_deep_pull tool.

    `ticker` must be in AssetUniverse.
    `categories` must be a non-empty subset of TickerDeepPullCategory values; unknown
    values are accepted at the Pydantic layer (StrEnum coercion is loose) but produce
    UNAVAILABLE rows downstream.
    """

    ticker: str = Field(min_length=1)
    categories: tuple[str, ...] = ()
```

### 3\. Per-category payload models

```python
class PriceVolumePayload(BaseModel, frozen=True):
    """Q1: recent price + volume snapshot.

    Pulls the last 20 trading days of `OhlcvBars` (timeframe=daily) for the ticker.
    """

    last_close: float | None
    last_volume: int | None
    pct_change_1d: float | None
    pct_change_5d: float | None
    pct_change_20d: float | None
    avg_volume_20d: int | None
    volume_vs_avg_ratio: float | None     # last_volume / avg_volume_20d


class ShortDataPayload(BaseModel, frozen=True):
    """Q4: short interest + borrow cost summary."""

    short_interest_pct: float | None      # most recent ShortInterestSnapshot
    borrow_rate_pct: float | None         # most recent BorrowCostDaily
    days_to_cover: float | None
    short_interest_change_30d_pct: float | None  # pp delta vs. 30 days prior


class EarningsPayload(BaseModel, frozen=True):
    """Q5: most-recent earnings event + revision summary."""

    most_recent_event_date: str | None    # ISO date of most recent earnings event
    eps_actual: float | None
    eps_consensus: float | None
    eps_surprise_pct: float | None
    revisions_30d_count: int | None       # count of EarningsEstimateRevisions in last 30d
    revision_direction: str | None        # "up" | "down" | "mixed" | "none"


class MacroContextPayload(BaseModel, frozen=True):
    """Q6: ticker-relevant macro snapshot.

    Limited subset — for v1, returns the 2y/10y treasury yields and the most recent
    sector-relevant macro indicator (read from a ticker→indicator mapping when the
    ticker's sector has a canonical macro driver; None otherwise).
    """

    treasury_2y_yield: float | None
    treasury_10y_yield: float | None
    yield_curve_2_10_spread: float | None
    sector_relevant_indicator: str | None  # e.g., "wti_spot" for energy tickers
    sector_relevant_value: float | None
```

### 4\. Output model

```python
class TickerDeepPullOutput(ToolEnvelope, frozen=True):
    """Output envelope for ticker_deep_pull.

    Per-category payloads are Optional: None when the caller did not request the
    category, OR when the category was requested but data was unavailable for the ticker.
    The aggregate `quality` reflects the worst per-category result; aggregate
    `data_freshness` is the minimum freshness across populated categories.
    """

    ticker: str
    price_volume: PriceVolumePayload | None
    short_data: ShortDataPayload | None
    earnings: EarningsPayload | None
    macro_context: MacroContextPayload | None
```

### 5\. Implementation

```python
def ticker_deep_pull_factory(session: Session) -> Callable[[TickerDeepPullInput], TickerDeepPullOutput]:
    """Build the ticker_deep_pull callable bound to `session`."""

    def _call(args: TickerDeepPullInput) -> TickerDeepPullOutput:
        # 1. Validate ticker against AssetUniverse; if not in universe → UNAVAILABLE envelope
        # 2. For each category in args.categories that's in _SUPPORTED_CATEGORIES, dispatch
        #    to the per-category loader; unknown categories → leave that field None
        # 3. Aggregate quality: worst per-category quality; data_freshness: min across populated
        ...

    return _call
```

Per-category loaders are private module-level functions (`_load_price_volume`, `_load_short_data`, `_load_earnings`, `_load_macro_context`), each taking `(session, ticker)` and returning `(payload | None, freshness | None, quality)`. None payload + UNAVAILABLE quality means no data exists for the ticker in that category.

The aggregate `quality` resolution follows a deterministic min-rank: `COMPLETE > PARTIAL > STALE > UNAVAILABLE` (so any UNAVAILABLE downgrades the envelope). Encode the rank as a module constant `_QUALITY_RANK: dict[ToolQuality, int]`.

### 6\. Tests

`tests/analysis/tools/test_ticker_deep_pull.py` covers:

* Happy path with all four categories requested against a populated DB → envelope `quality=COMPLETE`, all four payloads non-None, fields populated.
* Single-category request — only `price_volume` requested → other three fields are `None`, envelope quality reflects the price_volume-only result.
* Ticker not in `AssetUniverse` → envelope `quality=UNAVAILABLE`, all four payloads `None`.
* Unknown category in input (e.g., `categories=("price_volume", "options_flow")`) — `price_volume` populates normally, `options_flow` is silently dropped (not a supported category), envelope quality reflects price_volume only.
* Partial data — ticker in universe but no `ShortInterestSnapshot` rows → `short_data` payload non-None but with all None fields and per-category quality=PARTIAL; aggregate envelope quality = PARTIAL.
* Empty `categories` tuple → envelope `quality=UNAVAILABLE` (no work to do).
* Determinism — two calls against the same fixture data return byte-equal payloads.

### Out of scope

* Editing `src/alphamind/analysis/tools/__init__.py` to register the tool — story 04a.
* Editing `src/alphamind/analysis/tools/_sdk_adapter.py` — story 04a.
* The four simpler tools — story 03d.
* Q2 / Q3 categories (microstructure, options flow) — storage not provisioned per parent issue resolutions (A) and (B); not eligible for this story's category set.

## Acceptance criteria

- [ ] `src/alphamind/analysis/tools/ticker_deep_pull.py` exists and exports `TickerDeepPullInput`, `TickerDeepPullOutput`, `TickerDeepPullCategory`, `PriceVolumePayload`, `ShortDataPayload`, `EarningsPayload`, `MacroContextPayload`, `ticker_deep_pull_factory`.
- [ ] `from alphamind.analysis.tools.ticker_deep_pull import TickerDeepPullInput, TickerDeepPullOutput, ticker_deep_pull_factory` resolves cleanly.
- [ ] `TickerDeepPullOutput` extends `ToolEnvelope` (carries `data_freshness` and `quality`).
- [ ] Calling with `categories=("price_volume", "short_data", "earnings", "macro_context")` against a populated test database returns an envelope with all four payload fields non-None and `quality=COMPLETE`.
- [ ] Calling with `categories=("price_volume",)` returns an envelope with only `price_volume` populated; the other three are `None`.
- [ ] Calling with a ticker not in `AssetUniverse` returns an envelope with `quality=UNAVAILABLE` and all four payloads `None`.
- [ ] Calling with an unknown category in the input (e.g., `categories=("unknown_category",)`) does not raise; the unknown category is silently skipped and the envelope reports `quality=UNAVAILABLE` if no other categories produced data.
- [ ] Calling with `categories=()` returns an envelope with `quality=UNAVAILABLE`.
- [ ] No module hard-codes a magic numeric threshold; the supported-category set, quality-rank dict, and any aggregation thresholds use named module constants.
- [ ] `git diff --stat main..HEAD` shows NO modifications to `src/alphamind/analysis/tools/__init__.py` or `src/alphamind/analysis/tools/_sdk_adapter.py`.
- [ ] `tests/analysis/tools/test_ticker_deep_pull.py` covers each acceptance criterion and passes under `uv run pytest tests/analysis/tools/test_ticker_deep_pull.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/tools/test_ticker_deep_pull.py -n auto`. Inspect `TickerDeepPullOutput.model_json_schema()` and confirm the wire shape exposes four `Optional[<CategoryPayload>]` fields keyed by category name. Run `git diff --stat main..HEAD -- src/alphamind/analysis/tools/__init__.py src/alphamind/analysis/tools/_sdk_adapter.py` and confirm zero changed lines.
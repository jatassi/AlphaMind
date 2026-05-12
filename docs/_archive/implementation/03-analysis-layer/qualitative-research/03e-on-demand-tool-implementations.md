# 03e — On-demand tool implementations

## Goal

Implement the three on-demand tools the qualitative researcher calls during reasoning: `news_search`, `prediction_markets`, and `earnings_commentary` (Tier 1). Each tool is a typed Python callable with a Pydantic input model, a Pydantic output model, and an Agent SDK adapter that the harness (story 04b) registers. `social_sentiment` is omitted per the parent issue's cross-feature dependencies (data-layer storage deferred). `earnings_commentary` Tier 2 fields are returned stub-shaped (`transcript_available: false`, `transcript_analysis: null`, `quality: "partial_no_transcript"`) until the NLP pipeline lands. Tools live under `src/alphamind/analysis/tools/` so the future adaptive-research work tree can consume the two shared ones without relocation.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § Inputs § On-demand tools — names the four-tool catalog and the use case for each.
* `docs/design/03-analysis-layer/qualitative-research.md` § `earnings_commentary` tool contract — the literal input and output schemas the `earnings_commentary` tool must conform to (Tier 1 fields, Tier 2 fields, `quality` enum, data-source mapping table).
* `docs/design/03-analysis-layer/adaptive-research.md` § Tool inventory § Tool usage contracts — names the shared input-validation discipline (invalid inputs return an error rather than consuming a rate-limit slot), the `data_freshness` and `quality` envelope, and the rate-limit framing.
* `prompts/analysis/qualitative_researcher.md` § `<tool_policy>` — the narrative tool-usage discipline the implementations support (the agent's prompt-side description must align with the runtime contracts).
* `src/alphamind/persistence/models.py` § `NewsArticles`, `NewsArticleTickers`, `PredictionMarketContracts`, `PredictionMarketSnapshots`, `EarningsEventDetails`, `EarningsEstimateRevisions`, `OhlcvBars` — the read targets.
* `src/alphamind/data_sources/finnhub/estimate_revisions.py`, `src/alphamind/data_sources/finnhub/calendar.py` — existing Finnhub adapters; the earnings-commentary tool may compose them rather than calling vendor APIs directly.
* `src/alphamind/analysis/domain_researchers/qualitative_input.py` — analog query patterns for `news_articles` joins; reuse where applicable.

## Depends on

* ALP-241 — story 01 lands the `src/alphamind/analysis/tools/` package skeleton.

## Scope

In scope under `src/alphamind/analysis/tools/news_search.py`, `prediction_markets.py`, `earnings_commentary.py`, plus `tests/analysis/tools/test_<tool>.py` per tool. The shared envelope types live at `src/alphamind/analysis/tools/_envelope.py`.

### 1\. Shared envelope

`_envelope.py`:

```python
class ToolQuality(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    PARTIAL_NO_TRANSCRIPT = "partial_no_transcript"  # earnings_commentary-specific


class ToolEnvelope(BaseModel, frozen=True):
    """Mixin shape for every tool output: every payload carries data_freshness + quality."""

    data_freshness: datetime
    quality: ToolQuality
```

Each tool's output extends `ToolEnvelope` (composition rather than inheritance is fine; the constraint is the two fields appear on the wire shape the agent sees).

### 2\. `news_search`

Input: `NewsSearchInput(query: str, tickers: tuple[str, ...] = (), lookback_hours: int = 24, max_results: int = 20)`. `query` may be empty when `tickers` is non-empty; at least one of the two must be present.

Output: `NewsSearchOutput(articles: tuple[NewsSearchArticle, ...], data_freshness: datetime, quality: ToolQuality)` where `NewsSearchArticle(headline: str, source: str, source_outlet: str | None, source_credibility_tier: str | None, published_at: datetime, url: str | None, summary: str | None, tickers: tuple[str, ...], topic_tags: tuple[str, ...], relevance_score: float, body_excerpt: str | None)`.

Implementation: a SQLAlchemy query against `news_articles` joined to `news_article_tickers`, filtered by `published_at >= as_of - lookback_hours`, optionally filtered by `tickers` overlap, optionally filtered by ILIKE-substring against `headline_text` for `query`. `relevance_score` is a simple recency+ticker-mention composite (mirror story 04a's ranking weights for consistency, or factor that helper out as `_score_news_article` in `news_search.py` so 04a can import it). `body_excerpt` reads the first ~500 characters of `body_path` if present, otherwise `None`.

Invalid input behavior: empty query AND empty tickers → returns `NewsSearchOutput(articles=(), quality=UNAVAILABLE, data_freshness=now)` rather than raising; the agent sees a typed empty result, not a stack trace.

### 3\. `prediction_markets`

Input: `PredictionMarketsInput(query: str = "", categories: tuple[str, ...] = (), max_results: int = 10)`. At least one of `query` or `categories` must be non-empty.

Output: `PredictionMarketsOutput(contracts: tuple[PredictionMarketContract, ...], data_freshness: datetime, quality: ToolQuality)` where `PredictionMarketContract(contract_id: str, description: str, platform: str, category: str, current_probability: float, prob_24h_ago: float | None, prob_delta_24h_pp: float | None, volume_24h_usd: float | None, expiration: str | None, resolution_outcome: str | None)`.

Implementation: query `prediction_market_contracts` filtered by ILIKE against `description` for `query` and by `category` membership for `categories`. Join the latest two `prediction_market_snapshots` per contract for the 24h delta computation. Result count capped at `max_results` ordered by `volume_24h_usd` descending (the agent's prompt names "specific contracts beyond the routine tracked set").

### 4\. `earnings_commentary` (Tier 1)

Input: `EarningsCommentaryInput(ticker: str, include_transcript_analysis: bool = True)`. `ticker` validation: must be in `asset_universe`; otherwise `quality=UNAVAILABLE`.

Output (mirrors the design doc § `earnings_commentary` tool contract verbatim):

```python
class EarningsResult(BaseModel, frozen=True):
    eps_actual: float
    eps_consensus: float
    eps_surprise_pct: float
    revenue_actual: float
    revenue_consensus: float
    revenue_surprise_pct: float


class PriceReaction(BaseModel, frozen=True):
    close_to_close_pct: float
    immediate_move_pct: float
    vs_implied_move: float | None  # None when options-implied data not available


class RatingChange(BaseModel, frozen=True):
    analyst_firm: str
    prior_rating: str
    new_rating: str
    prior_target: float | None
    new_target: float | None


class PostEarningsActivity(BaseModel, frozen=True):
    estimate_revisions_since: int
    revision_direction: Literal["up", "down", "mixed", "none"]
    rating_changes_since: tuple[RatingChange, ...]


class EarningsCommentaryOutput(BaseModel, frozen=True):
    ticker: str
    earnings_date: str
    data_freshness: datetime
    result: EarningsResult
    price_reaction: PriceReaction
    post_earnings_activity: PostEarningsActivity
    transcript_available: bool          # always False in this story's scope
    transcript_analysis: None           # always None in this story's scope
    quality: ToolQuality                # COMPLETE | PARTIAL_NO_TRANSCRIPT | STALE | UNAVAILABLE
```

Implementation:

* `result` reads `earnings_event_details` for the most recent earnings event for `ticker`. `eps_surprise_pct = (eps_actual - eps_consensus) / abs(eps_consensus) * 100` when `eps_consensus != 0`; otherwise `0.0`. Same for revenue.
* `price_reaction.close_to_close_pct` reads `ohlcv_bars` (timeframe=`1d`) — prior trading day's close vs. post-earnings close. `immediate_move_pct` reads the first 30-minute `ohlcv_bars` (timeframe=`1m` or `30m`, whichever exists) post-report. `vs_implied_move` is `None` for this story's scope (options-implied move requires options data not currently in scope).
* `post_earnings_activity.estimate_revisions_since` counts `earnings_estimate_revisions` rows with `revised_at >= reported_at`. `revision_direction` is `up` if all revisions raise the metric, `down` if all lower, `mixed` if both directions are present, `none` if no revisions.
* `post_earnings_activity.rating_changes_since` is `()` for this story's scope. The function docstring names the coordination follow-up: when analyst-rating ingestion lands, this tuple populates without a contract change.
* `transcript_available = False`, `transcript_analysis = None` always. `quality = COMPLETE` when Tier 1 fields all populate; `quality = PARTIAL_NO_TRANSCRIPT` when `include_transcript_analysis=True` (the call asked for transcript but Tier 2 is not yet built); `quality = STALE` when the most recent earnings event is more than 90 days old; `quality = UNAVAILABLE` when no earnings event is found for the ticker.

### 5\. Agent SDK adapters

Each tool exports a callable suitable for the Claude Agent SDK's `allowed_tools` mechanism. The exact adapter shape depends on whether the SDK uses Pydantic-typed callables, JSON Schema, or a registry. Match what the existing harness (`alphamind.analysis.domain_researchers.harness`) uses for `allowed_tools=[]` — the qualitative harness (story 04b) will need the same adapter shape on the non-empty case. If the SDK requires JSON Schema, derive it from the Pydantic input model via `.model_json_schema()`.

`src/alphamind/analysis/tools/__init__.py` exports a registry: `TOOLS: dict[str, ToolDefinition]` keyed by the tool name (`"news_search"`, `"prediction_markets"`, `"earnings_commentary"`), where `ToolDefinition` is a small dataclass holding `name`, `description`, `input_model`, `output_model`, and `callable_factory(session: Session) -> Callable[[InputModel], OutputModel]`. Story 04b's harness uses this registry to build the SDK option set.

### Out of scope

* `social_sentiment` — deferred per parent issue's cross-feature dependencies.
* `earnings_commentary` Tier 2 fields (`transcript_available=True`, populated `transcript_analysis`) — deferred per the Earnings transcript NLP pipeline trigger entry.
* `rating_changes_since` population — deferred per the analyst-rating-ingestion entry.
* Rate-limit accounting at the tool layer — the SDK's per-tool call counting is the system-level mechanism; tool implementations are stateless.

## Acceptance criteria

- [ ] `from alphamind.analysis.tools import TOOLS` resolves with three entries: `"news_search"`, `"prediction_markets"`, `"earnings_commentary"`.
- [ ] Each `TOOLS[name]` is a `ToolDefinition` with `input_model`, `output_model`, and a `callable_factory(session)` producing a typed callable.
- [ ] Calling `news_search` against a populated test database with `tickers=("NVDA",)` returns articles whose `tickers` tuple contains `"NVDA"`.
- [ ] Calling `prediction_markets` with `query="FOMC"` returns contracts whose `description` matches the query (case-insensitive substring).
- [ ] Calling `earnings_commentary` for a ticker with a recent earnings event returns `quality=COMPLETE` (when `include_transcript_analysis=False`) or `quality=PARTIAL_NO_TRANSCRIPT` (when `include_transcript_analysis=True`).
- [ ] Calling `earnings_commentary` for a ticker with no earnings event returns `quality=UNAVAILABLE`.
- [ ] Every tool's output carries a populated `data_freshness: datetime` and a `quality: ToolQuality`.
- [ ] Invalid inputs (empty `query` AND empty `tickers` for `news_search`; ticker not in universe for `earnings_commentary`) do not raise — they return an envelope with `quality=UNAVAILABLE`.
- [ ] `tests/analysis/tools/test_<tool>.py` for each tool exercises happy path, missing data, and invalid input. All tests pass under `uv run pytest tests/analysis/tools/ -n auto`.
- [ ] No tool implementation hard-codes a magic numeric threshold; recency boosts and other ranking factors share named constants with story 04a where applicable.

## Verification

Run `uv run pytest tests/analysis/tools/ -n auto`. Run `uv run mypy src/alphamind/analysis/tools/` clean. Inspect each tool's output type's JSON schema via `model.model_json_schema()` and confirm it matches the design doc's contract for the corresponding tool. Confirm `TOOLS` carries exactly three entries (no `"social_sentiment"`).

## Symptom

Two stub paths in `src/alphamind/analysis/qualitative_research/loaders.py` silently feed the qualitative researcher LLM degraded inputs on every invocation. Both share the same fix pattern: replace a placeholder return with a real query against existing SQL tables.

## Part 1 — Active thesis summaries (stale stub; dependencies have landed)

`src/alphamind/analysis/qualitative_research/loaders.py:619-631`:

```python
def load_active_thesis_summaries(
    session: Session,  # noqa: ARG001
    *,
    as_of: datetime,  # noqa: ARG001
) -> tuple[ActiveThesis, ...]:
    """Return active thesis summaries. Stub — returns () unconditionally.

    ALP-111 § Sequencing context § Position and thesis model: the thesis model
    lives in the execution layer and has not yet landed. When it does, the
    function body is replaced with a real query against the thesis-model table;
    nothing else in this module changes.
    """
    return ()
```

The comment says "the thesis model has not yet landed", but both [ALP-111](https://linear.app/alphamind-jatassi/issue/ALP-111/qualitative-research) (qualitative research) and [ALP-122](https://linear.app/alphamind-jatassi/issue/ALP-122/position-and-thesis-model) (position & thesis model) are Done. The thesis tables (`theses`, `thesis_components`) exist; `ThesisRecord` / `ThesisRow` / `ThesisComponentRow` are shipped (see `src/alphamind/portfolio_state/records/theses.py`, `src/alphamind/state/tables/theses.py`, `src/alphamind/state/tables/thesis_components.py`).

**Production effect.** Every qualitative-researcher invocation produces a brief that says `(no active theses — execution-layer thesis model pending per ALP-111).` (`input_bundle.py:187` `_THESES_STUB`). The qualitative researcher LLM never sees the real active theses; it makes its catalyst calls without that context.

## Part 2 — SentimentAggregate v1 stub fields

`src/alphamind/analysis/qualitative_research/loaders.py:88-91, 367-373`. Three of seven `SentimentAggregate` fields are hardcoded `None`:

```python
SentimentAggregate(
    ticker=row.ticker,
    directional_score=directional_score,
    magnitude=magnitude,
    # v1 stub: these three fields require pipeline pieces that
    # have not yet landed. None signals "data pending" to the LLM
    # via the bundle renderer's `pending` placeholder.
    rate_of_change=None,
    volume=None,
    divergence_flag=None,
    percentile_vs_self=percentile,
    data_freshness=_parse_iso_utc(row.as_of),
)
```

**Production effect.** Every qualitative-researcher invocation sees `pending` for `rate_of_change`, `volume`, and `divergence_flag` on every ticker in the sentiment block. The LLM loses three of the seven signal columns it's documented to read.

## Scope

**(A) Active thesis query.** Query `theses` joined to `positions` (open + pending), filtering by `ThesisRecordStatus.ACTIVE`. Map each `ThesisRow` to `ActiveThesis(thesis_id, ticker, summary, key_catalyst, time_expectation_hours)` — pull `summary` and `key_catalyst` from `ThesisComponentRow` rows joined on `thesis_id`.

**(B) Sentiment rate-of-change loader.** Compute sentiment rate-of-change from `distillation_ticker_baseline` rows (`kind=sentiment`) — diff between latest and prior-period means. Already implementable from existing data.

**(C) Sentiment volume loader.** Sum `news_article_tickers` count per ticker over the rate-of-change window. Existing tables; no new ingestion needed.

**(D) News-price divergence detector.** Compare sentiment direction (sign of `directional_score`) vs. price direction over a configurable window (e.g., 5-day return). `divergence_flag=True` when they disagree by a configurable threshold.

**(E) Cleanup.** Update `load_active_thesis_summaries` docstring to document the real query. Remove the `noqa: ARG001` markers on `session` / `as_of`. Remove the `v1 stub` comments at lines 88-91 and 367-373. Repurpose `_THESES_STUB` in `input_bundle.py:187` as the empty-portfolio fallback message (drop the "[ALP-111](https://linear.app/alphamind-jatassi/issue/ALP-111/qualitative-research) pending" wording).

## Acceptance criteria

- [ ] `load_active_thesis_summaries` queries `theses` / `thesis_components` and returns real records.
- [ ] `SentimentAggregate.rate_of_change` populated from sentiment-baseline diff.
- [ ] `SentimentAggregate.volume` populated from `news_article_tickers` count.
- [ ] `SentimentAggregate.divergence_flag` populated from sentiment-vs-price comparison.
- [ ] Bundle renderer no longer shows `pending` for the three sentiment fields when source data is present.
- [ ] Qualitative-researcher rendering emits real thesis lines for active theses.
- [ ] `_THESES_STUB` no longer references "[ALP-111](https://linear.app/alphamind-jatassi/issue/ALP-111/qualitative-research) pending"; renders only when no active theses exist.
- [ ] Tests cover: active theses present, no active theses, only resolved/cancelled theses (filtered out); sentiment fields populated; sentiment fields fall back gracefully when source baseline is missing.
- [ ] `uv run pytest tests/analysis/qualitative_research/ -n auto` passes; lint chain clean per CLAUDE.md.

## Verification

* `grep -n "thesis model pending\|v1 stub" src/alphamind/analysis/qualitative_research/` returns zero hits.

## Consolidation note

This issue merges the original [ALP-515](https://linear.app/alphamind-jatassi/issue/ALP-515/load-active-thesis-summaries-returns-but-thesis-model-has-landed-stale) (active-thesis stub) and [ALP-517](https://linear.app/alphamind-jatassi/issue/ALP-517/sentimentaggregate-v1-stub-rate-of-change-volume-divergence-flag) (sentiment v1 stub fields) — both in the same module, same fix pattern, same LLM affected. [ALP-517](https://linear.app/alphamind-jatassi/issue/ALP-517/sentimentaggregate-v1-stub-rate-of-change-volume-divergence-flag) is marked as a duplicate of this issue.
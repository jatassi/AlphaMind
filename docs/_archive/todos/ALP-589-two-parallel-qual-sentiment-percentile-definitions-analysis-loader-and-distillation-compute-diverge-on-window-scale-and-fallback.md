# Two parallel `qual.sentiment_percentile` definitions — analysis loader and distillation compute diverge on window, scale, and fallback

## Context

[ALP-583](https://linear.app/alphamind-jatassi/issue/ALP-583/sentiment-percentilemagnitude-degenerate-calibrated-branch-always) fixed the degenerate `percentile_vs_self` / `magnitude` in `load_sentiment_aggregates` (`src/alphamind/analysis/qualitative_research/loaders.py`). During its PR review (PR #130) a parallel surface surfaced: the distillation layer already computes the *same concept* — a per-ticker percentile of a current sentiment reading against the ticker's own rolling `(mean, stdev)` baseline — in `compute_sentiment_percentile_blocks` (`src/alphamind/distillation/qualitative/sentiment_percentile_compute.py`, [ALP-487](https://linear.app/alphamind-jatassi/issue/ALP-487/07-follow-up-propagate-computeload-split-to-qualitative-derived)).

The two computations are independent and diverge on every axis except the underlying baseline. Two notions of "the current sentiment reading" in the codebase is a consistency and maintenance hazard: the qualitative researcher's input bundle reads one, the distillation `qual.sentiment_percentile` output block carries the other, and nothing keeps them aligned.

## Evidence

The analysis-layer loader and the distillation-layer compute diverge on every axis but the underlying rolling baseline.

* **Current-reading window.** Loader: the inter-baseline window (variable — the gap between the latest two sentiment baselines). Distillation: a fixed 4-hour trailing window (`SENTIMENT_PROXY_WINDOW_HOURS = 4`, `sentiment_percentile_compute.py:40`).
* **Output scale.** Loader: unit interval `[0.0, 1.0]`. Distillation: `0–100` (`_percentile_from_normal` returns `cdf * 100.0`, `sentiment_percentile_compute.py:116`).
* **Degenerate-stdev handling.** Loader: emits `None`. Distillation: returns `50.0` (`sentiment_percentile_compute.py:111-112`).
* **Sub-threshold fallback.** Loader: non-calibrated baselines emit an all-`None` record ([ALP-568](https://linear.app/alphamind-jatassi/issue/ALP-568/sentiment-pipeline-frozen-changevol-universally-zero-benchmarketf)). Distillation: falls back to a universe-pooled `(mean, stdev)` via `tag_with_fallback`.
* `_percentile_from_normal` **is duplicated.** `loaders.py:280` (unit interval) and `sentiment_percentile_compute.py:99` (0–100) are the same Normal-CDF formula with a scale factor. A third Normal-CDF, `_norm_cdf`, lives in `risk_guardrails/guardrail_evaluation/black_scholes.py:38`.

The analysis loader does not consume the distillation `qual.sentiment_percentile` block — it reads `distillation_ticker_baseline` directly and re-derives the percentile. Per the layered architecture, the analysis layer is meant to consume distillation outputs.

## Scope

Decide whether the analysis-layer `SentimentAggregate.percentile_vs_self` / `magnitude` should consume the distillation `qual.sentiment_percentile` block instead of re-deriving it, or whether the two surfaces are intentionally distinct (different windows serving different consumers) and should simply be documented as such. If kept separate, reconcile the incidental divergences (scale, degenerate-stdev handling) so a reader is not misled. Either way, extract the duplicated `_percentile_from_normal` to one shared location.

This is a design decision, not a mechanical fix — surface the consume-vs-keep-separate choice to the operator before implementing.

## Acceptance criteria

* A decision is recorded (in code and/or docs) on whether the analysis loader consumes the distillation block or computes its own; if kept separate, the rationale (distinct window semantics for distinct consumers) is documented at both call sites.
* `_percentile_from_normal` exists in exactly one place, imported by both `loaders.py` and `sentiment_percentile_compute.py` (scale handled by a flag or two named wrappers).
* The stale design-doc cross-reference in `docs/design/03-analysis-layer/qualitative-research.md:21` is corrected: it cites `distillation external.md §3` for sentiment calibration, but that content sits under `## 2. Technical indicators and derived metrics` of `external.md`.

## Notes

Discovered during [ALP-583](https://linear.app/alphamind-jatassi/issue/ALP-583/sentiment-percentilemagnitude-degenerate-calibrated-branch-always) PR review (PR #130). [ALP-583](https://linear.app/alphamind-jatassi/issue/ALP-583/sentiment-percentilemagnitude-degenerate-calibrated-branch-always) added a docstring note on `_load_window_sentiment_mean_by_ticker` acknowledging the divergence as a stopgap; this issue is the proper reconciliation.

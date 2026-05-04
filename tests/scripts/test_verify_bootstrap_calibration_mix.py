"""Unit tests for ``scripts/verify_bootstrap_calibration_mix.py`` (story 13).

The calibration-mix verifier reads ``distillation_ticker_baseline`` and
asserts the calibration-state distribution per ``baseline_kind`` matches
the qualitative warm-up estimate from
``docs/design/02-distillation-layer/threshold-calibration.md``
§ Warm-up duration estimate.

Coverage map per the story scope:

- Mostly-calibrated volume / ATR / spread → pass.
- Mostly-bootstrap sentiment → pass.
- Mostly-bootstrap lead-lag (over the pair-lag table) → pass.
- A volume distribution that is not mostly-calibrated → fail.
- Empty table → reports failure (cannot assert against zero rows).
- Summary table renders the documented columns.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from alphamind.persistence.models import (
    AssetUniverse,
    DistillationPairLag,
    DistillationTickerBaseline,
)
from alphamind.scripts.verify_bootstrap_calibration_mix import (
    CalibrationMixReport,
    KindDistribution,
    compute_calibration_mix_report,
    format_calibration_mix_report,
)

_AS_OF = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _seed_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            avg_daily_volume_shares=1_000_000,
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _seed_baseline(
    session: Session,
    *,
    ticker: str,
    kind: str,
    state: str,
    n_obs: int = 20,
    window_days: int = 20,
) -> None:
    ts = _AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        DistillationTickerBaseline(
            ticker=ticker,
            baseline_kind=kind,
            as_of=ts,
            mean=1_000_000.0,
            stdev=100_000.0,
            n_observations=n_obs,
            window_days=window_days,
            calibration_state=state,
            ingested_at=ts,
        )
    )


def _seed_pair(session: Session, *, lead: str, lag: str, state: str) -> None:
    ts = _AS_OF.strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        DistillationPairLag(
            lead_ticker=lead,
            lag_ticker=lag,
            as_of=ts,
            lead_lag_days_estimate=1.0,
            n_pair_events=5,
            last_overdue_flag=0,
            calibration_state=state,
            ingested_at=ts,
        )
    )


def _populate_typical_warm_up(session: Session) -> None:
    """Seed a distribution that matches the warm-up estimate on day 30+.

    - 9 of 10 volume baselines calibrated (≥ 80% per warm-up estimate).
    - Same for ATR / spread.
    - 7 of 10 sentiment baselines bootstrap (mostly-bootstrap on day 1-2).
    - 3 of 4 pair-lag rows bootstrap (event-driven).
    """
    for i in range(10):
        ticker = f"T{i:02d}"
        _seed_ticker(session, ticker)
    session.flush()
    for kind in ("volume", "atr", "spread"):
        for i in range(10):
            ticker = f"T{i:02d}"
            state = "calibrated" if i < 9 else "bootstrap"
            _seed_baseline(session, ticker=ticker, kind=kind, state=state)
    for i in range(10):
        ticker = f"T{i:02d}"
        state = "bootstrap" if i < 7 else "calibrated"
        _seed_baseline(session, ticker=ticker, kind="sentiment", state=state)
    pair_states = ("bootstrap", "bootstrap", "bootstrap", "calibrated")
    for i, state in enumerate(pair_states):
        lead = f"T{i * 2:02d}"
        lag = f"T{i * 2 + 1:02d}"
        _seed_pair(session, lead=lead, lag=lag, state=state)
    session.commit()


# ---------------------------------------------------------------------------
# Tracer-bullet: typical post-bootstrap distribution → pass
# ---------------------------------------------------------------------------


def test_typical_warm_up_distribution_passes(session: Session) -> None:
    """A distribution within the warm-up bands → report.passed is True."""
    _populate_typical_warm_up(session)

    report = compute_calibration_mix_report(session=session)

    assert isinstance(report, CalibrationMixReport)
    assert report.passed is True
    assert report.failures == ()


def test_underaged_volume_baseline_fails(session: Session) -> None:
    """When < 80% of volume baselines are calibrated, the report fails."""
    for i in range(10):
        ticker = f"T{i:02d}"
        _seed_ticker(session, ticker)
    session.flush()
    # 5 of 10 volume baselines calibrated → below 80% lower bound.
    for i in range(10):
        ticker = f"T{i:02d}"
        state = "calibrated" if i < 5 else "bootstrap"
        _seed_baseline(session, ticker=ticker, kind="volume", state=state)
    session.commit()

    report = compute_calibration_mix_report(session=session)

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "calibration-distribution-out-of-band" in codes


def test_empty_table_fails(session: Session) -> None:
    """An empty distillation_ticker_baseline cannot satisfy the warm-up assertion."""
    report = compute_calibration_mix_report(session=session)

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "no-baseline-rows" in codes


def test_kind_distributions_have_documented_kinds(session: Session) -> None:
    """The report names every documented baseline kind plus the pair-lag kind."""
    _populate_typical_warm_up(session)

    report = compute_calibration_mix_report(session=session)

    kinds = {dist.kind for dist in report.distributions}
    # The documented per-ticker baseline kinds plus the lead-lag pair table.
    assert "volume" in kinds
    assert "atr" in kinds
    assert "spread" in kinds
    assert "sentiment" in kinds
    assert "lead_lag" in kinds


def test_format_renders_summary_table(session: Session) -> None:
    """The renderer prints a per-kind row with calibrated / bootstrap counts."""
    _populate_typical_warm_up(session)

    report = compute_calibration_mix_report(session=session)
    rendered = format_calibration_mix_report(report)

    assert "Calibration Mix" in rendered
    assert "volume" in rendered
    assert "sentiment" in rendered
    assert "lead_lag" in rendered
    assert "RESULT: PASS" in rendered


def test_kind_distribution_records_share(session: Session) -> None:
    """``KindDistribution.calibrated_share`` is observed/total."""
    _populate_typical_warm_up(session)

    report = compute_calibration_mix_report(session=session)
    by_kind = {dist.kind: dist for dist in report.distributions}

    volume = by_kind["volume"]
    assert isinstance(volume, KindDistribution)
    assert volume.total == 10
    assert volume.calibrated == 9
    assert volume.bootstrap == 1
    assert 0.85 < volume.calibrated_share <= 1.0


# ---------------------------------------------------------------------------
# Cold-start exemption (ALP-273): fresh-DB first invocation should not fail
# ---------------------------------------------------------------------------


def _populate_cold_start(session: Session, *, universe_size: int = 10) -> None:
    """Seed a fresh-DB first-invocation snapshot.

    Every per-ticker baseline is ``bootstrap`` with ``n_observations < window_days``
    — the rolling window has not yet filled — and one row per ticker covers the
    configured universe. Lead-lag has no rows (event-driven, accumulates over
    weeks).
    """
    for i in range(universe_size):
        ticker = f"T{i:02d}"
        _seed_ticker(session, ticker)
    session.flush()
    for kind in ("volume", "atr", "spread", "sentiment"):
        for i in range(universe_size):
            ticker = f"T{i:02d}"
            _seed_baseline(
                session,
                ticker=ticker,
                kind=kind,
                state="bootstrap",
                n_obs=5,
                window_days=252,
            )
    session.commit()


def test_cold_start_signature_passes_with_deferred_status(session: Session) -> None:
    """Fresh DB on day 1: high-freq bands report DEFERRED, run passes."""
    _populate_cold_start(session, universe_size=10)

    report = compute_calibration_mix_report(session=session, ticker_universe_size=10)

    assert report.passed is True
    assert report.failures == ()
    by_kind = {dist.kind: dist for dist in report.distributions}
    for kind in ("volume", "atr", "spread"):
        assert by_kind[kind].cold_start_deferred is True
        assert by_kind[kind].in_band is False
    # Sentiment trivially satisfies its upper-bound band on cold start;
    # the deferred flag is reserved for kinds with a lower bound that
    # would otherwise have failed.
    assert by_kind["sentiment"].cold_start_deferred is False
    assert by_kind["sentiment"].in_band is True


def test_cold_start_renderer_shows_deferred(session: Session) -> None:
    """The summary table renders ``DEFERRED`` for cold-start high-freq kinds."""
    _populate_cold_start(session, universe_size=10)

    report = compute_calibration_mix_report(session=session, ticker_universe_size=10)
    rendered = format_calibration_mix_report(report)

    assert "DEFERRED" in rendered
    assert "RESULT: PASS" in rendered


def test_cold_start_skipped_when_universe_coverage_partial(session: Session) -> None:
    """All-bootstrap underaged but row count < universe → still fails.

    Defends against misreading a stale partial state (some tickers never
    processed) as cold-start. Drops the cold-start exemption when the
    orchestrator has not covered the full universe.
    """
    # Seed only 3 of 10 tickers.
    _populate_cold_start(session, universe_size=3)

    report = compute_calibration_mix_report(session=session, ticker_universe_size=10)

    assert report.passed is False
    by_kind = {dist.kind: dist for dist in report.distributions}
    assert by_kind["volume"].cold_start_deferred is False
    codes = {f.code for f in report.failures}
    assert "calibration-distribution-out-of-band" in codes


def test_cold_start_skipped_when_window_already_filled(session: Session) -> None:
    """All-bootstrap but ``n_observations >= window_days`` → not cold-start.

    The ``n_observations < window_days`` clause distinguishes a fresh DB
    from a long-running DB whose calibration is wedged in bootstrap for an
    unrelated reason (e.g. ``min_observations`` raised above the window).
    """
    for i in range(10):
        _seed_ticker(session, f"T{i:02d}")
    session.flush()
    for i in range(10):
        _seed_baseline(
            session,
            ticker=f"T{i:02d}",
            kind="volume",
            state="bootstrap",
            n_obs=252,
            window_days=252,
        )
    session.commit()

    report = compute_calibration_mix_report(session=session, ticker_universe_size=10)

    assert report.passed is False
    by_kind = {dist.kind: dist for dist in report.distributions}
    assert by_kind["volume"].cold_start_deferred is False


def test_cold_start_skipped_when_a_row_is_calibrated(session: Session) -> None:
    """Mixed bootstrap + calibrated → not cold-start; band failure stands."""
    for i in range(10):
        _seed_ticker(session, f"T{i:02d}")
    session.flush()
    for i in range(10):
        # 5 calibrated, 5 bootstrap → calibrated_share=0.5, below 0.80 lower bound.
        state = "calibrated" if i < 5 else "bootstrap"
        _seed_baseline(
            session,
            ticker=f"T{i:02d}",
            kind="volume",
            state=state,
            n_obs=5 if state == "bootstrap" else 200,
            window_days=252,
        )
    session.commit()

    report = compute_calibration_mix_report(session=session, ticker_universe_size=10)

    assert report.passed is False
    by_kind = {dist.kind: dist for dist in report.distributions}
    assert by_kind["volume"].cold_start_deferred is False

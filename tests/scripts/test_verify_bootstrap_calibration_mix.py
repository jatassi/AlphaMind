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
            window_days=20,
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

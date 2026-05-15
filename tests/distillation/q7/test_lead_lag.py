"""Tests for ``q7_cross_asset.compute_lead_lag`` — story 08d.

Cover the lead-lag overdue flag and the lead-lag regime-shift / inversion
detection. The function reads the persisted lead-lag estimate from
``distillation_pair_lag`` (story 03 / 07) and inspects recent lead vs. lag
returns from ``ohlcv_bars``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q7 import (
    LeadLagPair,
    compute_lead_lag,
)
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationPairLag,
    OhlcvBars,
)
from alphamind.persistence.session import make_engine, make_session_factory


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    factory = make_session_factory(engine)
    with factory() as sess:
        yield sess


def _add_ticker(session: Session, ticker: str) -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker}",
            asset_class="equity",
            asset_role="universe",
            exchange="NYSE",
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


def _add_close(session: Session, *, ticker: str, period_start: str, close: float) -> None:
    session.add(
        OhlcvBars(
            ticker=ticker,
            timeframe="1d",
            period_start=period_start,
            period_end=period_start,
            session="regular",
            adj_open=close,
            adj_high=close,
            adj_low=close,
            adj_close=close,
            adj_volume=1_000_000,
            adj_vwap=close,
            unadj_open=close,
            unadj_high=close,
            unadj_low=close,
            unadj_close=close,
            unadj_volume=1_000_000,
            unadj_vwap=close,
            trade_count=None,
            source="test",
            ingested_at="2026-04-26T00:00:00Z",
        )
    )


def _seed_path(
    session: Session,
    *,
    ticker: str,
    closes: list[float],
    start_day: datetime,
) -> None:
    _add_ticker(session, ticker)
    for i, close in enumerate(closes):
        ts = (start_day + timedelta(days=i)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        _add_close(session, ticker=ticker, period_start=ts, close=close)


def _add_pair_lag_row(
    session: Session,
    *,
    lead: str,
    lag: str,
    as_of: datetime,
    estimate_days: float = 1.0,
    n_pair_events: int = 30,
    state: CalibrationState = CalibrationState.CALIBRATED,
) -> None:
    session.add(
        DistillationPairLag(
            lead_ticker=lead,
            lag_ticker=lag,
            as_of=as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            lead_lag_days_estimate=estimate_days,
            n_pair_events=n_pair_events,
            last_overdue_flag=0,
            calibration_state=state.value,
            ingested_at=as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )


# ---------------------------------------------------------------------------
# Overdue flag — fires when lead has moved >=1.5sigma but lag hasn't tracked
# ---------------------------------------------------------------------------


class TestLeadLagOverdueFlag:
    def test_overdue_flag_fires_when_lead_moved_but_lag_did_not(self, session: Session) -> None:
        # SMH (lead) moves +5% over the most recent day; QQQ (lag) is flat.
        # The pair's ``_max_days`` is 2; the lead's z-score against its
        # trailing returns is well above 1.5 → overdue flag fires.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=21)
        # 20 days of small-volatility returns plus a final spike for SMH only.
        smh_returns = [0.001] * 19 + [0.05]  # last day +5%
        qqq_returns = [0.001] * 20  # flat throughout
        smh_closes = [100.0]
        qqq_closes = [200.0]
        for r in smh_returns:
            smh_closes.append(smh_closes[-1] * (1.0 + r))
        for r in qqq_returns:
            qqq_closes.append(qqq_closes[-1] * (1.0 + r))
        _seed_path(session, ticker=Symbol("SMH"), closes=smh_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("QQQ"), closes=qqq_closes, start_day=start_day)
        _add_pair_lag_row(
            session,
            lead="SMH",
            lag="QQQ",
            as_of=as_of,
            estimate_days=1.0,
            n_pair_events=30,
        )
        session.commit()

        blocks = compute_lead_lag(
            session,
            pairs=(
                LeadLagPair(
                    pair_key="semis_to_tech",
                    lead_ticker="SMH",
                    lag_ticker="QQQ",
                    max_days=2,
                ),
            ),
            as_of=as_of,
            overdue_lead_sigma=1.5,
        )

        # One block per pair.
        assert len(blocks) == 1
        block = blocks[0]
        assert block.block_id == "q7.lead_lag.semis_to_tech"
        assert OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
        names = [flag.name for flag in block.anomaly_flags]
        assert any("overdue_lag_flag" in name for name in names), (
            f"expected overdue_lag_flag, got {names}"
        )

    def test_overdue_flag_suppressed_when_lag_tracked(self, session: Session) -> None:
        # SMH up 5%, QQQ also up 5% on the next day → lag tracked; no flag.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=21)
        smh_returns = [0.001] * 19 + [0.05]
        qqq_returns = [0.001] * 19 + [0.05]  # also up 5% — lag tracked
        smh_closes = [100.0]
        qqq_closes = [200.0]
        for r in smh_returns:
            smh_closes.append(smh_closes[-1] * (1.0 + r))
        for r in qqq_returns:
            qqq_closes.append(qqq_closes[-1] * (1.0 + r))
        _seed_path(session, ticker=Symbol("SMH"), closes=smh_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("QQQ"), closes=qqq_closes, start_day=start_day)
        _add_pair_lag_row(
            session,
            lead="SMH",
            lag="QQQ",
            as_of=as_of,
            estimate_days=1.0,
            n_pair_events=30,
        )
        session.commit()

        blocks = compute_lead_lag(
            session,
            pairs=(
                LeadLagPair(
                    pair_key="semis_to_tech",
                    lead_ticker="SMH",
                    lag_ticker="QQQ",
                    max_days=2,
                ),
            ),
            as_of=as_of,
            overdue_lead_sigma=1.5,
        )

        block = blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert not any("overdue_lag_flag" in name for name in names), (
            f"unexpected overdue_lag_flag (lag tracked): {names}"
        )

    def test_inversion_flag_fires_when_lag_moves_first(self, session: Session) -> None:
        # The "lag" asset (QQQ) makes a large move first; the "lead" (SMH)
        # follows. This is the regime-shift / inversion case.
        as_of = datetime(2026, 4, 30, tzinfo=UTC)
        start_day = as_of - timedelta(days=21)
        # Day 19 (penultimate): QQQ moves +5%; SMH is flat.
        # Day 20 (final): SMH moves +5%; QQQ flat.
        smh_returns = [0.001] * 18 + [0.001, 0.05]
        qqq_returns = [0.001] * 18 + [0.05, 0.001]
        smh_closes = [100.0]
        qqq_closes = [200.0]
        for r in smh_returns:
            smh_closes.append(smh_closes[-1] * (1.0 + r))
        for r in qqq_returns:
            qqq_closes.append(qqq_closes[-1] * (1.0 + r))
        _seed_path(session, ticker=Symbol("SMH"), closes=smh_closes, start_day=start_day)
        _seed_path(session, ticker=Symbol("QQQ"), closes=qqq_closes, start_day=start_day)
        _add_pair_lag_row(
            session,
            lead="SMH",
            lag="QQQ",
            as_of=as_of,
            estimate_days=1.0,
            n_pair_events=30,
        )
        session.commit()

        blocks = compute_lead_lag(
            session,
            pairs=(
                LeadLagPair(
                    pair_key="semis_to_tech",
                    lead_ticker="SMH",
                    lag_ticker="QQQ",
                    max_days=2,
                ),
            ),
            as_of=as_of,
            overdue_lead_sigma=1.5,
        )

        block = blocks[0]
        names = [flag.name for flag in block.anomaly_flags]
        assert any("inversion_flag" in name for name in names), (
            f"expected inversion_flag, got {names}"
        )

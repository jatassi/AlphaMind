"""Tests for input-bundle integrity diagnostics — ALP-492.

Covers the four data-pipeline integrity gaps surfaced by the 2026-05-16
e2e run:

- Gap 1: realized_vol_5d and realized_vol_20d both exactly 0.0.
- Gap 2: sentiment aggregates empty despite N calibrated baselines.
- Gap 3: prediction-market snapshot empty despite contracts with history.
- Gap 4: news-ticker label drifts from headline content.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind._kernel.calibration import CalibrationState
from alphamind.analysis.qualitative_research import input_bundle_integrity as integrity
from alphamind.analysis.qualitative_research.input_bundle_integrity import (
    SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD,
    log_input_bundle_integrity_warnings,
)
from alphamind.analysis.qualitative_research.loaders import (
    PredictionMarketSnapshot,
    QualitativeInputs,
    SentimentAggregate,
)
from alphamind.analysis.qualitative_research.news_digest import DigestEntry, NewsDigest
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    DistillationContractHistory,
    DistillationTickerBaseline,
    PredictionMarketContracts,
)
from alphamind.persistence.session import make_engine, make_session_factory

AS_OF = datetime(2026, 5, 16, 17, 0, tzinfo=UTC)
EARLIER_ISO = (AS_OF - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    sf = make_session_factory(engine)
    with sf() as sess:
        yield sess


def _ticker_row(ticker: str) -> AssetUniverse:
    return AssetUniverse(
        asset_id=f"asset-{ticker.lower()}",
        ticker=ticker,
        full_name=ticker,
        asset_class="equity",
        asset_role="universe",
        exchange="NASDAQ",
        is_active=1,
        added_date="2026-01-01",
        last_updated="2026-01-01T00:00:00Z",
    )


def _baseline(
    session: Session,
    ticker: str,
    *,
    mean: float = 0.0,
    stdev: float = 1.0,
    n_observations: int = 30,
    calibration_state: str = "calibrated",
    as_of_str: str = EARLIER_ISO,
) -> None:
    session.add(_ticker_row(ticker))
    session.add(
        DistillationTickerBaseline(
            ticker=ticker,
            baseline_kind="sentiment",
            as_of=as_of_str,
            mean=mean,
            stdev=stdev,
            n_observations=n_observations,
            window_days=90,
            calibration_state=calibration_state,
            ingested_at=as_of_str,
        )
    )


def _contract_row(contract_id: str) -> PredictionMarketContracts:
    return PredictionMarketContracts(
        contract_id=contract_id,
        platform="polymarket",
        description="x",
        category="macro",
        resolution_date="2026-12-31",
        resolution_outcome=None,
        created_at="2026-01-01T00:00:00Z",
        last_seen_at=EARLIER_ISO,
    )


def _history(session: Session, contract_id: str, *, snapshot_ts: str = EARLIER_ISO) -> None:
    session.add(_contract_row(contract_id))
    session.flush()
    session.add(
        DistillationContractHistory(
            contract_id=contract_id,
            snapshot_ts=snapshot_ts,
            yes_probability=0.5,
            delta_pp_since_prior=0.0,
            liquidity_usd=100_000.0,
            calibration_state="calibrated",
            ingested_at=snapshot_ts,
        )
    )


def _sentiment(ticker: str = "AAPL") -> SentimentAggregate:
    return SentimentAggregate(
        ticker=ticker,
        directional_score=0.1,
        magnitude=0.2,
        rate_of_change=None,
        volume=None,
        divergence_flag=None,
        percentile_vs_self=0.5,
        data_freshness=AS_OF,
        calibration_state=CalibrationState.CALIBRATED,
    )


def _prediction_market(contract_id: str = "0x01") -> PredictionMarketSnapshot:
    return PredictionMarketSnapshot(
        contract_id=contract_id,
        description="x",
        platform="polymarket",
        category="macro",
        current_probability=0.5,
        delta_since_last_invocation_pp=0.0,
        delta_since_prior_pp=0.0,
        volume_24h_usd=50_000.0,
        expiration="2026-06-01",
        is_low_liquidity=False,
        meets_threshold_flag=False,
        is_stale_low_signal=False,
        data_freshness=AS_OF,
    )


def _inputs(
    *,
    sentiment: tuple[SentimentAggregate, ...] = (),
    prediction_markets: tuple[PredictionMarketSnapshot, ...] = (),
) -> QualitativeInputs:
    return QualitativeInputs(
        sentiment_aggregates=sentiment,
        prediction_markets=prediction_markets,
        events=(),
        theses=(),
        data_freshness=AS_OF,
    )


def _digest_entry(
    *, reference_id: str = "ND-T1", headline: str = "", tickers: tuple[str, ...] = ()
) -> DigestEntry:
    return DigestEntry(
        reference_id=reference_id,
        cluster_id=None,
        published_at=AS_OF,
        tier="tier_3",
        headline=headline,
        source_outlet="Yahoo",
        tickers=tickers,
        tags=(),
    )


def _digest(*entries: DigestEntry) -> NewsDigest:
    return NewsDigest(
        as_of=AS_OF,
        last_invocation_time=AS_OF - timedelta(hours=1),
        total_collected=len(entries),
        total_shown=len(entries),
        entries=tuple(entries),
        digest_text="",
    )


def _gap_records(caplog: pytest.LogCaptureFixture, gap: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if gap in r.getMessage()]


# ---------------------------------------------------------------------------
# Gap 1 — realized-vol double-zero
# ---------------------------------------------------------------------------


def test_gap1_double_zero_emits_warning(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    regime_label = {"realized_vol_5d": 0.0, "realized_vol_20d": 0.0}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert _gap_records(caplog, "Gap 1"), "expected Gap 1 WARN"


@pytest.mark.parametrize(
    "regime_label",
    [
        {},
        {"realized_vol_5d": 0.0},
        {"realized_vol_20d": 0.0},
        {"realized_vol_5d": None, "realized_vol_20d": None},
    ],
)
def test_gap1_missing_or_none_keys_no_warning(
    session: Session,
    caplog: pytest.LogCaptureFixture,
    regime_label: dict[str, Any],
) -> None:
    # Locks in current behavior: missing or None values are not the
    # empty-bar-window signature. A separate detector would be needed to
    # treat a *missing* regime block as a payload-drop failure.
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 1")


@pytest.mark.parametrize(
    ("rv_5d", "rv_20d"),
    [(0.0, 0.05), (0.05, 0.0), (0.05, 0.06)],
)
def test_gap1_not_both_zero_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture, rv_5d: float, rv_20d: float
) -> None:
    regime_label = {"realized_vol_5d": rv_5d, "realized_vol_20d": rv_20d}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 1"), "Gap 1 WARN should not fire when only one is 0.0"


# ---------------------------------------------------------------------------
# Gap 2 — sentiment-aggregate empty despite calibrated baselines
# ---------------------------------------------------------------------------


def test_gap2_sentiment_present_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Populate calibrated baselines but sentiment_aggregates is non-empty:
    # the WARN should not fire.
    for i in range(SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD + 1):
        _baseline(session, f"TKR{i}")
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 2")


def test_gap2_sentiment_empty_below_threshold_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Calibrated count below threshold — assume bootstrap window, no WARN.
    for i in range(SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD - 1):
        _baseline(session, f"TKR{i}")
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 2")


def test_gap2_sentiment_empty_above_threshold_emits_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    for i in range(SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD + 2):
        _baseline(session, f"TKR{i}")
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    records = _gap_records(caplog, "Gap 2")
    assert records, "expected Gap 2 WARN"
    assert str(SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD + 2) in records[0].getMessage()


def test_gap2_only_bootstrap_baselines_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Bootstrap baselines do not count toward the calibrated threshold.
    for i in range(SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD + 5):
        _baseline(session, f"TKR{i}", calibration_state="accumulating")
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 2")


# ---------------------------------------------------------------------------
# Gap 3 — prediction-market snapshot empty despite contracts with history
# ---------------------------------------------------------------------------


def test_gap3_pm_present_no_warning(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    _history(session, "0x01")
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 3")


def test_gap3_pm_empty_no_history_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 3")


def test_gap3_pm_empty_with_history_emits_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    _history(session, "0x01")
    _history(session, "0x02")
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),)),
            news_digest=_digest(),
        )
    records = _gap_records(caplog, "Gap 3")
    assert records
    assert "2" in records[0].getMessage()


def test_gap3_future_history_only_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # History snapshot rows in the future (after as_of) should not count.
    future_ts = (AS_OF + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _history(session, "0x01", snapshot_ts=future_ts)
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 3")


def test_gap3_resolved_contracts_excluded_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Resolved contracts (resolution_date <= as_of) should not count toward
    # the "contract scope is non-empty" precondition — they mirror the
    # contract_scope resolver's exclusion.
    past = (AS_OF - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    session.add(
        PredictionMarketContracts(
            contract_id="0xRESOLVED",
            platform="polymarket",
            description="x",
            category="macro",
            resolution_date=past,
            resolution_outcome="yes",
            created_at="2026-01-01T00:00:00Z",
            last_seen_at=past,
        )
    )
    session.flush()
    session.add(
        DistillationContractHistory(
            contract_id="0xRESOLVED",
            snapshot_ts=EARLIER_ISO,
            yes_probability=0.5,
            delta_pp_since_prior=0.0,
            liquidity_usd=100_000.0,
            calibration_state="calibrated",
            ingested_at=EARLIER_ISO,
        )
    )
    session.commit()

    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),)),
            news_digest=_digest(),
        )
    assert not _gap_records(caplog, "Gap 3")


# ---------------------------------------------------------------------------
# Gap 4 — news ticker labeling drift
# ---------------------------------------------------------------------------


def test_gap4_labeled_ticker_in_headline_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _digest_entry(headline="Microsoft cloud revenue jumps after AI push", tickers=("MSFT",))
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not _gap_records(caplog, "Gap 4")


def test_gap4_symbol_in_headline_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _digest_entry(headline="NVDA breaks above prior high", tickers=("NVDA",))
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not _gap_records(caplog, "Gap 4")


def test_gap4_labeled_msft_but_headline_says_nvidia_emits_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _digest_entry(
        reference_id="ND-T3",
        headline="Nvidia overtakes silver to become world's second-largest asset",
        tickers=("MSFT",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    records = _gap_records(caplog, "Gap 4")
    assert records
    msg = records[0].getMessage()
    assert "ND-T3" in msg
    assert "MSFT" in msg
    assert "NVDA" in msg


def test_gap4_labeled_goog_but_headline_says_nvidia_emits_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    entry = _digest_entry(
        reference_id="ND-T4",
        headline="Nvidia Overtakes Silver To Become World's Second-Largest Asset At $5.52 Trillion",
        tickers=("GOOG",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    records = _gap_records(caplog, "Gap 4")
    assert records
    msg = records[0].getMessage()
    assert "ND-T4" in msg
    assert "NVDA" in msg


def test_gap4_labeled_ticker_absent_but_no_other_in_headline_no_warning(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Labeled NVDA but headline doesn't reference any high-profile ticker.
    entry = _digest_entry(
        headline="If You Want Commodity Exposure Without Futures Contracts, Start Here",
        tickers=("NVDA",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not _gap_records(caplog, "Gap 4")


def test_gap4_labeled_ticker_outside_alias_map_no_check(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Labeled SLB (not in alias map) — no drift check applied.
    entry = _digest_entry(
        headline="Nvidia breakout drives semis higher",
        tickers=("SLB",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not _gap_records(caplog, "Gap 4")


def test_gap4_googl_class_share_no_warning_when_goog_in_headline(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # GOOG and GOOGL share aliases — labeling one when the other is in the
    # headline must NOT flag drift.
    entry = _digest_entry(
        headline="Alphabet's Google search revenue accelerates",
        tickers=("GOOGL",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not _gap_records(caplog, "Gap 4")


def test_gap4_word_boundary_avoids_substring_false_positive(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # "Apple" inside "applesauce" or "applet" must not match the AAPL alias —
    # the matcher uses word boundaries so it doesn't confuse common English
    # words with company names.
    entry = _digest_entry(
        headline="Pineapple festival draws record crowd in Florida",
        tickers=("MSFT",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not _gap_records(caplog, "Gap 4")


def test_gap4_multiple_other_tickers_sorted_alphabetically(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # When more than one high-profile ticker is referenced in the headline,
    # the WARN message lists them alphabetically — locks the
    # ``tuple(sorted(...))`` contract in _drift_other_tickers.
    entry = _digest_entry(
        reference_id="ND-T9",
        headline="Apple and Microsoft strike AI deal that sidelines smaller chip makers",
        tickers=("NVDA",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    records = _gap_records(caplog, "Gap 4")
    assert records
    assert "AAPL, MSFT" in records[0].getMessage()


def test_gap4_message_includes_truncated_headline(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    long_headline = (
        "Nvidia's record-setting trillion-dollar AI infrastructure announcement "
        "sends markets surging in unexpected ways across every sector watched today"
    )
    entry = _digest_entry(
        reference_id="ND-T10",
        headline=long_headline,
        tickers=("MSFT",),
    )
    regime_label = {"realized_vol_5d": 0.1, "realized_vol_20d": 0.1}
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label=regime_label,
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    records = _gap_records(caplog, "Gap 4")
    assert records
    msg = records[0].getMessage()
    # First few words of the headline should appear in the WARN so an operator
    # grepping the log can identify the story without cross-referencing.
    assert "Nvidia's record-setting" in msg
    # The full headline is longer than 80 chars — message must include the
    # ellipsis sentinel to signal truncation.
    assert "…" in msg


# ---------------------------------------------------------------------------
# Combined invocation
# ---------------------------------------------------------------------------


def test_all_four_gaps_emit_independently(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    # Gap 1: rv both 0.0.
    # Gap 2: empty sentiment with N+1 calibrated baselines.
    # Gap 3: empty PM with history rows.
    # Gap 4: NVDA-titled story labeled MSFT.
    for i in range(SENTIMENT_CALIBRATED_BASELINE_WARN_THRESHOLD + 1):
        _baseline(session, f"TKR{i}")
    _history(session, "0x01")
    session.commit()

    entry = _digest_entry(
        reference_id="ND-T3",
        headline="Nvidia AI revenue continues climb",
        tickers=("MSFT",),
    )
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label={"realized_vol_5d": 0.0, "realized_vol_20d": 0.0},
            inputs=_inputs(),
            news_digest=_digest(entry),
        )
    assert _gap_records(caplog, "Gap 1")
    assert _gap_records(caplog, "Gap 2")
    assert _gap_records(caplog, "Gap 3")
    assert _gap_records(caplog, "Gap 4")


def test_clean_inputs_emit_no_warnings(session: Session, caplog: pytest.LogCaptureFixture) -> None:
    entry = _digest_entry(headline="Microsoft Azure margins beat", tickers=("MSFT",))
    with caplog.at_level(logging.WARNING, logger=integrity.__name__):
        log_input_bundle_integrity_warnings(
            session,
            as_of=AS_OF,
            regime_label={"realized_vol_5d": 0.12, "realized_vol_20d": 0.15},
            inputs=_inputs(sentiment=(_sentiment(),), prediction_markets=(_prediction_market(),)),
            news_digest=_digest(entry),
        )
    assert not [r for r in caplog.records if "ALP-492" in r.getMessage()]

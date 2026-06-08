"""Distillation anomaly-flag → activity_log mapping + emission (ALP-881 / story 04b).

Two layers under test:

- The pure mapper :func:`anomaly_summary_to_activity_log_entry` in
  ``distillation.aggregation`` — turns one :class:`AnomalySummary` into a typed
  ``DISTILLATION_ANOMALY_FLAG`` :class:`ActivityLogEntry`, populating the detail
  payload from the originating flag/summary and resolving
  ``threshold_class`` / ``threshold_key`` via ``resolve_flag_taxonomy``.
- The orchestrator emission hook — a distillation run that produces N flags
  writes N anomaly-typed rows, queryable by the anomaly ``EventType`` (the
  shape ALP-96's reporter consumes), each carrying the running invocation FK.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind._kernel.calibration import CalibrationState
from alphamind.distillation.aggregation import (
    AnomalySummary,
    anomaly_summary_to_activity_log_entry,
)
from alphamind.distillation.output import AnomalyFlag, OutputAudience
from alphamind.portfolio_state.events import (
    DistillationAnomalyFlagDetail,
    EventGroup,
    EventSource,
    EventType,
)

_AS_OF = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)


def _summary(
    *,
    name: str,
    magnitude: float = 3.5,
    severity: str = "investigate_now",
    block_id: str = "q1.volume.NVDA",
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
) -> AnomalySummary:
    return AnomalySummary(
        flag=AnomalyFlag(name=name, magnitude=magnitude, severity=severity),  # type: ignore[arg-type]
        source_block_id=block_id,
        audiences=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        flagged_at=_AS_OF,
        calibration_state=calibration_state,
    )


def test_mapper_builds_distillation_anomaly_flag_entry() -> None:
    """A market-wide flag maps to a ``DISTILLATION_ANOMALY_FLAG`` entry with the
    detail populated from the flag/summary and the taxonomy resolved."""
    summary = _summary(name="volume_anomaly", magnitude=4.2, severity="investigate_now")

    entry = anomaly_summary_to_activity_log_entry(summary, invocation_id="inv-1", timestamp=_AS_OF)

    assert entry.invocation_id == "inv-1"
    assert entry.timestamp == _AS_OF
    assert entry.event_type is EventType.DISTILLATION_ANOMALY_FLAG
    assert entry.event_group is EventGroup.DISTILLATION_ANOMALY
    assert entry.source is EventSource.DISTILLATION_ORCHESTRATOR
    assert entry.position_id is None
    assert entry.order_id is None
    assert entry.thesis_id is None

    detail = entry.detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    # resolve_flag_taxonomy("volume_anomaly") -> ("anomaly_detection", "volume_anomaly_sigma")
    assert detail.threshold_class == "anomaly_detection"
    assert detail.threshold_key == "volume_anomaly_sigma"
    assert detail.magnitude == 4.2
    assert detail.severity == "investigate_now"
    assert detail.calibration_state is CalibrationState.CALIBRATED
    assert detail.block_id == "q1.volume.NVDA"
    # Market-wide flag (no embedded ticker) -> None.
    assert detail.ticker is None


def test_mapper_entry_id_is_deterministic_for_idempotency() -> None:
    """Two builds for one ``(invocation_id, source_block_id, flag.name)`` share an entry_id.

    The orchestrator persists the returned entry on its own session; a stable PK
    keeps a re-run of the same invocation from accumulating duplicate anomaly rows.
    """
    summary = _summary(name="volume_anomaly", block_id="q1.volume.NVDA")

    first = anomaly_summary_to_activity_log_entry(summary, invocation_id="inv-1", timestamp=_AS_OF)
    second = anomaly_summary_to_activity_log_entry(summary, invocation_id="inv-1", timestamp=_AS_OF)

    assert first.entry_id == second.entry_id
    assert first.entry_id == (
        f"inv-1-{EventType.DISTILLATION_ANOMALY_FLAG.value}-q1.volume.NVDA-volume_anomaly"
    )


def test_mapper_extracts_ticker_from_ticker_bearing_flag() -> None:
    """``correlation_locus_flag:{ticker}`` carries the ticker into the detail."""
    summary = _summary(name="correlation_locus_flag:NVDA", block_id="q7.correlation_locus.NVDA")

    detail = anomaly_summary_to_activity_log_entry(
        summary, invocation_id="inv-1", timestamp=_AS_OF
    ).detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    assert detail.ticker == "NVDA"
    # The dynamic suffix is stripped before taxonomy resolution.
    assert detail.threshold_class == "narrative_lag"
    assert detail.threshold_key == "correlation_locus_pair_count_threshold"


def test_mapper_extracts_etf_ticker_from_divergence_flag() -> None:
    """``etf_vs_single_name_divergence:{etf}`` carries the ETF into the detail (ALP-934).

    The ETF *is* a symbol, so the per-subject suffix doubles as ticker attribution —
    unlike the sector-audience suffix on ``q12_event_novelty`` below.
    """
    summary = _summary(
        name="etf_vs_single_name_divergence:SPY",
        block_id="q12.etf_vs_single_name_divergence",
    )

    detail = anomaly_summary_to_activity_log_entry(
        summary, invocation_id="inv-1", timestamp=_AS_OF
    ).detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    assert detail.ticker == "SPY"
    # Suffix stripped before taxonomy resolution.
    assert detail.threshold_class == "corporate_actions"
    assert detail.threshold_key == "etf_vs_single_name_divergence"


def test_mapper_event_novelty_flag_has_no_ticker() -> None:
    """``q12_event_novelty:{audience}`` embeds a sector audience, not a symbol → ticker ``None``.

    Mirrors ``prediction_market_delta:{contract_id}``: the suffix disambiguates the
    entry_id without claiming a single subject ticker (ALP-934).
    """
    summary = _summary(
        name="q12_event_novelty:sector_financials",
        block_id="q12.event_novelty",
    )

    detail = anomaly_summary_to_activity_log_entry(
        summary, invocation_id="inv-1", timestamp=_AS_OF
    ).detail
    assert isinstance(detail, DistillationAnomalyFlagDetail)
    assert detail.ticker is None
    assert detail.threshold_class == "corporate_actions"
    assert detail.threshold_key == "q12_event_novelty"


def test_mapper_pair_key_flag_has_no_ticker() -> None:
    """A pair / pair-key flag (two-segment or pair-key suffix) resolves ticker to ``None``.

    ``correlation_breakdown_flag:{row}:{col}`` is a pair; ``overdue_lag_flag:{pair_key}``
    is a pair-key. Neither names a single subject ticker, so the detail's ``ticker``
    is ``None`` even though a ``:``-suffix is present.
    """
    pair = _summary(name="correlation_breakdown_flag:NVDA:AMD", block_id="q7.cbd.NVDA_AMD")
    pair_key = _summary(name="overdue_lag_flag:SOXX_QQQ", block_id="q7.lead_lag.SOXX_QQQ")

    for summary in (pair, pair_key):
        detail = anomaly_summary_to_activity_log_entry(
            summary, invocation_id="inv-1", timestamp=_AS_OF
        ).detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        assert detail.ticker is None


def test_mapper_per_ticker_q1_anomalies_get_distinct_entry_ids_and_ticker() -> None:
    """Two per-ticker q1 flags of one threshold → distinct entry_ids + populated ticker.

    The prod abort (ALP-934): q1 anomaly detection fires *per ticker* but the
    flag name was the block-level constant ``"volume_anomaly"``, so two tickers
    breaching the threshold in one ``q1.volume_anomaly`` block produced two flags
    with the *identical* ``(invocation_id, block_id, flag.name)`` triple — hence
    one shared ``entry_id`` PK, colliding inside the emission ``executemany``.
    Embedding the ticker (``volume_anomaly:{ticker}``) restores both PK
    uniqueness and the per-ticker attribution dropped to ``ticker=None``.
    """
    block_id = "q1.volume_anomaly"
    nvda = _summary(name="volume_anomaly:NVDA", magnitude=1.787, block_id=block_id)
    amd = _summary(name="volume_anomaly:AMD", magnitude=1.693, block_id=block_id)

    nvda_entry = anomaly_summary_to_activity_log_entry(
        nvda, invocation_id="inv-1", timestamp=_AS_OF
    )
    amd_entry = anomaly_summary_to_activity_log_entry(amd, invocation_id="inv-1", timestamp=_AS_OF)

    # Distinct PKs within one block — the collision the prod run hit.
    assert nvda_entry.entry_id != amd_entry.entry_id

    # The subject ticker is recovered into the detail (no longer ticker=None),
    # while the taxonomy still resolves off the stripped canonical prefix.
    for entry, ticker in ((nvda_entry, "NVDA"), (amd_entry, "AMD")):
        detail = entry.detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        assert detail.ticker == ticker
        assert detail.threshold_class == "anomaly_detection"
        assert detail.threshold_key == "volume_anomaly_sigma"


def test_mapper_per_contract_prediction_market_deltas_get_distinct_entry_ids() -> None:
    """Two per-contract prediction-market deltas in one block → distinct entry_ids.

    Prediction markets are not ticker-scoped, so the embedded ``:{contract_id}``
    suffix only disambiguates the PK — the detail's ``ticker`` stays ``None``
    (``prediction_market_delta`` is deliberately not a ticker-bearing prefix).
    """
    block_id = "qual.prediction_market_delta"
    first = _summary(name="prediction_market_delta:KX-A", block_id=block_id)
    second = _summary(name="prediction_market_delta:KX-B", block_id=block_id)

    first_entry = anomaly_summary_to_activity_log_entry(
        first, invocation_id="inv-1", timestamp=_AS_OF
    )
    second_entry = anomaly_summary_to_activity_log_entry(
        second, invocation_id="inv-1", timestamp=_AS_OF
    )

    assert first_entry.entry_id != second_entry.entry_id
    for entry in (first_entry, second_entry):
        detail = entry.detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        assert detail.ticker is None
        assert detail.threshold_class == "prediction_market"
        assert detail.threshold_key == "prediction_market_delta_pp_threshold"


def test_mapper_q3_pair_and_sweep_flags_get_distinct_entry_ids_no_ticker() -> None:
    """q3 pair / sweep flags embed their subject → distinct entry_ids, ticker ``None``.

    ALP-935: ``assemble_q3_pair_trade_blocks`` and
    ``assemble_q3_sector_wide_sweep_blocks`` each fan a constant ``block_id``
    block out per subject (an ordered leg pair / a ``(sector, direction)``), while
    the flag name was a bare constant — so two same-subject flags in one run
    collapsed to one shared ``(invocation_id, block_id, flag.name)`` PK. The
    per-subject ``:``-suffix restores PK uniqueness; neither a leg pair nor a
    ``(sector, direction)`` is a single symbol, so ``ticker`` stays ``None`` and
    the taxonomy still resolves off the stripped ``options_flow`` prefix.
    """
    pair_block = "q3.pair_trade_signature"
    sweep_block = "q3.sector_wide_sweep"
    summaries = (
        _summary(name="pair_trade_signature:NVDA:JPM", block_id=pair_block),
        _summary(name="pair_trade_signature:AMD:BAC", block_id=pair_block),
        _summary(name="sector_wide_sweep:tech_semis:call", block_id=sweep_block),
        _summary(name="sector_wide_sweep:tech_semis:put", block_id=sweep_block),
    )
    entries = [
        anomaly_summary_to_activity_log_entry(s, invocation_id="inv-1", timestamp=_AS_OF)
        for s in summaries
    ]

    # Every entry_id is distinct — the collision the per-subject suffix prevents.
    assert len({e.entry_id for e in entries}) == 4

    expected_taxonomy = {
        pair_block: ("options_flow", "pair_trade_signature"),
        sweep_block: ("options_flow", "sector_wide_sweep"),
    }
    for entry in entries:
        detail = entry.detail
        assert isinstance(detail, DistillationAnomalyFlagDetail)
        assert detail.ticker is None
        assert (detail.threshold_class, detail.threshold_key) == expected_taxonomy[detail.block_id]

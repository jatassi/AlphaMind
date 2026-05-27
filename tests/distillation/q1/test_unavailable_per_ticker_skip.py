"""Per-ticker UNAVAILABLE-skip tests for q1.trend_state and q1.technicals (ALP-630).

When a ticker's underlying baseline carries ``calibration_state=unavailable``
the per-ticker payload for the ``q1.trend_state`` and ``q1.technicals``
blocks must omit that ticker. The alternative — a row with zero-defaulted
EMA / ATR-dependent fields — silently overrides the block's ``unavailable``
header, which the researcher and synthesizer prompts trust as the gate
contract from ALP-540.

The fixture mirrors the field shape of :class:`DailyBarRow` and
:class:`TickerBaselineRow` directly so the tests stay in the pure-compute
half of the q1 stack (no SQLAlchemy, no Session).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._repository import (
    DailyBarRow,
    GapEventCounts,
    SectorClassificationRow,
    TickerBaselineRow,
)
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q1._loaders import GapFillHistoryEntry
from alphamind.distillation.q1.assemble import (
    BLOCK_ID_PRICE_MOVE_ANOMALY,
    _build_anomaly_block,
    _compute_gap_per_ticker,
    _compute_technicals_per_ticker,
    _compute_trend_state_per_ticker,
    _new_accumulator,
    _record_price_move_anomaly,
    _record_volume_anomaly,
    _trend_state_payload_for_ticker,
)

_BAR_DAYS: int = 240
"""Enough bars to cover the 200-period EMA and the 14-period ATR baseline."""


def _build_bars(ticker: str, *, base_price: float = 100.0) -> list[DailyBarRow]:
    """Return a deterministic ascending price series for ``ticker``.

    The ``period_start`` strings are monotonic sequential ordinals — compute
    never parses them, only attaches them to the row, so a unique-but-opaque
    label is enough.
    """
    bars: list[DailyBarRow] = []
    for d in range(1, _BAR_DAYS + 1):
        close = base_price + d * 0.5
        bars.append(
            DailyBarRow(
                ticker=ticker,
                period_start=f"day-{d:04d}",
                adj_open=close,
                adj_high=close + 1.0,
                adj_low=close - 1.0,
                adj_close=close,
                adj_volume=1_000_000,
            )
        )
    return bars


def _calibrated_atr_baseline(ticker: str) -> TickerBaselineRow:
    return TickerBaselineRow(
        ticker=ticker,
        baseline_kind="atr",
        as_of="2026-05-20T00:00:00Z",
        mean=2.0,
        stdev=0.2,
        n_observations=60,
        window_days=14,
        calibration_state=CalibrationState.CALIBRATED.value,
    )


def _unavailable_atr_baseline(ticker: str) -> TickerBaselineRow:
    return TickerBaselineRow(
        ticker=ticker,
        baseline_kind="atr",
        as_of="2026-05-20T00:00:00Z",
        mean=2.0,
        stdev=0.2,
        n_observations=0,
        window_days=14,
        calibration_state=CalibrationState.UNAVAILABLE.value,
    )


def _sectors_for(tickers: Sequence[str]) -> Mapping[str, SectorClassificationRow]:
    return {
        ticker: SectorClassificationRow(
            ticker=ticker,
            alphamind_sector="energy",
            sector_etf="XLE",
        )
        for ticker in tickers
    }


def _calibrated_gap_history(ticker: str) -> GapFillHistoryEntry:
    return GapFillHistoryEntry(
        ticker_counts=GapEventCounts(resolved=30, filled=15, pending=0),
        sector_counts=GapEventCounts(resolved=120, filled=60, pending=0),
    )


# ---------------------------------------------------------------------------
# (a) trend_state: per-ticker skip + non-None block_reason
# ---------------------------------------------------------------------------


def test_trend_state_payload_returns_none_when_atr_baseline_is_unavailable() -> None:
    """A ticker with a stored ``unavailable`` ATR baseline yields ``None``."""
    result = _trend_state_payload_for_ticker(
        ticker="CTRA",
        bars=_build_bars("CTRA"),
        atr_baseline=_unavailable_atr_baseline("CTRA"),
    )
    assert result is None


def test_trend_state_payload_returns_none_when_atr_baseline_is_missing() -> None:
    """A ticker whose ATR baseline row is absent yields ``None``.

    Per ALP-540 a missing baseline maps to :attr:`CalibrationState.UNAVAILABLE`,
    so the same skip path applies.
    """
    result = _trend_state_payload_for_ticker(
        ticker="CTRA",
        bars=_build_bars("CTRA"),
        atr_baseline=None,
    )
    assert result is None


def test_compute_trend_state_per_ticker_skips_unavailable_ticker_and_keeps_block_reason() -> None:
    """One ticker UNAVAILABLE → that ticker absent; block_reason is non-None."""
    bars_by_ticker = {
        "APA": _build_bars("APA", base_price=80.0),
        "CTRA": _build_bars("CTRA", base_price=90.0),
        "COP": _build_bars("COP", base_price=110.0),
    }
    baselines_atr: dict[str, TickerBaselineRow | None] = {
        "APA": _calibrated_atr_baseline("APA"),
        "CTRA": _unavailable_atr_baseline("CTRA"),
        "COP": _calibrated_atr_baseline("COP"),
    }
    payload, block_state, block_reason = _compute_trend_state_per_ticker(
        tickers=("APA", "COP", "CTRA"),
        bars_by_ticker=bars_by_ticker,
        baselines_atr=baselines_atr,
    )

    assert "CTRA" not in payload, (
        f"CTRA must be skipped under UNAVAILABLE baseline; got per_ticker keys {sorted(payload)}"
    )
    assert "APA" in payload
    assert "COP" in payload
    assert block_state is CalibrationState.UNAVAILABLE
    assert block_reason is not None, "block_reason must not be None when block_state is UNAVAILABLE"
    assert "CTRA" in block_reason


def test_compute_trend_state_per_ticker_no_zero_default_ema_in_payload() -> None:
    """Surviving rows never carry the zero-default EMA tuple from a skipped row.

    Pins AC3: the renderer never sees ``ema_20=0 ema_20_slope=0 ema_50_slope=0
    distance_from_ema_20_in_atr=0`` for a ticker whose block is UNAVAILABLE.
    """
    bars_by_ticker = {
        "APA": _build_bars("APA", base_price=80.0),
        "CTRA": _build_bars("CTRA", base_price=90.0),
    }
    baselines_atr: dict[str, TickerBaselineRow | None] = {
        "APA": _calibrated_atr_baseline("APA"),
        "CTRA": _unavailable_atr_baseline("CTRA"),
    }
    payload, _, _ = _compute_trend_state_per_ticker(
        tickers=("APA", "CTRA"),
        bars_by_ticker=bars_by_ticker,
        baselines_atr=baselines_atr,
    )
    for ticker, row in payload.items():
        zeros = (
            row["ema_20"] == 0.0
            and row["ema_20_slope"] == 0.0
            and row["ema_50_slope"] == 0.0
            and row["distance_from_ema_20_in_atr"] == 0.0
        )
        assert not zeros, f"Zero-default EMA tuple leaked for {ticker}: {row}"


# ---------------------------------------------------------------------------
# (b) technicals: per-ticker skip
# ---------------------------------------------------------------------------


def test_compute_technicals_per_ticker_skips_unavailable_ticker() -> None:
    """A ticker with an UNAVAILABLE ATR baseline is absent from technicals."""
    bars_by_ticker = {
        "APA": _build_bars("APA", base_price=80.0),
        "CTRA": _build_bars("CTRA", base_price=90.0),
        "COP": _build_bars("COP", base_price=110.0),
    }
    baselines_atr: dict[str, TickerBaselineRow | None] = {
        "APA": _calibrated_atr_baseline("APA"),
        "CTRA": _unavailable_atr_baseline("CTRA"),
        "COP": _calibrated_atr_baseline("COP"),
    }
    out = _compute_technicals_per_ticker(bars_by_ticker, baselines_atr)

    assert "CTRA" not in out, (
        f"CTRA must be skipped under UNAVAILABLE baseline; got per_ticker keys {sorted(out)}"
    )
    assert "APA" in out
    assert "COP" in out


def test_compute_technicals_per_ticker_skips_when_baseline_missing() -> None:
    """A ticker with no baseline row is absent (missing == UNAVAILABLE per ALP-540)."""
    bars_by_ticker = {
        "APA": _build_bars("APA", base_price=80.0),
        "CTRA": _build_bars("CTRA", base_price=90.0),
    }
    baselines_atr: dict[str, TickerBaselineRow | None] = {
        "APA": _calibrated_atr_baseline("APA"),
        "CTRA": None,
    }
    out = _compute_technicals_per_ticker(bars_by_ticker, baselines_atr)

    assert "CTRA" not in out
    assert "APA" in out


# ---------------------------------------------------------------------------
# (c) parity — gap / trend_state / technicals all skip the UNAVAILABLE ticker
# ---------------------------------------------------------------------------


def test_gap_trend_state_technicals_all_skip_unavailable_ticker() -> None:
    """Observable parity under the production UNAVAILABLE fixture (AC4 c).

    Under the production fixture shape — CTRA's ATR baseline is
    ``unavailable`` AND CTRA has no resolved/pending gap-fill events — all
    three modules omit CTRA from ``per_ticker``. The three skip mechanisms
    are deliberately distinct:

    * ``q1.trend_state`` and ``q1.technicals`` (ALP-630): the new
      ``_baseline_calibration_state(...) is UNAVAILABLE`` gate at the top
      of each per-ticker compute function.
    * ``q1.gap`` (pre-existing): the ``history_entry is None`` guard at
      :func:`_compute_gap_per_ticker`. In production an
      ``unavailable``-baseline ticker reliably co-occurs with an empty
      ``gap_fill_event`` history (both shaped by the same upstream
      collector outage), so the observable parity holds — but ``gap``
      has no baseline-state-aware gate of its own. If a future change
      gives an UNAVAILABLE ticker a non-empty gap-fill history, this
      test will surface the divergence and ``gap`` will need its own
      ALP-630-style fix.
    """
    tickers = ("APA", "COP", "CTRA")
    bars_by_ticker = {
        "APA": _build_bars("APA", base_price=80.0),
        "COP": _build_bars("COP", base_price=110.0),
        "CTRA": _build_bars("CTRA", base_price=90.0),
    }
    baselines_atr: dict[str, TickerBaselineRow | None] = {
        "APA": _calibrated_atr_baseline("APA"),
        "COP": _calibrated_atr_baseline("COP"),
        "CTRA": _unavailable_atr_baseline("CTRA"),
    }
    sector_per_ticker = _sectors_for(tickers)
    # CTRA's empty gap-fill history mirrors production: the same upstream
    # collector outage that left CTRA's ATR baseline UNAVAILABLE also left
    # gap-fill events un-populated, so ``_compute_gap_per_ticker`` drops the
    # ticker via the existing ``history_entry is None`` guard.
    gap_history = {
        "APA": _calibrated_gap_history("APA"),
        "COP": _calibrated_gap_history("COP"),
    }

    gap_payload, _, _ = _compute_gap_per_ticker(
        bars_by_ticker=bars_by_ticker,
        sector_per_ticker=sector_per_ticker,
        gap_fill_history=gap_history,
        gap_fill_min_events=3,
    )
    trend_payload, _, _ = _compute_trend_state_per_ticker(
        tickers=tickers,
        bars_by_ticker=bars_by_ticker,
        baselines_atr=baselines_atr,
    )
    technicals_payload = _compute_technicals_per_ticker(bars_by_ticker, baselines_atr)

    for name, payload in (
        ("gap", gap_payload),
        ("trend_state", trend_payload),
        ("technicals", technicals_payload),
    ):
        assert "CTRA" not in payload, (
            f"{name} per_ticker leaked CTRA under UNAVAILABLE fixture; got keys {sorted(payload)}"
        )
        assert "APA" in payload, f"{name} per_ticker missing calibrated APA"
        assert "COP" in payload, f"{name} per_ticker missing calibrated COP"


# ---------------------------------------------------------------------------
# (d) price_move_anomaly + volume_anomaly: per-ticker suppress under UNAVAILABLE
# ---------------------------------------------------------------------------
#
# ALP-704: the q1.price_move_anomaly / q1.volume_anomaly emission paths
# previously fired a sector-level flag even when the underlying ticker's
# baseline was ``unavailable``. ``AnomalyFlag.name`` is the rollup-block
# constant ("price_move_anomaly" / "volume_anomaly") — no ticker bake-in —
# so the rendered flag landed downstream with no attribution. The fix
# mirrors the existing ALP-630 per-ticker skip pattern in
# :func:`_compute_technicals_per_ticker` /
# :func:`_trend_state_payload_for_ticker`.


def _bars_with_terminal_anomaly(ticker: str, *, base_price: float = 100.0) -> list[DailyBarRow]:
    """``_build_bars`` plus a +5.0 close jump on the terminal bar.

    The base series gives ATR ≈ 2.0 (±1.0 high-low band, see ``_build_bars``);
    the terminal jump shoves ``|price_move| / atr`` well above the 1.5 ATR
    multiple threshold the tests pass, so the producer fires under any
    non-suppressed code path.
    """
    bars = _build_bars(ticker, base_price=base_price)
    final = bars[-1]
    spiked_close = float(final.adj_close) + 5.0
    bars[-1] = DailyBarRow(
        ticker=final.ticker,
        period_start=final.period_start,
        adj_open=final.adj_open,
        adj_high=max(float(final.adj_high), spiked_close),
        adj_low=final.adj_low,
        adj_close=spiked_close,
        adj_volume=final.adj_volume,
    )
    return bars


def test_record_price_move_anomaly_suppresses_flag_when_baseline_is_unavailable() -> None:
    """A UNAVAILABLE baseline yields no flag, even when the move clears threshold.

    Pins ALP-704 AC1 / AC2: q1.price_move_anomaly emits no flag for a
    ticker whose ATR baseline is ``unavailable``. The terminal-anomaly bars
    guarantee the multiple is well above the threshold so the test
    distinguishes "skipped due to UNAVAILABLE" from "skipped due to no
    detection" (the control test below proves the same bars fire under
    CALIBRATED).
    """
    bars = _bars_with_terminal_anomaly("CTRA")
    acc = _record_price_move_anomaly(
        _new_accumulator(),
        ticker="CTRA",
        bars=bars,
        baseline=_unavailable_atr_baseline("CTRA"),
        atr_multiple_threshold=1.5,
    )

    assert acc.flags == [], (
        f"UNAVAILABLE baseline must suppress flag emission; got flags={acc.flags}"
    )
    assert "CTRA" not in acc.per_ticker, (
        f"UNAVAILABLE baseline must not record per_ticker entry; got keys {sorted(acc.per_ticker)}"
    )


def test_record_price_move_anomaly_suppresses_flag_when_baseline_is_missing() -> None:
    """A missing baseline (None) is treated as UNAVAILABLE and yields no flag.

    Per ALP-540, a missing baseline maps to :attr:`CalibrationState.UNAVAILABLE`
    via :func:`_baseline_calibration_state`, so the same suppression path
    applies.
    """
    bars = _bars_with_terminal_anomaly("CTRA")
    acc = _record_price_move_anomaly(
        _new_accumulator(),
        ticker="CTRA",
        bars=bars,
        baseline=None,
        atr_multiple_threshold=1.5,
    )

    assert acc.flags == []
    assert "CTRA" not in acc.per_ticker


def test_record_price_move_anomaly_still_fires_on_calibrated_baseline() -> None:
    """Control: a CALIBRATED baseline with the same bars DOES fire a flag.

    Without this companion, the suppression test could pass for the wrong
    reason (e.g., the synthetic bars happen not to fire). Pinning the
    fire-on-calibrated path here proves the baseline state is the
    differentiator.
    """
    bars = _bars_with_terminal_anomaly("APA")
    acc = _record_price_move_anomaly(
        _new_accumulator(),
        ticker="APA",
        bars=bars,
        baseline=_calibrated_atr_baseline("APA"),
        atr_multiple_threshold=1.5,
    )

    assert len(acc.flags) == 1, f"expected one flag; got {acc.flags}"
    assert acc.flags[0].name == "price_move_anomaly"
    assert "APA" in acc.per_ticker


def test_price_move_anomaly_block_keeps_calibrated_state_with_mixed_roster() -> None:
    """A mixed CALIBRATED + UNAVAILABLE roster yields a CALIBRATED q1.price_move_anomaly block.

    Pins the deliberate divergence from the ALP-630 technicals pattern: the
    technicals block header reflects the FULL ticker roster (so it goes
    UNAVAILABLE the moment any ticker in the sector has an unavailable
    baseline), but anomaly blocks are sparse-by-design — only firing
    tickers contribute, so the block header reflects only the survivors.
    Pre-fix, an UNAVAILABLE ticker's flag would have poisoned the block to
    UNAVAILABLE and the severity cap would have downgraded the legitimate
    APA flag along with it. Post-fix, APA's flag retains its full severity
    because the UNAVAILABLE ticker is suppressed before the block is built.
    """
    price_acc = _new_accumulator()
    price_acc = _record_price_move_anomaly(
        price_acc,
        ticker="APA",
        bars=_bars_with_terminal_anomaly("APA", base_price=80.0),
        baseline=_calibrated_atr_baseline("APA"),
        atr_multiple_threshold=1.5,
    )
    price_acc = _record_price_move_anomaly(
        price_acc,
        ticker="CTRA",
        bars=_bars_with_terminal_anomaly("CTRA", base_price=90.0),
        baseline=_unavailable_atr_baseline("CTRA"),
        atr_multiple_threshold=1.5,
    )

    block = _build_anomaly_block(
        block_id=BLOCK_ID_PRICE_MOVE_ANOMALY,
        audience=frozenset({OutputAudience.SECTOR_ENERGY}),
        accumulator=price_acc,
        freshness_ts=datetime(2026, 5, 20, tzinfo=UTC),
    )

    assert block is not None
    assert block.calibration_state is CalibrationState.CALIBRATED
    assert block.bootstrap_reason is None
    assert "APA" in block.payload["per_ticker"]
    assert "CTRA" not in block.payload["per_ticker"]
    assert len(block.anomaly_flags) == 1
    assert block.anomaly_flags[0].severity == "investigate_now"


def test_record_volume_anomaly_suppresses_flag_when_baseline_is_unavailable() -> None:
    """Volume parity with price_move: UNAVAILABLE baseline emits no flag.

    The volume rollup-flag is the same shape as price_move's
    (``AnomalyFlag.name="volume_anomaly"`` carries no ticker), so an
    UNAVAILABLE-baseline ticker firing it would land downstream with the
    same attribution-less defect ALP-704 fixes for price_move. The today-
    volume is far above the baseline so the producer fires under any non-
    suppressed code path.
    """
    baseline = _unavailable_atr_baseline("CTRA")
    acc = _record_volume_anomaly(
        _new_accumulator(),
        ticker="CTRA",
        today_volume=1_000_000_000.0,
        baseline=baseline,
        baseline_state=CalibrationState.UNAVAILABLE,
        sigma_threshold=2.5,
    )

    assert acc.flags == []
    assert "CTRA" not in acc.per_ticker

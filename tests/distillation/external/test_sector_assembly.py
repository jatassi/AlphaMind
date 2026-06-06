"""Tests for sector-scoped output assembly — story 02-distillation/11a.

Verifies that :func:`assemble_sector_output` produces a per-sector
:class:`SectorOutput` with the documented fields, the documented
filtering rules (sector audience + universal broadcast, per-ticker
restriction to the sector roster), and a deterministic document
framing so the invocation archive diffs cleanly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
    format_block,
    format_blocks_for_audience,
)
from alphamind.distillation.sector_assembly import (
    DOMAIN_RESEARCHER_BY_AUDIENCE,
    SECTOR_LABEL_BY_AUDIENCE,
    SectorOutput,
    assemble_sector_output,
    load_sector_roster,
)
from alphamind.persistence.models import AssetUniverse, SectorClassification

# ---------------------------------------------------------------------------
# Roster fixtures — pre-computed in the test (mirrors the orchestrator's
# main-thread roster lookup before fanning out to per-audience workers).
# ---------------------------------------------------------------------------

TECH_SEMIS_ROSTER: tuple[str, ...] = ("AAPL", "NVDA")
FINANCIALS_ROSTER: tuple[str, ...] = ("BAC", "JPM")
ENERGY_ROSTER: tuple[str, ...] = ("CVX", "XOM")


def _add_ticker(session: Session, ticker: str) -> None:
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
    session.flush()


def _add_sector(
    session: Session,
    *,
    ticker: str,
    alphamind_sector: str,
    domain_researcher: str,
    sector_etf: str = "XLK",
) -> None:
    session.add(
        SectorClassification(
            ticker=ticker,
            asset_id=f"asset-{ticker.lower()}",
            alphamind_sector=alphamind_sector,
            domain_researcher=domain_researcher,
            sector_etf=sector_etf,
            classification_source="test",
            last_updated="2026-04-26T00:00:00Z",
        )
    )


@pytest.fixture()
def populated_session(session: Session) -> Session:
    """Seed a small three-sector ticker roster covering the four ``alphamind_sector`` values."""
    for ticker, alphamind_sector, domain_researcher, etf in (
        ("NVDA", "semis", "tech_semis", "SMH"),
        ("AAPL", "tech", "tech_semis", "XLK"),
        ("JPM", "financials", "financials", "XLF"),
        ("BAC", "financials", "financials", "XLF"),
        ("XOM", "energy", "energy", "XLE"),
        ("CVX", "energy", "energy", "XLE"),
    ):
        _add_ticker(session, ticker)
        _add_sector(
            session,
            ticker=ticker,
            alphamind_sector=alphamind_sector,
            domain_researcher=domain_researcher,
            sector_etf=etf,
        )
    session.flush()
    return session


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------


_BASE_TS = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)


def _make_block(
    *,
    block_id: str,
    audience: frozenset[OutputAudience],
    payload: dict[str, object] | None = None,
    anomaly_flags: tuple[AnomalyFlag, ...] = (),
    freshness_ts: datetime = _BASE_TS,
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
    bootstrap_reason: str | None = None,
    regime_context: str | None = None,
) -> OutputBlock:
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=freshness_ts,
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
        payload=payload if payload is not None else {"note": "stub"},
        anomaly_flags=anomaly_flags,
        regime_context=regime_context,
    )


def _per_ticker_block(
    *,
    block_id: str,
    audience: frozenset[OutputAudience],
    per_ticker: dict[str, dict[str, object]],
    freshness_ts: datetime = _BASE_TS,
) -> OutputBlock:
    return _make_block(
        block_id=block_id,
        audience=audience,
        payload={"per_ticker": per_ticker},
        freshness_ts=freshness_ts,
    )


# ---------------------------------------------------------------------------
# da23fc74-shaped fixture (ALP-839)
# ---------------------------------------------------------------------------
#
# Mirrors the production artifact inv-20260603T170000Z-da23fc74's tech_semis
# SECTOR INDICATORS composition: a 40-ticker roster, a handful of small
# CALIBRATED aggregate blocks, and a dozen heavy non-calibrated per-ticker
# tables (95% of the rendered volume) whose baselines aren't built yet.

_DA23_ROSTER: tuple[str, ...] = tuple(sorted(f"TK{i:02d}" for i in range(40)))
_TECH_AUDIENCE = frozenset({OutputAudience.SECTOR_TECH_SEMIS})


def _heavy_per_ticker() -> dict[str, dict[str, object]]:
    """A 40-ticker x 10-metric table — the shape that dominated the bundle."""
    return {
        ticker: {
            "rsi_1d": 55.0 + idx,
            "rsi_5d": 50.0 + idx,
            "macd_state": "bullish",
            "atr": 4.0 + idx / 10,
            "ema_20": 100.0 + idx,
            "ema_50": 98.0 + idx,
            "volume_z": 1.0 + idx / 20,
            "gap_pct": 0.5,
            "rel_perf_5d": -0.2,
            "vol_profile": "balanced",
        }
        for idx, ticker in enumerate(_DA23_ROSTER)
    }


def _da23fc74_tech_semis_blocks() -> tuple[tuple[str, ...], list[OutputBlock]]:
    """Return ``(roster, blocks)`` shaped like the da23fc74 tech_semis slice."""
    calibrated_specs: tuple[tuple[str, dict[str, object]], ...] = (
        ("q9.regime_alignment", {"regime": "normal", "confidence": 0.82}),
        ("q7.sector_correlation", {"avg_corr": 0.45, "dispersion": 0.12}),
        ("q6.vix_level", {"vix": 16.2, "term_structure": "contango"}),
        ("q12.corporate_actions", {"events": 0}),
        ("q6.funding_stress", {"composite_pct": 35.0}),
        ("q7.lead_lag", {"semis_to_tech": "aligned"}),
        ("q1.session_summary", {"advancers": 21, "decliners": 19}),
        ("q6.macro_calendar", {"high_impact_events": 2}),
    )
    calibrated_aggregates: list[OutputBlock] = [
        _make_block(block_id=bid, audience=_TECH_AUDIENCE, payload=payload)
        for bid, payload in calibrated_specs
    ]
    # One CALIBRATED per-ticker block proves calibrated tables are preserved.
    calibrated_table = _per_ticker_block(
        block_id="q1.price_summary",
        audience=_TECH_AUDIENCE,
        per_ticker={t: {"last": 100.0 + i, "change_pct": 0.5} for i, t in enumerate(_DA23_ROSTER)},
    )

    def _heavy(block_id: str, state: CalibrationState, reason: str) -> OutputBlock:
        return _make_block(
            block_id=block_id,
            audience=_TECH_AUDIENCE,
            payload={"per_ticker": _heavy_per_ticker()},
            calibration_state=state,
            bootstrap_reason=reason,
        )

    def _aggregate_unavailable(block_id: str, reason: str, status: str) -> OutputBlock:
        return _make_block(
            block_id=block_id,
            audience=_TECH_AUDIENCE,
            payload={"status": status},
            calibration_state=CalibrationState.UNAVAILABLE,
            bootstrap_reason=reason,
        )

    acc = CalibrationState.ACCUMULATING
    unavail = CalibrationState.UNAVAILABLE
    non_calibrated: list[OutputBlock] = [
        _heavy("q1.technicals", acc, "atr_baseline: 3 < 14"),
        _heavy("q1.trend_state", acc, "trend_baseline: 3 < 20"),
        _heavy("q1.relative_performance", acc, "relperf_baseline: 3 < 20"),
        _heavy("q1.volume_profile", acc, "volprofile_baseline: 3 < 20"),
        _heavy("q1.momentum", acc, "momentum_baseline: 3 < 20"),
        _heavy("q1.volatility", acc, "vol_baseline: 3 < 14"),
        _heavy("q1.support_resistance", acc, "sr_baseline: 3 < 60"),
        _heavy("q1.range_compression", acc, "range_baseline: 3 < 20"),
        _heavy("q3.flow_classification", unavail, "options_snapshots: 0 < 1 (0 observations)"),
        _aggregate_unavailable("q1.gap", "gap_events: 0 < 30 (0 observations)", "no gap events"),
        _aggregate_unavailable(
            "q3.flow_imbalance", "options_snapshots: 0 < 1 (0 observations)", "no options snapshots"
        ),
        _make_block(
            block_id="q6.sector_breadth",
            audience=_TECH_AUDIENCE,
            payload={"advancers": 18, "decliners": 22},
            calibration_state=acc,
            bootstrap_reason="breadth_baseline: 3 < 20",
        ),
    ]
    blocks = [*calibrated_aggregates, calibrated_table, *non_calibrated]
    return _DA23_ROSTER, blocks


# ---------------------------------------------------------------------------
# Audience → researcher / label mapping
# ---------------------------------------------------------------------------


class TestAudienceMapping:
    def test_three_sector_audiences_map_to_domain_researchers(self) -> None:
        assert DOMAIN_RESEARCHER_BY_AUDIENCE == {
            OutputAudience.SECTOR_TECH_SEMIS: "tech_semis",
            OutputAudience.SECTOR_FINANCIALS: "financials",
            OutputAudience.SECTOR_ENERGY: "energy",
        }

    def test_each_sector_audience_has_a_human_readable_label(self) -> None:
        assert set(SECTOR_LABEL_BY_AUDIENCE.keys()) == {
            OutputAudience.SECTOR_TECH_SEMIS,
            OutputAudience.SECTOR_FINANCIALS,
            OutputAudience.SECTOR_ENERGY,
        }
        for label in SECTOR_LABEL_BY_AUDIENCE.values():
            assert label.strip() == label
            assert label != ""


# ---------------------------------------------------------------------------
# Roster from sector_classification
# ---------------------------------------------------------------------------


class TestRosterFromSectorClassification:
    def test_tech_semis_roster_pulls_from_domain_researcher(
        self, populated_session: Session
    ) -> None:
        roster = load_sector_roster(populated_session, "tech_semis")
        assert roster == TECH_SEMIS_ROSTER

    def test_financials_roster_pulls_from_domain_researcher(
        self, populated_session: Session
    ) -> None:
        roster = load_sector_roster(populated_session, "financials")
        assert roster == FINANCIALS_ROSTER

    def test_energy_roster_pulls_from_domain_researcher(self, populated_session: Session) -> None:
        roster = load_sector_roster(populated_session, "energy")
        assert roster == ENERGY_ROSTER

    def test_assembly_propagates_roster_into_output_tickers(self) -> None:
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert output.tickers == TECH_SEMIS_ROSTER

    def test_non_sector_audience_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="sector audience"):
            assemble_sector_output(
                audience=OutputAudience.UNIVERSAL_BROADCAST,
                blocks=(),
                roster=TECH_SEMIS_ROSTER,
                invocation_id="inv-001",
            )


# ---------------------------------------------------------------------------
# Audience filtering
# ---------------------------------------------------------------------------


class TestAudienceFiltering:
    def test_block_targeting_only_a_different_sector_is_excluded(self) -> None:
        fin_only = _make_block(
            block_id="q1.financials_local",
            audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(fin_only,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert "q1.financials_local" not in output.text
        assert "q1.financials_local" not in output.block_ids

    def test_universal_broadcast_block_appears_in_every_sector_output(self) -> None:
        universal = _make_block(
            block_id="regime.label",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            payload={"regime": "normal"},
        )
        for audience, roster in (
            (OutputAudience.SECTOR_TECH_SEMIS, TECH_SEMIS_ROSTER),
            (OutputAudience.SECTOR_FINANCIALS, FINANCIALS_ROSTER),
            (OutputAudience.SECTOR_ENERGY, ENERGY_ROSTER),
        ):
            output = assemble_sector_output(
                audience=audience,
                blocks=(universal,),
                roster=roster,
                invocation_id="inv-001",
            )
            assert "regime.label" in output.block_ids
            assert "regime.label" in output.text

    def test_multi_sector_block_appears_in_every_sector_it_targets(self) -> None:
        pair = _make_block(
            block_id="q3.pair_signature",
            audience=frozenset(
                {OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.SECTOR_FINANCIALS}
            ),
        )
        tech_output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(pair,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        fin_output = assemble_sector_output(
            audience=OutputAudience.SECTOR_FINANCIALS,
            blocks=(pair,),
            roster=FINANCIALS_ROSTER,
            invocation_id="inv-001",
        )
        energy_output = assemble_sector_output(
            audience=OutputAudience.SECTOR_ENERGY,
            blocks=(pair,),
            roster=ENERGY_ROSTER,
            invocation_id="inv-001",
        )
        assert "q3.pair_signature" in tech_output.block_ids
        assert "q3.pair_signature" in fin_output.block_ids
        assert "q3.pair_signature" not in energy_output.block_ids


# ---------------------------------------------------------------------------
# Per-ticker filtering
# ---------------------------------------------------------------------------


class TestPerTickerFiltering:
    def test_per_ticker_payload_keeps_only_in_roster_tickers(self) -> None:
        block = _per_ticker_block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            per_ticker={
                "AAPL": {"rsi": 55.0},
                "NVDA": {"rsi": 60.0},
                "JPM": {"rsi": 50.0},  # off-sector — must be dropped.
            },
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert "AAPL" in output.text
        assert "NVDA" in output.text
        assert "JPM" not in output.text

    def test_per_ticker_block_with_no_in_roster_tickers_is_dropped(self) -> None:
        block = _per_ticker_block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            per_ticker={"JPM": {"rsi": 50.0}},
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert "q1.technicals" not in output.block_ids

    def test_blocks_without_per_ticker_payload_are_kept_unchanged(self) -> None:
        block = _make_block(
            block_id="q9.regime",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            payload={"regime_context": "normal"},
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert "q9.regime" in output.block_ids


# ---------------------------------------------------------------------------
# Non-calibrated compression (ALP-839)
# ---------------------------------------------------------------------------


class TestNonCalibratedCompression:
    def test_non_calibrated_sector_block_renders_compactly(self) -> None:
        """A non-calibrated SECTOR INDICATORS block collapses to its summary line."""
        block = _make_block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            payload={"per_ticker": {"AAPL": {"rsi": 55.0}, "NVDA": {"rsi": 60.0}}},
            calibration_state=CalibrationState.ACCUMULATING,
            bootstrap_reason="atr_baseline: 3 < 14",
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        # The block is still represented (id + state + reason on the header).
        assert "q1.technicals" in output.block_ids
        assert "accumulating — reason: atr_baseline: 3 < 14" in output.text
        # But its full per-ticker table no longer appears in the sector slice.
        assert "per_ticker:" not in output.text
        assert "NVDA rsi=60" not in output.text
        assert "(non-calibrated — per-ticker detail omitted; 2 tickers)" in output.text

    def test_non_calibrated_universal_block_is_not_compressed(self) -> None:
        """UNIVERSAL CONTEXT is untouched — a non-calibrated universal block renders in full."""
        universal = _make_block(
            block_id="q6.breadth",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            payload={"advancers": 120, "decliners": 380},
            calibration_state=CalibrationState.ACCUMULATING,
            bootstrap_reason="breadth_baseline: 3 < 20",
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(universal,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        # Full payload survives in the universal-context section.
        assert "advancers: 120" in output.text
        assert "decliners: 380" in output.text
        assert "(non-calibrated" not in output.text

    def test_da23fc74_shaped_bundle_shrinks_to_calibrated_plus_summaries(self) -> None:
        """Replay a da23fc74-shaped tech_semis block set through the assembler (ALP-839 AC).

        The fixture mirrors the production artifact's composition: a 40-ticker
        roster, a handful of small CALIBRATED aggregate blocks, and a dozen
        heavy non-calibrated per-ticker tables (q1.technicals / q1.trend_state /
        q1.gap / q3.flow_classification / ...) that drove the 95%-non-calibrated
        SECTOR INDICATORS volume. After compression the sector slice equals
        exactly the calibrated-in-full volume plus the non-calibrated summary
        lines, every non-calibrated block still represented.
        """
        roster, blocks = _da23fc74_tech_semis_blocks()
        calibrated = [b for b in blocks if b.calibration_state is CalibrationState.CALIBRATED]
        non_calibrated = [
            b for b in blocks if b.calibration_state is not CalibrationState.CALIBRATED
        ]
        assert len(calibrated) and len(non_calibrated)  # fixture sanity

        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=tuple(blocks),
            roster=roster,
            invocation_id="inv-da23fc74",
        )
        compressed_slice = output.text.split("=== SECTOR INDICATORS ===\n", 1)[1]

        # Baseline: the same sector slice rendered with every block in full.
        baseline_full = format_blocks_for_audience(blocks, OutputAudience.SECTOR_TECH_SEMIS)
        assert len(compressed_slice) < len(baseline_full)  # the bundle shrinks

        # The compressed slice is *exactly* calibrated-in-full + non-calibrated
        # summaries — the reduction is a deterministic consequence of state.
        calibrated_full = "".join(format_block(b) for b in calibrated)
        non_calibrated_summaries = "".join(
            format_block(b, compress_non_calibrated=True) for b in non_calibrated
        )
        assert len(compressed_slice) == len(calibrated_full) + len(non_calibrated_summaries)

        # Every CALIBRATED block survives byte-for-byte (full table preserved).
        for block in calibrated:
            assert format_block(block) in compressed_slice

        # Every non-calibrated block survives as exactly one summary line.
        assert compressed_slice.count("(non-calibrated") == len(non_calibrated)
        for block in non_calibrated:
            assert block.block_id in output.block_ids
            assert f"### {block.block_id} " in compressed_slice
        # None of the heavy per-ticker rows leak through (the size driver is gone).
        assert "macd_state=bullish" not in compressed_slice

    def test_anomaly_summary_still_lists_flags_from_compressed_blocks(self) -> None:
        """Compressing a block's body does not hide its flags — they ride the top summary."""
        block = _make_block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            payload={"per_ticker": {"NVDA": {"rsi": 60.0}}},
            calibration_state=CalibrationState.ACCUMULATING,
            bootstrap_reason="atr_baseline: 3 < 14",
            anomaly_flags=(
                AnomalyFlag(name="rsi_extreme", magnitude=2.5, severity="investigate_if_persists"),
            ),
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        # The flag survives in the top-of-document anomaly summary...
        assert "=== ANOMALY FLAGS (1) ===" in output.text
        assert "rsi_extreme" in output.text
        # ...even though the block body is compressed (no per-block trailer).
        assert "(non-calibrated — per-ticker detail omitted; 1 ticker)" in output.text


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


class TestFreshness:
    def test_freshness_min_is_oldest_block_freshness(self) -> None:
        oldest_ts = datetime(2026, 4, 27, 9, 0, tzinfo=UTC)
        newest_ts = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
        old_block = _make_block(
            block_id="q1.old",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            freshness_ts=oldest_ts,
        )
        new_block = _make_block(
            block_id="q1.new",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            freshness_ts=newest_ts,
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(new_block, old_block),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert output.freshness_min == oldest_ts


# ---------------------------------------------------------------------------
# Document framing & determinism
# ---------------------------------------------------------------------------


class TestDocumentFraming:
    def test_header_carries_sector_label_invocation_tickers_freshness(self) -> None:
        block = _per_ticker_block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            per_ticker={"AAPL": {"rsi": 55.0}, "NVDA": {"rsi": 60.0}},
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-007",
        )
        sector_label = SECTOR_LABEL_BY_AUDIENCE[OutputAudience.SECTOR_TECH_SEMIS]
        assert f"DISTILLATION OUTPUT — {sector_label}" in output.text
        assert "Invocation: inv-007" in output.text
        assert "Tickers: AAPL, NVDA" in output.text
        assert f"Effective freshness: {_BASE_TS.isoformat()}" in output.text

    def test_section_headers_are_present_in_document(self) -> None:
        block = _make_block(
            block_id="q1.tech",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        )
        universal = _make_block(
            block_id="regime.label",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block, universal),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert "=== ANOMALY FLAGS" in output.text
        assert "=== UNIVERSAL CONTEXT ===" in output.text
        assert "=== SECTOR INDICATORS ===" in output.text

    def test_byte_identical_on_repeated_calls(self) -> None:
        flag = AnomalyFlag(name="volume_spike", magnitude=2.5, severity="investigate_now")
        block_a = _per_ticker_block(
            block_id="q1.technicals",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            per_ticker={"AAPL": {"rsi": 55.0}, "NVDA": {"rsi": 60.0}},
        )
        block_b = _make_block(
            block_id="regime.label",
            audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
            anomaly_flags=(flag,),
        )
        first = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block_a, block_b),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        second = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block_a, block_b),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert first.text == second.text

    def test_anomaly_summary_integrates_via_aggregation_primitives(self) -> None:
        flag = AnomalyFlag(name="rvol_breakout", magnitude=2.5, severity="investigate_now")
        block = _make_block(
            block_id="q1.tech",
            audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
            anomaly_flags=(flag,),
        )
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(block,),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert "=== ANOMALY FLAGS (1) ===" in output.text
        assert "rvol_breakout" in output.text


# ---------------------------------------------------------------------------
# Empty case
# ---------------------------------------------------------------------------


class TestEmptyCase:
    def test_empty_block_set_still_produces_well_formed_document(self) -> None:
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert isinstance(output, SectorOutput)
        sector_label = SECTOR_LABEL_BY_AUDIENCE[OutputAudience.SECTOR_TECH_SEMIS]
        assert f"DISTILLATION OUTPUT — {sector_label}" in output.text
        assert "Tickers: AAPL, NVDA" in output.text
        assert "=== UNIVERSAL CONTEXT ===" in output.text
        assert "=== SECTOR INDICATORS ===" in output.text
        assert output.block_ids == ()

    def test_empty_block_set_freshness_min_falls_back_to_epoch_zero(self) -> None:
        """With no blocks, ``freshness_min`` is the conservative epoch-zero sentinel.

        Domain researchers downstream need a non-null timestamp to compare
        against; epoch-zero forces them to treat the document as stale.
        """
        output = assemble_sector_output(
            audience=OutputAudience.SECTOR_TECH_SEMIS,
            blocks=(),
            roster=TECH_SEMIS_ROSTER,
            invocation_id="inv-001",
        )
        assert output.freshness_min == datetime.fromtimestamp(0, tz=UTC)

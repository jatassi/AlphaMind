"""Tests for sector-scoped output assembly — story 02-distillation/11a.

Verifies that :func:`assemble_sector_output` produces a per-sector
:class:`SectorOutput` with the documented fields, the documented
filtering rules (sector audience + universal broadcast, per-ticker
restriction to the sector roster), and a deterministic document
framing so the invocation archive diffs cleanly.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.sector_assembly import (
    DOMAIN_RESEARCHER_BY_AUDIENCE,
    SECTOR_LABEL_BY_AUDIENCE,
    SectorOutput,
    assemble_sector_output,
    load_sector_roster,
)
from alphamind.persistence.models import AssetUniverse, Base, SectorClassification
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Roster fixtures — pre-computed in the test (mirrors the orchestrator's
# main-thread roster lookup before fanning out to per-audience workers).
# ---------------------------------------------------------------------------

TECH_SEMIS_ROSTER: tuple[str, ...] = ("AAPL", "NVDA")
FINANCIALS_ROSTER: tuple[str, ...] = ("BAC", "JPM")
ENERGY_ROSTER: tuple[str, ...] = ("CVX", "XOM")

# ---------------------------------------------------------------------------
# In-memory SQLite scaffolding
# ---------------------------------------------------------------------------


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

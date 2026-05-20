"""Tests for the Phase 2 OPEN write path's position builder (ALP-594).

Story 01c extends ``_build_pending_position`` so a :class:`StrategyInstrument`
produces a PENDING strategy :class:`PositionRecord` carrying a
:class:`StrategyPositionDetails` skeleton — replacing the prior
``NotImplementedError``. Payoff metrics are zeroed here; story 02 recomputes
them from the filled legs.
"""

from __future__ import annotations

from alphamind._kernel.money import price
from alphamind.commands.command_models import (
    StrategyInstrument,
    StrategyLeg as WireStrategyLeg,
)
from alphamind.execution.constants import LISTED_OPTION_CONTRACT_MULTIPLIER
from alphamind.execution.write_paths.phase2.open import (
    _build_pending_position,
    _direction_from_instrument,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    PositionStatus,
    StrategyPositionDetails,
)


def _vertical_spread() -> StrategyInstrument:
    """A two-leg vertical call spread: long the 800 strike, short the 810."""
    return StrategyInstrument(
        asset_type="strategy",
        strategy_type="vertical_spread",
        underlying="NVDA",
        legs=(
            WireStrategyLeg(
                strike=price(800.0),
                expiration="2026-06-19",
                contract_type="call",
                direction="long",
                quantity_ratio=1,
            ),
            WireStrategyLeg(
                strike=price(810.0),
                expiration="2026-06-19",
                contract_type="call",
                direction="short",
                quantity_ratio=1,
            ),
        ),
    )


def _build_strategy_position(
    instrument: StrategyInstrument | None = None,
) -> PositionRecord:
    """Build a PENDING strategy position through ``_build_pending_position``."""
    return _build_pending_position(
        position_id="POS-NVDA-abc123",
        thesis_id="THE-NVDA-abc123",
        bracket_id="BRK-NVDA-abc123",
        instrument=instrument if instrument is not None else _vertical_spread(),
        direction=Direction.LONG,
    )


def test_strategy_instrument_yields_strategy_position_details() -> None:
    """A StrategyInstrument no longer raises NotImplementedError — it lands a
    PositionRecord carrying StrategyPositionDetails."""
    record = _build_strategy_position()

    assert isinstance(record, PositionRecord)
    assert isinstance(record.details, StrategyPositionDetails)

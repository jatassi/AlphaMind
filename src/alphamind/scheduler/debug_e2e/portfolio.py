"""Synthetic portfolio fixtures for ``--debug-e2e`` mode (story 02b / ALP-498).

Pure data — six frozen-slot dataclasses (``SyntheticEquity``,
``SyntheticOption``, ``SyntheticStrategyLeg``, ``SyntheticStrategy``,
``SyntheticThesis``, ``SyntheticPortfolio``), a ``SyntheticPosition`` union
type alias over the three instrument shapes, and two module-level
fixtures the CLI selects between:

* :data:`SYNTHETIC_PORTFOLIO` — 8 positions / 8 theses / $24,440 cash; the
  managed-portfolio fixture matching the debug-e2e design doc § 4.
* :data:`FRESH_START_PORTFOLIO` — empty positions / empty theses /
  $100,000 cash; exercises the analyst's OPEN-recommendation path and the
  cash-only initial-state edge cases (ALP-618).

Both fixtures are frozen module constants (not functions returning fresh
instances) — value identity matters because the same constant flows through
the ``configure_debug_e2e`` seam and into the seeder, the log-only broker,
and any downstream tests that assert against fixture values.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from alphamind._kernel.ids import Symbol, make_symbol
from alphamind.portfolio_state.records.positions import (
    Direction,
    OptionContractType,
)

__all__ = [
    "FRESH_START_PORTFOLIO",
    "SYNTHETIC_PORTFOLIO",
    "SyntheticEquity",
    "SyntheticOption",
    "SyntheticPortfolio",
    "SyntheticPosition",
    "SyntheticStrategy",
    "SyntheticStrategyLeg",
    "SyntheticThesis",
]


# A small literal of sector tags so downstream consumers (paper-eval harness,
# qualitative-research bundling diagnostics) can group positions sectorally
# without depending on the production sector classifier.
_Sector = Literal["tech", "semis", "financials", "energy"]


@dataclass(frozen=True, slots=True)
class SyntheticEquity:
    """A long or short equity leg of the synthetic portfolio.

    ``borrow_rate_pct`` is set only for short positions; long positions leave
    it ``None``. The seeder (story 01c) reflects that into the
    ``borrow_rate_pct`` column on ``PositionRow`` for the short variant.
    """

    symbol: Symbol
    direction: Direction
    qty: float
    avg_cost: float
    sector: _Sector
    borrow_rate_pct: float | None = None


@dataclass(frozen=True, slots=True)
class SyntheticOption:
    """A single-leg option position.

    ``expiration_offset_days`` is added to ``now`` at seed time so the
    expiration date is always anchored relative to the run start — no
    fixture rot when the test runs months later.
    """

    underlying: Symbol
    contract_type: OptionContractType
    strike: float
    expiration_offset_days: int
    contracts: float
    premium_per_contract: float
    sector: _Sector


@dataclass(frozen=True, slots=True)
class SyntheticStrategyLeg:
    """One leg of a multi-leg synthetic options strategy."""

    contract_type: OptionContractType
    strike: float
    direction: Direction
    contracts: float


@dataclass(frozen=True, slots=True)
class SyntheticStrategy:
    """A multi-leg options strategy (e.g., a bull-call spread).

    ``net_premium`` is the dollar value of the spread * number of spreads
    (positive for net debit, sign-flipped by the seeder if a future fixture
    needs a net-credit shape).
    """

    underlying: Symbol
    expiration_offset_days: int
    legs: tuple[SyntheticStrategyLeg, ...]
    net_premium: float
    strategy_label: str
    sector: _Sector


@dataclass(frozen=True, slots=True)
class SyntheticThesis:
    """A one-sentence thesis + 2-3 sentence rationale tied to a position.

    The seeder pairs the thesis with the position at ``position_index`` —
    indices reference :attr:`SyntheticPortfolio.positions` 0-based.
    """

    position_index: int
    headline: str
    rationale: str


SyntheticPosition = SyntheticEquity | SyntheticOption | SyntheticStrategy


@dataclass(frozen=True, slots=True)
class SyntheticPortfolio:
    """The full synthetic-portfolio fixture.

    Three parallel structures:

    * ``positions`` — the 8 instrument records (5 equities, 2 options, 1 strategy).
    * ``theses`` — the 8 thesis records (one per position by index).
    * ``starting_cash_usd`` — the seeded singleton ``cash_ledger`` balance.
    """

    positions: tuple[SyntheticPosition, ...]
    theses: tuple[SyntheticThesis, ...]
    starting_cash_usd: Decimal


# ---------------------------------------------------------------------------
# Canonical synthetic-portfolio constant — value identity matters.
# ---------------------------------------------------------------------------

SYNTHETIC_PORTFOLIO = SyntheticPortfolio(
    positions=(
        SyntheticEquity(
            symbol=make_symbol("NVDA"),
            direction=Direction.LONG,
            qty=30,
            avg_cost=620.0,
            sector="semis",
        ),
        SyntheticEquity(
            symbol=make_symbol("JPM"),
            direction=Direction.LONG,
            qty=100,
            avg_cost=190.0,
            sector="financials",
        ),
        SyntheticEquity(
            symbol=make_symbol("GOOGL"),
            direction=Direction.LONG,
            qty=70,
            avg_cost=200.0,
            sector="tech",
        ),
        SyntheticEquity(
            symbol=make_symbol("COP"),
            direction=Direction.LONG,
            qty=130,
            avg_cost=115.0,
            sector="energy",
        ),
        SyntheticEquity(
            symbol=make_symbol("TSLA"),
            direction=Direction.SHORT,
            qty=30,
            avg_cost=250.0,
            sector="tech",
            borrow_rate_pct=1.5,
        ),
        SyntheticOption(
            underlying=make_symbol("AAPL"),
            contract_type=OptionContractType.CALL,
            strike=230.0,
            expiration_offset_days=270,
            contracts=3,
            premium_per_contract=14.50,
            sector="tech",
        ),
        SyntheticOption(
            underlying=make_symbol("META"),
            contract_type=OptionContractType.PUT,
            strike=480.0,
            expiration_offset_days=90,
            contracts=1,
            premium_per_contract=22.00,
            sector="tech",
        ),
        SyntheticStrategy(
            underlying=make_symbol("MSFT"),
            expiration_offset_days=180,
            legs=(
                SyntheticStrategyLeg(
                    contract_type=OptionContractType.CALL,
                    strike=440.0,
                    direction=Direction.LONG,
                    contracts=2,
                ),
                SyntheticStrategyLeg(
                    contract_type=OptionContractType.CALL,
                    strike=480.0,
                    direction=Direction.SHORT,
                    contracts=2,
                ),
            ),
            net_premium=12.30 * 2,
            strategy_label="bull_call_spread",
            sector="tech",
        ),
    ),
    theses=(
        SyntheticThesis(
            position_index=0,
            headline="NVDA — sustained AI capex tailwind",
            rationale=(
                "Hyperscaler 2026 capex guides keep order-book visibility "
                "into late FY27. Margin trajectory holds even as the mix "
                "shifts toward GB300 silicon. We're paid to wait."
            ),
        ),
        SyntheticThesis(
            position_index=1,
            headline="JPM — quality bank levered to a flatter curve",
            rationale=(
                "NII headwinds priced in; capital-markets fee mix is "
                "recovering and reserves still over-cushion. Buyback "
                "cadence supports the floor on multiple."
            ),
        ),
        SyntheticThesis(
            position_index=2,
            headline="GOOGL — Search durability undervalued",
            rationale=(
                "AI-search disruption narrative has overshot; ad-rev "
                "comps remain intact and Cloud margin is inflecting. "
                "Multiple still compressed vs. peers on a 2027 basis."
            ),
        ),
        SyntheticThesis(
            position_index=3,
            headline="COP — capital-discipline beneficiary",
            rationale=(
                "Variable-dividend framework + low decline base = "
                "FCF/share growth even if WTI mean-reverts to mid-60s. "
                "Returns optionality on Marathon synergies still to run."
            ),
        ),
        SyntheticThesis(
            position_index=4,
            headline="TSLA short — robotaxi expectations overshoot",
            rationale=(
                "Auto-segment margins still compressing while the equity "
                "prices a >$200B AV TAM. Funding pivot to robotaxi capex "
                "weighs on near-term FCF; consensus catches down."
            ),
        ),
        SyntheticThesis(
            position_index=5,
            headline="AAPL — services-mix call on a soft cycle bottom",
            rationale=(
                "iPhone unit pessimism is fully priced; services growth "
                "and Vision Pro lifecycle ramp leave a clean setup into "
                "FY27 super-cycle catalysts."
            ),
        ),
        SyntheticThesis(
            position_index=6,
            headline="META — short-dated put on AI-capex digestion",
            rationale=(
                "Capex trajectory steepens just as Reels growth flattens. "
                "Near-term margin pressure not fully reflected; the put "
                "carries cleanly against post-print drawdown risk."
            ),
        ),
        SyntheticThesis(
            position_index=7,
            headline="MSFT — moderate bullish stance via bull-call spread",
            rationale=(
                "Azure deceleration concerns overblown; CoPilot uptake "
                "and gaming-segment normalization support a re-rate. "
                "Spread caps premium outlay vs. an outright call."
            ),
        ),
    ),
    starting_cash_usd=Decimal(24440),
)


# ---------------------------------------------------------------------------
# Clean-slate fixture — empty book, $100k cash (ALP-618).
# ---------------------------------------------------------------------------

FRESH_START_PORTFOLIO = SyntheticPortfolio(
    positions=(),
    theses=(),
    starting_cash_usd=Decimal(100_000),
)

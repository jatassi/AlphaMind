"""Per-proposal single-leg option replay primitives (ALP-562, design Steps 2-4).

Pure simulation over a hydrated analyst :class:`Recommendation` with an option
instrument: a market-style entry fill at the bar following the proposal,
BS-derived entry premium, an underlying-bar bracket walk (reusing story 05a's
:func:`~alphamind.execution.counterfactual_replay_engine.equity_replay.simulate_equity_brackets`),
a BS-derived exit premium at the trigger, and P/L composition with the contract
multiplier and paper-harness slippage / fees.

Black-Scholes runs exactly twice — once at entry, once at exit — per design
§ Why BS runs only at entry and exit. The conservative delta buffer used at
OPEN/ADD validation is intentionally disabled: replay estimates outcomes, it
does not gate decisions.

The engine driver (story 08) supplies the underlying bar sequence, the paper-
harness config, the risk-free rate, and the ADV / realized-volatility lookups,
then folds :class:`OptionReplayResult` into a ``CounterfactualReplayRecord``.
An exit-side IV miss surfaces as ``realized_pl=None`` (the sentinel the driver
maps to ``DATA_MISSING``) rather than a crash or a fabricated IV.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from alphamind._kernel.money import DECIMAL_ZERO, Money, Price, money, price, signed_money
from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.decision.analyst.models import InstrumentOption, Recommendation
from alphamind.execution.counterfactual_replay_engine.enums import ExitLeg
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    EquityEntryResult,
    simulate_equity_brackets,
)
from alphamind.execution.counterfactual_replay_engine.iv_lookup import IVSnapshotLookupResult
from alphamind.execution.counterfactual_replay_engine.repos import (
    OhlcvBar,
    OptionsSnapshotRepository,
)
from alphamind.execution.paper_evaluation_harness.harness import (
    compute_live_execution_estimate,
)
from alphamind.portfolio_state.records.positions import InstrumentType
from alphamind.risk_guardrails.guardrail_evaluation.black_scholes import bs_price
from alphamind.risk_guardrails.guardrail_evaluation.types import ContractType

__all__ = [
    "OptionBracketResult",
    "OptionEntryResult",
    "OptionPLResult",
    "OptionReplayResult",
    "compute_option_pl",
    "price_option_at_underlying_bar",
    "replay_option_proposal",
    "simulate_option_brackets",
    "simulate_option_entry",
]

# A market-style option entry needs the proposal bar plus the following bar.
_MIN_BARS_FOR_ENTRY = 2

# Seconds per calendar year (365.25 days), matching the design's TTE formula.
_SECONDS_PER_YEAR = 365.25 * 86400


# ---------------------------------------------------------------------------
# Step 1 — BS pricing wrapper
# ---------------------------------------------------------------------------


def price_option_at_underlying_bar(
    *,
    underlying_open: float,
    strike: Decimal,
    expiration: date,
    contract_type: Literal["call", "put"],
    bar_timestamp: datetime,
    implied_volatility: float,
    risk_free_rate: float,
) -> Price:
    """Black-Scholes option premium given the underlying bar open (design §1).

    ``time_to_expiration_years`` is the calendar gap between *bar_timestamp* and
    the expiration (midnight UTC on the expiration date), expressed in years of
    365.25 days. The premium is returned as a :class:`Price`. The conservative
    delta buffer used at OPEN/ADD validation is intentionally disabled — replay
    estimates outcomes; it does not gate decisions.

    This signature is part of the cross-story contract (story 07 imports it);
    keep it stable.
    """
    expiration_dt_utc = datetime(expiration.year, expiration.month, expiration.day, tzinfo=UTC)
    tte = (expiration_dt_utc - bar_timestamp).total_seconds() / _SECONDS_PER_YEAR
    premium = bs_price(
        spot=underlying_open,
        strike=float(strike),
        time_to_expiration_years=tte,
        risk_free_rate=risk_free_rate,
        implied_volatility=implied_volatility,
        contract_type=ContractType(contract_type.upper()),
    )
    return price(Decimal(str(premium)))


# ---------------------------------------------------------------------------
# Step 2 — Option entry simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionEntryResult:
    """Outcome of option entry simulation (design Step 2 — option branch).

    ``entered`` is always ``True`` for option entries: by design they fire as a
    market-style fill at the bar following the proposal, so there is no
    equity-style ``entered=False`` branch (that branch is equity-only). The
    engine driver pre-checks IV availability and marks the proposal
    ``DATA_MISSING`` upstream when the entry-timestamp lookup is ``None``, so
    this function expects a non-None lookup.

    ``entry_iv_lag_minutes`` carries the entry snapshot's lag for the
    confidence classifier (story 05b).
    """

    entered: bool
    entry_price: Price
    entry_timestamp: datetime
    entry_iv_lag_minutes: float


def simulate_option_entry(
    proposal: Recommendation,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    risk_free_rate: float,
) -> OptionEntryResult:
    """Simulate the entry fill for a single-leg option proposal (design Step 2).

    Entry is a market-style fill at the bar *following* the proposal bar,
    regardless of ``entry_order.type``. ``bars[0]`` is the proposal bar (per the
    engine driver's ``load_bars`` window contract); the fill is at
    ``bars[1].open`` / ``bars[1].period_start``. The premium is BS-derived from
    that open, the per-contract IV snapshot at the entry timestamp, the bar's
    time-to-expiration, and the risk-free rate. The proposal's
    ``entry_order.limit_price`` is recorded upstream but does not gate fill
    timing — a v2 simplification surfaced as a baseline confidence caveat
    (design § Option proposals).
    """
    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentOption)
    if len(bars) < _MIN_BARS_FOR_ENTRY:
        msg = "option entry requires the proposal bar plus the following fill bar"
        raise ValueError(msg)

    next_bar = bars[1]
    entry_timestamp = next_bar.period_start
    iv_at_entry = _lookup_iv(iv_repo, instrument, entry_timestamp)
    assert iv_at_entry is not None, "simulate_option_entry expects a non-None entry IV lookup"

    entry_price = price_option_at_underlying_bar(
        underlying_open=next_bar.open,
        strike=instrument.strike,
        expiration=instrument.expiration,
        contract_type=instrument.contract_type,
        bar_timestamp=entry_timestamp,
        implied_volatility=iv_at_entry.implied_volatility,
        risk_free_rate=risk_free_rate,
    )
    return OptionEntryResult(
        entered=True,
        entry_price=entry_price,
        entry_timestamp=entry_timestamp,
        entry_iv_lag_minutes=iv_at_entry.lag_minutes,
    )


def _lookup_iv(
    iv_repo: OptionsSnapshotRepository,
    instrument: InstrumentOption,
    target_ts: datetime,
) -> IVSnapshotLookupResult | None:
    """Resolve the contract ticker and look up the IV snapshot at *target_ts*."""
    contract_ticker = iv_repo.resolve_contract_ticker(
        underlying=instrument.underlying,
        strike=instrument.strike,
        expiration=instrument.expiration,
        contract_type=instrument.contract_type,
    )
    return iv_repo.lookup_iv(contract_ticker=contract_ticker, target_ts=target_ts)

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
from zoneinfo import ZoneInfo

from alphamind._kernel.money import DECIMAL_ZERO, Money, money, price, signed_money
from alphamind.config.models.execution import OrderType, PaperHarness
from alphamind.decision.analyst.models import InstrumentOption, Recommendation
from alphamind.execution.counterfactual_replay_engine.equity_replay import (
    EquityBracketResult,
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
from alphamind.state.tables.counterfactual_replays import ExitLeg

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

# US equity options expire at 16:00 in the exchange's local zone; the zone
# carries the EST/EDT rule so the UTC instant is DST-correct per date.
_EXCHANGE_ZONE = ZoneInfo("America/New_York")


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
) -> Money:
    """Black-Scholes option premium given the underlying bar open (design §1).

    ``time_to_expiration_years`` is the calendar gap between *bar_timestamp* and
    the expiration instant — **16:00 America/New_York** (US equity-option
    expiry), converted to UTC so DST is handled — expressed in years of 365.25
    days. Anchoring on the close-of-trade instant rather than midnight UTC keeps
    TTE positive for an expiry-day intraday bar (midnight UTC would land
    ~7-8 hours *before* a morning bar and yield a spurious negative TTE).

    The premium is returned as :class:`Money` (non-negative, may be zero): a
    worthless option — OTM at or after expiry, where ``bs_price`` returns the
    ``0.0`` intrinsic value — is a legitimate premium, not a pricing error, so
    it must not pass through the strictly-positive :func:`price` constructor.
    The conservative delta buffer used at OPEN/ADD validation is intentionally
    disabled — replay estimates outcomes; it does not gate decisions.

    This signature is part of the cross-story contract (story 07 imports it);
    keep the keyword parameters stable.
    """
    expiration_dt_utc = _expiration_instant_utc(expiration)
    tte = (expiration_dt_utc - bar_timestamp).total_seconds() / _SECONDS_PER_YEAR
    premium = bs_price(
        spot=underlying_open,
        strike=float(strike),
        time_to_expiration_years=tte,
        risk_free_rate=risk_free_rate,
        implied_volatility=implied_volatility,
        contract_type=ContractType(contract_type.upper()),
    )
    return money(Decimal(str(premium)))


def _expiration_instant_utc(expiration: date) -> datetime:
    """The 16:00 America/New_York expiry instant for *expiration*, in UTC.

    US equity options expire at 16:00 ET on the expiration date. Building the
    instant in the exchange zone and converting to UTC picks up the right
    EST/EDT offset for the date (no hardcoded ±5h/±4h).
    """
    expiry_et = datetime(
        expiration.year, expiration.month, expiration.day, 16, 0, tzinfo=_EXCHANGE_ZONE
    )
    return expiry_et.astimezone(UTC)


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

    ``entry_price`` is the BS-derived premium as :class:`Money` (non-negative,
    may be zero — a worthless contract is a legitimate premium).
    """

    entered: bool
    entry_price: Money
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


# ---------------------------------------------------------------------------
# Step 3 — Option bracket simulation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionBracketResult:
    """Outcome of the option bracket walk (design Step 3 — option exit).

    The underlying-bar trigger is found by reusing story 05a's equity walker
    (the trigger fires on the underlying for both asset types per
    orders-and-brackets.md § Options price-based stops). The trigger's
    underlying level is then translated into the option's BS-derived premium at
    the exit-timestamp IV snapshot.

    ``exit_price`` and ``exit_iv_lag_minutes`` are ``None`` on the exit-IV-miss
    sentinel: the underlying trigger was identified but no per-contract IV
    snapshot exists at or before the exit timestamp, so the exit premium cannot
    be derived. The engine driver (story 08) maps that sentinel to
    ``DATA_MISSING``. ``same_bar_ambiguity`` is propagated from the underlying
    walk. There is no P/L-bracket-leg flag: analyst option proposals carry no
    P/L-on-the-option's-own-price leg to omit.
    """

    exit_leg: ExitLeg
    exit_underlying_price: float
    exit_price: Money | None
    exit_timestamp: datetime
    same_bar_ambiguity: bool
    exit_iv_lag_minutes: float | None


def simulate_option_brackets(
    proposal: Recommendation,
    entry: OptionEntryResult,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    risk_free_rate: float,
) -> OptionBracketResult:
    """Walk the underlying bracket trigger, then BS-price the exit premium.

    Reuses :func:`simulate_equity_brackets` to identify the underlying-bar
    trigger (target / price-stop / time-stop) and its
    ``same_bar_ambiguity``. The equity walker records ``exit_price`` as the
    underlying exit level (target price, stop trigger, or bar open at the
    time-stop), which is exactly the ``exit_underlying_price`` the option
    premium is derived from. The exit premium is BS-derived from that
    underlying level, the per-contract IV snapshot at the exit timestamp, the
    bar's time-to-expiration, and the risk-free rate. When the exit IV lookup is
    ``None``, the exit-IV-miss sentinel is returned (``exit_price`` and
    ``exit_iv_lag_minutes`` ``None``) for the driver to map to ``DATA_MISSING``.
    """
    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentOption)

    # The equity walker reads only ``entered`` + ``entry_timestamp`` (and the
    # proposal's underlying levels) to find the trigger bar; it never reads the
    # entry price, so the option premium (``Money``, possibly zero) is not
    # threaded into this throwaway equity entry.
    equity_entry = EquityEntryResult(
        entered=entry.entered,
        entry_price=None,
        entry_timestamp=entry.entry_timestamp,
    )
    walk: EquityBracketResult = simulate_equity_brackets(proposal, equity_entry, bars)
    # Option entries always fire (Step 2), so the walk never short-circuits to
    # the entry-window-expired leg; the underlying trigger always resolves with
    # a concrete exit price and timestamp.
    assert walk.exit_price is not None
    assert walk.exit_timestamp is not None
    exit_underlying_price = float(walk.exit_price)
    exit_timestamp = walk.exit_timestamp

    exit_iv = _lookup_iv(iv_repo, instrument, exit_timestamp)
    if exit_iv is None:
        return OptionBracketResult(
            exit_leg=walk.exit_leg,
            exit_underlying_price=exit_underlying_price,
            exit_price=None,
            exit_timestamp=exit_timestamp,
            same_bar_ambiguity=walk.same_bar_ambiguity,
            exit_iv_lag_minutes=None,
        )

    exit_price = price_option_at_underlying_bar(
        underlying_open=exit_underlying_price,
        strike=instrument.strike,
        expiration=instrument.expiration,
        contract_type=instrument.contract_type,
        bar_timestamp=exit_timestamp,
        implied_volatility=exit_iv.implied_volatility,
        risk_free_rate=risk_free_rate,
    )
    return OptionBracketResult(
        exit_leg=walk.exit_leg,
        exit_underlying_price=exit_underlying_price,
        exit_price=exit_price,
        exit_timestamp=exit_timestamp,
        same_bar_ambiguity=walk.same_bar_ambiguity,
        exit_iv_lag_minutes=exit_iv.lag_minutes,
    )


# ---------------------------------------------------------------------------
# Step 4 — P/L composition for options
# ---------------------------------------------------------------------------

# Standard US listed-options contract multiplier (design Step 4: multiplier =
# 100 for options). 100 underlying shares per contract.
_OPTIONS_MULTIPLIER = Decimal(100)

# Option entries always fill as a market-style order (Step 2), so the entry
# crosses as a market order for the harness's impact coefficient.
_ENTRY_ORDER_TYPE = OrderType.market

# Exit leg → the order type the exit would have crossed as. A target fills as a
# resting limit; a price stop as a stop; a time stop as a market close. Mirrors
# the equity exit mapping.
_EXIT_ORDER_TYPE: dict[ExitLeg, OrderType] = {
    ExitLeg.TARGET_HIT: OrderType.limit,
    ExitLeg.STOP_HIT: OrderType.stop,
    ExitLeg.TIME_STOP_FIRED: OrderType.market,
}


@dataclass(frozen=True, slots=True)
class OptionPLResult:
    """Outcome of option P/L composition (design Step 4 — options).

    All fields are non-``None``: option entries always fire (Step 2) and this
    function is only reached for a resolved exit (the exit-IV-miss sentinel is
    handled by the driver before P/L). When the paper harness cannot produce an
    estimate (missing ADV or realized volatility), the corresponding slippage
    and fees are recorded as zero.
    """

    realized_pl: Money
    entry_slippage: Money
    entry_fees: Money
    exit_slippage: Money
    exit_fees: Money


def compute_option_pl(
    proposal: Recommendation,
    entry: OptionEntryResult,
    brackets: OptionBracketResult,
    *,
    paper_harness_config: PaperHarness,
    adv_contracts: float | None,
    realized_volatility: float | None,
) -> OptionPLResult:
    """Compose realized P/L for an option replay (design Step 4).

    ``realized_pl = (exit_price - entry_price) * quantity * 100 * direction_sign
    - entry_slippage - entry_fees - exit_slippage - exit_fees``, with
    ``direction_sign = +1`` for long and ``-1`` for short, ``quantity`` in
    contracts, and ``multiplier = 100``. Entry and exit slippage / fees come
    from :func:`compute_live_execution_estimate` with
    ``instrument_type=OPTIONS``, called once per side; a ``None`` return records
    that side's slippage and fees as zero. ``realized_pl`` is signed (losses are
    negative).
    """
    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentOption)
    assert brackets.exit_price is not None, "compute_option_pl reached with the exit-IV sentinel"

    direction = instrument.direction
    direction_sign = Decimal(1) if direction == "long" else Decimal(-1)
    quantity = Decimal(str(proposal.position_size.quantity))

    entry_side: Literal["buy", "sell"] = "buy" if direction == "long" else "sell"
    exit_side: Literal["buy", "sell"] = "sell" if direction == "long" else "buy"

    entry_slippage, entry_fees = _side_drag(
        fill_price=entry.entry_price,
        order_type=_ENTRY_ORDER_TYPE,
        side=entry_side,
        quantity=proposal.position_size.quantity,
        config=paper_harness_config,
        adv_contracts=adv_contracts,
        realized_volatility=realized_volatility,
    )
    exit_slippage, exit_fees = _side_drag(
        fill_price=brackets.exit_price,
        order_type=_EXIT_ORDER_TYPE[brackets.exit_leg],
        side=exit_side,
        quantity=proposal.position_size.quantity,
        config=paper_harness_config,
        adv_contracts=adv_contracts,
        realized_volatility=realized_volatility,
    )

    gross = (
        (Decimal(brackets.exit_price) - Decimal(entry.entry_price))
        * quantity
        * _OPTIONS_MULTIPLIER
        * direction_sign
    )
    realized = gross - entry_slippage - entry_fees - exit_slippage - exit_fees
    return OptionPLResult(
        realized_pl=signed_money(realized),
        entry_slippage=entry_slippage,
        entry_fees=entry_fees,
        exit_slippage=exit_slippage,
        exit_fees=exit_fees,
    )


def _side_drag(
    *,
    fill_price: Money,
    order_type: OrderType,
    side: Literal["buy", "sell"],
    quantity: float,
    config: PaperHarness,
    adv_contracts: float | None,
    realized_volatility: float | None,
) -> tuple[Money, Money]:
    """Return ``(slippage, fees)`` for one option fill side via the paper harness.

    ``slippage = estimated_spread_usd + estimated_impact_usd``;
    ``fees = estimated_regulatory_fees_usd``. The harness handles options via
    its 100-share multiplier branch (``adv_shares=adv_contracts``). A ``None``
    estimate (missing ADV / realized vol) records both as zero (design Step 4).

    A worthless ($0) premium carries no estimable transaction drag — there is no
    spread to cross and the harness's per-share-drag adjustment would drive the
    fill price negative — so both components record as zero.
    """
    if fill_price <= DECIMAL_ZERO:
        return money(DECIMAL_ZERO), money(DECIMAL_ZERO)
    estimate = compute_live_execution_estimate(
        fill_price=price(fill_price),
        fill_quantity=quantity,
        instrument_type=InstrumentType.OPTIONS,
        side=side,
        order_type=order_type,
        adv_shares=adv_contracts,
        realized_volatility=realized_volatility,
        config=config,
    )
    if estimate is None:
        return money(DECIMAL_ZERO), money(DECIMAL_ZERO)
    slippage = money(estimate.estimated_spread_usd + estimate.estimated_impact_usd)
    return slippage, estimate.estimated_regulatory_fees_usd


# ---------------------------------------------------------------------------
# Step 5 — Public driver
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OptionReplayResult:
    """The flat per-proposal option replay outcome the engine driver folds into
    a ``CounterfactualReplayRecord`` (story 08).

    The field set mirrors :class:`CounterfactualReplayRecord` (and
    :class:`~.equity_replay.EquityReplayResult`) so the story-08 driver maps
    straight through, plus the two option-only IV-lag fields the confidence
    classifier (story 05b) reads.

    Option entries always fire (Step 2), so ``entered`` is always ``True`` and
    there is no equity-style window-expired branch. ``data_missing`` is the
    explicit IV-miss flag: ``True`` only on the data-missing sentinel (a
    per-contract IV snapshot was unavailable at the entry or exit timestamp, so
    the BS fill estimate could not be derived), which the engine driver maps to
    ``DATA_MISSING``. On the sentinel ``exit_leg`` is ``None`` and ``entry_price``
    / ``exit_price`` / ``exit_slippage`` / ``exit_fees`` / ``exit_iv_lag_minutes``
    are ``None`` so no consumer reads a fabricated exit leg or premium; only the
    underlying-trigger diagnostics and ``entry_iv_lag_minutes`` (when the entry
    snapshot was present) survive. ``realized_pl is None`` continues to mark the
    sentinel for the legacy driver path.

    The premium fields (``entry_price`` / ``exit_price``) are :class:`Money`
    (non-negative, may be zero) since a worthless option is a legitimate premium.
    """

    entered: bool
    entry_price: Money | None
    entry_timestamp: datetime | None
    entry_slippage: Money | None
    entry_fees: Money | None
    entry_iv_lag_minutes: float | None
    exit_leg: ExitLeg | None
    exit_underlying_price: float | None
    exit_price: Money | None
    exit_timestamp: datetime | None
    exit_slippage: Money | None
    exit_fees: Money | None
    exit_iv_lag_minutes: float | None
    realized_pl: Money | None
    data_missing: bool
    same_bar_ambiguity: bool


def replay_option_proposal(
    proposal: Recommendation,
    bars: tuple[OhlcvBar, ...],
    *,
    iv_repo: OptionsSnapshotRepository,
    paper_harness_config: PaperHarness,
    risk_free_rate: float,
    adv_contracts: float | None,
    realized_volatility: float | None,
) -> OptionReplayResult:
    """Replay one single-leg option proposal end-to-end (design Steps 2-4).

    Composes :func:`simulate_option_entry`, :func:`simulate_option_brackets`,
    and :func:`compute_option_pl` into one :class:`OptionReplayResult`. When the
    per-contract IV snapshot is missing at the entry or exit timestamp, returns
    the data-missing sentinel (``realized_pl=None``) for the driver to map to
    ``DATA_MISSING`` — no fabricated IV, no crash.
    """
    instrument = proposal.instrument
    assert isinstance(instrument, InstrumentOption)
    if len(bars) < _MIN_BARS_FOR_ENTRY:
        msg = "option replay requires the proposal bar plus the following fill bar"
        raise ValueError(msg)

    # Gate on entry-IV availability before simulating: simulate_option_entry
    # expects a non-None lookup, so an entry-side miss is the data-missing
    # sentinel (the same DATA_MISSING the exit-side miss yields).
    entry_timestamp = bars[1].period_start
    if _lookup_iv(iv_repo, instrument, entry_timestamp) is None:
        return _data_missing_sentinel(entry_iv_lag_minutes=None)

    entry = simulate_option_entry(proposal, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate)
    brackets = simulate_option_brackets(
        proposal, entry, bars, iv_repo=iv_repo, risk_free_rate=risk_free_rate
    )
    if brackets.exit_price is None:
        # Exit-side IV miss: the underlying trigger is known but the exit premium
        # cannot be derived. DATA_MISSING; entry IV lag is still meaningful.
        return _data_missing_sentinel(
            entry_iv_lag_minutes=entry.entry_iv_lag_minutes,
            exit_leg=brackets.exit_leg,
            exit_underlying_price=brackets.exit_underlying_price,
            exit_timestamp=brackets.exit_timestamp,
            same_bar_ambiguity=brackets.same_bar_ambiguity,
        )

    pl = compute_option_pl(
        proposal,
        entry,
        brackets,
        paper_harness_config=paper_harness_config,
        adv_contracts=adv_contracts,
        realized_volatility=realized_volatility,
    )
    return OptionReplayResult(
        entered=entry.entered,
        entry_price=entry.entry_price,
        entry_timestamp=entry.entry_timestamp,
        entry_slippage=pl.entry_slippage,
        entry_fees=pl.entry_fees,
        entry_iv_lag_minutes=entry.entry_iv_lag_minutes,
        exit_leg=brackets.exit_leg,
        exit_underlying_price=brackets.exit_underlying_price,
        exit_price=brackets.exit_price,
        exit_timestamp=brackets.exit_timestamp,
        exit_slippage=pl.exit_slippage,
        exit_fees=pl.exit_fees,
        exit_iv_lag_minutes=brackets.exit_iv_lag_minutes,
        realized_pl=pl.realized_pl,
        data_missing=False,
        same_bar_ambiguity=brackets.same_bar_ambiguity,
    )


def _data_missing_sentinel(
    *,
    entry_iv_lag_minutes: float | None,
    exit_leg: ExitLeg | None = None,
    exit_underlying_price: float | None = None,
    exit_timestamp: datetime | None = None,
    same_bar_ambiguity: bool = False,
) -> OptionReplayResult:
    """Build the data-missing sentinel ``OptionReplayResult`` (``data_missing=True``).

    The engine driver (story 08) reads ``data_missing`` as the ``DATA_MISSING``
    signal and discards the partial entry/exit fields. ``exit_leg`` is ``None``
    on the entry-side miss (no exit was reached) and carries the genuinely
    identified underlying-trigger leg on the exit-side miss — no fabricated
    ``TARGET_HIT``. The surviving ``entry_iv_lag_minutes`` (when the entry
    snapshot was present) and the underlying-trigger fields are diagnostic only;
    ``realized_pl is None`` continues to mark the sentinel for the legacy path.
    """
    return OptionReplayResult(
        entered=True,
        entry_price=None,
        entry_timestamp=None,
        entry_slippage=None,
        entry_fees=None,
        entry_iv_lag_minutes=entry_iv_lag_minutes,
        exit_leg=exit_leg,
        exit_underlying_price=exit_underlying_price,
        exit_price=None,
        exit_timestamp=exit_timestamp,
        exit_slippage=None,
        exit_fees=None,
        exit_iv_lag_minutes=None,
        realized_pl=None,
        data_missing=True,
        same_bar_ambiguity=same_bar_ambiguity,
    )

"""Boundary-contract dataclasses and enums for the guardrail-evaluation library (story 01).

The library is a pure-function math layer with three callers — the agent-side
validation tool, the proposal pre-processor's combined-set check, and the
engine T3 enforcement check. This module defines the input/output shapes those
primitives consume and produce; downstream stories implement the math against
these types.

Every dataclass uses ``frozen=True, slots=True`` so the library's inputs and
outputs are immutable and hashable. ``Mapping``/``tuple`` substitute for
``dict``/``list`` on dataclass fields. The duplicated ``EscalationZones`` (the
Pydantic version lives in ``alphamind.config.models.guardrails``) exists
because ``LibraryOutput`` must be hashable for determinism tests, which the
Pydantic version cannot reliably satisfy. The ``from_resolved_config`` adapter
in story 02c bridges the two.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Protocol

# ``RiskZone`` is re-exported from :mod:`alphamind._kernel.regime` (ALP-457
# moved its canonical home). External consumers may still import it from
# this module for backward compatibility; new code should import from
# ``alphamind._kernel.regime``. The redundant ``as`` form marks the name
# as an explicit re-export for mypy.
from alphamind._kernel.money import Money
from alphamind._kernel.regime import RiskZone as RiskZone

# ---------------------------------------------------------------------------
# Classification enums
# ---------------------------------------------------------------------------


class Status(Enum):
    """Three-status collapse of breach-behavior's four zones for the projection.

    The mapping from breach-behavior zones (Normal/Warning/Critical/Hard block)
    to these three statuses lives in story 04 (projection engine).
    """

    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class Direction(Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class AssetType(Enum):
    EQUITY = "EQUITY"
    OPTION = "OPTION"
    STRATEGY = "STRATEGY"


class ContractType(Enum):
    CALL = "CALL"
    PUT = "PUT"


class Action(Enum):
    """OMS command names. Only OPEN/ADD/CLOSE change exposure; ADJUST/CANCEL
    are passed through for the entry point's gate logic in story 05."""

    OPEN = "OPEN"
    ADD = "ADD"
    CLOSE = "CLOSE"
    ADJUST = "ADJUST"
    CANCEL = "CANCEL"


class IvSource(Enum):
    """Where the IV used in any options computation came from. Reported on
    ``DeltaAdjustedExposure`` so callers can segment outcomes by IV provenance."""

    SURFACE = "SURFACE"
    REALIZED_VOL_FALLBACK = "REALIZED_VOL_FALLBACK"


# ---------------------------------------------------------------------------
# Math primitive shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IvLookupResult:
    """Outcome of a single ``IvProvider.lookup_iv`` call.

    ``notes`` is ``None`` for a clean surface hit; populated with a short tag
    (e.g., ``"strike_interpolated"``, ``"expiration_interpolated"``,
    ``"realized_vol_fallback_no_chain"``,
    ``"realized_vol_fallback_strike_outside_chain"``,
    ``"realized_vol_fallback_expiration_extrapolated"``) whenever the lookup
    interpolated non-trivially or fell back. ``notes`` is informational —
    callers log it for IV-provenance auditing but the projection math does not
    branch on its value.
    """

    implied_volatility: float
    source: IvSource
    notes: str | None


class IvProvider(Protocol):
    """The library's IV-sourcing contract.

    Concrete implementations: ``FixtureIvProvider`` (test/bootstrap) and
    ``SqlOptionsIvProvider`` (ALP-642 — the Polygon-backed production
    adapter that reads ``options_contract_snapshots``). ``lookup_iv`` is
    total — every successful path returns an ``IvLookupResult`` with
    positive ``implied_volatility``; the inability to produce one raises
    ``IvLookupError``.
    """

    def lookup_iv(
        self,
        *,
        underlying: str,
        strike: float,
        expiration: date,
        contract_type: ContractType,
        as_of: datetime,
    ) -> IvLookupResult: ...


@dataclass(frozen=True, slots=True)
class Greeks:
    """Per-leg result of the Black-Scholes core and strategy-level net result
    after aggregation."""

    delta: float
    gamma: float
    theta: float
    vega: float


@dataclass(frozen=True, slots=True)
class OptionLeg:
    """One leg of an option proposal. ``quantity`` is signed: positive = long
    the leg, negative = short the leg."""

    contract_type: ContractType
    strike: float
    expiration: date
    quantity: int


# ---------------------------------------------------------------------------
# Caller-supplied inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposedDelta:
    """Caller's proposed exposure change.

    ``direction`` is the position-level long/short sign for an EQUITY or
    single-leg OPTION proposal and ``None`` for a multi-leg STRATEGY (ALP-603):
    a strategy has no meaningful position-level direction — its directional
    sign lives in the per-leg / net-greeks data. Cross-field invariants (e.g.,
    ``option_legs is None ⇔ asset_type == EQUITY``, ``direction is None ⇔
    asset_type == STRATEGY``) are checked at the entry point in story 05; the
    dataclass itself is constructible without runtime validation beyond the
    type system.
    """

    id: str
    underlying: str
    sector: str
    direction: Direction | None
    asset_type: AssetType
    notional_usd: Money
    quantity: float
    option_legs: tuple[OptionLeg, ...] | None
    action: Action
    existing_position_id: str | None
    daily_borrow_cost_usd: float | None = None
    reserves_capital: bool = False


@dataclass(frozen=True, slots=True)
class ExistingPosition:
    """Minimal per-position fields the library consults for ADD/CLOSE/ADJUST.

    ``direction`` is the position-level long/short sign for an equity or
    single-leg option position and ``None`` for a multi-leg strategy (ALP-603):
    a strategy's directional sign lives in its per-leg / net-greeks data, not
    this field. ``current_greeks`` is per-contract (per-unit), not total
    position greeks; the position's total theta/vega/etc. is
    ``current_greeks.X * quantity * contract_multiplier``. It is None for
    equity. ``daily_borrow_cost_usd`` is None for long and options positions,
    populated for shorts; ``reserves_capital_usd`` is the capital the existing
    position holds against an unfilled non-marketable limit (zero for filled
    positions). ``quantity`` is the position's current contract count
    (options/strategies) or share count (equity); options/strategy positions
    must populate it for CLOSE on options-greeks rules to compute correctly.
    """

    position_id: str
    underlying: str
    sector: str
    direction: Direction | None
    asset_type: AssetType
    notional_usd: float
    delta_adjusted_exposure_usd: float
    current_greeks: Greeks | None
    daily_borrow_cost_usd: float | None
    reserves_capital_usd: float
    quantity: float = 0.0


@dataclass(frozen=True, slots=True)
class PortfolioStateSnapshot:
    """Phase-1 portfolio snapshot the library projects deltas against.

    Pre-aggregated read interface — the library does not iterate raw positions
    to compute exposures (that lives in the portfolio-state ingestion layer
    per ``portfolio-state.md`` § 4c). All percentages are of portfolio.

    ``position_max_size_pct`` is the actual maximum position size as a percent
    of portfolio value across ``open_positions + pending_positions`` (ALP-624)
    — NOT the rule's limit value. The basis is **underlying notional
    exposure** (``notional_exposure_usd``), matching the rule's projection
    math (which operates on proposal/position notional, not gross market
    value); the two differ by orders of magnitude for options/strategies
    because their ``current_market_value_usd`` is premium while
    ``notional_exposure_usd`` is the underlying exposure (ALP-621). The
    rule's limit lives in ``LibraryConfig.effective_limits`` keyed by the
    rule's ``effective_limit_key`` (``"position_max_size_pct"``); the
    projection engine reads ``state.position_max_size_pct`` as the current
    value and the limit from the config to derive the breach status.
    ``0.0`` when the book holds no positions.

    ``single_short_max_position_id`` carries the ``position_id`` of the short
    whose ``position_weight_pct`` equals ``single_short_max_pct``; the
    ``single_short_max_pct`` rule's projection routes this id into the
    cascade dispatcher so the breach handler closes the right position
    without re-scanning ``existing_positions``. ``None`` when no shorts are
    open.
    """

    portfolio_value_usd: float
    cash_usd: float
    reserved_for_pending_orders_usd: float
    sector_exposure_pct: Mapping[str, float]
    net_long_pct: float
    net_short_pct: float
    gross_pct: float
    options_delta_pct: float
    portfolio_theta_pct_per_day: float
    portfolio_vega_pct_per_iv_point: float
    total_short_pct: float
    single_short_max_pct: float
    daily_borrow_cost_pct: float
    position_max_size_pct: float
    existing_positions: Mapping[str, ExistingPosition]
    single_short_max_position_id: str | None = None

    def __hash__(self) -> int:
        return hash(
            (
                self.portfolio_value_usd,
                self.cash_usd,
                self.reserved_for_pending_orders_usd,
                tuple(sorted(self.sector_exposure_pct.items())),
                self.net_long_pct,
                self.net_short_pct,
                self.gross_pct,
                self.options_delta_pct,
                self.portfolio_theta_pct_per_day,
                self.portfolio_vega_pct_per_iv_point,
                self.total_short_pct,
                self.single_short_max_pct,
                self.daily_borrow_cost_pct,
                self.position_max_size_pct,
                tuple(sorted(self.existing_positions.items())),
                self.single_short_max_position_id,
            )
        )


@dataclass(frozen=True, slots=True)
class MarketInputs:
    """Market data the Black-Scholes math reads.

    ``risk_free_rate`` is annualized in decimal form (e.g., ``0.045`` for
    4.5%). ``as_of`` anchors time-to-expiration as ``(expiration - as_of)``
    in days/365.
    """

    underlying_prices: Mapping[str, float]
    risk_free_rate: float
    iv_provider: IvProvider
    as_of: datetime

    def __hash__(self) -> int:
        # ``iv_provider`` is hashed by identity — the protocol implementation
        # is callable and not generally value-hashable. Determinism tests
        # (story 05) hold the same provider across the run, so identity is the
        # right equivalence class here.
        return hash(
            (
                tuple(sorted(self.underlying_prices.items())),
                self.risk_free_rate,
                id(self.iv_provider),
                self.as_of,
            )
        )


@dataclass(frozen=True, slots=True)
class EscalationZones:
    """Per-rule warning/critical/hard-block percentages of the limit.

    Order invariant ``warning < critical < hard_block`` is asserted at
    construction. The Pydantic-shaped twin in ``alphamind.config.models``
    encodes the same data; the dataclass version exists so the library's
    ``LibraryOutput`` stays hashable (story 01 Notes).
    """

    warning: float
    critical: float
    hard_block: float

    def __post_init__(self) -> None:
        if not (self.warning < self.critical < self.hard_block):
            raise ValueError(
                f"EscalationZones must satisfy warning < critical < hard_block, got "
                f"{self.warning}/{self.critical}/{self.hard_block}"
            )


@dataclass(frozen=True, slots=True)
class FeatureFlagsView:
    """Subset of ``ResolvedConfig.feature_flags`` the library consults.

    The full flag bag carries more fields the library does not read (e.g.,
    ``fractional_shares_required``); carving the view keeps the boundary
    surface narrow.
    """

    options_enabled: bool
    short_selling_enabled: bool


@dataclass(frozen=True, slots=True)
class LibraryConfig:
    """The carved subset of ``ResolvedConfig`` this library reads.

    Adaptation from ``ResolvedConfig`` is story 02c's responsibility. Only
    rules in scope under the active profile appear in ``effective_limits``.
    ``conservative_buffer_pct`` is the base buffer applied to absolute delta
    (default ``10.0`` for +10%); per-regime override is applied at the call
    site in story 03.
    """

    effective_limits: Mapping[str, float]
    escalation_zones: Mapping[str, EscalationZones]
    feature_flags: FeatureFlagsView
    active_sectors: tuple[str, ...]
    active_regime: str
    active_profile: str
    conservative_buffer_pct: float

    def __hash__(self) -> int:
        return hash(
            (
                tuple(sorted(self.effective_limits.items())),
                tuple(sorted(self.escalation_zones.items())),
                self.feature_flags,
                self.active_sectors,
                self.active_regime,
                self.active_profile,
                self.conservative_buffer_pct,
            )
        )


# ---------------------------------------------------------------------------
# Library outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeltaAdjustedExposure:
    """Per-proposal Black-Scholes / aggregation result, consumed by the
    projection layer.

    For equity, ``net_greeks``/``iv_used``/``iv_source``/``unbuffered_delta``
    are all None. For options/strategies, ``signed_notional_usd`` is
    ``buffered_net_delta * spot * contract_multiplier * signed direction``.
    """

    proposal_id: str
    signed_notional_usd: float
    net_greeks: Greeks | None
    iv_used: float | None
    iv_source: IvSource | None
    unbuffered_delta: float | None


@dataclass(frozen=True, slots=True)
class ProposalContribution:
    """One proposal's signed attribution toward a rule's projected value.

    Returned by ``RuleSpec.contributors_from_batch`` for holistic rules
    whose contributors cannot be derived by walking per-proposal
    ``spec.contribute(...)`` (ALP-636). The proposal pre-processor wraps
    each entry into a schema-side ``ContributorEntry``.

    ``contribution`` semantics match the schema's contributor field: signed,
    in the rule's units, positive when the proposal pushes the rule toward
    breach. For ``position_max_size_pct`` it is the post-batch size of the
    position the proposal shaped, as % of portfolio.
    """

    proposal_id: str
    contribution: float


@dataclass(frozen=True, slots=True)
class FeatureDisabledRejection:
    """A proposal filtered by the feature-flag gate before the projection layer
    saw it.

    ``reason`` is a short identifier (e.g., ``"options_disabled_on_micro"``,
    ``"shorts_disabled_on_small"``); ``disabled_feature`` is one of
    ``"options"`` or ``"shorts"``.
    """

    proposal_id: str
    reason: str
    disabled_feature: str


@dataclass(frozen=True, slots=True)
class RuleProjection:
    """Canonical per-rule output reused verbatim by every caller.

    ``headroom_remaining`` is signed: ``limit - projected_after``, negative on
    FAIL. ``unit`` is a display string (e.g., ``"% of portfolio
    (delta-adjusted)"``, ``"% of portfolio per 1-pt IV move"``, ``"USD/day"``).
    ``inverse`` is the canonical floor-vs-cap flag: ``True`` when ``limit`` is
    a floor (e.g., ``min_cash_reserve_pct``) and ``False`` when ``limit`` is a
    cap (every other rule). Callers branching on rule semantics read this flag
    rather than maintaining a parallel registry of inverse-rule IDs.

    ``breaching_position_id`` identifies the position whose state triggered
    the rule. Per-position rules (``position_max_loss_*_pct``,
    ``single_short_max_pct``) populate it so the continuous-monitor cascade
    dispatcher routes the close envelope to the exact breaching position
    instead of re-scanning the open-positions list for the worst in-class
    loser. ``None`` for portfolio-scope rules.
    """

    rule: str
    status: Status
    current: float
    limit: float
    projected_after: float
    headroom_remaining: float
    unit: str
    inverse: bool = False
    breaching_position_id: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryOutput:
    """Entry point's return shape.

    ``per_rule`` is one entry per rule in scope (rules disabled by feature
    flags do not appear). ``delta_adjusted`` is keyed on ``ProposedDelta.id``
    with one entry per proposal — equity proposals included with
    ``net_greeks=None``. ``feature_disabled`` is empty when no proposals were
    filtered.
    """

    per_rule: tuple[RuleProjection, ...]
    delta_adjusted: Mapping[str, DeltaAdjustedExposure]
    feature_disabled: tuple[FeatureDisabledRejection, ...] = field(default_factory=tuple)

    def __hash__(self) -> int:
        return hash(
            (
                self.per_rule,
                tuple(sorted(self.delta_adjusted.items())),
                self.feature_disabled,
            )
        )

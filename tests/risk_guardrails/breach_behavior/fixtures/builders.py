"""Compact builder helpers for the end-to-end scenario tests.

Each helper covers one typed record the breach-behavior primitives consume.
Defaults are sensible for the design's worked examples; tests override only
the fields load-bearing for their assertion.

Library-callable Protocol stubs (`ScriptedLibrary`, `StubLibraryConfig`, etc.)
mirror the patterns in ``test_secondary_breach.py`` and ``test_cascade.py`` so
the cascade orchestration tests can drive ``evaluate_proposals`` deterministically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from alphamind._kernel.ids import (
    PositionId,
    Symbol,
)
from alphamind._kernel.money import money, price, signed_money
from alphamind._kernel.regime import (
    DrawdownTier,
    RegimeLabel,
    RegimeTransitionState,
    RiskZone,
)
from alphamind.portfolio_state.aggregates.drawdown import DrawdownState
from alphamind.portfolio_state.aggregates.risk_budget import (
    RiskBudgetConsumption,
    RiskBudgetEntry,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.positions import (
    Direction,
    EquityPositionDetails,
    LocateStatus,
    PositionFill,
    PositionRecord,
    PositionStatus,
)
from alphamind.portfolio_state.views.positions import PositionView

# ---------------------------------------------------------------------------
# Position builder
# ---------------------------------------------------------------------------

_DEFAULT_FILL_TS = datetime(2026, 4, 28, 14, 0, tzinfo=UTC)


def make_position_record(
    *,
    position_id: str,
    ticker: str,
    direction: Literal["long", "short"] = "long",
    size_pct: float,
    size_usd: float,
    unrealized_pnl_usd: float = 0.0,
    asset_type: Literal["equity"] = "equity",
    fill_timestamp: datetime = _DEFAULT_FILL_TS,
) -> PositionView:
    """Construct an equity ``PositionView`` for scenario-test use.

    ``size_pct`` is the position weight as a percentage of portfolio value;
    ``size_usd`` is the current notional / market value. The builder fills in
    plausible values for the audit fields the breach-behavior primitives do
    not consume (``unrealized_pnl_pct``, ``position_age_hours``, etc.) so
    tests can stay focused on the load-bearing inputs.
    """
    if asset_type != "equity":
        msg = f"asset_type {asset_type!r} not supported by builder yet; use equity"
        raise ValueError(msg)
    is_short = direction == "short"
    direction_enum = Direction.SHORT if is_short else Direction.LONG
    share_count = size_usd / 100.0  # synthetic; primitives use only weight/notional/pnl
    equity_details = EquityPositionDetails(
        ticker=Symbol(ticker),
        share_count=share_count,
        average_cost_basis_per_share=100.0,
        borrow_rate_pct=2.5 if is_short else None,
        accrued_borrow_cost_usd=0.0 if is_short else None,
        locate_status=LocateStatus.LOCATED if is_short else None,
        margin_held_usd=size_usd / 5.0 if is_short else None,
    )
    delta_signed = -size_usd if is_short else size_usd
    cost_basis = max(size_usd - unrealized_pnl_usd, 1e-9)
    unrealized_pnl_pct = (unrealized_pnl_usd / cost_basis) * 100.0
    record = PositionRecord(
        position_id=PositionId(position_id),
        thesis_id=None,
        bracket_id=None,
        status=PositionStatus.OPEN,
        direction=direction_enum,
        entry_timestamp=fill_timestamp,
        details=equity_details,
        execution_history=(
            PositionFill(
                fill_timestamp=fill_timestamp,
                fill_price=price(100.0),
                fill_quantity=share_count,
                slippage=signed_money(0.01),
                fees=money(1.0),
            ),
        ),
        realized_pnl_to_date_usd=None,
        corporate_action_adjustment_needed=False,
        parent_position_id=None,
        origin=None,
    )
    return PositionView(
        record=record,
        current_market_value_usd=signed_money(size_usd),
        unrealized_pnl_usd=signed_money(unrealized_pnl_usd),
        unrealized_pnl_pct=unrealized_pnl_pct,
        position_weight_pct=size_pct,
        position_age_hours=24.0,
        notional_exposure_usd=money(size_usd),
        delta_adjusted_exposure_usd=signed_money(delta_signed),
        distance_to_target_usd=None,
        distance_to_stop_usd=None,
        risk_reward_at_current=None,
    )


# ---------------------------------------------------------------------------
# RiskBudgetConsumption builder
# ---------------------------------------------------------------------------


def make_risk_budget(*, entries: list[dict[str, Any]]) -> RiskBudgetConsumption:
    """Construct a ``RiskBudgetConsumption`` from compact entry dicts.

    Each entry dict carries at minimum ``rule_id``, ``current_value``, and
    optionally ``limit_value`` (default 100), ``zone`` (default
    ``RiskZone.NORMAL``), ``unit`` (default ``"pct"``), and ``rule_label``
    (default the rule_id).
    """
    built: list[RiskBudgetEntry] = []
    for entry in entries:
        rule_id = entry["rule_id"]
        current_value = float(entry["current_value"])
        limit_value = float(entry.get("limit_value", 100.0))
        headroom = limit_value - current_value
        if limit_value > 0:
            headroom_pct = max(0.0, min(100.0, 100.0 * headroom / limit_value))
        else:
            headroom_pct = 0.0
        built.append(
            RiskBudgetEntry(
                rule_id=rule_id,
                rule_label=entry.get("rule_label", rule_id),
                current_value=current_value,
                limit_value=limit_value,
                headroom=headroom,
                headroom_pct_of_limit=headroom_pct,
                zone=entry.get("zone", RiskZone.NORMAL),
                unit=entry.get("unit", "pct"),
                cumulative_invocation_impact_value=float(
                    entry.get("cumulative_invocation_impact_value", 0.0)
                ),
            )
        )
    return RiskBudgetConsumption(entries=tuple(built))


# ---------------------------------------------------------------------------
# ActiveRiskParameterSet builder
# ---------------------------------------------------------------------------


def make_active_risk_parameters(
    *,
    regime: RegimeLabel,
    rule_values: dict[str, float],
    transition_state: RegimeTransitionState = RegimeTransitionState.STABLE,
    transition_invocations_remaining: int = 0,
    parameter_change_flag: bool = False,
    active_overlays: tuple[str, ...] = (),
) -> ActiveRiskParameterSet:
    """Construct an ``ActiveRiskParameterSet`` from a rule_id → value mapping.

    Each entry's ``base_value`` equals its ``value`` and
    ``regime_multiplier_applied`` defaults to ``1.0``; tests do not exercise
    the regime-multiplier dimension here. ``unit`` defaults to ``pct``;
    extend the mapping if a future scenario needs a different unit.
    """
    entries = tuple(
        ActiveRiskParameterEntry(
            rule_id=rule_id,
            rule_label=rule_id,
            value=value,
            unit="pct",
            regime_multiplier_applied=1.0,
            base_value=value,
        )
        for rule_id, value in rule_values.items()
    )
    return ActiveRiskParameterSet(
        regime_label=regime,
        transition_state=transition_state,
        transition_invocations_remaining=transition_invocations_remaining,
        parameter_change_flag=parameter_change_flag,
        entries=entries,
        active_overlays=active_overlays,
    )


# ---------------------------------------------------------------------------
# DrawdownState builder
# ---------------------------------------------------------------------------


def make_drawdown_state(
    *,
    intraday_pct: float,
    cumulative_pct: float,
    cumulative_tier: DrawdownTier | None = None,
    daily_zone: RiskZone = RiskZone.NORMAL,
    cumulative_zone: RiskZone = RiskZone.NORMAL,
    equity_high_water_mark_usd: float = 100_000.0,
    drawdown_duration_hours: float = 0.0,
    lifetime_max_drawdown_pct: float = 0.0,
) -> DrawdownState:
    """Construct a ``DrawdownState`` with the load-bearing drawdown levels set.

    ``intraday_pct`` populates ``intraday_drawdown_pct`` and
    ``cumulative_pct`` populates ``current_drawdown_pct``. The remaining
    fields default to non-binding values; tests override only the levers
    their scenario exercises.
    """
    return DrawdownState(
        current_drawdown_pct=cumulative_pct,
        equity_high_water_mark_usd=equity_high_water_mark_usd,
        drawdown_duration_hours=drawdown_duration_hours,
        lifetime_max_drawdown_pct=lifetime_max_drawdown_pct,
        intraday_drawdown_pct=intraday_pct,
        daily_zone=daily_zone,
        cumulative_zone=cumulative_zone,
        cumulative_tier=cumulative_tier,
        drawdown_by_source_pct={},
    )


# ---------------------------------------------------------------------------
# Library-callable Protocol stubs (for A7 cascade orchestration)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StubRuleProjection:
    """Structural stand-in for ``RuleProjectionProtocol``."""

    rule: str
    status: str
    current: float
    limit: float
    projected_after: float
    headroom_remaining: float
    unit: str
    inverse: bool = False


@dataclass(frozen=True)
class StubLibraryOutput:
    """Structural stand-in for ``LibraryOutputProtocol``."""

    per_rule: tuple[StubRuleProjection, ...]


@dataclass(frozen=True)
class StubLibraryConfig:
    """Structural stand-in for ``LibraryConfigProtocol``.

    The primitive only reads ``effective_limits`` (the rule-id key set) for
    the primary-rule-id-in-active-set check.
    """

    effective_limits: dict[str, float]


@dataclass(frozen=True)
class StubPortfolioState:
    """Structural stand-in for ``PortfolioStateSnapshotProtocol``. Opaque to the primitive."""

    label: str = "default"


@dataclass(frozen=True)
class StubMarketInputs:
    """Structural stand-in for ``MarketInputsProtocol``. Opaque to the primitive."""


@dataclass
class ScriptedLibrary:
    """Test-time stand-in for the ``evaluate_proposals`` callable.

    Returns ``outputs[i]`` on the i-th call, repeating the last entry for
    additional calls. Records every invocation so tests can assert call
    counts and proposal shapes.

    Note this fixture is *not* input-aware: identical inputs across two calls
    produce different outputs if scripted that way. For tests that need to
    *enforce* the orchestrator's projection plumbing, use
    :class:`InputAwareLibrary` instead — it dispatches based on the proposals
    tuple (specifically by the position-ids in the ``existing_position_id``
    field) and raises when the orchestrator skips a close it should have
    threaded through.
    """

    outputs: list[StubLibraryOutput]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        state: Any,
        proposals: Sequence[Any],
        config: Any,
        market: Any,
        delta_buffer_factor: float = 1.0,
    ) -> StubLibraryOutput:
        self.calls.append(
            {
                "state": state,
                "proposals": tuple(proposals),
                "config": config,
                "market": market,
                "delta_buffer_factor": delta_buffer_factor,
            }
        )
        idx = min(len(self.calls) - 1, len(self.outputs) - 1)
        return self.outputs[idx]


@dataclass
class InputAwareLibrary:
    """Input-aware ``evaluate_proposals`` stub for cascade-projection tests.

    Returns ``baseline_output`` on calls with ``proposals=()``; otherwise
    looks up an output keyed by the frozen set of ``existing_position_id``
    values in the proposals tuple. Used to enforce that the cascade
    orchestrator actually plumbs CLOSE-action ProposedDeltas through the
    library — a no-op call with ``proposals=()`` after the first close has
    been emitted will hit the baseline branch and the test will detect the
    bug.
    """

    baseline_output: StubLibraryOutput
    post_close_outputs: dict[frozenset[str], StubLibraryOutput]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(
        self,
        *,
        state: Any,
        proposals: Sequence[Any],
        config: Any,
        market: Any,
        delta_buffer_factor: float = 1.0,
    ) -> StubLibraryOutput:
        proposals_tuple = tuple(proposals)
        self.calls.append(
            {
                "state": state,
                "proposals": proposals_tuple,
                "config": config,
                "market": market,
                "delta_buffer_factor": delta_buffer_factor,
            }
        )
        if not proposals_tuple:
            return self.baseline_output
        position_ids = frozenset(p.existing_position_id for p in proposals_tuple)
        if position_ids in self.post_close_outputs:
            return self.post_close_outputs[position_ids]
        msg = (
            f"InputAwareLibrary: no scripted output for proposals "
            f"{[p.existing_position_id for p in proposals_tuple]!r}; "
            f"available keys: {sorted(map(sorted, self.post_close_outputs))!r}"
        )
        raise AssertionError(msg)

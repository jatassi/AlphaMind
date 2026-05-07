"""Forced-reduction position-selection primitives (story 04d).

Five public selectors — one per breach type — that consume a typed snapshot of
open positions plus per-breach metric inputs and return a
:class:`PositionSelectionResult` naming the position to close (or trim), the
prescribed action, and the human-readable rationale that flows into the engine
envelope's ``position_selection_rationale`` field. All selectors are pure
functions with deterministic tiebreakers, so identical inputs always produce
identical outputs (a load-bearing property for the audit trail).

Selection rules per
``docs/design/06-risk-guardrails/breach-behavior.md`` § *Position selection
logic for forced reductions*. The 95% trim target and 110% immediate-action
threshold come from :class:`BreachBehaviorConfig` — never hardcoded.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionsPositionDetails,
    StrategyPositionDetails,
)
from alphamind.portfolio_state.views.positions import PositionView
from alphamind.risk_guardrails.breach_behavior.config import BreachBehaviorConfig
from alphamind.risk_guardrails.breach_behavior.types import (
    PositionSelectionAction,
    PositionSelectionResult,
)


class PositionLiquidity(BaseModel):
    """Per-position liquidity metric used as a selection tiebreaker.

    The continuous monitor populates this from the data layer's per-symbol ADV
    (average daily volume) and the position's notional. The ratio (ADV in
    shares * current price / position notional) approximates how many days of
    typical volume the position represents — higher ratio means more liquid
    relative to size.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    adv_to_position_size_ratio: float

    @field_validator("adv_to_position_size_ratio")
    @classmethod
    def _require_non_negative(cls, v: float) -> float:
        if v < 0:
            msg = f"adv_to_position_size_ratio must be >= 0; got {v}"
            raise ValueError(msg)
        return v


class PositionRiskReward(BaseModel):
    """Per-position risk/reward ratio at current price.

    The continuous monitor computes this from the position's current price,
    its bracket's invalidation level (price-stop), and its target
    (take-profit) per ``breach-behavior.md`` § *Margin call position
    selection*.

    Definition: ratio = (distance to target) / (distance to invalidation).
    Lower means worse R/R (closer to invalidation than to target). Negative
    means the position is already past invalidation but hasn't been stopped
    yet — pathological but possible during gaps; treated as worst-of-the-worst.
    """

    model_config = ConfigDict(frozen=True)

    position_id: str
    risk_reward_ratio: float


# ---------------------------------------------------------------------------
# Helpers — shared across selectors
# ---------------------------------------------------------------------------


def _ticker_of(position: PositionView) -> str:
    """Return a human-readable ticker label for the rationale string.

    Equity positions carry a ticker directly; options/strategies surface their
    underlying or label so the rationale string remains intelligible to the PM.
    Falls through to ``position_id`` when none of the instrument-specific
    detail records is populated; this keeps callers (rationale strings, audit
    logs) from crashing on degenerate inputs.
    """
    details = position.details
    if isinstance(details, EquityPositionDetails):
        return details.ticker
    if isinstance(details, OptionsPositionDetails):
        return details.underlying_ticker
    if isinstance(details, StrategyPositionDetails):
        return details.strategy_type_label
    return position.position_id


def _build_liquidity_lookup(
    positions: tuple[PositionView, ...],
    liquidity: tuple[PositionLiquidity, ...],
) -> dict[str, float]:
    """Build a {position_id: ratio} lookup; raise if any open position is uncovered."""
    lookup: dict[str, float] = {
        liq.position_id: liq.adv_to_position_size_ratio for liq in liquidity
    }
    missing = [p.position_id for p in positions if p.position_id not in lookup]
    if missing:
        msg = f"liquidity missing entries for position_ids: {missing}"
        raise ValueError(msg)
    return lookup


# ---------------------------------------------------------------------------
# Drawdown breach selector
# ---------------------------------------------------------------------------


def select_for_drawdown_breach(
    *,
    open_positions: tuple[PositionView, ...],
    liquidity: tuple[PositionLiquidity, ...],
) -> PositionSelectionResult:
    """Select the position to close in response to a drawdown breach.

    Selection rule (``breach-behavior.md`` § *Position selection logic*):
      1. Largest unrealized loss in absolute USD (most negative ``unrealized_pnl_usd``).
      2. Tiebreaker: highest ``adv_to_position_size_ratio`` (most liquid relative to size).
      3. Final tiebreaker: lexicographically smallest ``position_id`` (deterministic floor).
      4. Action: full close — partial leaves residual risk and orphaned theses.

    Args:
        open_positions: All currently-open positions. Must be non-empty.
        liquidity: Per-position liquidity metrics; must cover every open position.

    Returns:
        A :class:`PositionSelectionResult` naming the chosen position with
        ``action=FULL_CLOSE`` and a rationale of the form
        ``"largest unrealized loss ($X.XX) in {ticker}; ..."``.

    Raises:
        ValueError: when ``open_positions`` is empty, ``liquidity`` is incomplete,
            or no position has a negative unrealized P/L.
    """
    if not open_positions:
        msg = "open_positions must be non-empty"
        raise ValueError(msg)
    liquidity_lookup = _build_liquidity_lookup(open_positions, liquidity)

    losers = [p for p in open_positions if p.unrealized_pnl_usd < 0.0]
    if not losers:
        msg = (
            "no losing positions; drawdown breach with no negative-PnL position "
            "is a structural error"
        )
        raise ValueError(msg)

    losers_sorted = sorted(
        losers,
        key=lambda p: (
            p.unrealized_pnl_usd,
            -liquidity_lookup[p.position_id],
            p.position_id,
        ),
    )
    selected = losers_sorted[0]
    selected_loss = selected.unrealized_pnl_usd
    selected_ratio = liquidity_lookup[selected.position_id]
    tied_on_loss = [p for p in losers if p.unrealized_pnl_usd == selected_loss]

    if len(tied_on_loss) == 1:
        rationale = (
            f"largest unrealized loss (${selected_loss:.2f}) in {_ticker_of(selected)}; "
            f"no tiebreaker — uniquely most-loss"
        )
    else:
        rationale = (
            f"largest unrealized loss (${selected_loss:.2f}) in {_ticker_of(selected)}; "
            f"tie-broken by liquidity (ADV/size ratio {selected_ratio:.2f})"
        )

    return PositionSelectionResult(
        position_id=selected.position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Position-level max loss selector
# ---------------------------------------------------------------------------


def select_for_position_max_loss(
    *,
    breaching_position_id: str,
    open_positions: tuple[PositionView, ...],
    loss_pct: float,
    limit_pct: float,
) -> PositionSelectionResult:
    """Select for a position-level max-loss breach.

    The breach is intrinsically tied to a single position; this selector exposes
    the per-breach-type uniform interface and constructs the canonical
    rationale string in one place rather than at every call site.

    Args:
        breaching_position_id: The ``position_id`` whose unrealized loss
            triggered the breach.
        open_positions: All currently-open positions; ``breaching_position_id``
            must be present.
        loss_pct: The position's unrealized loss as a percent of its cost basis
            (negative; e.g., ``-3.5`` for a 3.5% loss). Recorded in the rationale.
        limit_pct: The active per-position max-loss limit as a percent of cost
            basis (negative; e.g., ``-3.0`` for a -3.0% cap). Recorded in the rationale.

    Returns:
        :class:`PositionSelectionResult` with ``action=FULL_CLOSE`` and a
        rationale of the form ``"position-level max loss breach on {ticker}
        (loss: -X.X% of cost, limit: -Y.Y%)"``.

    Raises:
        ValueError: when ``breaching_position_id`` is not present in ``open_positions``.
    """
    by_id = {p.position_id: p for p in open_positions}
    selected = by_id.get(breaching_position_id)
    if selected is None:
        msg = (
            f"breaching_position_id {breaching_position_id!r} not present in open_positions; "
            f"cannot select"
        )
        raise ValueError(msg)

    rationale = (
        f"position-level max loss breach on {_ticker_of(selected)} "
        f"(loss: {loss_pct:.1f}% of cost, limit: {limit_pct:.1f}%)"
    )
    return PositionSelectionResult(
        position_id=selected.position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Total short exposure selector
# ---------------------------------------------------------------------------


def select_for_total_short_exposure_breach(
    *,
    short_positions: tuple[PositionView, ...],
    liquidity: tuple[PositionLiquidity, ...],
    total_short_limit_pct_of_portfolio: float,
    config: BreachBehaviorConfig,
) -> PositionSelectionResult:
    """Trim the largest short to cure a total-short-exposure breach.

    Caller responsibility (continuous monitor): only invoke this when total
    short exposure has crossed
    ``config.forced_reduction_total_short_immediate_threshold_pct_of_limit``
    (default 110%) of the active limit. Smaller overages defer to the PM.

    Selection rule (``breach-behavior.md`` § *Position selection logic*):
      1. Largest short by ``position_weight_pct`` (size as percent of portfolio).
      2. Tiebreaker: highest ``adv_to_position_size_ratio`` (most liquid).
      3. Final tiebreaker: lexicographically smallest ``position_id``.
      4. Action: ``PARTIAL_TRIM`` to a target that brings the aggregate short
         exposure to ``total_short_limit * forced_reduction_short_trim_target_pct_of_limit / 100``
         (default 95% of the cap).

    Args:
        short_positions: All currently-open short positions. Must be non-empty.
        liquidity: Per-position liquidity; must cover every short position.
        total_short_limit_pct_of_portfolio: The active total-short limit
            (e.g., ``30.0`` for 30% of portfolio).
        config: The :class:`BreachBehaviorConfig` — sources the 95% trim factor.

    Returns:
        :class:`PositionSelectionResult` with ``action=PARTIAL_TRIM`` and
        ``target_post_action_size_pct_of_portfolio`` set to the cure target.

    Raises:
        ValueError: when ``short_positions`` is empty, ``liquidity`` is incomplete,
            or the computed trim target is non-positive (a single-position trim
            cannot cure the breach — the caller must escalate).
    """
    if not short_positions:
        msg = "short_positions must be non-empty"
        raise ValueError(msg)
    liquidity_lookup = _build_liquidity_lookup(short_positions, liquidity)

    shorts_sorted = sorted(
        short_positions,
        key=lambda p: (
            -p.position_weight_pct,
            -liquidity_lookup[p.position_id],
            p.position_id,
        ),
    )
    selected = shorts_sorted[0]
    aggregate_short_pct = sum(p.position_weight_pct for p in short_positions)
    trim_factor = config.forced_reduction_short_trim_target_pct_of_limit / 100.0
    total_short_target_pct = total_short_limit_pct_of_portfolio * trim_factor
    other_shorts_total_pct = aggregate_short_pct - selected.position_weight_pct
    selected_target_pct = total_short_target_pct - other_shorts_total_pct
    if selected_target_pct <= 0.0:
        msg = (
            f"trim target non-positive ({selected_target_pct:.2f}%); "
            f"single-position trim cannot cure breach"
        )
        raise ValueError(msg)

    rationale = (
        f"total short exposure breach ({aggregate_short_pct:.1f}% > "
        f"{total_short_limit_pct_of_portfolio:.1f}% cap); largest short "
        f"{_ticker_of(selected)} trimmed to {selected_target_pct:.1f}% "
        f"({config.forced_reduction_short_trim_target_pct_of_limit:.0f}% of total cap "
        f"{total_short_limit_pct_of_portfolio:.1f}%)"
    )
    return PositionSelectionResult(
        position_id=selected.position_id,
        action=PositionSelectionAction.PARTIAL_TRIM,
        target_post_action_size_pct_of_portfolio=selected_target_pct,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Single short max size selector
# ---------------------------------------------------------------------------


def select_for_single_short_max_size_breach(
    *,
    breaching_position_id: str,
    open_positions: tuple[PositionView, ...],
    single_short_max_pct_of_portfolio: float,
    config: BreachBehaviorConfig,
) -> PositionSelectionResult:
    """Trim a single-short position whose size has exceeded the per-position cap.

    The breach identifies the position; this selector trims it to
    ``single_short_max_pct_of_portfolio * forced_reduction_short_trim_target_pct_of_limit / 100``
    (default 95% of the per-position cap).

    Args:
        breaching_position_id: The short whose size exceeds the per-position cap.
        open_positions: All currently-open positions; the breaching id must be present.
        single_short_max_pct_of_portfolio: The active per-position single-short
            limit (e.g., ``3.0`` for 3% of portfolio).
        config: The :class:`BreachBehaviorConfig` — sources the 95% trim factor.

    Returns:
        :class:`PositionSelectionResult` with ``action=PARTIAL_TRIM`` and
        ``target_post_action_size_pct_of_portfolio`` set to ``cap * 0.95``.

    Raises:
        ValueError: when ``breaching_position_id`` is not present in ``open_positions``.
    """
    by_id = {p.position_id: p for p in open_positions}
    selected = by_id.get(breaching_position_id)
    if selected is None:
        msg = (
            f"breaching_position_id {breaching_position_id!r} not present in open_positions; "
            f"cannot select"
        )
        raise ValueError(msg)

    trim_factor = config.forced_reduction_short_trim_target_pct_of_limit / 100.0
    target_pct = single_short_max_pct_of_portfolio * trim_factor
    rationale = (
        f"single short {_ticker_of(selected)} at {selected.position_weight_pct:.1f}% > "
        f"{single_short_max_pct_of_portfolio:.1f}% per-position cap; trimmed to "
        f"{target_pct:.2f}% ({config.forced_reduction_short_trim_target_pct_of_limit:.0f}% of cap)"
    )
    return PositionSelectionResult(
        position_id=selected.position_id,
        action=PositionSelectionAction.PARTIAL_TRIM,
        target_post_action_size_pct_of_portfolio=target_pct,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Margin call selector
# ---------------------------------------------------------------------------


def select_for_margin_call(
    *,
    open_positions: tuple[PositionView, ...],
    liquidity: tuple[PositionLiquidity, ...],
    additional_margin_required_usd: float,
    risk_reward_metric: tuple[PositionRiskReward, ...],
) -> PositionSelectionResult:
    """Close the worst risk/reward position to satisfy a margin call.

    Per ``breach-behavior.md`` § *Margin call cascade handling*:
      1. Worst risk/reward ratio at current price (closest to invalidation, farthest from target).
      2. Tiebreaker: highest ``adv_to_position_size_ratio`` (most liquid).
      3. Final tiebreaker: lexicographically smallest ``position_id``.
      4. Action: full close — partial may not satisfy the margin requirement.

    Args:
        open_positions: All currently-open positions. Must be non-empty.
        liquidity: Per-position liquidity; must cover every open position.
        additional_margin_required_usd: How much margin the broker is calling
            for. Recorded in the rationale; not used in selection.
        risk_reward_metric: Per-position R/R ratio at current price; must
            cover every open position.

    Returns:
        :class:`PositionSelectionResult` with ``action=FULL_CLOSE`` and a
        rationale of the form ``"margin call $X.XX additional margin required;
        worst risk/reward {ticker} (R/R ratio Y.YY) selected"``.

    Raises:
        ValueError: when ``open_positions`` is empty, ``liquidity`` is incomplete,
            or ``risk_reward_metric`` is incomplete.
    """
    if not open_positions:
        msg = "open_positions must be non-empty"
        raise ValueError(msg)
    liquidity_lookup = _build_liquidity_lookup(open_positions, liquidity)
    rr_lookup: dict[str, float] = {
        rr.position_id: rr.risk_reward_ratio for rr in risk_reward_metric
    }
    missing_rr = [p.position_id for p in open_positions if p.position_id not in rr_lookup]
    if missing_rr:
        msg = f"risk_reward_metric missing entries for position_ids: {missing_rr}"
        raise ValueError(msg)

    positions_sorted = sorted(
        open_positions,
        key=lambda p: (
            rr_lookup[p.position_id],
            -liquidity_lookup[p.position_id],
            p.position_id,
        ),
    )
    selected = positions_sorted[0]
    selected_rr = rr_lookup[selected.position_id]
    rationale = (
        f"margin call ${additional_margin_required_usd:,.2f} additional margin required; "
        f"worst risk/reward {_ticker_of(selected)} (R/R ratio {selected_rr:.2f}) selected"
    )
    return PositionSelectionResult(
        position_id=selected.position_id,
        action=PositionSelectionAction.FULL_CLOSE,
        target_post_action_size_pct_of_portfolio=None,
        rationale=rationale,
    )

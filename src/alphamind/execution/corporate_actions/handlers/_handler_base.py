"""Shared per-handler scaffolding for ALP-411 handlers.

The reverse-split, stock-dividend, and ticker-change handlers (and any future
handlers in stories 03b/03c/03d that follow the same skeleton) load the
position, mutate it per their per-action branch, then finalize identically:
persist, emit ``CORPORATE_ACTION_APPLIED``, cancel the bracket, write the
dedup ledger row.  Both the load and the finalize are extracted here so each
handler reduces to its action-specific mutation.

``apply_options_position_mutation`` covers the equity / options / strategy
dispatch shared by reverse splits and stock dividends — both pull
post-adjustment options state from ``AlpacaPositionLookup`` and apply equity
mutations parameterized by quantity and basis multiplicative factors.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import NamedTuple

from alphamind._kernel.ids import Symbol
from alphamind.execution.write_paths.ca_integration_ledger import (
    mark_ca_activity_processed,
)
from alphamind.portfolio_state.records.positions import (
    EquityPositionDetails,
    OptionGreeks,
    OptionsPositionDetails,
    PositionRecord,
    StrategyLeg,
    StrategyPositionDetails,
)
from alphamind.state.invocation_context.context import (
    InvocationHandle,
)
from alphamind.state.tables.positions import PositionRow
from alphamind.state.tables.positions_codec import (
    row_to_record as position_row_to_record,
)

from ..types import AlpacaPositionLookup, CorporateActionActivity
from ._shared import (
    _cancel_bracket_for_corporate_action,
    _emit_corporate_action_applied,
    _persist_position_update,
)

# ---------------------------------------------------------------------------
# Position load
# ---------------------------------------------------------------------------


async def load_position_for_ca(
    handle: InvocationHandle,
    activity: CorporateActionActivity,
) -> tuple[PositionRow, PositionRecord]:
    """Load and decode the ``PositionRecord`` referenced by *activity*.

    Raises ``ValueError`` when the ``position_id`` is missing — matches the
    existing SPLIT handler's contract.
    """
    pos_row = await handle.session.get(PositionRow, activity.position_id)
    if pos_row is None:
        msg = (
            f"CA activity {activity.alpaca_activity_id!r} references missing "
            f"position_id={activity.position_id!r}"
        )
        raise ValueError(msg)
    return pos_row, position_row_to_record(pos_row)


# ---------------------------------------------------------------------------
# Greeks staleness — see architecture.md § 4d
# ---------------------------------------------------------------------------


def _stale_greeks(prior: OptionGreeks) -> OptionGreeks:
    """Return *prior* greeks with ``refresh_failed = True``.

    ``OptionGreeks.delta``/``gamma``/``theta``/``vega`` are non-nullable
    floats; corporate actions invalidate the prior IV-derived values but
    cannot literally null them without a wide schema change.  We instead set
    the freshness flag the architecture defines for this purpose so
    downstream consumers know to re-derive at the next 4d refresh.
    """
    return dataclasses.replace(prior, refresh_failed=True)


def _project_options_from_snapshot(
    *,
    prior: OptionsPositionDetails,
    lookup: AlpacaPositionLookup,
    activity: CorporateActionActivity,
) -> OptionsPositionDetails:
    """Project Alpaca's post-adjustment snapshot onto the local options record.

    Reads ``lookup.get_position(activity.ticker)``; ``qty`` becomes the new
    contract count, ``avg_entry_price * contract_multiplier`` becomes the
    per-contract premium.  Strike and multiplier come from the snapshot when
    surfaced — Alpaca's options snapshot doesn't expose them directly today,
    so we keep the local values; future stories with a richer snapshot can
    replace this read.
    """
    snapshot = lookup.get_position(activity.ticker)
    if snapshot is None:
        msg = (
            f"AlpacaPositionLookup returned no position for {activity.ticker!r}; "
            f"options/strategy CA cannot project post-adjustment state"
        )
        raise ValueError(msg)
    # ALP-462 — ``snapshot.avg_entry_price`` is ``Price`` (Decimal); coerce the
    # float contract multiplier so the projected premium stays exact, then cast
    # the result to float for the legacy float field on OptionsPositionDetails.
    # The post-ALP-477 frozen dataclass no longer auto-coerces Decimal → float
    # the way Pydantic did, so we coerce explicitly at the codec boundary.
    projected_premium = float(snapshot.avg_entry_price * Decimal(str(prior.contract_multiplier)))
    return dataclasses.replace(
        prior,
        contract_count=snapshot.qty,
        premium_paid_per_contract=projected_premium,
        greeks=_stale_greeks(prior.greeks),
    )


def _project_strategy_from_snapshot(
    *,
    prior: StrategyPositionDetails,
    lookup: AlpacaPositionLookup,
    activity: CorporateActionActivity,
) -> StrategyPositionDetails:
    """Apply :func:`_project_options_from_snapshot` to each leg."""
    new_legs = tuple(
        StrategyLeg(
            leg_id=leg.leg_id,
            direction=leg.direction,
            options=_project_options_from_snapshot(
                prior=leg.options, lookup=lookup, activity=activity
            ),
        )
        for leg in prior.legs
    )
    return dataclasses.replace(
        prior, legs=new_legs, strategy_greeks=_stale_greeks(prior.strategy_greeks)
    )


# ---------------------------------------------------------------------------
# Multi-instrument equity-or-options-or-strategy mutation
# ---------------------------------------------------------------------------


class CAMutationResult(NamedTuple):
    """Result of applying a CA mutation to a ``PositionRecord``.

    The four scalar fields feed
    :func:`_emit_corporate_action_applied`'s audit-trail payload; ``updated``
    is the mutated record ready to persist.  For equity positions the
    quantity/basis fields are ``share_count`` / ``average_cost_basis_per_share``;
    for options and strategies they are ``contract_count`` /
    ``premium_paid_per_contract`` (first leg for strategies).
    """

    pre_qty: float
    post_qty: float
    pre_basis: float
    post_basis: float
    updated: PositionRecord


def audit_metrics(
    details: EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails,
) -> tuple[float, float]:
    """Return ``(quantity, basis)`` for the audit-trail event payload.

    For strategies the pair represents the first leg only — there is no
    single ``(qty, basis)`` for a multi-leg payload, so leg 0 is used as a
    representative sample.
    """
    if isinstance(details, EquityPositionDetails):
        return details.share_count, details.average_cost_basis_per_share
    if isinstance(details, OptionsPositionDetails):
        return details.contract_count, details.premium_paid_per_contract
    if details.legs:
        first = details.legs[0].options
        return first.contract_count, first.premium_paid_per_contract
    return 0.0, 0.0


def apply_options_position_mutation(
    position: PositionRecord,
    activity: CorporateActionActivity,
    lookup: AlpacaPositionLookup | None,
    *,
    equity_quantity_factor: float,
    equity_basis_factor: float,
) -> CAMutationResult:
    """Compute the post-mutation ``PositionRecord`` for a CA that scales equity.

    Equity branch: ``share_count *= equity_quantity_factor``;
    ``average_cost_basis_per_share *= equity_basis_factor``.

    Options / strategy branch: project from Alpaca's snapshot via *lookup*.
    *lookup* must be non-``None`` for non-equity positions or a ``ValueError``
    is raised.
    """
    details = position.details
    pre_qty, pre_basis = audit_metrics(details)

    if isinstance(details, EquityPositionDetails):
        new_details: EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails = (
            dataclasses.replace(
                details,
                share_count=pre_qty * equity_quantity_factor,
                average_cost_basis_per_share=pre_basis * equity_basis_factor,
            )
        )
    else:
        if lookup is None:
            msg = (
                f"AlpacaPositionLookup is required for non-equity CA "
                f"{activity.alpaca_activity_id!r} on position {position.position_id!r}"
            )
            raise ValueError(msg)
        if isinstance(details, OptionsPositionDetails):
            new_details = _project_options_from_snapshot(
                prior=details, lookup=lookup, activity=activity
            )
        elif isinstance(details, StrategyPositionDetails):
            new_details = _project_strategy_from_snapshot(
                prior=details, lookup=lookup, activity=activity
            )
        else:
            msg = f"Unrecognized position details type: {type(details).__name__!r}"
            raise NotImplementedError(msg)

    post_qty, post_basis = audit_metrics(new_details)
    updated = dataclasses.replace(
        position, details=new_details, corporate_action_adjustment_needed=True
    )
    return CAMutationResult(pre_qty, post_qty, pre_basis, post_basis, updated)


# ---------------------------------------------------------------------------
# Ticker-only mutation (SYMBOL_CHANGE)
# ---------------------------------------------------------------------------


def apply_ticker_only_mutation(
    position: PositionRecord,
    activity: CorporateActionActivity,
) -> CAMutationResult:
    """Update the position's ticker references without touching quantity or basis.

    Equity: ``EquityPositionDetails.ticker`` → ``activity.new_ticker``.
    Options: ``OptionsPositionDetails.underlying_ticker`` → ``activity.new_ticker``.
    Strategy: each leg's ``underlying_ticker`` → ``activity.new_ticker``.

    Greeks remain valid for symbol changes (no structural change in the
    contract spec, per the design doc).  ``corporate_action_adjustment_needed``
    is set to ``True`` so the strategist re-evaluates at the next invocation.
    """
    if activity.new_ticker is None:
        msg = (
            f"SYMBOL_CHANGE activity {activity.alpaca_activity_id!r} requires "
            f"new_ticker, but it is None"
        )
        raise ValueError(msg)
    new_ticker = Symbol(activity.new_ticker)
    details = position.details
    if isinstance(details, EquityPositionDetails):
        new_details: EquityPositionDetails | OptionsPositionDetails | StrategyPositionDetails = (
            dataclasses.replace(details, ticker=new_ticker)
        )
    elif isinstance(details, OptionsPositionDetails):
        new_details = dataclasses.replace(details, underlying_ticker=new_ticker)
    elif isinstance(details, StrategyPositionDetails):
        new_legs = tuple(
            StrategyLeg(
                leg_id=leg.leg_id,
                direction=leg.direction,
                options=dataclasses.replace(leg.options, underlying_ticker=new_ticker),
            )
            for leg in details.legs
        )
        new_details = dataclasses.replace(details, legs=new_legs)
    else:
        msg = f"Unrecognized position details type: {type(details).__name__!r}"
        raise NotImplementedError(msg)

    qty, basis = audit_metrics(details)
    updated = dataclasses.replace(
        position, details=new_details, corporate_action_adjustment_needed=True
    )
    return CAMutationResult(qty, qty, basis, basis, updated)


# ---------------------------------------------------------------------------
# Finalization: persist, emit, cancel bracket, write ledger row.
# ---------------------------------------------------------------------------


async def finalize_ca_handler(
    handle: InvocationHandle,
    *,
    pos_row: PositionRow,
    activity: CorporateActionActivity,
    result: CAMutationResult,
) -> None:
    """Persist the mutated record and emit the standard CA log + ledger triplet.

    Order matches the SPLIT handler:

    1. Project ``result.updated`` back onto ``pos_row``.
    2. Emit ``CORPORATE_ACTION_APPLIED``.
    3. Cancel the position's bracket (``BRACKET_CANCELLED_CORPORATE_ACTION``).
    4. Write the dedup ledger row.

    Cash movements (when applicable) are emitted by the calling handler
    *before* this function so the chronological log order is
    "cash → applied → bracket-cancelled → ledger".
    """
    _persist_position_update(pos_row, result.updated)
    await _emit_corporate_action_applied(
        handle,
        activity=activity,
        position=result.updated,
        pre_qty=result.pre_qty,
        post_qty=result.post_qty,
        pre_basis=result.pre_basis,
        post_basis=result.post_basis,
    )
    await _cancel_bracket_for_corporate_action(handle, result.updated.bracket_id, activity)
    await mark_ca_activity_processed(
        handle,
        activity.alpaca_activity_id,
        processing_timestamp=activity.transaction_time,
    )


__all__ = [
    "CAMutationResult",
    "apply_options_position_mutation",
    "apply_ticker_only_mutation",
    "audit_metrics",
    "finalize_ca_handler",
    "load_position_for_ca",
]

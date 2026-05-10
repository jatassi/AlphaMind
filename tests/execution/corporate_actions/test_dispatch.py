"""Tests for the corporate-actions dispatch table (ALP-409).

Verifies that:
* ``_HANDLERS`` contains exactly one entry per ``CorporateActionType`` member.
* Every non-SPLIT handler raises ``NotImplementedError`` with the documented
  message pattern when invoked.
* The public ``integrate_ca_activity`` entry point routes correctly.
"""

from __future__ import annotations

import pytest

from alphamind.portfolio_state.events.activity_log import CorporateActionType

# ---------------------------------------------------------------------------
# Dispatch table completeness
# ---------------------------------------------------------------------------


def test_handlers_dict_has_exactly_nine_entries() -> None:
    """``_HANDLERS`` has exactly one entry per ``CorporateActionType`` member."""
    from alphamind.execution.corporate_actions.dispatch import _HANDLERS

    assert set(_HANDLERS.keys()) == set(CorporateActionType)
    assert len(_HANDLERS) == len(CorporateActionType)


def test_handlers_dict_covers_all_enum_members() -> None:
    """Every ``CorporateActionType`` member appears as a key in ``_HANDLERS``."""
    from alphamind.execution.corporate_actions.dispatch import _HANDLERS

    for member in CorporateActionType:
        assert member in _HANDLERS, f"{member!r} missing from _HANDLERS"


# ---------------------------------------------------------------------------
# Non-SPLIT stubs raise NotImplementedError
# ---------------------------------------------------------------------------


_IMPLEMENTED_TYPES = {
    CorporateActionType.SPLIT,
    CorporateActionType.CASH_DIVIDEND_LONG,
    CorporateActionType.CASH_DIVIDEND_SHORT,
    CorporateActionType.CASH_MERGER,
    CorporateActionType.STOCK_MERGER,
    CorporateActionType.SPIN_OFF,
}
_STUB_TYPES = [t for t in CorporateActionType if t not in _IMPLEMENTED_TYPES]


@pytest.mark.parametrize("action_type", _STUB_TYPES, ids=[t.value for t in _STUB_TYPES])
@pytest.mark.asyncio
async def test_non_split_handler_raises_not_implemented(
    action_type: CorporateActionType,
) -> None:
    """Each non-SPLIT type raises ``NotImplementedError`` with the standard message."""
    from datetime import UTC, datetime

    from alphamind.execution.corporate_actions import integrate_ca_activity
    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    activity = CorporateActionActivity(
        alpaca_activity_id=f"ca-{action_type.value}-1",
        action_type=action_type,
        ticker="AAPL",
        new_ticker=None,
        ratio_or_amount=1.0,
        position_id="pos-1",
        signed_cash_impact_usd=0.0,
        transaction_time=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
    )

    # We need a mock InvocationHandle; use a sentinel object since the
    # stubs raise before touching the handle.
    class _FakeHandle:
        pass

    with pytest.raises(NotImplementedError) as exc_info:
        await integrate_ca_activity(
            _FakeHandle(),  # type: ignore[arg-type]
            activity,
            alpaca_position_lookup=None,
        )

    assert action_type.value in str(exc_info.value)
    assert "not yet supported by Phase 1" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Handler message format
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action_type", _STUB_TYPES, ids=[t.value for t in _STUB_TYPES])
@pytest.mark.asyncio
async def test_non_split_message_contains_action_type_name(
    action_type: CorporateActionType,
) -> None:
    """The ``NotImplementedError`` message names the action_type."""
    from datetime import UTC, datetime

    from alphamind.execution.corporate_actions.dispatch import _HANDLERS

    handler = _HANDLERS[action_type]

    class _FakeHandle:
        pass

    from alphamind.execution.corporate_actions.types import CorporateActionActivity

    activity = CorporateActionActivity(
        alpaca_activity_id="ca-test-1",
        action_type=action_type,
        ticker="TEST",
        new_ticker=None,
        ratio_or_amount=1.0,
        position_id="pos-test",
        signed_cash_impact_usd=0.0,
        transaction_time=datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC),
    )

    with pytest.raises(NotImplementedError) as exc_info:
        await handler(_FakeHandle(), activity, None)  # type: ignore[arg-type]

    assert action_type.value in str(exc_info.value)

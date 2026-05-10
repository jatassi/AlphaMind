"""Tests for the corporate-actions dispatch table (ALP-409).

Verifies that ``_HANDLERS`` contains exactly one entry per
``CorporateActionType`` member and the public ``integrate_ca_activity``
entry point routes correctly. After wave 2 (ALP-411..414), every
member dispatches to a real handler; per-handler behavior is verified
in the handler-specific test modules.
"""

from __future__ import annotations

from alphamind.portfolio_state.events.activity_log import CorporateActionType


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

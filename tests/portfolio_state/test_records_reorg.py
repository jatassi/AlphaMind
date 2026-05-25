"""Structural tests for the records/ -> records/+events/+aggregates reorg (ALP-347).

These tests pin three invariants:

1. New subpackages ``events`` and ``aggregates`` exist alongside ``records`` under
   ``alphamind.portfolio_state``, and each declares a curated ``__all__`` public API.
2. Former capital.py members (split in ALP-347, with the re-export shim deleted in
   ALP-457) live at their new canonical home.
3. No new circular imports are introduced by the reorg.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Subpackage existence + curated public API
# ---------------------------------------------------------------------------


def test_events_subpackage_importable() -> None:
    import alphamind.portfolio_state.events as events_pkg

    assert events_pkg is not None


def test_aggregates_subpackage_importable() -> None:
    import alphamind.portfolio_state.aggregates as aggregates_pkg

    assert aggregates_pkg is not None


def test_records_subpackage_declares_all() -> None:
    import alphamind.portfolio_state.records as records_pkg

    assert hasattr(records_pkg, "__all__")
    assert len(records_pkg.__all__) > 0
    # Every name in __all__ must be a real attribute of the package.
    for name in records_pkg.__all__:
        assert hasattr(records_pkg, name), f"records.__all__ lists {name!r} but it is not present"


def test_events_subpackage_declares_all() -> None:
    import alphamind.portfolio_state.events as events_pkg

    assert hasattr(events_pkg, "__all__")
    assert len(events_pkg.__all__) > 0
    for name in events_pkg.__all__:
        assert hasattr(events_pkg, name), f"events.__all__ lists {name!r} but it is not present"


def test_aggregates_subpackage_declares_all() -> None:
    import alphamind.portfolio_state.aggregates as aggregates_pkg

    assert hasattr(aggregates_pkg, "__all__")
    assert len(aggregates_pkg.__all__) > 0
    for name in aggregates_pkg.__all__:
        assert hasattr(aggregates_pkg, name), (
            f"aggregates.__all__ lists {name!r} but it is not present"
        )


# ---------------------------------------------------------------------------
# capital.py split: cash families -> records/cash.py
# ---------------------------------------------------------------------------
#
# ALP-347 split capital.py; ALP-457 deleted the re-export shim entirely.
# Identity-check fixtures below pin each former member at its canonical home.


def test_cash_ledger_canonical_home_is_records_cash() -> None:
    from alphamind.portfolio_state.records.cash import CashLedger

    assert isinstance(CashLedger, type)


def test_unsettled_proceeds_entry_canonical_home_is_records_cash() -> None:
    from alphamind.portfolio_state.records.cash import UnsettledProceedsEntry

    assert isinstance(UnsettledProceedsEntry, type)


# ---------------------------------------------------------------------------
# capital.py split: drawdown -> aggregates/drawdown.py
# ---------------------------------------------------------------------------


def test_drawdown_state_canonical_home_is_aggregates_drawdown() -> None:
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState

    assert isinstance(DrawdownState, type)


# ---------------------------------------------------------------------------
# capital.py split: risk budget -> aggregates/risk_budget.py
# ---------------------------------------------------------------------------


def test_risk_budget_entry_canonical_home_is_aggregates_risk_budget() -> None:
    from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetEntry

    assert isinstance(RiskBudgetEntry, type)


def test_risk_budget_consumption_canonical_home_is_aggregates_risk_budget() -> None:
    from alphamind.portfolio_state.aggregates.risk_budget import RiskBudgetConsumption

    assert isinstance(RiskBudgetConsumption, type)


def test_assert_unique_rule_ids_canonical_home_is_aggregates_risk_budget() -> None:
    from alphamind.portfolio_state.aggregates.risk_budget import _assert_unique_rule_ids

    # The helper must be a module-level callable used by the validators.
    assert callable(_assert_unique_rule_ids)


# ---------------------------------------------------------------------------
# capital.py split: risk parameters -> aggregates/risk_parameters.py
# ---------------------------------------------------------------------------


def test_active_risk_parameter_entry_canonical_home_is_aggregates_risk_parameters() -> None:
    from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterEntry

    assert isinstance(ActiveRiskParameterEntry, type)


def test_active_risk_parameter_set_canonical_home_is_aggregates_risk_parameters() -> None:
    from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet

    assert isinstance(ActiveRiskParameterSet, type)


# ---------------------------------------------------------------------------
# Curated public APIs cover the core typed records
# ---------------------------------------------------------------------------


def test_records_init_exposes_tier1_core_types() -> None:
    """The records/__init__.py must expose Tier 1 core entities."""
    from alphamind.portfolio_state.records import (
        BracketRecord,
        CashLedger,
        OrderRecord,
        PositionRecord,
        ThesisRecord,
    )

    # Sanity: each is a class.
    for cls in (BracketRecord, CashLedger, OrderRecord, PositionRecord, ThesisRecord):
        assert isinstance(cls, type)


def test_events_init_exposes_activity_log_types() -> None:
    """The events/__init__.py must expose the activity log entry plus its enums."""
    from alphamind.portfolio_state.events import (
        ActivityLogEntry,
        EventGroup,
        EventSource,
        EventType,
    )

    assert isinstance(ActivityLogEntry, type)
    assert isinstance(EventGroup, type)
    assert isinstance(EventType, type)
    assert isinstance(EventSource, type)


def test_aggregates_init_exposes_tier3_types() -> None:
    """The aggregates/__init__.py must expose the Tier 3 derived/aggregate types."""
    from alphamind.portfolio_state.aggregates import (
        ActiveRiskParameterSet,
        DrawdownState,
        RiskBudgetConsumption,
        ThesisQualityAggregate,
    )

    for cls in (
        ActiveRiskParameterSet,
        DrawdownState,
        RiskBudgetConsumption,
        ThesisQualityAggregate,
    ):
        assert isinstance(cls, type)


# ---------------------------------------------------------------------------
# Circular-import guard
# ---------------------------------------------------------------------------


def test_no_circular_imports_among_new_modules() -> None:
    """Importing every new module in sequence must succeed without cycles."""
    import importlib

    importlib.import_module("alphamind.portfolio_state.records")
    importlib.import_module("alphamind.portfolio_state.events")
    importlib.import_module("alphamind.portfolio_state.aggregates")
    importlib.import_module("alphamind.portfolio_state.records.cash")
    importlib.import_module("alphamind.portfolio_state.events.activity_log")
    importlib.import_module("alphamind.portfolio_state.aggregates.thesis_quality")
    importlib.import_module("alphamind.portfolio_state.aggregates.drawdown")
    importlib.import_module("alphamind.portfolio_state.aggregates.risk_budget")
    importlib.import_module("alphamind.portfolio_state.aggregates.risk_parameters")

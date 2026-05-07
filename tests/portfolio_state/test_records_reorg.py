"""Identity tests for the records/ -> records/+events/+aggregates reorg (ALP-347).

These tests pin three invariants:

1. New subpackages ``events`` and ``aggregates`` exist alongside ``records`` under
   ``alphamind.portfolio_state``, and each declares a curated ``__all__`` public API.
2. Symbols that moved (``ActivityLogEntry``, ``ThesisQualityAggregate``, the
   capital.py split families) live at their new canonical home AND remain
   importable from their former path — and both imports return the *same* class
   object (re-export via Python's import machinery, not duplication).
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
# Activity log moved to events/
# ---------------------------------------------------------------------------


def test_activity_log_canonical_home_is_events() -> None:
    """``ActivityLogEntry`` lives at events/activity_log.py; the records/ path
    is a backward-compat shim that re-exports the same class object."""
    from alphamind.portfolio_state.events.activity_log import (
        ActivityLogEntry as FromEvents,
    )
    from alphamind.portfolio_state.records.activity_log import (
        ActivityLogEntry as FromRecords,
    )

    assert FromEvents is FromRecords


def test_event_group_canonical_home_is_events() -> None:
    from alphamind.portfolio_state.events.activity_log import EventGroup as FromEvents
    from alphamind.portfolio_state.records.activity_log import EventGroup as FromRecords

    assert FromEvents is FromRecords


def test_event_type_canonical_home_is_events() -> None:
    from alphamind.portfolio_state.events.activity_log import EventType as FromEvents
    from alphamind.portfolio_state.records.activity_log import EventType as FromRecords

    assert FromEvents is FromRecords


def test_event_source_canonical_home_is_events() -> None:
    from alphamind.portfolio_state.events.activity_log import EventSource as FromEvents
    from alphamind.portfolio_state.records.activity_log import EventSource as FromRecords

    assert FromEvents is FromRecords


def test_distillation_config_change_detail_canonical_home_is_events() -> None:
    """Pick a representative detail-payload class to prove every detail class
    re-exports from the events/activity_log shim."""
    from alphamind.portfolio_state.events.activity_log import (
        DistillationConfigChangeDetail as FromEvents,
    )
    from alphamind.portfolio_state.records.activity_log import (
        DistillationConfigChangeDetail as FromRecords,
    )

    assert FromEvents is FromRecords


# ---------------------------------------------------------------------------
# Thesis quality moved to aggregates/
# ---------------------------------------------------------------------------


def test_thesis_quality_aggregate_canonical_home_is_aggregates() -> None:
    from alphamind.portfolio_state.aggregates.thesis_quality import (
        ThesisQualityAggregate as FromAggregates,
    )
    from alphamind.portfolio_state.records.thesis_quality import (
        ThesisQualityAggregate as FromRecords,
    )

    assert FromAggregates is FromRecords


def test_trailing_window_canonical_home_is_aggregates() -> None:
    from alphamind.portfolio_state.aggregates.thesis_quality import TrailingWindow as FromAggregates
    from alphamind.portfolio_state.records.thesis_quality import TrailingWindow as FromRecords

    assert FromAggregates is FromRecords


# ---------------------------------------------------------------------------
# capital.py split: cash families -> records/cash.py
# ---------------------------------------------------------------------------


def test_cash_ledger_canonical_home_is_records_cash() -> None:
    from alphamind.portfolio_state.records.capital import CashLedger as FromCapital
    from alphamind.portfolio_state.records.cash import CashLedger as FromCash

    assert FromCapital is FromCash


def test_unsettled_proceeds_entry_canonical_home_is_records_cash() -> None:
    from alphamind.portfolio_state.records.capital import (
        UnsettledProceedsEntry as FromCapital,
    )
    from alphamind.portfolio_state.records.cash import UnsettledProceedsEntry as FromCash

    assert FromCapital is FromCash


# ---------------------------------------------------------------------------
# capital.py split: drawdown -> aggregates/drawdown.py
# ---------------------------------------------------------------------------


def test_drawdown_state_canonical_home_is_aggregates_drawdown() -> None:
    from alphamind.portfolio_state.aggregates.drawdown import DrawdownState as FromAggregates
    from alphamind.portfolio_state.records.capital import DrawdownState as FromCapital

    assert FromAggregates is FromCapital


# ---------------------------------------------------------------------------
# capital.py split: risk budget -> aggregates/risk_budget.py
# ---------------------------------------------------------------------------


def test_risk_budget_entry_canonical_home_is_aggregates_risk_budget() -> None:
    from alphamind.portfolio_state.aggregates.risk_budget import (
        RiskBudgetEntry as FromAggregates,
    )
    from alphamind.portfolio_state.records.capital import RiskBudgetEntry as FromCapital

    assert FromAggregates is FromCapital


def test_risk_budget_consumption_canonical_home_is_aggregates_risk_budget() -> None:
    from alphamind.portfolio_state.aggregates.risk_budget import (
        RiskBudgetConsumption as FromAggregates,
    )
    from alphamind.portfolio_state.records.capital import RiskBudgetConsumption as FromCapital

    assert FromAggregates is FromCapital


def test_assert_unique_rule_ids_canonical_home_is_aggregates_risk_budget() -> None:
    from alphamind.portfolio_state.aggregates.risk_budget import _assert_unique_rule_ids

    # The helper must be a module-level callable used by the validators.
    assert callable(_assert_unique_rule_ids)


# ---------------------------------------------------------------------------
# capital.py split: risk parameters -> aggregates/risk_parameters.py
# ---------------------------------------------------------------------------


def test_active_risk_parameter_entry_canonical_home_is_aggregates_risk_parameters() -> None:
    from alphamind.portfolio_state.aggregates.risk_parameters import (
        ActiveRiskParameterEntry as FromAggregates,
    )
    from alphamind.portfolio_state.records.capital import (
        ActiveRiskParameterEntry as FromCapital,
    )

    assert FromAggregates is FromCapital


def test_active_risk_parameter_set_canonical_home_is_aggregates_risk_parameters() -> None:
    from alphamind.portfolio_state.aggregates.risk_parameters import (
        ActiveRiskParameterSet as FromAggregates,
    )
    from alphamind.portfolio_state.records.capital import ActiveRiskParameterSet as FromCapital

    assert FromAggregates is FromCapital


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
# Capital.py shim still re-exports the four risk-guardrail enums
# ---------------------------------------------------------------------------


def test_capital_shim_still_reexports_regime_label() -> None:
    """ALP-344's invariant must still hold post-reorg: capital.py re-exports
    the four risk-guardrail enums for backward compat."""
    from alphamind.portfolio_state.records.capital import RegimeLabel as FromCapital
    from alphamind.risk_guardrails.regime_adaptation.types import RegimeLabel as FromCanonical

    assert FromCapital is FromCanonical


def test_capital_shim_still_reexports_risk_zone() -> None:
    from alphamind.portfolio_state.records.capital import RiskZone as FromCapital
    from alphamind.risk_guardrails.guardrail_evaluation.types import RiskZone as FromCanonical

    assert FromCapital is FromCanonical


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
    # Old paths must still resolve.
    importlib.import_module("alphamind.portfolio_state.records.activity_log")
    importlib.import_module("alphamind.portfolio_state.records.thesis_quality")
    importlib.import_module("alphamind.portfolio_state.records.capital")

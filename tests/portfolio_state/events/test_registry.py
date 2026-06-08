"""Per-submodule registry tests (ALP-463).

Each per-event-group submodule under ``portfolio_state/events/`` exposes a
``_REGISTRY`` list of ``(EventType, detail-class, EventGroup)`` triples that
covers exactly the event types belonging to that group. The aggregated
dispatch dicts in ``events/__init__.py`` are derived from the union of
these registries.
"""

from __future__ import annotations

from alphamind.portfolio_state.events import (
    EVENT_TYPE_TO_DETAIL_CLASS,
    EVENT_TYPE_TO_GROUP,
    EventGroup,
    EventType,
)


class TestPositionLifecycleRegistry:
    def test_registry_covers_position_lifecycle_event_types(self) -> None:
        from alphamind.portfolio_state.events import position_lifecycle

        keys = {entry[0] for entry in position_lifecycle._REGISTRY}
        expected = {
            EventType.POSITION_OPENED,
            EventType.POSITION_CLOSED,
            EventType.POSITION_ADDED,
            EventType.POSITION_REDUCED,
        }
        assert keys == expected

    def test_registry_groups_are_position_lifecycle(self) -> None:
        from alphamind.portfolio_state.events import position_lifecycle

        for _, _, group in position_lifecycle._REGISTRY:
            assert group is EventGroup.POSITION_LIFECYCLE


class TestOrderLifecycleRegistry:
    def test_registry_covers_order_lifecycle_event_types(self) -> None:
        from alphamind.portfolio_state.events import order_lifecycle

        keys = {entry[0] for entry in order_lifecycle._REGISTRY}
        expected = {
            EventType.ORDER_SUBMITTED,
            EventType.ORDER_FILLED,
            EventType.ORDER_PARTIALLY_FILLED,
            EventType.ORDER_CANCELLED,
            EventType.ORDER_EXPIRED,
            EventType.ORDER_REJECTED,
            EventType.ORDER_MODIFIED,
        }
        assert keys == expected


class TestBracketRegistry:
    def test_registry_covers_bracket_event_types(self) -> None:
        from alphamind.portfolio_state.events import bracket

        keys = {entry[0] for entry in bracket._REGISTRY}
        expected = {
            EventType.BRACKET_ACTIVATED,
            EventType.BRACKET_COMPLETED,
            EventType.BRACKET_DISSOLVED,
            EventType.BRACKET_MODIFIED,
            EventType.BRACKET_INCOMPLETE_WARNING,
            EventType.BRACKET_CANCELLED_CORPORATE_ACTION,
        }
        assert keys == expected


class TestThesisRegistry:
    def test_registry_covers_thesis_event_types(self) -> None:
        from alphamind.portfolio_state.events import thesis

        keys = {entry[0] for entry in thesis._REGISTRY}
        expected = {
            EventType.THESIS_CREATED,
            EventType.THESIS_COMPONENT_ADDED,
            EventType.THESIS_COMPONENT_UPDATED,
            EventType.THESIS_RESOLVED,
            EventType.THESIS_STATUS_CHANGED,
        }
        assert keys == expected


class TestCashMarginRegistry:
    def test_registry_covers_cash_margin_event_types(self) -> None:
        from alphamind.portfolio_state.events import cash_margin

        keys = {entry[0] for entry in cash_margin._REGISTRY}
        expected = {
            EventType.CASH_DEBITED,
            EventType.CASH_CREDITED,
            EventType.CAPITAL_RESERVED,
            EventType.CAPITAL_RELEASED,
            EventType.MARGIN_CALL,
            EventType.MARGIN_CALL_RESOLVED,
            EventType.MARGIN_LIQUIDATION,
            EventType.BORROW_COST_ACCRUED,
        }
        assert keys == expected


class TestRiskGuardrailRegistry:
    def test_registry_covers_risk_guardrail_event_types(self) -> None:
        from alphamind.portfolio_state.events import risk_guardrail

        keys = {entry[0] for entry in risk_guardrail._REGISTRY}
        expected = {
            EventType.GUARDRAIL_REJECTION,
            EventType.RISK_LIMIT_APPROACHED,
            EventType.RISK_PARAMETER_CHANGED,
            EventType.EMERGENCY_INVOCATION_REQUESTED,
            EventType.HALT_ACTIVATED,
            EventType.HALT_LIFTED,
            EventType.GREEKS_REFRESH_FAILED,
        }
        assert keys == expected


class TestPMDecisionRegistry:
    def test_registry_covers_pm_decision_event_types(self) -> None:
        from alphamind.portfolio_state.events import pm_decision

        keys = {entry[0] for entry in pm_decision._REGISTRY}
        expected = {
            EventType.PM_DECISION,
            EventType.COMMAND_ABANDONED,
            EventType.ENVELOPE_PARSE_FAILED,
            EventType.ENVELOPE_REJECTED,
        }
        assert keys == expected


class TestCorporateActionRegistry:
    def test_registry_covers_corporate_action_event_types(self) -> None:
        from alphamind.portfolio_state.events import corporate_action

        keys = {entry[0] for entry in corporate_action._REGISTRY}
        assert keys == {EventType.CORPORATE_ACTION_APPLIED}


class TestReconciliationRegistry:
    def test_registry_covers_reconciliation_event_types(self) -> None:
        from alphamind.portfolio_state.events import reconciliation

        keys = {entry[0] for entry in reconciliation._REGISTRY}
        assert keys == {
            EventType.RECONCILIATION_ALERT,
            EventType.RECONCILIATION_CORRECTION,
        }


class TestConfigurationRegistry:
    def test_registry_covers_configuration_event_types(self) -> None:
        from alphamind.portfolio_state.events import configuration

        keys = {entry[0] for entry in configuration._REGISTRY}
        assert keys == {EventType.DISTILLATION_CONFIG_CHANGE, EventType.PROFILE_SWITCHED}


class TestAggregatedDispatch:
    """The dicts re-exported by ``events/__init__`` cover every EventType."""

    def test_event_type_to_detail_class_covers_all_event_types(self) -> None:
        assert set(EVENT_TYPE_TO_DETAIL_CLASS) == set(EventType)

    def test_event_type_to_group_covers_all_event_types(self) -> None:
        assert set(EVENT_TYPE_TO_GROUP) == set(EventType)

    def test_dispatch_is_derived_not_hardcoded(self) -> None:
        """The dispatch dicts are derived from per-submodule registries."""
        from alphamind.portfolio_state.events import (
            bracket,
            cash_margin,
            configuration,
            corporate_action,
            order_lifecycle,
            pm_decision,
            position_lifecycle,
            reconciliation,
            risk_guardrail,
            thesis,
        )

        all_registries = [
            *position_lifecycle._REGISTRY,
            *order_lifecycle._REGISTRY,
            *bracket._REGISTRY,
            *thesis._REGISTRY,
            *cash_margin._REGISTRY,
            *risk_guardrail._REGISTRY,
            *pm_decision._REGISTRY,
            *corporate_action._REGISTRY,
            *reconciliation._REGISTRY,
            *configuration._REGISTRY,
        ]
        expected_detail = {et: cls for et, cls, _ in all_registries}
        expected_group = {et: grp for et, _, grp in all_registries}

        assert expected_detail == EVENT_TYPE_TO_DETAIL_CLASS
        assert expected_group == EVENT_TYPE_TO_GROUP

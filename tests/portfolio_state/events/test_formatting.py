"""Tests for the shared activity-log detail summariser (ALP-552).

The same helper feeds both the strategist and the PM input bundles' intra-
invocation log surfaces. Each test pins down the human-readable phrasing
for one detail class so adding a new event type can't silently regress to
bare-class-name output.
"""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.portfolio_state.events.bracket import BracketModifiedDetail
from alphamind.portfolio_state.events.configuration import (
    DistillationConfigChange,
    DistillationConfigChangeDetail,
)
from alphamind.portfolio_state.events.formatting import summarize_activity_detail
from alphamind.portfolio_state.events.order_lifecycle import (
    OrderModifiedDetail,
    OrderRejectedDetail,
)
from alphamind.portfolio_state.events.reconciliation import ReconciliationAlertDetail
from alphamind.portfolio_state.events.risk_guardrail import (
    EmergencyInvocationRequestedDetail,
    GreeksRefreshFailedDetail,
    HaltActivatedDetail,
    HaltLiftedDetail,
)
from alphamind.portfolio_state.events.thesis import ThesisStatusChangedDetail
from alphamind.portfolio_state.events.types import (
    BracketModificationSource,
    OrderRejectionSource,
)

_HASH_64 = "a" * 64
_OTHER_HASH_64 = "b" * 64
_TIMESTAMP = datetime(2026, 5, 18, 11, 11, 40, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Bespoke phrasings — one test per detail class with structured rendering.
# ---------------------------------------------------------------------------


class TestFieldChangedTriplet:
    def test_bracket_modified_renders_field_old_new_and_rationale(self) -> None:
        detail = BracketModifiedDetail(
            source=BracketModificationSource.PM,
            field_changed="target_price",
            old_value="185.00",
            new_value="189.00",
            rationale="PM raised target on momentum",
        )
        assert (
            summarize_activity_detail(detail)
            == "target_price: 185.00 → 189.00 (PM raised target on momentum)"
        )

    def test_bracket_modified_without_rationale_drops_parenthetical(self) -> None:
        detail = BracketModifiedDetail(
            source=BracketModificationSource.FILL_ANCHOR_RECALCULATION,
            field_changed="anchor_price",
            old_value="100.00",
            new_value="101.50",
            rationale=None,
        )
        assert summarize_activity_detail(detail) == "anchor_price: 100.00 → 101.50"

    def test_order_modified_renders_field_old_new(self) -> None:
        detail = OrderModifiedDetail(
            field_changed="limit_price",
            old_value="100.00",
            new_value="101.00",
            pm_rationale="bumped to top of book",
        )
        # OrderModifiedDetail carries ``pm_rationale``, not ``rationale``; the
        # shared formatter still surfaces it instead of dropping silently.
        assert (
            summarize_activity_detail(detail)
            == "limit_price: 100.00 → 101.00 (bumped to top of book)"
        )

    def test_thesis_status_changed_renders_old_new(self) -> None:
        detail = ThesisStatusChangedDetail(old_status="ACTIVE", new_status="VALIDATED")
        assert summarize_activity_detail(detail) == "status: ACTIVE → VALIDATED"


class TestRiskGuardrailDetails:
    def test_halt_activated_renders_halt_type_drawdown_and_limit(self) -> None:
        detail = HaltActivatedDetail(
            halt_type="daily_drawdown",
            current_drawdown_pct=0.045,
            limit_pct=0.04,
            detected_at=_TIMESTAMP,
        )
        out = summarize_activity_detail(detail)
        assert out == "daily_drawdown halt activated at drawdown=4.50% limit=4.00%"

    def test_halt_lifted_renders_halt_type_and_drawdown(self) -> None:
        detail = HaltLiftedDetail(
            halt_type="cumulative_drawdown_tier3",
            current_drawdown_pct=0.08,
            lifted_at=_TIMESTAMP,
        )
        assert (
            summarize_activity_detail(detail)
            == "cumulative_drawdown_tier3 halt lifted at drawdown=8.00%"
        )

    def test_greeks_refresh_failed_renders_symbol_and_reason(self) -> None:
        detail = GreeksRefreshFailedDetail(
            underlying_ticker="AAPL",
            occ_symbol="O:AAPL260619C00200000",
            failure_reason="iv_fetch_no_row",
            prior_as_of=_TIMESTAMP,
        )
        assert (
            summarize_activity_detail(detail)
            == "greeks refresh failed: O:AAPL260619C00200000 (iv_fetch_no_row)"
        )

    def test_emergency_invocation_requested_renders_trigger(self) -> None:
        detail = EmergencyInvocationRequestedDetail(
            trigger_type="regime_jump",
            trigger_reason="NORMAL → CRISIS",
            cooldown_remaining_seconds=0,
        )
        assert (
            summarize_activity_detail(detail)
            == "emergency invocation requested: regime_jump — NORMAL → CRISIS"
        )


class TestReconciliationAlertDetail:
    def test_position_share_count_mismatch_surfaces_domain_field_and_values(self) -> None:
        detail = ReconciliationAlertDetail(
            domain="position",
            field_name="share_count",
            local_value=100.0,
            alpaca_value=99.5,
            delta_description="AAPL: local share_count=100.0 vs Alpaca qty=99.5",
        )
        out = summarize_activity_detail(detail)
        # ALP-552 acceptance criterion: the entry surfaces the mismatched
        # source(s) (local vs alpaca) and the magnitude. The entry-level
        # position_id slot carries the position pointer in the outer
        # renderer, so it doesn't need re-stating here.
        assert "ReconciliationAlertDetail" not in out
        assert "position" in out
        assert "share_count" in out
        assert "100.0" in out
        assert "99.5" in out
        assert "local share_count=100.0 vs Alpaca qty=99.5" in out

    def test_cash_mismatch_surfaces_domain_and_delta_description(self) -> None:
        detail = ReconciliationAlertDetail(
            domain="cash",
            field_name="current_cash_usd",
            local_value=50_000.0,
            alpaca_value=49_750.0,
            delta_description="cash ledger 50000.0 vs Alpaca 49750.0",
        )
        out = summarize_activity_detail(detail)
        assert "ReconciliationAlertDetail" not in out
        assert "cash" in out
        assert "current_cash_usd" in out
        assert "cash ledger 50000.0 vs Alpaca 49750.0" in out


class TestDistillationConfigChangeDetail:
    def test_single_change_surfaces_key_path_and_old_new(self) -> None:
        detail = DistillationConfigChangeDetail(
            prior_hash=_OTHER_HASH_64,
            new_hash=_HASH_64,
            changes=(
                DistillationConfigChange(
                    key_path="anomaly_detection.volume_anomaly_sigma",
                    old_value=2.0,
                    new_value=2.5,
                ),
            ),
            git_sha="abc1234",
        )
        out = summarize_activity_detail(detail)
        assert "DistillationConfigChangeDetail" not in out
        assert "anomaly_detection.volume_anomaly_sigma" in out
        assert "2.0" in out
        assert "2.5" in out

    def test_multiple_changes_are_joined(self) -> None:
        detail = DistillationConfigChangeDetail(
            prior_hash=_OTHER_HASH_64,
            new_hash=_HASH_64,
            changes=(
                DistillationConfigChange(key_path="a.first", old_value=1, new_value=2),
                DistillationConfigChange(key_path="b.second", old_value="x", new_value="y"),
            ),
            git_sha="abc1234",
        )
        out = summarize_activity_detail(detail)
        assert "DistillationConfigChangeDetail" not in out
        assert "a.first: 1 → 2" in out
        assert "b.second: x → y" in out

    def test_empty_changes_falls_back_to_hash_summary(self) -> None:
        detail = DistillationConfigChangeDetail(
            prior_hash=None,
            new_hash=_HASH_64,
            changes=(),
            git_sha="abc1234",
        )
        out = summarize_activity_detail(detail)
        # Empty-changes case is rare (filter elides identical reloads upstream)
        # but must still avoid the bare class name and convey the reload.
        assert "DistillationConfigChangeDetail" not in out
        assert "config reloaded" in out


# ---------------------------------------------------------------------------
# Generic fallback — any dataclass without bespoke handling renders fields.
# ---------------------------------------------------------------------------


class TestGenericFallback:
    def test_order_rejected_renders_fields_not_class_name(self) -> None:
        # OrderRejectedDetail has no bespoke branch — it should still render
        # its fields rather than just "OrderRejectedDetail".
        detail = OrderRejectedDetail(
            rejection_reason="insufficient_buying_power",
            rejection_source=OrderRejectionSource.BROKER,
        )
        out = summarize_activity_detail(detail)
        assert out != "OrderRejectedDetail"
        assert "rejection_reason" in out
        assert "insufficient_buying_power" in out

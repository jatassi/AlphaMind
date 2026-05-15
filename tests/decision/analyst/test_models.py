"""Tests for AnalystOutput data model — ALP-293.

Exercises every conditional invariant declared in
``docs/design/04-decision-layer/analyst-output-schema.md`` via direct
construction (not parser round-trip) plus a round-trip of the analyst.md
``<example_output>`` payload.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from alphamind._kernel.ids import (
    InvocationId,
    Symbol,
)
from alphamind._kernel.money import money, price
from alphamind.decision.analyst.models import (
    AnalystOutput,
    EntryOrder,
    EntryWindow,
    EventCondition,
    GuardrailValidationResult,
    InstrumentEquity,
    InstrumentOption,
    InstrumentStrategy,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    StrategyLeg,
    Target,
    TimeCondition,
    WatchlistEntry,
)
from alphamind.risk_guardrails.guardrail_evaluation import Greeks, RuleProjection, Status

# ---------------------------------------------------------------------------
# The analyst.md <example_output> normal-mode recommendation, expressed as a
# JSON-shaped dict suitable for round-trip via ``AnalystOutput.model_validate``.
# Mirrors prompts/decision/analyst.md lines 124-206 verbatim.
# ---------------------------------------------------------------------------


_EXAMPLE_OUTPUT_PAYLOAD: dict[str, Any] = {
    "invocation_id": "inv-2026-04-23T14-30Z",
    "timestamp": "2026-04-23T14:31:22Z",
    "mode": "normal",
    "recommendations": [
        {
            "recommendation_id": "REC-1",
            "instrument": {
                "asset_type": "equity",
                "ticker": "NVDA",
                "direction": "long",
            },
            "underlying": "NVDA",
            "sector": "semis",
            "conviction_level": 4,
            "entry_order": {"type": "limit", "limit_price": 842.50},
            "position_size": {
                "quantity": 4,
                "dollar_value": 3370.00,
                "pct_of_portfolio": 3.37,
                "delta_adjusted_exposure": 3370.00,
            },
            "target": {
                "target_type": "absolute_price",
                "price": 890.00,
                "dollar_pl_target": 190.00,
            },
            "invalidation_legs": [
                {
                    "leg_id": "INV-1",
                    "type": "price",
                    "is_hard": True,
                    "condition": {
                        "underlying_trigger": "NVDA",
                        "comparator": "<=",
                        "trigger_price": 820.00,
                    },
                    "order_parameters": {"order_type": "market"},
                },
                {
                    "leg_id": "INV-2",
                    "type": "event",
                    "is_hard": False,
                    "condition": {
                        "event_description": (
                            "MSFT or GOOGL cuts FY capex guidance on their earnings "
                            "call before NVDA reports"
                        )
                    },
                },
            ],
            "entry_window": {
                "deadline": "2026-04-23T19:55:00Z",
                "decay_type": "binary",
                "rationale": (
                    "NVDA reports after close 2026-04-23. Post-close the setup is "
                    "resolved; the edge is strictly pre-print positioning."
                ),
            },
            "time_expectation_hours": 18,
            "guardrail_validation_result": {
                "overall": "PASS",
                "per_rule": [
                    {
                        "rule": "per_position_max_size",
                        "status": "PASS",
                        "current": 0.0,
                        "limit": 5.0,
                        "projected_after": 3.37,
                        "headroom_remaining": 1.63,
                        "unit": "% of portfolio",
                    },
                    {
                        "rule": "sector_concentration",
                        "status": "PASS",
                        "current": 12.8,
                        "limit": 25.0,
                        "projected_after": 16.17,
                        "headroom_remaining": 8.83,
                        "unit": "% of portfolio (delta-adjusted)",
                    },
                    {
                        "rule": "net_long_exposure",
                        "status": "PASS",
                        "current": 38.2,
                        "limit": 60.0,
                        "projected_after": 41.57,
                        "headroom_remaining": 18.43,
                        "unit": "% of portfolio (delta-adjusted)",
                    },
                    {
                        "rule": "gross_exposure",
                        "status": "PASS",
                        "current": 54.0,
                        "limit": 120.0,
                        "projected_after": 57.37,
                        "headroom_remaining": 62.63,
                        "unit": "% of portfolio (delta-adjusted)",
                    },
                ],
                "delta_adjusted_exposure": 3370.00,
                "cumulative_impact_note": "Proposal #1 of 1 in this invocation.",
                "checked_at": "2026-04-23T14:31:10Z",
            },
            "thesis_narrative": (
                "Hyperscaler capex signals in [SA-TECH-2] align with the supply-chain "
                "read in [SA-TECH-4]."
            ),
            "target_rationale": (
                "$890 is the first level where prior post-earnings reactions have stalled."
            ),
            "invalidation_rationale": [
                {
                    "leg_id": "INV-1",
                    "rationale": (
                        "Break of $820 before the print would imply flow has already "
                        "contradicted the capex-driven demand thesis."
                    ),
                },
                {
                    "leg_id": "INV-2",
                    "rationale": ("The supply-chain leg depends on hyperscaler capex holding."),
                },
            ],
            "position_size_rationale": (
                "Conviction 4 maps to the 2-4% advisory band. Sized at 3.37%."
            ),
            "entry_window_rationale": ("Binary decay: the trade's edge is entirely pre-print."),
            "counterarguments_acknowledged": ("Implied-move pricing is in line with history."),
        }
    ],
}


# ---------------------------------------------------------------------------
# Helper builders (used by invariant tests)
# ---------------------------------------------------------------------------


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _make_guardrail_result(**overrides: Any) -> GuardrailValidationResult:
    defaults: dict[str, Any] = {
        "overall": "PASS",
        "per_rule": (
            RuleProjection(
                rule="per_position_max_size",
                status=Status.PASS,
                current=0.0,
                limit=5.0,
                projected_after=3.0,
                headroom_remaining=2.0,
                unit="% of portfolio",
            ),
        ),
        "delta_adjusted_exposure": 3000.0,
        "greeks": None,
        "cumulative_impact_note": "Proposal #1 of 1.",
        "checked_at": _ts("2026-04-23T14:31:10Z"),
    }
    return GuardrailValidationResult(**(defaults | overrides))


def _make_recommendation(**overrides: Any) -> Recommendation:
    defaults: dict[str, Any] = {
        "recommendation_id": "REC-1",
        "instrument": InstrumentEquity(
            asset_type="equity", ticker=Symbol("NVDA"), direction="long"
        ),
        "underlying": "NVDA",
        "sector": "semis",
        "conviction_level": 4,
        "entry_order": EntryOrder(type="limit", limit_price=price(842.50)),
        "position_size": PositionSize(
            quantity=4, dollar_value=money(3370.0), pct_of_portfolio=3.37
        ),
        "target": Target(
            target_type="absolute_price", price=price(890.0), dollar_pl_target=money(190.0)
        ),
        "invalidation_legs": (
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol("NVDA"), comparator="<=", trigger_price=price(820.0)
                ),
                order_parameters=OrderParameters(order_type="market"),
            ),
        ),
        "time_expectation_hours": 18.0,
        "guardrail_validation_result": _make_guardrail_result(),
        "thesis_narrative": "Test thesis.",
        "target_rationale": "Test target rationale.",
        "invalidation_rationale": (
            InvalidationRationale(leg_id="INV-1", rationale="Test inv rationale."),
        ),
        "position_size_rationale": "Test sizing rationale.",
        "counterarguments_acknowledged": "Test counters.",
    }
    return Recommendation(**(defaults | overrides))


def _make_output(**overrides: Any) -> AnalystOutput:
    defaults: dict[str, Any] = {
        "invocation_id": "inv-001",
        "timestamp": _ts("2026-04-23T14:31:22Z"),
        "mode": "normal",
        "recommendations": (_make_recommendation(),),
    }
    return AnalystOutput(**(defaults | overrides))


# ---------------------------------------------------------------------------
# Tracer: round-trip the analyst.md <example_output>
# ---------------------------------------------------------------------------


class TestExampleOutputRoundTrip:
    def test_example_output_round_trips_via_model_validate(self) -> None:
        """The analyst.md <example_output> coerces and serializes back without loss."""
        output = AnalystOutput.model_validate(_EXAMPLE_OUTPUT_PAYLOAD)
        assert output.mode == "normal"
        assert output.recommendations is not None
        assert len(output.recommendations) == 1
        rec = output.recommendations[0]
        assert rec.recommendation_id == "REC-1"
        assert isinstance(rec.instrument, InstrumentEquity)
        assert rec.instrument.ticker == "NVDA"
        assert rec.sector == "semis"
        assert rec.conviction_level == 4
        assert rec.entry_window is not None
        assert rec.entry_window.decay_type == "binary"
        assert rec.guardrail_validation_result.overall == "PASS"
        assert len(rec.guardrail_validation_result.per_rule) == 4


# ---------------------------------------------------------------------------
# 1. Mode-conditional invariant
# ---------------------------------------------------------------------------


class TestModeInvariant:
    def test_normal_requires_recommendations(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)recommendations"):
            AnalystOutput(
                invocation_id=InvocationId("inv-001"),
                timestamp=_ts("2026-04-23T14:31:22Z"),
                mode="normal",
                recommendations=None,
                watchlist=None,
            )

    def test_normal_forbids_watchlist(self) -> None:
        watch = WatchlistEntry(
            ticker=Symbol("NVDA"),
            sector="semis",
            thesis_summary="Watching for confirmation.",
            estimated_conviction=3,
        )
        with pytest.raises((ValueError, TypeError), match=r"(?i)watchlist"):
            AnalystOutput(
                invocation_id=InvocationId("inv-001"),
                timestamp=_ts("2026-04-23T14:31:22Z"),
                mode="normal",
                recommendations=(_make_recommendation(),),
                watchlist=(watch,),
            )

    def test_watchlist_requires_watchlist_field(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)watchlist"):
            AnalystOutput(
                invocation_id=InvocationId("inv-001"),
                timestamp=_ts("2026-04-23T14:31:22Z"),
                mode="watchlist",
                recommendations=None,
                watchlist=None,
            )

    def test_watchlist_forbids_recommendations(self) -> None:
        watch = WatchlistEntry(
            ticker=Symbol("NVDA"),
            sector="semis",
            thesis_summary="Watching for confirmation.",
            estimated_conviction=3,
        )
        with pytest.raises((ValueError, TypeError), match=r"(?i)recommendations"):
            AnalystOutput(
                invocation_id=InvocationId("inv-001"),
                timestamp=_ts("2026-04-23T14:31:22Z"),
                mode="watchlist",
                recommendations=(_make_recommendation(),),
                watchlist=(watch,),
            )

    def test_normal_with_empty_recommendations_allowed(self) -> None:
        """Quiet-day case: mode=normal, recommendations=()."""
        output = AnalystOutput(
            invocation_id=InvocationId("inv-001"),
            timestamp=_ts("2026-04-23T14:31:22Z"),
            mode="normal",
            recommendations=(),
        )
        assert output.recommendations == ()
        assert output.watchlist is None

    def test_watchlist_with_empty_watchlist_allowed(self) -> None:
        output = AnalystOutput(
            invocation_id=InvocationId("inv-001"),
            timestamp=_ts("2026-04-23T14:31:22Z"),
            mode="watchlist",
            watchlist=(),
        )
        assert output.watchlist == ()
        assert output.recommendations is None


# ---------------------------------------------------------------------------
# 2. Timestamp tz-awareness
# ---------------------------------------------------------------------------


class TestTimestampTzAware:
    def test_naive_datetime_rejected(self) -> None:
        # The whole point of this test is to construct a naive datetime and
        # verify the model rejects it. DTZ001 fires on the naive constructor;
        # warranted suppression because the rejection is the behavior under test.
        with pytest.raises((ValueError, TypeError), match=r"(?i)tz|timezone|aware"):
            AnalystOutput(
                invocation_id=InvocationId("inv-001"),
                timestamp=datetime(2026, 4, 23, 14, 31, 22),  # noqa: DTZ001
                mode="normal",
                recommendations=(),
            )

    def test_utc_aware_datetime_accepted(self) -> None:
        output = AnalystOutput(
            invocation_id=InvocationId("inv-001"),
            timestamp=datetime(2026, 4, 23, 14, 31, 22, tzinfo=UTC),
            mode="normal",
            recommendations=(),
        )
        assert output.timestamp.tzinfo is not None


# ---------------------------------------------------------------------------
# 3. entry_window <-> entry_window_rationale paired
# ---------------------------------------------------------------------------


class TestEntryWindowPairing:
    def test_window_without_rationale_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)entry_window"):
            _make_recommendation(
                entry_window=EntryWindow(
                    deadline=_ts("2026-04-23T19:55:00Z"),
                    decay_type="binary",
                    rationale="window rationale",
                ),
                entry_window_rationale=None,
            )

    def test_rationale_without_window_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)entry_window"):
            _make_recommendation(entry_window=None, entry_window_rationale="orphan rationale")

    def test_both_present_accepted(self) -> None:
        rec = _make_recommendation(
            entry_window=EntryWindow(
                deadline=_ts("2026-04-23T19:55:00Z"),
                decay_type="binary",
                rationale="binary decay before earnings.",
            ),
            entry_window_rationale="Binary decay rationale.",
        )
        assert rec.entry_window is not None
        assert rec.entry_window_rationale == "Binary decay rationale."

    def test_both_absent_accepted(self) -> None:
        rec = _make_recommendation(entry_window=None, entry_window_rationale=None)
        assert rec.entry_window is None
        assert rec.entry_window_rationale is None


# ---------------------------------------------------------------------------
# 4. EntryOrder type <-> price-fields invariant
# ---------------------------------------------------------------------------


class TestEntryOrderInvariant:
    def test_market_no_prices_required(self) -> None:
        eo = EntryOrder(type="market")
        assert eo.type == "market"
        assert eo.limit_price is None
        assert eo.stop_price is None

    def test_limit_requires_limit_price(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)limit_price"):
            EntryOrder(type="limit")

    def test_limit_with_limit_price_accepted(self) -> None:
        eo = EntryOrder(type="limit", limit_price=price(100.0))
        assert eo.limit_price == 100.0

    def test_stop_limit_requires_both_prices(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)stop_price|limit_price"):
            EntryOrder(type="stop_limit", limit_price=price(100.0))
        with pytest.raises((ValueError, TypeError), match=r"(?i)limit_price|stop_price"):
            EntryOrder(type="stop_limit", stop_price=price(99.0))

    def test_stop_limit_with_both_prices_accepted(self) -> None:
        eo = EntryOrder(type="stop_limit", limit_price=price(100.0), stop_price=price(99.0))
        assert eo.limit_price == 100.0
        assert eo.stop_price == 99.0


# ---------------------------------------------------------------------------
# 5. Target target_type <-> pl_percentage/pl_dollar invariant
# ---------------------------------------------------------------------------


class TestTargetInvariant:
    def test_absolute_price_no_extras_required(self) -> None:
        t = Target(target_type="absolute_price", price=price(890.0), dollar_pl_target=money(190.0))
        assert t.pl_percentage is None
        assert t.pl_dollar is None

    def test_pl_percentage_requires_pct(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)pl_percentage"):
            Target(target_type="pl_percentage", price=price(890.0), dollar_pl_target=money(190.0))

    def test_pl_percentage_with_pct_accepted(self) -> None:
        t = Target(
            target_type="pl_percentage",
            price=price(890.0),
            dollar_pl_target=money(190.0),
            pl_percentage=80.0,
        )
        assert t.pl_percentage == 80.0

    def test_pl_dollar_requires_dollar(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)pl_dollar"):
            Target(target_type="pl_dollar", price=price(890.0), dollar_pl_target=money(190.0))

    def test_pl_dollar_with_dollar_accepted(self) -> None:
        t = Target(
            target_type="pl_dollar",
            price=price(890.0),
            dollar_pl_target=money(190.0),
            pl_dollar=money(190.0),
        )
        assert t.pl_dollar == 190.0


# ---------------------------------------------------------------------------
# 6. InvalidationLeg type <-> is_hard <-> order_parameters invariant
# ---------------------------------------------------------------------------


class TestInvalidationLegInvariant:
    def test_price_must_be_hard_with_order_parameters(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)is_hard|hard"):
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=False,
                condition=PriceCondition(
                    underlying_trigger=Symbol("NVDA"), comparator="<=", trigger_price=price(820.0)
                ),
                order_parameters=OrderParameters(order_type="market"),
            )

    def test_price_requires_order_parameters(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)order_parameters"):
            InvalidationLeg(
                leg_id="INV-1",
                type="price",
                is_hard=True,
                condition=PriceCondition(
                    underlying_trigger=Symbol("NVDA"), comparator="<=", trigger_price=price(820.0)
                ),
                order_parameters=None,
            )

    def test_price_valid(self) -> None:
        leg = InvalidationLeg(
            leg_id="INV-1",
            type="price",
            is_hard=True,
            condition=PriceCondition(
                underlying_trigger=Symbol("NVDA"), comparator="<=", trigger_price=price(820.0)
            ),
            order_parameters=OrderParameters(order_type="market"),
        )
        assert leg.is_hard is True

    def test_time_must_be_hard_with_order_parameters(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)is_hard|hard"):
            InvalidationLeg(
                leg_id="INV-2",
                type="time",
                is_hard=False,
                condition=TimeCondition(deadline=_ts("2026-04-23T20:00:00Z")),
                order_parameters=OrderParameters(order_type="market"),
            )

    def test_time_requires_order_parameters(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)order_parameters"):
            InvalidationLeg(
                leg_id="INV-2",
                type="time",
                is_hard=True,
                condition=TimeCondition(deadline=_ts("2026-04-23T20:00:00Z")),
                order_parameters=None,
            )

    def test_event_must_be_soft(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)is_hard|hard|soft"):
            InvalidationLeg(
                leg_id="INV-3",
                type="event",
                is_hard=True,
                condition=EventCondition(event_description="Earnings miss."),
            )

    def test_event_forbids_order_parameters(self) -> None:
        with pytest.raises((ValueError, TypeError), match=r"(?i)order_parameters"):
            InvalidationLeg(
                leg_id="INV-3",
                type="event",
                is_hard=False,
                condition=EventCondition(event_description="Earnings miss."),
                order_parameters=OrderParameters(order_type="market"),
            )

    def test_event_valid(self) -> None:
        leg = InvalidationLeg(
            leg_id="INV-3",
            type="event",
            is_hard=False,
            condition=EventCondition(event_description="Earnings miss."),
        )
        assert leg.is_hard is False
        assert leg.order_parameters is None


# ---------------------------------------------------------------------------
# 7. Pattern checks
# ---------------------------------------------------------------------------


class TestPatterns:
    def test_recommendation_id_pattern(self) -> None:
        for valid in ("REC-1", "REC-12", "REC-100"):
            rec = _make_recommendation(recommendation_id=valid)
            assert rec.recommendation_id == valid
        for invalid in ("rec-1", "REC1", "INV-1", "REC-"):
            with pytest.raises((ValueError, TypeError)):
                _make_recommendation(recommendation_id=invalid)

    def test_invalidation_leg_id_pattern(self) -> None:
        for valid in ("INV-1", "INV-99"):
            leg = InvalidationLeg(
                leg_id=valid,
                type="event",
                is_hard=False,
                condition=EventCondition(event_description="Earnings miss."),
            )
            assert leg.leg_id == valid
        for invalid in ("inv-1", "INV1", "REC-1", "INV-"):
            with pytest.raises((ValueError, TypeError)):
                InvalidationLeg(
                    leg_id=invalid,
                    type="event",
                    is_hard=False,
                    condition=EventCondition(event_description="x"),
                )


# ---------------------------------------------------------------------------
# 8. Sector enum (4-way risk-side taxonomy)
# ---------------------------------------------------------------------------


class TestSectorEnum:
    def test_all_four_values_accepted(self) -> None:
        for s in ("tech", "semis", "financials", "energy"):
            rec = _make_recommendation(sector=s)
            assert rec.sector == s

    def test_three_way_value_rejected(self) -> None:
        """tech_semis is the analysis-side 3-way taxonomy; not allowed here."""
        with pytest.raises((ValueError, TypeError)):
            _make_recommendation(sector="tech_semis")

    def test_unknown_sector_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_recommendation(sector="healthcare")


class TestWatchlistEntrySector:
    def test_all_four_values_accepted(self) -> None:
        for s in ("tech", "semis", "financials", "energy"):
            we = WatchlistEntry(
                ticker=Symbol("NVDA"),
                sector=s,
                thesis_summary="Watching catalyst.",
                estimated_conviction=3,
            )
            assert we.sector == s

    def test_three_way_value_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            WatchlistEntry(
                ticker=Symbol("NVDA"),
                sector="tech_semis",  # type: ignore[arg-type]  # deliberate runtime rejection
                thesis_summary="x",
                estimated_conviction=3,
            )


# ---------------------------------------------------------------------------
# 9. Discriminated instrument union
# ---------------------------------------------------------------------------


class TestInstrumentUnion:
    def test_equity_dispatches(self) -> None:
        rec = _make_recommendation(
            instrument=InstrumentEquity(
                asset_type="equity", ticker=Symbol("AAPL"), direction="long"
            )
        )
        assert isinstance(rec.instrument, InstrumentEquity)

    def test_option_dispatches(self) -> None:
        rec = _make_recommendation(
            instrument=InstrumentOption(
                asset_type="option",
                underlying=Symbol("NVDA"),
                strike=price(850.0),
                expiration=date(2026, 5, 17),
                contract_type="call",
                direction="long",
            )
        )
        assert isinstance(rec.instrument, InstrumentOption)
        assert rec.instrument.strike == 850.0

    def test_strategy_dispatches(self) -> None:
        legs = (
            StrategyLeg(
                strike=price(850.0),
                expiration=date(2026, 5, 17),
                contract_type="call",
                direction="long",
                quantity_ratio=1,
            ),
            StrategyLeg(
                strike=price(900.0),
                expiration=date(2026, 5, 17),
                contract_type="call",
                direction="short",
                quantity_ratio=1,
            ),
        )
        rec = _make_recommendation(
            instrument=InstrumentStrategy(
                asset_type="strategy",
                strategy_type="vertical_spread",
                underlying=Symbol("NVDA"),
                legs=legs,
            )
        )
        assert isinstance(rec.instrument, InstrumentStrategy)
        assert len(rec.instrument.legs) == 2

    def test_strategy_requires_two_legs_minimum(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            InstrumentStrategy(
                asset_type="strategy",
                strategy_type="vertical_spread",
                underlying=Symbol("NVDA"),
                legs=(
                    StrategyLeg(
                        strike=price(850.0),
                        expiration=date(2026, 5, 17),
                        contract_type="call",
                        direction="long",
                        quantity_ratio=1,
                    ),
                ),
            )

    def test_via_model_validate_dict(self) -> None:
        """Round-trip dict-form payloads through the discriminator."""
        rec = _make_recommendation(
            instrument={
                "asset_type": "option",
                "underlying": "NVDA",
                "strike": 850.0,
                "expiration": "2026-05-17",
                "contract_type": "call",
                "direction": "long",
            }
        )
        assert isinstance(rec.instrument, InstrumentOption)


# ---------------------------------------------------------------------------
# 10. ConvictionLevel and time_expectation_hours bounds
# ---------------------------------------------------------------------------


class TestNumericBounds:
    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5])
    def test_conviction_level_in_range(self, level: int) -> None:
        rec = _make_recommendation(conviction_level=level)
        assert rec.conviction_level == level

    @pytest.mark.parametrize("level", [0, 6, -1])
    def test_conviction_level_out_of_range_rejected(self, level: int) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_recommendation(conviction_level=level)

    def test_time_expectation_hours_zero_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_recommendation(time_expectation_hours=0.0)

    def test_time_expectation_hours_above_72_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_recommendation(time_expectation_hours=73.0)

    def test_time_expectation_hours_72_accepted(self) -> None:
        rec = _make_recommendation(time_expectation_hours=72.0)
        assert rec.time_expectation_hours == 72.0


# ---------------------------------------------------------------------------
# 11. Invalidation legs minimum-length
# ---------------------------------------------------------------------------


class TestInvalidationLegsMinimum:
    def test_empty_invalidation_legs_rejected(self) -> None:
        with pytest.raises((ValueError, TypeError)):
            _make_recommendation(invalidation_legs=())


# ---------------------------------------------------------------------------
# 12. RuleProjection / Greeks reuse — imported, not redefined
# ---------------------------------------------------------------------------


class TestTypeReuse:
    def test_rule_projection_imported(self) -> None:
        from alphamind.decision.analyst.models import (
            RuleProjection as ModelsRP,
        )
        from alphamind.risk_guardrails.guardrail_evaluation import (
            RuleProjection as CanonRP,
        )

        assert ModelsRP is CanonRP

    def test_greeks_imported(self) -> None:
        from alphamind.decision.analyst.models import Greeks as ModelsG
        from alphamind.risk_guardrails.guardrail_evaluation import Greeks as CanonG

        assert ModelsG is CanonG

    def test_guardrail_validation_result_accepts_canonical_rule_projection(self) -> None:
        rp = RuleProjection(
            rule="per_position_max_size",
            status=Status.PASS,
            current=0.0,
            limit=5.0,
            projected_after=3.0,
            headroom_remaining=2.0,
            unit="% of portfolio",
        )
        gr = _make_guardrail_result(per_rule=(rp,))
        assert gr.per_rule[0] is rp

    def test_guardrail_validation_result_accepts_canonical_greeks(self) -> None:
        g = Greeks(delta=0.5, gamma=0.01, theta=-0.05, vega=0.10)
        gr = _make_guardrail_result(greeks=g)
        assert gr.greeks is g


# ---------------------------------------------------------------------------
# 13. JSON Schema export — Draft 2020-12, SDK-compatible top-level shape
# ---------------------------------------------------------------------------


class TestModelJsonSchema:
    def test_returns_dict(self) -> None:
        schema = AnalystOutput.model_json_schema()
        assert isinstance(schema, dict)

    def test_top_level_is_object_not_oneof(self) -> None:
        """Anthropic's JSON-Schema mode rejects top-level oneOf/allOf/anyOf
        under recent SDK builds. Per ALP-115 architectural-invariants the
        schema must be acceptable; mirror the qualitative-research adjustment
        if Pydantic ever generates a top-level oneOf."""
        schema = AnalystOutput.model_json_schema()
        assert "oneOf" not in schema
        assert "anyOf" not in schema
        assert schema.get("type") == "object"

    def test_required_top_level_fields_present(self) -> None:
        schema = AnalystOutput.model_json_schema()
        required = set(schema.get("required", ()))
        # invocation_id, timestamp, mode are always required
        assert {"invocation_id", "timestamp", "mode"}.issubset(required)


# ---------------------------------------------------------------------------
# 14. Public-surface re-exports
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_models_module_all_contains_every_name(self) -> None:
        import alphamind.decision.analyst.models as m

        expected = {
            "AnalystOutput",
            "EntryOrder",
            "EntryWindow",
            "EventCondition",
            "Greeks",
            "GuardrailValidationResult",
            "Instrument",
            "InstrumentEquity",
            "InstrumentOption",
            "InstrumentStrategy",
            "InvalidationCondition",
            "InvalidationLeg",
            "InvalidationRationale",
            "OrderParameters",
            "PositionSize",
            "PriceCondition",
            "Recommendation",
            "RuleProjection",
            "StrategyLeg",
            "Target",
            "TimeCondition",
            "WatchlistEntry",
        }
        assert expected.issubset(set(m.__all__))

    def test_package_init_re_exports_models(self) -> None:
        import alphamind.decision.analyst as pkg

        for name in (
            "AnalystOutput",
            "Recommendation",
            "WatchlistEntry",
            "InstrumentEquity",
            "InstrumentOption",
            "InstrumentStrategy",
            "EntryOrder",
            "EntryWindow",
            "Target",
            "PositionSize",
            "InvalidationLeg",
            "InvalidationRationale",
            "GuardrailValidationResult",
        ):
            assert hasattr(pkg, name), f"{name} not re-exported from package __init__"

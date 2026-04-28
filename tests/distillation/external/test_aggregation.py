"""Tests for the anomaly aggregation module — story 02-distillation/10."""

from __future__ import annotations

from datetime import UTC, datetime

from alphamind.distillation.aggregation import (
    AnomalySummary,
    assemble_audience_output,
    collect_anomalies,
    format_anomaly_summary,
    group_anomalies_by_audience,
    partition_blocks,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
    format_block,
)


def _make_block(
    *,
    block_id: str = "q1.volume_anomaly",
    audience: frozenset[OutputAudience] = frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    calibration_state: CalibrationState = CalibrationState.CALIBRATED,
    bootstrap_reason: str | None = None,
    payload: dict[str, object] | None = None,
    anomaly_flags: tuple[AnomalyFlag, ...] = (),
    regime_context: str | None = None,
    freshness_ts: datetime = datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
) -> OutputBlock:
    """Construct an ``OutputBlock`` with sensible defaults for tests."""
    return OutputBlock(
        block_id=block_id,
        audience=audience,
        freshness_ts=freshness_ts,
        calibration_state=calibration_state,
        bootstrap_reason=bootstrap_reason,
        payload=payload if payload is not None else {"ticker": "NVDA", "z_score": 2.5},
        anomaly_flags=anomaly_flags,
        regime_context=regime_context,
    )


def test_partition_blocks_groups_single_audience_blocks() -> None:
    """Single-audience blocks land in exactly one bucket."""
    tech = _make_block(
        block_id="q1.tech",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )
    fin = _make_block(
        block_id="q1.fin",
        audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
    )

    partitioned = partition_blocks([tech, fin])

    assert partitioned[OutputAudience.SECTOR_TECH_SEMIS] == [tech]
    assert partitioned[OutputAudience.SECTOR_FINANCIALS] == [fin]
    assert OutputAudience.SECTOR_ENERGY not in partitioned


def test_partition_blocks_routes_multi_audience_blocks_to_each_audience() -> None:
    """A block with multiple audiences appears in every targeted bucket."""
    block = _make_block(
        block_id="q7.cross_correlation",
        audience=frozenset(
            {
                OutputAudience.SECTOR_TECH_SEMIS,
                OutputAudience.CORRELATION_REGIME_BRIEF,
                OutputAudience.UNIVERSAL_BROADCAST,
            }
        ),
    )

    partitioned = partition_blocks([block])

    assert partitioned[OutputAudience.SECTOR_TECH_SEMIS] == [block]
    assert partitioned[OutputAudience.CORRELATION_REGIME_BRIEF] == [block]
    assert partitioned[OutputAudience.UNIVERSAL_BROADCAST] == [block]


def test_partition_blocks_sorts_by_block_id_within_each_audience() -> None:
    """Within each audience the blocks are sorted ascending by ``block_id``."""
    qual = _make_block(
        block_id="qual.headline_cluster",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )
    q1 = _make_block(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )
    q7 = _make_block(
        block_id="q7.cross_correlation",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )

    partitioned = partition_blocks([qual, q7, q1])

    assert partitioned[OutputAudience.SECTOR_TECH_SEMIS] == [q1, q7, qual]


def test_collect_anomalies_extracts_every_flag_into_summaries() -> None:
    """Every ``AnomalyFlag`` becomes an ``AnomalySummary`` carrying source-block context."""
    flag_one = AnomalyFlag(name="volume_spike", magnitude=2.5, severity="investigate_now")
    flag_two = AnomalyFlag(name="rvol_breakout", magnitude=1.8, severity="note_for_context")
    block = _make_block(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.UNIVERSAL_BROADCAST}),
        anomaly_flags=(flag_one, flag_two),
        calibration_state=CalibrationState.BOOTSTRAP,
        bootstrap_reason="volume_baseline: 5 < 20",
    )

    summaries = collect_anomalies([block])

    expected = {
        AnomalySummary(
            flag=flag_one,
            source_block_id="q1.volume_anomaly",
            audiences=frozenset(
                {OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.UNIVERSAL_BROADCAST}
            ),
            flagged_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
            calibration_state=CalibrationState.BOOTSTRAP,
        ),
        AnomalySummary(
            flag=flag_two,
            source_block_id="q1.volume_anomaly",
            audiences=frozenset(
                {OutputAudience.SECTOR_TECH_SEMIS, OutputAudience.UNIVERSAL_BROADCAST}
            ),
            flagged_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
            calibration_state=CalibrationState.BOOTSTRAP,
        ),
    }
    assert set(summaries) == expected


def test_collect_anomalies_returns_empty_list_for_blocks_with_no_flags() -> None:
    """Blocks without anomaly flags contribute zero summaries."""
    block = _make_block(anomaly_flags=())

    summaries = collect_anomalies([block])

    assert summaries == []


def test_collect_anomalies_walks_every_block() -> None:
    """Flags from multiple blocks are flattened into the same list."""
    flag_a = AnomalyFlag(name="a", magnitude=1.0, severity="investigate_if_persists")
    flag_b = AnomalyFlag(name="b", magnitude=2.0, severity="investigate_now")
    block_a = _make_block(block_id="q1.a", anomaly_flags=(flag_a,))
    block_b = _make_block(block_id="q3.b", anomaly_flags=(flag_b,))

    summaries = collect_anomalies([block_a, block_b])

    assert {summary.flag for summary in summaries} == {flag_a, flag_b}
    sources = {summary.source_block_id for summary in summaries}
    assert sources == {"q1.a", "q3.b"}


def test_group_anomalies_by_audience_distributes_universal_broadcasts_to_every_audience() -> None:
    """A summary on a UNIVERSAL_BROADCAST block appears in every audience's list.

    Universal anomalies are visible to every consumer per the
    load-bearing universal-broadcast contract — a regime-skip emergency
    on a universal block must reach sector audiences and the
    correlation/regime brief.
    """
    universal = AnomalySummary(
        flag=AnomalyFlag(name="regime_skip_emergency", magnitude=1.0, severity="investigate_now"),
        source_block_id="regime.label",
        audiences=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        flagged_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
    )

    grouped = group_anomalies_by_audience([universal])

    for audience in OutputAudience:
        assert grouped[audience] == [universal], f"missing universal anomaly at {audience}"


def test_group_anomalies_by_audience_only_distributes_to_targeted_audiences() -> None:
    """A non-universal summary appears only at its source-block audiences."""
    summary = AnomalySummary(
        flag=AnomalyFlag(name="local", magnitude=1.0, severity="note_for_context"),
        source_block_id="q1.local",
        audiences=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        flagged_at=datetime(2026, 4, 27, 14, 30, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
    )

    grouped = group_anomalies_by_audience([summary])

    assert grouped[OutputAudience.SECTOR_TECH_SEMIS] == [summary]
    assert OutputAudience.SECTOR_FINANCIALS not in grouped
    assert OutputAudience.SECTOR_ENERGY not in grouped
    assert OutputAudience.CORRELATION_REGIME_BRIEF not in grouped
    assert OutputAudience.UNIVERSAL_BROADCAST not in grouped


def test_group_anomalies_by_audience_orders_by_severity_then_magnitude_then_block_id() -> None:
    """Within each audience: severity rank, magnitude desc, block_id asc.

    Severity rank: ``investigate_now`` > ``investigate_if_persists`` >
    ``note_for_context``.
    """
    base_ts = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    audience_set = frozenset({OutputAudience.SECTOR_TECH_SEMIS})
    note_low = AnomalySummary(
        flag=AnomalyFlag(name="note_low", magnitude=1.0, severity="note_for_context"),
        source_block_id="q1.a_note_low",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )
    persist_high = AnomalySummary(
        flag=AnomalyFlag(name="persist_high", magnitude=3.0, severity="investigate_if_persists"),
        source_block_id="q1.b_persist_high",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )
    now_low = AnomalySummary(
        flag=AnomalyFlag(name="now_low", magnitude=1.5, severity="investigate_now"),
        source_block_id="q1.c_now_low",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )
    now_high_b = AnomalySummary(
        flag=AnomalyFlag(name="now_high_b", magnitude=2.5, severity="investigate_now"),
        source_block_id="q1.b_now_high",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )
    now_high_a = AnomalySummary(
        flag=AnomalyFlag(name="now_high_a", magnitude=2.5, severity="investigate_now"),
        source_block_id="q1.a_now_high",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )

    # Intentionally shuffled input.
    grouped = group_anomalies_by_audience([note_low, persist_high, now_low, now_high_b, now_high_a])

    assert grouped[OutputAudience.SECTOR_TECH_SEMIS] == [
        # investigate_now first; within it: magnitude desc; ties → block_id asc.
        now_high_a,
        now_high_b,
        now_low,
        # then investigate_if_persists.
        persist_high,
        # then note_for_context.
        note_low,
    ]


def test_format_anomaly_summary_renders_severity_groups_in_order() -> None:
    """The rendered text shows ``investigate_now``, then ``investigate_if_persists``,
    then ``note_for_context``."""
    base_ts = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    audience_set = frozenset({OutputAudience.SECTOR_TECH_SEMIS})
    summaries = [
        AnomalySummary(
            flag=AnomalyFlag(name="alpha", magnitude=2.5, severity="investigate_now"),
            source_block_id="q1.a",
            audiences=audience_set,
            flagged_at=base_ts,
            calibration_state=CalibrationState.CALIBRATED,
        ),
        AnomalySummary(
            flag=AnomalyFlag(name="beta", magnitude=1.8, severity="investigate_if_persists"),
            source_block_id="q1.b",
            audiences=audience_set,
            flagged_at=base_ts,
            calibration_state=CalibrationState.CALIBRATED,
        ),
        AnomalySummary(
            flag=AnomalyFlag(name="gamma", magnitude=1.0, severity="note_for_context"),
            source_block_id="q1.c",
            audiences=audience_set,
            flagged_at=base_ts,
            calibration_state=CalibrationState.CALIBRATED,
        ),
    ]

    rendered = format_anomaly_summary(summaries)

    now_pos = rendered.index("--- INVESTIGATE NOW")
    persist_pos = rendered.index("--- INVESTIGATE IF PERSISTS")
    note_pos = rendered.index("--- NOTE FOR CONTEXT")
    assert now_pos < persist_pos < note_pos


def test_format_anomaly_summary_orders_within_severity_by_magnitude_descending() -> None:
    """Within a severity, higher magnitude appears first."""
    base_ts = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    audience_set = frozenset({OutputAudience.SECTOR_TECH_SEMIS})
    low = AnomalySummary(
        flag=AnomalyFlag(name="lower", magnitude=1.5, severity="investigate_now"),
        source_block_id="q1.a_low",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )
    high = AnomalySummary(
        flag=AnomalyFlag(name="higher", magnitude=3.0, severity="investigate_now"),
        source_block_id="q1.b_high",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )

    rendered = format_anomaly_summary([low, high])

    assert rendered.index("higher") < rendered.index("lower")


def test_format_anomaly_summary_omits_empty_severity_sections() -> None:
    """Severity sections with zero entries are not rendered (no ``(0)`` headers)."""
    base_ts = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    audience_set = frozenset({OutputAudience.SECTOR_TECH_SEMIS})
    only_now = AnomalySummary(
        flag=AnomalyFlag(name="alpha", magnitude=2.5, severity="investigate_now"),
        source_block_id="q1.a",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.CALIBRATED,
    )

    rendered = format_anomaly_summary([only_now])

    assert "INVESTIGATE NOW" in rendered
    assert "INVESTIGATE IF PERSISTS" not in rendered
    assert "NOTE FOR CONTEXT" not in rendered


def test_format_anomaly_summary_includes_calibration_state_per_anomaly() -> None:
    """Calibration state is visible on every anomaly line.

    Downstream LLMs weight bootstrap-tagged anomalies lower per
    ``docs/design/02-distillation-layer/threshold-calibration.md``
    § Downstream propagation.
    """
    base_ts = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)
    audience_set = frozenset({OutputAudience.SECTOR_TECH_SEMIS})
    bootstrap_summary = AnomalySummary(
        flag=AnomalyFlag(name="bootstrap_anomaly", magnitude=2.5, severity="investigate_now"),
        source_block_id="q1.a",
        audiences=audience_set,
        flagged_at=base_ts,
        calibration_state=CalibrationState.BOOTSTRAP,
    )

    rendered = format_anomaly_summary([bootstrap_summary])

    assert "calibration bootstrap" in rendered
    assert "bootstrap_anomaly" in rendered


def test_format_anomaly_summary_zero_flags_renders_zero_count_header() -> None:
    """Zero anomalies produces a deterministic, non-empty document."""
    rendered = format_anomaly_summary([])
    assert rendered.strip() != ""
    assert "=== ANOMALY FLAGS (0) ===" in rendered


def test_assemble_audience_output_combines_anomaly_summary_then_block_renders() -> None:
    """The convenience entry point stacks the anomaly summary above the routed blocks."""
    flag = AnomalyFlag(name="volume_spike", magnitude=2.5, severity="investigate_now")
    tech_block = _make_block(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        anomaly_flags=(flag,),
    )
    fin_block = _make_block(
        block_id="q1.financials",
        audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
    )

    rendered = assemble_audience_output(OutputAudience.SECTOR_TECH_SEMIS, [tech_block, fin_block])

    # Anomaly summary appears at the top.
    assert rendered.startswith("=== ANOMALY FLAGS (1) ===")
    # The tech block is rendered after the summary; the fin-only block is not.
    assert format_block(tech_block) in rendered
    assert "q1.financials" not in rendered


def test_assemble_audience_output_is_byte_identical_on_repeated_calls() -> None:
    """Repeated calls produce identical bytes — required for diffable archives."""
    flag = AnomalyFlag(name="volume_spike", magnitude=3.14159, severity="investigate_now")
    block_a = _make_block(
        block_id="q1.a",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        anomaly_flags=(flag,),
    )
    block_b = _make_block(
        block_id="q1.b",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )
    blocks = [block_a, block_b]

    first = assemble_audience_output(OutputAudience.SECTOR_TECH_SEMIS, blocks)
    second = assemble_audience_output(OutputAudience.SECTOR_TECH_SEMIS, blocks)

    assert first == second


def test_assemble_audience_output_empty_input_produces_zero_count_header() -> None:
    """Empty inputs produce a deterministic non-empty document."""
    rendered = assemble_audience_output(OutputAudience.SECTOR_TECH_SEMIS, [])

    assert rendered.strip() != ""
    assert "=== ANOMALY FLAGS (0) ===" in rendered


def test_assemble_audience_output_includes_universal_broadcast_anomalies() -> None:
    """A flag attached to a UNIVERSAL_BROADCAST block surfaces in every audience's output.

    This is the load-bearing universal-broadcast contract: a regime-skip
    emergency must reach every per-sector audience and the
    correlation/regime brief.
    """
    universal_flag = AnomalyFlag(
        name="regime_skip_emergency", magnitude=1.0, severity="investigate_now"
    )
    universal_block = _make_block(
        block_id="regime.label",
        audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
        anomaly_flags=(universal_flag,),
    )
    sector_block = _make_block(
        block_id="q1.tech",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
    )

    rendered = assemble_audience_output(
        OutputAudience.SECTOR_TECH_SEMIS, [universal_block, sector_block]
    )

    assert "regime_skip_emergency" in rendered

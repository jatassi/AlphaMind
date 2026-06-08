"""Anomaly aggregation and per-consumer partitioning.

Pure in-memory aggregation layer that sits between the per-category
producers (stories 08*, 09) and the per-consumer document assemblers
(stories 11a, 11b). Three responsibilities:

- Route every :class:`OutputBlock` to the consumer audiences it targets.
- Collect every :class:`AnomalyFlag` carried by those blocks into the
  ``Anomaly flags`` summary view downstream agents scan first per
  ``docs/design/02-distillation-layer/external.md`` § Output format.
- Produce a deterministic, byte-identical rendering so the invocation
  archive diffs cleanly across runs.

Reference docs:

- ``docs/design/02-distillation-layer/external.md`` § Output format —
  "Anomaly flags: Binary flags plus magnitude per detected anomaly,
  grouped for easy scanning" — motivates the severity-grouped layout.
- ``docs/design/02-distillation-layer/threshold-calibration.md``
  § Downstream propagation — a volume anomaly on a non-calibrated
  baseline (``accumulating`` or ``unavailable``) is weaker than the
  same anomaly on a ``calibrated`` baseline; this motivates
  per-anomaly calibration-state visibility.
- ``docs/design/03-analysis-layer/domain-researchers/tech-semis.md``
  § Domain researcher output contract — the analyst-side severity
  taxonomy preserved here unchanged.

The module is purely functional: every helper takes the inputs it needs
and returns the result. No I/O, no SQL, no shared state.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.flag_event_types import resolve_flag_taxonomy
from alphamind.distillation.output import (
    PERCENTAGE_FLOAT_FORMAT,
    SEVERITY_ORDER,
    AnomalyFlag,
    AnomalySeverity,
    OutputAudience,
    OutputBlock,
    format_blocks_for_audience,
    severity_rank,
)
from alphamind.portfolio_state.events import (
    ActivityLogEntry,
    DistillationAnomalyFlagDetail,
    EventSource,
    EventType,
    build_activity_log_entry,
)

# ---------------------------------------------------------------------------
# Severity ordering — re-exported from output.py
# ---------------------------------------------------------------------------
#
# The severity total order lives in :mod:`alphamind.distillation.output`
# alongside the :data:`AnomalySeverity` literal type. The aggregation
# layer's per-audience sort and the publishing-layer cap
# (:mod:`alphamind.distillation._severity_cap`) consume the same order;
# centralizing it next to the type prevents the two consumers from
# drifting.


def partition_blocks(
    blocks: Iterable[OutputBlock],
) -> dict[OutputAudience, list[OutputBlock]]:
    """Group blocks by consumer audience.

    A block carrying multiple audiences appears in each audience's list.
    Within each audience the blocks are sorted ascending by ``block_id``
    so consumers see a deterministic order independent of producer-side
    iteration.
    """
    grouped: dict[OutputAudience, list[OutputBlock]] = defaultdict(list)
    for block in blocks:
        for audience in block.audience:
            grouped[audience].append(block)
    return {
        audience: sorted(items, key=lambda block: block.block_id)
        for audience, items in grouped.items()
    }


@dataclass(frozen=True, slots=True)
class AnomalySummary:
    """An :class:`AnomalyFlag` paired with its source-block context.

    The downstream consumer reads ``calibration_state`` to weight the
    anomaly — a volume anomaly on a non-calibrated baseline
    (``accumulating`` / ``unavailable``) is weaker than the same anomaly
    on a ``calibrated`` baseline per
    ``docs/design/02-distillation-layer/threshold-calibration.md``
    § Downstream propagation.
    """

    flag: AnomalyFlag
    source_block_id: str
    audiences: frozenset[OutputAudience]
    flagged_at: datetime
    calibration_state: CalibrationState


def collect_anomalies(blocks: Iterable[OutputBlock]) -> list[AnomalySummary]:
    """Walk every block and wrap each :class:`AnomalyFlag` in an :class:`AnomalySummary`.

    Returns a flat list across all blocks. Source-block context (id,
    audiences, freshness, calibration state) flows through to each
    summary so downstream consumers can render the anomaly without
    re-parsing the underlying block.
    """
    summaries: list[AnomalySummary] = []
    for block in blocks:
        for flag in block.anomaly_flags:
            summaries.append(
                AnomalySummary(
                    flag=flag,
                    source_block_id=block.block_id,
                    audiences=block.audience,
                    flagged_at=block.freshness_ts,
                    calibration_state=block.calibration_state,
                )
            )
    return summaries


# ---------------------------------------------------------------------------
# Activity-log emission (ALP-881 / story 04b)
# ---------------------------------------------------------------------------
#
# Persist each produced :class:`AnomalyFlag` as a ``DISTILLATION_ANOMALY_FLAG``
# activity-log entry so the ALP-96 flag-rate reporter can query anomaly
# history without re-parsing the invocation archive. The mapping is pure
# (no I/O, no SQL); the distillation orchestrator owns the sync ``session.add``
# of the resulting row.

# Flag prefixes that embed a single ticker as their one dynamic ``:``-segment
# (``correlation_locus_flag:{ticker}``; the per-ticker q1 / qualitative anomalies
# ``volume_anomaly:{ticker}`` / ``price_move_anomaly:{ticker}`` /
# ``news_price_divergence:{ticker}``; and q12's ``etf_vs_single_name_divergence:{etf}``
# — a sector ETF is a symbol — ALP-934). Every other dynamic-suffix flag carries a
# *pair* (``correlation_breakdown_flag:{row}:{col}``,
# ``intra_sector_correlation_divergence:{row}:{col}``,
# ``pair_trade_signature:{bullish_leg}:{bearish_leg}`` — a leg pair, not one
# symbol — ALP-935), a pair-key
# (``lead_lag_inversion_flag:{pair_key}``, ``overdue_lag_flag:{pair_key}``), or a
# non-symbol subject (``q12_event_novelty:{sector_audience}``,
# ``sector_wide_sweep:{sector}:{direction}`` — a sector, not a symbol — ALP-935,
# ``prediction_market_delta:{contract_id}`` — a contract, not a symbol) — none
# names a single subject ticker, so all resolve to ``ticker=None``. Segment
# *count* alone can't separate the locus / per-ticker case from the pair-key /
# contract case (both have one suffix segment), so the ticker-bearing prefixes
# are enumerated explicitly.
_TICKER_BEARING_FLAG_PREFIXES: frozenset[str] = frozenset(
    {
        "correlation_locus_flag",
        "volume_anomaly",
        "price_move_anomaly",
        "news_price_divergence",
        "etf_vs_single_name_divergence",
    }
)


def _ticker_from_flag_name(flag_name: str) -> str | None:
    """Return the embedded ticker for a ticker-bearing flag name, else ``None``.

    ``correlation_locus_flag:NVDA`` → ``"NVDA"``. Market-wide flags (no ``:``
    suffix) and pair / pair-key flags return ``None`` — they have no single
    subject ticker.
    """
    prefix, _, suffix = flag_name.partition(":")
    if not suffix or prefix not in _TICKER_BEARING_FLAG_PREFIXES:
        return None
    return suffix


def anomaly_summary_to_activity_log_entry(
    summary: AnomalySummary,
    *,
    invocation_id: str,
    timestamp: datetime,
) -> ActivityLogEntry:
    """Build the ``DISTILLATION_ANOMALY_FLAG`` entry for one :class:`AnomalySummary`.

    ``threshold_class`` / ``threshold_key`` come from
    :func:`~alphamind.distillation.flag_event_types.resolve_flag_taxonomy`
    (the story 02g registry, which strips any dynamic ``:``-suffix). ``magnitude``
    / ``severity`` come from the flag; ``calibration_state`` / ``block_id`` from
    the summary; ``ticker`` is the embedded segment for ticker-bearing flag
    names and ``None`` for pair-key / market-wide flags.

    Pure: no I/O, no SQL. The orchestrator persists the returned entry on its
    own session. ``entry_id`` is ``(invocation_id, block_id, flag.name)`` joined
    with ``-``: deterministic (a re-run of the same invocation reproduces the same
    PKs, so the table never accumulates duplicate rows for one logical anomaly)
    **and** unique within an invocation — but only because every producer that can
    emit two flags into one block gives those flags distinct names. Block-level
    constant names with a per-subject fan-out (the q1 / qualitative per-ticker and
    per-contract anomalies) embed their subject as a ``:``-suffix to keep the
    triple unique; ``resolve_flag_taxonomy`` strips that suffix so the threshold
    taxonomy is unaffected (ALP-934). A producer that reused one bare constant
    across two flags in a single block would collide N rows on this PK inside one
    ``executemany`` — the regression these subject-embedded names exist to prevent.
    """
    flag = summary.flag
    taxonomy = resolve_flag_taxonomy(flag.name)
    detail = DistillationAnomalyFlagDetail(
        threshold_class=taxonomy.threshold_class,
        threshold_key=taxonomy.threshold_key,
        magnitude=flag.magnitude,
        severity=flag.severity,
        ticker=_ticker_from_flag_name(flag.name),
        calibration_state=summary.calibration_state,
        block_id=summary.source_block_id,
    )
    entry_id = (
        f"{invocation_id}-{EventType.DISTILLATION_ANOMALY_FLAG.value}-"
        f"{summary.source_block_id}-{flag.name}"
    )
    return build_activity_log_entry(
        invocation_id=invocation_id,
        event_type=EventType.DISTILLATION_ANOMALY_FLAG,
        position_id=None,
        order_id=None,
        thesis_id=None,
        timestamp=timestamp,
        detail=detail,
        source=EventSource.DISTILLATION_ORCHESTRATOR,
        entry_id=entry_id,
    )


def group_anomalies_by_audience(
    summaries: Iterable[AnomalySummary],
) -> dict[OutputAudience, list[AnomalySummary]]:
    """Group summaries by audience, broadcasting universal anomalies to every audience.

    A summary whose source block targets :attr:`OutputAudience.UNIVERSAL_BROADCAST`
    appears in every audience's list — universal anomalies are visible
    to every consumer. Non-universal summaries appear only at the
    audiences their source block targets.
    """
    grouped: dict[OutputAudience, list[AnomalySummary]] = defaultdict(list)
    for summary in summaries:
        is_universal = OutputAudience.UNIVERSAL_BROADCAST in summary.audiences
        target_audiences: Iterable[OutputAudience] = (
            tuple(OutputAudience) if is_universal else summary.audiences
        )
        for audience in target_audiences:
            grouped[audience].append(summary)
    return {audience: sorted(items, key=_anomaly_sort_key) for audience, items in grouped.items()}


def _anomaly_sort_key(summary: AnomalySummary) -> tuple[int, float, str]:
    """Sort key implementing the in-audience presentation order.

    The order is severity rank ascending, magnitude descending, then
    ``source_block_id`` ascending. Negating ``magnitude`` flips its
    direction within ``sorted``'s ascending default.
    """
    return (
        severity_rank(summary.flag.severity),
        -summary.flag.magnitude,
        summary.source_block_id,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
#
# The rendered "Anomaly flags" block follows the severity-grouped layout
# specified by ``docs/design/02-distillation-layer/external.md``
# § Output format ("grouped for easy scanning"). Empty severity sections
# are omitted entirely; the calibration state is visible per anomaly so
# downstream consumers can weight non-calibrated anomalies lower
# without parsing the underlying block.

# Shared empty-state marker for the universal-context section. The same
# section is rendered by ``sector_assembly`` (per-audience sector outputs)
# and ``correlation_brief`` (the synthesizer's CR brief); centralizing the
# marker here gives the never-drop contract a single source of truth and
# prevents the two assemblers from drifting (ALP-577).
EMPTY_UNIVERSAL_CONTEXT_MARKER = "(no universal-broadcast blocks)"

_SEVERITY_HEADER: dict[AnomalySeverity, str] = {
    "investigate_now": "INVESTIGATE NOW",
    "investigate_if_persists": "INVESTIGATE IF PERSISTS",
    "note_for_context": "NOTE FOR CONTEXT",
}


def format_anomaly_summary(summaries: Iterable[AnomalySummary]) -> str:
    """Render the ``Anomaly flags`` block per ``external.md`` § Output format.

    The output is byte-identical for the same input so the invocation
    archive diffs cleanly. Severity sections appear in the order
    pinned by :data:`SEVERITY_ORDER`; empty sections are omitted.
    Within a section, summaries follow :func:`_anomaly_sort_key`
    (magnitude descending, then ``source_block_id`` ascending). The
    zero-flag case renders as a single line so the empty summary doesn't
    eat tokens (ALP-272).
    """
    materialized = list(summaries)
    if not materialized:
        return "=== ANOMALY FLAGS (0) ===\n"
    by_severity: dict[AnomalySeverity, list[AnomalySummary]] = {
        severity: [] for severity in SEVERITY_ORDER
    }
    for summary in materialized:
        by_severity[summary.flag.severity].append(summary)

    lines: list[str] = [f"=== ANOMALY FLAGS ({len(materialized)}) ==="]
    for severity in SEVERITY_ORDER:
        bucket = sorted(by_severity[severity], key=_anomaly_sort_key)
        if not bucket:
            continue
        lines.append(f"--- {_SEVERITY_HEADER[severity]} ({len(bucket)}) ---")
        for summary in bucket:
            magnitude = format(summary.flag.magnitude, PERCENTAGE_FLOAT_FORMAT)
            lines.append(
                f"- {summary.flag.name} | magnitude {magnitude} | "
                f"source {summary.source_block_id} | "
                f"calibration {summary.calibration_state.value}"
            )
    return "\n".join(lines) + "\n"


def assemble_audience_output(
    audience: OutputAudience,
    blocks: Iterable[OutputBlock],
) -> str:
    """Produce the per-audience document body for ``audience``.

    Combines :func:`collect_anomalies`, :func:`group_anomalies_by_audience`,
    :func:`format_anomaly_summary`, and (story 05's)
    :func:`format_blocks_for_audience` to produce the per-audience
    document body — anomaly summary at the top, followed by every block
    routed to ``audience`` in ``block_id`` order.

    Stories 11a / 11b can either call this directly or wrap it with
    sector / ``CR`` framing. Per-call repeatability: the same inputs
    produce byte-identical output, which is what makes the invocation
    archive diffable.
    """
    materialized = list(blocks)
    summaries = collect_anomalies(materialized)
    grouped_summaries = group_anomalies_by_audience(summaries)
    audience_summaries = grouped_summaries.get(audience, [])
    return format_anomaly_summary(audience_summaries) + format_blocks_for_audience(
        materialized, audience
    )

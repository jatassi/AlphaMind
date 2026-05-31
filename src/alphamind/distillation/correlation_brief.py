"""Correlation/regime brief assembler.

Produces the document the synthesizer agent consumes (reference prefix
``CR``). The brief packs the volatility regime label (universal context),
the category-7 cross-asset findings (intra-sector divergences,
cross-sector rotation, intermarket regime signals, lead-lag gaps,
correlation regime changes, narrative lag), and the universal-broadcast
macro / breadth context, then emits each finding under a sequential
``[CR-N]`` reference. The reverse index lives next to the document body
for the synthesizer's retrieval-store side-effect — see
``docs/design/03-analysis-layer/synthesizer.md`` § Retrieval store
side-effect.

The module is purely functional. The same input produces a byte-identical
document — sorting is deterministic, floats use the named precision
constants from :mod:`alphamind.distillation.output`, and the assembler
never embeds the current wall clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from alphamind.distillation.aggregation import (
    EMPTY_UNIVERSAL_CONTEXT_MARKER,
    collect_anomalies,
    format_anomaly_summary,
    group_anomalies_by_audience,
)
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.output import (
    GENERAL_FLOAT_FORMAT,
    OutputAudience,
    OutputBlock,
    format_blocks_for_audience,
)
from alphamind.distillation.q7.correlation_regime_change_compute import (
    DISPERSION_SHIFT_BLOCK_ID,
)
from alphamind.distillation.regime import REGIME_BLOCK_ID

# ---------------------------------------------------------------------------
# Source-block prefixes used to route Q7 blocks into category sections
# ---------------------------------------------------------------------------
#
# Stories 08c / 08d pin these block_id namespaces; the assembler keys section
# routing off the prefix so adding a new block within an existing namespace
# (e.g., a new intermarket pair) requires no changes here.

_INTRA_SECTOR_CORRELATION_PREFIX = "q7.intra_sector_correlation."
_CROSS_SECTOR_ROTATION_PREFIX = "q7.cross_sector_rotation"
_INTERMARKET_REGIME_PREFIX = "q7.intermarket_regime."
_LEAD_LAG_PREFIX = "q7.lead_lag."
_CORRELATION_BREAKDOWN_PREFIX = "q7.correlation_breakdown."
_CORRELATION_LOCUS_PREFIX = "q7.correlation_locus."
_NARRATIVE_LAG_PREFIX = "q7.narrative_lag"


# ---------------------------------------------------------------------------
# Category enum — pins both section ordering and reference-index sort keys
# ---------------------------------------------------------------------------
#
# The four-tier ladder of cross-asset categories per
# ``docs/design/02-distillation-layer/external.md`` § Output format
# (category 7) plus the regime tier (universal context) the synthesizer
# always cites first. The enum doubles as the section ordering — earlier
# members render first; the deterministic CR-N sort keys derive from
# member position so adding a new category in the future is one edit.


class _Category(StrEnum):
    """Section categories the brief renders, in display order."""

    REGIME = "REGIME"
    INTRA_SECTOR_CORRELATION = "INTRA-SECTOR CORRELATION"
    CROSS_SECTOR_ROTATION = "CROSS-SECTOR ROTATION"
    INTERMARKET = "INTERMARKET REGIME SIGNALS"
    LEAD_LAG = "LEAD-LAG"
    CORRELATION_LOCUS = "LOCUS FLAGS"
    CORRELATION_REGIME_CHANGE = "CORRELATION REGIME CHANGE"
    NARRATIVE_LAG = "NARRATIVE LAG"


_CATEGORY_ORDER: tuple[_Category, ...] = (
    _Category.REGIME,
    _Category.INTRA_SECTOR_CORRELATION,
    _Category.CROSS_SECTOR_ROTATION,
    _Category.INTERMARKET,
    _Category.LEAD_LAG,
    _Category.CORRELATION_LOCUS,
    _Category.CORRELATION_REGIME_CHANGE,
    _Category.NARRATIVE_LAG,
)


def _category_rank(category: _Category) -> int:
    """Return the display-order index of ``category``.

    The rank only ever participates in sort keys, so the absolute integer
    is irrelevant — earlier in :data:`_CATEGORY_ORDER` sorts earlier.
    """
    return _CATEGORY_ORDER.index(category)


# ---------------------------------------------------------------------------
# Finding decomposition — block payload → one or more (CR-N target) tuples
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Finding:
    """One CR-N entry: a category, a source block, and an optional payload key.

    The decomposition rule from the story scope: a block may carry several
    findings in its payload (``q7.lead_lag.*`` carries one ``pair_lag``
    entry per pair); each finding gets its own ``CR-N``. A block with a
    single logical finding (``q7.cross_sector_rotation``) becomes a single
    ``CR-N``. The ``natural_key`` field captures the intra-payload key
    when disambiguation is needed (e.g., the pair key inside ``pair_lag``)
    and is :data:`None` for single-finding blocks.

    The reference-index value is built from ``source_block_id`` plus
    ``natural_key`` per the story scope: ``"q7.lead_lag::funding_to_credit"``
    when a pair key is present, plain ``"q7.lead_lag.funding_to_credit"``
    when the natural key is already encoded into the block_id.
    """

    category: _Category
    source_block_id: str
    natural_key: str | None
    summary: str
    detail_lines: tuple[str, ...]


# Routing table — exact-match block_ids first, then prefix matches. The
# tuple shape is (predicate-kind, value, category): "exact" matches the
# whole id, "prefix" matches the leading namespace. Order of evaluation
# is the iteration order of this tuple.
_CATEGORY_ROUTES: tuple[tuple[str, str, _Category], ...] = (
    ("exact", REGIME_BLOCK_ID, _Category.REGIME),
    ("exact", _CROSS_SECTOR_ROTATION_PREFIX, _Category.CROSS_SECTOR_ROTATION),
    ("exact", _NARRATIVE_LAG_PREFIX, _Category.NARRATIVE_LAG),
    ("prefix", _INTRA_SECTOR_CORRELATION_PREFIX, _Category.INTRA_SECTOR_CORRELATION),
    ("prefix", _INTERMARKET_REGIME_PREFIX, _Category.INTERMARKET),
    ("prefix", _LEAD_LAG_PREFIX, _Category.LEAD_LAG),
    ("prefix", _CORRELATION_LOCUS_PREFIX, _Category.CORRELATION_LOCUS),
    ("prefix", _CORRELATION_BREAKDOWN_PREFIX, _Category.CORRELATION_REGIME_CHANGE),
)


def _category_for_block_id(block_id: str) -> _Category | None:
    """Route a block_id into its CR brief category, or :data:`None`.

    Returns :data:`None` for blocks that do not contribute a CR-N entry
    (universal-broadcast blocks beyond ``regime.label`` belong in the
    trailing universal-context section, not in the CR-N enumeration).
    """
    for predicate, value, category in _CATEGORY_ROUTES:
        if predicate == "exact" and block_id == value:
            return category
        if predicate == "prefix" and block_id.startswith(value):
            return category
    return None


def _format_value(value: Any) -> str:
    """Render a leaf value with the same float-precision rule as ``output.py``."""
    if isinstance(value, float):
        return format(value, GENERAL_FLOAT_FORMAT)
    return str(value)


# ---------------------------------------------------------------------------
# Per-category finding extractors
# ---------------------------------------------------------------------------
#
# Each helper produces the (summary, detail_lines) pair that becomes the
# rendered CR-N body. Detail lines are sorted deterministically; the
# summary is a one-line headline the synthesizer can quote directly.


def _decompose_regime_block(block: OutputBlock) -> list[_Finding]:
    """Decompose the universal ``regime.label`` block into a single CR-1 finding."""
    payload = block.payload
    label = payload.get("regime_label", "")
    transition = payload.get("transition_state", "")
    agreement = payload.get("indicator_agreement_count", "")
    prior = payload.get("prior_label")
    prior_text = "n/a (first invocation)" if prior is None else str(prior)
    held = payload.get("invocations_held", "")
    skip = payload.get("regime_skip_emergency", False)

    # Render "unavailable" explicitly so downstream agents don't read
    # ``str(None)`` as a live percentile (ALP-571).
    vvix_value = payload.get("vvix_percentile")
    vvix_text = "unavailable" if vvix_value is None else _format_value(vvix_value)
    # Same treatment for the term-structure basis (ALP-572). The prior
    # 0.0 default was indistinguishable from a live "flat term structure"
    # reading; ``None`` surfaces the explicit missing-data signal.
    basis_value = payload.get("term_structure_basis")
    basis_text = "unavailable" if basis_value is None else _format_value(basis_value)

    summary = f"{label} ({transition}, indicator agreement {agreement}/4)"
    detail: list[str] = [
        f"Prior label: {prior_text}",
        f"Invocations held: {held}",
        (
            "Underlying: "
            f"VIX {_format_value(payload.get('vix_level', ''))}, "
            f"term-structure basis {basis_text}, "
            f"VVIX percentile {vvix_text}, "
            f"realized vol {_format_value(payload.get('realized_vol_5d', ''))}"
        ),
    ]
    if block.calibration_state is not CalibrationState.CALIBRATED and block.bootstrap_reason:
        detail.append(f"Signal quality: {block.calibration_state.value} ({block.bootstrap_reason})")
    if skip:
        detail.append("Emergency-skip flag active")

    return [
        _Finding(
            category=_Category.REGIME,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=tuple(detail),
        )
    ]


def _decompose_intra_sector_block(block: OutputBlock) -> list[_Finding]:
    """One finding per ``q7.intra_sector_correlation.<sector>`` block.

    The block's ``payload`` carries one sector's short/long correlation
    matrices; the section is fully described by a single CR-N entry.
    """
    sector = block.payload.get("sector", block.block_id)
    short_window = block.payload.get("short_window", {})
    long_window = block.payload.get("long_window", {})
    short_days = short_window.get("window_days", "") if isinstance(short_window, Mapping) else ""
    long_days = long_window.get("window_days", "") if isinstance(long_window, Mapping) else ""
    summary = f"{sector}: short-window vs. long-window correlation matrices"
    detail = (
        f"Short window: {short_days} days",
        f"Long window: {long_days} days",
    )
    return [
        _Finding(
            category=_Category.INTRA_SECTOR_CORRELATION,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=detail,
        )
    ]


def _decompose_cross_sector_rotation_block(block: OutputBlock) -> list[_Finding]:
    """The single rotation block becomes one CR-N entry."""
    velocity = block.payload.get("velocity_label", "")
    narrative = block.payload.get("narrative_label", "")
    summary = f"Cross-sector rotation: {narrative} ({velocity})"
    detail = (
        f"Narrative: {narrative}",
        f"Velocity: {velocity}",
    )
    return [
        _Finding(
            category=_Category.CROSS_SECTOR_ROTATION,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=detail,
        )
    ]


def _decompose_intermarket_block(block: OutputBlock) -> list[_Finding]:
    """One finding per intermarket pair block."""
    pair_key = block.block_id.removeprefix(_INTERMARKET_REGIME_PREFIX)
    payload = block.payload
    correlation = payload.get("correlation")
    regime_label = payload.get("regime_label")

    detail: list[str] = []
    if correlation is not None:
        detail.append(f"Correlation: {_format_value(correlation)}")
    if regime_label is not None:
        detail.append(f"Regime label: {regime_label}")
    if "long_beta" in payload:
        detail.append(f"Long beta: {_format_value(payload['long_beta'])}")
    if "short_beta" in payload:
        detail.append(f"Short beta: {_format_value(payload['short_beta'])}")
    if "beta_drift" in payload:
        detail.append(f"Beta drift: {_format_value(payload['beta_drift'])}")

    summary_parts: list[str] = [pair_key]
    if regime_label is not None:
        summary_parts.append(str(regime_label))
    elif correlation is not None:
        summary_parts.append(f"correlation {_format_value(correlation)}")
    summary = ": ".join(summary_parts)

    return [
        _Finding(
            category=_Category.INTERMARKET,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=tuple(detail),
        )
    ]


def _decompose_lead_lag_block(block: OutputBlock) -> list[_Finding]:
    """One finding per ``pair_lag`` entry inside a lead-lag block.

    Story 08d emits one block per pair (block_id encodes the pair key) and
    the payload's ``pair_lag`` mapping carries that one pair. We still
    decompose by ``pair_lag`` keys so a future block carrying multiple
    pairs in one payload would degrade gracefully.
    """
    pair_lag = block.payload.get("pair_lag", {})
    if not isinstance(pair_lag, Mapping):
        return []
    findings: list[_Finding] = []
    for pair_key in sorted(pair_lag):
        entry = pair_lag[pair_key]
        if not isinstance(entry, Mapping):
            continue
        lead = entry.get("lead_ticker", "")
        lag = entry.get("lag_ticker", "")
        days = entry.get("lead_lag_days_estimate", "")
        n_events = entry.get("n_pair_events", "")
        max_days = entry.get("max_days", "")
        summary = f"{pair_key}: {lead} → {lag}, lag {_format_value(days)} days"
        detail = (
            f"Lead ticker: {lead}",
            f"Lag ticker: {lag}",
            f"Lead-lag days estimate: {_format_value(days)}",
            f"N pair events: {n_events}",
            f"Max days: {max_days}",
        )
        findings.append(
            _Finding(
                category=_Category.LEAD_LAG,
                source_block_id=block.block_id,
                natural_key=pair_key,
                summary=summary,
                detail_lines=detail,
            )
        )
    return findings


def _decompose_dispersion_shift_block(block: OutputBlock) -> list[_Finding]:
    """One finding for the dispersion_shift block."""
    payload = block.payload
    today = payload.get("today_dispersion")
    mean_trailing = payload.get("mean_trailing_dispersion")
    stdev_trailing = payload.get("stdev_trailing_dispersion")
    zscore = payload.get("zscore")
    window_days = payload.get("dispersion_window_days")
    summary = (
        f"dispersion shift: today {_format_value(today)} "
        f"vs. trailing mean {_format_value(mean_trailing)} "
        f"(z-score {_format_value(zscore)})"
    )
    detail = (
        f"Today dispersion: {_format_value(today)}",
        f"Trailing mean dispersion: {_format_value(mean_trailing)}",
        f"Trailing stdev dispersion: {_format_value(stdev_trailing)}",
        f"Z-score: {_format_value(zscore)}",
        f"Window days: {window_days}",
    )
    return [
        _Finding(
            category=_Category.CORRELATION_REGIME_CHANGE,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=detail,
        )
    ]


def _decompose_correlation_breakdown_block(block: OutputBlock) -> list[_Finding]:
    """One finding per correlation-breakdown block (one block already = one pair).

    The dispersion_shift sibling lives under the same namespace prefix but
    carries a distinct payload schema — dispatch to its dedicated decomposer
    rather than treating the block as a correlation pair (ALP-546).
    """
    if block.block_id == DISPERSION_SHIFT_BLOCK_ID:
        return _decompose_dispersion_shift_block(block)
    payload = block.payload
    # The pair label is encoded into the block_id by the producer
    # (story 08d emits ``q7.correlation_breakdown.<lead>_<lag>``); parsing
    # from the id avoids any payload-shape coupling.
    pair_label = block.block_id.removeprefix(_CORRELATION_BREAKDOWN_PREFIX)
    short_corr = payload.get("short_correlation")
    long_corr = payload.get("long_correlation")
    deviation = payload.get("deviation_sigma")
    q_value = payload.get("q_value")
    overlap_n = payload.get("n_overlapping_observations")
    summary = (
        f"{pair_label}: short {_format_value(short_corr)} "
        f"vs. long {_format_value(long_corr)} "
        f"(deviation {_format_value(deviation)} sigma)"
    )
    detail = (
        f"Short correlation: {_format_value(short_corr)}",
        f"Long correlation: {_format_value(long_corr)}",
        f"Deviation sigma: {_format_value(deviation)}",
        f"FDR q-value: {_format_value(q_value)}",
        f"Overlapping observations: {overlap_n}",
    )
    return [
        _Finding(
            category=_Category.CORRELATION_REGIME_CHANGE,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=detail,
        )
    ]


def _decompose_correlation_locus_block(block: OutputBlock) -> list[_Finding]:
    """One CR-N entry per locus block.

    Renders the rolled-up "ticker X is the dislocation locus" finding the
    locus-aggregation pass emits when a single ticker appears in the
    configured number of pair-wise breakdowns. The summary names the
    locus ticker and pair count; the detail lines surface max sigma,
    partner tickers, and (when sector context is available) the
    per-sector grouping plus cross-sector spread.
    """
    payload = block.payload
    locus_ticker = payload.get("locus_ticker", "")
    pair_count = payload.get("pair_count")
    max_sigma = payload.get("max_deviation_sigma")
    partners = payload.get("partner_tickers") or ()
    partners_by_sector = payload.get("partners_by_sector")
    cross_sector_spread = payload.get("cross_sector_spread")

    summary = (
        f"{locus_ticker}: locus of {_format_value(pair_count)} pair-wise breakdowns "
        f"(max deviation {_format_value(max_sigma)} sigma)"
    )
    detail: list[str] = [
        f"Locus ticker: {locus_ticker}",
        f"Pair count: {_format_value(pair_count)}",
        f"Max deviation sigma: {_format_value(max_sigma)}",
        f"Partner tickers: {', '.join(partners) if partners else ''}",
    ]
    if isinstance(partners_by_sector, Mapping):
        for sector in sorted(partners_by_sector):
            sector_partners = partners_by_sector[sector]
            detail.append(f"Sector {sector}: {', '.join(sector_partners)}")
    if cross_sector_spread:
        detail.append(f"Cross-sector spread: {cross_sector_spread}")

    return [
        _Finding(
            category=_Category.CORRELATION_LOCUS,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=tuple(detail),
        )
    ]


def _decompose_narrative_lag_block(block: OutputBlock) -> list[_Finding]:
    """The narrative-lag block contributes one CR-N entry to its own section."""
    payload = block.payload
    qualifying = payload.get("qualifying_news_present", False)
    breakdowns = payload.get("n_breakdowns", 0)
    silence_hours = payload.get("media_silence_hours", "")
    summary = (
        "narrative lag: "
        f"{breakdowns} breakdowns, "
        f"qualifying news {'present' if qualifying else 'silent'}"
    )
    detail = (
        f"Media silence hours: {silence_hours}",
        f"Qualifying news present: {qualifying}",
        f"N breakdowns: {breakdowns}",
    )
    return [
        _Finding(
            category=_Category.NARRATIVE_LAG,
            source_block_id=block.block_id,
            natural_key=None,
            summary=summary,
            detail_lines=detail,
        )
    ]


_DECOMPOSERS: dict[_Category, Callable[[OutputBlock], list[_Finding]]] = {
    _Category.REGIME: _decompose_regime_block,
    _Category.INTRA_SECTOR_CORRELATION: _decompose_intra_sector_block,
    _Category.CROSS_SECTOR_ROTATION: _decompose_cross_sector_rotation_block,
    _Category.INTERMARKET: _decompose_intermarket_block,
    _Category.LEAD_LAG: _decompose_lead_lag_block,
    _Category.CORRELATION_LOCUS: _decompose_correlation_locus_block,
    _Category.CORRELATION_REGIME_CHANGE: _decompose_correlation_breakdown_block,
    _Category.NARRATIVE_LAG: _decompose_narrative_lag_block,
}


# ---------------------------------------------------------------------------
# Public dataclass + assembly entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CorrelationRegimeBrief:
    """The synthesizer's correlation/regime brief.

    Fields:

    - ``text`` — the assembled brief body. Byte-identical for the same
      input across repeated calls so the invocation archive diffs cleanly
      per ``docs/architecture/infrastructure.md`` § Invocation archive.
    - ``reference_index`` — mapping from ``CR-{N}`` reference IDs to the
      source ``block_id`` (or ``block_id::natural_key`` when an
      intra-payload key disambiguates a multi-finding block). Populates
      the synthesizer's retrieval store per
      ``docs/design/03-analysis-layer/synthesizer.md`` § Retrieval store
      side-effect; lifetime is one invocation.
    - ``freshness_min`` — oldest contributing block's ``freshness_ts``.
    """

    text: str
    reference_index: dict[str, str]
    freshness_min: datetime


def _decompose_block(block: OutputBlock) -> list[_Finding]:
    """Route ``block`` to its category's decomposer and return the findings."""
    category = _category_for_block_id(block.block_id)
    if category is None:
        return []
    return _DECOMPOSERS[category](block)


def _finding_sort_key(finding: _Finding) -> tuple[int, str, str]:
    """Sort findings deterministically: category, then block_id, then natural key.

    Per the story scope: "sort findings by category then by source
    ``block_id`` then by intra-payload natural key." Empty natural keys
    sort first (a single-finding block is "before" any multi-finding
    siblings under the same source block).
    """
    return (
        _category_rank(finding.category),
        finding.source_block_id,
        finding.natural_key or "",
    )


def _reference_value(finding: _Finding) -> str:
    """Build the ``reference_index`` value for ``finding``.

    When ``natural_key`` is present, render the value as
    ``block_id::natural_key`` per the story scope; otherwise use
    ``block_id`` plain.
    """
    if finding.natural_key is None:
        return finding.source_block_id
    return f"{finding.source_block_id}::{finding.natural_key}"


EMPTY_FINDINGS_MARKER = "(no findings)"


def _render_universal_context(blocks: Iterable[OutputBlock]) -> str:
    """Render the trailing universal-context section.

    Excludes the ``regime.label`` block — it is already embedded in the
    REGIME section as ``CR-1``. Other universal-broadcast blocks (macro,
    breadth, intermarket-when-broadcast) appear without ``CR-N`` IDs;
    the synthesizer cites them via the underlying source block IDs. When
    no universal-broadcast blocks beyond ``regime.label`` exist, the
    section header is still emitted with an explicit empty-state marker
    so the section never silently drops (ALP-577). The leading newline
    produces the blank-line separator between the last CR-N section and
    the universal-context header that every other section pair already has.
    """
    body = format_blocks_for_audience(
        (block for block in blocks if block.block_id != REGIME_BLOCK_ID),
        OutputAudience.UNIVERSAL_BROADCAST,
    )
    if not body:
        return f"\n=== UNIVERSAL CONTEXT ===\n{EMPTY_UNIVERSAL_CONTEXT_MARKER}\n"
    return "\n=== UNIVERSAL CONTEXT ===\n" + body


def _render_anomaly_summary(blocks: Iterable[OutputBlock]) -> str:
    """Render the anomaly-summary section scoped to the CR brief's audience.

    Reuses the story-10 aggregation primitives so the layout matches the
    rest of the distillation layer's anomaly rendering.
    """
    summaries = collect_anomalies(blocks)
    grouped = group_anomalies_by_audience(summaries)
    audience_summaries = grouped.get(OutputAudience.CORRELATION_REGIME_BRIEF, [])
    return format_anomaly_summary(audience_summaries)


def assemble_correlation_brief(
    blocks: Iterable[OutputBlock],
    invocation_id: str,
) -> CorrelationRegimeBrief:
    """Assemble the correlation/regime brief from the day's output blocks.

    Filtering: every block whose ``audience`` contains
    :attr:`OutputAudience.CORRELATION_REGIME_BRIEF`, plus the universal
    ``regime.label`` block (always embedded as the regime context the
    synthesizer reasons over). Universal-broadcast blocks beyond
    ``regime.label`` flow into the trailing universal-context section
    without ``CR-N`` IDs.

    Decomposition: each block contributes one or more :class:`_Finding`
    instances per the story rules — multi-payload blocks (lead-lag pairs)
    yield one finding per pair, single-finding blocks yield one. Findings
    sort by category → block_id → natural key, then receive sequential
    ``CR-1``, ``CR-2``, ... ``CR-N`` references.

    Reference index: each ``CR-N`` maps back to the originating block —
    plain ``block_id`` for single-finding blocks,
    ``block_id::natural_key`` when an intra-payload key disambiguates
    multi-finding blocks.

    Section contract (ALP-577): every section renders at its fixed
    position with either findings or an explicit empty-state marker —
    never silently drops. CR-N category sections use
    :data:`EMPTY_FINDINGS_MARKER`; the universal-context section uses
    :data:`EMPTY_UNIVERSAL_CONTEXT_MARKER`; the anomaly-flags section
    embeds the count in its header (``=== ANOMALY FLAGS (0) ===`` for the
    zero case).

    Per the story scope "byte-identical across repeated calls" — the
    document is sorted deterministically, floats use the named precision
    constants, and the assembler never embeds the current wall clock.
    """
    materialized = list(blocks)

    cr_blocks = [
        block
        for block in materialized
        if (
            OutputAudience.CORRELATION_REGIME_BRIEF in block.audience
            or block.block_id == REGIME_BLOCK_ID
        )
    ]

    findings: list[_Finding] = []
    for block in cr_blocks:
        findings.extend(_decompose_block(block))
    findings.sort(key=_finding_sort_key)

    reference_index: dict[str, str] = {}
    cr_entries_by_category: dict[_Category, list[tuple[str, _Finding]]] = {
        category: [] for category in _CATEGORY_ORDER
    }
    for index, finding in enumerate(findings, start=1):
        reference_id = f"CR-{index}"
        reference_index[reference_id] = _reference_value(finding)
        cr_entries_by_category[finding.category].append((reference_id, finding))

    freshness_min = min(block.freshness_ts for block in cr_blocks)

    lines: list[str] = [
        "CORRELATION & REGIME BRIEF",
        f"Invocation: {invocation_id}",
        f"Effective freshness: {freshness_min.isoformat()}",
        "",
    ]

    for category in _CATEGORY_ORDER:
        lines.append(f"=== {category.value} ===")
        entries = cr_entries_by_category[category]
        if not entries:
            lines.append(EMPTY_FINDINGS_MARKER)
        else:
            for reference_id, finding in entries:
                lines.append(f"[{reference_id}] {finding.summary}")
                for detail in finding.detail_lines:
                    lines.append(f"  {detail}")
        lines.append("")

    document = "\n".join(lines) + _render_universal_context(materialized) + "\n"
    document += _render_anomaly_summary(cr_blocks)

    return CorrelationRegimeBrief(
        text=document,
        reference_index=reference_index,
        freshness_min=freshness_min,
    )


__all__ = [
    "CorrelationRegimeBrief",
    "assemble_correlation_brief",
]

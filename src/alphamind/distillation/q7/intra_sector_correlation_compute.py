"""Pure compute for Q7 intra-sector correlation matrices (ALP-486).

Splits the intra-sector-correlation compute path along the compute/load
boundary. This module is ORM-free; the session-bound read + write surface
lives in :mod:`.intra_sector_correlation_loaders` — that loader is
responsible for persisting one ``correlation_divergence`` event per pair
flag the pure compute emits.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime

from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.q7._correlation_locus import (
    CorrelationLocusContribution,
    build_correlation_locus_block,
)
from alphamind.distillation.q7._helpers import (
    _BLOCK_NAMESPACE,
    _calibration_for_window,
    _correlation_matrix,
)

# Pair-divergence flag name prefix shared by emitter and locus-aggregation pass.
_DIVERGENCE_FLAG_PREFIX = "intra_sector_correlation_divergence"


def _detect_pair_divergence(
    *,
    short_matrix: Mapping[str, Mapping[str, float]],
    long_matrix: Mapping[str, Mapping[str, float]],
    divergence_sigma: float,
) -> list[AnomalyFlag]:
    """Emit one :class:`AnomalyFlag` per sector pair whose correlation diverged.

    The baseline distribution is the population of off-diagonal long-window
    correlations within the sector; a pair fires when its short-window
    correlation deviates from the long-window value by at least
    ``divergence_sigma`` multiples of that distribution's standard deviation.
    """
    tickers = sorted(short_matrix)
    long_values: list[float] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            long_values.append(long_matrix[row][col])
    # Pairs require at least one off-diagonal entry (i.e., a non-singleton
    # ticker set); pstdev needs at least one observation to be defined.
    if not long_values:
        return []
    sigma = statistics.pstdev(long_values)
    if sigma == 0.0:
        return []
    flags: list[AnomalyFlag] = []
    for i, row in enumerate(tickers):
        for col in tickers[i + 1 :]:
            short_corr = short_matrix[row][col]
            long_corr = long_matrix[row][col]
            deviation = abs(short_corr - long_corr)
            magnitude = deviation / sigma
            if magnitude >= divergence_sigma:
                flags.append(
                    AnomalyFlag(
                        name=f"{_DIVERGENCE_FLAG_PREFIX}:{row}:{col}",
                        magnitude=magnitude,
                        severity="investigate_if_persists",
                    )
                )
    return flags


def compute_intra_sector_correlation_pure(
    *,
    sector: str,
    sector_tickers: Sequence[str],
    long_returns_by_ticker: Mapping[str, Sequence[float]],
    short_window_days: int,
    long_window_days: int,
    divergence_sigma: float,
    as_of: datetime,
) -> OutputBlock:
    """Pure compute of the per-sector intra-sector correlation block.

    Operates entirely over the pre-loaded long-window log-return series
    for the sector's tickers. The short window is sliced from the tail of
    the long-window series.

    The pair-divergence detector attaches :class:`AnomalyFlag` instances
    to the block; the loader is responsible for writing the corresponding
    ``correlation_divergence`` event rows synchronously under the shared
    session before the parallel compute fires.
    """
    del sector_tickers  # informational; long_returns_by_ticker enumerates the sector

    short_returns: dict[str, Sequence[float]] = {}
    short_n_min: float = math.inf
    long_n_min: float = math.inf
    for ticker, returns in long_returns_by_ticker.items():
        short_returns[ticker] = list(returns)[-short_window_days:]
        short_n_min = min(short_n_min, len(short_returns[ticker]))
        long_n_min = min(long_n_min, len(returns))

    short_matrix = _correlation_matrix(short_returns)
    long_matrix = _correlation_matrix(long_returns_by_ticker)

    short_n = 0 if short_n_min is math.inf else int(short_n_min)
    long_n = 0 if long_n_min is math.inf else int(long_n_min)
    state, reason = _calibration_for_window(
        n_observations=min(short_n, long_n),
        required=short_window_days,
        input_name="correlation_short_days",
    )

    flags = _detect_pair_divergence(
        short_matrix=short_matrix,
        long_matrix=long_matrix,
        divergence_sigma=divergence_sigma,
    )

    payload = {
        "short_window": {
            "correlation_matrix": short_matrix,
            "window_days": short_window_days,
            "n_observations": short_n,
        },
        "long_window": {
            "correlation_matrix": long_matrix,
            "window_days": long_window_days,
            "n_observations": long_n,
        },
        "sector": sector,
    }
    return OutputBlock(
        block_id=f"{_BLOCK_NAMESPACE}.intra_sector_correlation.{sector}",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=as_of,
        calibration_state=state,
        bootstrap_reason=reason,
        payload=payload,
        anomaly_flags=tuple(flags),
        regime_context=None,
    )


def _parse_divergence_flag(flag: AnomalyFlag) -> tuple[str, str] | None:
    """Return ``(row, col)`` parsed from a divergence flag name, or :data:`None`."""
    try:
        prefix, row, col = flag.name.split(":")
    except ValueError:
        return None
    if prefix != _DIVERGENCE_FLAG_PREFIX:
        return None
    return row, col


def apply_intra_sector_locus_aggregation(
    *,
    sector_blocks: Sequence[OutputBlock],
    pair_count_threshold: int,
    as_of: datetime,
) -> tuple[OutputBlock, ...]:
    """Roll per-sector divergence pairs into per-ticker locus blocks.

    Walks each sector block's
    ``intra_sector_correlation_divergence:<row>:<col>`` flags, counts
    ticker occurrences, and emits one ``q7.correlation_locus.<ticker>``
    block per ticker reaching ``pair_count_threshold``. The contributing
    per-pair flags are suppressed from the sector block's
    ``anomaly_flags`` rollup so the synthesizer sees one rolled-up signal
    rather than N independent-looking pair flags — matching the
    cross-universe ``correlation_breakdown`` locus path (ALP-543).

    Returns the filtered sector blocks first (preserving input order),
    followed by the per-ticker locus blocks in deterministic ticker-sort
    order. Sector blocks without any locus ticker are passed through
    unchanged.

    All partners of a per-sector locus share the sector by construction,
    so each locus block carries a single-key ``partners_by_sector`` and a
    ``"<sector>-only"`` ``cross_sector_spread`` — the same payload shape
    the cross-universe path uses.
    """
    out_sector_blocks: list[OutputBlock] = []
    locus_blocks: list[OutputBlock] = []
    for block in sector_blocks:
        sector = str(block.payload.get("sector", ""))
        parsed: list[tuple[str, str, AnomalyFlag]] = []
        other_flags: list[AnomalyFlag] = []
        for flag in block.anomaly_flags:
            pair = _parse_divergence_flag(flag)
            if pair is None:
                other_flags.append(flag)
                continue
            row, col = pair
            parsed.append((row, col, flag))

        ticker_counts: Counter[str] = Counter()
        for row, col, _flag in parsed:
            ticker_counts[row] += 1
            ticker_counts[col] += 1
        locus_tickers = frozenset(
            ticker for ticker, count in ticker_counts.items() if count >= pair_count_threshold
        )

        if not locus_tickers:
            out_sector_blocks.append(block)
            continue

        contributions_by_locus: dict[str, list[CorrelationLocusContribution]] = {
            ticker: [] for ticker in locus_tickers
        }
        supporting_by_locus: dict[str, list[str]] = {ticker: [] for ticker in locus_tickers}
        for row, col, flag in parsed:
            for ticker in (row, col):
                if ticker in locus_tickers:
                    contributions_by_locus[ticker].append(
                        CorrelationLocusContribution(
                            row=row, col=col, magnitude=float(flag.magnitude)
                        )
                    )
                    supporting_by_locus[ticker].append(flag.name)

        # Every ticker appearing in this sector's pairs maps to this sector by
        # construction; build the mapping once and reuse across loci.
        sector_by_ticker = dict.fromkeys(
            {ticker for row, col, _ in parsed for ticker in (row, col)},
            sector,
        )
        for locus_ticker in sorted(locus_tickers):
            locus_blocks.append(
                build_correlation_locus_block(
                    locus_ticker=locus_ticker,
                    contributions=tuple(contributions_by_locus[locus_ticker]),
                    sector_by_ticker=sector_by_ticker,
                    supporting_pair_ids=tuple(sorted(supporting_by_locus[locus_ticker])),
                    as_of=as_of,
                )
            )

        # Per-pair flags involving any locus ticker are suppressed; non-locus
        # pairs continue to publish on the sector block's rollup.
        filtered_divergence = [
            flag
            for row, col, flag in parsed
            if row not in locus_tickers and col not in locus_tickers
        ]
        out_sector_blocks.append(
            OutputBlock(
                block_id=block.block_id,
                audience=block.audience,
                freshness_ts=block.freshness_ts,
                calibration_state=block.calibration_state,
                bootstrap_reason=block.bootstrap_reason,
                payload=block.payload,
                anomaly_flags=tuple(other_flags) + tuple(filtered_divergence),
                regime_context=block.regime_context,
            )
        )

    return tuple(out_sector_blocks) + tuple(locus_blocks)


__all__ = [
    "apply_intra_sector_locus_aggregation",
    "compute_intra_sector_correlation_pure",
]

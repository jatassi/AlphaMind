"""Freshness contract for portfolio state snapshots.

Three frozen-dataclass value objects and one pure function:

- ``PriceFetchOutcomes``   — per-position price-fetch record (assembler accumulator)
- ``SnapshotFreshness``   — typed sidecar reporting how fresh the snapshot's data is
- ``AssembledSnapshot``   — bundle of (snapshot, freshness, price_map) returned by
  assemble_snapshot
- ``compute_snapshot_freshness`` — builds SnapshotFreshness from snapshot + outcomes + config
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alphamind.portfolio_state import PortfolioStateConfig
from alphamind.portfolio_state.pricing import PriceQuote
from alphamind.portfolio_state.snapshot import PortfolioStateSnapshot


def _assert_pairwise_disjoint(
    fresh: frozenset[str],
    stale: frozenset[str],
    unknown: frozenset[str],
) -> None:
    """Raise ValueError if any two of the three position-ID sets overlap."""
    overlap_fs = fresh & stale
    overlap_fu = fresh & unknown
    overlap_su = stale & unknown
    if overlap_fs or overlap_fu or overlap_su:
        msg = (
            f"position ID sets must be pairwise disjoint; "
            f"overlaps: fresh∩stale={overlap_fs!r}, "
            f"fresh∩unknown={overlap_fu!r}, "
            f"stale∩unknown={overlap_su!r}"
        )
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# PriceFetchOutcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PriceFetchOutcomes:
    """Assembler's per-position price-fetch record — produced during Step 5/6."""

    position_ids_priced_fresh: frozenset[str]
    position_ids_priced_stale: frozenset[str]
    position_ids_unknown_ticker: frozenset[str]
    oldest_price_as_of: datetime | None

    def __post_init__(self) -> None:
        _assert_pairwise_disjoint(
            self.position_ids_priced_fresh,
            self.position_ids_priced_stale,
            self.position_ids_unknown_ticker,
        )
        if self.oldest_price_as_of is not None:
            ts = self.oldest_price_as_of
            if ts.tzinfo is None or ts.utcoffset() is None:
                msg = "oldest_price_as_of must be tz-aware UTC when not None"
                raise ValueError(msg)


# ---------------------------------------------------------------------------
# SnapshotFreshness
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SnapshotFreshness:
    """Typed sidecar reporting how fresh the snapshot's data is."""

    # Invocation timing
    phase1_committed_at: datetime
    snapshot_assembled_at: datetime
    phase1_to_snapshot_seconds: float
    max_phase1_to_snapshot_seconds: float
    phase1_to_snapshot_within_threshold: bool

    # Position counts
    total_open_positions: int
    total_pending_positions: int
    total_positions: int

    # Per-position classification
    position_ids_priced_fresh: frozenset[str]
    position_ids_priced_stale: frozenset[str]
    position_ids_unknown_ticker: frozenset[str]

    # At-a-glance counters
    count_priced_fresh: int
    count_priced_stale: int
    count_unknown_ticker: int
    all_position_prices_fresh: bool

    # Price age
    oldest_price_as_of: datetime | None
    oldest_price_age_seconds: float | None
    max_price_age_seconds: float

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        self._check_phase1_to_snapshot_non_negative()
        self._check_total_positions()
        self._check_count_conservation()
        _assert_pairwise_disjoint(
            self.position_ids_priced_fresh,
            self.position_ids_priced_stale,
            self.position_ids_unknown_ticker,
        )
        self._check_oldest_price_as_of()
        self._check_oldest_price_age_consistency()

    def _check_phase1_to_snapshot_non_negative(self) -> None:
        if self.phase1_to_snapshot_seconds < 0:
            msg = f"phase1_to_snapshot_seconds must be >= 0; got {self.phase1_to_snapshot_seconds}"
            raise ValueError(msg)

    def _check_total_positions(self) -> None:
        expected = self.total_open_positions + self.total_pending_positions
        if self.total_positions != expected:
            msg = (
                f"total_positions ({self.total_positions}) must equal "
                f"total_open_positions ({self.total_open_positions}) + "
                f"total_pending_positions ({self.total_pending_positions}) = {expected}"
            )
            raise ValueError(msg)

    def _check_count_conservation(self) -> None:
        total = self.count_priced_fresh + self.count_priced_stale + self.count_unknown_ticker
        if total != self.total_positions:
            msg = (
                f"count_priced_fresh ({self.count_priced_fresh}) + "
                f"count_priced_stale ({self.count_priced_stale}) + "
                f"count_unknown_ticker ({self.count_unknown_ticker}) = {total} "
                f"must equal total_positions ({self.total_positions})"
            )
            raise ValueError(msg)

    def _check_oldest_price_as_of(self) -> None:
        if self.oldest_price_as_of is not None:
            ts = self.oldest_price_as_of
            if ts.tzinfo is None or ts.utcoffset() is None:
                msg = "oldest_price_as_of must be tz-aware UTC when not None"
                raise ValueError(msg)
            if ts > self.snapshot_assembled_at:
                msg = (
                    f"oldest_price_as_of ({ts}) must be <= "
                    f"snapshot_assembled_at ({self.snapshot_assembled_at})"
                )
                raise ValueError(msg)

    def _check_oldest_price_age_consistency(self) -> None:
        if (self.oldest_price_age_seconds is None) != (self.oldest_price_as_of is None):
            msg = (
                "oldest_price_age_seconds is None if and only if oldest_price_as_of is None; "
                f"got oldest_price_age_seconds={self.oldest_price_age_seconds!r}, "
                f"oldest_price_as_of={self.oldest_price_as_of!r}"
            )
            raise ValueError(msg)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def is_position_price_stale(self, position_id: str) -> bool:
        """Return True for stale and unknown-ticker position IDs, False otherwise.

        Never raises — returns False for unknown position IDs.
        """
        return position_id in (self.position_ids_priced_stale | self.position_ids_unknown_ticker)

    def staleness_summary(self) -> str:
        """Return a short human-readable summary of freshness.

        Format (all fresh):
            "phase1→snapshot 2.3s (within 30.0s); 13/13 positions priced fresh"
        Format (some stale):
            "phase1→snapshot 2.3s (within 30.0s); 12/13 positions priced fresh; 1 stale (POS-A)"
        """
        stale_ids = self.position_ids_priced_stale | self.position_ids_unknown_ticker
        summary = (
            f"phase1→snapshot {self.phase1_to_snapshot_seconds:.1f}s"
            f" (within {self.max_phase1_to_snapshot_seconds:.1f}s);"
            f" {self.count_priced_fresh}/{self.total_positions} positions priced fresh"
        )
        if stale_ids:
            summary += f"; {len(stale_ids)} stale ({', '.join(sorted(stale_ids))})"
        return summary


# ---------------------------------------------------------------------------
# compute_snapshot_freshness
# ---------------------------------------------------------------------------


def compute_snapshot_freshness(
    snapshot: PortfolioStateSnapshot,
    *,
    fetch_outcomes: PriceFetchOutcomes,
    config: PortfolioStateConfig,
) -> SnapshotFreshness:
    """Build SnapshotFreshness from the snapshot's timestamps, per-position fetch outcomes,
    and config thresholds. Pure function, no I/O.

    Raises:
        ValueError: when fetch_outcomes references a position_id not in the snapshot, or
                    when the snapshot has a position not classified in any fetch-outcome set.
    """
    snapshot_position_ids: frozenset[str] = frozenset(
        p.position_id for p in (*snapshot.open_positions, *snapshot.pending_positions)
    )
    all_outcome_ids = (
        fetch_outcomes.position_ids_priced_fresh
        | fetch_outcomes.position_ids_priced_stale
        | fetch_outcomes.position_ids_unknown_ticker
    )

    # Check fetch_outcomes doesn't reference unknown positions
    extra = all_outcome_ids - snapshot_position_ids
    if extra:
        offending = next(iter(extra))
        msg = (
            f"fetch_outcomes references position_id not in snapshot: {offending!r}; "
            f"all extra: {extra!r}"
        )
        raise ValueError(msg)

    # Check every snapshot position is classified
    missing = snapshot_position_ids - all_outcome_ids
    if missing:
        offending = next(iter(missing))
        msg = (
            f"snapshot position_id not classified in fetch_outcomes: {offending!r}; "
            f"all missing: {missing!r}"
        )
        raise ValueError(msg)

    phase1_to_snapshot = (
        snapshot.snapshot_assembled_at - snapshot.phase1_committed_at
    ).total_seconds()
    max_p1s = config.snapshot_freshness_max_phase1_to_snapshot_seconds

    total_open = len(snapshot.open_positions)
    total_pending = len(snapshot.pending_positions)
    total_positions = total_open + total_pending

    count_fresh = len(fetch_outcomes.position_ids_priced_fresh)
    count_stale = len(fetch_outcomes.position_ids_priced_stale)
    count_unknown = len(fetch_outcomes.position_ids_unknown_ticker)

    oldest = fetch_outcomes.oldest_price_as_of
    oldest_age: float | None = None
    if oldest is not None:
        oldest_age = (snapshot.snapshot_assembled_at - oldest).total_seconds()

    return SnapshotFreshness(
        phase1_committed_at=snapshot.phase1_committed_at,
        snapshot_assembled_at=snapshot.snapshot_assembled_at,
        phase1_to_snapshot_seconds=phase1_to_snapshot,
        max_phase1_to_snapshot_seconds=max_p1s,
        phase1_to_snapshot_within_threshold=phase1_to_snapshot <= max_p1s,
        total_open_positions=total_open,
        total_pending_positions=total_pending,
        total_positions=total_positions,
        position_ids_priced_fresh=fetch_outcomes.position_ids_priced_fresh,
        position_ids_priced_stale=fetch_outcomes.position_ids_priced_stale,
        position_ids_unknown_ticker=fetch_outcomes.position_ids_unknown_ticker,
        count_priced_fresh=count_fresh,
        count_priced_stale=count_stale,
        count_unknown_ticker=count_unknown,
        all_position_prices_fresh=(count_stale == 0 and count_unknown == 0),
        oldest_price_as_of=oldest,
        oldest_price_age_seconds=oldest_age,
        max_price_age_seconds=config.snapshot_freshness_max_price_age_seconds,
    )


# ---------------------------------------------------------------------------
# AssembledSnapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AssembledSnapshot:
    """Bundle of (snapshot, freshness, price_map) returned by assemble_snapshot.

    ``price_map`` exposes the assembler-internal ticker → :class:`PriceQuote`
    mapping the assembler already fetched while building the snapshot, so
    downstream consumers (e.g., the decision pipeline composition) can reuse
    the materialized quotes instead of re-querying the price provider. The
    field is typed ``dict`` (not ``Mapping``) because the prior Pydantic model
    stored a plain ``dict`` regardless of annotation; ``frozen=True`` blocks
    field reassignment but does not prevent dict mutation, so the type
    honestly reflects runtime behavior.
    """

    snapshot: PortfolioStateSnapshot
    freshness: SnapshotFreshness
    price_map: dict[str, PriceQuote]


__all__ = [
    "AssembledSnapshot",
    "PriceFetchOutcomes",
    "SnapshotFreshness",
    "compute_snapshot_freshness",
]

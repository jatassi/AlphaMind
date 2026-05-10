"""Freshness contract for portfolio state snapshots (story 08).

Three Pydantic value objects and one pure function:

- ``PriceFetchOutcomes``   — per-position price-fetch record (assembler accumulator)
- ``SnapshotFreshness``   — typed sidecar reporting how fresh the snapshot's data is
- ``AssembledSnapshot``   — bundle of (snapshot, freshness, price_map) returned by
  assemble_snapshot
- ``compute_snapshot_freshness`` — builds SnapshotFreshness from snapshot + outcomes + config
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

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


class PriceFetchOutcomes(BaseModel):
    """Assembler's per-position price-fetch record — produced during Step 5/6."""

    model_config = ConfigDict(frozen=True)

    position_ids_priced_fresh: frozenset[str]
    position_ids_priced_stale: frozenset[str]
    position_ids_unknown_ticker: frozenset[str]
    oldest_price_as_of: datetime | None

    @model_validator(mode="after")
    def _validate_disjoint_sets(self) -> PriceFetchOutcomes:
        _assert_pairwise_disjoint(
            self.position_ids_priced_fresh,
            self.position_ids_priced_stale,
            self.position_ids_unknown_ticker,
        )
        return self

    @model_validator(mode="after")
    def _validate_oldest_price_tz(self) -> PriceFetchOutcomes:
        if self.oldest_price_as_of is not None:
            ts = self.oldest_price_as_of
            if ts.tzinfo is None or ts.utcoffset() is None:
                msg = "oldest_price_as_of must be tz-aware UTC when not None"
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# SnapshotFreshness
# ---------------------------------------------------------------------------


class SnapshotFreshness(BaseModel):
    """Typed sidecar reporting how fresh the snapshot's data is."""

    model_config = ConfigDict(frozen=True)

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

    @model_validator(mode="after")
    def _validate_phase1_to_snapshot_non_negative(self) -> SnapshotFreshness:
        if self.phase1_to_snapshot_seconds < 0:
            msg = f"phase1_to_snapshot_seconds must be >= 0; got {self.phase1_to_snapshot_seconds}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_total_positions(self) -> SnapshotFreshness:
        expected = self.total_open_positions + self.total_pending_positions
        if self.total_positions != expected:
            msg = (
                f"total_positions ({self.total_positions}) must equal "
                f"total_open_positions ({self.total_open_positions}) + "
                f"total_pending_positions ({self.total_pending_positions}) = {expected}"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_count_conservation(self) -> SnapshotFreshness:
        total = self.count_priced_fresh + self.count_priced_stale + self.count_unknown_ticker
        if total != self.total_positions:
            msg = (
                f"count_priced_fresh ({self.count_priced_fresh}) + "
                f"count_priced_stale ({self.count_priced_stale}) + "
                f"count_unknown_ticker ({self.count_unknown_ticker}) = {total} "
                f"must equal total_positions ({self.total_positions})"
            )
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_disjoint_sets(self) -> SnapshotFreshness:
        _assert_pairwise_disjoint(
            self.position_ids_priced_fresh,
            self.position_ids_priced_stale,
            self.position_ids_unknown_ticker,
        )
        return self

    @model_validator(mode="after")
    def _validate_oldest_price_as_of(self) -> SnapshotFreshness:
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
        return self

    @model_validator(mode="after")
    def _validate_oldest_price_age_consistency(self) -> SnapshotFreshness:
        if (self.oldest_price_age_seconds is None) != (self.oldest_price_as_of is None):
            msg = (
                "oldest_price_age_seconds is None if and only if oldest_price_as_of is None; "
                f"got oldest_price_age_seconds={self.oldest_price_age_seconds!r}, "
                f"oldest_price_as_of={self.oldest_price_as_of!r}"
            )
            raise ValueError(msg)
        return self

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


class AssembledSnapshot(BaseModel):
    """Bundle of (snapshot, freshness, price_map) returned by assemble_snapshot.

    ``price_map`` exposes the assembler-internal ticker → :class:`PriceQuote`
    mapping the assembler already fetched while building the snapshot, so
    downstream consumers (e.g., the decision pipeline composition) can reuse
    the materialized quotes instead of re-querying the price provider.
    """

    model_config = ConfigDict(frozen=True)

    snapshot: PortfolioStateSnapshot
    freshness: SnapshotFreshness
    price_map: Mapping[str, PriceQuote]


__all__ = [
    "AssembledSnapshot",
    "PriceFetchOutcomes",
    "SnapshotFreshness",
    "compute_snapshot_freshness",
]

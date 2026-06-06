"""Per-contract IV snapshot lookup for the counterfactual replay engine (ALP-561).

Provides:

* :class:`IVSnapshotLookupResult` — typed result carrying the resolved IV,
  the snapshot timestamp, the underlying price at that snapshot, and the lag
  (in minutes) between the snapshot and the requested target timestamp.

* :func:`resolve_contract_ticker` — pure function that builds the canonical
  Polygon OCC symbol ``O:{UNDERLYING}{YYMMDD}{C|P}{round(strike*1000):08d}``.
  Reuses the same encoding as
  :func:`~alphamind.portfolio_state.records.positions.occ_symbol_for_options`
  so the resolver never does float-equality on ``strike_price``.

* :func:`lookup_iv_at_timestamp` — selects the nearest at-or-before snapshot
  with a non-null ``implied_volatility`` from ``options_contract_snapshots``.

* :class:`SqlOptionsSnapshotRepository` — synchronous SQLAlchemy Session
  implementation of the widened :class:`~.repos.OptionsSnapshotRepository`
  Protocol; injectable into the engine driver (story 08) and into eligibility
  (story 04) via the same Protocol surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamind.persistence.models import OptionsContractSnapshots

__all__ = [
    "IVSnapshotLookupResult",
    "SqlOptionsSnapshotRepository",
    "lookup_iv_at_timestamp",
    "resolve_contract_ticker",
]


# ---------------------------------------------------------------------------
# Typed result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IVSnapshotLookupResult:
    """Result of a successful per-contract IV snapshot lookup.

    ``implied_volatility`` is always non-None on a returned result — callers
    receive ``None`` from :func:`lookup_iv_at_timestamp` when no usable row
    exists (no snapshot at-or-before target, or all at-or-before rows have a
    NULL ``implied_volatility``).

    ``underlying_price_at_snapshot`` mirrors the ``underlying_price`` column,
    which the collector may leave NULL; consumers must handle ``None``.

    ``lag_minutes`` is ``(target_ts - snapshot_ts).total_seconds() / 60.0``;
    it is always non-negative because the snapshot is at-or-before the target.
    The confidence classifier (story 05b) compares this value against
    ``CounterfactualReplayEngineConfig.iv_lag_low_confidence_threshold_minutes``.
    """

    snapshot_ts: datetime
    implied_volatility: float
    underlying_price_at_snapshot: float | None
    lag_minutes: float


# ---------------------------------------------------------------------------
# Contract ticker resolver (pure — no session)
# ---------------------------------------------------------------------------


def resolve_contract_ticker(
    *,
    underlying: str,
    strike: Decimal,
    expiration: date,
    contract_type: Literal["call", "put"],
) -> str:
    """Build the canonical Polygon OCC ``contract_ticker`` for an option.

    Format: ``O:{UNDERLYING}{YYMMDD}{C|P}{round(strike*1000):08d}``

    The encoding is identical to
    :func:`~alphamind.portfolio_state.records.positions.occ_symbol_for_options`
    and to :func:`~alphamind.portfolio_state.records.positions.alpaca_occ_symbol`
    (without the ``O:`` prefix); all three share the same ``round(strike * 1000)``
    semantics so a mismatch between the collector's write key and the replay
    engine's read key is impossible.

    The ``round`` (not ``int`` / floor) is load-bearing: a strike of
    ``149.9995`` rounds to ``150000``, not ``149999``. The ``O:`` prefix
    identifies this as the Polygon encoding, not the bare Alpaca OCC symbol.
    """
    exp_str = expiration.strftime("%y%m%d")
    cp = "C" if contract_type == "call" else "P"
    strike_milli = round(strike * 1000)
    return f"O:{underlying}{exp_str}{cp}{strike_milli:08d}"


# ---------------------------------------------------------------------------
# Snapshot lookup (session-bound)
# ---------------------------------------------------------------------------


def _parse_snapshot_ts(raw: str) -> datetime:
    """Parse the collector's snapshot timestamp into a tz-aware UTC datetime.

    Mirrors the helper in
    :mod:`alphamind.execution.continuous_monitor.greeks_refresh.iv_provider`.
    The collector writes ``datetime.now(UTC).isoformat()`` (``+00:00`` suffix);
    older rows may carry ``Z``; naive strings (hand-crafted test rows) are
    normalised by attaching UTC.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def lookup_iv_at_timestamp(
    contract_ticker: str,
    target_ts: datetime,
    *,
    session: Session,
) -> IVSnapshotLookupResult | None:
    """Return the nearest usable IV snapshot at or before *target_ts*.

    Queries ``options_contract_snapshots`` for:

    * ``contract_ticker == contract_ticker``
    * ``snapshot_ts <= target_ts``
    * ``implied_volatility IS NOT NULL``

    Ordered by ``snapshot_ts DESC``, limit 1.  Returns ``None`` when no such
    row exists — including the case where snapshots exist at-or-before the
    target but all have ``implied_volatility IS NULL`` (BS pricing inputs are
    unusable; the engine driver maps this to ``DATA_MISSING``).

    The ``snapshot_ts`` column is stored as ISO 8601 TEXT; the comparison
    works correctly for ISO 8601 strings because their lexicographic order
    matches chronological order (no timezone mixing — the collector always
    writes UTC strings).
    """
    stmt = (
        select(
            OptionsContractSnapshots.snapshot_ts,
            OptionsContractSnapshots.implied_volatility,
            OptionsContractSnapshots.underlying_price,
        )
        .where(
            OptionsContractSnapshots.contract_ticker == contract_ticker,
            OptionsContractSnapshots.snapshot_ts <= target_ts.isoformat(),
            OptionsContractSnapshots.implied_volatility.is_not(None),
        )
        .order_by(OptionsContractSnapshots.snapshot_ts.desc())
        .limit(1)
    )
    row = session.execute(stmt).first()
    if row is None:
        return None
    snapshot_ts_str, iv_value, underlying_price = row
    if iv_value is None:
        # Defensive: the WHERE clause already excludes NULLs, but guard anyway.
        return None
    snapshot_ts = _parse_snapshot_ts(snapshot_ts_str)
    lag_minutes = (target_ts - snapshot_ts).total_seconds() / 60.0
    return IVSnapshotLookupResult(
        snapshot_ts=snapshot_ts,
        implied_volatility=float(iv_value),
        underlying_price_at_snapshot=float(underlying_price)
        if underlying_price is not None
        else None,
        lag_minutes=lag_minutes,
    )


# ---------------------------------------------------------------------------
# SqlOptionsSnapshotRepository — Protocol implementation
# ---------------------------------------------------------------------------


class SqlOptionsSnapshotRepository:
    """Synchronous SQLAlchemy Session implementation of :class:`~.repos.OptionsSnapshotRepository`.

    All three protocol methods delegate to the pure helpers in this module.
    Wired at the composition root (story 08); in tests, pass a real Session
    backed by an in-memory SQLite database.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def has_snapshot_at_or_before(
        self,
        contract_ticker: str,
        when: datetime,
    ) -> bool:
        """Return ``True`` if a non-null IV snapshot exists at or before *when*.

        Consistent with :func:`lookup_iv_at_timestamp`: only rows whose
        ``implied_volatility`` is non-null are counted so the eligibility
        pre-check (story 04) agrees with the lookup (story 05c).
        """
        stmt = (
            select(OptionsContractSnapshots.snapshot_ts)
            .where(
                OptionsContractSnapshots.contract_ticker == contract_ticker,
                OptionsContractSnapshots.snapshot_ts <= when.isoformat(),
                OptionsContractSnapshots.implied_volatility.is_not(None),
            )
            .limit(1)
        )
        return self._session.execute(stmt).first() is not None

    def resolve_contract_ticker(
        self,
        *,
        underlying: str,
        strike: Decimal,
        expiration: date,
        contract_type: Literal["call", "put"],
    ) -> str:
        """Delegate to the pure :func:`resolve_contract_ticker` function."""
        return resolve_contract_ticker(
            underlying=underlying,
            strike=strike,
            expiration=expiration,
            contract_type=contract_type,
        )

    def lookup_iv(
        self,
        *,
        contract_ticker: str,
        target_ts: datetime,
    ) -> IVSnapshotLookupResult | None:
        """Return the nearest usable IV snapshot at or before *target_ts*.

        Delegates to :func:`lookup_iv_at_timestamp`.
        """
        return lookup_iv_at_timestamp(contract_ticker, target_ts, session=self._session)

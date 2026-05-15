"""Replay execution engine — story 02-distillation-layer/replay-harness/05.

The harness's central primitive: take a :class:`FixtureSlice` and a
:class:`LoadedCandidateConfig`, spin up an isolated SQLAlchemy session
seeded from the slice's ``raw_inputs.sqlite``, and run the live external
distillation orchestrator across the slice's invocation timestamps. Every
anomaly flag, regime label, and Class B baseline emitted is captured into
a :class:`SliceReplayResult` for the aggregator (story 06) and the
report renderer (story 07) to consume in-memory.

State isolation is the central invariant: the engine MUST NOT read from or
write to the runtime distillation database (resolved via the
``DATABASE_PATH`` environment variable). Every call uses a fresh temporary
SQLite file under :func:`tempfile.mkdtemp`, and the orchestrator's
archive/provenance writes are redirected into the same temp directory so
the operator's archive root is never touched. Cleanup goes through
:func:`_rmtree_with_retry` because Windows holds a mandatory share-lock on
the SQLite file briefly after ``engine.dispose``.

See ``docs/design/02-distillation-layer/replay-harness.md`` § Process for
the design contract this module implements.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import shutil
import sqlite3
import tempfile
import time
from argparse import Namespace
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from alphamind.distillation.orchestrator import (
    DistillationOutputs,
    run_external_distillation,
)
from alphamind.distillation.replay_harness.candidate_config import LoadedCandidateConfig
from alphamind.distillation.replay_harness.fixtures import (
    RAW_INPUTS_FILENAME,
    FixtureSlice,
)
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationContractHistory,
    DistillationEventHistory,
    DistillationPairLag,
    DistillationTickerBaseline,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Raw-input table copy list
# ---------------------------------------------------------------------------
#
# Authoritative list of raw-input tables the engine copies from the slice's
# ``raw_inputs.sqlite`` into the isolated session. Every table here is one
# the live distillation orchestrator queries — every category module
# (``q1``, ``q3``, ``q6``, ``q7``, ``q12``, qualitative-derived, regime)
# imports its row producers from :mod:`alphamind.persistence.models` and
# this list mirrors the union of those imports excluding the distillation
# state tables (which the harness leaves empty for the orchestrator's
# Phase 1 refresh primitives to populate).
#
# When the data-layer schema adds a new table the orchestrator reads, this
# constant must grow alongside. A schema mismatch (an entry here that does
# not exist in the migrated DB) fails loudly at copy time so drift
# surfaces immediately rather than silently dropping data.

RAW_INPUT_TABLE_NAMES: tuple[str, ...] = (
    "asset_universe",
    "sector_classification",
    "etf_membership",
    "ohlcv_bars",
    "corporate_actions",
    "options_contracts",
    "options_contract_snapshots",
    "macro_observations",
    "event_calendar",
    "news_articles",
    "news_article_tickers",
    "prediction_market_contracts",
    "prediction_market_snapshots",
)


# ---------------------------------------------------------------------------
# Captured-output dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnomalyFlagRecord:
    """One anomaly flag emitted by the orchestrator during replay.

    The harness deduplicates by ``(flag_type, ticker_or_pair_key)`` so the
    same anomaly emitted into both a sector output and the correlation
    brief is recorded once.
    """

    flag_type: str
    ticker_or_pair_key: str | None
    magnitude: float | None
    calibration_state: str


@dataclass(frozen=True, slots=True)
class BaselineValueRecord:
    """One Class B baseline value snapshot from the isolated state tables."""

    baseline_kind: str
    entity_key: str
    value: float | None
    calibration_state: str


@dataclass(frozen=True, slots=True)
class InvocationOutputs:
    """What the engine retains for one invocation of the orchestrator."""

    as_of: datetime
    invocation_id: str
    regime_label: str
    regime_transition_state: str
    anomaly_flags: tuple[AnomalyFlagRecord, ...]
    class_b_baseline_values: tuple[BaselineValueRecord, ...]


@dataclass(frozen=True, slots=True)
class SliceReplayResult:
    """Aggregated per-slice replay outputs.

    ``regime_label`` here is the slice's *exemplary* regime from the manifest,
    distinct from the per-invocation ``InvocationOutputs.regime_label`` the
    orchestrator classifies. ``config_content_hash`` lets the aggregator
    (story 06) attribute outputs to the right config in diff mode.
    """

    slice_id: str
    regime_label: str
    invocations: tuple[InvocationOutputs, ...]
    config_content_hash: str


# ---------------------------------------------------------------------------
# Isolated session setup
# ---------------------------------------------------------------------------


def _apply_replay_pragmas(dbapi_connection: Any, _record: Any) -> None:
    """Match the runtime FK enforcement; WAL is irrelevant for the temp DB."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _migrate_isolated_db(db_path: Path) -> None:
    """Run alembic upgrade head against ``db_path`` to land the full schema."""
    repo_root = Path(__file__).resolve().parents[4]
    cfg = Config(repo_root / "alembic.ini", cmd_opts=Namespace(x=[f"db={db_path}"]))
    command.upgrade(cfg, "head")


# Worst-case wait under exponential backoff is ~1.3s (10ms+20+40+80+160+200*5);
# in practice the first ``gc.collect()`` resolves the lock by forcing alembic's
# transient pooled connection to finalize, so most callers exit on attempt 1.
_RMTREE_MAX_ATTEMPTS = 10
_RMTREE_BASE_DELAY_S = 0.01
_RMTREE_MAX_DELAY_S = 0.2


def _rmtree_with_retry(path: Path) -> None:
    """Recursively remove ``path``, tolerating Windows post-dispose share-locks.

    SQLite handle finalizers can briefly outlive ``engine.dispose`` on Windows,
    holding a mandatory share-lock that surfaces as
    ``PermissionError [WinError 32]`` when ``shutil.rmtree`` calls ``os.unlink``.
    POSIX never enters this branch — unlink succeeds against open files.

    ``gc.collect()`` between attempts forces alembic's transient engine
    (created inside ``command.upgrade`` and not exposed to us) to finalize
    its pooled connection so the next ``rmtree`` sees the file unlocked.
    """
    last_error: PermissionError | None = None
    for attempt in range(_RMTREE_MAX_ATTEMPTS):
        try:
            shutil.rmtree(path)
        except PermissionError as exc:
            last_error = exc
            gc.collect()
            time.sleep(min(_RMTREE_BASE_DELAY_S * 2**attempt, _RMTREE_MAX_DELAY_S))
        else:
            return
    assert last_error is not None
    raise last_error


# Chunked-copy batch size — large enough to keep the inner loop's overhead
# negligible while bounded enough that a degenerate slice cannot pin
# arbitrary memory. Not a Class A threshold; an internal performance knob.
_COPY_BATCH_SIZE = 500


def _copy_raw_input_tables(target_db: Path, source_db: Path) -> None:
    """Copy every :data:`RAW_INPUT_TABLE_NAMES` table from ``source_db`` to ``target_db``.

    Two connections (rather than ``ATTACH``) sidesteps SQLite's lock
    interaction when the target was just modified by alembic. Foreign-key
    checks are temporarily relaxed so inserts can land in any order;
    FKs re-enable after the copy.
    """
    source_conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target_conn = sqlite3.connect(str(target_db))
    try:
        target_conn.execute("PRAGMA foreign_keys=OFF")
        for table in RAW_INPUT_TABLE_NAMES:
            _copy_one_table(source_conn, target_conn, table)
        target_conn.commit()
        target_conn.execute("PRAGMA foreign_keys=ON")
    finally:
        target_conn.close()
        source_conn.close()


def _copy_one_table(
    source_conn: sqlite3.Connection, target_conn: sqlite3.Connection, table: str
) -> None:
    """Copy every row from ``source.table`` into ``target.table`` in batches.

    Insert by explicit column name list so a target schema that's been
    extended via Alembic since the fixture was captured (e.g. a new
    nullable column) absorbs the older fixture without errors.
    """
    cursor = source_conn.execute(f"SELECT * FROM {table}")
    column_names = [c[0] for c in cursor.description]
    columns_sql = ", ".join(column_names)
    placeholders = ", ".join("?" * len(column_names))
    insert_sql = f"INSERT INTO {table} ({columns_sql}) VALUES ({placeholders})"
    while True:
        batch = cursor.fetchmany(_COPY_BATCH_SIZE)
        if not batch:
            return
        target_conn.executemany(insert_sql, batch)


def _build_isolated_engine(db_path: Path) -> tuple[Engine, sessionmaker[Session]]:
    """Build a SQLAlchemy engine + session factory pair bound to ``db_path``.

    The orchestrator drives synchronous DB calls through ``asyncio.to_thread``
    so the engine pins :class:`StaticPool` plus ``check_same_thread=False``
    to share one connection across worker threads (matching the orchestrator
    test fixture's pattern). The harness builds a fresh session per
    invocation off the returned factory so each orchestrator call sees a
    clean identity-map / state cache, mirroring the per-invocation lifecycle
    in production.
    """
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", _apply_replay_pragmas)
    factory: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
    return engine, factory


# ---------------------------------------------------------------------------
# Ticker scope derivation
# ---------------------------------------------------------------------------


def _resolve_ticker_scope(session: Session) -> tuple[str, ...]:
    """Active-universe tickers as recorded in the slice's ``asset_universe``.

    The runtime resolves the ticker scope via the configuration cascade.
    The harness instead reads the universe-as-it-stood-at-curation directly
    from the slice's snapshot — a ``LIVE_ARCHIVE`` slice's universe is
    frozen to its curation moment, and the harness reasons about regime
    behavior at that moment.
    """
    rows = session.execute(
        select(AssetUniverse.ticker)
        .where(AssetUniverse.asset_role == "universe")
        .where(AssetUniverse.is_active == 1)
        .order_by(AssetUniverse.ticker)
    ).all()
    return tuple(row[0] for row in rows)


# ---------------------------------------------------------------------------
# Output extraction
# ---------------------------------------------------------------------------


def _split_flag_name(flag_name: str) -> tuple[str, str | None]:
    """Split a flag name into ``(flag_type, ticker_or_pair_key)``.

    Producers vary in convention: some emit a bare flag type
    (e.g. ``volume_anomaly``); others embed the keyed entity after a
    ``:`` (e.g. ``overdue_lag_flag:HYG_SPY``). The harness honors both.
    """
    flag_type, separator, entity_key = flag_name.partition(":")
    if not separator:
        return flag_type, None
    return flag_type, entity_key


def _extract_anomaly_flags(outputs: DistillationOutputs) -> tuple[AnomalyFlagRecord, ...]:
    """Walk every block, collect anomaly flags, dedupe by ``(flag_type, entity_key)``.

    The same anomaly may appear in both a sector-targeted block and a
    correlation-brief-targeted block; the engine retains one record. The
    record carries the source block's calibration state so weighting in
    downstream aggregation matches the orchestrator's own propagation.
    """
    seen: dict[tuple[str, str | None], AnomalyFlagRecord] = {}
    for block in outputs.all_blocks:
        for flag in block.anomaly_flags:
            flag_type, entity_key = _split_flag_name(flag.name)
            key = (flag_type, entity_key)
            if key in seen:
                continue
            seen[key] = AnomalyFlagRecord(
                flag_type=flag_type,
                ticker_or_pair_key=entity_key,
                magnitude=float(flag.magnitude),
                calibration_state=block.calibration_state.value,
            )
    return tuple(seen.values())


def _extract_class_b_baselines(
    session: Session, as_of: datetime
) -> tuple[BaselineValueRecord, ...]:
    """Snapshot the four Class B state tables for rows at ``as_of``.

    The orchestrator's Phase 1 refresh writes one row per
    ``(ticker, baseline_kind)`` per invocation; tickers/pairs/contracts the
    slice does not cover produce no record. Outcome rows (event history)
    have no baseline ``value`` so the record's ``value`` is ``None`` for
    those entries.
    """
    as_of_iso = _format_as_of(as_of)
    records: list[BaselineValueRecord] = []

    ticker_rows = session.execute(
        select(
            DistillationTickerBaseline.baseline_kind,
            DistillationTickerBaseline.ticker,
            DistillationTickerBaseline.mean,
            DistillationTickerBaseline.calibration_state,
        )
        .where(DistillationTickerBaseline.as_of == as_of_iso)
        .order_by(
            DistillationTickerBaseline.baseline_kind,
            DistillationTickerBaseline.ticker,
        )
    ).all()
    for baseline_kind, ticker, mean, calibration_state in ticker_rows:
        records.append(
            BaselineValueRecord(
                baseline_kind=baseline_kind,
                entity_key=ticker,
                value=float(mean) if mean is not None else None,
                calibration_state=calibration_state,
            )
        )

    pair_rows = session.execute(
        select(
            DistillationPairLag.lead_ticker,
            DistillationPairLag.lag_ticker,
            DistillationPairLag.lead_lag_days_estimate,
            DistillationPairLag.calibration_state,
        )
        .where(DistillationPairLag.as_of == as_of_iso)
        .order_by(DistillationPairLag.lead_ticker, DistillationPairLag.lag_ticker)
    ).all()
    for lead_ticker, lag_ticker, days_estimate, calibration_state in pair_rows:
        records.append(
            BaselineValueRecord(
                baseline_kind="pair_lag",
                entity_key=f"{lead_ticker}_{lag_ticker}",
                value=float(days_estimate) if days_estimate is not None else None,
                calibration_state=calibration_state,
            )
        )

    contract_rows = session.execute(
        select(
            DistillationContractHistory.contract_id,
            DistillationContractHistory.yes_probability,
            DistillationContractHistory.calibration_state,
        )
        .where(DistillationContractHistory.snapshot_ts == as_of_iso)
        .order_by(DistillationContractHistory.contract_id)
    ).all()
    for contract_id, yes_probability, calibration_state in contract_rows:
        records.append(
            BaselineValueRecord(
                baseline_kind="contract_history",
                entity_key=contract_id,
                value=float(yes_probability) if yes_probability is not None else None,
                calibration_state=calibration_state,
            )
        )

    event_rows = session.execute(
        select(
            DistillationEventHistory.ticker,
            DistillationEventHistory.event_kind,
            DistillationEventHistory.magnitude_atr_multiple,
        )
        .where(DistillationEventHistory.event_ts == as_of_iso)
        .order_by(
            DistillationEventHistory.event_kind,
            DistillationEventHistory.ticker,
        )
    ).all()
    for ticker, event_kind, magnitude in event_rows:
        records.append(
            BaselineValueRecord(
                baseline_kind=f"event_history_{event_kind}",
                entity_key=ticker,
                value=float(magnitude) if magnitude is not None else None,
                # Event history rows have no calibration_state column — use
                # the calibrated sentinel because a row's existence is the
                # signal here, not a derived baseline calibration.
                calibration_state="calibrated",
            )
        )

    return tuple(records)


# ---------------------------------------------------------------------------
# Per-invocation regime extraction
# ---------------------------------------------------------------------------


def _format_as_of(as_of: datetime) -> str:
    """Format a tz-aware datetime as ISO 8601 ``Z``-suffixed UTC.

    Mirrors the orchestrator's own ``_format_as_of`` so the harness reads
    rows with the same key the orchestrator writes.
    """
    return as_of.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _regime_payload_value(outputs: DistillationOutputs, key: str) -> str:
    """Read a string field from the universal regime label payload."""
    value = outputs.universal_regime_label.get(key)
    return str(value) if value is not None else ""


def _synthesize_invocation_id(slice_id: str, as_of: datetime) -> str:
    """Build the harness's invocation_id per the documented pattern.

    Format: ``replay_<slice_id>_<as_of_compact>`` where ``as_of_compact``
    is filesystem-safe (no colons) and lexicographically sortable.
    """
    return f"replay_{slice_id}_{as_of.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _run_one_invocation(
    session: Session,
    *,
    candidate: LoadedCandidateConfig,
    slice_id: str,
    as_of: datetime,
    archive_root: Path,
    provenance_root: Path,
) -> InvocationOutputs:
    """Drive one orchestrator call and capture its outputs."""
    invocation_id = _synthesize_invocation_id(slice_id, as_of)
    ticker_scope = _resolve_ticker_scope(session)

    started = time.monotonic()
    # Project the Pydantic ``DistillationConfig`` boundary type onto its
    # frozen-dataclass mirror (ALP-471) before the orchestrator runs — the
    # orchestrator's compute path consumes the dataclass form.
    outputs = asyncio.run(
        run_external_distillation(
            session=session,
            config=candidate.config.to_domain(),
            ticker_scope=ticker_scope,
            as_of=as_of,
            invocation_id=invocation_id,
            archive_root=archive_root,
            provenance_root=provenance_root,
        )
    )
    elapsed_ms = (time.monotonic() - started) * 1000.0

    anomaly_flags = _extract_anomaly_flags(outputs)
    baselines = _extract_class_b_baselines(session, as_of)
    regime_label = _regime_payload_value(outputs, "regime_label")
    transition_state = _regime_payload_value(outputs, "transition_state")

    logger.info(
        "replay invocation complete: slice=%s as_of=%s anomaly_count=%d regime=%s elapsed_ms=%.1f",
        slice_id,
        _format_as_of(as_of),
        len(anomaly_flags),
        regime_label,
        elapsed_ms,
    )

    return InvocationOutputs(
        as_of=as_of,
        invocation_id=invocation_id,
        regime_label=regime_label,
        regime_transition_state=transition_state,
        anomaly_flags=anomaly_flags,
        class_b_baseline_values=baselines,
    )


def _parse_invocation_timestamps(timestamps: Sequence[str]) -> tuple[datetime, ...]:
    """Convert the manifest's ISO 8601 strings to UTC ``datetime`` instances."""
    return tuple(datetime.fromisoformat(ts).astimezone(UTC) for ts in timestamps)


def replay_slice(
    fixture_slice: FixtureSlice, candidate: LoadedCandidateConfig
) -> SliceReplayResult:
    """Replay every invocation in ``fixture_slice`` under ``candidate``.

    Procedure (per ``replay-harness.md`` § Process):

    1. Spin up an isolated SQLite database under :func:`tempfile.mkdtemp`
       and run :func:`alembic upgrade head` to land the schema.
    2. Copy every :data:`RAW_INPUT_TABLE_NAMES` table from the slice's
       ``raw_inputs.sqlite`` into the isolated DB.
    3. Leave the Class B distillation state tables empty — refresh primitives
       inside the orchestrator populate them from the slice's own history.
    4. Run :func:`run_external_distillation` for each manifest invocation
       timestamp in ascending order, capturing anomaly flags / regime label /
       Class B baselines per call.
    5. Return the aggregated :class:`SliceReplayResult`.
    6. Tear down the temp directory regardless of success/failure.
    """
    invocation_timestamps = _parse_invocation_timestamps(
        fixture_slice.manifest.invocation_timestamps
    )
    overall_start = time.monotonic()
    logger.info(
        "replay slice start: slice=%s regime=%s invocations=%d",
        fixture_slice.manifest.slice_id,
        fixture_slice.manifest.regime_label,
        len(invocation_timestamps),
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="alphamind_replay_"))
    try:
        isolated_db = tmp_dir / "isolated.sqlite"
        archive_root = tmp_dir / "archive"
        provenance_root = tmp_dir / "provenance"

        _migrate_isolated_db(isolated_db)
        _copy_raw_input_tables(
            target_db=isolated_db,
            source_db=fixture_slice.slice_dir / RAW_INPUTS_FILENAME,
        )

        engine, session_factory = _build_isolated_engine(isolated_db)
        try:
            invocations: list[InvocationOutputs] = []
            cumulative_anomalies = 0
            for as_of in invocation_timestamps:
                with session_factory() as session:
                    invocation = _run_one_invocation(
                        session,
                        candidate=candidate,
                        slice_id=fixture_slice.manifest.slice_id,
                        as_of=as_of,
                        archive_root=archive_root,
                        provenance_root=provenance_root,
                    )
                invocations.append(invocation)
                cumulative_anomalies += len(invocation.anomaly_flags)
        finally:
            engine.dispose()
    finally:
        _rmtree_with_retry(tmp_dir)

    elapsed_s = time.monotonic() - overall_start
    logger.info(
        "replay slice end: slice=%s cumulative_anomalies=%d elapsed_s=%.3f",
        fixture_slice.manifest.slice_id,
        cumulative_anomalies,
        elapsed_s,
    )

    return SliceReplayResult(
        slice_id=fixture_slice.manifest.slice_id,
        regime_label=fixture_slice.manifest.regime_label,
        invocations=tuple(invocations),
        config_content_hash=candidate.content_hash,
    )


__all__ = [
    "RAW_INPUT_TABLE_NAMES",
    "AnomalyFlagRecord",
    "BaselineValueRecord",
    "InvocationOutputs",
    "SliceReplayResult",
    "replay_slice",
]

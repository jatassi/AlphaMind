"""Drain the ``unattributed_fills`` retry queue back into ``fill_records`` (ALP-763).

A fill-bearing event that arrives before its local ``orders`` row is committed
(deferred Phase-2 writeback) is parked on the ``unattributed_fills`` queue
rather than silently dropped. This module's :func:`drain_unattributed_fills`
re-attempts attribution for every queued row:

* **resolved** (the order materialized — the race) → translate, append to
  ``fill_records`` as unprocessed so Phase 1 integrates it, and delete the
  queue row;
* **still unresolved** (an out-of-band manual order that will never get a local
  row) → bump ``retry_count`` and emit a one-time alert; the row stays queued.

It is invoked opportunistically at the top of each fill-stream reconnect cycle
so a queued race-fill integrates as soon as its order row exists. Each row runs
in its own short transaction (mirroring the per-fill write discipline) so one
failure never wedges the batch.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.execution.broker_adapter import FillReport
from alphamind.execution.continuous_monitor.fill_stream_consumer.persistence import (
    EnrichmentCallable,
    _resolve_oms_order_id,
)
from alphamind.execution.continuous_monitor.fill_stream_consumer.translation import (
    fill_report_to_fill_record,
)
from alphamind.execution.write_paths.fill_persistence import append_fill_record
from alphamind.execution.write_paths.phase1 import integrate_recovered_fills
from alphamind.execution.write_paths.unattributed_fill_persistence import (
    delete_unattributed_fill,
    list_unattributed_fills,
    mark_unattributed_fill_alerted,
    touch_unattributed_fill_retry,
)

log = logging.getLogger(__name__)


async def drain_unattributed_fills(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    enrichment_callable: EnrichmentCallable | None = None,
    process_lifetime_id: str | None = None,
) -> int:
    """Re-resolve every queued unattributed fill; return the count integrated.

    For each queued row, rehydrate the serialized :class:`FillReport` and
    re-attempt resolution via the shared :func:`_resolve_oms_order_id` (the
    same two-step PK / broker-UUID lookup the live path uses). On success the
    fill is appended to ``fill_records`` (enriched first if a paper-mode
    ``enrichment_callable`` is supplied, mirroring ``persist_fill_report``) and
    the queue row deleted. On failure the row is touched (``retry_count``
    bumped) and alerted once, then left queued.

    When *process_lifetime_id* is supplied and at least one fill was integrated,
    :func:`integrate_recovered_fills` is called immediately after the drain so
    the PENDING→OPEN position transition and bracket-leg activation run within
    seconds rather than waiting for the next scheduled pipeline run (ALP-767).
    """
    async with session_factory() as db:
        queued = await list_unattributed_fills(db)

    integrated = 0
    for queued_fill in queued:
        report = FillReport.model_validate_json(queued_fill.raw_report_json)
        async with session_factory() as db:
            oms_order_id = await _resolve_oms_order_id(db, report)
            if oms_order_id is None:
                await touch_unattributed_fill_retry(
                    db, queued_fill.broker_fill_key, observed_at=datetime.now(UTC)
                )
                if not queued_fill.alerted:
                    log.warning(
                        "QUARANTINED unattributed fill still unresolved on drain: "
                        "broker_fill_key=%s client_order_id=%s alpaca_order_id=%s event=%s",
                        queued_fill.broker_fill_key,
                        report.client_order_id,
                        report.alpaca_order_id,
                        report.event_type,
                    )
                    await mark_unattributed_fill_alerted(db, queued_fill.broker_fill_key)
                await db.commit()
                continue

            record = fill_report_to_fill_record(report, oms_order_id=oms_order_id)
            if record is None:  # pragma: no cover — only fill-bearing reports are queued
                await db.commit()
                continue
            if enrichment_callable is not None:
                record = await enrichment_callable(record)
            await append_fill_record(db, record)
            await delete_unattributed_fill(db, queued_fill.broker_fill_key)
            await db.commit()
        integrated += 1
        log.info(
            "integrated previously-unattributed fill: broker_fill_key=%s order_id=%s",
            queued_fill.broker_fill_key,
            oms_order_id,
        )

    if integrated > 0 and process_lifetime_id is not None:
        count = await integrate_recovered_fills(
            session_factory,
            process_lifetime_id=process_lifetime_id,
        )
        log.info(
            "fill recovery Phase-1 integration completed: fills_processed=%d",
            count,
        )

    return integrated


__all__ = ["drain_unattributed_fills"]

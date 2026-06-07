"""Shared substrate for the thesis-resolution resolver tests (ALP-899).

A fresh on-disk SQLite DB plus builders that seed a CLOSED position, an
ACTIVE thesis with all three component types (codec-round-trippable), a
``thesis_pnl_ledger`` realized-PnL row, and the POSITION_CLOSED
activity-log entry the resolver reads the exit method from. Tests mock only
the SDK (the LLM-fallback path) and the database (this on-disk session).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.ids import PositionId, ThesisId
from alphamind._kernel.money import money, signed_money
from alphamind.config.models.agents import AllowedModel, BaseAgentConfig
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.portfolio_state.events.activity_log import (
    EVENT_TYPE_TO_GROUP,
    ActivityLogEntry,
    EventSource,
    EventType,
    PositionClosedDetail,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.theses import (
    KeyAssumption,
    ThesisComponent,
    ThesisComponentType,
    ThesisRecord,
    ThesisRecordStatus,
)
from alphamind.state.invocation_context.activity_log import activity_log_entry_to_row
from alphamind.state.tables.theses_codec import record_to_rows
from alphamind.state.tables.thesis_pnl_ledger import ThesisPnlLedgerRow
from tests.state._fk_substrate import (
    stub_invocation_row,
    stub_position_row,
    stub_process_lifetime_row,
)

NOW = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
GENERATION = NOW - timedelta(hours=24)
PROMPT_PATH = "prompts/analysis/thesis_component_evaluator.md"


@pytest.fixture()
async def db(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    """Yield (async_engine, async_session_factory) over a fresh on-disk SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    import alphamind.state.tables  # noqa: F401 — register state tables on Base.metadata

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    async_engine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    yield async_engine, factory
    await async_engine.dispose()


def make_evaluator_config() -> BaseAgentConfig:
    """A non-roster config pointing at the committed minimal-eval prompt (04d seam)."""
    return BaseAgentConfig(
        model=AllowedModel.sonnet_4_6,
        prompt=PROMPT_PATH,
        latency_budget_seconds=120,
        context_token_budget=4000,
        output_token_budget=2000,
        tools=[],
    )


def make_component(
    component_type: ThesisComponentType,
    thesis_id: str,
    *,
    component_id: str | None = None,
    instrument_reference: str = "NVDA",
) -> ThesisComponent:
    return ThesisComponent(
        component_id=component_id or f"{thesis_id}-{component_type.value}",
        thesis_id=ThesisId(thesis_id),
        component_type=component_type,
        linked_bracket_leg_type=None,
        instrument_reference=instrument_reference,
        narrative=f"{component_type.value} narrative for {thesis_id}.",
        key_assumptions=(KeyAssumption(text="A falsifiable claim.", outcome=None),),
        generation_timestamp=GENERATION,
        resolution_outcome=None,
        resolution_notes=None,
    )


def make_active_thesis(
    *,
    thesis_id: str = "thesis-1",
    position_id: str = "pos-1",
    instrument_reference: str = "NVDA",
) -> ThesisRecord:
    """An ACTIVE thesis with all three mandatory component types."""
    components = tuple(
        make_component(ct, thesis_id, instrument_reference=instrument_reference)
        for ct in (
            ThesisComponentType.ENTRY_RATIONALE,
            ThesisComponentType.TARGET_RATIONALE,
            ThesisComponentType.INVALIDATION_RATIONALE,
        )
    )
    return ThesisRecord(
        thesis_id=ThesisId(thesis_id),
        position_id=PositionId(position_id),
        summary=f"Thesis {thesis_id} summary.",
        key_catalyst="Earnings catalyst.",
        components=components,
        status=ThesisRecordStatus.ACTIVE,
        generation_timestamp=GENERATION,
        time_expectation_hours=24.0,
        age_hours=24.0,
        expected_resolution_at=GENERATION + timedelta(hours=24.0),
        resolution_timestamp=None,
        resolution_category=None,
        resolution_pnl_usd=None,
        entry_fill_gap_usd=None,
    )


async def seed_closed_position_thesis(
    factory: async_sessionmaker[AsyncSession],
    *,
    thesis: ThesisRecord,
    realized_pnl_usd: float,
    exit_method: PositionExitMethod,
    invocation_id: str,
    closed_in_invocation_id: str | None = None,
) -> None:
    """Seed the CLOSED position + ACTIVE thesis + ledger row + POSITION_CLOSED entry.

    ``closed_in_invocation_id`` defaults to ``invocation_id`` (closed this
    invocation); pass a prior id to model a position closed by an earlier
    invocation (e.g. the continuous monitor).
    """
    position_id = str(thesis.position_id)
    closer_inv = closed_in_invocation_id or invocation_id
    thesis_row, component_rows = record_to_rows(thesis)
    async with factory() as sess:
        sess.add(stub_process_lifetime_row())
        await sess.flush()
        sess.add(stub_invocation_row(invocation_id))
        if closer_inv != invocation_id:
            sess.add(stub_invocation_row(closer_inv))
        await sess.flush()
        sess.add(
            stub_position_row(
                position_id,
                thesis_id=str(thesis.thesis_id),
                status="CLOSED",
            )
        )
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        await sess.flush()
        sess.add(
            ThesisPnlLedgerRow(
                thesis_id=str(thesis.thesis_id),
                realized_pnl_usd=Decimal(str(realized_pnl_usd)),
                cost_basis_usd=Decimal("1000.0"),
                provenance_json="{}",
                derived_from_invocation_id=closer_inv,
                updated_at=NOW.isoformat().replace("+00:00", "Z"),
                last_derived_event_seq=None,
            )
        )
        entry = ActivityLogEntry(
            entry_id=f"{closer_inv}-POSITION_CLOSED-{uuid.uuid4().hex}",
            invocation_id=closer_inv,
            timestamp=NOW,
            event_type=EventType.POSITION_CLOSED,
            event_group=EVENT_TYPE_TO_GROUP[EventType.POSITION_CLOSED],
            position_id=position_id,
            order_id=None,
            thesis_id=str(thesis.thesis_id),
            source=EventSource.FILL_PROCESSOR,
            detail=PositionClosedDetail(
                exit_method=exit_method,
                exit_price=money(150.0),
                realized_pnl_usd=signed_money(realized_pnl_usd),
                thesis_resolution_category="",
            ),
        )
        sess.add(activity_log_entry_to_row(entry))
        await sess.commit()


async def seed_open_position_thesis(
    factory: async_sessionmaker[AsyncSession],
    *,
    thesis: ThesisRecord,
    invocation_id: str,
) -> None:
    """Seed an ACTIVE thesis whose linked position is still OPEN (not eligible)."""
    position_id = str(thesis.position_id)
    thesis_row, component_rows = record_to_rows(thesis)
    async with factory() as sess:
        sess.add(stub_process_lifetime_row())
        await sess.flush()
        sess.add(stub_invocation_row(invocation_id))
        await sess.flush()
        sess.add(
            stub_position_row(
                position_id,
                thesis_id=str(thesis.thesis_id),
                status="OPEN",
            )
        )
        sess.add(thesis_row)
        for crow in component_rows:
            sess.add(crow)
        await sess.commit()

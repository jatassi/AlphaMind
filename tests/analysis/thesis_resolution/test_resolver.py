"""Tests for resolve_closed_position_theses — ALP-899 (story 04e).

The resolver finds closed-position ACTIVE theses, assesses each component
(programmatic 04c, LLM fallback 04d for the ambiguous ones), computes
realized P/L from the ledger, classifies via the relocated
classify_thesis_resolution, writes ACTIVE → RESOLVED satisfying the
read-model invariant, and emits one real THESIS_RESOLVED per resolved thesis.

Mocks only the SDK (the LLM-fallback path) and the database (an on-disk
SQLite session).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.analysis.thesis_resolution.resolver import (
    resolve_closed_position_theses,
)
from alphamind.portfolio_state.events.activity_log import (
    EventType,
    PositionExitMethod,
)
from alphamind.portfolio_state.records.theses import (
    ThesisRecordStatus,
    ThesisResolutionCategory,
)
from alphamind.state.invocation_context.context import InvocationHandle
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.theses import ThesisRow
from alphamind.state.tables.thesis_components import ThesisComponentRow
from tests.analysis.thesis_resolution.conftest import (
    make_active_thesis,
    make_evaluator_config,
    seed_closed_position_thesis,
)

pytestmark = pytest.mark.asyncio

_INV_ID = "inv-2026-05-08T12:00:00Z-aaaa"


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for item in items:
        yield item


def _sdk_response(outcome: str = "INCONCLUSIVE", notes: str = "Qualitative read.") -> list[Any]:
    from claude_agent_sdk import AssistantMessage, ResultMessage

    usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    assistant = AssistantMessage(
        content=[], model="claude-sonnet-4-6", stop_reason="end_turn", usage=usage
    )
    result = ResultMessage(
        subtype="result",
        duration_ms=500,
        duration_api_ms=450,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        stop_reason="end_turn",
        usage=usage,
        structured_output={"outcome": outcome, "notes": notes},
    )
    return [assistant, result]


def _make_sdk_stub(outcome: str = "INCONCLUSIVE") -> Any:
    calls = {"n": 0}

    async def _stub(**_kwargs: Any) -> AsyncIterator[Any]:
        calls["n"] += 1
        async for msg in _async_iter(_sdk_response(outcome)):
            yield msg

    _stub.calls = calls  # type: ignore[attr-defined]
    return _stub


async def test_closed_position_thesis_resolves_to_valid_record(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """A closed-position ACTIVE thesis is written ACTIVE → RESOLVED with a
    category, realized P/L, and every component resolution_outcome —
    satisfying _check_resolved_fields (which fires on rehydration)."""
    _, factory = db
    thesis = make_active_thesis()
    # STOP_TRIGGERED + negative P/L: TARGET → WRONG, INVALIDATION → VALIDATED
    # (the fired invalidation correctly flagged the exit — ALP-914 finding 7,
    # programmatic); ENTRY → INCONCLUSIVE programmatically, so the LLM fallback
    # fires for it (the SDK stub returns a verdict). The thesis-level category
    # is still INVALIDATED_STOPPED_CORRECTLY (pnl<=0 partitions by exit method).
    await seed_closed_position_thesis(
        factory,
        thesis=thesis,
        realized_pnl_usd=-250.0,
        exit_method=PositionExitMethod.STOP_TRIGGERED,
        invocation_id=_INV_ID,
    )

    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_make_sdk_stub("WRONG"),
        )
        await session.commit()

    assert len(resolved) == 1

    async with factory() as session:
        thesis_row = (
            await session.execute(select(ThesisRow).where(ThesisRow.thesis_id == "thesis-1"))
        ).scalar_one()
        assert thesis_row.status == ThesisRecordStatus.RESOLVED.value
        assert thesis_row.resolution_timestamp is not None
        assert (
            thesis_row.resolution_category
            == ThesisResolutionCategory.INVALIDATED_STOPPED_CORRECTLY.value
        )

        comp_rows = (
            (
                await session.execute(
                    select(ThesisComponentRow).where(ThesisComponentRow.thesis_id == "thesis-1")
                )
            )
            .scalars()
            .all()
        )
        assert comp_rows
        assert all(c.resolution_outcome is not None for c in comp_rows)

        # One real THESIS_RESOLVED activity-log entry was emitted for this thesis.
        log_rows = (
            (
                await session.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.THESIS_RESOLVED.value,
                        ActivityLogRow.thesis_id == "thesis-1",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(log_rows) == 1


async def test_llm_fallback_fires_only_for_ambiguous_components(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """STOP_TRIGGERED resolves TARGET + INVALIDATION programmatically; only the
    qualitative ENTRY_RATIONALE component (always INCONCLUSIVE programmatically)
    falls back to the LLM evaluator — so the SDK is invoked exactly once."""
    _, factory = db
    thesis = make_active_thesis()
    await seed_closed_position_thesis(
        factory,
        thesis=thesis,
        realized_pnl_usd=-100.0,
        exit_method=PositionExitMethod.STOP_TRIGGERED,
        invocation_id=_INV_ID,
    )
    stub = _make_sdk_stub("WRONG")

    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV_ID)
        await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=stub,
        )
        await session.commit()

    # Exactly one SDK call — for the single ambiguous (ENTRY_RATIONALE) component.
    assert stub.calls["n"] == 1


async def test_no_closed_position_theses_is_a_noop(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """With no closed-position ACTIVE theses the resolver returns empty, makes
    no SDK call, and emits no THESIS_RESOLVED entry."""
    _, factory = db

    def _no_sdk(**_kwargs: Any) -> Any:
        raise AssertionError("no-op path must not call the SDK")

    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_no_sdk,
        )
        await session.commit()

    assert resolved == ()

    async with factory() as session:
        log_rows = (
            (
                await session.execute(
                    select(ActivityLogRow).where(
                        ActivityLogRow.event_type == EventType.THESIS_RESOLVED.value
                    )
                )
            )
            .scalars()
            .all()
        )
        assert log_rows == []


async def test_active_thesis_with_open_position_is_not_resolved(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """An ACTIVE thesis whose position is still OPEN is not eligible — the
    resolver leaves it ACTIVE and resolves nothing."""
    from tests.analysis.thesis_resolution.conftest import seed_open_position_thesis

    _, factory = db
    thesis = make_active_thesis()
    await seed_open_position_thesis(factory, thesis=thesis, invocation_id=_INV_ID)

    def _no_sdk(**_kwargs: Any) -> Any:
        raise AssertionError("open-position thesis must not be assessed")

    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_no_sdk,
        )
        await session.commit()

    assert resolved == ()
    async with factory() as session:
        thesis_row = (
            await session.execute(select(ThesisRow).where(ThesisRow.thesis_id == "thesis-1"))
        ).scalar_one()
        assert thesis_row.status == ThesisRecordStatus.ACTIVE.value


async def test_target_reached_profitable_thesis_classifies_validated(
    db: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> None:
    """TARGET_REACHED + positive P/L with the LLM confirming the qualitative
    components VALIDATED classifies the thesis VALIDATED — the classifier's
    positive branch, fed programmatic + LLM outcomes."""
    _, factory = db
    thesis = make_active_thesis()
    await seed_closed_position_thesis(
        factory,
        thesis=thesis,
        realized_pnl_usd=500.0,
        exit_method=PositionExitMethod.TARGET_REACHED,
        invocation_id=_INV_ID,
    )

    async with factory() as session:
        handle = InvocationHandle(session=session, invocation_id=_INV_ID)
        resolved = await resolve_closed_position_theses(
            handle,
            evaluator_config=make_evaluator_config(),
            sdk_query_fn=_make_sdk_stub("VALIDATED"),
        )
        await session.commit()

    assert len(resolved) == 1
    assert resolved[0].record.resolution_category == ThesisResolutionCategory.VALIDATED
    assert resolved[0].record.resolution_pnl_usd == 500.0
    assert resolved[0].detail.resolution_category == ThesisResolutionCategory.VALIDATED.value

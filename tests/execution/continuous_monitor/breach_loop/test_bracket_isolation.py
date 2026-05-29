"""Per-bracket isolation in the breach-loop's tick bracket load (ALP-732 Gap 1).

The breach loop assembles the full portfolio-state graph every tick. Step 3 of
``assemble_snapshot`` calls ``repository.get_brackets_for_positions`` — and
before ALP-732 that method reconstructed every bracket in one eager ``tuple``
generator, so a single unreadable bracket (e.g. the ALP-731 data-corruption
case: a ``DISSOLVED`` bracket whose legs are not all ``CANCELLED``) raised
``ValueError`` and poisoned the whole batch. That single bad row failed the
entire tick and left every position unmonitored.

These tests pin the resilience contract: a corrupt bracket is skipped and
surfaced at ``ERROR`` (naming the offending bracket), while every healthy
bracket — and therefore every healthy position the tick enriches from it — is
still returned. They run against the real :class:`SqlPortfolioStateRepository`
because the corruption only manifests during row→record reconstruction.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind._kernel.ids import BracketId, OrderId, PositionId, Symbol
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
)
from alphamind.portfolio_state.aggregates.risk_parameters import (
    ActiveRiskParameterEntry,
    ActiveRiskParameterSet,
)
from alphamind.portfolio_state.records.orders import (
    BracketLeg,
    BracketLegEnforcement,
    BracketLegStatus,
    BracketLegType,
    BracketRecord,
    BracketStatus,
    PriceTrigger,
)
from alphamind.portfolio_state.repository import PortfolioStateRepository
from alphamind.risk_guardrails.breach_behavior import RegimeLabel, RegimeTransitionState
from alphamind.state.config import StatePersistenceConfig
from alphamind.state.tables.brackets_codec import record_to_rows
from tests.state._fk_substrate import stub_order_row, stub_position_row

pytestmark = pytest.mark.asyncio


@pytest.fixture()
async def db_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Fresh on-disk SQLite DB with the state-persistence schema materialized."""
    import alphamind.state.tables  # noqa: F401  — registers tables on Base.metadata

    engine = make_async_engine(str(tmp_path / "alphamind.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = make_async_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _make_bracket_record(*, bracket_id: str, position_id: str) -> BracketRecord:
    leg = BracketLeg(
        leg_id=f"{bracket_id}-leg-stop",
        leg_type=BracketLegType.PRICE_STOP,
        order_id=OrderId(f"{bracket_id}-ord-stop"),
        trigger=PriceTrigger(
            underlying_ticker=Symbol("AAPL"), threshold_usd=140.0, direction="LTE"
        ),
        enforcement=BracketLegEnforcement.MECHANICAL,
        status=BracketLegStatus.ACTIVE,
    )
    return BracketRecord(
        bracket_id=BracketId(bracket_id),
        position_id=PositionId(position_id),
        status=BracketStatus.ACTIVE,
        entry_order_id=OrderId(f"{bracket_id}-ord-entry"),
        protective_legs=(leg,),
        modification_history=(),
        corporate_action_cancellation_reason=None,
        entry_window_deadline=None,
    )


async def _seed_bracket(factory: async_sessionmaker[AsyncSession], record: BracketRecord) -> None:
    """Seed bracket + position stub + entry/leg-order stubs in one deferred-FK txn."""
    parent_row, leg_rows = record_to_rows(record)
    order_ids = [parent_row.entry_order_id] + [
        lrow.order_id for lrow in leg_rows if lrow.order_id is not None
    ]
    async with factory() as sess:
        sess.add(stub_position_row(record.position_id))
        for oid in order_ids:
            sess.add(stub_order_row(oid, record.bracket_id))
        sess.add(parent_row)
        await sess.flush()
        for lrow in leg_rows:
            sess.add(lrow)
        await sess.commit()


async def _corrupt_to_unreadable_dissolved(
    factory: async_sessionmaker[AsyncSession], *, bracket_id: str
) -> None:
    """Reproduce the ALP-731 corruption: flip the bracket to ``DISSOLVED`` while
    its legs stay non-``CANCELLED`` — ``BracketRecord.__post_init__`` then raises
    ``ValueError`` when the row is reconstructed."""
    async with factory() as sess:
        await sess.execute(
            text("UPDATE brackets SET status = :status WHERE bracket_id = :bid"),
            {"status": BracketStatus.DISSOLVED.value, "bid": bracket_id},
        )
        await sess.commit()


def _build_repo(factory: async_sessionmaker[AsyncSession]) -> PortfolioStateRepository:
    # ``state.repository``'s package __init__ has a known circular dependency
    # with ``state.invocation_context``; load the latter first so importing the
    # builder does not trip a partially-initialized-module ImportError.
    import importlib

    importlib.import_module("alphamind.state.invocation_context")
    from alphamind.state.repository import build_sql_portfolio_state_repository

    arp = ActiveRiskParameterSet(
        regime_label=RegimeLabel.NORMAL,
        transition_state=RegimeTransitionState.STABLE,
        transition_invocations_remaining=0,
        parameter_change_flag=False,
        entries=(
            ActiveRiskParameterEntry(
                rule_id="daily_drawdown_pct",
                rule_label="daily_drawdown_pct",
                value=5.0,
                unit="pct",
                regime_multiplier_applied=1.0,
                base_value=5.0,
            ),
        ),
        active_overlays=(),
    )
    return build_sql_portfolio_state_repository(
        session_factory=factory,
        invocation_id="mon-bracket-isolation",
        active_risk_parameters_provider=lambda: arp,
        prior_active_risk_parameters_provider=lambda _path: arp,
        config=StatePersistenceConfig.model_validate(
            {
                "pm_decision_log_sliding_window_invocations": 3,
                "snapshot_read_timeout_seconds": 5.0,
                "pip_freeze_snapshot_root": str(Path("/tmp/pip-freeze")),
                "invocation_provenance_root": str(Path("/tmp/provenance")),
            }
        ),
    )


async def test_breach_loop_bracket_load_isolates_one_unreadable_bracket(
    db_factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One corrupt bracket is skipped + surfaced; the healthy book still loads."""
    healthy = _make_bracket_record(bracket_id="brk-good", position_id="pos-good")
    corrupt = _make_bracket_record(bracket_id="brk-bad", position_id="pos-bad")
    await _seed_bracket(db_factory, healthy)
    await _seed_bracket(db_factory, corrupt)
    await _corrupt_to_unreadable_dissolved(db_factory, bracket_id="brk-bad")

    repo = _build_repo(db_factory)
    with caplog.at_level(logging.ERROR):
        result = repo.get_brackets_for_positions(position_ids=("pos-good", "pos-bad"))

    # The healthy bracket survives; the unreadable one is excluded rather than
    # aborting the whole batch (which previously failed the entire tick).
    assert tuple(b.bracket_id for b in result) == ("brk-good",)
    assert result[0] == healthy

    # The bad bracket is surfaced loudly, naming the offending ids so the
    # operator sees *which* bracket is corrupt instead of an opaque tick failure.
    skip_logs = [r for r in caplog.records if "brk-bad" in r.getMessage()]
    assert skip_logs, "expected an ERROR log naming the skipped bracket"
    assert skip_logs[0].levelno == logging.ERROR
    assert "pos-bad" in skip_logs[0].getMessage()


async def test_breach_loop_bracket_load_returns_all_when_none_corrupt(
    db_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No corruption → every requested bracket is returned (regression guard)."""
    one = _make_bracket_record(bracket_id="brk-1", position_id="pos-1")
    two = _make_bracket_record(bracket_id="brk-2", position_id="pos-2")
    await _seed_bracket(db_factory, one)
    await _seed_bracket(db_factory, two)

    repo = _build_repo(db_factory)
    result = repo.get_brackets_for_positions(position_ids=("pos-1", "pos-2"))

    assert {b.bracket_id for b in result} == {"brk-1", "brk-2"}

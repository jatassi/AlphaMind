"""Tests for ``alphamind.scheduler.phase1_inputs.gather_phase1_inputs`` (story 03b).

The gatherer assembles the typed bundle that ``process_unprocessed_fills``
consumes: ``ca_activities`` from the v1beta1 fetcher, ``alpaca_positions`` /
``alpaca_account`` from the broker adapter, and ``market_inputs`` built from
those positions + the macro-observations table. Per parent decision (H),
any broker-side failure degrades the bundle (returning no-op defaults +
``staleness_flag=True``) rather than aborting the invocation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind._kernel.money import money, price
from alphamind.config.models.main import ExecutionMode
from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)
from alphamind.execution.broker_adapter.queries import (
    AccountStateQueries,
    PositionSnapshot,
    TradeAccountSnapshot,
)
from alphamind.execution.state_persistence.invocation_context.context import InvocationHandle
from alphamind.execution.state_persistence.invocation_context.records import (
    ProcessLifetimeRecord,
    process_lifetime_record_to_row,
)
from alphamind.persistence.models import Base
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)
from alphamind.risk_guardrails.guardrail_evaluation import (
    FixtureIvProvider,
    MarketInputs,
)
from alphamind.scheduler.invocation import insert_invocation_record

_NOW = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)
_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)
REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"


def _write_placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _make_process_lifetime_record() -> ProcessLifetimeRecord:
    return ProcessLifetimeRecord(
        process_lifetime_id="proc-p1-1",
        process_role="pipeline",
        process_start_at="2026-05-07T14:30:00Z",
        process_pid=12345,
        hostname="alpha-prod-01",
        git_sha="a" * 40,
        git_branch="main",
        git_dirty=False,
        python_version="3.13.1",
        pip_freeze_hash="0" * 64,
        pip_freeze_snapshot_path="/tmp/provenance/process_lifetimes/proc-p1-1/pip_freeze.txt",
        anthropic_sdk_version="0.40.0",
        claude_agent_sdk_version="0.1.69",
        os_release="Linux-6.5.0-generic-x86_64",
    )


@pytest.fixture
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory bound to an initialized SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
        with make_session_factory(sync_engine)() as sess:
            sess.add(process_lifetime_record_to_row(_make_process_lifetime_record()))
            sess.commit()
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


def _make_venue_config() -> VenueConfig:
    creds = AlpacaCredentials(
        rest_url="https://paper-api.alpaca.markets",
        ws_url="wss://paper-api.alpaca.markets",
        api_key_env="ALPACA_PAPER_KEY",
        api_secret_env="ALPACA_PAPER_SECRET",
    )
    return VenueConfig(
        alpaca=Alpaca(paper=creds, live=creds, rate_limit_per_minute=200),
        session_hours=SessionHours(
            regular=SessionWindow(open="09:30", close="16:00"),
            pre_market=SessionWindow(open="04:00", close="09:30"),
            after_hours=SessionWindow(open="16:00", close="20:00"),
        ),
    )


def _make_account_snapshot() -> TradeAccountSnapshot:
    return TradeAccountSnapshot(
        account_id="acc-1",
        cash=money(10_000.0),
        equity=money(10_000.0),
        buying_power=money(10_000.0),
        regt_buying_power=money(10_000.0),
        daytrading_buying_power=money(10_000.0),
        maintenance_margin=money(0.0),
        daytrade_count=0,
        pattern_day_trader=False,
        status="ACTIVE",
    )


def _make_position_snapshot(
    symbol: str = "AAPL", current_price_value: float = 150.0
) -> PositionSnapshot:
    return PositionSnapshot(
        symbol=symbol,
        asset_class="us_equity",
        qty=10.0,
        avg_entry_price=price(140.0),
        market_value=money(1500.0),
        cost_basis=money(1400.0),
        unrealized_pl=money(100.0),
        unrealized_plpc=0.0714,
        current_price=price(current_price_value),
        side="long",
    )


class _StubQueries:
    """Sync stand-in for ``AccountStateQueries`` that returns canned data."""

    def __init__(
        self,
        *,
        account: TradeAccountSnapshot,
        positions: tuple[PositionSnapshot, ...],
    ) -> None:
        self._account = account
        self._positions = positions

    def get_account(self) -> TradeAccountSnapshot:
        return self._account

    def get_positions(self) -> tuple[PositionSnapshot, ...]:
        return self._positions


class _StubCorporateActionsQueries:
    """Async stand-in for ``CorporateActionsQueries``."""

    async def get_corporate_actions(  # type: ignore[no-untyped-def]
        self,
        *,
        symbols=None,
        start=None,
        end=None,
        types=None,
    ):
        return ()


async def _open_phase1_handle(
    *,
    async_factory: async_sessionmaker[AsyncSession],
    env_path: Path,
    archive_root: Path,
) -> tuple[AsyncSession, InvocationHandle]:
    """Insert the invocation row + open a fresh Phase 1 session.

    Returns ``(session, handle)``; caller is responsible for closing the
    session (use ``async with closing(session)`` or call
    ``await session.close()`` in a finally block).
    """
    from alphamind.config.models.modes import Mode
    from alphamind.config.models.regimes import Regime
    from alphamind.config.models.run_types import RunType
    from alphamind.config.resolver import RuntimeDimensions

    runtime = RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.pre_open,
    )
    invocation_id, _ = await insert_invocation_record(
        session_factory=async_factory,
        process_lifetime_id="proc-p1-1",
        trigger_type="scheduled",
        trigger_source="cron",
        trigger_reason="test",
        firing_run_type=RunType.pre_open,
        runtime=runtime,
        archive_root=archive_root,
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        now=_NOW,
    )
    session = async_factory()
    return session, InvocationHandle(session=session, invocation_id=invocation_id)


class TestGatherPhase1Inputs:
    async def test_returns_populated_bundle_on_happy_path(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: alpaca account+positions present, no CA activities, no degradation."""
        from alphamind.scheduler import phase1_inputs as module

        positions = (_make_position_snapshot(),)
        account = _make_account_snapshot()
        monkeypatch.setattr(
            module,
            "_build_account_state_queries",
            lambda venue_config, execution_mode: _StubQueries(account=account, positions=positions),
        )
        monkeypatch.setattr(
            module,
            "_build_corporate_actions_queries",
            lambda venue_config, execution_mode: _StubCorporateActionsQueries(),
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_phase1_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
            )
        finally:
            await session.close()

        assert inputs.alpaca_account == account
        assert inputs.alpaca_positions == positions
        assert inputs.ca_activities == ()
        assert isinstance(inputs.market_inputs, MarketInputs)
        assert inputs.market_inputs.underlying_prices["AAPL"] == 150.0
        assert inputs.staleness_flag is False

    async def test_account_runtimeerror_degrades_bundle(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``RuntimeError`` from ``get_account`` returns a bundle with
        ``alpaca_account=None`` and ``staleness_flag=True``; the function does
        not raise."""
        from alphamind.scheduler import phase1_inputs as module

        class _FailingQueries:
            def get_account(self) -> TradeAccountSnapshot:
                msg = "alpaca auth failed"
                raise RuntimeError(msg)

            def get_positions(self) -> tuple[PositionSnapshot, ...]:
                return ()

        monkeypatch.setattr(
            module,
            "_build_account_state_queries",
            lambda venue_config, execution_mode: _FailingQueries(),
        )
        monkeypatch.setattr(
            module,
            "_build_corporate_actions_queries",
            lambda venue_config, execution_mode: _StubCorporateActionsQueries(),
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_phase1_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
            )
        finally:
            await session.close()

        assert inputs.alpaca_account is None
        assert inputs.staleness_flag is True

    async def test_factory_runtimeerror_degrades_alpaca_fields(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When the broker-adapter factory itself raises (missing creds),
        both account and positions degrade to defaults."""
        from alphamind.scheduler import phase1_inputs as module

        def _failing_factory(*args: object, **kwargs: object) -> AccountStateQueries:
            msg = "Alpaca paper credentials not set"
            raise RuntimeError(msg)

        monkeypatch.setattr(module, "_build_account_state_queries", _failing_factory)
        monkeypatch.setattr(
            module,
            "_build_corporate_actions_queries",
            lambda venue_config, execution_mode: _StubCorporateActionsQueries(),
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_phase1_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
            )
        finally:
            await session.close()

        assert inputs.alpaca_account is None
        assert inputs.alpaca_positions == ()
        assert inputs.staleness_flag is True

    async def test_uses_position_current_prices_for_market_inputs(
        self,
        async_factory: async_sessionmaker[AsyncSession],
        env_path: Path,
        archive_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``market_inputs.underlying_prices`` is built from position
        ``current_price`` values when available."""
        from alphamind.scheduler import phase1_inputs as module

        positions = (
            _make_position_snapshot("AAPL", 175.0),
            _make_position_snapshot("MSFT", 410.0),
        )
        monkeypatch.setattr(
            module,
            "_build_account_state_queries",
            lambda venue_config, execution_mode: _StubQueries(
                account=_make_account_snapshot(), positions=positions
            ),
        )
        monkeypatch.setattr(
            module,
            "_build_corporate_actions_queries",
            lambda venue_config, execution_mode: _StubCorporateActionsQueries(),
        )

        session, handle = await _open_phase1_handle(
            async_factory=async_factory,
            env_path=env_path,
            archive_root=archive_root,
        )
        try:
            inputs = await module.gather_phase1_inputs(
                handle=handle,
                venue_config=_make_venue_config(),
                execution_mode=ExecutionMode.paper,
                as_of=_NOW,
            )
        finally:
            await session.close()

        assert dict(inputs.market_inputs.underlying_prices) == {
            "AAPL": 175.0,
            "MSFT": 410.0,
        }
        assert isinstance(inputs.market_inputs.iv_provider, FixtureIvProvider)
        assert inputs.market_inputs.as_of == _NOW

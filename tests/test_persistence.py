"""
Persistence layer tests — story 03b.

Each test targets one acceptance criterion.  Round-trip and constraint tests
use an in-memory SQLite database.  Pragma tests use a temporary file-backed
database (WAL mode is not available on in-memory databases).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from alphamind._kernel.ids import Symbol
from alphamind.persistence.models import (
    AssetUniverse,
    Base,
    CollectionRuns,
    CorporateActions,
    EarningsEventDetails,
    EtfMembership,
    EventCalendar,
    MacroObservations,
    NewsArticles,
    NewsArticleTickers,
    OhlcvBars,
    OptionsContracts,
    OptionsContractSnapshots,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
    SectorClassification,
    TickerChangeHistory,
    TreasuryAuctions,
)
from alphamind.persistence.retry import run_with_sqlite_busy_retry
from alphamind.persistence.session import (
    _resolve_path,
    begin_write_immediate,
    make_async_engine,
    make_async_session_factory,
    make_engine,
    make_session_factory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine() -> Iterator[Engine]:
    """In-memory SQLite engine with all pragmas and tables."""
    eng = make_engine(":memory:")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def file_engine(tmp_path: Path) -> Iterator[Engine]:
    """File-backed SQLite engine — needed for WAL-mode pragma tests."""
    db_path = str(tmp_path / "test.db")
    eng = make_engine(db_path)
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """Session bound to the in-memory engine."""
    session_factory = make_session_factory(engine)
    with session_factory() as sess:
        yield sess


@pytest.fixture()
def seeded_universe(session: Session) -> AssetUniverse:
    """Insert a minimal AssetUniverse row so FK constraints can be satisfied."""
    row = AssetUniverse(
        asset_id="asset-aapl",
        ticker=Symbol("AAPL"),
        full_name="Apple Inc.",
        asset_class="equity",
        asset_role="universe",
        exchange="NASDAQ",
        is_active=1,
        added_date="2020-01-01",
        last_updated="2026-04-26T00:00:00Z",
    )
    session.add(row)
    session.commit()
    return row


# ---------------------------------------------------------------------------
# AC: PRAGMA settings
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_yaml_path_expands_environment_variables(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``main.yaml`` stores paths with literal ``%VAR%`` for portability."""
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        monkeypatch.setenv("ALPHAMIND_TEST_ROOT", str(tmp_path))
        fake_yaml = tmp_path / "config" / "main.yaml"
        fake_yaml.parent.mkdir()
        fake_yaml.write_text("paths:\n  database: '%ALPHAMIND_TEST_ROOT%/data/alphamind.db'\n")

        import alphamind.persistence.session as session_mod

        monkeypatch.setattr(
            session_mod,
            "__file__",
            str(tmp_path / "src" / "alphamind" / "persistence" / "session.py"),
        )

        resolved = _resolve_path(None)
        assert "%ALPHAMIND_TEST_ROOT%" not in resolved
        assert resolved.endswith(("data/alphamind.db", r"data\alphamind.db"))
        assert str(tmp_path) in resolved

    def test_raises_when_chain_falls_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No explicit path, no env var, and no main.yaml ⇒ loud failure."""
        monkeypatch.delenv("DATABASE_PATH", raising=False)

        import alphamind.persistence.session as session_mod

        # Point __file__ at a tmp dir with no config/main.yaml so YAML lookup misses.
        monkeypatch.setattr(
            session_mod,
            "__file__",
            str(tmp_path / "src" / "alphamind" / "persistence" / "session.py"),
        )

        with pytest.raises(RuntimeError, match="not configured"):
            _resolve_path(None)

    def test_malformed_yaml_falls_back_to_runtime_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ALP-480 — ``yaml.YAMLError`` from a malformed ``main.yaml`` is
        caught by the narrowed handler and the canonical ``RuntimeError(
        'not configured')`` surfaces, not a parser traceback."""
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        fake_yaml = tmp_path / "config" / "main.yaml"
        fake_yaml.parent.mkdir()
        # Tab-indented YAML is invalid syntax → yaml.YAMLError.
        fake_yaml.write_text("paths:\n\tdatabase: foo\n")

        import alphamind.persistence.session as session_mod

        monkeypatch.setattr(
            session_mod,
            "__file__",
            str(tmp_path / "src" / "alphamind" / "persistence" / "session.py"),
        )

        with pytest.raises(RuntimeError, match="not configured"):
            _resolve_path(None)

    def test_unexpected_exception_propagates_unmasked(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ALP-480 — the narrowed handler catches only
        ``(yaml.YAMLError, OSError, ImportError)``; a synthetic ``RuntimeError``
        bubbles up so a real bug is not masked as ``RuntimeError('not
        configured')``."""
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        fake_yaml = tmp_path / "config" / "main.yaml"
        fake_yaml.parent.mkdir()
        fake_yaml.write_text("paths:\n  database: foo\n")

        import alphamind.persistence.session as session_mod

        monkeypatch.setattr(
            session_mod,
            "__file__",
            str(tmp_path / "src" / "alphamind" / "persistence" / "session.py"),
        )

        class _SyntheticBugError(RuntimeError):
            """Stand-in for an unrelated decoder bug."""

        def _raise_synthetic(*_args: object, **_kwargs: object) -> object:
            raise _SyntheticBugError("decoder bug")

        # Patch ``yaml.safe_load`` to raise an unexpected type — the narrowed
        # catch covers ``yaml.YAMLError`` / ``OSError`` / ``ImportError`` only.
        import yaml

        monkeypatch.setattr(yaml, "safe_load", _raise_synthetic)

        with pytest.raises(_SyntheticBugError):
            _resolve_path(None)


class TestPragmas:
    def test_journal_mode_is_wal(self, file_engine: Engine) -> None:
        """WAL mode requires a file-backed database."""
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert result == "wal"

    def test_foreign_keys_on(self, file_engine: Engine) -> None:
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA foreign_keys")).scalar()
        assert result == 1

    def test_busy_timeout(self, file_engine: Engine) -> None:
        with file_engine.connect() as conn:
            result = conn.execute(text("PRAGMA busy_timeout")).scalar()
        assert result == 60000

    async def test_journal_mode_and_busy_timeout_on_async_engine(self, tmp_path: Path) -> None:
        """ALP-824 regression: the isolation/begin-hook change keeps the four pragmas
        live on the async (aiosqlite) engine, not only the sync one."""
        engine = make_async_engine(str(tmp_path / "async_pragmas.db"))
        try:
            async with engine.connect() as conn:
                journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
                busy_timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()
                foreign_keys = (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar()
        finally:
            await engine.dispose()
        assert journal_mode == "wal"
        assert busy_timeout == 60000
        assert foreign_keys == 1


# ---------------------------------------------------------------------------
# AC (ALP-824): selective BEGIN IMMEDIATE write-transaction support
# ---------------------------------------------------------------------------


def _capture_begin_statements(engine: Engine) -> list[str]:
    """Record every ``BEGIN ...`` the engine emits into a returned list."""
    seen: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _cap(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        if statement.upper().startswith("BEGIN"):
            seen.append(statement)

    return seen


class TestBeginMode:
    """The async begin hook emits ``BEGIN <mode>`` from the ``sqlite_begin_mode``
    option; the sync engine keeps pysqlite's implicit BEGIN (ALP-824)."""

    async def test_unmarked_async_transaction_stays_deferred(self, tmp_path: Path) -> None:
        engine = make_async_engine(str(tmp_path / "deferred.db"))
        begins = _capture_begin_statements(engine.sync_engine)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
                await conn.rollback()
        finally:
            await engine.dispose()
        assert begins == ["BEGIN DEFERRED"]

    async def test_begin_write_immediate_helper_emits_begin_immediate(self, tmp_path: Path) -> None:
        engine = make_async_engine(str(tmp_path / "immediate_helper.db"))
        begins = _capture_begin_statements(engine.sync_engine)
        factory = make_async_session_factory(engine)
        try:
            async with factory() as session:
                await begin_write_immediate(session)
                await session.execute(text("SELECT 1"))
                await session.rollback()
        finally:
            await engine.dispose()
        assert begins == ["BEGIN IMMEDIATE"]

    def test_sync_engine_keeps_implicit_begin(self, file_engine: Engine) -> None:
        """The begin hooks are async-only: the sync engine emits no explicit BEGIN,
        preserving pysqlite's lazy implicit BEGIN so an in-flight
        ``PRAGMA foreign_keys=OFF`` (Alembic batch migrations) still runs in
        autocommit. Guards against re-broadening the begin hook to the sync engine."""
        begins = _capture_begin_statements(file_engine)
        with file_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.execute(text("CREATE TABLE _t (id INTEGER PRIMARY KEY)"))
            conn.commit()
        assert begins == []

    async def test_begin_write_immediate_rejects_open_transaction(self, tmp_path: Path) -> None:
        """The helper refuses an already-open transaction rather than silently
        degrading IMMEDIATE to the default deferred begin."""
        engine = make_async_engine(str(tmp_path / "guard.db"))
        factory = make_async_session_factory(engine)
        try:
            async with factory() as session:
                await session.execute(text("SELECT 1"))  # opens a deferred txn
                with pytest.raises(RuntimeError, match="before the session opens"):
                    await begin_write_immediate(session)
                await session.rollback()
        finally:
            await engine.dispose()


class TestCrossWriterSnapshotConflict:
    """Reproduce the two-writer race the fix targets (ALP-824)."""

    @staticmethod
    async def _seed(engine: object) -> None:
        async with engine.begin() as conn:  # type: ignore[attr-defined]
            await conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)")
            await conn.exec_driver_sql("INSERT INTO t (id, v) VALUES (1, 0)")

    async def test_deferred_read_then_write_upgrade_raises_busy_snapshot(
        self, tmp_path: Path
    ) -> None:
        """Deferred path: A reads (snapshot), B commits, A's write-upgrade fails
        immediately with SQLITE_BUSY_SNAPSHOT — the production bug."""
        db = str(tmp_path / "race.db")
        engine_a = make_async_engine(db)
        engine_b = make_async_engine(db)
        try:
            await self._seed(engine_b)
            conn_a = await engine_a.connect()  # deferred by default
            await conn_a.exec_driver_sql("SELECT v FROM t WHERE id = 1")  # read snapshot
            async with engine_b.begin() as conn_b:  # writer B commits in between
                await conn_b.exec_driver_sql("UPDATE t SET v = 1 WHERE id = 1")
            with pytest.raises(OperationalError) as excinfo:
                await conn_a.exec_driver_sql("UPDATE t SET v = 99 WHERE id = 1")
            await conn_a.rollback()
            await conn_a.close()
        finally:
            await engine_a.dispose()
            await engine_b.dispose()
        assert excinfo.value.orig is not None
        assert getattr(excinfo.value.orig, "sqlite_errorname", "") == "SQLITE_BUSY_SNAPSHOT"

    async def test_immediate_holder_serializes_concurrent_writer(self, tmp_path: Path) -> None:
        """IMMEDIATE path: the holder takes the write lock up front, so a concurrent
        writer *waits* (governed by busy_timeout) and commits after — no error."""
        db = str(tmp_path / "race2.db")
        engine_a = make_async_engine(db)
        engine_b = make_async_engine(db)
        await self._seed(engine_a)
        factory_a = make_async_session_factory(engine_a)
        factory_b = make_async_session_factory(engine_b)
        order: list[str] = []
        a_holds_lock = asyncio.Event()
        try:

            async def holder() -> None:
                async with factory_a() as session:
                    await begin_write_immediate(session)
                    await session.execute(text("UPDATE t SET v = 10 WHERE id = 1"))
                    order.append("A_wrote")
                    a_holds_lock.set()
                    await asyncio.sleep(0.2)  # hold the write lock while B contends
                    await session.commit()
                    order.append("A_committed")

            async def contender() -> None:
                await a_holds_lock.wait()
                async with factory_b() as session:
                    # Blocks on A's write lock instead of raising BUSY_SNAPSHOT.
                    await session.execute(text("UPDATE t SET v = 20 WHERE id = 1"))
                    order.append("B_wrote")
                    await session.commit()
                    order.append("B_committed")

            await asyncio.gather(holder(), contender())
            async with engine_a.connect() as conn:
                final_v = (await conn.exec_driver_sql("SELECT v FROM t WHERE id = 1")).scalar()
        finally:
            await engine_a.dispose()
            await engine_b.dispose()
        # B waited for A: it wrote only after A committed, then committed last.
        assert order == ["A_wrote", "A_committed", "B_wrote", "B_committed"]
        assert final_v == 20


class TestSqliteBusyRetry:
    """``run_with_sqlite_busy_retry`` semantics (ALP-824)."""

    @staticmethod
    def _locked_error() -> OperationalError:
        return OperationalError("UPDATE t", {}, Exception("database is locked"))

    async def test_returns_success_after_transient_failures(self) -> None:
        calls = {"n": 0}

        async def op() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise self._locked_error()
            return "ok"

        result = await run_with_sqlite_busy_retry(op, attempts=5, base_backoff_s=0.0)
        assert result == "ok"
        assert calls["n"] == 3

    async def test_reraises_non_transient_operational_error_immediately(self) -> None:
        calls = {"n": 0}

        async def op() -> str:
            calls["n"] += 1
            raise OperationalError("UPDATE t", {}, Exception("no such table: t"))

        with pytest.raises(OperationalError, match="no such table"):
            await run_with_sqlite_busy_retry(op, attempts=5, base_backoff_s=0.0)
        assert calls["n"] == 1  # not retried

    async def test_reraises_after_exhausting_attempts(self) -> None:
        calls = {"n": 0}

        async def op() -> str:
            calls["n"] += 1
            raise self._locked_error()

        with pytest.raises(OperationalError, match="database is locked"):
            await run_with_sqlite_busy_retry(op, attempts=3, base_backoff_s=0.0)
        assert calls["n"] == 3

    async def test_detects_transient_via_sqlite_errorname(self) -> None:
        """A lock is classified transient by ``orig.sqlite_errorname`` even when the
        message text does not contain a lock marker (robust to message drift)."""

        class _SnapshotConflictError(Exception):
            sqlite_errorname = "SQLITE_BUSY_SNAPSHOT"

        calls = {"n": 0}

        async def op() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise OperationalError("UPDATE t", {}, _SnapshotConflictError("snapshot conflict"))
            return "ok"

        result = await run_with_sqlite_busy_retry(op, attempts=3, base_backoff_s=0.0)
        assert result == "ok"
        assert calls["n"] == 2


# ---------------------------------------------------------------------------
# AC: Round-trip insert + select for every table
# ---------------------------------------------------------------------------


class TestRoundTrips:
    def test_asset_universe(self, session: Session, seeded_universe: AssetUniverse) -> None:
        fetched = session.get(AssetUniverse, "asset-aapl")
        assert fetched is not None
        assert fetched.ticker == "AAPL"
        assert fetched.asset_class == "equity"

    def test_sector_classification(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = SectorClassification(
            ticker=Symbol("AAPL"),
            asset_id="asset-aapl",
            alphamind_sector="tech",
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="polygon",
            last_updated="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(SectorClassification, "AAPL")
        assert fetched is not None
        assert fetched.alphamind_sector == "tech"

    def test_etf_membership(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = EtfMembership(
            ticker=Symbol("AAPL"),
            etf_ticker="XLK",
            etf_name="Tech Select Sector SPDR",
            weight_pct=22.5,
            weight_as_of="2026-04-01",
            is_top_10=1,
        )
        session.add(row)
        session.commit()
        fetched = session.get(EtfMembership, ("AAPL", "XLK", "2026-04-01"))
        assert fetched is not None
        assert fetched.weight_pct == 22.5

    def test_ticker_change_history(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = TickerChangeHistory(
            asset_id="asset-aapl",
            previous_ticker="AAPL",
            new_ticker="AAPL2",
            effective_date="2026-01-01",
            reason="rebrand",
        )
        session.add(row)
        session.commit()
        fetched = session.get(TickerChangeHistory, ("asset-aapl", "2026-01-01"))
        assert fetched is not None
        assert fetched.reason == "rebrand"

    def test_ohlcv_bars(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = OhlcvBars(
            ticker=Symbol("AAPL"),
            timeframe="1d",
            period_start="2026-04-25T09:30:00Z",
            period_end="2026-04-25T16:00:00Z",
            session="regular",
            adj_open=170.0,
            adj_high=175.0,
            adj_low=169.0,
            adj_close=174.0,
            adj_volume=50000000,
            unadj_open=170.0,
            unadj_high=175.0,
            unadj_low=169.0,
            unadj_close=174.0,
            unadj_volume=50000000,
            source="polygon",
            ingested_at="2026-04-25T20:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(OhlcvBars, ("AAPL", "1d", "2026-04-25T09:30:00Z"))
        assert fetched is not None
        assert fetched.adj_close == 174.0

    def test_corporate_actions(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = CorporateActions(
            action_id="act-001",
            ticker=Symbol("AAPL"),
            action_type="split",
            ex_date="2026-03-01",
            source="polygon",
            ingested_at="2026-04-01T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(CorporateActions, "act-001")
        assert fetched is not None
        assert fetched.action_type == "split"

    def test_options_contracts(self, session: Session, seeded_universe: AssetUniverse) -> None:
        row = OptionsContracts(
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker=Symbol("AAPL"),
            expiration_date="2025-01-17",
            strike_price=200.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
        session.add(row)
        session.commit()
        fetched = session.get(OptionsContracts, "O:AAPL250117C00200000")
        assert fetched is not None
        assert fetched.strike_price == 200.0

    def test_options_contract_snapshots(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        # Requires parent options contract
        contract = OptionsContracts(
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker=Symbol("AAPL"),
            expiration_date="2025-01-17",
            strike_price=200.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
        session.add(contract)
        session.flush()
        row = OptionsContractSnapshots(
            snapshot_ts="2026-04-26T15:00:00Z",
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker=Symbol("AAPL"),
            source="polygon",
            ingested_at="2026-04-26T15:01:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(
            OptionsContractSnapshots,
            ("2026-04-26T15:00:00Z", "O:AAPL250117C00200000"),
        )
        assert fetched is not None
        assert fetched.underlying_ticker == "AAPL"

    def test_macro_observations(self, session: Session) -> None:
        row = MacroObservations(
            source="fred",
            series_id="DGS10",
            observation_date="2026-04-25",
            revision_number=0,
            value=4.5,
            ingested_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(MacroObservations, ("fred", "DGS10", "2026-04-25", 0))
        assert fetched is not None
        assert fetched.value == 4.5

    def test_treasury_auctions(self, session: Session) -> None:
        row = TreasuryAuctions(
            auction_id="2026-03-15_10Y",
            tenor="10Y",
            auction_date="2026-03-15",
            source="treasury",
            ingested_at="2026-03-15T20:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(TreasuryAuctions, "2026-03-15_10Y")
        assert fetched is not None
        assert fetched.tenor == "10Y"

    def test_event_calendar(self, session: Session) -> None:
        row = EventCalendar(
            event_id="evt-001",
            event_type="fomc",
            scheduled_at="2026-05-01T14:00:00Z",
            status="scheduled",
            source="manual",
            ingested_at="2026-04-01T00:00:00Z",
            last_updated="2026-04-01T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(EventCalendar, "evt-001")
        assert fetched is not None
        assert fetched.event_type == "fomc"

    def test_earnings_event_details(self, session: Session) -> None:
        # Requires parent event_calendar row
        parent = EventCalendar(
            event_id="evt-earn-001",
            event_type="earnings",
            scheduled_at="2026-04-30T16:30:00Z",
            status="scheduled",
            source="finnhub",
            ingested_at="2026-04-01T00:00:00Z",
            last_updated="2026-04-01T00:00:00Z",
        )
        session.add(parent)
        session.flush()
        detail = EarningsEventDetails(
            event_id="evt-earn-001",
            ticker=Symbol("AAPL"),
            fiscal_period="Q1",
            fiscal_year=2026,
            source="finnhub",
        )
        session.add(detail)
        session.commit()
        fetched = session.get(EarningsEventDetails, "evt-earn-001")
        assert fetched is not None
        assert fetched.fiscal_period == "Q1"

    def test_news_articles(self, session: Session) -> None:
        row = NewsArticles(
            article_id="art-001",
            source="finnhub",
            language="en",
            headline_text="AAPL beats estimates",
            published_at="2026-04-26T12:00:00Z",
            ingested_at="2026-04-26T12:05:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(NewsArticles, "art-001")
        assert fetched is not None
        assert fetched.headline_text == "AAPL beats estimates"

    def test_news_article_tickers(self, session: Session, seeded_universe: AssetUniverse) -> None:
        article = NewsArticles(
            article_id="art-002",
            source="marketaux",
            language="en",
            headline_text="Markets rally",
            published_at="2026-04-26T10:00:00Z",
            ingested_at="2026-04-26T10:05:00Z",
        )
        session.add(article)
        session.flush()
        link = NewsArticleTickers(
            article_id="art-002",
            ticker=Symbol("AAPL"),
            is_primary=1,
        )
        session.add(link)
        session.commit()
        fetched = session.get(NewsArticleTickers, ("art-002", "AAPL"))
        assert fetched is not None
        assert fetched.is_primary == 1

    def test_prediction_market_contracts(self, session: Session) -> None:
        row = PredictionMarketContracts(
            contract_id="pm-001",
            platform="polymarket",
            description="Fed cuts in May?",
            category="monetary_policy",
            created_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
        )
        session.add(row)
        session.commit()
        fetched = session.get(PredictionMarketContracts, "pm-001")
        assert fetched is not None
        assert fetched.platform == "polymarket"

    def test_prediction_market_snapshots(self, session: Session) -> None:
        contract = PredictionMarketContracts(
            contract_id="pm-002",
            platform="kalshi",
            description="Rate cut Q2?",
            category="monetary_policy",
            created_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
        )
        session.add(contract)
        session.flush()
        snap = PredictionMarketSnapshots(
            contract_id="pm-002",
            snapshot_ts="2026-04-26T14:00:00Z",
            yes_probability=0.65,
            ingested_at="2026-04-26T14:01:00Z",
        )
        session.add(snap)
        session.commit()
        fetched = session.get(PredictionMarketSnapshots, ("pm-002", "2026-04-26T14:00:00Z"))
        assert fetched is not None
        assert fetched.yes_probability == 0.65

    def test_collection_runs(self, session: Session) -> None:
        row = CollectionRuns(
            run_id="run-001",
            collector="polygon.equity",
            started_at="2026-04-26T09:30:00Z",
            status="success",
        )
        session.add(row)
        session.commit()
        fetched = session.get(CollectionRuns, "run-001")
        assert fetched is not None
        assert fetched.collector == "polygon.equity"


# ---------------------------------------------------------------------------
# AC: Composite-key uniqueness
# ---------------------------------------------------------------------------


class TestCompositeKeyUniqueness:
    def test_ohlcv_bars_duplicate_raises(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        def make_bar() -> OhlcvBars:
            return OhlcvBars(
                ticker=Symbol("AAPL"),
                timeframe="1d",
                period_start="2026-04-25T09:30:00Z",
                period_end="2026-04-25T16:00:00Z",
                session="regular",
                adj_open=170.0,
                adj_high=175.0,
                adj_low=169.0,
                adj_close=174.0,
                adj_volume=50000000,
                unadj_open=170.0,
                unadj_high=175.0,
                unadj_low=169.0,
                unadj_close=174.0,
                unadj_volume=50000000,
                source="polygon",
                ingested_at="2026-04-25T20:00:00Z",
            )

        session.add(make_bar())
        session.commit()
        session.add(make_bar())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_macro_observations_duplicate_raises(self, session: Session) -> None:
        def make_obs() -> MacroObservations:
            return MacroObservations(
                source="fred",
                series_id="DGS10",
                observation_date="2026-04-25",
                revision_number=0,
                value=4.5,
                ingested_at="2026-04-26T00:00:00Z",
            )

        session.add(make_obs())
        session.commit()
        session.add(make_obs())
        with pytest.raises(IntegrityError):
            session.commit()

    def test_options_contract_snapshots_duplicate_raises(
        self, session: Session, seeded_universe: AssetUniverse
    ) -> None:
        contract = OptionsContracts(
            contract_ticker="O:AAPL250117C00200000",
            underlying_ticker=Symbol("AAPL"),
            expiration_date="2025-01-17",
            strike_price=200.0,
            contract_type="call",
            first_seen_at="2026-04-01T00:00:00Z",
            last_seen_at="2026-04-26T00:00:00Z",
            source="polygon",
        )
        session.add(contract)
        session.commit()

        def make_snap() -> OptionsContractSnapshots:
            return OptionsContractSnapshots(
                snapshot_ts="2026-04-26T15:00:00Z",
                contract_ticker="O:AAPL250117C00200000",
                underlying_ticker=Symbol("AAPL"),
                source="polygon",
                ingested_at="2026-04-26T15:01:00Z",
            )

        session.add(make_snap())
        session.commit()
        session.add(make_snap())
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# AC: Foreign-key constraint enforcement
# ---------------------------------------------------------------------------


class TestForeignKeyConstraints:
    def test_sector_classification_fk_ticker(self, session: Session) -> None:
        """ticker must exist in asset_universe."""
        row = SectorClassification(
            ticker=Symbol("NONEXISTENT"),
            asset_id="asset-xxx",
            alphamind_sector="tech",
            domain_researcher="tech_semis",
            sector_etf="XLK",
            classification_source="polygon",
            last_updated="2026-04-26T00:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_ohlcv_bars_fk_ticker(self, session: Session) -> None:
        """ticker must exist in asset_universe."""
        row = OhlcvBars(
            ticker=Symbol("NONEXISTENT"),
            timeframe="1d",
            period_start="2026-04-25T09:30:00Z",
            period_end="2026-04-25T16:00:00Z",
            session="regular",
            adj_open=170.0,
            adj_high=175.0,
            adj_low=169.0,
            adj_close=174.0,
            adj_volume=50000000,
            unadj_open=170.0,
            unadj_high=175.0,
            unadj_low=169.0,
            unadj_close=174.0,
            unadj_volume=50000000,
            source="polygon",
            ingested_at="2026-04-25T20:00:00Z",
        )
        session.add(row)
        with pytest.raises(IntegrityError):
            session.commit()

    def test_news_article_tickers_cascade_delete(
        self, engine: Engine, seeded_universe: AssetUniverse
    ) -> None:
        """
        Deleting an article via raw SQL should cascade to news_article_tickers
        (ON DELETE CASCADE declared in FK).  Uses a fresh session so the ORM
        identity map doesn't shadow the DB-level delete.
        """
        session_factory = make_session_factory(engine)
        with session_factory() as sess:
            # Insert parent universe row for FK satisfaction
            sess.merge(seeded_universe)
            article = NewsArticles(
                article_id="art-cascade",
                source="finnhub",
                language="en",
                headline_text="Test cascade",
                published_at="2026-04-26T12:00:00Z",
                ingested_at="2026-04-26T12:05:00Z",
            )
            sess.add(article)
            sess.flush()
            link = NewsArticleTickers(
                article_id="art-cascade",
                ticker=Symbol("AAPL"),
                is_primary=1,
            )
            sess.add(link)
            sess.commit()

            # Delete via raw SQL to exercise DB-level cascade
            sess.execute(text("DELETE FROM news_articles WHERE article_id = 'art-cascade'"))
            sess.commit()

        # Open a fresh session — no identity map interference
        with session_factory() as sess2:
            fetched = sess2.get(NewsArticleTickers, ("art-cascade", "AAPL"))
            assert fetched is None

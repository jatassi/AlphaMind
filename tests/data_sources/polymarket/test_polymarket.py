"""
Tests for src/alphamind/data_sources/polymarket/ — story 05i.

All HTTP calls are mocked.  No real network access.  Each test class targets
one acceptance criterion from the story spec.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from alphamind.persistence.models import (
    Base,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_FETCH_MARKETS = "alphamind.data_sources.polymarket.contracts._fetch_markets"
_FETCH_PRICES = "alphamind.data_sources.polymarket.contracts._fetch_prices"
_NOW = "alphamind.data_sources.polymarket.contracts._now"


def _make_market(**overrides: object) -> dict:
    """Build a minimal Gamma market dict with sensible defaults."""
    base: dict = {
        "conditionId": "cid-001",
        "question": "Will the Fed cut rates in May?",
        "tags": ["FED", "Monetary Policy"],
        "liquidity": 50_000.0,
        "volume24hr": 100_000.0,
        "closed": False,
        "endDate": "2026-05-01",
        "createdAt": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _make_price_response(yes_price: float = 0.65, bid: float = 0.64, ask: float = 0.66) -> dict:
    """Build a minimal CLOB /prices response."""
    return {"yes": yes_price, "no": 1.0 - yes_price, "bid": bid, "ask": ask}


def _make_session_and_engine():
    """Create a fresh in-memory SQLite engine+session for each test."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


class _FakeRunRepo:
    """In-memory stand-in for the persistence layer used by track_run."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def insert_running(self, run_id: str, collector: str, started_at: str) -> None:
        self.rows[run_id] = {
            "run_id": run_id,
            "collector": collector,
            "started_at": started_at,
            "status": "running",
            "completed_at": None,
            "rows_written": None,
            "error_summary": None,
        }

    def update_success(self, run_id: str, completed_at: str, rows_written: int) -> None:
        self.rows[run_id].update(
            status="success", completed_at=completed_at, rows_written=rows_written
        )

    def update_failed(self, run_id: str, error_summary: str) -> None:
        self.rows[run_id].update(status="failed", error_summary=error_summary)


_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# AC: verify_connectivity exits 0 on successful Gamma /markets reach
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_when_gamma_responds(self) -> None:
        """verify_connectivity() returns True on a successful Gamma /markets GET."""
        from alphamind.data_sources.polymarket.client import PolymarketClient

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = [_make_market()]
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        assert PolymarketClient(http_client=mock_http).verify_connectivity() is True

    def test_returns_false_when_gamma_raises(self) -> None:
        """verify_connectivity() returns False when the HTTP call raises."""
        from alphamind.data_sources.polymarket.client import PolymarketClient

        mock_http = MagicMock()
        mock_http.get.side_effect = httpx.ConnectError("unreachable")

        assert PolymarketClient(http_client=mock_http).verify_connectivity() is False


# ---------------------------------------------------------------------------
# AC: client integrates with_retries(optional), RateLimiter, and track_run
# ---------------------------------------------------------------------------


class TestClientPrimitives:
    def test_client_exposes_rate_limiter(self) -> None:
        """PolymarketClient accepts and stores a RateLimiter."""
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.polymarket.client import PolymarketClient

        limiter = RateLimiter()
        assert PolymarketClient(rate_limiter=limiter).rate_limiter is limiter

    def test_get_markets_calls_rate_limiter(self) -> None:
        """Each call to get_markets() acquires a token from the rate limiter."""
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.polymarket.client import PolymarketClient

        limiter = RateLimiter()
        limiter.set_limit("polymarket", rate_per_minute=1800)
        acquire_calls: list[str] = []
        original_acquire = limiter.acquire

        def tracking_acquire(provider: str) -> None:
            acquire_calls.append(provider)
            original_acquire(provider)

        limiter.acquire = tracking_acquire  # type: ignore[method-assign]

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = []
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        PolymarketClient(http_client=mock_http, rate_limiter=limiter).get_markets(limit=10)
        assert "polymarket" in acquire_calls

    def test_get_prices_uses_with_retries_optional(self) -> None:
        """get_prices() retries once on a transient 503 error (optional shape)."""
        from alphamind.data_sources.polymarket.client import PolymarketClient

        call_count = 0

        def side_effect(url: str, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                request = httpx.Request("GET", url)
                response = httpx.Response(503, request=request)
                raise httpx.HTTPStatusError("503", request=request, response=response)
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = _make_price_response()
            return mock_resp

        mock_http = MagicMock()
        mock_http.get.side_effect = side_effect

        result = PolymarketClient(http_client=mock_http, _sleep=lambda s: None).get_prices(
            "cid-001"
        )
        assert result is not None
        assert call_count == 2


# ---------------------------------------------------------------------------
# AC: collect_snapshots UPSERTs prediction_market_contracts rows
# ---------------------------------------------------------------------------


class TestUpsertContracts:
    def test_new_contract_is_inserted(self) -> None:
        """A new in-scope market is inserted into prediction_market_contracts."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-001", tags=["FED"])]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=_make_price_response()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-001")
            assert contract is not None
            assert contract.platform == "polymarket"

    def test_second_run_updates_last_seen_at(self) -> None:
        """Re-running on the same contract updates last_seen_at without inserting a duplicate."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-002", tags=["FED"])]
        prices = _make_price_response()
        ts1 = datetime(2026, 4, 1, tzinfo=UTC)
        ts2 = datetime(2026, 4, 2, tzinfo=UTC)

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=prices),
            patch(_NOW, return_value=ts1),
        ):
            collect_snapshots(since=ts1, _session_factory=session_factory, _repo=_FakeRunRepo())

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=prices),
            patch(_NOW, return_value=ts2),
        ):
            collect_snapshots(since=ts2, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            rows = sess.query(PredictionMarketContracts).filter_by(contract_id="cid-002").all()
            assert len(rows) == 1
            assert rows[0].last_seen_at == ts2.isoformat()


# ---------------------------------------------------------------------------
# AC: category derivation uses documented mapping; unknown → "other" + warn
# ---------------------------------------------------------------------------


class TestCategoryDerivation:
    def _run(self, session_factory, condition_id: str, tags: list[str]) -> str:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        markets = [_make_market(conditionId=condition_id, tags=tags)]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=_make_price_response()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())
        with session_factory() as sess:
            contract = sess.get(PredictionMarketContracts, condition_id)
            assert contract is not None
            return contract.category

    def test_fed_tags_map_to_monetary_policy(self) -> None:
        """Markets tagged with FED-related tags map to 'monetary_policy'."""
        _engine, sf = _make_session_and_engine()
        assert self._run(sf, "cid-fed", ["FED", "FOMC"]) == "monetary_policy"

    def test_antitrust_tags_map_to_antitrust(self) -> None:
        """Markets tagged with antitrust-related tags map to 'antitrust'."""
        _engine, sf = _make_session_and_engine()
        assert self._run(sf, "cid-antitrust", ["Antitrust", "DOJ"]) == "antitrust"

    def test_opec_tags_map_to_opec(self) -> None:
        """Markets tagged with OPEC map to 'opec'."""
        _engine, sf = _make_session_and_engine()
        assert self._run(sf, "cid-opec", ["OPEC", "Oil"]) == "opec"

    def test_unknown_tags_default_to_other_and_warn(self, caplog) -> None:
        """Unknown category produces 'other' and a WARNING log."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-other", tags=["Celebrity", "Entertainment"])]

        with (
            caplog.at_level(logging.WARNING, logger="alphamind.data_sources.polymarket.contracts"),
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=_make_price_response()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-other")
            assert contract is not None
            assert contract.category == "other"

        assert any(
            "other" in r.message.lower() or "unknown" in r.message.lower() for r in caplog.records
        )


# ---------------------------------------------------------------------------
# AC: prediction_market_snapshots rows written per active contract per fire
# ---------------------------------------------------------------------------


class TestSnapshotRows:
    def test_snapshot_written_with_correct_fields(self) -> None:
        """One snapshot row per active contract with all required fields."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        markets = [_make_market(conditionId="cid-snap", tags=["FED"], volume24hr=200_000.0)]
        prices = _make_price_response(yes_price=0.72, bid=0.71, ask=0.73)

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=prices),
            patch(_NOW, return_value=now),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            snap = sess.get(PredictionMarketSnapshots, ("cid-snap", now.isoformat()))
            assert snap is not None
            assert snap.yes_probability == pytest.approx(0.72)
            assert snap.bid == pytest.approx(0.71)
            assert snap.ask == pytest.approx(0.73)
            assert snap.volume_24h_usd == pytest.approx(200_000.0)
            assert snap.liquidity_usd == pytest.approx(50_000.0)

    def test_no_duplicate_snapshots_on_rerun(self) -> None:
        """Re-running with the same timestamp produces no duplicate snapshot rows."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        markets = [_make_market(conditionId="cid-dedup", tags=["FED"])]
        prices = _make_price_response()

        for _ in range(2):
            with (
                patch(_FETCH_MARKETS, return_value=markets),
                patch(_FETCH_PRICES, return_value=prices),
                patch(_NOW, return_value=now),
            ):
                collect_snapshots(
                    since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo()
                )

        with session_factory() as sess:
            snaps = sess.query(PredictionMarketSnapshots).filter_by(contract_id="cid-dedup").all()
            assert len(snaps) == 1


# ---------------------------------------------------------------------------
# AC: markets below liquidity_usd < 10_000 are skipped
# ---------------------------------------------------------------------------


class TestLiquidityFilter:
    def test_low_liquidity_market_is_skipped(self) -> None:
        """Markets with liquidity_usd < 10_000 are not written to the DB."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-low", tags=["FED"], liquidity=5_000.0)]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=_make_price_response()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            assert sess.get(PredictionMarketContracts, "cid-low") is None

    def test_exactly_10000_liquidity_is_included(self) -> None:
        """Markets with liquidity_usd == 10_000 are included (boundary)."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-boundary", tags=["FED"], liquidity=10_000.0)]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=_make_price_response()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            assert sess.get(PredictionMarketContracts, "cid-boundary") is not None


# ---------------------------------------------------------------------------
# AC: closed markets update resolution_outcome on the contract row
# ---------------------------------------------------------------------------


class TestClosedMarkets:
    def test_closed_market_sets_resolution_outcome(self) -> None:
        """A closed market with an outcome updates resolution_outcome on the contract."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        prices = _make_price_response()

        open_market = _make_market(conditionId="cid-close", tags=["FED"])
        with (
            patch(_FETCH_MARKETS, return_value=[open_market]),
            patch(_FETCH_PRICES, return_value=prices),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        closed_market = _make_market(
            conditionId="cid-close", tags=["FED"], closed=True, outcome="yes"
        )
        with (
            patch(_FETCH_MARKETS, return_value=[closed_market]),
            patch(_FETCH_PRICES, return_value=prices),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-close")
            assert contract is not None
            assert contract.resolution_outcome == "yes"

    def test_closed_market_without_outcome_sets_undecided(self) -> None:
        """A closed market with no outcome sets resolution_outcome to 'undecided'."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-undecided", tags=["FED"], closed=True)]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=_make_price_response()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-undecided")
            assert contract is not None
            assert contract.resolution_outcome == "undecided"


# ---------------------------------------------------------------------------
# AC: on failure, collection_runs records 'failed'; no data rows written
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_collection_run_records_failed_on_exception(self) -> None:
        """When _fetch_markets raises, the run repo records 'failed'."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        run_repo = _FakeRunRepo()

        with (
            patch(_FETCH_MARKETS, side_effect=RuntimeError("network failure")),
            pytest.raises(RuntimeError, match="network failure"),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=run_repo)

        row = next(iter(run_repo.rows.values()))
        assert row["status"] == "failed"
        assert "network failure" in row["error_summary"]

    def test_no_data_rows_written_on_failure(self) -> None:
        """When collection fails, no partial contract rows are committed."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()

        with (
            patch(_FETCH_MARKETS, side_effect=RuntimeError("partial failure")),
            pytest.raises(RuntimeError),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            assert sess.query(PredictionMarketContracts).count() == 0


# ---------------------------------------------------------------------------
# AC: non-binary markets are skipped with a warn-log
# ---------------------------------------------------------------------------


class TestNonBinaryMarkets:
    def test_non_binary_market_is_skipped_with_warn(self, caplog) -> None:
        """A market returning non-binary prices is skipped and a WARNING is logged."""
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, session_factory = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-nonbinary", tags=["FED"])]
        non_binary_prices: dict = {"outcome_a": 0.33, "outcome_b": 0.33, "outcome_c": 0.34}

        with (
            caplog.at_level(logging.WARNING, logger="alphamind.data_sources.polymarket.contracts"),
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_PRICES, return_value=non_binary_prices),
        ):
            collect_snapshots(since=_SINCE, _session_factory=session_factory, _repo=_FakeRunRepo())

        with session_factory() as sess:
            assert sess.query(PredictionMarketSnapshots).count() == 0

        assert any(
            "non-binary" in r.message.lower() or "binary" in r.message.lower()
            for r in caplog.records
        )

"""
Tests for src/alphamind/data_sources/polymarket/ — story 05i.

All HTTP calls are mocked.  No real network access.
"""

from __future__ import annotations

import json
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
_NOW = "alphamind.data_sources.polymarket.contracts._now"


def _make_market(
    *,
    yes_price: float = 0.65,
    bid: float = 0.64,
    ask: float = 0.66,
    outcomes: list[str] | None = None,
    outcome_prices: list[str] | None = None,
    **overrides: object,
) -> dict:
    """Build a minimal Gamma market dict matching the live shape."""
    if outcomes is None:
        outcomes = ["Yes", "No"]
    if outcome_prices is None:
        outcome_prices = [f"{yes_price}", f"{1.0 - yes_price}"]
    base: dict = {
        "conditionId": "cid-001",
        "question": "Will the Fed cut rates in May?",
        "tags": ["FED", "Monetary Policy"],
        "liquidity": 50_000.0,
        "volume24hr": 100_000.0,
        "closed": False,
        "endDate": "2026-05-01",
        "createdAt": "2026-01-01T00:00:00Z",
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps(outcome_prices),
        "bestBid": bid,
        "bestAsk": ask,
    }
    base.update(overrides)
    return base


def _make_session_and_engine():
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


class _FakeRunRepo:
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
# verify_connectivity
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_when_gamma_responds(self) -> None:
        from alphamind.data_sources.polymarket.client import PolymarketClient

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = [_make_market()]
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        assert PolymarketClient(http_client=mock_http).verify_connectivity() is True

    def test_returns_false_when_gamma_raises(self) -> None:
        from alphamind.data_sources.polymarket.client import PolymarketClient

        mock_http = MagicMock()
        mock_http.get.side_effect = httpx.ConnectError("unreachable")
        assert PolymarketClient(http_client=mock_http).verify_connectivity() is False


# ---------------------------------------------------------------------------
# Client primitives
# ---------------------------------------------------------------------------


class TestClientPrimitives:
    def test_client_exposes_rate_limiter(self) -> None:
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.polymarket.client import PolymarketClient

        limiter = RateLimiter()
        assert PolymarketClient(rate_limiter=limiter).rate_limiter is limiter

    def test_get_markets_calls_rate_limiter(self) -> None:
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.polymarket.client import PolymarketClient

        limiter = RateLimiter()
        limiter.set_limit("polymarket", rate_per_minute=1800)
        acquire_calls: list[str] = []
        original = limiter.acquire

        def tracking(provider: str) -> None:
            acquire_calls.append(provider)
            original(provider)

        limiter.acquire = tracking  # type: ignore[method-assign]

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = []
        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp

        PolymarketClient(http_client=mock_http, rate_limiter=limiter).get_markets(limit=10)
        assert "polymarket" in acquire_calls


# ---------------------------------------------------------------------------
# UPSERT contracts
# ---------------------------------------------------------------------------


class TestUpsertContracts:
    def test_new_contract_is_inserted(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-001", tags=["FED"])]

        with patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-001")
            assert contract is not None
            assert contract.platform == "polymarket"

    def test_second_run_updates_last_seen_at(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-002", tags=["FED"])]
        ts1 = datetime(2026, 4, 1, tzinfo=UTC)
        ts2 = datetime(2026, 4, 2, tzinfo=UTC)

        with patch(_FETCH_MARKETS, return_value=markets), patch(_NOW, return_value=ts1):
            collect_snapshots(since=ts1, _session_factory=sf, _repo=_FakeRunRepo())
        with patch(_FETCH_MARKETS, return_value=markets), patch(_NOW, return_value=ts2):
            collect_snapshots(since=ts2, _session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            rows = sess.query(PredictionMarketContracts).filter_by(contract_id="cid-002").all()
            assert len(rows) == 1
            assert rows[0].last_seen_at == ts2.isoformat()


# ---------------------------------------------------------------------------
# Category derivation
# ---------------------------------------------------------------------------


class TestCategoryDerivation:
    def _category(self, sf, condition_id: str, tags: list[str]) -> str:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        markets = [_make_market(conditionId=condition_id, tags=tags)]
        with patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, condition_id)
            assert contract is not None
            return contract.category

    def test_fed_tags_map_to_monetary_policy(self) -> None:
        _engine, sf = _make_session_and_engine()
        assert self._category(sf, "cid-fed", ["FED", "FOMC"]) == "monetary_policy"

    def test_antitrust_tags_map_to_antitrust(self) -> None:
        _engine, sf = _make_session_and_engine()
        assert self._category(sf, "cid-anti", ["antitrust"]) == "antitrust"

    def test_opec_tags_map_to_opec(self) -> None:
        _engine, sf = _make_session_and_engine()
        assert self._category(sf, "cid-opec", ["OPEC"]) == "opec"

    def test_unknown_tags_default_to_other_and_warn(self, caplog) -> None:
        _engine, sf = _make_session_and_engine()
        with caplog.at_level(logging.WARNING):
            assert self._category(sf, "cid-unk", ["random_unknown"]) == "other"
        assert any("unknown category" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Snapshot fields
# ---------------------------------------------------------------------------


class TestSnapshotRows:
    def test_snapshot_written_with_correct_fields(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        markets = [
            _make_market(
                conditionId="cid-snap",
                tags=["FED"],
                yes_price=0.72,
                bid=0.71,
                ask=0.73,
                liquidity=50_000.0,
                volume24hr=200_000.0,
            )
        ]
        with patch(_FETCH_MARKETS, return_value=markets), patch(_NOW, return_value=now):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            snap = sess.get(PredictionMarketSnapshots, ("cid-snap", now.isoformat()))
            assert snap is not None
            assert snap.yes_probability == pytest.approx(0.72)
            assert snap.bid == pytest.approx(0.71)
            assert snap.ask == pytest.approx(0.73)
            assert snap.volume_24h_usd == pytest.approx(200_000.0)
            assert snap.liquidity_usd == pytest.approx(50_000.0)

    def test_no_duplicate_snapshots_on_rerun(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        markets = [_make_market(conditionId="cid-dedup", tags=["FED"])]
        for _ in range(2):
            with patch(_FETCH_MARKETS, return_value=markets), patch(_NOW, return_value=now):
                collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            snaps = sess.query(PredictionMarketSnapshots).filter_by(contract_id="cid-dedup").all()
            assert len(snaps) == 1


# ---------------------------------------------------------------------------
# Liquidity filter
# ---------------------------------------------------------------------------


class TestLiquidityFilter:
    def test_low_liquidity_market_is_skipped(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-low", tags=["FED"], liquidity=5_000.0)]

        with patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            assert sess.get(PredictionMarketContracts, "cid-low") is None

    def test_exactly_10000_liquidity_is_included(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-edge", tags=["FED"], liquidity=10_000.0)]

        with patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            assert sess.get(PredictionMarketContracts, "cid-edge") is not None


# ---------------------------------------------------------------------------
# Closed markets
# ---------------------------------------------------------------------------


class TestClosedMarkets:
    def test_closed_market_sets_resolution_outcome(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [
            _make_market(conditionId="cid-close", tags=["FED"], closed=True, outcome="yes"),
        ]
        with patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-close")
            assert contract is not None
            assert contract.resolution_outcome == "yes"

    def test_closed_market_without_outcome_sets_undecided(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-undecided", tags=["FED"], closed=True)]
        with patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-undecided")
            assert contract is not None
            assert contract.resolution_outcome == "undecided"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_no_data_rows_written_on_failure(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        run_repo = _FakeRunRepo()

        with (
            patch(_FETCH_MARKETS, side_effect=RuntimeError("partial failure")),
            pytest.raises(RuntimeError),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=run_repo)

        with sf() as sess:
            assert sess.query(PredictionMarketContracts).count() == 0
        row = next(iter(run_repo.rows.values()))
        assert row["status"] == "failed"


# ---------------------------------------------------------------------------
# Non-binary / missing prices
# ---------------------------------------------------------------------------


class TestNonBinaryMarkets:
    def test_market_with_no_outcome_prices_is_skipped(self, caplog) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-nonbin", tags=["FED"], outcome_prices=[])]
        with caplog.at_level(logging.WARNING), patch(_FETCH_MARKETS, return_value=markets):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=_FakeRunRepo())

        with sf() as sess:
            assert sess.query(PredictionMarketSnapshots).count() == 0
        assert any("no parsable outcomeprices" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Runner contract
# ---------------------------------------------------------------------------


class TestNoArgsCallable:
    def test_collect_snapshots_callable_with_no_args(self) -> None:
        from alphamind.data_sources.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        with patch(_FETCH_MARKETS, return_value=[]):
            collect_snapshots(_session_factory=sf, _repo=_FakeRunRepo())
        with sf() as sess:
            assert sess.query(PredictionMarketSnapshots).count() == 0

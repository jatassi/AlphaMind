"""
Tests for ``src/alphamind/data_sources/prediction_market/polymarket/``.

All HTTP calls go through fakes — no real network access.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.models import (
    Base,
    PredictionMarketContracts,
    PredictionMarketSnapshots,
)
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.run_repo import FakeRunRepo

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

_MODULE = "alphamind.data_sources.prediction_market.polymarket.contracts"
_FETCH_MARKETS = f"{_MODULE}._fetch_markets"
_FETCH_EVENT_LABELS = f"{_MODULE}._fetch_event_labels"
_NOW = f"{_MODULE}._now"


def _make_market(
    *,
    yes_price: float = 0.65,
    bid: float = 0.64,
    ask: float = 0.66,
    outcomes: list[str] | None = None,
    outcome_prices: list[str] | None = None,
    event_id: str = "ev-001",
    **overrides: object,
) -> dict[str, Any]:
    """Build a minimal Gamma market dict matching the live shape."""
    if outcomes is None:
        outcomes = ["Yes", "No"]
    if outcome_prices is None:
        outcome_prices = [f"{yes_price}", f"{1.0 - yes_price}"]
    base: dict[str, Any] = {
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
        "events": [{"id": event_id}],
    }
    base.update(overrides)
    return base


def _labels(
    event_id: str = "ev-001", labels: tuple[str, ...] = ("Politics",)
) -> dict[str, tuple[str, ...]]:
    return {event_id: labels}


def _make_session_and_engine() -> tuple[Engine, sessionmaker[Session]]:
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return engine, make_session_factory(engine)


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(timeout=30.0, transport=httpx.MockTransport(handler))


_SINCE = datetime(2026, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# verify_connectivity
# ---------------------------------------------------------------------------


class TestVerifyConnectivity:
    def test_returns_true_when_gamma_responds(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[_make_market()])

        assert PolymarketClient(http_client=_http_client(handler)).verify_connectivity() is True

    def test_returns_false_when_gamma_raises(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        assert PolymarketClient(http_client=_http_client(handler)).verify_connectivity() is False


# ---------------------------------------------------------------------------
# Client primitives
# ---------------------------------------------------------------------------


class TestClientPrimitives:
    def test_client_exposes_rate_limiter(self) -> None:
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

        limiter = RateLimiter()
        assert PolymarketClient(rate_limiter=limiter).rate_limiter is limiter

    def test_get_markets_calls_rate_limiter(self) -> None:
        from alphamind.data_sources._common import RateLimiter
        from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

        limiter = RateLimiter()
        limiter.set_limit("polymarket", rate_per_minute=1800)
        acquire_calls: list[str] = []
        original = limiter.acquire

        def tracking(provider: str) -> None:
            acquire_calls.append(provider)
            original(provider)

        limiter.acquire = tracking  # type: ignore[method-assign]  # mock-method assignment

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        PolymarketClient(http_client=_http_client(handler), rate_limiter=limiter).get_markets(
            limit=10
        )
        assert "polymarket" in acquire_calls

    def test_get_events_by_ids_returns_events(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=[
                    {"id": "ev-1", "tags": [{"label": "Politics"}, {"label": "World"}]},
                    {"id": "ev-2", "tags": [{"label": "Crypto"}]},
                ],
            )

        client = PolymarketClient(http_client=_http_client(handler), _sleep=lambda _s: None)
        events = client.get_events_by_ids(["ev-1", "ev-2"])
        assert len(events) == 2
        assert events[0]["tags"][0]["label"] == "Politics"

    def test_get_events_by_ids_empty_input_skips_request(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.client import PolymarketClient

        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=[])

        client = PolymarketClient(http_client=_http_client(handler), _sleep=lambda _s: None)
        assert client.get_events_by_ids([]) == []
        # No HTTP request issued
        assert captured == []


# ---------------------------------------------------------------------------
# UPSERT contracts
# ---------------------------------------------------------------------------


class TestUpsertContracts:
    def test_new_contract_is_inserted(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-001")]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-001")
            assert contract is not None
            assert contract.platform == "polymarket"

    def test_second_run_updates_last_seen_at(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-002")]
        ts1 = datetime(2026, 4, 1, tzinfo=UTC)
        ts2 = datetime(2026, 4, 2, tzinfo=UTC)

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
            patch(_NOW, return_value=ts1),
        ):
            collect_snapshots(since=ts1, _session_factory=sf, _repo=FakeRunRepo())
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
            patch(_NOW, return_value=ts2),
        ):
            collect_snapshots(since=ts2, _session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            rows = sess.query(PredictionMarketContracts).filter_by(contract_id="cid-002").all()
            assert len(rows) == 1
            assert rows[0].last_seen_at == ts2.isoformat()


# ---------------------------------------------------------------------------
# Category derivation
# ---------------------------------------------------------------------------


class TestCategoryDerivation:
    def _category(
        self,
        sf: sessionmaker[Session],
        condition_id: str,
        question: str,
        labels: tuple[str, ...],
    ) -> str:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        markets = [_make_market(conditionId=condition_id, question=question, event_id="ev-x")]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value={"ev-x": labels}),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, condition_id)
            assert contract is not None
            return contract.category

    def test_fed_question_with_politics_label_maps_to_monetary_policy(self) -> None:
        _engine, sf = _make_session_and_engine()
        assert (
            self._category(
                sf,
                "cid-fed",
                "Will the Fed cut rates in May?",
                ("Politics", "Finance"),
            )
            == "monetary_policy"
        )

    def test_no_passthrough_label_falls_through_to_other(self) -> None:
        _engine, sf = _make_session_and_engine()
        assert (
            self._category(
                sf,
                "cid-other",
                "Will the Fed cut rates in May?",
                ("Crypto", "Sports"),
            )
            == "other"
        )

    def test_empty_vendor_labels_default_to_other(self) -> None:
        _engine, sf = _make_session_and_engine()
        assert self._category(sf, "cid-empty", "Will the Fed cut rates?", ()) == "other"


# ---------------------------------------------------------------------------
# Snapshot fields
# ---------------------------------------------------------------------------


class TestSnapshotRows:
    def test_snapshot_written_with_correct_fields(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        markets = [
            _make_market(
                conditionId="cid-snap",
                yes_price=0.72,
                bid=0.71,
                ask=0.73,
                liquidity=50_000.0,
                volume24hr=200_000.0,
            )
        ]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
            patch(_NOW, return_value=now),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            snap = sess.get(PredictionMarketSnapshots, ("cid-snap", now.isoformat()))
            assert snap is not None
            assert snap.yes_probability == pytest.approx(0.72)
            assert snap.bid == pytest.approx(0.71)
            assert snap.ask == pytest.approx(0.73)
            assert snap.volume_24h_usd == pytest.approx(200_000.0)
            assert snap.liquidity_usd == pytest.approx(50_000.0)

    def test_no_duplicate_snapshots_on_rerun(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        now = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
        markets = [_make_market(conditionId="cid-dedup")]
        for _ in range(2):
            with (
                patch(_FETCH_MARKETS, return_value=markets),
                patch(_FETCH_EVENT_LABELS, return_value=_labels()),
                patch(_NOW, return_value=now),
            ):
                collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            snaps = sess.query(PredictionMarketSnapshots).filter_by(contract_id="cid-dedup").all()
            assert len(snaps) == 1


# ---------------------------------------------------------------------------
# Liquidity filter
# ---------------------------------------------------------------------------


class TestLiquidityFilter:
    def test_low_liquidity_market_is_skipped(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-low", liquidity=5_000.0)]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            assert sess.get(PredictionMarketContracts, "cid-low") is None

    def test_exactly_10000_liquidity_is_included(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-edge", liquidity=10_000.0)]

        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            assert sess.get(PredictionMarketContracts, "cid-edge") is not None


# ---------------------------------------------------------------------------
# Closed markets — resolution_outcome derived from outcomePrices
# ---------------------------------------------------------------------------


class TestClosedMarkets:
    def test_resolved_yes_when_first_price_dominant_above_threshold(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [
            _make_market(
                conditionId="cid-yes",
                closed=True,
                outcome_prices=["0.99", "0.01"],
            )
        ]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-yes")
            assert contract is not None
            assert contract.resolution_outcome == "yes"

    def test_resolved_no_when_second_price_dominant_above_threshold(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [
            _make_market(
                conditionId="cid-no",
                closed=True,
                outcome_prices=["0.005", "0.995"],
            )
        ]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-no")
            assert contract is not None
            assert contract.resolution_outcome == "no"

    def test_canceled_when_both_prices_zero(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [
            _make_market(
                conditionId="cid-cancel",
                closed=True,
                outcome_prices=["0", "0"],
            )
        ]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-cancel")
            assert contract is not None
            assert contract.resolution_outcome == "canceled"

    def test_undecided_below_threshold_leaves_resolution_null(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [
            _make_market(
                conditionId="cid-mid",
                closed=True,
                outcome_prices=["0.55", "0.45"],
            )
        ]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-mid")
            assert contract is not None
            assert contract.resolution_outcome is None

    def test_open_market_has_null_resolution(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [
            _make_market(
                conditionId="cid-open",
                closed=False,
                outcome_prices=["0.99", "0.01"],
            )
        ]
        with (
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            contract = sess.get(PredictionMarketContracts, "cid-open")
            assert contract is not None
            assert contract.resolution_outcome is None


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_no_data_rows_written_on_failure(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        run_repo = FakeRunRepo()

        with (
            patch(_FETCH_MARKETS, side_effect=RuntimeError("partial failure")),
            pytest.raises(RuntimeError),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=run_repo)

        with sf() as sess:
            assert sess.query(PredictionMarketContracts).count() == 0
        assert run_repo.failed()


# ---------------------------------------------------------------------------
# Non-binary / missing prices
# ---------------------------------------------------------------------------


class TestNonBinaryMarkets:
    def test_market_with_no_outcome_prices_is_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        markets = [_make_market(conditionId="cid-nonbin", outcome_prices=[])]
        with (
            caplog.at_level(logging.WARNING),
            patch(_FETCH_MARKETS, return_value=markets),
            patch(_FETCH_EVENT_LABELS, return_value=_labels()),
        ):
            collect_snapshots(since=_SINCE, _session_factory=sf, _repo=FakeRunRepo())

        with sf() as sess:
            assert sess.query(PredictionMarketSnapshots).count() == 0
        assert any("no parsable outcomeprices" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# Runner contract
# ---------------------------------------------------------------------------


class TestNoArgsCallable:
    def test_collect_snapshots_callable_with_no_args(self) -> None:
        from alphamind.data_sources.prediction_market.polymarket.contracts import collect_snapshots

        _engine, sf = _make_session_and_engine()
        with (
            patch(_FETCH_MARKETS, return_value=[]),
            patch(_FETCH_EVENT_LABELS, return_value={}),
        ):
            collect_snapshots(_session_factory=sf, _repo=FakeRunRepo())
        with sf() as sess:
            assert sess.query(PredictionMarketSnapshots).count() == 0

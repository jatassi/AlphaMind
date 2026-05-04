"""
Tests for ``src/alphamind/data_sources/prediction_market/kalshi/contracts.py``.

All HTTP calls are mocked — no real network traffic.
Tests use an in-memory SQLite database.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.models import Base, PredictionMarketContracts, PredictionMarketSnapshots
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    """Provide an in-memory SQLite session factory pre-populated with schema."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = make_session_factory(engine)
    return sf


def _make_events_payload(
    *,
    series: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Build an /events payload.  ``series`` is list of ``(series_ticker, category)``."""
    if series is None:
        series = [("KXFED", "Politics")]
    return {
        "events": [
            {
                "event_ticker": f"{st}-2024",
                "series_ticker": st,
                "title": f"{st} market",
                "status": "open",
                "category": cat,
            }
            for st, cat in series
        ]
    }


def _make_markets_payload(
    series_ticker: str,
    market_ticker: str = "MKT-001",
    title: str = "Will the Fed cut rates?",
    yes_bid: int = 60,
    yes_ask: int = 70,
    volume: int = 1000,
    last_price: int = 65,
    status: str = "active",
    result: str | None = None,
    close_time: str | None = None,
) -> dict[str, Any]:
    market: dict[str, Any] = {
        "ticker": market_ticker,
        "event_ticker": f"{series_ticker}-2024",
        "title": title,
        "status": status,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "volume": volume,
        "last_price": last_price,
        "close_time": close_time or "2024-12-31T00:00:00Z",
        "open_time": "2024-01-01T00:00:00Z",
    }
    if result is not None:
        market["result"] = result
    return {"markets": [market]}


def _make_mock_client(
    events_payload: dict[str, Any], markets_payloads: dict[str, dict[str, Any]]
) -> MagicMock:
    """Build a mock KalshiClient that returns given payloads."""

    def fake_get(path: str, **params: Any) -> dict[str, Any]:
        if path == "/events":
            return events_payload
        if path == "/markets":
            st = params.get("series_ticker", "")
            return markets_payloads.get(st, {"markets": []})
        return {}

    client = MagicMock()
    client.get.side_effect = fake_get
    return client


# ---------------------------------------------------------------------------
# collect_snapshots
# ---------------------------------------------------------------------------


class TestCollectSnapshots:
    def test_contracts_upserted_with_canonical_category(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Contracts derive canonical category from event.category + market.title."""
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(series=[("KXFED", "Politics")]),
            {
                "KXFED": _make_markets_payload(
                    "KXFED",
                    "KXFED-25MAR-T5.00",
                    title="Will the Fed cut rates in March?",
                )
            },
        )

        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contracts = sess.query(PredictionMarketContracts).all()
            assert len(contracts) == 1
            assert contracts[0].contract_id == "KXFED-25MAR-T5.00"
            assert contracts[0].platform == "kalshi"
            assert contracts[0].category == "monetary_policy"

    def test_no_passthrough_event_category_falls_through_to_other(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """Events with off-topic event.category short-circuit to 'other'."""
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(series=[("KXSPORTS", "Sports")]),
            {
                "KXSPORTS": _make_markets_payload(
                    "KXSPORTS",
                    "KXSPORTS-001",
                    title="Will the Fed cut rates in March?",
                )
            },
        )

        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contracts = sess.query(PredictionMarketContracts).all()
            assert len(contracts) == 1
            assert contracts[0].category == "other"

    def test_election_question_with_politics_category_maps_to_election(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(series=[("KXPRES", "Elections")]),
            {
                "KXPRES": _make_markets_payload(
                    "KXPRES",
                    "KXPRES-2024",
                    title="Will the Republican candidate win the presidential election?",
                )
            },
        )

        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contracts = sess.query(PredictionMarketContracts).all()
            assert contracts[0].category == "election"

    def test_snapshot_yes_probability_derived_correctly(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(),
            {"KXFED": _make_markets_payload("KXFED", "KXFED-001", yes_bid=60, yes_ask=70)},
        )
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            snap = sess.query(PredictionMarketSnapshots).first()
            assert snap is not None
            assert abs(snap.yes_probability - 0.65) < 1e-9  # (60+70)/200

    def test_snapshot_bid_ask_in_dollars(self, session_factory: sessionmaker[Session]) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(),
            {"KXFED": _make_markets_payload("KXFED", "KXFED-001", yes_bid=60, yes_ask=70)},
        )
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            snap = sess.query(PredictionMarketSnapshots).first()
            assert snap is not None
            assert snap.bid is not None
            assert snap.ask is not None
            assert abs(snap.bid - 0.60) < 1e-9
            assert abs(snap.ask - 0.70) < 1e-9

    def test_closed_market_sets_resolution_outcome_yes(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(),
            {
                "KXFED": _make_markets_payload(
                    "KXFED", "KXFED-001", status="finalized", result="yes"
                )
            },
        )
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contract = sess.query(PredictionMarketContracts).first()
            assert contract is not None
            assert contract.resolution_outcome == "yes"

    def test_closed_market_sets_resolution_outcome_no(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(series=[("KXPRES", "Elections")]),
            {
                "KXPRES": _make_markets_payload(
                    "KXPRES",
                    "KXPRES-001",
                    title="Will the Republican candidate win the presidential election?",
                    status="closed",
                    result="no",
                )
            },
        )
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contract = sess.query(PredictionMarketContracts).first()
            assert contract is not None
            assert contract.resolution_outcome == "no"

    def test_no_duplicate_snapshots_on_rerun(self, session_factory: sessionmaker[Session]) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(),
            {"KXFED": _make_markets_payload("KXFED", "KXFED-001")},
        )
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _snapshot_ts="2024-06-01T12:00:00+00:00",
        )
        collect_snapshots(
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _snapshot_ts="2024-06-01T12:00:00+00:00",
        )

        with session_factory() as sess:
            snaps = sess.query(PredictionMarketSnapshots).all()
            assert len(snaps) == 1

    def test_failure_records_failed_in_collection_runs(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        client = MagicMock()
        client.get.side_effect = RuntimeError("network error")

        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_failed.return_value = None

        with pytest.raises(RuntimeError, match="network error"):
            collect_snapshots(client=client, session_factory=session_factory, _repo=repo)

        repo.update_failed.assert_called_once()
        with session_factory() as sess:
            assert sess.query(PredictionMarketContracts).count() == 0
            assert sess.query(PredictionMarketSnapshots).count() == 0


# ---------------------------------------------------------------------------
# Runner contract
# ---------------------------------------------------------------------------


class TestNoArgsCallable:
    def test_collect_snapshots_callable_with_no_args(self) -> None:
        from alphamind.data_sources.prediction_market.kalshi.contracts import collect_snapshots

        engine = make_engine(":memory:")
        Base.metadata.create_all(engine)
        sf = make_session_factory(engine)

        mock_client = _make_mock_client({"events": []}, {})
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=mock_client, session_factory=sf, _repo=repo)

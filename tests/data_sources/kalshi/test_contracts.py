"""
Tests for kalshi/contracts.py.

All HTTP calls are mocked — no real network traffic.
Tests use an in-memory SQLite database.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alphamind.persistence.models import Base, PredictionMarketContracts, PredictionMarketSnapshots
from alphamind.persistence.session import make_engine, make_session_factory

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_factory():
    """Provide an in-memory SQLite session factory pre-populated with schema."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    return make_session_factory(engine)


def _make_events_payload(series_tickers: list[str]) -> dict:
    return {
        "events": [
            {
                "event_ticker": f"{st}-2024",
                "series_ticker": st,
                "title": f"{st} market",
                "status": "open",
            }
            for st in series_tickers
        ]
    }


def _make_markets_payload(
    series_ticker: str,
    market_ticker: str = "MKT-001",
    yes_bid: int = 60,
    yes_ask: int = 70,
    volume: int = 1000,
    last_price: int = 65,
    status: str = "active",
    result: str | None = None,
    close_time: str | None = None,
) -> dict:
    market = {
        "ticker": market_ticker,
        "event_ticker": f"{series_ticker}-2024",
        "title": f"{series_ticker} market question",
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


def _make_mock_client(events_payload: dict, markets_payloads: dict[str, dict]) -> MagicMock:
    """Build a mock KalshiClient that returns given payloads."""

    def fake_get(path: str, **params):
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
# collect_snapshots: contracts upserted for in-scope categories
# ---------------------------------------------------------------------------


class TestCollectSnapshots:
    def test_contracts_upserted_for_known_series(self, session_factory) -> None:
        """Contracts for known series tickers are inserted into prediction_market_contracts."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["FED"]),
            {"FED": _make_markets_payload("FED", "FED-24DEC-0525")},
        )

        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contracts = sess.query(PredictionMarketContracts).all()
            assert len(contracts) == 1
            assert contracts[0].contract_id == "FED-24DEC-0525"
            assert contracts[0].platform == "kalshi"
            assert contracts[0].category in ("monetary_policy", "fed", "other")

    def test_contracts_have_correct_category_for_fed(self, session_factory) -> None:
        """FED series_ticker maps to monetary_policy category."""
        from alphamind.data_sources.kalshi.contracts import SERIES_CATEGORY_MAP

        assert "FED" in SERIES_CATEGORY_MAP
        assert SERIES_CATEGORY_MAP["FED"] == "monetary_policy"

    def test_unknown_series_defaults_to_other(self, session_factory) -> None:
        """Unknown series_ticker maps to 'other' with a warning."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["UNKNOWN_XYZ"]),
            {"UNKNOWN_XYZ": _make_markets_payload("UNKNOWN_XYZ", "UNK-001")},
        )
        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contracts = sess.query(PredictionMarketContracts).all()
            assert len(contracts) == 1
            assert contracts[0].category == "other"

    # ------------------------------------------------------------------
    # Snapshot probability fields
    # ------------------------------------------------------------------

    def test_snapshot_yes_probability_derived_correctly(self, session_factory) -> None:
        """yes_probability = (yes_bid + yes_ask) / 200."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["FED"]),
            {"FED": _make_markets_payload("FED", "FED-001", yes_bid=60, yes_ask=70)},
        )
        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            snap = sess.query(PredictionMarketSnapshots).first()
            assert snap is not None
            assert abs(snap.yes_probability - 0.65) < 1e-9  # (60+70)/200

    def test_snapshot_bid_ask_in_dollars(self, session_factory) -> None:
        """bid = yes_bid/100, ask = yes_ask/100 (dollars)."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["FED"]),
            {"FED": _make_markets_payload("FED", "FED-001", yes_bid=60, yes_ask=70)},
        )
        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            snap = sess.query(PredictionMarketSnapshots).first()
            assert snap is not None
            assert abs(snap.bid - 0.60) < 1e-9
            assert abs(snap.ask - 0.70) < 1e-9

    # ------------------------------------------------------------------
    # Closed markets update resolution_outcome
    # ------------------------------------------------------------------

    def test_closed_market_sets_resolution_outcome_yes(self, session_factory) -> None:
        """Closed market with result='yes' sets resolution_outcome on the contract."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["FED"]),
            {"FED": _make_markets_payload("FED", "FED-001", status="finalized", result="yes")},
        )
        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contract = sess.query(PredictionMarketContracts).first()
            assert contract is not None
            assert contract.resolution_outcome == "yes"

    def test_closed_market_sets_resolution_outcome_no(self, session_factory) -> None:
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["ELECTION"]),
            {
                "ELECTION": _make_markets_payload(
                    "ELECTION", "ELEC-001", status="closed", result="no"
                )
            },
        )
        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        with session_factory() as sess:
            contract = sess.query(PredictionMarketContracts).first()
            assert contract is not None
            assert contract.resolution_outcome == "no"

    # ------------------------------------------------------------------
    # No duplicate snapshot rows on re-run
    # ------------------------------------------------------------------

    def test_no_duplicate_snapshots_on_rerun(self, session_factory) -> None:
        """Running collect_snapshots twice with the same window adds no duplicate rows."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = _make_mock_client(
            _make_events_payload(["FED"]),
            {"FED": _make_markets_payload("FED", "FED-001")},
        )
        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        # Use a fixed snapshot_ts by freezing time via a fixed ingested_at
        collect_snapshots(
            since,
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _snapshot_ts="2024-06-01T12:00:00+00:00",
        )
        collect_snapshots(
            since,
            client=client,
            session_factory=session_factory,
            _repo=repo,
            _snapshot_ts="2024-06-01T12:00:00+00:00",
        )

        with session_factory() as sess:
            snaps = sess.query(PredictionMarketSnapshots).all()
            assert len(snaps) == 1

    # ------------------------------------------------------------------
    # Failure records failed in collection_runs, no data rows
    # ------------------------------------------------------------------

    def test_failure_records_failed_in_collection_runs(self, session_factory) -> None:
        """When collection fails, the repo records failed and no data rows are written."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        client = MagicMock()
        client.get.side_effect = RuntimeError("network error")

        since = "2024-01-01T00:00:00Z"
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_failed.return_value = None

        with pytest.raises(RuntimeError, match="network error"):
            collect_snapshots(since, client=client, session_factory=session_factory, _repo=repo)

        repo.update_failed.assert_called_once()
        # No data rows
        with session_factory() as sess:
            assert sess.query(PredictionMarketContracts).count() == 0
            assert sess.query(PredictionMarketSnapshots).count() == 0

    # ------------------------------------------------------------------
    # SERIES_CATEGORY_MAP coverage
    # ------------------------------------------------------------------

    def test_category_map_covers_key_series(self) -> None:
        """SERIES_CATEGORY_MAP includes key Kalshi series tickers."""
        from alphamind.data_sources.kalshi.contracts import SERIES_CATEGORY_MAP

        for ticker in ("FED", "CPI", "OPEC", "ELECTION"):
            assert ticker in SERIES_CATEGORY_MAP, f"{ticker} missing from SERIES_CATEGORY_MAP"


# ---------------------------------------------------------------------------
# Runner contract: callable with no args (mocked client + injected session_factory)
# ---------------------------------------------------------------------------


class TestNoArgsCallable:
    def test_collect_snapshots_callable_with_no_args(self) -> None:
        """collect_snapshots() is callable with no positional args (runner contract)."""
        from alphamind.data_sources.kalshi.contracts import collect_snapshots

        engine = make_engine(":memory:")
        Base.metadata.create_all(engine)
        sf = make_session_factory(engine)

        mock_client = _make_mock_client(
            {"events": []},
            {},
        )
        repo = MagicMock()
        repo.insert_running.return_value = None
        repo.update_success.return_value = None

        collect_snapshots(client=mock_client, session_factory=sf, _repo=repo)

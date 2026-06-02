"""
Tests for the EIA vendor adapter — story 05c.

All HTTP / SDK access is routed through fakes; tests exercise public
interfaces only and survive internal refactors.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from alphamind.persistence.models import Base, MacroObservations
from alphamind.persistence.session import make_engine, make_session_factory
from tests.data_sources._fakes.eia import FakeEIAAPI
from tests.data_sources._fakes.run_repo import FakeRunRepo

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_factory() -> sessionmaker[Session]:
    """Session factory backed by in-memory SQLite."""
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    sf: sessionmaker[Session] = make_session_factory(engine)
    return sf


def _eia_response(
    frequency: str = "weekly",
    data: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal EIA v2 API response envelope.

    When ``data`` is ``None`` a single default row is used so callers that
    don't care about the data payload get something non-empty.  Pass an
    explicit ``[]`` to get an empty data array.
    """
    actual_data: list[dict[str, Any]] = (
        [{"period": "2026-04-18", "value": 442.1, "units": "MBBL"}] if data is None else data
    )
    return {
        "response": {
            "total": len(actual_data),
            "dateFormat": "YYYY-MM-DD",
            "frequency": frequency,
            "data": actual_data,
        }
    }


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), timeout=30.0)


# ---------------------------------------------------------------------------
# Behavior 1: series.py — series list structure
# ---------------------------------------------------------------------------


class TestSeriesList:
    def test_series_list_has_four_entries(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        assert len(SERIES) == 4

    def test_each_entry_has_required_keys(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        required = {"series_id", "route", "facets", "frequency"}
        for entry in SERIES:
            assert required.issubset(entry.keys()), f"Missing keys in {entry}"

    def test_series_ids_are_eia_prefixed(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        for entry in SERIES:
            assert entry["series_id"].startswith("eia."), (
                f"series_id {entry['series_id']!r} must start with 'eia.'"
            )

    def test_crude_inventory_entry(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        ids = {e["series_id"]: e for e in SERIES}
        assert "eia.crude_inventory_total" in ids
        entry = ids["eia.crude_inventory_total"]
        assert entry["route"] == "/v2/petroleum/stoc/wstk/"
        assert entry["frequency"] == "weekly"
        assert "EPC0" in str(entry["facets"])

    def test_nat_gas_entry(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        ids = {e["series_id"]: e for e in SERIES}
        assert "eia.nat_gas_storage_lower_48" in ids
        entry = ids["eia.nat_gas_storage_lower_48"]
        assert entry["route"] == "/v2/natural-gas/stor/wkly/"
        assert entry["frequency"] == "weekly"

    def test_wti_spot_entry(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        ids = {e["series_id"]: e for e in SERIES}
        assert "eia.wti_spot_price" in ids
        entry = ids["eia.wti_spot_price"]
        assert entry["route"] == "/v2/petroleum/pri/spt/"
        assert entry["frequency"] == "daily"
        assert "RWTC" in str(entry["facets"])

    def test_refinery_utilization_entry(self) -> None:
        from alphamind.data_sources.eia.series import SERIES

        ids = {e["series_id"]: e for e in SERIES}
        assert "eia.refinery_utilization" in ids
        entry = ids["eia.refinery_utilization"]
        assert entry["route"] == "/v2/petroleum/pnp/wiup/"
        assert entry["frequency"] == "weekly"


# ---------------------------------------------------------------------------
# Behavior 2: EIAClient.fetch_series — parses response correctly
# ---------------------------------------------------------------------------


class TestEIAClientFetchSeries:
    def _client_returning(self, payload: dict[str, Any]) -> Any:
        from alphamind.data_sources.eia.client import EIAClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        return EIAClient(api_key="test_key", _http_client=_http_client(handler))

    def test_returns_list_of_data_points(self) -> None:
        payload = _eia_response(data=[{"period": "2026-04-18", "value": 442.1, "units": "MBBL"}])
        client = self._client_returning(payload)
        results = client.fetch_series(
            route="/v2/petroleum/stoc/wstk/",
            facets={"product": ["EPC0"]},
            frequency="weekly",
            since=date(2026, 1, 1),
        )
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["period"] == "2026-04-18"
        assert results[0]["value"] == 442.1

    def test_includes_api_key_in_request(self) -> None:
        from alphamind.data_sources.eia.client import EIAClient

        captured_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_requests.append(request)
            return httpx.Response(200, json=_eia_response(data=[]))

        client = EIAClient(api_key="secret_key", _http_client=_http_client(handler))
        client.fetch_series(
            route="/v2/petroleum/stoc/wstk/",
            facets={},
            frequency="weekly",
            since=date(2026, 1, 1),
        )

        # api_key appears in the URL params
        assert captured_requests[0].url.params.get("api_key") == "secret_key"

    def test_raises_on_http_error(self) -> None:
        from alphamind.data_sources.eia.client import EIAClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        client = EIAClient(api_key="test_key", _http_client=_http_client(handler))
        with pytest.raises(httpx.HTTPStatusError):
            client.fetch_series(
                route="/v2/petroleum/stoc/wstk/",
                facets={},
                frequency="weekly",
                since=date(2026, 1, 1),
            )

    def test_empty_response_returns_empty_list(self) -> None:
        payload = _eia_response(data=[])
        client = self._client_returning(payload)
        results = client.fetch_series(
            route="/v2/petroleum/stoc/wstk/",
            facets={},
            frequency="weekly",
            since=date(2026, 1, 1),
        )
        assert results == []


# ---------------------------------------------------------------------------
# Behavior 3: EIAClient.verify_connectivity
# ---------------------------------------------------------------------------


class TestEIAClientVerifyConnectivity:
    def test_returns_true_on_200(self) -> None:
        from alphamind.data_sources.eia.client import EIAClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"response": {"data": []}})

        client = EIAClient(api_key="test_key", _http_client=_http_client(handler))
        assert client.verify_connectivity() is True

    def test_raises_on_auth_failure(self) -> None:
        from alphamind.data_sources.eia.client import EIAClient

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        client = EIAClient(api_key="bad_key", _http_client=_http_client(handler))
        with pytest.raises(httpx.HTTPStatusError):
            client.verify_connectivity()


# ---------------------------------------------------------------------------
# Behavior 4: collect_series writes MacroObservations rows
# ---------------------------------------------------------------------------


class TestCollectSeriesWrites:
    def test_collect_series_writes_full_row(self, db_factory: sessionmaker[Session]) -> None:
        """collect_series() writes rows with all columns correctly populated."""
        from alphamind.data_sources.eia.energy import collect_series
        from alphamind.data_sources.eia.series import SERIES

        crude_series = [s for s in SERIES if s["series_id"] == "eia.crude_inventory_total"]
        repo = FakeRunRepo()
        client = FakeEIAAPI(
            data=[
                {"period": "2026-04-18", "value": 442.1, "units": "MBBL"},
                {"period": "2026-04-11", "value": 438.5, "units": "MBBL"},
            ]
        )

        collect_series(
            crude_series,
            since=date(2026, 1, 1),
            _repo=repo,
            _client=client,
            _session_factory=db_factory,
        )

        with db_factory() as sess:
            rows = sess.query(MacroObservations).order_by(MacroObservations.observation_date).all()

        assert len(rows) == 2
        row = rows[1]  # 2026-04-18
        assert row.source == "eia"
        assert row.series_id == "eia.crude_inventory_total"
        assert row.frequency == "weekly"
        assert row.units == "bbl"
        assert row.revision_number == 0
        assert row.value == 442.1
        run = repo.latest()
        assert run["status"] == "success"
        assert run["rows_written"] == 2

    def test_dollars_per_barrel_units_mapped(self, db_factory: sessionmaker[Session]) -> None:
        from alphamind.data_sources.eia.energy import collect_series
        from alphamind.data_sources.eia.series import SERIES

        wti_series = [s for s in SERIES if s["series_id"] == "eia.wti_spot_price"]
        client = FakeEIAAPI(
            data=[{"period": "2026-04-18", "value": 82.50, "units": "DOLLARS PER BARREL"}]
        )

        collect_series(
            wti_series,
            since=date(2026, 1, 1),
            _repo=FakeRunRepo(),
            _client=client,
            _session_factory=db_factory,
        )

        with db_factory() as sess:
            row = sess.query(MacroObservations).first()

        assert row is not None
        assert row.units == "usd"


# ---------------------------------------------------------------------------
# Behavior 5: UPSERT idempotency
# ---------------------------------------------------------------------------


class TestCollectSeriesIdempotency:
    def test_rerun_produces_no_duplicates(self, db_factory: sessionmaker[Session]) -> None:
        from alphamind.data_sources.eia.energy import collect_series
        from alphamind.data_sources.eia.series import SERIES

        client = FakeEIAAPI(
            data=[
                {"period": "2026-04-18", "value": 442.1, "units": "MBBL"},
                {"period": "2026-04-11", "value": 438.5, "units": "MBBL"},
            ]
        )

        kwargs: dict[str, Any] = dict(
            since=date(2026, 1, 1),
            _repo=FakeRunRepo(),
            _client=client,
            _session_factory=db_factory,
        )

        collect_series(SERIES[:1], **kwargs)

        # Re-run with fresh repo (same DB)
        kwargs["_repo"] = FakeRunRepo()
        collect_series(SERIES[:1], **kwargs)

        with db_factory() as sess:
            count = sess.query(MacroObservations).count()

        assert count == 2  # still 2, not 4


# ---------------------------------------------------------------------------
# Behavior 6: On failure, collection_runs records 'failed'; no data rows
# ---------------------------------------------------------------------------


class TestCollectSeriesFailure:
    def test_http_failure_records_failed_status(self, db_factory: sessionmaker[Session]) -> None:
        from alphamind.data_sources.eia.energy import collect_series
        from alphamind.data_sources.eia.series import SERIES

        request = httpx.Request("GET", "https://api.eia.gov/v2/test/")
        client = FakeEIAAPI(
            error=httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=request,
                response=httpx.Response(500, request=request),
            )
        )
        repo = FakeRunRepo()

        with pytest.raises(httpx.HTTPStatusError):
            collect_series(
                SERIES[:1],
                since=date(2026, 1, 1),
                _repo=repo,
                _client=client,
                _session_factory=db_factory,
            )

        assert repo.failed()

    def test_http_failure_leaves_no_data_rows(self, db_factory: sessionmaker[Session]) -> None:
        from alphamind.data_sources.eia.energy import collect_series
        from alphamind.data_sources.eia.series import SERIES

        request = httpx.Request("GET", "https://api.eia.gov/v2/test/")
        client = FakeEIAAPI(
            error=httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=request,
                response=httpx.Response(500, request=request),
            )
        )

        with pytest.raises(httpx.HTTPStatusError):
            collect_series(
                SERIES[:1],
                since=date(2026, 1, 1),
                _repo=FakeRunRepo(),
                _client=client,
                _session_factory=db_factory,
            )

        with db_factory() as sess:
            count = sess.query(MacroObservations).count()

        assert count == 0


# ---------------------------------------------------------------------------
# Behavior 7: bootstrap_series covers 252 days
# ---------------------------------------------------------------------------


class TestCollectSeriesNoArgs:
    """collect_series is callable with no positional args (cron registry contract)."""

    def test_callable_with_no_args(self, db_factory: sessionmaker[Session]) -> None:
        client = FakeEIAAPI()
        repo = FakeRunRepo()

        from alphamind.data_sources.eia.energy import collect_series

        # No series, no since — must not raise
        collect_series(
            _repo=repo,
            _client=client,
            _session_factory=db_factory,
        )


class TestBootstrapSeries:
    def test_bootstrap_passes_252_day_since(self) -> None:
        from alphamind.data_sources.eia import energy

        calls: list[Any] = []

        def fake_collect(series: Any, since: date, **kwargs: Any) -> None:
            calls.append(since)

        engine = make_engine(":memory:")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)

        with (
            patch.object(energy, "collect_series", side_effect=fake_collect),
            patch.object(
                energy,
                "_make_default_client",
                return_value=FakeEIAAPI(),
            ),
        ):
            energy.bootstrap_series(
                _repo=FakeRunRepo(),
                _session_factory=factory,
            )

        assert len(calls) == 1
        since_date = calls[0]
        today = datetime.now(UTC).date()
        expected = today - timedelta(days=252)
        # Allow ±1 day for clock edge cases
        assert abs((since_date - expected).days) <= 1

    def test_bootstrap_collects_all_series(self) -> None:
        from alphamind.data_sources.eia import energy
        from alphamind.data_sources.eia.series import SERIES

        captured_series: list[Any] = []

        def fake_collect(series: list[Any], since: date, **kwargs: Any) -> None:
            captured_series.extend(series)

        engine = make_engine(":memory:")
        Base.metadata.create_all(engine)
        factory = make_session_factory(engine)

        with (
            patch.object(energy, "collect_series", side_effect=fake_collect),
            patch.object(
                energy,
                "_make_default_client",
                return_value=FakeEIAAPI(),
            ),
        ):
            energy.bootstrap_series(
                _repo=FakeRunRepo(),
                _session_factory=factory,
            )

        assert len(captured_series) == len(SERIES)

"""
HTTP client for the EIA v2 API.

Uses ``httpx`` directly — no official SDK exists for EIA.
Integrates ``with_retries(critical)``, ``RateLimiter``, and exposes
``verify_connectivity()`` for smoke-testing the API key.

EIA v2 endpoint shape::

    GET /v2/{route}/data/
        ?api_key={k}
        &frequency=weekly
        &data[0]=value
        &facets[product][]=EPC0
        &sort[0][column]=period
        &sort[0][direction]=desc
        &start={since}
        &length={length}
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from alphamind.data_sources._common import RateLimiter, RetryShape, with_retries

_BASE_URL = "https://api.eia.gov"
_PROVIDER = "eia"
_DEFAULT_LENGTH = 5000

# Shared rate-limiter instance — 80 req/min per story spec (conservative vs
# documented EIA limit of 5000 req/hour ≈ 83/min).
_rate_limiter = RateLimiter()
_rate_limiter.set_limit(_PROVIDER, rate_per_minute=80)


class EIAClient:
    """
    Thin wrapper around the EIA v2 REST API.

    Parameters
    ----------
    api_key:
        EIA API key.
    _http_client:
        Optional injectable ``httpx``-compatible client (for testing).
        When ``None``, a real ``httpx.Client`` is created.
    """

    def __init__(
        self,
        api_key: str,
        *,
        _http_client: Any = None,
    ) -> None:
        self._api_key = api_key
        self._http = _http_client if _http_client is not None else httpx.Client(timeout=30.0)

    @with_retries(RetryShape.critical)
    def fetch_series(
        self,
        route: str,
        facets: dict[str, list[str]],
        frequency: str,
        since: date,
        length: int = _DEFAULT_LENGTH,
    ) -> list[dict[str, Any]]:
        """
        Fetch data points from one EIA series.

        Parameters
        ----------
        route:
            EIA v2 route, e.g. ``"/v2/petroleum/stoc/wstk/"``.
        facets:
            Facet filter mapping, e.g. ``{"product": ["EPC0"]}``.
        frequency:
            EIA frequency string: ``"weekly"``, ``"daily"``, etc.
        since:
            Earliest observation date (inclusive).
        length:
            Maximum rows to retrieve per request.

        Returns
        -------
        list[dict[str, Any]]
            Raw data point dicts from the EIA ``response.data`` array.
        """
        _rate_limiter.acquire(_PROVIDER)

        url = f"{_BASE_URL}{route}data/"
        params: dict[str, Any] = {
            "api_key": self._api_key,
            "frequency": frequency,
            "data[0]": "value",
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "start": since.isoformat(),
            "length": length,
        }
        for facet_name, values in facets.items():
            for value in values:
                params[f"facets[{facet_name}][]"] = value

        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        result: list[dict[str, Any]] = body.get("response", {}).get("data", [])
        return result

    def verify_connectivity(self) -> bool:
        """
        Verify the API key is valid by fetching one row from a known endpoint.

        Returns ``True`` on success; raises ``httpx.HTTPStatusError`` on
        authentication or server failure.
        """
        _rate_limiter.acquire(_PROVIDER)

        url = f"{_BASE_URL}/v2/petroleum/stoc/wstk/data/"
        params: dict[str, Any] = {
            "api_key": self._api_key,
            "frequency": "weekly",
            "data[0]": "value",
            "length": 1,
        }
        resp = self._http.get(url, params=params)
        resp.raise_for_status()
        return True


# Runtime contract: EIAClient must structurally implement EIAAPI.
from alphamind.data_sources.eia._protocol import EIAAPI  # noqa: E402

_: EIAAPI = EIAClient(api_key="<unused-for-typecheck>")

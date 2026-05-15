"""Protocol declaration for the Polygon SDK surface used by AlphaMind.

The production wrapper :class:`PolygonClient` already returns simple
attribute-bearing record objects from the underlying ``polygon-api-client``
SDK.  Tests substitute :class:`FakePolygonAPI` from
``tests/data_sources/_fakes/polygon.py`` which returns plain dataclass
records with the same attribute names.
"""

from __future__ import annotations

from typing import Protocol


class PolygonAPI(Protocol):
    """Methods AlphaMind invokes on the Polygon SDK."""

    def verify_connectivity(self) -> bool: ...
    def acquire_rate_limit(self) -> None: ...
    def get_aggs(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        from_: str,
        to: str,
        adjusted: bool = True,
        sort: str = "asc",
        limit: int = 50_000,
    ) -> list[object]: ...
    def list_snapshot_options_chain(self, underlying: str) -> list[object]: ...
    def list_dividends(
        self, ticker: str, ex_dividend_date_gte: str | None = None
    ) -> list[object]: ...
    def list_splits(self, ticker: str, execution_date_gte: str | None = None) -> list[object]: ...
    def get_ticker_details(self, ticker: str) -> object: ...

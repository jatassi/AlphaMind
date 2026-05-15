"""In-memory fake for :class:`BLSAPI`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeBLSAPI:
    """Stateful fake implementing :class:`BLSAPI`."""

    responses: list[list[dict[str, Any]]] = field(default_factory=list)
    """Successive ``post_timeseries`` calls consume entries in order."""

    handler: Callable[..., list[dict[str, Any]]] | None = None
    connectivity: bool = True
    error: Exception | None = None

    calls: list[dict[str, Any]] = field(default_factory=list)
    _index: int = 0

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def post_timeseries(
        self,
        series_ids: list[str],
        *,
        start_year: str,
        end_year: str,
    ) -> list[dict[str, Any]]:
        call = {
            "series_ids": series_ids,
            "start_year": start_year,
            "end_year": end_year,
        }
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if self.handler is not None:
            return list(self.handler(**call))
        if not self.responses:
            return []
        idx = min(self._index, len(self.responses) - 1)
        self._index += 1
        return list(self.responses[idx])


def make_bls_series(series_id: str, observations: list[tuple[str, str, str]]) -> dict[str, Any]:
    """Build a BLS series response dict.

    Each observation is (year, period, value).
    """
    return {
        "seriesID": series_id,
        "data": [
            {"year": y, "period": p, "value": v, "footnotes": [{}]} for y, p, v in observations
        ],
    }

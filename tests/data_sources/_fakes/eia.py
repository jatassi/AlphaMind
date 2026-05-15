"""In-memory fake for :class:`EIAAPI`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class FakeEIAAPI:
    """Stateful fake implementing :class:`EIAAPI`."""

    data: list[dict[str, Any]] = field(default_factory=list)
    """Default rows returned from ``fetch_series``."""

    data_handler: Callable[..., list[dict[str, Any]]] | None = None
    connectivity: bool = True
    error: Exception | None = None

    # Call-tracking
    calls: list[dict[str, Any]] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def fetch_series(
        self,
        route: str,
        facets: dict[str, list[str]],
        frequency: str,
        since: date,
        length: int = 5000,
    ) -> list[dict[str, Any]]:
        call = {
            "route": route,
            "facets": facets,
            "frequency": frequency,
            "since": since,
            "length": length,
        }
        self.calls.append(call)
        if self.error is not None:
            raise self.error
        if self.data_handler is not None:
            return list(self.data_handler(**call))
        return list(self.data)

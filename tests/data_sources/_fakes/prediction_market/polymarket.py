"""In-memory fake for :class:`PolymarketAPI`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakePolymarketAPI:
    """Stateful fake implementing :class:`PolymarketAPI`."""

    markets: list[dict[str, Any]] = field(default_factory=list)
    events: dict[str, dict[str, Any]] = field(default_factory=dict)

    connectivity: bool = True
    markets_error: Exception | None = None
    events_error: Exception | None = None

    get_markets_calls: list[dict[str, Any]] = field(default_factory=list)
    get_events_calls: list[list[str]] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def get_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]:
        self.get_markets_calls.append({"limit": limit, "offset": offset, "closed": closed})
        if self.markets_error is not None:
            raise self.markets_error
        return list(self.markets)

    def get_events_by_ids(self, event_ids: list[str]) -> list[dict[str, Any]]:
        self.get_events_calls.append(list(event_ids))
        if self.events_error is not None:
            raise self.events_error
        return [self.events[eid] for eid in event_ids if eid in self.events]

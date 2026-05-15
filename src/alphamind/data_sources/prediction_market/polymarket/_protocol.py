"""Protocol declaration for the Polymarket HTTP wrapper."""

from __future__ import annotations

from typing import Any, Protocol


class PolymarketAPI(Protocol):
    """Methods AlphaMind invokes on the Polymarket client."""

    def verify_connectivity(self) -> bool: ...
    def get_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]: ...
    def get_events_by_ids(self, event_ids: list[str]) -> list[dict[str, Any]]: ...

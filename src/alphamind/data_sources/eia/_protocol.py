"""Protocol declaration for the EIA HTTP wrapper."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol


class EIAAPI(Protocol):
    """Methods AlphaMind invokes on the EIA HTTP wrapper."""

    def verify_connectivity(self) -> bool: ...
    def fetch_series(
        self,
        route: str,
        facets: dict[str, list[str]],
        frequency: str,
        since: date,
        length: int = 5000,
    ) -> list[dict[str, Any]]: ...

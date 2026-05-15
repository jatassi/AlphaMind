"""Protocol declaration for the BLS HTTP wrapper."""

from __future__ import annotations

from typing import Any, Protocol


class BLSAPI(Protocol):
    """Methods AlphaMind invokes on the BLS HTTP wrapper."""

    def verify_connectivity(self) -> bool: ...
    def post_timeseries(
        self,
        series_ids: list[str],
        *,
        start_year: str,
        end_year: str,
    ) -> list[dict[str, Any]]: ...

"""Protocol declaration for the FRED SDK surface used by AlphaMind."""

from __future__ import annotations

from typing import Any, Protocol


class FredAPI(Protocol):
    """Methods AlphaMind invokes on the fredapi SDK."""

    def verify_connectivity(self) -> bool: ...
    def get_series(self, series_id: str, **kwargs: Any) -> Any: ...
    def get_series_info(self, series_id: str) -> Any: ...
    def get_series_all_releases(self, series_id: str, **kwargs: Any) -> Any: ...

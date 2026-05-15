"""Protocol declaration for the Kalshi HTTP wrapper."""

from __future__ import annotations

from typing import Any, Protocol


class KalshiAPI(Protocol):
    """Methods AlphaMind invokes on the Kalshi client."""

    def verify_connectivity(self) -> bool: ...
    def get(self, path: str, **params: Any) -> dict[str, Any]: ...

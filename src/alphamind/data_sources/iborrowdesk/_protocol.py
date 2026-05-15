"""Protocol declaration for the iBorrowDesk HTTP wrapper."""

from __future__ import annotations

from typing import Any, Protocol


class IBorrowDeskAPI(Protocol):
    """Methods AlphaMind invokes on the iBorrowDesk client."""

    def verify_connectivity(self) -> bool: ...
    def fetch_ticker(self, ticker: str) -> dict[str, Any]: ...

"""Protocol declaration for the FINRA CDN client surface."""

from __future__ import annotations

from typing import Protocol


class FinraAPI(Protocol):
    """Methods AlphaMind invokes on the FINRA HTTP wrapper."""

    def verify_connectivity(self) -> bool: ...
    def get(self, path: str) -> str: ...

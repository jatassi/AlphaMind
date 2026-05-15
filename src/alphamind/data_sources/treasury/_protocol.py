"""Protocol declaration for the Treasury Fiscal Data API surface.

`TreasuryAPI` names only the methods AlphaMind calls — not the full
vendor surface. The production wrapper :class:`TreasuryClient` implements
this Protocol structurally; tests use :class:`FakeTreasuryAPI` from
``tests/data_sources/_fakes/treasury.py`` to inject deterministic data.
"""

from __future__ import annotations

from typing import Any, Protocol


class TreasuryAPI(Protocol):
    """Minimal Treasury Fiscal Data API surface used by AlphaMind."""

    def verify_connectivity(self) -> bool: ...
    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]: ...

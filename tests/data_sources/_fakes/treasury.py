"""In-memory fake for :class:`TreasuryAPI`.

Construction-time keyword arguments hold the data the fake returns from
each method call:

    api = FakeTreasuryAPI(
        responses={"/auctions": page1},
        connectivity=True,
    )
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeTreasuryAPI:
    """Stateful fake implementing :class:`TreasuryAPI`."""

    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Map of path → response body. Missing paths raise ``KeyError`` so
    tests can detect unexpected calls."""

    connectivity: bool = True

    get_handler: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None
    """Optional handler that overrides :attr:`responses`. Use for tests
    that need stateful or side-effecting behavior (e.g. pagination)."""

    calls: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((path, params))
        if self.get_handler is not None:
            return self.get_handler(path, params)
        return self.responses[path]

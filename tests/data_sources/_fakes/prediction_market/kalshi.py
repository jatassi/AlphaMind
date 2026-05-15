"""In-memory fake for :class:`KalshiAPI`."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeKalshiAPI:
    """Stateful fake implementing :class:`KalshiAPI`."""

    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Map of ``path`` → response body for the simple constant case."""

    get_handler: Callable[..., dict[str, Any]] | None = None
    """Override for dispatch-on-params behavior."""

    connectivity: bool = True
    error: Exception | None = None

    calls: list[dict[str, Any]] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        self.calls.append({"path": path, **params})
        if self.error is not None:
            raise self.error
        if self.get_handler is not None:
            return self.get_handler(path, **params)
        return self.responses.get(path, {})

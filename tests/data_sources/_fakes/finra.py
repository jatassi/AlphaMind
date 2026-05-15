"""In-memory fake for :class:`FinraAPI`.

Tests construct a FakeFinraAPI with ``responses`` mapping path → body
string (or ``Exception`` to raise).  Missing paths raise
``httpx.HTTPStatusError(404)`` to mirror the production behavior the FINRA
collectors rely on for "file not yet published" semantics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


@dataclass
class FakeFinraAPI:
    """Stateful fake implementing :class:`FinraAPI`."""

    responses: dict[str, Any] = field(default_factory=dict)
    """Map of path → body string or Exception."""

    connectivity: bool = True

    calls: list[str] = field(default_factory=list)

    def verify_connectivity(self) -> bool:
        return self.connectivity

    def get(self, path: str) -> str:
        self.calls.append(path)
        val = self.responses.get(path)
        if val is None:
            req = httpx.Request("GET", f"https://cdn.finra.org{path}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)
        if isinstance(val, Exception):
            raise val
        assert isinstance(val, str)
        return val

"""Clock primitive — story 10a (ALP-474).

Replaces direct ``datetime.now(UTC)`` calls in analysis tools with a
:class:`Clock` Protocol so replay tests can supply a deterministic timestamp
without monkey-patching the ``datetime`` module.

The shape is intentionally tiny: a single ``now() -> datetime`` method
returning a tz-aware UTC instant. Tools that need the current moment take a
:class:`Clock` via constructor injection (factory closure); the production
composition root supplies :class:`RealClock`; tests supply a fake.

This lives in ``_kernel`` because the analysis tools are not the only
subdivision that may eventually need a clock seam; future stories can
re-use the same Protocol.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

__all__ = ["Clock", "RealClock"]


class Clock(Protocol):
    """Source of the current wall-clock instant.

    Implementations must return a timezone-aware UTC :class:`datetime`. The
    Protocol is structural — callers depend on shape, not inheritance.
    """

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...


class RealClock:
    """Production implementation — wraps :func:`datetime.now`.

    The single source of wall-clock time used by analysis-tool factories
    when the composition root is the live pipeline.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

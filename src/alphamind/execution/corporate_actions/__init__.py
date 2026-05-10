"""Corporate-actions integration package (ALP-409).

Imports are lazy to avoid a circular-import cycle between this package and
``state_persistence.config`` (which imports ``corporate_actions.config`` during
its own load).

Public API:

* :func:`integrate_ca_activity` — dispatch one Alpaca CA activity through the
  per-type handler table.
* :class:`CorporateActionActivity` — typed input model for a single CA activity.
* :class:`AlpacaPositionLookup` — Protocol for live Alpaca position reads.
* :class:`CorporateActionsConfig` — configuration model (lookback window, etc.).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import CorporateActionsConfig
    from .dispatch import integrate_ca_activity
    from .types import AlpacaPositionLookup, CorporateActionActivity

__all__ = [
    "AlpacaPositionLookup",
    "CorporateActionActivity",
    "CorporateActionsConfig",
    "integrate_ca_activity",
]


def __getattr__(name: str) -> object:
    """Lazy-load public names to avoid circular imports at package init time."""
    if name == "integrate_ca_activity":
        from .dispatch import integrate_ca_activity

        return integrate_ca_activity
    if name == "CorporateActionActivity":
        from .types import CorporateActionActivity

        return CorporateActionActivity
    if name == "AlpacaPositionLookup":
        from .types import AlpacaPositionLookup

        return AlpacaPositionLookup
    if name == "CorporateActionsConfig":
        from .config import CorporateActionsConfig

        return CorporateActionsConfig
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)

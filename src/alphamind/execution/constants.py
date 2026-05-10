"""Listed-options execution-layer constants (ALP-401).

Constants here are structurally fixed by the listed-options market itself —
not configurable, not venue-specific. They live alongside (but distinct from)
``alphamind.execution.venue_configuration.constants`` which carries
Alpaca-venue regulatory constants (settlement, PDT, fees).

Reference: https://linear.app/alphamind-jatassi/issue/ALP-401
"""

from __future__ import annotations

from typing import Final

LISTED_OPTION_CONTRACT_MULTIPLIER: Final[float] = 100.0
"""Standard listed-options contract multiplier — one contract represents 100
shares of the underlying.

This is the OCC / CBOE convention for U.S. listed equity options; AlphaMind
trades only this instrument class (no mini-options, no futures), so this
multiplier is structurally fixed at 100.0 across all options call sites.

Promoted to a shared constant per ALP-401 to remove the ~30 inline ``100.0``
literals scattered through ``OptionsPositionDetails`` /
``OptionsInstrumentSpec`` constructors and helper-function defaults. If
AlphaMind ever needs to support a non-100 multiplier (mini-contracts, etc.),
this is the single place that changes.
"""

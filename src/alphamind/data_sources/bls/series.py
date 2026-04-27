"""
Configurable BLS series registry.

Each key is a BLS series ID; value carries the metadata needed for
``macro_observations`` rows that the BLS API does not return.
"""

from __future__ import annotations

# Keys match BLS series ID format.  Frequency is hardcoded because the BLS v2
# API response does not include it.  Units follow the macro_observations
# convention used throughout the data layer.
SERIES: dict[str, dict[str, str]] = {
    # Employment
    "CES0000000001": {"frequency": "monthly", "units": "thousands"},
    "LNS14000000": {"frequency": "monthly", "units": "pct"},
    # CPI-U
    "CUSR0000SA0": {"frequency": "monthly", "units": "index"},
    "CUSR0000SA0L1E": {"frequency": "monthly", "units": "index"},  # core CPI-U (ex food & energy)
    "CUSR0000SAF1": {"frequency": "monthly", "units": "index"},  # food
    "CUSR0000SACE": {"frequency": "monthly", "units": "index"},  # energy commodities
}

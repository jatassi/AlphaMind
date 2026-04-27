"""
EIA series registry for the AlphaMind energy adapter.

Convention for synthesized series_id values:
    ``eia.<short_name>``

Each entry is a dict with keys:
    series_id  — synthesized stable ID written to macro_observations.series_id
    route      — EIA v2 API path segment, e.g. "/v2/petroleum/stoc/wstk/"
    facets     — dict of facet name → list of values; empty dict means no facets
    frequency  — EIA frequency string: "weekly" | "daily" | "monthly"

These four series cover the EIA categories required by story 05c:
    Q8a  crude oil inventory / production
    Q8b  natural gas storage
    Q8   WTI spot price
    Q8e  refinery utilization
"""

from __future__ import annotations

from typing import Any

# Each element selects one unique EIA timeseries via a (route, facets) tuple.
SERIES: list[dict[str, Any]] = [
    {
        "series_id": "eia.crude_inventory_total",
        "route": "/v2/petroleum/stoc/wstk/",
        "facets": {"product": ["EPC0"]},
        "frequency": "weekly",
    },
    {
        "series_id": "eia.nat_gas_storage_lower_48",
        "route": "/v2/natural-gas/stor/wkly/",
        "facets": {},
        "frequency": "weekly",
    },
    {
        "series_id": "eia.wti_spot_price",
        "route": "/v2/petroleum/pri/spt/",
        "facets": {"series": ["RWTC"]},
        "frequency": "daily",
    },
    {
        "series_id": "eia.refinery_utilization",
        "route": "/v2/petroleum/pnp/wiup/",
        "facets": {},
        "frequency": "weekly",
    },
]

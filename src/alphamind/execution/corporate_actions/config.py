"""Configuration for the corporate-actions integration package (ALP-409).

``CorporateActionsConfig`` exposes the fetcher lookback window consumed by the
activity-fetcher story (ALP-410 / story 02).  No other story in this work tree
reads the config beyond the lookback field.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CorporateActionsConfig(BaseModel):
    """Operator-tunable knobs for the corporate-actions integration layer."""

    model_config = ConfigDict(frozen=True)

    fetcher_lookback_days: int = 7
    """Number of calendar days to look back when fetching Alpaca CA activities.

    Story 02 passes this to the Alpaca ``GET /v2/account/activities`` call.
    Defaults to 7, matching the design-doc recommendation.
    """


__all__ = ["CorporateActionsConfig"]

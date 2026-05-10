"""Configuration for the corporate-actions integration package (ALP-409).

``CorporateActionsConfig`` exposes the fetcher lookback window consumed by the
activity-fetcher story (ALP-410 / story 02).  No other story in this work tree
reads the config beyond the lookback field.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class CorporateActionsConfig(BaseModel):
    """Operator-tunable knobs for the corporate-actions integration layer."""

    model_config = ConfigDict(frozen=True)

    fetcher_lookback_days: Annotated[int, Field(ge=1)] = 7
    """Number of calendar days to look back when fetching Alpaca CA activities.

    Story 02 passes this to the Alpaca ``GET /v1/corporate-actions`` (v1beta1
    Corporate Actions Market Data API) ``start`` parameter.  Defaults to 7,
    matching the design-doc recommendation.
    """


__all__ = ["CorporateActionsConfig"]

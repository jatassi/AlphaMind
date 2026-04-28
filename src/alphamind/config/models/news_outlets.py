"""Pydantic models for news_outlets.yaml."""

from enum import StrEnum

from pydantic import BaseModel


class CredibilityTier(StrEnum):
    tier_1 = "tier_1"
    tier_2 = "tier_2"
    tier_3 = "tier_3"


class OutletEntry(BaseModel):
    tier: CredibilityTier


class NewsOutletsConfig(BaseModel):
    outlets: dict[str, OutletEntry]

"""Pydantic models for assets.yaml — authoritative ticker universe (story 03a)."""

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, field_validator

_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.]*$")


def _validate_ticker(ticker: str) -> str:
    if not _TICKER_RE.match(ticker):
        msg = (
            f"Invalid ticker {ticker!r}: must match {_TICKER_RE.pattern} "
            f"(uppercase start, then uppercase/digit/dot)"
        )
        raise ValueError(msg)
    return ticker


class AssetRole(StrEnum):
    broad_market = "broad_market"
    breadth = "breadth"
    sector_etf = "sector_etf"
    intermarket = "intermarket"


class DiscoveryVendor(StrEnum):
    spdr = "spdr"
    ishares = "ishares"


class DiscoverySource(BaseModel):
    model_config = ConfigDict(frozen=True)

    etf: str
    vendor: DiscoveryVendor
    ishares_product_id: str | None = None


class Benchmark(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: AssetRole
    description: str


class AssetsConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    last_full_validation: date | None = None
    discovery_sources: dict[str, DiscoverySource]
    sectors: dict[str, list[str]]
    benchmarks: dict[str, Benchmark]

    @field_validator("sectors")
    @classmethod
    def sector_tickers_are_well_formed(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for tickers in v.values():
            for ticker in tickers:
                _validate_ticker(ticker)
        return v

    @field_validator("benchmarks")
    @classmethod
    def benchmark_keys_are_well_formed(cls, v: dict[str, Benchmark]) -> dict[str, Benchmark]:
        for key in v:
            _validate_ticker(key)
        return v

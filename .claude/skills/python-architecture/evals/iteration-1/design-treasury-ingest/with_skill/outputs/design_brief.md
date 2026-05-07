# Design — Treasury auction ingest module

**Date:** 2026-05-06
**Shape:** Data pipeline (episodic, single daily run)
**Status:** Ready to implement

---

## 1. What we're building

The Treasury ingest module pulls auction results from the Fiscal Data API daily, normalises the untrusted JSON into typed records, persists them to the SQLite database, and emits a structured event the distillation layer consumes to recalibrate decision models.

The trust boundary is the API response: untrusted JSON enters as a Pydantic model at the adapter layer, is converted once to a frozen dataclass, and flows through pure domain logic as fully typed values. The module runs once per day (24h cadence) via the pipeline orchestrator. Persistence is durable (SQLite); the module is idempotent across daily reruns due to upsert-style deduplication on the `(auction_date, tenor)` composite key.

## 2. Principles guiding this design

- **P1 (functional core, imperative shell):** The core logic (tenor mapping, yield calculation, participant-fraction derivation) is pure; all I/O (HTTP, DB, time, event publication) lives at the boundary where it's testable with a fake clock and fake repository.
- **P3 (illegal states unrepresentable):** Domain types encode constraints directly—`Tenor` as a closed enum, `AuctionYieldBp` as a newtype over `Decimal` to prevent float rounding errors in financial data, `AuctionDate` as a validated ISO 8601 string. Untrusted JSON is parsed once at the boundary into these types; downstream code trusts the type.
- **P5 (Pydantic at boundary, frozen dataclass inside):** Pydantic `BaseModel` validates and coerces the Fiscal Data API JSON; conversion functions produce frozen dataclasses for the domain. Internal code does not import Pydantic.

## 3. Package layout

```
src/alphamind/data_sources/treasury/
├── __init__.py        # empty
├── domain.py          # pure functions, frozen dataclasses, domain primitives
├── adapters.py        # HTTP client, DB repository, event publisher
├── api.py             # entrypoint: collect_auctions, bootstrap_auctions
└── exceptions.py      # domain exceptions (TreasuryIngestError, etc.)
```

The import discipline is enforced via `import-linter`:

```toml
[[importlinter.contracts]]
name = "Treasury domain doesn't import I/O"
type = "forbidden"
source_modules = ["alphamind.data_sources.treasury.domain"]
forbidden_modules = ["httpx", "sqlalchemy", "alphamind.data_sources.treasury.adapters"]
```

The existing `client.py` (HTTP wrapper) and `auctions.py` (current ingest entrypoint) will be refactored into this layout. The new `domain.py` will hold the pure logic; `adapters.py` will own the DB repository and event publisher; `api.py` will be the orchestrator entrypoint.

## 4. Types

### Identifiers and closed sets

```python
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import NewType
from dataclasses import dataclass

# Closed set of valid tenors
class Tenor(StrEnum):
    TWO_YEAR = "2Y"
    FIVE_YEAR = "5Y"
    TEN_YEAR = "10Y"
    THIRTY_YEAR = "30Y"

# Domain primitives — newtype wrappers prevent accidental unit confusion
AuctionYieldBp = NewType("AuctionYieldBp", Decimal)  # basis points
TailBp = NewType("TailBp", Decimal)  # tail (high yield - median yield), basis points
AuctionSizeUsd = NewType("AuctionSizeUsd", Decimal)  # in billions USD
BidToCover = NewType("BidToCover", Decimal)  # ratio, dimensionless
ParticipantPct = NewType("ParticipantPct", Decimal)  # 0–100, percentage

# Internal domain record
@dataclass(frozen=True, slots=True)
class TreasuryAuction:
    """Single auction result record."""
    auction_id: str  # composite key: f"{auction_date}_{tenor}"
    tenor: Tenor
    auction_date: str  # ISO 8601 date string, validated at boundary
    auction_yield_bp: AuctionYieldBp | None
    bid_to_cover: BidToCover | None
    tail_bp: TailBp | None
    primary_dealer_pct: ParticipantPct | None
    indirect_pct: ParticipantPct | None
    direct_pct: ParticipantPct | None
    auction_size_usd: AuctionSizeUsd | None
```

### Boundary types

The Fiscal Data API response is parsed via Pydantic; a conversion function produces the domain type:

```python
from pydantic import BaseModel, Field, field_validator

# Inbound boundary type — Pydantic for validation only
class FiscalDataAuctionRecord(BaseModel):
    """One auction record from the Fiscal Data API /auctions_query endpoint."""
    record_date: str  # ISO 8601 date string
    security_term: str  # e.g., "2-Year", "5-Year", etc.
    high_yield: str | float | None
    avg_med_yield: str | float | None
    bid_to_cover_ratio: str | float | None
    primary_dealer_accepted: str | float | None
    indirect_bidder_accepted: str | float | None
    direct_bidder_accepted: str | float | None
    total_accepted: str | float | None

    @field_validator("record_date")
    @classmethod
    def validate_date_format(cls, v: str) -> str:
        """Ensure record_date is ISO 8601 date."""
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError(f"Invalid ISO 8601 date: {v}")
        return v

    @field_validator("security_term")
    @classmethod
    def validate_tenor(cls, v: str) -> str:
        """Ensure security_term is a known tenor."""
        tenor_map = {"2-Year": "2Y", "5-Year": "5Y", "10-Year": "10Y", "30-Year": "30Y"}
        if v not in tenor_map:
            raise ValueError(f"Unknown tenor: {v}")
        return v

    def to_domain(self) -> TreasuryAuction | None:
        """Convert to domain type, or None if tenor unknown."""
        tenor_map = {
            "2-Year": Tenor.TWO_YEAR,
            "5-Year": Tenor.FIVE_YEAR,
            "10-Year": Tenor.TEN_YEAR,
            "30-Year": Tenor.THIRTY_YEAR,
        }
        tenor = tenor_map.get(self.security_term)
        if tenor is None:
            return None

        # Parse floats, handling API nulls and string representations
        high_yield = _parse_float(self.high_yield)
        avg_med_yield = _parse_float(self.avg_med_yield)
        total_accepted = _parse_float(self.total_accepted)

        # Compute derived fields
        auction_yield_bp = _to_basis_points(high_yield)
        tail_bp = _compute_tail(high_yield, avg_med_yield)
        auction_size_usd = _to_billions(total_accepted)
        
        primary_dealer_pct = _participant_fraction(
            _parse_float(self.primary_dealer_accepted), total_accepted
        )
        indirect_pct = _participant_fraction(
            _parse_float(self.indirect_bidder_accepted), total_accepted
        )
        direct_pct = _participant_fraction(
            _parse_float(self.direct_bidder_accepted), total_accepted
        )

        return TreasuryAuction(
            auction_id=f"{self.record_date}_{tenor.value}",
            tenor=tenor,
            auction_date=self.record_date,
            auction_yield_bp=auction_yield_bp,
            bid_to_cover=BidToCover(Decimal(str(self.bid_to_cover_ratio)))
            if self.bid_to_cover_ratio is not None else None,
            tail_bp=tail_bp,
            primary_dealer_pct=primary_dealer_pct,
            indirect_pct=indirect_pct,
            direct_pct=direct_pct,
            auction_size_usd=auction_size_usd,
        )

# Domain exception
class TreasuryAuctionParseError(Exception):
    """Raised when auction record cannot be converted to domain type."""
    pass

class TreasuryIngestError(Exception):
    """Raised on I/O failures (API, DB, event pub)."""
    pass
```

## 5. Testing seam

### HTTP boundary

```python
from typing import Protocol

class TreasuryApiClient(Protocol):
    """Abstraction over the Fiscal Data API HTTP layer."""
    
    def fetch_auctions(
        self,
        since: date,
        tenors: list[str],
    ) -> list[dict[str, Any]]:
        """Fetch paginated auction records from the API.
        
        Returns the raw JSON-deserialized list of records.
        Handles pagination, rate limiting, and retries internally.
        """
        ...

# In tests, fake the protocol with in-memory data:
class FakeTreasuryApiClient:
    def __init__(self, fixture_data: list[dict[str, Any]]) -> None:
        self._data = fixture_data

    def fetch_auctions(self, since: date, tenors: list[str]) -> list[dict[str, Any]]:
        """Return fixture data, filtering by date if needed."""
        return [
            r for r in self._data
            if date.fromisoformat(r["record_date"]) >= since
        ]
```

### Database boundary

```python
class TreasuryAuctionRepository(Protocol):
    """Abstraction over the treasury_auctions table."""
    
    def get(self, auction_id: str) -> TreasuryAuction | None:
        """Retrieve a single auction by ID."""
        ...
    
    def upsert(self, auction: TreasuryAuction) -> None:
        """Insert or replace an auction (by primary key)."""
        ...
    
    def list_since(self, since: date) -> list[TreasuryAuction]:
        """List all auctions on or after a date."""
        ...

# In tests, fake with in-memory dict:
class FakeTreasuryAuctionRepository:
    def __init__(self) -> None:
        self._store: dict[str, TreasuryAuction] = {}

    def get(self, auction_id: str) -> TreasuryAuction | None:
        return self._store.get(auction_id)

    def upsert(self, auction: TreasuryAuction) -> None:
        self._store[auction.auction_id] = auction

    def list_since(self, since: date) -> list[TreasuryAuction]:
        return [
            a for a in self._store.values()
            if date.fromisoformat(a.auction_date) >= since
        ]
```

### Event publisher boundary

```python
class DistillationEventPublisher(Protocol):
    """Abstraction over the event publication mechanism."""
    
    def publish(self, event: TreasuryAuctionIngestedEvent) -> None:
        """Publish a structured event to the distillation layer."""
        ...

# In tests, fake with a list accumulator:
class FakeDistillationEventPublisher:
    def __init__(self) -> None:
        self.published_events: list[TreasuryAuctionIngestedEvent] = []

    def publish(self, event: TreasuryAuctionIngestedEvent) -> None:
        self.published_events.append(event)
```

## 6. Hardest-to-reverse decisions

### 6.1 Emit structured events vs. direct DB write

**Picked.** Emit a `TreasuryAuctionIngestedEvent` event to a distillation-layer queue or topic.

**Alternative.** Write directly to `treasury_auctions` table; distillation polls the table on a timer.

**Why.** Events decouple ingest from downstream consumption. The distillation layer decides *when* to recalibrate models—not on every ingest, but on a schedule that balances freshness against compute cost. An event-driven architecture allows the distillation layer to subscribe to "auction data ready" and act within milliseconds. Direct polling incurs polling latency and wastes DB queries. P1 (functional core, imperative shell) favours the event boundary.

**Reconsider if.** The distillation layer genuinely doesn't care about timeliness and a daily batch query is sufficient. Then direct writes with no event are simpler.

### 6.2 Decimal vs. float for financial fields

**Picked.** `Decimal` for all yield, price, and percentage fields; `NewType` wrappers enforce semantic distinction (basis points vs. percentages vs. ratios).

**Alternative.** `float` with careful rounding; trust the type system via `AuctionYieldBp = NewType("AuctionYieldBp", float)`.

**Why.** Treasury yields are basis-point precision (0.01% increments). Float rounding introduces cumulative error that corrupts financial calculations. Decimal is exact. The cost is slightly heavier arithmetic, negligible at scale (daily ingestion). P3 (illegal states unrepresentable) — encoding the semantic unit in the type (basis points, not yield percentage) prevents unit-confusion bugs. The Decimal choice is non-negotiable for financial data.

**Reconsider if.** The distillation layer works exclusively with machine-learning models that require numpy arrays and float32 precision. Then convert to float at the distillation boundary, not in the ingest module.

### 6.3 Daily episodic run vs. continuous streaming

**Picked.** Episodic: the module runs once per day from the orchestrator; no background daemon.

**Alternative.** Continuous: spawn an async task at startup that polls the API hourly and publishes events asynchronously.

**Why.** Auction results are published once daily (Treasury announces auctions on a fixed schedule). A daily episodic job is aligned with the data cadence and eliminates polling waste. Simpler to reason about, easier to test (no background task lifecycle). P7 (async at the I/O boundary) doesn't justify async here because there is no concurrency benefit to gaining—one daily sequential API call is not concurrent, and wrapping it in async adds overhead. Sync at the entrypoint is clearer.

**Reconsider if.** The system needs to ingest intra-day auction updates or feed multiple downstream consumers that require sub-hourly freshness. Then async with structured concurrency (TaskGroup) and event streaming becomes justified.

### 6.4 Separate events for success vs. partial failure

**Picked.** Single `TreasuryAuctionIngestedEvent` event carrying `rows_ingested: int` and `parsing_errors: list[dict]`; if any auctions parse, the event is published; if all fail, raise `TreasuryIngestError`.

**Alternative.** Fail-fast: if any record fails to parse, abort the entire run and raise an exception.

**Alternative.** Partial events: emit separate success and failure events.

**Why.** Partial failures are real—a single malformed record should not block ingestion of ten others. The event carries the count and logs errors; distillation sees "8 auctions ingested" and can proceed. The pattern aligns with P1 (the core function returns a result; the shell decides what to do). A single event carrying outcome metrics is simpler than multiple event types and easier for distillation to subscribe to.

**Reconsider if.** The distillation layer needs to retry on any parsing failure, or if zero-tolerance error handling is required by compliance. Then fail-fast with `TreasuryIngestError` on the first malformed record.

---

## 7. Open decisions

- **Event persistence engine**: Currently undefined. Will events be persisted to a table, a message broker (RabbitMQ, Kafka), or fire-and-forget? Defer pending distillation layer design.
- **Retention policy on treasury_auctions**: How long should historical auction records be kept? Defer until lifecycle policy is finalised.
- **API pagination tuning**: Page size (currently 100) and retry delays are hardcoded. Move to config when the data sources configuration schema is stabilised.

---

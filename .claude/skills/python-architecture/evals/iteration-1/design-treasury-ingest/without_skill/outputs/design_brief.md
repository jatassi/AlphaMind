# Design — Treasury Auction Ingest Module

## Overview
Design a new AlphaMind module that ingests Treasury auction results from the Fiscal Data API daily. The module normalizes raw API JSON into strongly typed records, persists them durably to SQLite via SQLAlchemy, and emits a structured event for the distillation layer to consume.

**Key constraints:** Python 3.13, async at I/O boundary, SQLAlchemy + Alembic migrations, existing `src/alphamind/<feature>/...` layout, untrusted input (Fiscal Data JSON), once-per-day invocation from pipeline orchestrator.

---

## Architecture

### Module Structure

```
src/alphamind/treasury_auctions/
├── __init__.py                    # Public API
├── client.py                      # HTTP client (wraps TreasuryClient)
├── models.py                      # Pydantic v2 typed records
├── normalizer.py                  # Untrusted JSON → typed record conversion
├── persistence.py                 # DB operations + event emission
└── collector.py                   # Main entry point (called by orchestrator)
```

Reuse the existing `TreasuryClient` from `src/alphamind/data_sources/treasury/client.py` (already live, rate-limited, with retry wrappers). Do not duplicate it.

---

## Design Details

### 1. Typed Records (`models.py`)

Define **immutable Pydantic v2 models** for API shape and internal domain models. Constraint: trust nothing from the API.

```python
from pydantic import BaseModel, Field, field_validator
from typing import Optional
from decimal import Decimal

class RawAuctionRecord(BaseModel):
    """Direct mapping of Fiscal Data API JSON schema."""
    record_date: str
    security_term: str
    high_yield: Optional[str]
    avg_med_yield: Optional[str]
    bid_to_cover_ratio: Optional[str]
    primary_dealer_accepted: Optional[str]
    indirect_bidder_accepted: Optional[str]
    direct_bidder_accepted: Optional[str]
    total_accepted: Optional[str]
    
    class Config:
        extra = "forbid"  # Reject unknown fields


class TreasuryAuctionRecord(BaseModel):
    """Internal domain model — normalized, validated, ready for storage."""
    auction_id: str
    tenor: str  # "2Y" | "5Y" | "10Y" | "30Y"
    auction_date: str  # ISO 8601 date
    auction_yield_bp: Optional[float] = None  # basis points
    bid_to_cover: Optional[float] = None
    tail_bp: Optional[float] = None
    primary_dealer_pct: Optional[float] = None
    indirect_pct: Optional[float] = None
    direct_pct: Optional[float] = None
    auction_size_usd: Optional[float] = None
    source: str = "treasury"
    
    @field_validator("auction_id")
    @classmethod
    def validate_auction_id_format(cls, v: str) -> str:
        # Must be YYYY-MM-DD_<TENOR>
        assert "_" in v and len(v.split("_")) == 2, "Invalid auction_id format"
        return v
    
    @field_validator("tenor")
    @classmethod
    def validate_tenor(cls, v: str) -> str:
        assert v in ("2Y", "5Y", "10Y", "30Y"), f"Unknown tenor: {v}"
        return v
```

**Rationale:**
- Pydantic v2 provides field validation, JSON serialization, and immutability guarantees.
- `extra="forbid"` on `RawAuctionRecord` rejects any surprise API fields.
- Validators on the domain model enforce invariants (tenor membership, format).
- Separation of API shape from domain shape allows future API changes without data-model churn.

---

### 2. Untrusted Input Normalization (`normalizer.py`)

Strict conversion from raw API record to typed record. **Fail early, log clearly, skip bad rows.**

```python
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation
import logging

logger = logging.getLogger(__name__)

_TENOR_MAP: dict[str, str] = {
    "2-Year": "2Y",
    "5-Year": "5Y",
    "10-Year": "10Y",
    "30-Year": "30Y",
}

def _parse_float_safe(value: str | None) -> float | None:
    """Parse a float from a string-or-None API field.
    
    Returns None for:
    - None or empty string
    - Non-numeric values (logs warning)
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError) as exc:
        logger.warning(f"Failed to parse float: {value!r} ({exc})")
        return None


def _pct_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Compute (numerator / denominator) * 100.0, handling None and zero division."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * 100.0


def normalize_raw_record(
    raw: dict[str, str | None],
) -> TreasuryAuctionRecord | None:
    """Convert raw API dict to typed record, or return None if record is invalid.
    
    Logs all validation failures.  Caller must handle None returns (filter out).
    """
    try:
        # Validate raw shape
        raw_record = RawAuctionRecord(**raw)
    except Exception as exc:
        logger.error(f"Raw record validation failed: {exc}", extra={"raw": raw})
        return None
    
    # Map tenor
    tenor = _TENOR_MAP.get(raw_record.security_term)
    if tenor is None:
        logger.warning(f"Unknown tenor: {raw_record.security_term}")
        return None
    
    # Parse numerics
    high_yield = _parse_float_safe(raw_record.high_yield)
    avg_med_yield = _parse_float_safe(raw_record.avg_med_yield)
    total_accepted = _parse_float_safe(raw_record.total_accepted)
    primary_amt = _parse_float_safe(raw_record.primary_dealer_accepted)
    indirect_amt = _parse_float_safe(raw_record.indirect_bidder_accepted)
    direct_amt = _parse_float_safe(raw_record.direct_bidder_accepted)
    
    # Compute derived fields
    auction_id = f"{raw_record.record_date}_{tenor}"
    auction_yield_bp = high_yield * 100.0 if high_yield is not None else None
    tail_bp = (
        (high_yield - avg_med_yield) * 100.0
        if high_yield is not None and avg_med_yield is not None
        else None
    )
    auction_size_usd = total_accepted / 1e9 if total_accepted is not None else None
    
    # Construct domain record
    try:
        return TreasuryAuctionRecord(
            auction_id=auction_id,
            tenor=tenor,
            auction_date=raw_record.record_date,
            auction_yield_bp=auction_yield_bp,
            bid_to_cover=_parse_float_safe(raw_record.bid_to_cover_ratio),
            tail_bp=tail_bp,
            primary_dealer_pct=_pct_ratio(primary_amt, total_accepted),
            indirect_pct=_pct_ratio(indirect_amt, total_accepted),
            direct_pct=_pct_ratio(direct_amt, total_accepted),
            auction_size_usd=auction_size_usd,
        )
    except Exception as exc:
        logger.error(f"Domain model construction failed: {exc}", extra={"raw": raw})
        return None
```

**Rationale:**
- Each parse step is defensive; failures are logged and skipped.
- Normalization is **deterministic and testable** — no state mutations.
- Caller filters out `None` returns; valid rows flow to persistence.
- Logging includes the raw record for audit trails.

---

### 3. Persistence & Event Emission (`persistence.py`)

Write validated records to `treasury_auctions` table and emit a structured event for the distillation layer.

```python
from datetime import datetime, UTC
from sqlalchemy.orm import Session
from sqlalchemy import select
import logging
import json

from alphamind.persistence.models import TreasuryAuctions
from alphamind.persistence.session import make_session_factory

logger = logging.getLogger(__name__)

class TreasuryAuctionStore:
    """Encapsulates persistence and event emission."""
    
    def __init__(self, session_factory):
        self._session_factory = session_factory
    
    def insert_auctions(
        self,
        records: list[TreasuryAuctionRecord],
    ) -> tuple[int, int]:
        """Insert records, skipping duplicates. Returns (inserted, skipped)."""
        inserted = 0
        skipped = 0
        
        with self._session_factory() as sess:
            for record in records:
                existing = sess.query(TreasuryAuctions).filter(
                    TreasuryAuctions.auction_id == record.auction_id
                ).first()
                
                if existing is not None:
                    logger.debug(f"Auction {record.auction_id} already persisted; skipping")
                    skipped += 1
                    continue
                
                # Convert Pydantic model to ORM row
                orm_row = TreasuryAuctions(
                    auction_id=record.auction_id,
                    tenor=record.tenor,
                    auction_date=record.auction_date,
                    auction_yield_bp=record.auction_yield_bp,
                    bid_to_cover=record.bid_to_cover,
                    tail_bp=record.tail_bp,
                    primary_dealer_pct=record.primary_dealer_pct,
                    indirect_pct=record.indirect_pct,
                    direct_pct=record.direct_pct,
                    auction_size_usd=record.auction_size_usd,
                    source=record.source,
                    ingested_at=datetime.now(UTC).isoformat(),
                )
                sess.add(orm_row)
                inserted += 1
            
            sess.commit()
        
        return inserted, skipped
    
    def emit_auction_event(
        self,
        auction_record: TreasuryAuctionRecord,
    ) -> None:
        """Emit a structured event for the distillation layer.
        
        The event shape is consumed by q6_macro and other macro-sensitive
        distillation modules. See `docs/design/02-distillation-layer/external.md`.
        """
        event = {
            "event_type": "treasury_auction",
            "auction_id": auction_record.auction_id,
            "tenor": auction_record.tenor,
            "auction_date": auction_record.auction_date,
            "auction_yield_bp": auction_record.auction_yield_bp,
            "bid_to_cover": auction_record.bid_to_cover,
            "tail_bp": auction_record.tail_bp,
            "auction_size_usd": auction_record.auction_size_usd,
            "emitted_at": datetime.now(UTC).isoformat(),
        }
        
        # TODO: Define event queue / publish mechanism with distillation layer.
        # For now, log as JSON so distillation can consume async.
        logger.info(
            f"Treasury auction event: {auction_record.auction_id}",
            extra={"event": json.dumps(event)},
        )
```

**Rationale:**
- Duplicate detection is per-`auction_id` (date + tenor combo).
- ORM rows are constructed from Pydantic models (type-safe mapping).
- Events are emitted for every new auction (not skipped duplicates).
- Event schema is JSON-serializable and includes all fields distillation may need.
- TODO: Event queue will be defined in coordination with distillation-layer design.

---

### 4. Main Collector Entry Point (`collector.py`)

Orchestrator-facing function. Implements the daily collection lifecycle.

```python
from datetime import date, datetime, timedelta, UTC
import logging

from alphamind.data_sources.treasury.client import TreasuryClient
from alphamind.data_sources._common import (
    default_session_factory,
    resume_since,
    track_run,
)
from alphamind.persistence.models import TreasuryAuctions

from .models import TreasuryAuctionRecord, RawAuctionRecord
from .normalizer import normalize_raw_record
from .persistence import TreasuryAuctionStore

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_AUCTIONS_PATH = "/services/api/fiscal_service/v1/accounting/od/auctions_query"
_TENOR_MAP = {
    "2-Year": "2Y",
    "5-Year": "5Y",
    "10-Year": "10Y",
    "30-Year": "30Y",
}


def _fetch_all_pages(
    client: TreasuryClient,
    since: date,
) -> list[dict]:
    """Fetch all pages of auction records from the API since *since*."""
    security_terms = ",".join(_TENOR_MAP.keys())
    params = {
        "fields": ",".join([
            "record_date",
            "security_term",
            "high_yield",
            "avg_med_yield",
            "bid_to_cover_ratio",
            "primary_dealer_accepted",
            "indirect_bidder_accepted",
            "direct_bidder_accepted",
            "total_accepted",
        ]),
        "filter": f"record_date:gte:{since.isoformat()},security_term:in:({security_terms})",
        "page[size]": _PAGE_SIZE,
        "page[number]": 1,
    }
    
    all_records: list[dict] = []
    page_num = 1
    
    while True:
        params["page[number]"] = page_num
        response = client.get(_AUCTIONS_PATH, params=params)
        data = response.get("data", [])
        all_records.extend(data)
        
        links = response.get("links", {})
        if not links.get("next"):
            break
        page_num += 1
        logger.debug(f"Fetched page {page_num-1}, continuing pagination")
    
    logger.info(f"Fetched {len(all_records)} raw records from API")
    return all_records


def collect_treasury_auctions(
    since: date | None = None,
    *,
    _client: TreasuryClient | None = None,
    _session_factory: Any | None = None,
    _repo: Any | None = None,
) -> None:
    """Collect Treasury auction results since *since* and persist.
    
    Parameters
    ----------
    since:
        Start date (inclusive) for the collection window. Defaults to:
        - 7 days before the latest stored auction, or
        - 30 days ago if the table is empty.
    _client:
        TreasuryClient override for testing.
    _session_factory:
        Session factory override for testing.
    _repo:
        Repo override for testing (passed to track_run).
    """
    if _session_factory is None:
        _session_factory = default_session_factory()
    
    if _client is None:
        _client = TreasuryClient()
    
    if since is None:
        since = resume_since(
            column=TreasuryAuctions.auction_date,
            default_lookback=timedelta(days=30),
            overlap=timedelta(days=7),
            session_factory=_session_factory,
        ).date()
    
    with track_run("treasury.auctions", _repo=_repo) as run:
        logger.info(f"Starting Treasury auction collection since {since}")
        
        # Fetch raw records
        raw_records = _fetch_all_pages(_client, since)
        
        # Normalize (filter bad rows)
        normalized = []
        for raw in raw_records:
            record = normalize_raw_record(raw)
            if record is not None:
                normalized.append(record)
        
        logger.info(f"Normalized {len(normalized)} / {len(raw_records)} records")
        
        # Persist and emit events
        store = TreasuryAuctionStore(_session_factory)
        inserted, skipped = store.insert_auctions(normalized)
        
        for record in normalized:
            store.emit_auction_event(record)
        
        run.rows_written = inserted
        logger.info(f"Collection complete: {inserted} inserted, {skipped} duplicates")


def bootstrap_treasury_auctions(
    *,
    _session_factory: Any | None = None,
    _repo: Any | None = None,
) -> None:
    """Bootstrap 12 months of Treasury auction history."""
    today = datetime.now(UTC).date()
    twelve_months_ago = date(today.year - 1, today.month, today.day)
    
    logger.info(f"Bootstrapping Treasury auctions from {twelve_months_ago}")
    collect_treasury_auctions(
        since=twelve_months_ago,
        _session_factory=_session_factory,
        _repo=_repo,
    )
```

**Rationale:**
- Entry point matches existing collector patterns (`collect_*`, `bootstrap_*`).
- `track_run` context manager logs the run to `collection_runs` automatically.
- Resume logic pulls from the latest stored `auction_date` with 7-day overlap (catches late arrivals).
- Injection points (`_client`, `_session_factory`, `_repo`) enable testability.
- Event emission happens for every successful (non-duplicate) record.

---

### 5. Public API (`__init__.py`)

```python
"""Treasury auction data collection and persistence."""

from .collector import collect_treasury_auctions, bootstrap_treasury_auctions
from .models import TreasuryAuctionRecord

__all__ = [
    "collect_treasury_auctions",
    "bootstrap_treasury_auctions",
    "TreasuryAuctionRecord",
]
```

---

## Integration Points

### Orchestrator Invocation
The pipeline orchestrator calls this once per day:

```python
from alphamind.treasury_auctions import collect_treasury_auctions

collect_treasury_auctions()  # Uses defaults: resume from last stored date, today's UTC time
```

Register in `config/collector_schedule.yaml`:

```yaml
treasury.auctions:
  cadence: daily
  hour_utc: 10  # 10 UTC every day
  bootstrap: true
```

### Distillation Layer Consumption
Events are logged as JSON (step 3, above). The distillation layer (`q6_macro`) subscribes:

```python
# In distillation/q6_macro.py
def consume_treasury_auction_event(event: dict) -> None:
    """Ingest Treasury event and update internal state."""
    tenor = event["tenor"]
    auction_yield_bp = event["auction_yield_bp"]
    
    # Use in yield-curve regime classifier
    update_2y10y_spread(tenor, auction_yield_bp)
```

**TODO:** Define formal event queue / pub-sub with the distillation layer. For MVP, structured JSON logging is sufficient.

---

## Migration & Schema

No new Alembic migration needed — `TreasuryAuctions` already exists in `src/alphamind/persistence/models.py`.

If schema changes are needed, generate a new migration:

```bash
alembic revision --autogenerate -m "update_treasury_auctions_schema"
```

---

## Testing Strategy

### Unit Tests (`tests/treasury_auctions/`)

**`test_models.py`:**
- Pydantic validators (tenor validation, auction_id format).
- RawAuctionRecord rejects unknown fields.

**`test_normalizer.py`:**
- `_parse_float_safe`: valid numbers, None, empty string, non-numeric strings.
- `_pct_ratio`: normal case, None denominator, zero denominator.
- `normalize_raw_record`: full happy path, missing tenor, malformed JSON shapes.
- Log output verification (each failure is logged).

**`test_persistence.py`:**
- `insert_auctions`: insert new, skip duplicates.
- `emit_auction_event`: JSON shape correctness.

**`test_collector.py`:**
- `_fetch_all_pages`: mock `TreasuryClient`, verify pagination, field extraction.
- `collect_treasury_auctions`: mock fetch + persistence, verify `track_run` context.
- Resume logic: query latest `auction_date`, compute overlap window.
- Bootstrap: 12 months ago is correct.

### Integration Tests

- E2E: real API (or canned response), full normalize → persist → emit pipeline.
- DB state verification: rows in `treasury_auctions`, no duplicates on re-run.

---

## Error Handling

| Error | Handling |
|-------|----------|
| API connectivity lost | Wrapped by `TreasuryClient` retries (`critical` tier); if all retries exhaust, exception propagates and `track_run` marks run as failed. |
| Malformed JSON record | `normalize_raw_record` returns None; row is filtered and skipped; logged at WARNING level. |
| Duplicate `auction_id` | Skipped on insert; logged at DEBUG; run.rows_written is not incremented. |
| DB write failure | SQLAlchemy exception propagates; `track_run` marks run as failed. |
| Unknown tenor | `normalize_raw_record` returns None; skipped; logged at WARNING. |

---

## Configuration & Environment

No new environment variables required. Uses existing Treasury API setup:

- **API key:** None (public API).
- **Base URL:** Defaults to `https://api.fiscaldata.treasury.gov`.
- **Rate limit:** 60 req/min (conservative; set in `TreasuryClient`).

---

## Performance & Scalability

- **Daily cadence:** 20–50 auction records per day (4 tenors × daily auctions). Negligible DB load.
- **Pagination:** Up to 100 records per page; typical run is 1–2 pages.
- **Retention:** No retention policy yet. All historical auctions kept indefinitely.
- **Indexing:** Existing `ix_treasury_auctions_date_tenor` supports `auction_date` + `tenor` queries (used by distillation).

---

## Security & Validation

- **Untrusted input:** All API fields validated by Pydantic; unknown fields rejected.
- **Type safety:** Normalized to Python `float`, `str` types; Pydantic ensures bounds.
- **SQL injection:** SQLAlchemy ORM used exclusively; parameterized queries prevent injection.
- **Logging:** Sensitive data (API responses) are logged only at DEBUG level; PII (if any) is excluded.

---

## Future Extensions

1. **Event queue:** Replace JSON logging with formal pub-sub (e.g., Redis Streams, in-process queue).
2. **Retention policy:** Prune old auctions after N months (configurable via Class A).
3. **Incremental updates:** Support mid-auction updates (e.g., if CUSIP data corrections occur).
4. **Cross-tenor correlations:** Compute and cache tenor spread metrics (2y10y, 2y30y, etc.) for distillation's use.

# 04a — Settlement-date calculator

## Goal

Ship a settlement-date calculator that computes the date proceeds from a sell-side fill become available cash, per `SETTLEMENT_DAYS_BY_INSTRUMENT` (T+1 for equities and options) and Alpaca's trading calendar (skipping weekends + holidays). One module-level function `compute_settlement_date(trade_date: date, instrument_class: InstrumentClass, calendar: TradingCalendarCache) -> date` that consumers — paper-evaluation harness, future cash-ledger settlement integration — call. No state, no side effects.

## Reading

* `docs/design/05-execution-layer/venue-configuration.md` § Settlement — T+1 cycle, business-day basis, pre-settlement credit semantics.
* `src/alphamind/execution/venue_configuration/constants.py` (story 02g) — `SETTLEMENT_DAYS_BY_INSTRUMENT`, `InstrumentClass`.
* `src/alphamind/execution/venue_configuration/calendar_cache.py` (story 03a) — `TradingCalendarCache.business_day_offset(start, days)` is the primitive this story composes with.
* <issue id="e12a677b-a183-4f5d-bc48-5669337441c1">ALP-121</issue> parent body § Pre-resolved configuration decisions (E), (J).

## Depends on

* <issue id="c2950555-2a1c-4bc4-b07a-03999713ebdf">ALP-385</issue> (02g — Venue constants module).
* <issue id="9c9aacfd-3da4-4968-9061-4fad9defd0ea">ALP-386</issue> (03a — Trading calendar / clock cache).

## Scope

Source under `src/alphamind/execution/venue_configuration/settlement.py` (new file). Tests at `tests/execution/venue_configuration/test_settlement.py`.

### 1. `compute_settlement_date`

```python
from datetime import date

from alphamind.execution.venue_configuration.calendar_cache import TradingCalendarCache
from alphamind.execution.venue_configuration.constants import (
    SETTLEMENT_DAYS_BY_INSTRUMENT,
    InstrumentClass,
)


def compute_settlement_date(
    trade_date: date,
    instrument_class: InstrumentClass,
    *,
    calendar: TradingCalendarCache,
) -> date:
    """Return the settlement date for a trade executed on `trade_date`.

    Per venue-configuration.md § Settlement:
    * Equities and options: T+1 (one business day after trade_date).
    * Crypto: T+0 (same day; not in AlphaMind's universe).

    Business days are derived from Alpaca's trading calendar — weekends
    and holidays are excluded. Uses TradingCalendarCache.business_day_offset.

    For trades executed on a non-business day (e.g., a CA settled in the
    OMS on a holiday), the trade_date is rolled forward to the next
    business day before applying the settlement offset.
    """
    settlement_days = SETTLEMENT_DAYS_BY_INSTRUMENT[instrument_class]
    if settlement_days == 0:
        # Crypto: same-day. Still roll forward if trade_date isn't a business day.
        return calendar.business_day_offset(trade_date, 0)
    return calendar.business_day_offset(trade_date, settlement_days)
```

`business_day_offset(d, 0)` is defined per story 03a as "the next business day at or after `d`" so non-business `trade_date`s roll forward.

### 2. Helper: `is_settled`

A second small helper for callers that need a boolean:

```python
def is_settled(
    trade_date: date,
    instrument_class: InstrumentClass,
    *,
    as_of: date,
    calendar: TradingCalendarCache,
) -> bool:
    """True if a trade executed on trade_date has settled as of `as_of`."""
    return as_of >= compute_settlement_date(trade_date, instrument_class, calendar=calendar)
```

### 3. Public surface

In `src/alphamind/execution/venue_configuration/__init__.py`:

```python
from alphamind.execution.venue_configuration.settlement import (
    compute_settlement_date,
    is_settled,
)
```

### Out of scope

* Pre-settlement credit accounting — already covered by `PRE_SETTLEMENT_CREDIT_ENABLED = True` in story 02g; consumers (cash ledger, paper harness) decide when to apply it.
* Cash-ledger settled-vs-unsettled balance tracking — that's the OMS cash-ledger's responsibility; this story only computes the date.
* T+0 / T+2 / longer settlement cycles — design says T+1 for AlphaMind's universe.

## Acceptance criteria

- [ ] `src/alphamind/execution/venue_configuration/settlement.py` exists; `compute_settlement_date` and `is_settled` are importable from `alphamind.execution.venue_configuration`.
- [ ] `compute_settlement_date(date(2026, 5, 11), "equity", calendar=cache)` returns `date(2026, 5, 12)` (Mon → Tue, both business days).
- [ ] `compute_settlement_date(date(2026, 5, 8), "equity", calendar=cache)` returns `date(2026, 5, 11)` (Fri → Mon, skipping weekend).
- [ ] `compute_settlement_date(date(2026, 5, 9), "equity", calendar=cache)` returns the next business day's settlement (Sat trade_date rolls forward to Mon trade_date, then T+1 = Tue) — i.e., `date(2026, 5, 12)`.
- [ ] `compute_settlement_date(<trade date the day before a market holiday>, "equity", calendar=cache)` correctly skips the holiday.
- [ ] `compute_settlement_date(<trade_date>, "option", calendar=cache)` returns the same as for equity (both T+1).
- [ ] `compute_settlement_date(<trade_date>, "crypto", calendar=cache)` returns `trade_date` if it's a business day, else the next business day.
- [ ] `is_settled(trade_date=date(2026, 5, 8), "equity", as_of=date(2026, 5, 11), calendar=cache)` is `True`.
- [ ] `is_settled(trade_date=date(2026, 5, 8), "equity", as_of=date(2026, 5, 10), calendar=cache)` is `False`.
- [ ] Tests use a fake `TradingCalendarCache` with controlled holidays; cover: standard T+1, Friday → Monday, around a market holiday, weekend trade-date roll-forward, crypto T+0, is_settled boundary.
- [ ] `uv run pytest tests/execution/venue_configuration/test_settlement.py -n auto` is green.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` is clean.

## Verification

* Run `uv run pytest tests/execution/venue_configuration/test_settlement.py -n auto -v` — every new test passes.
* Run `uv run pytest -n auto` — full suite green.
* Spot-check via story 05's verify script — fetch Alpaca's calendar for the next 30 days, compute settlement for a fixture trade_date, assert the result is one business day past trade_date.
* Lint clean per CLAUDE.md.

---
status: not_started
completed_date:
commit_id:
---

# 05 — Event calendar storage and loader

## Goal

Land the operator-curated event calendar that drives the pre-event tightening overlay activation. Ships the YAML schema (`config/event_calendar.yaml`), the Pydantic model (`EventCalendarFile`), the loader (`load_event_calendar`), and the lookup API the pre-event overlay activator (story 06a) consumes — "given a current invocation timestamp, return the events that are within the active window." The calendar is operator-maintained quarterly per the design's `Implementation: Pipeline schedule includes a catalog of known event dates (FOMC, economic calendar), updated quarterly`.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Scheduled high-impact events — the pre-event overlay's activation contract; events relevant to the overlay (FOMC, CPI/PPI, major earnings clusters) and the operator-maintained-quarterly cadence
- `docs/design/configuration-management.md` § overlays/pre-event.yaml — the overlay's `activation.events` enum (`fomc`, `cpi`, `ppi`, `pce`, `nfp`, `earnings`); the calendar's events draw from this same vocabulary
- `config/overlays/pre-event.yaml` — the live overlay config; `activation.windows_before_event: 2` (number of invocations before event); the calendar's job is to declare *when* the events are
- `src/alphamind/config/models/overlays.py` — `EventType` StrEnum (`fomc`, `cpi`, `ppi`, `pce`, `nfp`, `earnings`) — the calendar's `event_type` field uses this enum
- `02-package-skeleton-and-types.md` — `EventCalendarEntry` and `EventCalendar` typed records
- `src/alphamind/config/loaders.py` — sibling YAML loader patterns (`_load_yaml`, parse-then-validate cycle); the calendar's loader follows the same shape
- `src/alphamind/config/validation/` — sibling cross-reference validation patterns
- `06a-pre-event-overlay-activator.md` — the consumer; the calendar's lookup API matches what the activator needs

## Depends on

- 02 (package skeleton + types)

## Scope

In scope: a new YAML file, a Pydantic schema, a loader, a lookup function, and a validation pass. Source under `src/alphamind/risk_guardrails/regime_adaptation/event_calendar.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_event_calendar.py`. The shipped YAML at `config/event_calendar.yaml`.

### 1. The shipped `config/event_calendar.yaml`

```yaml
# Operator-curated catalog of scheduled high-impact catalysts.
# Drives the pre-event tightening overlay (config/overlays/pre-event.yaml).
# Updated quarterly per docs/design/06-risk-guardrails/regime-adaptation.md
# § Scheduled high-impact events. Each entry's event_type must match an
# EventType enum member (config/models/overlays.py). Timestamps are UTC
# ISO-8601; the overlay activator combines them with config/scheduler.yaml's
# trigger schedule to determine "N invocations before the event".

entries:
  # Bootstrap empty list. Operator populates with upcoming events as part
  # of the quarterly review. Example shape:
  #
  # - event_type: fomc
  #   event_timestamp_utc: 2026-06-17T18:00:00Z
  #   label: "FOMC June 2026"
  #
  # - event_type: cpi
  #   event_timestamp_utc: 2026-05-13T12:30:00Z
  #   label: "CPI April 2026 release"
  #
  # - event_type: earnings
  #   event_timestamp_utc: 2026-04-29T20:00:00Z
  #   label: "NVDA Q1 earnings"
```

The shipped file has an empty `entries: []` (or a single commented-out example block). The operator populates it during the quarterly review.

### 2. Pydantic schema — `EventCalendarFile`

In `src/alphamind/risk_guardrails/regime_adaptation/event_calendar.py`:

```python
class EventCalendarFile(BaseModel):
    """Pydantic model for config/event_calendar.yaml."""
    model_config = ConfigDict(frozen=True)

    entries: list[EventCalendarFileEntry] = Field(default_factory=list)


class EventCalendarFileEntry(BaseModel):
    """One entry as parsed from YAML."""
    model_config = ConfigDict(frozen=True)

    event_type: EventType                    # imports from config/models/overlays.py
    event_timestamp_utc: datetime
    label: str = Field(min_length=1)

    @field_validator("event_timestamp_utc")
    @classmethod
    def _require_tz_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            msg = "event_timestamp_utc must be timezone-aware"
            raise ValueError(msg)
        if v.utcoffset().total_seconds() != 0:
            msg = "event_timestamp_utc must be UTC (offset 00:00)"
            raise ValueError(msg)
        return v
```

The Pydantic schema is the parse-time surface. The downstream typed records `EventCalendar` and `EventCalendarEntry` (from story 02's `types.py`) are the runtime surface — produced by the loader, consumed by the activator.

### 3. Loader

```python
def load_event_calendar(path: Path) -> EventCalendar:
    """Parse the YAML, validate, and produce the typed runtime record.

    Raises EventCalendarParseError (subclass of ValueError) if the file is
    missing, malformed, or fails schema validation. The error wraps the
    underlying exception with the file path in the message.
    """
```

Implementation:
- Reads the YAML file.
- Parses into `EventCalendarFile` via `EventCalendarFile.model_validate(...)`.
- Adapts each `EventCalendarFileEntry` to `EventCalendarEntry` (the typed record from `types.py`).
- Sorts entries by `event_timestamp_utc` ascending. Duplicate `(event_type, event_timestamp_utc)` pairs raise `EventCalendarParseError` with both fields in the message — the calendar must not double-count an event.
- Returns `EventCalendar(entries=tuple(adapted_entries))`.

### 4. Lookup API

```python
def select_events_within_window(
    *,
    calendar: EventCalendar,
    now_utc: datetime,
    window_end_utc: datetime,
) -> tuple[EventCalendarEntry, ...]:
    """Return the entries whose event_timestamp_utc is in the half-open window
    [now_utc, window_end_utc). Result is sorted by event_timestamp_utc ascending.

    Raises ValueError if either timestamp is naive or non-UTC, or if
    window_end_utc <= now_utc.
    """
```

The activator (06a) computes `window_end_utc` from `now_utc` plus the scheduler's per-trigger cadence × `windows_before_event`. This loader-level function is purely a window query.

### 5. Validation pass — operator-curated freshness

```python
def warn_on_stale_calendar(
    *,
    calendar: EventCalendar,
    now_utc: datetime,
    stale_threshold: timedelta = _DEFAULT_STALE_THRESHOLD,  # 7 days
) -> StaleCalendarReport:
    """Inspect the calendar and emit a stale-calendar warning if the latest
    entry is in the past or fewer than `stale_threshold` ahead of `now_utc`.

    Returns StaleCalendarReport(is_stale: bool, latest_event_timestamp_utc: datetime | None,
    days_until_latest: float | None). Does not raise; the operator dashboard
    surfaces the warning, the orchestrator still runs.
    """
```

```python
@dataclass(frozen=True, slots=True)
class StaleCalendarReport:
    is_stale: bool
    latest_event_timestamp_utc: datetime | None
    days_until_latest: float | None  # negative if latest is in the past; None if calendar is empty
```

`_DEFAULT_STALE_THRESHOLD: timedelta = timedelta(days=7)` — module-level constant. Mirrors the stale-vendor-data alerting pattern: warn the operator if the calendar runs out within a week so they can populate the next quarter's entries.

The orchestrator (09) calls `warn_on_stale_calendar` and surfaces the report through its audit-log entries; an empty calendar is `is_stale=False, days_until_latest=None` (no events means nothing to be stale).

### 6. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_event_calendar.py`:

- **Empty calendar parses:** the shipped `config/event_calendar.yaml` parses into `EventCalendar(entries=())`.
- **Single entry parses:** a fixture YAML with one FOMC entry round-trips into the expected `EventCalendarEntry`.
- **Multiple entries sort by timestamp:** a fixture with three entries in scrambled order produces a tuple sorted ascending by `event_timestamp_utc`.
- **Naive timestamp rejected:** `event_timestamp_utc: 2026-06-17T18:00:00` (no `Z`) raises `EventCalendarParseError`.
- **Non-UTC timestamp rejected:** `event_timestamp_utc: 2026-06-17T18:00:00-05:00` raises `EventCalendarParseError` mentioning "UTC".
- **Empty `label` rejected:** `label: ""` raises `EventCalendarParseError`.
- **Duplicate `(event_type, timestamp)` rejected:** two entries with the same event type and timestamp raise `EventCalendarParseError` mentioning the duplicate.
- **Unknown `event_type` rejected:** `event_type: unknown` raises `EventCalendarParseError` (Pydantic enum validation surfaces it).
- **Missing file raises `EventCalendarParseError`** with the path in the message.
- **Malformed YAML raises `EventCalendarParseError`.**
- **`select_events_within_window` happy path:** fixture with three events at `T=10`, `T=20`, `T=30`; window `[T=15, T=25)` returns only the `T=20` event.
- **`select_events_within_window` lower bound is inclusive:** event at exactly `now_utc` is included.
- **`select_events_within_window` upper bound is exclusive:** event at exactly `window_end_utc` is excluded.
- **`select_events_within_window` returns sorted-ascending tuple:** verified by checking the result against the sorted input.
- **`select_events_within_window` rejects naive `now_utc` or `window_end_utc`** with `ValueError`.
- **`select_events_within_window` rejects `window_end_utc <= now_utc`** with `ValueError`.
- **`warn_on_stale_calendar` empty calendar:** `is_stale=False`, `latest_event_timestamp_utc=None`, `days_until_latest=None`.
- **`warn_on_stale_calendar` future calendar:** latest event 30 days ahead, `is_stale=False`, `days_until_latest≈30`.
- **`warn_on_stale_calendar` near-stale calendar:** latest event 5 days ahead, `is_stale=True` (under 7-day threshold).
- **`warn_on_stale_calendar` past calendar:** latest event 10 days in the past, `is_stale=True`, `days_until_latest≈-10`.

Out of scope:

- The pre-event overlay activator's logic for "N invocations before event" — that consumes this loader's lookup function and adds the scheduler-cadence math; it lives in story 06a.
- A YAML-edit tool or operator UI for populating the calendar — the operator edits the file by hand quarterly; the command-center config view (when it ships) renders the file for review.
- Importing events from external calendars (e.g., Federal Reserve API for FOMC dates, BLS API for CPI) — the operator transcribes manually for now per the design's "updated quarterly" cadence.
- Enriching events with venue-specific timing (NYSE open/close, fed-funds release time) — the `event_timestamp_utc` is what the overlay activator consumes; the time-of-day specificity is what the operator chooses to encode.

## Notes

The calendar is intentionally small in scope. Its only consumer is the pre-event overlay activator; its only operator surface is the YAML file. Per `feedback_simplify_before_building.md`, the file does not include per-entry overrides like "use a different `windows_before_event` for this specific FOMC" — the overlay's parameters are uniform across events. If a real operating need surfaces (e.g., presidential elections deserve a 5-invocation window), that lands as an overlay-config additive, not as per-entry calendar metadata.

Per `feedback_no_inventing_component_names.md`, `EventCalendar` and `EventCalendarEntry` are introduced in story 02 (typed records). This story's Pydantic shapes (`EventCalendarFile`, `EventCalendarFileEntry`) are parse-time intermediaries; they exist because Pydantic's `frozen=True` model is the established YAML-validation pattern in `alphamind.config.models`. The runtime contract is the typed-record pair from story 02.

Per `feedback_avoid_numeric_anchors.md`, the only numeric constant is `_DEFAULT_STALE_THRESHOLD = timedelta(days=7)`. Named, documented, configurable via the function parameter. The 7-day default mirrors typical operator quarterly-review cadence (one week of buffer to catch the calendar before it lapses); not derived from any market-behavior threshold.

The `select_events_within_window` half-open interval semantic is the standard Python convention. Edge cases (an event at exactly `now_utc`) are included; an event at exactly `window_end_utc` is excluded. This means an event scheduled at the exact moment of the next invocation is captured by the window for *this* invocation; the activator's "windows_before_event" semantic counts that as the +1 step.

Per `feedback_per_producer_schema.md`, the calendar is one schema with one writer (the operator) and one consumer (the pre-event activator). No discriminated union; one entry per event. If a future overlay (e.g., a "post-event recovery" overlay) wants the same calendar data, it imports the loader and calls `select_events_within_window` with its own window — no schema fork needed.

## Acceptance criteria

- [ ] `config/event_calendar.yaml` exists with `entries: []` (or one commented-out example) and the documented header comment.
- [ ] `src/alphamind/risk_guardrails/regime_adaptation/event_calendar.py` exists and defines `EventCalendarFile`, `EventCalendarFileEntry`, `load_event_calendar`, `select_events_within_window`, `warn_on_stale_calendar`, `StaleCalendarReport`, `EventCalendarParseError`, `_DEFAULT_STALE_THRESHOLD`.
- [ ] `load_event_calendar`, `select_events_within_window`, `warn_on_stale_calendar`, `StaleCalendarReport`, `EventCalendarParseError` are re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] The shipped `config/event_calendar.yaml` parses into `EventCalendar(entries=())`.
- [ ] Multi-entry YAML parses with entries sorted ascending by `event_timestamp_utc`.
- [ ] Naive or non-UTC `event_timestamp_utc` raises `EventCalendarParseError` mentioning the field.
- [ ] Empty `label` and unknown `event_type` raise `EventCalendarParseError`.
- [ ] Duplicate `(event_type, event_timestamp_utc)` pairs raise `EventCalendarParseError`.
- [ ] Missing file path or malformed YAML raises `EventCalendarParseError` with the path in the message.
- [ ] `select_events_within_window` returns events in `[now, end)` half-open interval, sorted ascending.
- [ ] `select_events_within_window` rejects naive timestamps or `window_end_utc <= now_utc` with `ValueError`.
- [ ] `warn_on_stale_calendar` correctly classifies empty / future / near-stale (≤ 7 days) / past calendars.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.

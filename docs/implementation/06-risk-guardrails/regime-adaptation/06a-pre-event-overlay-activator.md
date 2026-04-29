---
status: in_progress
completed_date:
commit_id:
---

# 06a — Pre-event overlay activator

## Goal

Land the pure function that, given the current invocation timestamp, the loaded event calendar, the scheduler config, and the pre-event overlay config, decides whether the `pre_event` overlay is active for *this* invocation — and whether this is the *final* invocation before a pre-event activation (the case where `block_new_positions: true` fires). Encodes the design's "the pre-event tightening overlay applies for the 2 invocations preceding the event" mechanic by walking the scheduler's upcoming firings, counting how many fall before each event in the calendar window, and matching against the overlay's `windows_before_event` knob.

## Reading

- `docs/design/06-risk-guardrails/regime-adaptation.md` § Scheduled high-impact events — the activation contract: 2 invocations preceding the event, `block_new_positions: true` on the final invocation, lifts at the first invocation after the event
- `config/overlays/pre-event.yaml` — the overlay's parameters: `activation.windows_before_event: 2`, `multipliers.position_max_size_pct: 0.80`, `final_invocation_before_event.block_new_positions: true`
- `config/scheduler.yaml` — the scheduler's per-trigger cron expressions; the activator computes "next N firings" from these
- `src/alphamind/config/models/scheduler.yaml` (model) and `models/overlays.py` — the typed shapes the activator consumes
- `02-package-skeleton-and-types.md` — `OverlayActivationDecision` is the activator's output; its invariants (`pre_event_block_new_positions=True` requires `overlay==pre_event` AND `is_active==True`) are checked by the typed-record constructor
- `05-event-calendar-loader.md` — `select_events_within_window` is the calendar lookup; this story consumes it

## Depends on

- 02 (package skeleton + types)
- 05 (event calendar loader)

## Scope

In scope, all under `src/alphamind/risk_guardrails/regime_adaptation/pre_event_activator.py`. Tests at `tests/risk_guardrails/regime_adaptation/test_pre_event_activator.py`.

### 1. Public function

```python
def evaluate_pre_event_overlay(
    *,
    now_utc: datetime,
    event_calendar: EventCalendar,
    scheduler_config: SchedulerConfig,
    pre_event_overlay: PreEventOverlay,
) -> OverlayActivationDecision
```

Returns an `OverlayActivationDecision` with `overlay=Overlay.pre_event`. Determines `is_active`, `pre_event_block_new_positions`, and `rationale` per the rules below.

### 2. Activation rules

The function asks: **"Is this invocation within `windows_before_event` invocations preceding any event whose `event_type` is in `pre_event_overlay.activation.events`?"**

Step-by-step:

1. **Filter the calendar to relevant events.** Drop entries whose `event_type` is not in `pre_event_overlay.activation.events`.
2. **Compute the upcoming-firings ladder.** From `now_utc` (inclusive), enumerate the next `windows_before_event + 1` scheduled firings across all triggers in `scheduler_config.triggers`. The +1 is because the activator needs to look one invocation past the window's end to detect the case where the current invocation *is* the firing immediately preceding an event.
3. **For each calendar event in the look-ahead window**, count how many of the upcoming firings fall in the half-open interval `[now_utc, event_timestamp_utc)`:
   - **0 firings before the event between now and the event** → the current invocation is the final invocation before the event. `is_active=True`, `pre_event_block_new_positions=True`.
   - **`1..(windows_before_event - 1)` firings before the event** → the overlay is active but this is not the final invocation. `is_active=True`, `pre_event_block_new_positions=False`.
   - **`>= windows_before_event` firings before the event** → outside the window. Continue scanning the next event.
4. **If any event triggered an `is_active=True` decision**, return that decision. The first activating event wins (multiple events within the window collapse to one activation; the overlay's parameters do not stack per-event).
5. **No activating event** → `is_active=False`, `pre_event_block_new_positions=False`, `rationale=""`.

### 3. Computing the upcoming-firings ladder

`scheduler_config.triggers` is a `dict[str, str]` mapping a trigger key to a cron expression. For each cron expression, the activator computes the next `K` firings ≥ `now_utc` using `croniter` (already a project dependency for the scheduler).

```python
def _compute_upcoming_firings(
    *,
    now_utc: datetime,
    scheduler_config: SchedulerConfig,
    horizon_count: int,
) -> tuple[datetime, ...]:
    """Return the next horizon_count firings across all triggers, sorted ascending.

    Cron expressions are interpreted in scheduler_config.timezone, and the
    resulting firings are converted to UTC. Duplicates (multiple triggers
    firing at the same wall-clock instant) collapse to one firing per UTC
    instant.
    """
```

Implementation requirements:
- Uses `croniter` per trigger to enumerate the next `horizon_count` fires from `now_utc`.
- Combines all triggers' fires into a single sorted-ascending tuple.
- De-duplicates by UTC instant (a `pre_open` and `weekend_sunday` could in theory overlap; in practice they don't, but the function is defensive).
- Truncates the combined list to `horizon_count` elements (the union may exceed it).
- Returns the truncated, sorted, de-duplicated tuple.

### 4. Computing `horizon_count`

The activator passes `horizon_count = pre_event_overlay.activation.windows_before_event + 1` to `_compute_upcoming_firings`, then for each event walks the firings list and counts the ones in `[now_utc, event_timestamp_utc)`.

This bounds the look-ahead — the activator does not need to enumerate firings beyond the window's far edge. For a `windows_before_event: 2` overlay, the activator enumerates at most 3 firings. For each calendar event in the next ~12 hours (the longest plausible inter-firing gap is the weekend), the activator measures whether ≤ 1 firing falls between now and the event.

### 5. Computing `rationale`

When `is_active=True`, populate `rationale` with the activating event's `label` and the ordinal-position-within-window:

- `pre_event_block_new_positions=True` → `f"Pre-event window: final invocation before {event.label} (event at {event.event_timestamp_utc.isoformat()})"`
- otherwise → `f"Pre-event window: invocation T-{n_firings_before_event + 1} of {windows_before_event} preceding {event.label}"`

The string is human-readable and lands in the audit log; the orchestrator (09) does not parse it.

### 6. Tests

Tests at `tests/risk_guardrails/regime_adaptation/test_pre_event_activator.py`. Use a fixture scheduler config matching `config/scheduler.yaml` (or a simpler synthetic schedule for some cases — both shapes appear).

- **No upcoming events:** empty calendar, any `now_utc`. Returns `is_active=False`, `pre_event_block_new_positions=False`, `rationale=""`.
- **Event well outside window:** calendar has a `fomc` event 5 days ahead; the next 3 firings are all at least a day before the event. Returns `is_active=False`. (Overlay window = 2; no firing-count of 0 or 1 falls before the event.)
- **Final invocation before event:** calendar has an `fomc` event at `2026-06-17T18:00Z`; `now_utc=2026-06-17T17:30Z` (the `pre_close` trigger time on a Wednesday). The next firing in the schedule is `2026-06-17T20:00Z` (off_hours_rolling), which is *after* the event. So zero firings fall in `[17:30, 18:00)`. Returns `is_active=True`, `pre_event_block_new_positions=True`, `rationale` mentions "final invocation before".
- **One invocation before final:** calendar has an `fomc` event at `2026-06-17T18:00Z`; `now_utc=2026-06-17T15:30Z` (`pre_close`); the next firing is `2026-06-17T17:30Z` (synthetic — to set up the test). One firing falls in `[15:30, 18:00)` (the `17:30` pre-close run). With `windows_before_event=2`, this is the T-2 step. Returns `is_active=True`, `pre_event_block_new_positions=False`, `rationale` mentions "T-2".
  - Use a synthetic scheduler config for this case rather than the production one — the production schedule's gaps don't always produce the right firing density.
- **Two invocations before — outside the window:** with `windows_before_event=2`, two firings between now and the event mean the current invocation is at T-3, outside the window. Returns `is_active=False`.
- **Multiple events, only one activating:** calendar has two events — one `fomc` event 1 day ahead (within window) and one `cpi` event 2 weeks ahead (outside window). Returns the activating decision for the `fomc` event.
- **Multiple events all within window:** calendar has two events in the next day. Returns `is_active=True`; the rationale references the first (earliest by timestamp) event. The overlay's parameters do not stack — a single activation regardless of event count.
- **Event type not in `activation.events`:** calendar has a `nfp` event but the overlay's `activation.events` is `[fomc, cpi]`. Returns `is_active=False`.
- **Event in the past:** calendar has an event 1 day ago. Returns `is_active=False` (the `[now_utc, event_timestamp_utc)` interval is empty for past events; the activator does not look backwards).
- **Event exactly at `now_utc`:** edge case — the event's timestamp equals `now_utc`. The interval `[now_utc, event_timestamp_utc) = [now_utc, now_utc)` is empty; the activator does not activate. (The overlay's purpose is *pre-event* tightening; events that have already arrived are managed by the regime classification or by the overlay lifting "at the first invocation after the event".)
- **Cron timezone correctness:** scheduler config with `timezone: US/Eastern`; calendar event in UTC; the activator correctly converts cron firings to UTC for comparison. Verified by setting up a scenario where a US/Eastern firing time differs from UTC by 5 hours (winter EST) or 4 hours (summer EDT) and confirming the activation decision matches the UTC-aligned answer.
- **`OverlayActivationDecision.overlay` is always `Overlay.pre_event`** regardless of activation outcome.
- **Determinism / purity:** identical inputs produce identical outputs.

Out of scope:

- The stress overlay activator — story 06b.
- Lifting the overlay at the first invocation *after* the event — that follows automatically from the activation rules: the next invocation after the event has zero events ahead of it within the window, so `is_active=False`. No special "lift" code path needed.
- Detecting whether the event triggered a regime transition (e.g., FOMC drives VIX up enough that the regime goes from `normal` to `elevated`) — that's the regime mapper's job; the overlay activator only consults the calendar.
- Per-event override of overlay parameters (e.g., a particular FOMC deserves a 5-invocation window) — the overlay's parameters are uniform across all events the calendar contains.
- Caching of `croniter` outputs across calls — the activator is invoked once per pipeline invocation and the look-ahead is bounded; caching would be premature.

## Notes

The "final invocation before" semantic is the trickiest part. The design says the overlay applies for "the 2 invocations preceding the event" with `block_new_positions: true` on "the final invocation before the event". This means:

- Invocation at T-2 (two firings before the event): `is_active=True`, `block_new=False`
- Invocation at T-1 (the final firing before the event): `is_active=True`, `block_new=True`
- Invocation at T (the first firing after the event): `is_active=False`

The activator computes "T-N" by counting firings in `[now_utc, event_timestamp_utc)`:
- 0 firings between now and event → this is T-1 (the final invocation before)
- 1 firing → this is T-2
- 2 firings → this is T-3 (outside window with `windows_before_event=2`)

The +1 horizon offset (`windows_before_event + 1`) is what lets the activator detect T-1 — it needs to confirm that "the next firing after now is *after* the event" (i.e., zero firings fall before the event).

Per `feedback_no_inventing_component_names.md`, the function name `evaluate_pre_event_overlay` mirrors the design's "evaluate the pre-event overlay activation" phrasing. The internal `_compute_upcoming_firings` helper uses standard scheduling-library terminology.

Per `feedback_simplify_before_building.md`, the activator does not maintain a state-of-the-overlay across invocations. Activation is recomputed every invocation from scratch. If a future requirement emerges (e.g., "remember that we activated for this event so we don't re-activate after a calendar correction"), that's a follow-up — premature for v1.

Per `feedback_avoid_numeric_anchors.md`, all numeric thresholds come from the overlay config (`windows_before_event: 2`) — none hard-coded in the function.

The cron-firings computation is the most expensive operation. With 6 triggers and a horizon of 3 firings each, `croniter` is invoked 6 times to produce 18 candidate firings, then sorted-and-truncated to 3. Bounded; no scaling concern.

Per `feedback_per_producer_schema.md`, the activator returns `OverlayActivationDecision` with `overlay=Overlay.pre_event`. The stress activator (06b) uses the same record with `overlay=Overlay.stress`. Same shape; one record per overlay; the consumer (08) treats them uniformly.

## Acceptance criteria

- [ ] `src/alphamind/risk_guardrails/regime_adaptation/pre_event_activator.py` exists and defines `evaluate_pre_event_overlay`, `_compute_upcoming_firings`.
- [ ] `evaluate_pre_event_overlay` is re-exported from `src/alphamind/risk_guardrails/regime_adaptation/__init__.py`.
- [ ] Empty calendar returns `is_active=False`, `pre_event_block_new_positions=False`, `rationale=""`.
- [ ] Calendar event well outside window returns `is_active=False`.
- [ ] Calendar event with zero firings before it (final invocation case) returns `is_active=True`, `pre_event_block_new_positions=True`, rationale mentioning "final invocation before" and the event label.
- [ ] Calendar event with 1 firing before it (T-2 of a window=2 overlay) returns `is_active=True`, `pre_event_block_new_positions=False`, rationale mentioning "T-2".
- [ ] Calendar event with 2 firings before it (window=2) returns `is_active=False`.
- [ ] Multiple events with one within window returns the activating decision for that event.
- [ ] Event whose `event_type` is not in `pre_event_overlay.activation.events` does not activate.
- [ ] Event in the past does not activate.
- [ ] Event exactly at `now_utc` does not activate.
- [ ] Cron firings computed in the scheduler's `timezone` are correctly compared against UTC event timestamps.
- [ ] Returned `OverlayActivationDecision.overlay` is always `Overlay.pre_event`.
- [ ] The function is pure: equal inputs produce equal outputs.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.

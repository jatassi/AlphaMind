"""Pydantic model for ``config/continuous_monitor.yaml`` (ALP-432 story 01).

The continuous monitor's Class A tunables per parent issue ALP-123's
pre-resolved decision (E):

* ``breach_evaluation_cadence_seconds`` — how often the breach loop wakes.
* ``greeks_refresh_interval_minutes`` — scheduled greeks refresh cadence
  (per-position).
* ``greeks_refresh_underlying_move_threshold_pct`` — underlying move that
  triggers an out-of-cadence greeks refresh.
* ``greeks_refresh_inspection_cadence_seconds`` — how often the refresh
  task wakes to evaluate triggers and consider due positions (independent
  of the per-position scheduled interval above). Added by story 03a.
* ``bracket_stop_evaluation_cadence_seconds`` — how often the options
  bracket-stop watcher wakes to evaluate price-based + P/L-based triggers
  against the underlying-price cache and greeks. Added by story 04c.
* ``entry_window_evaluation_cadence_seconds`` — how often the entry-window
  watcher wakes to auto-cancel ``PENDING_ENTRY`` brackets past their
  ``entry_window_deadline``. A patient-entry deadline is coarse (hours), so the
  default is far slower than the price-driven bracket-stop cadence. Added by
  ALP-737.
* ``entry_window_max_reprices`` — how many times the entry-window watcher may
  reprice/escalate a still-resting patient-retest limit toward the market
  before giving up and cancelling it (ALP-740). Bounds the reprice loop so an
  unfilled entry cannot chase the market indefinitely; ``0`` disables repricing
  and restores ALP-737's plain cancel-at-deadline behaviour.
* ``underlying_stream_provider`` — single-value ``Literal`` today; story 02b
  may extend the union if a second provider gets validated.
* ``subscription_refresh_seconds`` — cadence at which the underlying-price
  stream (story 02b) diffs its target subscription set against the
  open-position set and issues add / remove deltas.
* ``max_reconnect_attempts`` — websocket-reconnect ceiling per session.
* ``supervisor_shutdown_timeout_seconds`` — per-task cancellation budget the
  ``MonitorSupervisor`` enforces at shutdown (scope section 7 default = 5s).
* ``control_port`` — loopback TCP port the FastAPI ``/control`` + ``/events``
  surface (story 01c / ALP-665) binds to. Defaults to ``8766`` per the
  story; operator may override per-environment.
* ``breach_loop_consecutive_failure_alert_threshold`` — number of consecutive
  failed breach-loop ticks before the loop emits a degraded health signal
  (ALP-732). Reset on the first successful tick, so a silently-failing breach
  loop becomes visible instead of looking healthy on the process surfaces.
* ``fill_backfill_interval_seconds`` — cadence at which the periodic
  fill-backfill backstop (ALP-763) sweeps Alpaca for fills missing from
  ``fill_records`` (independent of any websocket reconnect).
* ``fill_backfill_lookback_seconds`` — independent, generous ``since`` lookback
  bound for each backfill sweep — wide enough to re-capture a fill dropped
  earlier in the swing-trading horizon.
* ``unattributed_fill_escalation_ttl_seconds`` — seconds after ``first_seen_at``
  before an unresolved unattributed fill emits a one-shot terminal ERROR
  escalation (ALP-771).
* ``fill_stream_stale_timeout_seconds`` — seconds of trade_updates silence
  during RTH before the fill-stream consumer forces a reconnect (ALP-819),
  catching the library-internal-reconnect path the ALP-768 sentinel misses.

This file is loaded directly by ``alphamind.config.load`` and surfaced on
``ResolvedConfig.continuous_monitor``; the resolver does not cascade it
through profile / regime / overlay multipliers — these are runtime knobs, not
risk-rule values.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from alphamind.config.models._shared import validate_hh_mm


class ContinuousMonitorConfig(BaseModel):
    """Operator-tunable knobs for the continuous monitor process."""

    model_config = ConfigDict(frozen=True, strict=True)

    breach_evaluation_cadence_seconds: int = Field(ge=1)
    greeks_refresh_interval_minutes: int = Field(ge=1)
    greeks_refresh_underlying_move_threshold_pct: float = Field(gt=0.0)
    greeks_refresh_inspection_cadence_seconds: int = Field(default=30, ge=1)
    bracket_stop_evaluation_cadence_seconds: float = Field(default=1.0, gt=0.0)
    entry_window_evaluation_cadence_seconds: float = Field(default=60.0, gt=0.0)
    entry_window_max_reprices: int = Field(
        default=2,
        ge=0,
        description=(
            "Maximum times the entry-window watcher reprices/escalates a still-"
            "resting patient-retest limit toward the market before cancelling it "
            "(ALP-740). 0 disables repricing (ALP-737 cancel-at-deadline)."
        ),
    )
    underlying_stream_provider: Literal["alpaca-iex"]
    subscription_refresh_seconds: int = Field(default=30, ge=1)
    max_reconnect_attempts: int = Field(ge=1)
    supervisor_shutdown_timeout_seconds: int = Field(ge=1)
    control_port: int = Field(default=8766, ge=1, le=65535)
    breach_loop_consecutive_failure_alert_threshold: int = Field(
        default=3,
        ge=1,
        description=(
            "Consecutive failed breach-loop ticks before a degraded operator "
            "health signal fires; cleared on the first successful tick (ALP-732)."
        ),
    )
    fill_backfill_interval_seconds: int = Field(
        default=900,
        ge=1,
        description=(
            "Cadence (seconds) of the periodic fill-backfill backstop (ALP-763). "
            "Each sweep recovers any Alpaca fill missing from fill_records, "
            "independent of a websocket reconnect. 15 min keeps a dropped entry "
            "fill's position/cash divergence short-lived without hammering the "
            "broker's GET /v2/orders surface."
        ),
    )
    fill_backfill_lookback_seconds: int = Field(
        default=259_200,
        ge=1,
        description=(
            "Independent lookback (seconds) for each backfill sweep's `since` "
            "bound (ALP-763). NOT max(fill_timestamp) — that permanently excludes "
            "an earlier dropped fill once a later one lands. 72h spans the upper "
            "end of the 4-72h swing-trading horizon, so a fill dropped any time "
            "earlier in a position's life is still in-window. append_fill_record's "
            "dedupe makes re-feeding already-persisted fills a no-op, so a wide "
            "window costs only redundant reads."
        ),
    )
    unattributed_fill_escalation_ttl_seconds: int = Field(
        default=1800,
        ge=1,
        description=(
            "Seconds after ``first_seen_at`` before an unresolved unattributed "
            "fill emits a one-shot terminal ERROR escalation (ALP-771). The "
            "escalation fires once per fill via the ``escalated`` flag and does "
            "not re-fire on subsequent drains. Default 1800s (30 min) = 2x the "
            "15-min backfill interval, giving one full drain-cycle grace period "
            "before going loud."
        ),
    )
    borrow_accrual_tick_local_time: str = Field(
        default="16:00",
        description=(
            "Trading-day local time (HH:MM, US/Eastern) at which the "
            "borrow-accrual tick fires. See ALP-715 pre-resolved decision (E). "
            "Same HH:MM format as ``SessionWindow.open``/``.close`` in venue.yaml."
        ),
    )
    watchdog_stall_timeout_seconds: int = Field(
        default=3600,
        ge=60,
        description=(
            "Seconds without a heartbeat before the in-process watchdog "
            "forces ``os._exit(1)`` so NSSM restarts the monitor (ALP-768). "
            "Only tasks that call ``MonitorSupervisor.beat(name)`` are watched; "
            "tasks that never beat are ignored. Set conservatively — periodic "
            "tasks (breach loop, greeks refresh) should beat at every tick; "
            "the timeout must exceed the longest legitimate inter-tick gap."
        ),
    )
    fill_stream_stale_timeout_seconds: int = Field(
        default=900,
        ge=1,
        description=(
            "Seconds of trade_updates silence during RTH before the fill-stream "
            "consumer forces a reconnect (ALP-819). The websockets library "
            "reconnects internally on a transport error (WinError 121) without "
            "raising or delivering frames, so the ALP-768 sentinel never fires "
            "and the consumer starves on queue.get(). On silence past this "
            "bound the stream is torn down and rebuilt; REST recovery on each "
            "reconnect (plus the fill-backfill backstop) re-captures any gap, "
            "so a needless reconnect during a quiet-but-healthy session is "
            "cheap. Must exceed the longest legitimate RTH gap between fills; "
            "900s (15 min) matches the fill-backfill interval."
        ),
    )

    @field_validator("borrow_accrual_tick_local_time")
    @classmethod
    def _hh_mm_well_formed(cls, value: str) -> str:
        return validate_hh_mm(value)

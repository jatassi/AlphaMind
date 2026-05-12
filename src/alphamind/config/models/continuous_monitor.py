"""Pydantic model for ``config/continuous_monitor.yaml`` (ALP-432 story 01).

The continuous monitor's Class A tunables per parent issue ALP-123's
pre-resolved decision (E):

* ``breach_evaluation_cadence_seconds`` — how often the breach loop wakes.
* ``greeks_refresh_interval_minutes`` — scheduled greeks refresh cadence.
* ``greeks_refresh_underlying_move_threshold_pct`` — underlying move that
  triggers an out-of-cadence greeks refresh.
* ``underlying_stream_provider`` — single-value ``Literal`` today; story 02b
  may extend the union if a second provider gets validated.
* ``subscription_refresh_seconds`` — cadence at which the underlying-price
  stream (story 02b) diffs its target subscription set against the
  open-position set and issues add / remove deltas.
* ``max_reconnect_attempts`` — websocket-reconnect ceiling per session.
* ``supervisor_shutdown_timeout_seconds`` — per-task cancellation budget the
  ``MonitorSupervisor`` enforces at shutdown (scope section 7 default = 5s).

This file is loaded directly by ``alphamind.config.load`` and surfaced on
``ResolvedConfig.continuous_monitor``; the resolver does not cascade it
through profile / regime / overlay multipliers — these are runtime knobs, not
risk-rule values.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ContinuousMonitorConfig(BaseModel):
    """Operator-tunable knobs for the continuous monitor process."""

    model_config = ConfigDict(frozen=True, strict=True)

    breach_evaluation_cadence_seconds: int = Field(ge=1)
    greeks_refresh_interval_minutes: int = Field(ge=1)
    greeks_refresh_underlying_move_threshold_pct: float = Field(gt=0.0)
    underlying_stream_provider: Literal["alpaca-iex"]
    subscription_refresh_seconds: int = Field(default=30, ge=1)
    max_reconnect_attempts: int = Field(ge=1)
    supervisor_shutdown_timeout_seconds: int = Field(ge=1)

"""Portfolio state package — raw portfolio data assembled per invocation."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

from alphamind.config.loaders import read_yaml_file


@dataclass(frozen=True, slots=True)
class PortfolioStateConfig:
    """Configuration for the portfolio-state snapshot assembler."""

    pm_decision_log_sliding_window_invocations: int
    thesis_resolutions_lookback_trading_days: int
    thesis_quality_aggregates_trailing_windows_days: tuple[int, ...]
    snapshot_freshness_max_phase1_to_snapshot_seconds: float
    snapshot_freshness_max_price_age_seconds: float

    def __post_init__(self) -> None:
        if self.pm_decision_log_sliding_window_invocations <= 0:
            msg = (
                "pm_decision_log_sliding_window_invocations must be > 0; "
                f"got {self.pm_decision_log_sliding_window_invocations}"
            )
            raise ValueError(msg)
        if self.thesis_resolutions_lookback_trading_days <= 0:
            msg = (
                "thesis_resolutions_lookback_trading_days must be > 0; "
                f"got {self.thesis_resolutions_lookback_trading_days}"
            )
            raise ValueError(msg)
        if self.snapshot_freshness_max_phase1_to_snapshot_seconds <= 0:
            msg = (
                "snapshot_freshness_max_phase1_to_snapshot_seconds must be > 0; "
                f"got {self.snapshot_freshness_max_phase1_to_snapshot_seconds}"
            )
            raise ValueError(msg)
        if self.snapshot_freshness_max_price_age_seconds <= 0:
            msg = (
                "snapshot_freshness_max_price_age_seconds must be > 0; "
                f"got {self.snapshot_freshness_max_price_age_seconds}"
            )
            raise ValueError(msg)
        if not self.thesis_quality_aggregates_trailing_windows_days:
            msg = "thesis_quality_aggregates_trailing_windows_days must be non-empty"
            raise ValueError(msg)
        if any(d <= 0 for d in self.thesis_quality_aggregates_trailing_windows_days):
            msg = "all thesis_quality_aggregates_trailing_windows_days elements must be positive"
            raise ValueError(msg)


def _flatten_portfolio_state_yaml(raw: dict[str, Any]) -> dict[str, Any]:
    """Flatten the nested YAML structure into the flat field-name keys."""
    ps: dict[str, Any] = raw["portfolio_state"]
    pm: dict[str, Any] = ps["pm_decision_log"]
    tr: dict[str, Any] = ps["thesis_resolutions"]
    tq: dict[str, Any] = ps["thesis_quality_aggregates"]
    sf: dict[str, Any] = ps["snapshot_freshness"]
    return {
        "pm_decision_log_sliding_window_invocations": pm["sliding_window_invocations"],
        "thesis_resolutions_lookback_trading_days": tr["lookback_trading_days"],
        "thesis_quality_aggregates_trailing_windows_days": tuple(tq["trailing_windows_days"]),
        "snapshot_freshness_max_phase1_to_snapshot_seconds": sf["max_phase1_to_snapshot_seconds"],
        "snapshot_freshness_max_price_age_seconds": sf["max_price_age_seconds"],
    }


def load_portfolio_state_config(path: pathlib.Path) -> PortfolioStateConfig:
    """Parse *path* as YAML and return a validated :class:`PortfolioStateConfig`."""
    return PortfolioStateConfig(**_flatten_portfolio_state_yaml(read_yaml_file(path)))

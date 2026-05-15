"""Tests for ``ContinuousMonitorConfig`` Pydantic model + YAML resolution (story 01).

The model covers the Class A tunables documented in parent issue ALP-123's
pre-resolved decision (E):

* ``breach_evaluation_cadence_seconds: 60``
* ``greeks_refresh_interval_minutes: 15``
* ``greeks_refresh_underlying_move_threshold_pct: 2.0``
* ``underlying_stream_provider: alpaca-iex``
* ``max_reconnect_attempts: 5``
* ``supervisor_shutdown_timeout_seconds: 5`` (per scope section 7 default)

The shipped ``config/continuous_monitor.yaml`` must parse cleanly via the
pipeline's resolved-config loader so the values surface on
``resolved_config.continuous_monitor``.
"""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from alphamind.config import load_full_config
from alphamind.config.models import Mode, Regime, RuntimeDimensions, RunType
from alphamind.config.models.continuous_monitor import ContinuousMonitorConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"
TODAY = date(2026, 4, 27)

_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)


def _placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{k}=placeholder" for k in _VENUE_ENV_KEYS) + "\n")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture
def shipped_runtime() -> RuntimeDimensions:
    return RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.pre_open,
    )


# ---------------------------------------------------------------------------
# Model — parse-time validation
# ---------------------------------------------------------------------------


class TestContinuousMonitorConfigModel:
    def _valid_payload(self) -> dict[str, object]:
        return {
            "breach_evaluation_cadence_seconds": 60,
            "greeks_refresh_interval_minutes": 15,
            "greeks_refresh_underlying_move_threshold_pct": 2.0,
            "greeks_refresh_inspection_cadence_seconds": 30,
            "underlying_stream_provider": "alpaca-iex",
            "subscription_refresh_seconds": 30,
            "max_reconnect_attempts": 5,
            "supervisor_shutdown_timeout_seconds": 5,
        }

    def test_valid_payload_constructs(self) -> None:
        cfg = ContinuousMonitorConfig(**self._valid_payload())  # type: ignore[arg-type]
        assert cfg.breach_evaluation_cadence_seconds == 60
        assert cfg.greeks_refresh_interval_minutes == 15
        assert cfg.greeks_refresh_underlying_move_threshold_pct == 2.0
        assert cfg.greeks_refresh_inspection_cadence_seconds == 30
        assert cfg.underlying_stream_provider == "alpaca-iex"
        assert cfg.subscription_refresh_seconds == 30
        assert cfg.max_reconnect_attempts == 5
        assert cfg.supervisor_shutdown_timeout_seconds == 5

    def test_inspection_cadence_defaults_to_30_when_absent(self) -> None:
        """Story 03a adds ``greeks_refresh_inspection_cadence_seconds`` with a
        default so existing YAML files continue to parse without explicit edits."""
        payload = self._valid_payload()
        del payload["greeks_refresh_inspection_cadence_seconds"]
        cfg = ContinuousMonitorConfig(**payload)  # type: ignore[arg-type]
        assert cfg.greeks_refresh_inspection_cadence_seconds == 30

    def test_model_is_frozen(self) -> None:
        cfg = ContinuousMonitorConfig(**self._valid_payload())  # type: ignore[arg-type]
        with pytest.raises((ValueError, TypeError)):
            cfg.breach_evaluation_cadence_seconds = 30

    @pytest.mark.parametrize(
        "field",
        [
            "breach_evaluation_cadence_seconds",
            "greeks_refresh_interval_minutes",
            "greeks_refresh_inspection_cadence_seconds",
            "max_reconnect_attempts",
            "supervisor_shutdown_timeout_seconds",
            "subscription_refresh_seconds",
        ],
    )
    def test_non_positive_int_fields_rejected(self, field: str) -> None:
        payload = self._valid_payload()
        payload[field] = 0
        with pytest.raises((ValueError, TypeError)):
            ContinuousMonitorConfig(**payload)  # type: ignore[arg-type]

    def test_non_positive_move_threshold_rejected(self) -> None:
        payload = self._valid_payload()
        payload["greeks_refresh_underlying_move_threshold_pct"] = 0.0
        with pytest.raises((ValueError, TypeError)):
            ContinuousMonitorConfig(**payload)  # type: ignore[arg-type]

    def test_underlying_stream_provider_must_be_alpaca_iex(self) -> None:
        payload = self._valid_payload()
        payload["underlying_stream_provider"] = "polygon-stream"
        with pytest.raises((ValueError, TypeError)):
            ContinuousMonitorConfig(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# YAML wiring — the shipped config exposes the values on the resolved snapshot
# ---------------------------------------------------------------------------


class TestContinuousMonitorYamlWiring:
    def test_shipped_yaml_parses_and_surfaces_on_resolved_config(
        self,
        env_path: Path,
        archive_root: Path,
        shipped_runtime: RuntimeDimensions,
    ) -> None:
        loaded = load_full_config(
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id="inv-continuous-monitor-001",
            runtime=shipped_runtime,
            today=TODAY,
        )
        cm = loaded.resolved.continuous_monitor
        assert isinstance(cm, ContinuousMonitorConfig)
        # The decision-(E) defaults are baked into the shipped YAML.
        assert cm.breach_evaluation_cadence_seconds == 60
        assert cm.greeks_refresh_interval_minutes == 15
        assert cm.greeks_refresh_underlying_move_threshold_pct == 2.0
        assert cm.greeks_refresh_inspection_cadence_seconds == 30
        assert cm.underlying_stream_provider == "alpaca-iex"
        assert cm.subscription_refresh_seconds == 30
        assert cm.max_reconnect_attempts == 5

    def test_malformed_yaml_raises_validation_error(
        self,
        env_path: Path,
        archive_root: Path,
        shipped_runtime: RuntimeDimensions,
        tmp_path: Path,
    ) -> None:
        """A non-positive cadence in the YAML trips the model's validator."""
        fixture_config = tmp_path / "config"
        shutil.copytree(SHIPPED_CONFIG_DIR, fixture_config)
        cm_yaml = fixture_config / "continuous_monitor.yaml"
        payload = cm_yaml.read_text()
        broken = payload.replace(
            "breach_evaluation_cadence_seconds: 60",
            "breach_evaluation_cadence_seconds: 0",
        )
        assert broken != payload
        cm_yaml.write_text(broken)

        with pytest.raises((ValueError, TypeError)):
            load_full_config(
                config_dir=fixture_config,
                env_path=env_path,
                archive_root=archive_root,
                invocation_id="inv-continuous-monitor-bad",
                runtime=shipped_runtime,
                today=TODAY,
            )

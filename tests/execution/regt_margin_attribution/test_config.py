"""Tests for RegT margin attribution package skeleton and config (ALP-421)."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest
import yaml

_CONFIG_PATH = pathlib.Path(__file__).parents[3] / "config" / "regt_margin_attribution.yaml"

_MINIMUM_ETF_SYMBOLS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "VTI",
    "VOO",
    "XLF",
    "XLE",
    "XLK",
    "XLV",
    "XLY",
    "XLP",
    "XLU",
    "XLI",
    "XLB",
    "XLRE",
    "XLC",
}

_TOP_SP500_SYMBOLS = {
    "AAPL",
    "MSFT",
    "NVDA",
    "GOOGL",
    "AMZN",
    "META",
    "BRK.B",
    "TSLA",
    "JPM",
    "UNH",
}


def _minimal_yaml_payload() -> dict[str, Any]:
    return {
        "pm_model_version": "ibkr_mirror_v1_2026Q2",
        "risk_free_rate_annual": 0.0425,
        "iv_shock": {
            "worst_down_multiplier": 1.20,
            "worst_up_multiplier": 0.95,
        },
        "shock_parameters": {
            "high_cap_equity": 0.15,
            "small_cap_equity": 0.20,
            "unmapped_default": 0.20,
            "per_symbol_overrides": {
                "SPY": 0.15,
            },
        },
    }


def test_config_round_trips_minimal_yaml(tmp_path: pathlib.Path) -> None:
    """Minimal valid yaml round-trips through the loader with correct field values."""
    from alphamind.execution.regt_margin_attribution import (
        RegTMarginAttributionConfig,
        load_regt_margin_attribution_config,
    )

    payload = _minimal_yaml_payload()
    yaml_file = tmp_path / "regt_margin_attribution.yaml"
    yaml_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    cfg = load_regt_margin_attribution_config(yaml_file)

    assert isinstance(cfg, RegTMarginAttributionConfig)
    assert cfg.pm_model_version == "ibkr_mirror_v1_2026Q2"
    assert cfg.risk_free_rate_annual == pytest.approx(0.0425)
    assert cfg.iv_shock.worst_down_multiplier == pytest.approx(1.20)
    assert cfg.iv_shock.worst_up_multiplier == pytest.approx(0.95)
    assert cfg.shock_parameters.high_cap_equity == pytest.approx(0.15)
    assert cfg.shock_parameters.small_cap_equity == pytest.approx(0.20)
    assert cfg.shock_parameters.unmapped_default == pytest.approx(0.20)
    assert cfg.shock_parameters.per_symbol_overrides["SPY"] == pytest.approx(0.15)


def test_config_rejects_non_finite_risk_free_rate(tmp_path: pathlib.Path) -> None:
    """risk_free_rate_annual of .inf raises ValidationError."""
    from alphamind.execution.regt_margin_attribution import load_regt_margin_attribution_config

    payload = _minimal_yaml_payload()
    payload["risk_free_rate_annual"] = float("inf")
    yaml_file = tmp_path / "regt.yaml"
    # yaml.safe_dump doesn't handle inf natively, write manually
    yaml_file.write_text(
        "pm_model_version: ibkr_mirror_v1_2026Q2\n"
        "risk_free_rate_annual: .inf\n"
        "iv_shock:\n"
        "  worst_down_multiplier: 1.20\n"
        "  worst_up_multiplier: 0.95\n"
        "shock_parameters:\n"
        "  high_cap_equity: 0.15\n"
        "  small_cap_equity: 0.20\n"
        "  unmapped_default: 0.20\n"
        "  per_symbol_overrides:\n"
        "    SPY: 0.15\n",
        encoding="utf-8",
    )

    with pytest.raises((ValueError, TypeError)):
        load_regt_margin_attribution_config(yaml_file)


def test_config_rejects_invalid_iv_shock_direction(tmp_path: pathlib.Path) -> None:
    """worst_down_multiplier < 1.0 raises ValidationError (IV must rise on down-shock)."""
    from alphamind.execution.regt_margin_attribution import load_regt_margin_attribution_config

    payload = _minimal_yaml_payload()
    payload["iv_shock"]["worst_down_multiplier"] = 0.9  # invalid: must be >= 1.0
    yaml_file = tmp_path / "regt.yaml"
    yaml_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises((ValueError, TypeError)):
        load_regt_margin_attribution_config(yaml_file)


def test_config_rejects_invalid_iv_shock_up_direction(tmp_path: pathlib.Path) -> None:
    """worst_up_multiplier > 1.0 raises ValidationError (IV must fall or hold on up-shock)."""
    from alphamind.execution.regt_margin_attribution import load_regt_margin_attribution_config

    payload = _minimal_yaml_payload()
    payload["iv_shock"]["worst_up_multiplier"] = 1.1  # invalid: must be <= 1.0
    yaml_file = tmp_path / "regt.yaml"
    yaml_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises((ValueError, TypeError)):
        load_regt_margin_attribution_config(yaml_file)


def test_config_rejects_shock_outside_unit_interval(tmp_path: pathlib.Path) -> None:
    """Shock percentage >= 1.0 raises ValidationError (must be in (0.0, 1.0))."""
    from alphamind.execution.regt_margin_attribution import load_regt_margin_attribution_config

    payload = _minimal_yaml_payload()
    payload["shock_parameters"]["high_cap_equity"] = 1.5
    yaml_file = tmp_path / "regt.yaml"
    yaml_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    with pytest.raises((ValueError, TypeError)):
        load_regt_margin_attribution_config(yaml_file)


def test_config_normalises_per_symbol_override_keys_to_uppercase(tmp_path: pathlib.Path) -> None:
    """Per-symbol override keys are normalised to upper-case on construction."""
    from alphamind.execution.regt_margin_attribution import load_regt_margin_attribution_config

    payload = _minimal_yaml_payload()
    # Use lowercase keys in the yaml
    payload["shock_parameters"]["per_symbol_overrides"] = {
        "spy": 0.15,
        "qqq": 0.15,
    }
    yaml_file = tmp_path / "regt.yaml"
    yaml_file.write_text(yaml.safe_dump(payload), encoding="utf-8")

    cfg = load_regt_margin_attribution_config(yaml_file)

    assert "SPY" in cfg.shock_parameters.per_symbol_overrides
    assert "QQQ" in cfg.shock_parameters.per_symbol_overrides
    assert "spy" not in cfg.shock_parameters.per_symbol_overrides
    assert "qqq" not in cfg.shock_parameters.per_symbol_overrides


def test_default_path_yaml_loads_cleanly() -> None:
    """load_regt_margin_attribution_config() with no args returns the v1 snapshot model."""
    from alphamind.execution.regt_margin_attribution import load_regt_margin_attribution_config

    cfg = load_regt_margin_attribution_config()

    assert cfg.pm_model_version == "occ_tims_v1_2026Q2"

    overrides = cfg.shock_parameters.per_symbol_overrides
    # All major ETF + sector SPDR symbols must be present
    missing = _MINIMUM_ETF_SYMBOLS - set(overrides.keys())
    assert not missing, f"Missing ETF symbols in per_symbol_overrides: {missing}"

    # At least 5 of the top S&P 500 symbols must be present
    present_sp500 = _TOP_SP500_SYMBOLS & set(overrides.keys())
    assert len(present_sp500) >= 5, (
        f"Expected at least 5 top S&P 500 symbols, found {len(present_sp500)}: {present_sp500}"
    )

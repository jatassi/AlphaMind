"""Tests for flag_event_types.py — ALP-908.

Covers:
- Forward lookup for all 21 canonical prefixes
- Dynamic-suffix stripping (colon-delimited ticker / pair-key)
- UnregisteredAnomalyFlagError for unknown names
- flag_keys_for_class partitioning
- all_threshold_classes / known_flag_keys set semantics
- Config-gated threshold_key cross-check against DistillationConfig fields
- DistillationAnomalyFlagDetail construction from resolved taxonomy
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from alphamind.distillation.flag_event_types import (
    FlagTaxonomy,
    UnregisteredAnomalyFlagError,
    all_threshold_classes,
    flag_keys_for_class,
    known_flag_keys,
    resolve_flag_taxonomy,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Canonical prefix → expected taxonomy (B = config-gated, C = structural)
# ---------------------------------------------------------------------------

_TABLE_B: list[tuple[str, str, str]] = [
    ("volume_anomaly", "anomaly_detection", "volume_anomaly_sigma"),
    ("price_move_anomaly", "anomaly_detection", "price_move_atr_multiple"),
    ("macro_surprise_anomaly", "anomaly_detection", "macro_surprise_percentile"),
    ("funding_stress_alert", "anomaly_detection", "funding_stress_component_alert_count"),
    ("market_liquidity_alert", "anomaly_detection", "market_liquidity_alert_percentile"),
    ("news_price_divergence", "anomaly_detection", "news_price_divergence_window_hours"),
    (
        "intra_sector_correlation_divergence",
        "narrative_lag",
        "narrative_lag_correlation_shift_sigma",
    ),
    ("correlation_breakdown_flag", "narrative_lag", "correlation_breakdown_sigma"),
    ("dispersion_shift_flag", "narrative_lag", "narrative_lag_correlation_shift_sigma"),
    ("narrative_lag_flag", "narrative_lag", "narrative_lag_media_silence_hours"),
    ("correlation_locus_flag", "narrative_lag", "correlation_locus_pair_count_threshold"),
    ("overdue_lag_flag", "lead_lag", "lead_lag_overdue_lead_sigma"),
    ("lead_lag_inversion_flag", "lead_lag", "lead_lag_overdue_lead_sigma"),
    ("prediction_market_delta", "prediction_market", "prediction_market_delta_pp_threshold"),
]

_TABLE_C: list[tuple[str, str, str]] = [
    ("pair_trade_signature", "options_flow", "pair_trade_signature"),
    ("sector_wide_sweep", "options_flow", "sector_wide_sweep"),
    ("gold_real_yields_divergence", "intermarket_regime", "gold_real_yields_divergence"),
    ("oil_xle_beta_drift", "intermarket_regime", "oil_xle_beta_drift"),
    ("vix_spy_divergence", "intermarket_regime", "vix_spy_divergence"),
    ("q12_event_novelty", "corporate_actions", "q12_event_novelty"),
    ("etf_vs_single_name_divergence", "corporate_actions", "etf_vs_single_name_divergence"),
]

_ALL_ENTRIES = _TABLE_B + _TABLE_C

_STRUCTURAL_CLASSES = {"options_flow", "intermarket_regime", "corporate_actions"}
_CONFIG_CLASSES = {"anomaly_detection", "narrative_lag", "lead_lag", "prediction_market"}


# ---------------------------------------------------------------------------
# 1. Forward lookup — all 21 canonical prefixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("flag_name,threshold_class,threshold_key", _ALL_ENTRIES)
def test_resolve_flag_taxonomy_canonical(
    flag_name: str, threshold_class: str, threshold_key: str
) -> None:
    result = resolve_flag_taxonomy(flag_name)
    assert isinstance(result, FlagTaxonomy)
    assert result.threshold_class == threshold_class
    assert result.threshold_key == threshold_key


# ---------------------------------------------------------------------------
# 2. Dynamic-suffix stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "full_name,expected_prefix",
    [
        ("correlation_breakdown_flag:AAPL:MSFT", "correlation_breakdown_flag"),
        ("overdue_lag_flag:semis_to_tech", "overdue_lag_flag"),
        ("correlation_locus_flag:NVDA", "correlation_locus_flag"),
        ("intra_sector_correlation_divergence:SPY:QQQ", "intra_sector_correlation_divergence"),
        ("lead_lag_inversion_flag:credit_to_equity", "lead_lag_inversion_flag"),
        # ALP-934 q12 per-subject suffixes: ETF symbol and sector audience.
        ("etf_vs_single_name_divergence:SPY", "etf_vs_single_name_divergence"),
        ("q12_event_novelty:sector_financials", "q12_event_novelty"),
    ],
)
def test_resolve_strips_colon_suffix(full_name: str, expected_prefix: str) -> None:
    expected = resolve_flag_taxonomy(expected_prefix)
    assert resolve_flag_taxonomy(full_name) == expected


# ---------------------------------------------------------------------------
# 3. Unregistered name raises UnregisteredAnomalyFlagError
# ---------------------------------------------------------------------------


def test_resolve_unknown_raises() -> None:
    with pytest.raises(UnregisteredAnomalyFlagError):
        resolve_flag_taxonomy("nonexistent_flag")


def test_unregistered_error_is_value_error() -> None:
    with pytest.raises(ValueError):
        resolve_flag_taxonomy("nonexistent_flag")


def test_resolve_unknown_with_colon_suffix_raises() -> None:
    with pytest.raises(UnregisteredAnomalyFlagError):
        resolve_flag_taxonomy("totally_unknown_flag:AAPL")


# ---------------------------------------------------------------------------
# 4. flag_keys_for_class — partition property
# ---------------------------------------------------------------------------


def test_flag_keys_for_class_union_equals_known_flag_keys() -> None:
    classes = all_threshold_classes()
    union: set[str] = set()
    for cls in classes:
        keys = flag_keys_for_class(cls)
        union.update(keys)
    assert frozenset(union) == known_flag_keys()


def test_flag_keys_for_class_no_overlap_between_classes() -> None:
    classes = all_threshold_classes()
    seen: set[str] = set()
    for cls in classes:
        keys = set(flag_keys_for_class(cls))
        overlap = seen & keys
        assert not overlap, f"Keys {overlap!r} appear in multiple classes"
        seen |= keys


def test_flag_keys_for_class_returns_sorted_tuple() -> None:
    for cls in all_threshold_classes():
        keys = flag_keys_for_class(cls)
        assert isinstance(keys, tuple)
        assert list(keys) == sorted(keys)


def test_flag_keys_for_class_unknown_class_raises_key_error() -> None:
    with pytest.raises(KeyError):
        flag_keys_for_class("nonexistent_class")


# ---------------------------------------------------------------------------
# 5. all_threshold_classes and known_flag_keys
# ---------------------------------------------------------------------------


def test_all_threshold_classes_contains_expected() -> None:
    expected = _CONFIG_CLASSES | _STRUCTURAL_CLASSES
    assert all_threshold_classes() == expected


def test_known_flag_keys_contains_all_prefixes() -> None:
    expected_prefixes = frozenset(name for name, _, _ in _ALL_ENTRIES)
    assert known_flag_keys() == expected_prefixes


# ---------------------------------------------------------------------------
# 6. Config-gated threshold_key cross-check
# ---------------------------------------------------------------------------


def test_config_gated_threshold_keys_exist_on_pydantic_model() -> None:
    """Every table-B threshold_key must be a field of the corresponding section model."""
    yaml_path = _REPO_ROOT / "config" / "distillation.yaml"
    parsed = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))

    from alphamind.config.models.distillation import DistillationConfig

    cfg = DistillationConfig.model_validate(parsed)

    for flag_name, threshold_class, threshold_key in _TABLE_B:
        section = getattr(cfg, threshold_class)
        assert hasattr(section, threshold_key), (
            f"Flag {flag_name!r}: threshold_key {threshold_key!r} is not a field "
            f"of DistillationConfig.{threshold_class}"
        )
        # Also confirm the attribute is accessible (not None/missing)
        _ = getattr(section, threshold_key)


# ---------------------------------------------------------------------------
# 7. DistillationAnomalyFlagDetail from resolved taxonomy passes validators
# ---------------------------------------------------------------------------


def test_resolved_taxonomy_satisfies_detail_contract() -> None:
    """A detail built from any resolved FlagTaxonomy passes 02e's non-empty validators."""
    from alphamind._kernel.calibration import CalibrationState
    from alphamind.portfolio_state.events.distillation_anomaly import (
        DistillationAnomalyFlagDetail,
    )

    for flag_name, _, _ in _ALL_ENTRIES:
        taxonomy = resolve_flag_taxonomy(flag_name)
        detail = DistillationAnomalyFlagDetail(
            threshold_class=taxonomy.threshold_class,
            threshold_key=taxonomy.threshold_key,
            magnitude=1.5,
            severity="investigate_now",
            ticker=None,
            calibration_state=CalibrationState.CALIBRATED,
            block_id=f"q1.{flag_name}",
        )
        # __post_init__ validated — non-empty fields are fine
        assert detail.threshold_class == taxonomy.threshold_class
        assert detail.threshold_key == taxonomy.threshold_key

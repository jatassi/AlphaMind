"""Distillation anomaly-flag threshold-taxonomy registry (ALP-908).

Single source of truth mapping every :class:`~alphamind.distillation.output.AnomalyFlag`
``name`` to its ``(threshold_class, threshold_key)`` taxonomy.

**Public API**

- :class:`FlagTaxonomy` — NamedTuple returned by the forward lookup.
- :exc:`UnregisteredAnomalyFlagError` — raised by :func:`resolve_flag_taxonomy` when
  the canonical prefix is not in the registry.
- :func:`resolve_flag_taxonomy` — forward lookup used by the 04b emission hook.
- :func:`flag_keys_for_class` — reverse view used by the ALP-96 flag-rate reporter.
- :func:`all_threshold_classes` — full class set.
- :func:`known_flag_keys` — full canonical-prefix set.

**Purity invariant**: no I/O, no SQLAlchemy, no config load at import time.
All four exported functions derive from the module-level private dict ``_REGISTRY``.
"""

from __future__ import annotations

from typing import NamedTuple

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class FlagTaxonomy(NamedTuple):
    """Taxonomy pair for one anomaly flag.

    Field names mirror :class:`~alphamind.portfolio_state.events.distillation_anomaly.
    DistillationAnomalyFlagDetail` exactly so the 04b emission hook can unpack
    directly.

    - ``threshold_class`` — the indicator-family / config-section label
      (e.g. ``"anomaly_detection"``, ``"options_flow"``).
    - ``threshold_key`` — the specific gate key within that class
      (e.g. ``"volume_anomaly_sigma"`` for config-gated flags, or the flag's
      own canonical name for structural flags).
    """

    threshold_class: str
    threshold_key: str


class UnregisteredAnomalyFlagError(ValueError):
    """Raised when :func:`resolve_flag_taxonomy` cannot find the canonical prefix.

    Inherits :exc:`ValueError` so callers that catch ``ValueError`` continue to
    work, but the specific subclass lets emission hooks distinguish this case.
    """


# ---------------------------------------------------------------------------
# Authoritative mapping
# ---------------------------------------------------------------------------
#
# (A) Config-gated flags — threshold_class = config section name,
#     threshold_key = the distillation.yaml leaf that gates firing.
# (B) Structural flags — threshold_key = the flag's own canonical name
#     (no tunable YAML leaf; fires on a hardcoded module constant).
#
# Source-of-truth tables are in the ALP-908 issue body (§ Scope B and C).
# Do NOT add numeric literals here — they trip the no-magic-numbers guard
# (tests/distillation/test_no_magic_numbers.py).

_REGISTRY: dict[str, FlagTaxonomy] = {
    "volume_anomaly": FlagTaxonomy("anomaly_detection", "volume_anomaly_sigma"),
    "price_move_anomaly": FlagTaxonomy("anomaly_detection", "price_move_atr_multiple"),
    "macro_surprise_anomaly": FlagTaxonomy("anomaly_detection", "macro_surprise_percentile"),
    "funding_stress_alert": FlagTaxonomy(
        "anomaly_detection", "funding_stress_component_alert_count"
    ),
    "market_liquidity_alert": FlagTaxonomy(
        "anomaly_detection", "market_liquidity_alert_percentile"
    ),
    "news_price_divergence": FlagTaxonomy(
        "anomaly_detection", "news_price_divergence_window_hours"
    ),
    "intra_sector_correlation_divergence": FlagTaxonomy(
        "narrative_lag", "narrative_lag_correlation_shift_sigma"
    ),
    "correlation_breakdown_flag": FlagTaxonomy("narrative_lag", "correlation_breakdown_sigma"),
    "dispersion_shift_flag": FlagTaxonomy("narrative_lag", "narrative_lag_correlation_shift_sigma"),
    "narrative_lag_flag": FlagTaxonomy("narrative_lag", "narrative_lag_media_silence_hours"),
    "correlation_locus_flag": FlagTaxonomy(
        "narrative_lag", "correlation_locus_pair_count_threshold"
    ),
    "overdue_lag_flag": FlagTaxonomy("lead_lag", "lead_lag_overdue_lead_sigma"),
    "lead_lag_inversion_flag": FlagTaxonomy("lead_lag", "lead_lag_overdue_lead_sigma"),
    "prediction_market_delta": FlagTaxonomy(
        "prediction_market", "prediction_market_delta_pp_threshold"
    ),
    "pair_trade_signature": FlagTaxonomy("options_flow", "pair_trade_signature"),
    "sector_wide_sweep": FlagTaxonomy("options_flow", "sector_wide_sweep"),
    "gold_real_yields_divergence": FlagTaxonomy(
        "intermarket_regime", "gold_real_yields_divergence"
    ),
    "oil_xle_beta_drift": FlagTaxonomy("intermarket_regime", "oil_xle_beta_drift"),
    "vix_spy_divergence": FlagTaxonomy("intermarket_regime", "vix_spy_divergence"),
    "q12_event_novelty": FlagTaxonomy("corporate_actions", "q12_event_novelty"),
    "etf_vs_single_name_divergence": FlagTaxonomy(
        "corporate_actions", "etf_vs_single_name_divergence"
    ),
}


# ---------------------------------------------------------------------------
# Pre-computed reverse index (built once at import; immutable thereafter)
# ---------------------------------------------------------------------------

_class_to_keys_build: dict[str, list[str]] = {}
for _key, _taxonomy in _REGISTRY.items():
    _class_to_keys_build.setdefault(_taxonomy.threshold_class, []).append(_key)
_CLASS_TO_KEYS: dict[str, tuple[str, ...]] = {
    cls: tuple(sorted(keys)) for cls, keys in _class_to_keys_build.items()
}
del _class_to_keys_build

_ALL_THRESHOLD_CLASSES: frozenset[str] = frozenset(_CLASS_TO_KEYS)
_KNOWN_FLAG_KEYS: frozenset[str] = frozenset(_REGISTRY)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def resolve_flag_taxonomy(flag_name: str) -> FlagTaxonomy:
    """Return the :class:`FlagTaxonomy` for *flag_name*.

    Dynamic suffixes (colon-delimited ticker or pair-key appended at runtime,
    e.g. ``"correlation_breakdown_flag:AAPL:MSFT"``) are stripped before the
    lookup: ``flag_name.split(":", 1)[0]`` is the canonical prefix.

    Raises :exc:`UnregisteredAnomalyFlagError` (a :exc:`ValueError`) when the
    canonical prefix is not in the registry.  A new producer flag that hasn't
    been registered here surfaces as this error the first time it reaches
    emission — the registry is the single source of truth, so an unknown prefix
    is a code gap, not a runtime condition to swallow.
    """
    prefix = flag_name.split(":", maxsplit=1)[0]
    try:
        return _REGISTRY[prefix]
    except KeyError:
        msg = (
            f"Anomaly flag {flag_name!r} (canonical prefix {prefix!r}) is not in the "
            "flag_event_types registry.  Add it to _REGISTRY in "
            "src/alphamind/distillation/flag_event_types.py."
        )
        raise UnregisteredAnomalyFlagError(msg) from None


def flag_keys_for_class(threshold_class: str) -> tuple[str, ...]:
    """Return the sorted tuple of canonical flag prefixes belonging to *threshold_class*.

    Raises :exc:`KeyError` for an unknown class — the caller is responsible for
    passing a class from :func:`all_threshold_classes`.
    """
    return _CLASS_TO_KEYS[threshold_class]


def all_threshold_classes() -> frozenset[str]:
    """Return the frozenset of all registered threshold classes."""
    return _ALL_THRESHOLD_CLASSES


def known_flag_keys() -> frozenset[str]:
    """Return the frozenset of all registered canonical flag prefixes."""
    return _KNOWN_FLAG_KEYS

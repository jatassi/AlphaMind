"""Shared loader for the cumulative-drawdown progressive tiers.

The progressive-tier sequence governing the cumulative-drawdown breach response
ships in ``config/guardrails.yaml`` under the ``cumulative_drawdown_pct`` rule.
``cumulative_drawdown_pct`` is the only rule that carries ``progressive_tiers``
— this is locked by ``GuardrailsConfig.progressive_tiers_only_on_cumulative_drawdown``
in ``alphamind.config.models.guardrails``. Centralizing the loader here means
the rule-id literal lives once; renaming the rule in
``config/guardrails.yaml`` requires updating exactly one call site.

The cumulative-drawdown enforcement orchestrator (story 02 / ALP-395) reads
this same tier sequence at runtime through the same loader; the verify scripts
and the test fixture suite consume it via the public function below.
"""

from __future__ import annotations

import pathlib
from functools import cache
from typing import Any, cast

import yaml

from alphamind.config.models.guardrails import GuardrailsConfig, ProgressiveTier

__all__ = ["load_cumulative_drawdown_progressive_tiers"]

_GUARDRAILS_YAML = pathlib.Path("config/guardrails.yaml")
_CUMULATIVE_DRAWDOWN_RULE_ID = "cumulative_drawdown_pct"


@cache
def load_cumulative_drawdown_progressive_tiers() -> tuple[ProgressiveTier, ...]:
    """Load the cumulative-drawdown progressive tiers from ``config/guardrails.yaml``.

    Reads + ``GuardrailsConfig.model_validate``s the shipped guardrails config,
    selects the unique rule whose ``id == "cumulative_drawdown_pct"`` (the only
    rule that carries ``progressive_tiers``), and returns its tier tuple.

    Cached — the YAML round-trip is sub-millisecond, but caching keeps every
    consumer (production orchestrator, verify scripts, test fixture state)
    pointed at the same tuple object so identity comparisons hold.
    """
    raw = cast(dict[str, Any], yaml.safe_load(_GUARDRAILS_YAML.read_text()))
    config = GuardrailsConfig.model_validate(raw)
    rule = next(r for r in config.rules if r.id == _CUMULATIVE_DRAWDOWN_RULE_ID)
    assert rule.progressive_tiers is not None
    return tuple(rule.progressive_tiers)

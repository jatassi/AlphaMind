"""Repository-provider closures for the SQL portfolio-state repository (ALP-472 lift).

The :class:`SqlPortfolioStateRepository` factory takes two providers — a
zero-arg ``active_risk_parameters_provider`` and a one-arg
``prior_active_risk_parameters_provider`` (keyed by prior invocation
``resolved_config_snapshot_path``). This module builds the canonical pair
the scheduler (and continuous-monitor substrate) consume.

Both closures are synchronous per ALP-454 Pre-resolved decision (C):
the SQL repository surface is sync, so its provider seam is too. The prior
provider rehydrates via :func:`load_prior_active_risk_parameters` and falls
back to the current set when the snapshot file is missing (first-ever
invocation, archive relocation); corrupt JSON propagates so an actual
contract violation aborts the invocation rather than silently substituting
an unrelated set.
"""

from __future__ import annotations

from collections.abc import Callable

from alphamind.portfolio_state.aggregates.risk_parameters import ActiveRiskParameterSet
from alphamind.risk_guardrails.regime_adaptation.active_parameters import (
    load_prior_active_risk_parameters,
)

__all__ = ["make_repository_providers"]


def make_repository_providers(
    active_risk_parameters: ActiveRiskParameterSet,
) -> tuple[
    Callable[[], ActiveRiskParameterSet],
    Callable[[str], ActiveRiskParameterSet],
]:
    """Build the two closure-providers ``SqlPortfolioStateRepository`` consumes.

    The repository factory's ``active_risk_parameters_provider`` is zero-arg;
    ``prior_active_risk_parameters_provider`` takes the prior invocation's
    resolved-config snapshot path and rehydrates the
    :class:`ActiveRiskParameterSet` that was active at that point. When the
    snapshot file is missing on disk (first-ever invocation, archive
    relocation), the prior provider falls back to the current set so the
    snapshot assembler stays operational.

    Both closures are synchronous per ALP-454 Pre-resolved decision (C):
    the SQL repository surface is sync, so its provider seam is too.
    """

    def _active_provider() -> ActiveRiskParameterSet:
        return active_risk_parameters

    def _prior_provider(snapshot_path: str) -> ActiveRiskParameterSet:
        try:
            return load_prior_active_risk_parameters(snapshot_path)
        except FileNotFoundError:
            return active_risk_parameters

    return _active_provider, _prior_provider

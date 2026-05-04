"""Analyst decision-layer public surface.

The model names (story 03 / ALP-293), the parser (story 05a / ALP-295), and
the Layer-2/3 cross-field validator (story 05b / ALP-296) are re-exported so
downstream callers can import from :mod:`alphamind.decision.analyst` without
reaching into the submodules. Later stories add harness/runner under the same
package.
"""

from alphamind.decision.analyst.models import (
    AnalystOutput,
    EntryOrder,
    EntryWindow,
    EventCondition,
    Greeks,
    GuardrailValidationResult,
    Instrument,
    InstrumentEquity,
    InstrumentOption,
    InstrumentStrategy,
    InvalidationCondition,
    InvalidationLeg,
    InvalidationRationale,
    OrderParameters,
    PositionSize,
    PriceCondition,
    Recommendation,
    RuleProjection,
    StrategyLeg,
    Target,
    TimeCondition,
    WatchlistEntry,
)
from alphamind.decision.analyst.parser import ParseError, parse_analyst_output
from alphamind.decision.analyst.validation import (
    DEFAULT_CONVICTION_BANDS,
    ValidationError,
    ValidationResult,
    ValidationWarning,
    validate_analyst_output,
)

__all__ = [
    "DEFAULT_CONVICTION_BANDS",
    "AnalystOutput",
    "EntryOrder",
    "EntryWindow",
    "EventCondition",
    "Greeks",
    "GuardrailValidationResult",
    "Instrument",
    "InstrumentEquity",
    "InstrumentOption",
    "InstrumentStrategy",
    "InvalidationCondition",
    "InvalidationLeg",
    "InvalidationRationale",
    "OrderParameters",
    "ParseError",
    "PositionSize",
    "PriceCondition",
    "Recommendation",
    "RuleProjection",
    "StrategyLeg",
    "Target",
    "TimeCondition",
    "ValidationError",
    "ValidationResult",
    "ValidationWarning",
    "WatchlistEntry",
    "parse_analyst_output",
    "validate_analyst_output",
]

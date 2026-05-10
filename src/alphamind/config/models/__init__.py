"""Public re-exports for alphamind.config.models package (story 02)."""

from pathlib import Path

from alphamind.config.models.agents import (
    AdaptiveAgentConfig,
    AgentName,
    AgentsConfig,
    AllowedModel,
    BaseAgentConfig,
)
from alphamind.config.models.assets import (
    AssetRole,
    AssetsConfig,
    Benchmark,
    DiscoverySource,
    DiscoveryVendor,
)
from alphamind.config.models.collector_schedule import CollectorEntry, CollectorScheduleConfig
from alphamind.config.models.data_sources import (
    BackoffStrategy,
    CategoryConfig,
    CriticalityTier,
    DataSourcesConfig,
    ProviderConfig,
    RetryShapeConfig,
)
from alphamind.config.models.digest import (
    AntiPatternSpike,
    CitationChainShift,
    DigestConfig,
    RegimeChange,
    SectorUnderperform,
    SourceSignalSurvivalDrop,
    ValidationSuperseded,
    ValidationWindowEnd,
)
from alphamind.config.models.distillation import (
    AnomalyDetection,
    DistillationConfig,
    LeadLag,
    NarrativeLag,
    PersistenceWindows,
    PredictionMarket,
    RegimeClassification,
    RegimeTransition,
)
from alphamind.config.models.execution import (
    ExecutionConfig,
    GreeksRefresh,
    OrderType,
    PaperHarness,
)
from alphamind.config.models.guardrails import (
    BreachResponse,
    EmergencyInvocation,
    EnforcementTier,
    EscalationZones,
    GuardrailsConfig,
    ProgressiveTier,
    RuleEntry,
)
from alphamind.config.models.llm_failure import (
    FailureMode,
    LLMFailureConfig,
    RetryCondition,
    RetryPolicy,
    RetryStrategy,
)
from alphamind.config.models.main import ExecutionMode, MainConfig, Paths, Profile
from alphamind.config.models.modes import (
    AnalystMode,
    AnalystOutputMode,
    CommandType,
    Mode,
    ModeConfig,
    PendingOrdersDefault,
    PmEmphasis,
    PmMode,
    StrategistAction,
    StrategistMode,
    StrategistOutputMode,
)
from alphamind.config.models.news_outlets import CredibilityTier, NewsOutletsConfig, OutletEntry
from alphamind.config.models.overlays import (
    EventType,
    FinalInvocationBeforeEvent,
    Overlay,
    PreEventActivation,
    PreEventOverlay,
    StressActivation,
    StressOverlay,
    StressTrigger,
)
from alphamind.config.models.profiles import (
    FeatureFlags,
    ProfileConfig,
    RiskPriority,
    ThesisPerformanceReviewConfig,
    TokenBudgetRange,
)
from alphamind.config.models.regimes import (
    LoosenOnExit,
    Regime,
    RegimeConfig,
    TightenOnEntry,
    TransitionPolicy,
)
from alphamind.config.models.run_types import (
    AgentsSection,
    NewsDigestConfig,
    QualitativeResearcherSection,
    RunType,
    RunTypeConfig,
)
from alphamind.config.models.scheduler import SchedulerConfig
from alphamind.config.models.venue import (
    Alpaca,
    AlpacaCredentials,
    SessionHours,
    SessionWindow,
    VenueConfig,
)
from alphamind.config.resolver import (
    LoadedConfig,
    ResolvedConfig,
    RuntimeDimensions,
    compose_config,
)
from alphamind.config.snapshot import SnapshotResult, persist_snapshot
from alphamind.config.tools import REGISTERED_TOOLS
from alphamind.config.validation.cross_reference import (
    CrossReferenceError,
    validate_cross_references,
)
from alphamind.config.validation.semantic import (
    SemanticInvariantError,
    enumerate_compositions,
    validate_semantic_invariants,
)

# ---------------------------------------------------------------------------
# .env.example helpers — kept here so monkeypatching
# `alphamind.config.models._ENV_EXAMPLE_KEYS` is observed by DataSourcesConfig's
# validator (which does a lazy in-function import of this name).
# ---------------------------------------------------------------------------

_ENV_EXAMPLE_PATH = Path(__file__).parent.parent.parent.parent.parent / ".env.example"


def _load_env_example_keys() -> frozenset[str]:
    keys: set[str] = set()
    if not _ENV_EXAMPLE_PATH.exists():
        return frozenset()
    for line in _ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, _ = line.partition("=")
            keys.add(key.strip())
    return frozenset(keys)


_ENV_EXAMPLE_KEYS: frozenset[str] = _load_env_example_keys()

__all__ = [
    "REGISTERED_TOOLS",
    "AdaptiveAgentConfig",
    "AgentName",
    "AgentsConfig",
    "AgentsSection",
    "AllowedModel",
    "Alpaca",
    "AlpacaCredentials",
    "AnalystMode",
    "AnalystOutputMode",
    "AnomalyDetection",
    "AntiPatternSpike",
    "AssetRole",
    "AssetsConfig",
    "BackoffStrategy",
    "BaseAgentConfig",
    "Benchmark",
    "BreachResponse",
    "CategoryConfig",
    "CitationChainShift",
    "CollectorEntry",
    "CollectorScheduleConfig",
    "CommandType",
    "CredibilityTier",
    "CriticalityTier",
    "CrossReferenceError",
    "DataSourcesConfig",
    "DigestConfig",
    "DiscoverySource",
    "DiscoveryVendor",
    "DistillationConfig",
    "EmergencyInvocation",
    "EnforcementTier",
    "EscalationZones",
    "EventType",
    "ExecutionConfig",
    "ExecutionMode",
    "FailureMode",
    "FeatureFlags",
    "FinalInvocationBeforeEvent",
    "GreeksRefresh",
    "GuardrailsConfig",
    "LLMFailureConfig",
    "LeadLag",
    "LoadedConfig",
    "LoosenOnExit",
    "MainConfig",
    "Mode",
    "ModeConfig",
    "NarrativeLag",
    "NewsDigestConfig",
    "NewsOutletsConfig",
    "OrderType",
    "OutletEntry",
    "Overlay",
    "PaperHarness",
    "Paths",
    "PendingOrdersDefault",
    "PersistenceWindows",
    "PmEmphasis",
    "PmMode",
    "PreEventActivation",
    "PreEventOverlay",
    "PredictionMarket",
    "Profile",
    "ProfileConfig",
    "ProgressiveTier",
    "ProviderConfig",
    "QualitativeResearcherSection",
    "Regime",
    "RegimeChange",
    "RegimeClassification",
    "RegimeConfig",
    "RegimeTransition",
    "ResolvedConfig",
    "RetryCondition",
    "RetryPolicy",
    "RetryShapeConfig",
    "RetryStrategy",
    "RiskPriority",
    "RuleEntry",
    "RunType",
    "RunTypeConfig",
    "RuntimeDimensions",
    "SchedulerConfig",
    "SectorUnderperform",
    "SemanticInvariantError",
    "SessionHours",
    "SessionWindow",
    "SnapshotResult",
    "SourceSignalSurvivalDrop",
    "StrategistAction",
    "StrategistMode",
    "StrategistOutputMode",
    "StressActivation",
    "StressOverlay",
    "StressTrigger",
    "ThesisPerformanceReviewConfig",
    "TightenOnEntry",
    "TokenBudgetRange",
    "TransitionPolicy",
    "ValidationSuperseded",
    "ValidationWindowEnd",
    "VenueConfig",
    "compose_config",
    "enumerate_compositions",
    "persist_snapshot",
    "validate_cross_references",
    "validate_semantic_invariants",
]

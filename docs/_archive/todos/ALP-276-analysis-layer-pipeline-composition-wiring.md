Compose the analysis-layer agents into a single per-invocation pipeline runner: `run_external_distillation` → three `run_<sector>_researcher` calls (parallel) + `run_qualitative_researcher` (parallel) → `run_adaptive_researcher` (consumes upstream typed briefs) → `run_synthesizer`.

Each agent's runner returns a typed `*Result` value; the composition runner threads `DistillationOutputs`, `tuple[SectorBrief, ...]`, `QualitativeBrief`, `CorrelationRegimeBrief`, and `AdaptiveBrief` through to the synthesizer's input bundle and out to the decision-layer agents.

Per-trigger budget overrides from `config/run_types/<trigger>.yaml` apply to every agent's `agent_config` lookup.

The adaptive-researcher ([ALP-112](https://linear.app/alphamind-jatassi/issue/ALP-112/adaptive-research)) and synthesizer ([ALP-114](https://linear.app/alphamind-jatassi/issue/ALP-114/synthesizer)) work trees each ship with fixture-based E2E verification but defer live composition to this story.
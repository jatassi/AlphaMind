# 09a — Frozen-dataclass mirror for `DistillationConfig` (boundary→domain converter)

## Goal

Convert `config/models/distillation.py`'s `DistillationConfig` (Pydantic, 235 LOC, 10+ consumer leaks into compute code) to a boundary→domain pattern: keep the Pydantic model at the YAML-load boundary; add a frozen-dataclass mirror in `distillation/_config_domain.py`; introduce a single `to_domain()` converter on the Pydantic class. Downstream distillation modules consume the frozen-dataclass form; the YAML loader remains the only Pydantic touchpoint. This is the pilot for the broader Pydantic config-leak fix described in audit finding L7.

## Reading

* `audit-alphamind-2026-05-12.html` § Findings — load-bearing L7
* `src/alphamind/config/models/distillation.py` — current Pydantic model
* `src/alphamind/distillation/q1/assemble.py:42, 1035, 1106, 1179` — consumer leaks (takes `config: DistillationConfig` (Pydantic) as parameter)
* `src/alphamind/pipeline/_shared.py:54` — `cfg.model_copy(update=...)` Pydantic-specific call on config
* `src/alphamind/config/resolver.py:83-120` — existing `ResolvedConfig` frozen-dataclass pattern (template)
* Story 07 (<issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue>) — distillation compute/load split; the converted config type is what `compute_q*_blocks(inputs, config)` takes

## Depends on

* 07 (<issue id="37e90f04-4baf-482b-b0a0-1d092af29826">ALP-467</issue>) — compute/load split makes the consumer surface clean; the frozen-dataclass config slots into the pure compute functions

## Scope

In scope: frozen-dataclass mirror of `DistillationConfig`; `to_domain()` converter on the Pydantic class; update of distillation consumers to take the domain type. Tests at `tests/distillation/test_config_domain.py`.

### 1\. `distillation/_config_domain.py`

```python
@dataclass(frozen=True, slots=True)
class DistillationDomainConfig:
    """Domain-typed mirror of config.models.DistillationConfig.

    Internal distillation code consumes this; the Pydantic surface is the
    YAML-load boundary only. Frozen, sloted, hashable, mypy-strict.
    """
    anomaly_detection: AnomalyDetectionConfig
    regime_classification: RegimeClassificationConfig
    # ... mirror the Pydantic structure as nested frozen dataclasses
```

Mirror every nested config in `config/models/distillation.py` as a sibling frozen dataclass in `_config_domain.py`.

### 2\. `to_domain()` on Pydantic class

```python
class DistillationConfig(BaseModel):
    ...
    def to_domain(self) -> DistillationDomainConfig:
        return DistillationDomainConfig(
            anomaly_detection=self.anomaly_detection.to_domain(),
            regime_classification=self.regime_classification.to_domain(),
            ...
        )
```

Each nested Pydantic class gets a `to_domain()` method too. The conversion is mechanical.

### 3\. Update consumers

Every consumer that takes `config: DistillationConfig` updates its signature to `config: DistillationDomainConfig`. The composition root (config loader or pipeline orchestrator) calls `pydantic_config.to_domain()` once and passes the result down.

Replace `cfg.model_copy(update=...)` patterns with `dataclasses.replace(domain_cfg, ...)`.

### 4\. Update `ResolvedConfig`

`config/resolver.py:99-120` exposes `DistillationConfig` (Pydantic) via pass-through. Change to expose `DistillationDomainConfig`. Consumers of `ResolvedConfig.distillation` get the domain type.

### Out of scope

Other Pydantic config types (`GuardrailsConfig`, `ContinuousMonitorConfig`, etc.) — those are follow-up work, tracked as a sibling story (or just left for incremental conversion). This story is the `DistillationConfig` pilot.

## Acceptance criteria

- [ ] `src/alphamind/distillation/_config_domain.py` exists with `DistillationDomainConfig` frozen dataclass and nested mirrors.
- [ ] `config.models.DistillationConfig` has a `to_domain()` method returning `DistillationDomainConfig`.
- [ ] Every distillation consumer that previously took `config: DistillationConfig` now takes `config: DistillationDomainConfig`.
- [ ] `ResolvedConfig.distillation` is `DistillationDomainConfig` (or returns it via property).
- [ ] No `cfg.model_copy(...)` calls remain inside `distillation/*` — replaced with `dataclasses.replace`.
- [ ] `uv run ruff check .`, `uv run mypy`, `uv run pytest -n auto`, `uv run lint-imports` all pass.

## Verification

`grep -rn "DistillationConfig" src/alphamind/distillation/` returns hits only for the YAML-load entry point or boundary types; internal code uses `DistillationDomainConfig`. Round-trip test: load a YAML, build Pydantic, call `to_domain()`, assert all numeric fields match. Compute call: `compute_q1_blocks(inputs, domain_cfg)` runs without Pydantic in the call path.
# 03a — Anomaly-stream loaders

## Goal

Implement the loaders that extract the adaptive researcher's two anomaly input streams from upstream typed objects, producing the typed records story 04b's input-bundle assembler renders into the user message. Two stream-shaped inputs and one aggregator: `extract_distillation_anomalies` flattens every `OutputBlock.anomaly_flags` carried in `DistillationOutputs.all_blocks` into `DistillationAnomalyRecord`s; `extract_sector_anomalies` walks each `SectorBrief.anomalies` tuple into `SectorAnomalyRecord`s; `assemble_adaptive_anomaly_inputs` returns the combined `AdaptiveAnomalyInputs` container. Pure functions — no I/O, no logging, deterministic ordering. Per parent issue resolution (F): the loader does NOT collapse cross-stream duplicates; that is the LLM's triage job.

## Reading

* `docs/design/03-analysis-layer/adaptive-research.md` § Inputs — names the two anomaly streams and the universal regime label, and the design intent that triage is the agent's value-add (loaders surface raw streams, agent decides).
* `src/alphamind/distillation/orchestrator.py` § `DistillationOutputs` — the upstream container; `all_blocks: tuple[OutputBlock, ...]` is the source for distillation anomalies. `universal_regime_label: dict[str, Any]` is consumed by story 04b's bundle assembler, NOT by this loader.
* `src/alphamind/distillation/output.py` § `OutputBlock`, `AnomalyFlag`, `AnomalySeverity`, `OutputAudience` — typed contracts for the upstream payload. Each block's `block_id` follows the `<category>.<short_name>` convention (e.g., `q1.volume_spike`); `anomaly_flags` carries zero or more `AnomalyFlag(name, magnitude, severity)`.
* `src/alphamind/analysis/domain_researchers/models.py` § `Anomaly`, `SectorBrief` — the per-sector source. `Anomaly.anomaly_id` is the `SA-{SECTOR}-ANOM-N` reference the adaptive Trigger field cites; `Anomaly.suggested_question` is the design-doc-mandated researcher hint.
* `src/alphamind/analysis/_shared.py` § `AnomalySeverity`, `Sector` — re-exported from distillation; reuse rather than re-import from `distillation.output`.
* `src/alphamind/analysis/qualitative_research/loaders.py` — sibling-package loader pattern. Note the dataclass-based `QualitativeInputs` container with `data_freshness` resolution, and the `Pure function` discipline (no logging, no Session-mutating side effects).

## Depends on

* <issue id="5043b367-d100-413e-b7f0-3e1227963080">ALP-254</issue> (story 01) — package skeleton + `_shared.py` audit.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/loaders.py` and `tests/analysis/adaptive_research/test_loaders.py`.

### 1\. Typed input records

```python
class DistillationAnomalyRecord(BaseModel, frozen=True):
    """One anomaly flag extracted from DistillationOutputs.all_blocks.

    Trigger-attribution fields the LLM uses to construct its `Trigger:` field:
      - block_id: the OutputBlock's stable identifier (e.g., "q1.volume_spike").
      - flag_name: the AnomalyFlag.name within that block.
      - magnitude: the AnomalyFlag.magnitude (sigma / pct / pp depending on flag).
      - severity: investigate_now | investigate_if_persists | note_for_context.
      - regime_context: the OutputBlock.regime_context one-liner when present, else None.
      - freshness_ts: the OutputBlock.freshness_ts (UTC timestamp of the underlying data).
    """

    block_id: str = Field(min_length=1)
    flag_name: str = Field(min_length=1)
    magnitude: float
    severity: AnomalySeverity
    regime_context: str | None
    freshness_ts: datetime


class SectorAnomalyRecord(BaseModel, frozen=True):
    """One anomaly extracted from a domain researcher's SectorBrief.anomalies.

    Carries the upstream reference ID directly so the LLM's `Trigger:` field can cite it
    verbatim and the validator (story 03c) can resolve it.
    """

    anomaly_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-ANOM-\\d+$")
    description: str = Field(min_length=1)
    anomaly_type: str = Field(min_length=1)  # str rather than enum — the Anomaly.anomaly_type StrEnum value passes through unchanged
    tickers: tuple[str, ...]
    severity: AnomalySeverity
    suggested_question: str = Field(min_length=1)
    sector: Sector
```

`tickers` may be empty (some sector anomalies are sector-wide rather than per-ticker — same as the upstream `Anomaly` model permits).

`sector` is derived from the source `SectorBrief.sector` and added here so the bundle renderer can group by sector without a second pass.

### 2\. `AdaptiveAnomalyInputs` container

```python
@dataclass(frozen=True)
class AdaptiveAnomalyInputs:
    """The two anomaly streams plus a single freshness floor.

    `data_freshness` is the minimum (most stale) of the contributing record-set freshness
    timestamps, so the agent can detect that one stream is stale and signal that in the
    eventual brief. SectorAnomalyRecord has no freshness timestamp (sector briefs are
    always fresh-as-of-this-invocation by construction); only distillation contributes
    candidates.
    """

    distillation: tuple[DistillationAnomalyRecord, ...]
    sector: tuple[SectorAnomalyRecord, ...]
    data_freshness: datetime
```

### 3\. `extract_distillation_anomalies`

```python
def extract_distillation_anomalies(
    outputs: DistillationOutputs,
) -> tuple[DistillationAnomalyRecord, ...]:
    """Flatten every OutputBlock.anomaly_flags into a deterministically-ordered tuple.

    Walks `outputs.all_blocks` in the order the orchestrator emitted them, then within
    each block iterates `anomaly_flags` in the order they were attached. Returns ()
    when no block carries any anomaly flag.
    """
```

Implementation:

* Iterate `outputs.all_blocks` in given order (the orchestrator already emits deterministically per `DistillationOutputs` docstring).
* For each block, iterate `block.anomaly_flags` in given order.
* Construct a `DistillationAnomalyRecord` from each flag, copying `block_id`, `flag.name`, `flag.magnitude`, `flag.severity`, `block.regime_context`, `block.freshness_ts`.
* Return as tuple in flatten order.

### 4\. `extract_sector_anomalies`

```python
def extract_sector_anomalies(
    sector_briefs: tuple[SectorBrief, ...],
) -> tuple[SectorAnomalyRecord, ...]:
    """Walk each SectorBrief.anomalies tuple into typed records.

    Iterates `sector_briefs` in given order, then within each brief iterates
    `brief.anomalies` in given order. Returns () when no brief carries any anomaly.
    """
```

Implementation:

* Iterate `sector_briefs` in given order.
* For each `brief`, iterate `brief.anomalies` in given order.
* Construct a `SectorAnomalyRecord` from each `Anomaly`, copying `anomaly_id`, `description`, `anomaly_type.value` (as str), `tickers`, `severity`, `suggested_question`, plus `brief.sector` for the `sector` field.
* Return as tuple in flatten order.

### 5\. `assemble_adaptive_anomaly_inputs`

```python
def assemble_adaptive_anomaly_inputs(
    *,
    distillation_outputs: DistillationOutputs,
    sector_briefs: tuple[SectorBrief, ...],
    as_of: datetime,
) -> AdaptiveAnomalyInputs:
    """Compose the AdaptiveAnomalyInputs container from upstream typed objects.

    `data_freshness` is the minimum `freshness_ts` across all DistillationAnomalyRecords.
    When the distillation stream is empty, `data_freshness` falls through to `as_of`.
    """
```

Implementation:

* Call `extract_distillation_anomalies(distillation_outputs)`.
* Call `extract_sector_anomalies(sector_briefs)`.
* Compute `data_freshness = min(rec.freshness_ts for rec in distillation_records) if distillation_records else as_of`.
* Return the `AdaptiveAnomalyInputs` container.

### 6\. Module exports

```python
__all__ = [
    "AdaptiveAnomalyInputs",
    "DistillationAnomalyRecord",
    "SectorAnomalyRecord",
    "assemble_adaptive_anomaly_inputs",
    "extract_distillation_anomalies",
    "extract_sector_anomalies",
]
```

### Out of scope

* The bundle text rendering — story 04b owns the text-shape rendering.
* Any cross-stream deduplication or merging — explicit out per parent issue resolution (F); the LLM does triage.
* The `universal_regime_label` payload — consumed directly by story 04b from `DistillationOutputs`, not via this loader.
* SQLAlchemy / Session access — this loader takes typed objects, not a database session.

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.loaders import AdaptiveAnomalyInputs, DistillationAnomalyRecord, SectorAnomalyRecord, extract_distillation_anomalies, extract_sector_anomalies, assemble_adaptive_anomaly_inputs` resolves cleanly.
- [ ] `extract_distillation_anomalies` on a `DistillationOutputs` with N blocks carrying M total anomaly flags returns exactly M records in flatten order (block-major, then flag order within block).
- [ ] `extract_distillation_anomalies` on `DistillationOutputs.all_blocks=()` returns `()`.
- [ ] `extract_distillation_anomalies` preserves each `OutputBlock.regime_context` (str or None) and `OutputBlock.freshness_ts` on the produced records.
- [ ] `extract_sector_anomalies` on three `SectorBrief`s with N total anomalies returns exactly N records preserving their `anomaly_id`, `tickers`, `severity`, `suggested_question`, and the originating sector.
- [ ] `extract_sector_anomalies` on `()` returns `()`.
- [ ] `assemble_adaptive_anomaly_inputs` sets `data_freshness` to the minimum distillation-record `freshness_ts` when distillation records exist; falls through to `as_of` when distillation records are empty.
- [ ] `assemble_adaptive_anomaly_inputs` returns an `AdaptiveAnomalyInputs` whose `distillation` and `sector` tuples are the unmodified outputs of the two extractors (same record order).
- [ ] All loaders are pure functions: calling twice with the same inputs produces structurally-equal outputs.
- [ ] `tests/analysis/adaptive_research/test_loaders.py` covers each acceptance criterion with constructed fixture `DistillationOutputs` + `SectorBrief` instances (no DB session).
- [ ] `uv run pytest tests/analysis/adaptive_research/test_loaders.py -n auto` passes; `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run `uv run pytest tests/analysis/adaptive_research/test_loaders.py -n auto`. Construct a `DistillationOutputs` fixture mirroring the shape `run_external_distillation` produces (multi-block, mixed audience, mixed anomaly flag counts) and assert the flatten order is reproducible byte-equal across two runs.
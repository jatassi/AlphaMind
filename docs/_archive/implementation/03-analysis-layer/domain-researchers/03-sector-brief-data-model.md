## Goal

Implement the typed in-memory representation of a domain researcher's output — `SectorBrief` plus its constituent records (`Finding`, `Anomaly`, `ThesisCandidate`) and the closed-set enums local to the brief. The structure is what story 04's parser produces, what story 05's validator checks, and the form the synthesizer's retrieval store keys against by reference ID.

This story **imports** `Sector` and `AnomalySeverity` from `alphamind.analysis._shared` (story 02). It does **not** redefine them.

## Reading

* `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the prose schema this story formalizes.
* `docs/design/03-analysis-layer/domain-researchers/financials.md` § Output — sector prefix `SA-FIN`.
* `docs/design/03-analysis-layer/domain-researchers/energy.md` § Output — sector prefix `SA-ENERGY`.
* `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format rules for `SA-{SECTOR}-N`, `SA-{SECTOR}-ANOM-N`, `SA-{SECTOR}-TC-N`.
* `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — downstream consumption pattern that motivates the typed structure.
* `src/alphamind/analysis/_shared.py` (from story 02) — `Sector`, `AnomalySeverity` reused here.
* `src/alphamind/distillation/output.py` — `AnomalyFlag` (the distillation envelope's flag, NOT this story's `Anomaly`); read so the distinction is clear.

## Depends on

* **02** (<issue id="d47bb088-b447-4f25-b67c-67d73739dad4">ALP-133</issue>) — `Sector` enum and `AnomalySeverity` re-export.

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

### [models.py](<http://models.py>) — Pydantic v2 dataclasses

Use `pydantic.BaseModel` with `model_config = {"frozen": True}` so instances are hashable and immutable; downstream callers (validator, parser tests, retrieval-store builder) treat them as values, not records.

### Imports from the shared module

```python
from alphamind.analysis._shared import AnomalySeverity, Sector
```

Do not redefine these. The shared module's `Sector` is the single source for sector identity; the shared module's `AnomalySeverity` is the same `Literal["investigate_now", "investigate_if_persists", "note_for_context"]` the distillation envelope uses.

### Closed-set enums local to the brief

These do not collide with anything elsewhere; define as `enum.StrEnum`:

* `SignalType`: `PRICE_ACTION = "price_action"`, `FLOW = "flow"`, `OPTIONS = "options"`, `FUNDAMENTAL = "fundamental"`, `SENTIMENT = "sentiment"`, `TECHNICAL = "technical"`, `CROSS_ASSET = "cross_asset"` — the seven values listed in `tech-semis.md § Signal type taxonomy`.
* `Strength`: `STRONG`, `MODERATE`, `WEAK` — for finding strength.
* `AnomalyType`: `VOLUME`, `PRICE_FLOW_DIVERGENCE`, `CORRELATION_BREAK`, `OPTIONS_SKEW`, `OTHER` — exactly the five values listed for the anomaly-type field.
* `Direction`: `LONG`, `SHORT`.
* `SetupType`: `CATALYST`, `MEAN_REVERSION`, `MOMENTUM`, `DIVERGENCE`, `EVENT`.
* `ConvictionSketch`: `LOW`, `MODERATE`, `HIGH`.
* `SignalQuality`: `HIGH`, `MODERATE`, `LOW`, `DEGRADED`.

### Records

```python
class Finding(BaseModel, frozen=True):
    finding_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-\d+$")
    headline: str
    tickers: tuple[str, ...]
    signal_type: SignalType
    strength: Strength
    detail: str

class Anomaly(BaseModel, frozen=True):
    anomaly_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-ANOM-\d+$")
    description: str
    anomaly_type: AnomalyType
    tickers: tuple[str, ...]
    severity: AnomalySeverity   # imported from _shared
    suggested_question: str

class ThesisCandidate(BaseModel, frozen=True):
    thesis_candidate_id: str = Field(pattern=r"^SA-(TECH|FIN|ENERGY)-TC-\d+$")
    ticker: str
    direction: Direction
    setup_type: SetupType
    catalyst: str
    time_horizon_hours: str
    conviction_sketch: ConvictionSketch
    conviction_justification: str
    key_risk: str

class SectorBrief(BaseModel, frozen=True):
    invocation_id: str
    sector: Sector              # imported from _shared
    signal_quality: SignalQuality
    signal_quality_reason: str | None
    findings: tuple[Finding, ...]
    anomalies: tuple[Anomaly, ...]
    thesis_candidates: tuple[ThesisCandidate, ...]
```

The `signal_quality_reason` invariant ("required when `signal_quality == DEGRADED`, `None` otherwise") is enforced by a Pydantic `model_validator(mode="after")`.

### Helper

```python
SECTOR_PREFIX: dict[Sector, str] = {
    Sector.TECH_SEMIS: "SA-TECH",
    Sector.FINANCIALS: "SA-FIN",
    Sector.ENERGY: "SA-ENERGY",
}
```

The validator (story 05) and parser (story 04) consume this. Note the values do **not** match the sector enum's lowercase strings — the prefix is a brief-format convention, not the runtime sector identity.

### Distinction note in the module docstring

Add a short paragraph in the module docstring explicitly distinguishing this module's `Anomaly` (a brief-level record carrying narrative + suggested question + severity) from `alphamind.distillation.output.AnomalyFlag` (an envelope-level flag carrying name + magnitude + severity that travels with a distillation `OutputBlock`). They share the `severity` vocabulary via the shared `AnomalySeverity` literal. Both names stand by design.

### Unit tests

Under `tests/analysis/domain_researchers/test_models.py`:

* Each enum has the exact members listed in the design doc (regression test for accidental additions).
* `SectorBrief` rejects construction when `signal_quality == DEGRADED` and `signal_quality_reason is None`.
* `SectorBrief` rejects construction when `signal_quality_reason` is non-`None` for any value other than `DEGRADED`.
* `Finding` rejects construction when `tickers` is empty (a finding with no affected tickers is structurally meaningless; the prose contract implies at least one).
* `ThesisCandidate` rejects an empty `ticker` string.
* All records are hashable (test by adding to a `frozenset`).
* `SECTOR_PREFIX` covers every `Sector` member.
* The reference-ID regex on `Finding.finding_id` accepts `SA-TECH-1`, `SA-FIN-12`, `SA-ENERGY-3`; rejects `SA-tech-1`, `SA-FOO-1`, `SA-TECH-ANOM-1`, missing-digit forms.
* Same coverage for `Anomaly.anomaly_id` and `ThesisCandidate.thesis_candidate_id` regexes.

## Out of scope

* The text-format parser that produces `SectorBrief` instances — story 04.
* The validator that asserts sequential indexing and section-prefix correctness across a brief — story 05.
* Sector enum / AnomalySeverity definition — already in `_shared.py` per story 02.
* Any persistence schema for sector briefs — the brief lives in memory during the invocation and is rendered to the invocation archive as text, not as serialized objects.

## Notes

Why Pydantic v2 over plain `@dataclass(frozen=True)`: the parser (story 04) benefits from Pydantic's field validators (the regex on `finding_id`, the cross-field rule on `signal_quality_reason`); the runtime cost is acceptable on a per-invocation basis (briefs are small), and the validator (story 05) reuses the same `model_validator` machinery rather than implementing parallel checks.

Sequential indexing across a section (no gaps, no duplicates) is *not* a per-record property — it requires the full brief — so it lives in story 05's validator, not here.

The `tickers: tuple[str, ...]` choice over `list[str]`: tuples are hashable and immutable, matching `frozen=True`. The parser (story 04) builds them from comma-separated lists in the LLM output. Use `tuple[str, ...]` consistently across all records carrying a ticker collection.

The `time_horizon_hours: str` choice (as opposed to a structured `Range[int, int]`) deliberately preserves the design doc's prose form. The downstream consumer is the synthesizer (LLM), which reads the prose; promoting to a numeric range would be over-specification.

The `conviction_justification` field is split out from `conviction_sketch` — the prose contract has them on the same line ("Conviction sketch: {low | moderate | high} with 1-sentence justification") but downstream consumers benefit from having them separate. The parser (story 04) splits the line at the "with " separator.

`Anomaly` (this module) and `AnomalyFlag` (distillation): same severity vocabulary (via the shared `AnomalySeverity` literal), different surfaces. The brief's `Anomaly` is the LLM's output record; `AnomalyFlag` is the deterministic-distillation flag attached to an `OutputBlock`. Story 03 documents the distinction in the module docstring; downstream code keeps both names.

Do not invent a `to_text()` / serializer method here. Text rendering is the parser's inverse and lives in the parser story (04) if needed at all — the LLM produces text; the parser produces the model; tests compare model-to-model, not model-to-text-roundtrip.

## Acceptance criteria

- [ ] `src/alphamind/analysis/domain_researchers/models.py` imports `Sector` and `AnomalySeverity` from `alphamind.analysis._shared`; does not redefine them.
- [ ] `SignalType`, `Strength`, `AnomalyType`, `Direction`, `SetupType`, `ConvictionSketch`, `SignalQuality` are defined as `StrEnum` subclasses.
- [ ] Each enum has exactly the members listed in the design doc (verified by a unit test that asserts the member set).
- [ ] `Finding`, `Anomaly`, `ThesisCandidate`, `SectorBrief` are defined as frozen Pydantic v2 models.
- [ ] `Finding.finding_id` is regex-validated against `^SA-(TECH|FIN|ENERGY)-\d+$`.
- [ ] `Anomaly.anomaly_id` is regex-validated against `^SA-(TECH|FIN|ENERGY)-ANOM-\d+$`.
- [ ] `ThesisCandidate.thesis_candidate_id` is regex-validated against `^SA-(TECH|FIN|ENERGY)-TC-\d+$`.
- [ ] `SectorBrief` rejects `signal_quality == DEGRADED` with `signal_quality_reason is None`.
- [ ] `SectorBrief` rejects `signal_quality != DEGRADED` with a non-`None` `signal_quality_reason`.
- [ ] `SECTOR_PREFIX` mapping exposes the three sector → prefix-string entries.
- [ ] `Finding` rejects empty `tickers` tuple.
- [ ] All records are hashable.
- [ ] Module docstring distinguishes brief-level `Anomaly` from distillation envelope's `AnomalyFlag`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.

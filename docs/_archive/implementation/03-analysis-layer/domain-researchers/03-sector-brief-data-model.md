---
status: not_started
completed_date:
commit_id:
---

# 03 — `SectorBrief` data model

## Goal

Implement the typed in-memory representation of a domain researcher's output — `SectorBrief` plus its constituent records (`Finding`, `Anomaly`, `ThesisCandidate`) and closed-set enums for signal type, anomaly type, severity, direction, setup type, conviction sketch, and signal quality. This is the structure the parser (story 04) produces and the validator (story 05) checks; it is also the form the synthesizer's retrieval store keys against by reference ID.

## Reading

- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the prose schema this story formalizes
- `docs/design/03-analysis-layer/domain-researchers/financials.md` § Output — sector prefix `SA-FIN`, smaller token budget
- `docs/design/03-analysis-layer/domain-researchers/energy.md` § Output — sector prefix `SA-ENERGY`
- `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format rules for `SA-{SECTOR}-N`, `SA-{SECTOR}-ANOM-N`, `SA-{SECTOR}-TC-N`
- `docs/design/03-analysis-layer/synthesizer.md` § Retrieval store side-effect — downstream consumption pattern that motivates the typed structure

## Depends on

None.

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `models.py` defining the Pydantic v2 dataclasses below. Use `pydantic.BaseModel` with `model_config = {"frozen": True}` so instances are hashable and immutable; downstream callers (validator, parser tests, retrieval-store builder) treat them as values, not records.
- Closed-set enums via `enum.StrEnum` (Python 3.13 native):
  - `Sector`: `TECH_SEMIS = "tech_semis"`, `FINANCIALS = "financials"`, `ENERGY = "energy"`. Use this enum in fields and tests; never compare to raw strings.
  - `SignalType`: `PRICE_ACTION`, `FLOW`, `OPTIONS`, `FUNDAMENTAL`, `SENTIMENT`, `TECHNICAL`, `CROSS_ASSET` — exactly the seven values listed in `tech-semis.md § Signal type taxonomy`.
  - `Strength`: `STRONG`, `MODERATE`, `WEAK` — for finding strength.
  - `AnomalyType`: `VOLUME`, `PRICE_FLOW_DIVERGENCE`, `CORRELATION_BREAK`, `OPTIONS_SKEW`, `OTHER` — exactly the five values listed for the anomaly-type field.
  - `AnomalySeverity`: `INVESTIGATE_NOW`, `INVESTIGATE_IF_PERSISTS`, `NOTE_FOR_CONTEXT` — three values per `tech-semis.md § Anomaly severity levels`.
  - `Direction`: `LONG`, `SHORT`.
  - `SetupType`: `CATALYST`, `MEAN_REVERSION`, `MOMENTUM`, `DIVERGENCE`, `EVENT`.
  - `ConvictionSketch`: `LOW`, `MODERATE`, `HIGH`.
  - `SignalQuality`: `HIGH`, `MODERATE`, `LOW`, `DEGRADED`.
- Records:
  - `Finding(finding_id: str, headline: str, tickers: tuple[str, ...], signal_type: SignalType, strength: Strength, detail: str)`. The `finding_id` matches `^SA-(TECH|FIN|ENERGY)-\d+$`.
  - `Anomaly(anomaly_id: str, description: str, anomaly_type: AnomalyType, tickers: tuple[str, ...], severity: AnomalySeverity, suggested_question: str)`. The `anomaly_id` matches `^SA-(TECH|FIN|ENERGY)-ANOM-\d+$`.
  - `ThesisCandidate(thesis_candidate_id: str, ticker: str, direction: Direction, setup_type: SetupType, catalyst: str, time_horizon_hours: str, conviction_sketch: ConvictionSketch, conviction_justification: str, key_risk: str)`. The `thesis_candidate_id` matches `^SA-(TECH|FIN|ENERGY)-TC-\d+$`. `time_horizon_hours` is a string (e.g., `"4–24h"`, `"24–72h"`) because the design doc deliberately keeps it imprecise — a sketch, not a calibrated number. The `conviction_justification` field is the one-sentence justification paired with `conviction_sketch` per the prose contract.
  - `SectorBrief(invocation_id: str, sector: Sector, signal_quality: SignalQuality, signal_quality_reason: str | None, findings: tuple[Finding, ...], anomalies: tuple[Anomaly, ...], thesis_candidates: tuple[ThesisCandidate, ...])`. `signal_quality_reason` is required when `signal_quality == DEGRADED` and `None` otherwise; this constraint is enforced by a Pydantic `model_validator(mode="after")`.
- Type aliases / helpers:
  - `SECTOR_PREFIX: dict[Sector, str]` mapping `TECH_SEMIS → "SA-TECH"`, `FINANCIALS → "SA-FIN"`, `ENERGY → "SA-ENERGY"`. The validator (story 05) and parser (story 04) consume this.
- Unit tests:
  - Each enum has the exact members listed in the design doc (a regression test for accidental additions).
  - `SectorBrief` rejects construction when `signal_quality == DEGRADED` and `signal_quality_reason is None`.
  - `SectorBrief` rejects construction when `signal_quality_reason` is non-`None` for any value other than `DEGRADED`.
  - `Finding` rejects construction when `tickers` is empty (a finding with no affected tickers is structurally meaningless; the prose contract implies at least one).
  - `ThesisCandidate` rejects an empty `ticker` string.
  - All records are hashable (test by adding to a `frozenset`).

Out of scope:
- The text-format parser that produces `SectorBrief` instances (story 04).
- The validator that asserts sequential indexing and section-prefix correctness across a brief (story 05).
- Any persistence schema for sector briefs — the brief lives in memory during the invocation and is rendered to the invocation archive as text, not as serialized objects.

## Notes

Why Pydantic v2 over plain `@dataclass(frozen=True)`: the parser (story 04) benefits from Pydantic's field validators (the regex on `finding_id`, the cross-field rule on `signal_quality_reason`); the runtime cost is acceptable on a per-invocation basis (briefs are small), and the validator (story 05) reuses the same `model_validator` machinery rather than implementing parallel checks.

The reference-ID regexes are encoded as `Field(pattern=...)` constraints. Sequential indexing across a section (no gaps, no duplicates) is *not* a per-record property — it requires the full brief — so it lives in story 05's validator, not here.

The `tickers: tuple[str, ...]` choice over `list[str]`: tuples are hashable and immutable, matching the `frozen=True` model config. The parser (story 04) builds them from comma-separated lists in the LLM output. Use `tuple[str, ...]` consistently across all records carrying a ticker collection.

The `time_horizon_hours: str` choice (as opposed to a structured `Range[int, int]`) deliberately preserves the design doc's prose form. The downstream consumer is the synthesizer (LLM), which reads the prose; promoting to a numeric range would be over-specification.

The `conviction_justification` field is split out from `conviction_sketch` — the prose contract has them on the same line ("Conviction sketch: {low | moderate | high} with 1-sentence justification") but downstream consumers benefit from having them separate. The parser (story 04) splits the line at the "with " separator.

Do not invent a `to_text()` / serializer method here. Text rendering is the parser's inverse and lives in the parser story (04) if needed at all — the LLM produces text; the parser produces the model; tests compare model-to-model, not model-to-text-roundtrip.

## Acceptance criteria

- [ ] `src/alphamind/analysis/domain_researchers/models.py` defines `Sector`, `SignalType`, `Strength`, `AnomalyType`, `AnomalySeverity`, `Direction`, `SetupType`, `ConvictionSketch`, `SignalQuality` as `StrEnum` subclasses.
- [ ] Each enum has exactly the members listed in the design doc (verified by a unit test that asserts the member set).
- [ ] `Finding`, `Anomaly`, `ThesisCandidate`, `SectorBrief` are defined as frozen Pydantic v2 models.
- [ ] `Finding.finding_id` is regex-validated against `^SA-(TECH|FIN|ENERGY)-\d+$`.
- [ ] `Anomaly.anomaly_id` is regex-validated against `^SA-(TECH|FIN|ENERGY)-ANOM-\d+$`.
- [ ] `ThesisCandidate.thesis_candidate_id` is regex-validated against `^SA-(TECH|FIN|ENERGY)-TC-\d+$`.
- [ ] `SectorBrief` rejects `signal_quality == DEGRADED` with `signal_quality_reason is None`.
- [ ] `SectorBrief` rejects `signal_quality != DEGRADED` with a non-`None` `signal_quality_reason`.
- [ ] `SECTOR_PREFIX` mapping exposes the three sector → prefix-string entries.
- [ ] Records are hashable.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

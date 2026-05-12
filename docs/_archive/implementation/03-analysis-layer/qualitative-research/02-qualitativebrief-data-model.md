# 02 — `QualitativeBrief` data model

## Goal

Implement the typed in-memory representation of the qualitative-researcher agent's output: `QualitativeBrief` plus its constituent records (`NarrativeThread`, `EvidenceLine`, `CatalystWatch`, `SentimentSnapshot`) and the closed-set enums local to the brief (`ThreadDirection`, `TimeHorizon`). The structure is what story 03a's parser produces, what story 03b's validator checks, and the form the synthesizer's retrieval store keys against by reference ID. This story imports `SignalQuality` from `alphamind.analysis._shared` (promoted by story 01) rather than defining a parallel enum.

## Reading

* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Output schema — the literal structure of the brief and its three sections (narrative threads, catalyst watch, sentiment snapshot).
* `docs/design/03-analysis-layer/qualitative-research.md` § Output § Schema design rationale — names the load-bearing constraints (multi-source-per-thread, direction + subject, evidence with source attribution, three-field sentiment snapshot).
* `docs/design/testing/llm-output-validation.md` § Reference-ID format rules — `QR-N` and `QR-CW-N` pattern conventions; sequential indexing within each section starting at 1.
* `prompts/analysis/qualitative_researcher.md` § `<output_contract>` — the exact section markers and indentation conventions the parser will need to recognize.
* `prompts/analysis/qualitative_researcher.md` § `<example_output>` — a worked example demonstrating the section shapes; story 03a's parser must accept this verbatim and story 03b's validator must mark it valid.
* `src/alphamind/analysis/domain_researchers/models.py` — the canonical analog (`SectorBrief` + records). Mirror the immutability discipline (`BaseModel, frozen=True`), the `Field(pattern=...)` convention for reference IDs, and the use of `model_validator(mode="after")` for cross-field invariants.
* `src/alphamind/analysis/_shared.py` — imports `SignalQuality` here.

## Depends on

* ALP-241 — story 01 lands the `_shared.py` `SignalQuality` promotion this story imports, plus the `qualitative_research/` package skeleton this story populates.

## Scope

In scope under `src/alphamind/analysis/qualitative_research/models.py` and `tests/analysis/qualitative_research/test_models.py`.

### 1\. Closed-set enums

Two new enums local to this brief, both `enum.StrEnum` subclasses:

* `ThreadDirection` with values `BULLISH = "bullish"`, `BEARISH = "bearish"`, `MIXED = "mixed"`, `UNCERTAIN = "uncertain"`.
* `TimeHorizon` with values `IMMEDIATE = "immediate"` (paired display: `immediate (<24h)`), `NEAR_TERM = "near_term"` (display: `near-term (24-72h)`), `DEVELOPING = "developing"` (display: `developing (>72h)`).

Display-string mapping for parser/validator use lives in this module as a `dict[TimeHorizon, str]` constant (`_TIME_HORIZON_DISPLAY`) so the parser can recognize the `(<24h)` / `(24-72h)` / `(>72h)` suffix without hard-coding it elsewhere.

### 2\. Records

* `EvidenceLine(BaseModel, frozen=True)` — `source_type: str`, `observation: str`, `citation: str`. All three required and non-empty (use `Field(min_length=1)`).
* `NarrativeThread(BaseModel, frozen=True)` — `thread_id: str` matching `Field(pattern=r"^QR-\d+$")`; `summary: str`; `relevance: str` (free-form per design — sectors and/or tickers as text); `direction: ThreadDirection`; `subject: str` (the noun phrase the direction applies to); `time_horizon: TimeHorizon`; `evidence: tuple[EvidenceLine, ...]`; `implication: str`. Add a `model_validator(mode="after")` enforcing the design doc's "minimum two input sources per thread" rule: `len(evidence) >= 2`. All string fields non-empty.
* `CatalystWatch(BaseModel, frozen=True)` — `catalyst_id: str` matching `Field(pattern=r"^QR-CW-\d+$")`; `ticker: str` (non-empty; ticker-membership check belongs in story 03b's validator, not in the model); `catalyst_name: str`; `hours_to_event: int` (`Field(ge=0)`); `thesis_impact: str`.
* `SentimentSnapshot(BaseModel, frozen=True)` — `extremes: str`, `divergences: str`, `regime: str`. All three required and non-empty (the design doc says `"none"` is the valid quiet-day value, not absence).
* `QualitativeBrief(BaseModel, frozen=True)` — `invocation_id: str`; `signal_quality: SignalQuality` (imported from `_shared`); `signal_quality_reason: str | None`; `threads: tuple[NarrativeThread, ...]`; `catalyst_watches: tuple[CatalystWatch, ...]`; `sentiment_snapshot: SentimentSnapshot`. Add a `model_validator(mode="after")` enforcing two invariants: (a) `signal_quality_reason` is required when `signal_quality == SignalQuality.DEGRADED` and forbidden otherwise (mirrors `SectorBrief`'s rule); (b) `len(threads) >= 1` — the design doc says the narrative-threads section always carries at least one entry, including a "nothing is happening" thread on quiet days.

### 3\. Module exports

`__all__` lists every public name in alphabetical order. Module docstring names this story (ALP) and the design-doc anchor § Output § Output schema.

### Out of scope

* Parsing the brief's text shape — story 03a.
* Validating sequential indexing, prefix consistency, ticker universe membership, or evidence citation format — story 03b.
* Defining the `Anomaly` or `ThesisCandidate` records — those are domain-researcher concepts; the qualitative brief has neither.

## Acceptance criteria

- [ ] `from alphamind.analysis.qualitative_research.models import QualitativeBrief, NarrativeThread, EvidenceLine, CatalystWatch, SentimentSnapshot, ThreadDirection, TimeHorizon` resolves.
- [ ] `from alphamind.analysis.qualitative_research.models import SignalQuality` resolves and is the same object as `alphamind.analysis._shared.SignalQuality` (re-export, not redefinition).
- [ ] `NarrativeThread(thread_id="QR-1", ..., evidence=(one_evidence_line,))` raises `ValidationError` with the message naming the two-source minimum.
- [ ] `NarrativeThread(thread_id="QR-CW-1", ...)` raises `ValidationError` (wrong prefix).
- [ ] `CatalystWatch(catalyst_id="QR-1", ...)` raises `ValidationError` (wrong prefix).
- [ ] `QualitativeBrief(..., threads=(), ...)` raises `ValidationError` naming the at-least-one-thread invariant.
- [ ] `QualitativeBrief(signal_quality=DEGRADED, signal_quality_reason=None, ...)` raises `ValidationError`.
- [ ] `QualitativeBrief(signal_quality=HIGH, signal_quality_reason="...", ...)` raises `ValidationError` (reason forbidden when not degraded).
- [ ] All record classes are immutable (`frozen=True`) and slot-equivalent (Pydantic v2 `BaseModel`).
- [ ] `tests/analysis/qualitative_research/test_models.py` exists and exercises every invariant via direct construction (not parser round-trip). Tests pass under `uv run pytest tests/analysis/qualitative_research/test_models.py -n auto`.

## Verification

Run `uv run pytest tests/analysis/qualitative_research/test_models.py -n auto`. Run `uv run mypy src/alphamind/analysis/qualitative_research/models.py` and confirm zero errors. Confirm that `grep "class SignalQuality" src/alphamind/analysis/qualitative_research/` returns no matches (the brief reuses the shared enum).

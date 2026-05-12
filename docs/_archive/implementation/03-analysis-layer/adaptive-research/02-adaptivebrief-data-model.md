# 02 — `AdaptiveBrief` data model

## Goal

Implement the typed in-memory representation of the adaptive-researcher agent's output: `AdaptiveBrief` plus its constituent records (`InvestigationThread`) and the closed-set enums local to the brief (`Assessment`, `Confidence`). The structure is what story 03b's parser produces, what story 03c's validator checks, and the form story 05's harness returns inside `HarnessSuccess`. The model enforces the design doc's conditional-field discipline as Pydantic invariants: a `signal` thread carries `implication`/`strengthens`/`weakens`; a `noise` thread carries `dismissal_reason`; an `inconclusive` thread carries `missing`. Mirrors the structural discipline of `alphamind.analysis.qualitative_research.models` but with the discriminated-conditional-fields wrinkle unique to this agent.

## Reading

* `docs/design/03-analysis-layer/adaptive-research.md` § Output § Output schema — the canonical schema (`Trigger`, `Question`, `Tickers`, `Sector`, `Tools used`, `Findings`, `Assessment`, `Confidence`, then conditional fields per assessment).
* `docs/design/03-analysis-layer/adaptive-research.md` § Output § Schema design rationale — names the `Trigger` semantics (free-form string referencing either upstream `[SA-*-ANOM-*]` or a distillation detection like `"Distillation: Q2 volume spike, NVDA, 3.2σ"`), the three-way assessment invariant, and the `Anomalies deferred` header semantics.
* `prompts/analysis/adaptive_researcher.md` § `<output_contract>` — the exact wire shape the parser will read; the model fields must round-trip cleanly through parser → model → renderer for the diagnostic archive.
* `src/alphamind/analysis/qualitative_research/models.py` — sibling-package data model to mirror. Note the `model_validator(mode="after")` patterns for invariant enforcement and the `Field(pattern=r"^QR-\\d+$")` discipline for reference-ID format checks.
* `src/alphamind/analysis/domain_researchers/models.py` — sibling for `Anomaly` / `Finding` / `SectorBrief`; mirrors the `frozen=True` + tuple-of-records convention.
* `src/alphamind/analysis/_shared.py` — imports `Sector` for the per-thread sector field.

## Depends on

* <issue id="5043b367-d100-413e-b7f0-3e1227963080">ALP-254</issue> (story 01) — package skeleton + `_shared.py` audit confirming no new shared types are needed for this story.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/models.py` and `tests/analysis/adaptive_research/test_models.py`.

### 1\. Closed-set enums

Two `enum.StrEnum` types local to this module:

```python
class Assessment(StrEnum):
    """Three-way verdict on an investigation thread."""

    SIGNAL = "signal"
    NOISE = "noise"
    INCONCLUSIVE = "inconclusive"


class Confidence(StrEnum):
    """Agent's confidence in its own assessment."""

    HIGH = "high"
    MODERATE = "moderate"
    LOW = "low"
```

Display strings match the wire format from the design doc / prompt — lowercase, no formatting.

### 2\. `InvestigationThread`

```python
class InvestigationThread(BaseModel, frozen=True):
    """A single investigation thread within an AdaptiveBrief.

    `thread_id` follows `AR-{N}` where N is a positive integer.
    Conditional fields are enforced by `_assessment_invariant`:
      - SIGNAL  → implication, strengthens, weakens REQUIRED; dismissal_reason, missing MUST be None.
      - NOISE   → dismissal_reason REQUIRED; implication, strengthens, weakens, missing MUST be None.
      - INCONCLUSIVE → missing REQUIRED; implication, strengthens, weakens, dismissal_reason MUST be None.
    """

    thread_id: str = Field(pattern=r"^AR-\\d+$")
    trigger: str = Field(min_length=1)
    question: str = Field(min_length=1)
    tickers: tuple[str, ...]
    sector: Sector
    tools_used: tuple[str, ...]
    findings: tuple[str, ...]
    assessment: Assessment
    confidence: Confidence

    # Conditional fields — exactly one set is populated per assessment.
    implication: str | None = None
    strengthens: tuple[str, ...] | None = None
    weakens: tuple[str, ...] | None = None
    dismissal_reason: str | None = None
    missing: str | None = None
```

`tickers` may be an empty tuple — some anomalies (macro/funding-stress) have no specific ticker.

`tools_used` may be an empty tuple — a thread that triaged purely on the trigger payload without tool calls is valid (rare but allowed).

`findings` may be an empty tuple — a `noise` thread can dismiss based on the trigger characteristics alone.

`strengthens` and `weakens` are tuples (not strings) so the validator can iterate them for Layer-3 referential resolution. The wire format `none` parses to an empty tuple `()`, NOT to `None` — `None` means "not applicable to this assessment", `()` means "applicable, explicitly empty".

Add a `model_validator(mode="after")` named `_assessment_invariant` that enforces the conditional discipline above, raising `ValueError` with a message naming which field violated the rule.

### 3\. `AdaptiveBrief`

```python
class AdaptiveBrief(BaseModel, frozen=True):
    """Complete output of the adaptive researcher for one invocation.

    Invariants enforced here:
      - `threads_investigated_count` equals `len(threads)` (mechanical consistency check).
      - `anomalies_triaged_count >= threads_investigated_count` (you can't investigate more than you triaged).
      - `anomalies_deferred` is a (possibly empty) tuple of strings — the wire literal "none" parses to `()`.
      - Empty `threads` is valid (quiet cycle); the design doc explicitly names zero-thread output as correct.
    """

    invocation_id: str
    threads_investigated_count: int = Field(ge=0)
    anomalies_triaged_count: int = Field(ge=0)
    anomalies_deferred: tuple[str, ...]
    threads: tuple[InvestigationThread, ...]
```

Add a `model_validator(mode="after")` named `_brief_invariants` enforcing both numeric invariants above.

### 4\. Module exports

```python
__all__ = [
    "Assessment",
    "Confidence",
    "InvestigationThread",
    "AdaptiveBrief",
]
```

### Out of scope

* The parser that produces `AdaptiveBrief` from raw text — story 03b.
* The structural validator that checks Layer-2 + Layer-3 invariants — story 03c.
* Layer-3 referential resolution of `strengthens` / `weakens` against upstream briefs — story 03c (the validator owns the upstream-context walk).

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.models import AdaptiveBrief, InvestigationThread, Assessment, Confidence` resolves cleanly.
- [ ] `Assessment("signal")`, `Assessment("noise")`, `Assessment("inconclusive")` resolve; any other string raises.
- [ ] `Confidence("high")`, `Confidence("moderate")`, `Confidence("low")` resolve; any other string raises.
- [ ] Constructing an `InvestigationThread` with `assessment=SIGNAL` requires `implication`, `strengthens`, and `weakens` to be non-`None`; omitting any raises a `ValidationError` with a message naming the missing field.
- [ ] Constructing an `InvestigationThread` with `assessment=NOISE` requires `dismissal_reason` non-`None`; raises if missing or if any signal-only field is set.
- [ ] Constructing an `InvestigationThread` with `assessment=INCONCLUSIVE` requires `missing` non-`None`; raises if missing or if any other conditional field is set.
- [ ] `thread_id="AR-1"` validates; `thread_id="QR-1"` raises pattern mismatch.
- [ ] An empty `strengthens=()` is valid (means "explicitly no upstream strengthens"); `strengthens=None` is invalid for a SIGNAL thread.
- [ ] `AdaptiveBrief(threads=())` validates with `threads_investigated_count=0` and `anomalies_triaged_count=0` (quiet cycle).
- [ ] `AdaptiveBrief` raises when `threads_investigated_count != len(threads)` or when `anomalies_triaged_count < threads_investigated_count`.
- [ ] `tests/analysis/adaptive_research/test_models.py` covers each acceptance criterion above and passes under `uv run pytest tests/analysis/adaptive_research/ -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` all clean.

## Verification

Run `uv run pytest tests/analysis/adaptive_research/test_models.py -n auto`. Inspect `model_dump()` on a constructed `AdaptiveBrief` to confirm all fields serialize as expected (tuples become JSON arrays, enums become their string values).
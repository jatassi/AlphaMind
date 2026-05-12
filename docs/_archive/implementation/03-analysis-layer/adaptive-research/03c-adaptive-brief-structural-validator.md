# 03c — Adaptive brief structural validator

## Goal

Implement the hand-written structural validator for the adaptive researcher's output — the Layer-2 (structural) + Layer-3 (referential integrity) producer-side checks from `docs/design/testing/llm-output-validation.md` for this agent class. Runs on an `AdaptiveBrief` (already parsed by story 03b) and reports the structural and referential failures that schema-style validation would catch on JSON-output agents. Returns the *full* error list so the diagnostic record receives the complete inventory; the harness (story 05) surfaces only the first error in the corrective-retry message. The Layer-3 referential resolution against upstream briefs is the structural check unique to this agent — qualitative-research's validator has no equivalent.

## Reading

* `docs/design/testing/llm-output-validation.md` § Layer 2, § Layer 3, § Per-agent surface mapping (Adaptive researcher row), § Reference-ID taxonomy — the canonical contract this story implements.
* `docs/design/03-analysis-layer/adaptive-research.md` § Output § Schema design rationale — names the cross-reference semantics for `Strengthens` / `Weakens` (any upstream prefix family is permitted; `none` is the explicit empty case).
* `prompts/analysis/adaptive_researcher.md` § `<output_contract>` — the cross-reference families the agent is permitted to cite (`SA-{SECTOR}-*`, `SA-{SECTOR}-ANOM-*`, `SA-{SECTOR}-TC-*`, `QR-*`, `QR-CW-*`, `CR-*`).
* `src/alphamind/analysis/qualitative_research/validation.py` — sibling-package validator pattern. Mirror: `ValidationError` and `ValidationResult` shape, the `_check_*` private helper convention, the `_CHECKS` list pattern, the public entry point that aggregates errors from all checks.
* `src/alphamind/analysis/adaptive_research/models.py` § `AdaptiveBrief`, `InvestigationThread`, `Assessment` — types this validator runs on.
* `src/alphamind/analysis/domain_researchers/models.py` § `SectorBrief`, `Anomaly`, `Finding`, `ThesisCandidate`, `SECTOR_PREFIX` — upstream context for Layer-3 resolution; carries the `SA-{SECTOR}-*`, `SA-{SECTOR}-ANOM-*`, `SA-{SECTOR}-TC-*` ID families.
* `src/alphamind/analysis/qualitative_research/models.py` § `QualitativeBrief`, `NarrativeThread`, `CatalystWatch` — upstream context for `QR-*` and `QR-CW-*` resolution.
* `src/alphamind/distillation/correlation_brief.py` § `CorrelationRegimeBrief` — upstream context for `CR-*` resolution. The `reference_index: dict[str, str]` keys are the canonical `CR-N` IDs.

## Depends on

* <issue id="b01cd510-80fc-44fb-814b-3aad59e877d6">ALP-255</issue> (story 02) — the data model the validator runs on.

## Scope

In scope under `src/alphamind/analysis/adaptive_research/validation.py` and `tests/analysis/adaptive_research/test_validation.py`.

### 1\. Public types

Mirror the qualitative-research types verbatim:

```python
class ValidationError(BaseModel, frozen=True):
    field_path: str
    rule: str
    message: str


class ValidationResult(BaseModel, frozen=True):
    is_valid: bool
    errors: tuple[ValidationError, ...]
```

### 2\. `validate_adaptive_brief` entry point

Per parent issue resolution (E):

```python
def validate_adaptive_brief(
    brief: AdaptiveBrief,
    *,
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> ValidationResult:
    """Run Layer-2 + Layer-3 checks on `brief` and aggregate the error list.

    Layer-3 referential resolution: every Strengthens/Weakens reference in every
    SIGNAL thread must resolve to an ID present in one of the three upstream briefs.

    Parameters
    ----------
    brief: the parsed AdaptiveBrief.
    sector_briefs: the three domain-researcher SectorBriefs from the same invocation.
        Sources for SA-{SECTOR}-N (findings), SA-{SECTOR}-ANOM-N (anomalies),
        SA-{SECTOR}-TC-N (thesis candidates).
    qualitative_brief: the baseline QualitativeBrief from the same invocation.
        Sources for QR-N (narrative threads) and QR-CW-N (catalyst watches).
    correlation_regime_brief: the distillation-layer correlation/regime brief.
        Sources for CR-N (the keys of `reference_index`).
    """
```

### 3\. Layer-2 checks

* `_check_invocation_id_not_empty` — invocation_id is non-empty string.
* `_check_threads_sequential_indexing` — `[t.thread_id for t in brief.threads]` form `AR-1, AR-2, ..., AR-N` with no gaps. Reuse the qualitative-research `_check_sequential_indexing` helper (or inline a structurally-equivalent version — copying is fine; no need to factor a `_shared` validation utility this story).
* `_check_thread_id_prefix` — every `thread_id` matches `^AR-\\d+$`. (Pydantic's `Field(pattern=...)` already catches this at construction; this check is belt-and-suspenders for parser bypass paths.)
* `_check_threads_count_matches_header` — `brief.threads_investigated_count == len(brief.threads)`. (Model invariant; check is redundant but lives here for symmetry with the threshold checks.)
* `_check_anomalies_triaged_at_least_investigated` — `brief.anomalies_triaged_count >= brief.threads_investigated_count`. (Same: redundant model invariant.)
* `_check_findings_distinct_within_thread` — within each thread, `findings` entries are byte-distinct (no duplicate finding lines). Mirror `_check_evidence_lines_distinct_within_thread`.
* `_check_tools_used_distinct_within_thread` — within each thread, `tools_used` entries are byte-distinct.
* `_check_tickers_in_universe` — every `thread.tickers` member is in the asset universe `frozenset[str]` parameter when it's supplied. (Optional parameter, like the qualitative-research validator. Keep signature additive: `validate_adaptive_brief(... , universe: frozenset[str] | None = None)`.)

### 4\. Layer-3 checks

The unique-to-adaptive checks. One pre-pass builds the resolution context; per-thread checks then resolve.

`_build_reference_universe`:

```python
def _build_reference_universe(
    sector_briefs: tuple[SectorBrief, ...],
    qualitative_brief: QualitativeBrief,
    correlation_regime_brief: CorrelationRegimeBrief,
) -> frozenset[str]:
    """Walk every upstream brief once and return the set of valid reference IDs.

    Includes:
      - SA-{SECTOR}-N from each SectorBrief.findings[*].finding_id
      - SA-{SECTOR}-ANOM-N from each SectorBrief.anomalies[*].anomaly_id
      - SA-{SECTOR}-TC-N from each SectorBrief.thesis_candidates[*].thesis_candidate_id
      - QR-N from each QualitativeBrief.threads[*].thread_id
      - QR-CW-N from each QualitativeBrief.catalyst_watches[*].catalyst_id
      - CR-N from CorrelationRegimeBrief.reference_index keys
    """
```

`_check_strengthens_weakens_resolve`:

```python
def _check_strengthens_weakens_resolve(
    brief: AdaptiveBrief,
    valid_ids: frozenset[str],
) -> Iterable[ValidationError]:
    """For each SIGNAL thread, every Strengthens / Weakens reference must be in `valid_ids`.

    NOISE and INCONCLUSIVE threads have None for these fields and are skipped.
    """
```

For each SIGNAL thread, iterate its `strengthens` tuple then its `weakens` tuple; for each reference ID not in `valid_ids`, emit a `ValidationError` with:

* `field_path = f"threads[{i}].strengthens[{j}]"` or `field_path = f"threads[{i}].weakens[{j}]"`
* `rule = "referential_integrity"`
* `message = f"reference {ref!r} does not resolve to any upstream brief ID in this invocation"`

### 5\. Wire-up

```python
_LAYER_2_CHECKS: list[Callable[[AdaptiveBrief], Iterable[ValidationError]]] = [
    _check_invocation_id_not_empty,
    _check_threads_sequential_indexing,
    _check_thread_id_prefix,
    _check_threads_count_matches_header,
    _check_anomalies_triaged_at_least_investigated,
    _check_findings_distinct_within_thread,
    _check_tools_used_distinct_within_thread,
]


def validate_adaptive_brief(
    brief, *, sector_briefs, qualitative_brief, correlation_regime_brief, universe=None
) -> ValidationResult:
    errors: list[ValidationError] = []
    for check in _LAYER_2_CHECKS:
        errors.extend(check(brief))
    if universe is not None:
        errors.extend(_check_tickers_in_universe(brief, universe))
    valid_ids = _build_reference_universe(sector_briefs, qualitative_brief, correlation_regime_brief)
    errors.extend(_check_strengthens_weakens_resolve(brief, valid_ids))
    return ValidationResult(is_valid=not errors, errors=tuple(errors))
```

### 6\. Module exports

```python
__all__ = ["ValidationError", "ValidationResult", "validate_adaptive_brief"]
```

### Out of scope

* The parser that produces `AdaptiveBrief` — story 03b.
* Any LLM call or harness logic — story 05.
* Editing the qualitative-research validator to extract a shared `_check_sequential_indexing` helper into a `_shared` validation utility module — copy the pattern locally; refactoring across both validators is a separate cleanup, not part of this story.

## Acceptance criteria

- [ ] `from alphamind.analysis.adaptive_research.validation import validate_adaptive_brief, ValidationError, ValidationResult` resolves cleanly.
- [ ] A well-formed brief whose every `Strengthens` / `Weakens` reference resolves to an upstream ID returns `is_valid=True` with `errors=()`.
- [ ] A SIGNAL thread citing a non-existent `SA-TECH-99` in `Strengthens` produces a `ValidationError` with `rule="referential_integrity"` and `field_path` naming the offending element.
- [ ] A SIGNAL thread citing a non-existent `QR-99` in `Weakens` produces a `ValidationError` with the appropriate `field_path`.
- [ ] A NOISE thread with `strengthens=None` does NOT produce a referential-integrity error (the field is not applicable).
- [ ] An INCONCLUSIVE thread with `strengthens=None` does NOT produce a referential-integrity error.
- [ ] A `Strengthens: ()` (empty tuple — the wire `none`) on a SIGNAL thread does NOT produce a referential-integrity error (explicit empty is valid).
- [ ] A brief with two duplicate `findings` entries within one thread produces a `ValidationError` with `rule="findings_distinct_within_thread"`.
- [ ] A brief with two duplicate `tools_used` entries within one thread produces a `ValidationError` with `rule="tools_used_distinct_within_thread"`.
- [ ] A brief with non-sequential `thread_id`s (`AR-1`, `AR-3`) produces a `ValidationError` with `rule="threads_sequential_indexing"`.
- [ ] When `universe` is supplied and a thread cites a ticker not in the universe, a `ValidationError` with `rule="tickers_in_universe"` is produced; when `universe=None` no such check runs.
- [ ] `_build_reference_universe` returns a `frozenset[str]` containing every ID from all six families above; verify by constructing fixture upstream briefs with known IDs and asserting set equality.
- [ ] All checks aggregate into the returned `ValidationResult.errors` tuple; `is_valid=True` iff `errors=()`.
- [ ] `tests/analysis/adaptive_research/test_validation.py` covers each acceptance criterion and passes under `uv run pytest tests/analysis/adaptive_research/test_validation.py -n auto`.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy` clean.

## Verification

Run the validator against a fixture `(AdaptiveBrief, tuple[SectorBrief, ...], QualitativeBrief, CorrelationRegimeBrief)` quartet that exercises every reference-ID family. Construct a negative test that perturbs each Strengthens/Weakens reference one at a time and asserts each perturbation produces exactly one new `ValidationError`.
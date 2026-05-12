## Goal

Implement `run_domain_researchers` — the parallel orchestrator that fans out three per-sector runner calls (story 10) under `asyncio.gather`, propagates the first failure under fail-closed semantics, and aggregates the three results into a `DomainResearchersOutput` value object the synthesizer consumes downstream.

This story replaces the prior body that was accidentally a copy of story 06.

## Reading

* `docs/design/03-analysis-layer/README.md` — sequencing context (domain researchers run in parallel).
* `docs/design/llm-agent-failure-handling.md` — fail-closed policy: any sector failure aborts the whole invocation; the orchestrator propagates the first exception and lets the rest finish for diagnostic preservation but does not return a partial result.
* `docs/design/mid-pipeline-failure-handling.md` — broader pipeline contract; the orchestrator's caller (the pipeline-level runner) catches `HarnessFailure` and treats it as an invocation abort.
* `src/alphamind/analysis/_shared.py` (story 02) — `Sector`, `_SECTOR_AUDIENCE_MAP`, `TokensUsed`.
* Story 10 (<issue id="166e4acf-47b5-4b53-a35d-635d3ee0bef6">ALP-191</issue>) — `run_domain_researcher`, `DomainResearcherResult`.
* Story 07 (<issue id="ad9979d1-7ff8-426b-8431-2760050cdfbf">ALP-198</issue>) — `HarnessFailure` hierarchy this orchestrator propagates.
* `src/alphamind/distillation/orchestrator.py` § `DistillationOutputs` — the upstream value object whose `sector_outputs` and `universal_regime_label` fields this orchestrator consumes.

## Depends on

* **10** (<issue id="166e4acf-47b5-4b53-a35d-635d3ee0bef6">ALP-191</issue>) — `run_domain_researcher`, `DomainResearcherResult`.

## Scope — [orchestrator.py](<http://orchestrator.py>)

Under `src/alphamind/analysis/domain_researchers/orchestrator.py`:

```python
class DomainResearchersOutput(BaseModel, frozen=True):
    """Aggregated result of the parallel three-sector orchestrator."""
    invocation_id: str
    as_of: datetime
    tech_semis: DomainResearcherResult
    financials: DomainResearcherResult
    energy: DomainResearcherResult
    total_tokens_used: TokensUsed     # sum across the three sectors
    total_wall_clock_seconds: float   # max() of the three (parallel)
    total_retry_count: int            # sum across the three


async def run_domain_researchers(
    *,
    invocation_id: str,
    as_of: datetime,
    distillation_outputs: DistillationOutputs,
    session: Session,
    agents_config: AgentRegistry,
    archive_root: Path | None = None,
) -> DomainResearchersOutput:
    ...
```

## Scope — composition

For each sector, build the runner-call coroutine: extract `distillation_text` from `distillation_outputs.sector_outputs[_SECTOR_AUDIENCE_MAP[sector]].text`; extract `regime_payload` from `distillation_outputs.universal_regime_label` (a dict); call `run_domain_researcher(sector, invocation_id, as_of, distillation_text, regime_payload, session, agents_config, archive_root)`.

Run all three coroutines under `asyncio.gather(..., return_exceptions=False)` so the first raised `HarnessFailure` propagates immediately. Aggregate the three `DomainResearcherResult`s into `DomainResearchersOutput` once all three resolve cleanly.

## Scope — fail-closed propagation

`asyncio.gather(..., return_exceptions=False)` cancels in-flight coroutines as soon as one raises. The orchestrator does NOT swallow the exception and does NOT return a partial result. Per `llm-agent-failure-handling.md`, the caller aborts the invocation. Document at the orchestrator's docstring why `return_exceptions=False` is the correct choice (the alternative — `True` — would permit a partial-result code path that the fail-closed policy forbids).

## Scope — token + wall-clock aggregation

`total_tokens_used`: field-wise sum (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`) across the three results. `total_wall_clock_seconds`: `max()` of the three per-sector `wall_clock_seconds` (parallel execution; the orchestrator's wall-clock is the longest leg). `total_retry_count`: sum of the three `retry_count`s.

## Scope — sector roster determinism

The result fields `tech_semis`, `financials`, `energy` are positional and deterministic. The aggregation reads from a fixed list `[Sector.TECH_SEMIS, Sector.FINANCIALS, Sector.ENERGY]` so the field-naming convention does not depend on `asyncio.gather`'s internal completion order (it does preserve order, but spelling out the roster makes the determinism contract visible).

## Scope — unit tests

Under `tests/analysis/domain_researchers/test_orchestrator.py`:

* Happy path: stub `run_domain_researcher` to return canned `DomainResearcherResult`s for each sector; assert `DomainResearchersOutput` carries all three.
* Token aggregation: per-sector `TokensUsed(input=100, output=50, cache_read=0, cache_write=0)` × 3 → `total_tokens_used.input_tokens == 300`, etc.
* Wall-clock aggregation: per-sector `wall_clock_seconds` of `[5.0, 12.0, 8.0]` → `total_wall_clock_seconds == 12.0` (max, not sum).
* Retry-count aggregation: `[0, 1, 0]` → `total_retry_count == 1`.
* Fail-closed: stub one sector to raise `HarnessFailure`; assert the exception propagates and no partial result is returned.
* Fail-closed: when one sector raises, the other two coroutines are awaited or canceled (the diagnostic record from story 07 still gets written for whichever ones started); the orchestrator does not swallow the failure.
* The `distillation_text` argument passed to each runner matches `distillation_outputs.sector_outputs[<that sector's audience>].text`.
* The `regime_payload` argument passed to each runner is `distillation_outputs.universal_regime_label`.
* The orchestrator does NOT pre-render or wrap the regime payload — it passes through the dict per the story 08 contract.

## Out of scope

* The runner itself — story 10.
* Per-sector retry / fallback logic — fail-closed; one failure aborts.
* Cost tracking beyond simple token aggregation (e.g., dollar costs) — that's the cost-accounting layer's territory, not this orchestrator.
* Parallel-execution beyond `asyncio.gather` — no thread pool, no process pool; the SDK's own concurrency is the mechanism.
* Persistence of `DomainResearchersOutput` — the synthesizer consumes it in-process; archival is the diagnostic preservation in story 07's harness, not this orchestrator.

## Notes

The orchestrator is intentionally thin — it composes existing primitives and aggregates. The runner (story 10) carries the per-sector composition; this story is the parallelism + aggregation layer.

`asyncio.gather` preserves argument order in its result list, so iterating `[Sector.TECH_SEMIS, Sector.FINANCIALS, Sector.ENERGY]` and zipping with the gathered results gives a deterministic mapping from sector to result.

The `archive_root` argument is forwarded to each `run_domain_researcher` call (which forwards to the harness's diagnostic-archive write). Tests pass `tmp_path`; production passes `None` (defaulting to the platform-default location).

The choice of `return_exceptions=False` is intentional: the alternative permits a partial-result code path, which the fail-closed runtime policy does not allow. Documenting the choice in the orchestrator's docstring prevents a future "improve robustness" PR from flipping it.

## Acceptance criteria

- [ ] `DomainResearchersOutput` is a frozen Pydantic model with the documented fields.
- [ ] `run_domain_researchers(invocation_id, as_of, distillation_outputs, session, agents_config, archive_root)` returns a `DomainResearchersOutput`.
- [ ] All three sectors run under `asyncio.gather(..., return_exceptions=False)`.
- [ ] `total_tokens_used` is the field-wise sum across the three sectors.
- [ ] `total_wall_clock_seconds` is the `max()` of the three (not the sum).
- [ ] `total_retry_count` is the sum across the three.
- [ ] One-sector `HarnessFailure` propagates; no partial result is returned.
- [ ] Each runner receives `distillation_text` from the matching sector's `SectorOutput.text` and `regime_payload` from `universal_regime_label`.
- [ ] The result's positional fields (`tech_semis`, `financials`, `energy`) match the runner outputs deterministically.
- [ ] Stubbed dependencies in tests; no real SDK calls.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` all pass.

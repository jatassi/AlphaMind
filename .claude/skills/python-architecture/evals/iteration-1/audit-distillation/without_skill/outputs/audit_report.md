# Audit — alphamind.distillation

**Scope:** Medium-depth audit (~30 min) of layout, data modelling, runtime concerns, and testing.

**Date:** 2026-05-06 | **Codebase:** 54 source files, 61 test files (9309 SLOC distillation core)

**Assumptions:** 
- No re-litigation of SQLite or APScheduler architectural choices
- Focus on issues blocking integration with data layer inputs and analysis layer outputs
- Test coverage is measured against story-driven acceptance criteria, not line coverage

---

## Findings (Priority Order)

### 1. CRITICAL: Concurrent Session Flushing — Phase 2 Serialization Workaround Fragile

**Location:** `orchestrator.py:706-770` (Phase 2 per-category dispatchers)

**Issue:**
The orchestrator runs Q1/Q3/Q6/Q7/Q12/qualitative_derived sequentially via `asyncio.to_thread`, even though they are logically independent. The reason: Q3 and Q6 issue `session.flush()` calls inside `refresh_atm_iv_baselines()` and related helpers, triggering "Session is already flushing" `InvalidRequestError` when run concurrently. 

```python
pair_correlations = await asyncio.to_thread(compute_pair_correlations, ...)
# Then Q1, Q3, Q6, Q7, Q12, qualitative in sequence (not parallel)
per_category_blocks: tuple[list[OutputBlock], ...] = (
    await asyncio.to_thread(_compute_q1_blocks, ...),
    await asyncio.to_thread(_compute_q3_blocks, ...),  # Flushes internally
    await asyncio.to_thread(_compute_q6_blocks, ...),  # Flushes internally
    ...
)
```

**Impact:**
- Phase 2 is ~25–40% of total distillation runtime; sequential execution adds unnecessary latency
- The flush calls are internal to Q3/Q6 helpers (`atm_iv_baseline.py:216`, `intra_sector_correlation.py:191`), not exposed in the public API
- Future additions to Q3/Q6 categories that also flush will compound serialization

**Root Cause:**
SQLAlchemy's `Session.flush()` holds a write lock; concurrent `asyncio.to_thread` workers on the same session violate the assumption of non-concurrent access.

**Fix Strategy (Two Tiers):**

*Tier 1 (Immediate, No API Change):* Wrap Q3 and Q6 entry points with a session-isolation boundary so the flush is confined to a transaction context and released before the `asyncio.to_thread` returns. This requires moving the flush calls from helper functions to the entry points and ensuring they complete inside `to_thread`.

*Tier 2 (Longer-term):* Refactor Q3/Q6 helpers to use SQLAlchemy's `Session.begin_nested()` (SAVEPOINT) instead of `flush()` for intermediate writes, or batch their writes until the end and flush once in the main thread.

**Test Coverage:**
- `tests/distillation/external/test_orchestrator.py:370–388` exercises end-to-end but does not verify parallelization
- Add a parametrized test that runs Q3 + Q6 concurrently and asserts no `InvalidRequestError`

---

### 2. HIGH: Implicit Tuple Ordering Assumption in Sector Assembly

**Location:** `orchestrator.py:820–839`

**Issue:**
The orchestrator pre-computes sector rosters and sector assembly results, then zips them:

```python
sector_audiences = tuple(DOMAIN_RESEARCHER_BY_AUDIENCE)  # Unordered dict iteration
sector_rosters: dict[OutputAudience, tuple[str, ...]] = {
    audience: load_sector_roster(...)
    for audience in sector_audiences
}
sector_assembly_results: list[SectorOutput] = await asyncio.gather(
    *(
        asyncio.to_thread(assemble_sector_output, audience=audience, ...)
        for audience in sector_audiences
    )
)
sector_outputs: dict[OutputAudience, SectorOutput] = dict(
    zip(sector_audiences, sector_assembly_results, strict=True)
)
```

The `strict=True` guard catches length mismatches but not ordering mismatches if a bug silently reorders `sector_audiences` or `sector_assembly_results`.

**Impact:**
- Low runtime risk (Python 3.7+ dict order is guaranteed, and `tuple(dict)` is stable)
- Moderate correctness risk: if a refactor accidentally sorts the dict keys or reorders the gather results, the zip will silently pair the wrong rosters with the wrong outputs

**Root Cause:**
Reliance on implicit ordering across three separate data structures (dict keys, dict comprehension iteration, gather result list) without an invariant guard.

**Fix:**
Restructure using a named tuple or dataclass that holds both roster and assembly result:

```python
@dataclass(frozen=True)
class SectorAssemblyWork:
    audience: OutputAudience
    roster: tuple[str, ...]
    result: SectorOutput | None  # Populated after gather

sector_work = [
    SectorAssemblyWork(audience, rosters[audience], None)
    for audience in DOMAIN_RESEARCHER_BY_AUDIENCE
]
# ... populate results after gather ...
sector_outputs = {w.audience: w.result for w in sector_work}
```

**Test Coverage:**
- Current tests verify outputs are non-empty and file-written but not that sector rosters map to the correct sector outputs
- Add a test that verifies each `SectorOutput.tickers` matches the pre-computed roster for that audience

---

### 3. HIGH: Missing Null-Safety Check for Latest-Snapshot Window Functions

**Location:** `contract_scope.py:82–92`

**Issue:**
The contract scope resolution uses a window-function subquery to select the latest snapshot per contract:

```python
latest_snap = (
    select(ranked.c.contract_id, ranked.c.volume_24h_usd).where(ranked.c.rn == 1).subquery()
)

rows = session.execute(
    select(...).join(
        latest_snap,
        PredictionMarketContracts.contract_id == latest_snap.c.contract_id,
    )
    ...
)
```

If `latest_snap` returns no rows (i.e., no contract has a snapshot), the join becomes an implicit INNER JOIN that drops contracts with no snapshots. The downstream loop handles missing volumes:

```python
for row in rows:
    volume = row.volume_24h_usd
    if volume is None:
        continue
```

But this defense is incomplete because the join already filtered them out — the check is unreachable.

**Impact:**
- Contracts without snapshots silently exit scope without explanation
- Downstream analysis does not know whether a contract is out of scope due to low volume or due to missing snapshots
- Affects audit trails and reproducibility

**Root Cause:**
The join should be a LEFT JOIN to preserve contracts with no snapshots, and the volume-filtering logic should explicitly log dropped contracts.

**Fix:**

```python
latest_snap = (
    select(ranked.c.contract_id, ranked.c.volume_24h_usd).where(ranked.c.rn == 1).subquery()
)

rows = session.execute(
    select(...)
    .outerjoin(  # Changed to LEFT JOIN
        latest_snap,
        PredictionMarketContracts.contract_id == latest_snap.c.contract_id,
    )
    ...
)

in_scope: list[str] = []
dropped_contracts: list[tuple[str, str]] = []
for row in rows:
    volume = row.volume_24h_usd
    if volume is None:
        dropped_contracts.append((row.contract_id, "no_snapshot"))
        continue
    ...
logger.debug("Dropped %d contracts: %s", len(dropped_contracts), dropped_contracts)
```

**Test Coverage:**
- Add a test with a contract that has no snapshots and verify it is logged as dropped

---

### 4. HIGH: OutputBlock Audience Set Empty-Check Only Validated at Runtime

**Location:** `output.py:86–133`

**Issue:**
The `OutputBlock` dataclass enforces that `audience` (a `frozenset[OutputAudience]`) must be non-empty:

```python
@dataclass(frozen=True)
class OutputBlock:
    audience: frozenset[OutputAudience]
    ...
    
    def __post_init__(self) -> None:
        if not self.audience:
            raise ValueError(f"OutputBlock {self.block_id!r}: audience must declare at least one consumer")
```

This check is only triggered at construction time. There is no static type hint or dataclass validation that prevents constructing blocks with empty audiences at the call site before the error is raised.

**Impact:**
- Subtle runtime error (raises after construction but not at call site where bug was made)
- Difficult to debug if a category emits an empty-audience block during a late-stage indicator computation
- No way to catch this in type checking (Mypy allows `frozenset[OutputAudience]()`)

**Root Cause:**
`frozenset` is mutable-construction-agnostic; there's no type-level way to enforce non-emptiness without a custom type or a validator.

**Fix:**

Create a custom type that enforces non-empty at construction:

```python
from typing import NewType

NonEmptyAudienceSet = NewType("NonEmptyAudienceSet", frozenset[OutputAudience])

def make_audience_set(*audiences: OutputAudience) -> NonEmptyAudienceSet:
    if not audiences:
        raise ValueError("audience set must contain at least one member")
    return NonEmptyAudienceSet(frozenset(audiences))

@dataclass(frozen=True)
class OutputBlock:
    audience: NonEmptyAudienceSet  # Type hint prevents empty sets
    ...
    # Remove __post_init__ check
```

Alternatively, use Pydantic v2's `Field(min_items=1)` in a dataclass validator.

**Test Coverage:**
- Add a direct test that tries to construct an `OutputBlock` with an empty `frozenset` and asserts the ValueError is raised
- Add a category-level audit that walks every emitted block and verifies non-empty audience

---

### 5. MEDIUM: Per-Ticker Payload Filtering May Drop Valid Blocks Silently

**Location:** `sector_assembly.py:159–282`

**Issue:**
When assembling per-sector outputs, blocks whose `per_ticker` payload contains only off-roster tickers are filtered out entirely:

```python
def _restrict_per_ticker_payload(block: OutputBlock, roster: frozenset[str]) -> OutputBlock | None:
    """Return ``block`` with off-roster tickers pruned, or ``None`` if empty."""
    if per_ticker is None:
        return block
    # Filter to roster
    restricted = {ticker: per_ticker[ticker] for ticker in per_ticker if ticker in roster}
    if not restricted:
        return None  # Silently dropped
```

Then in the assembly:

```python
restricted = _restrict_per_ticker_payload(block, sector_roster)
if restricted is None:
    logger.debug("Filtered out block %s: no on-roster tickers", block.block_id)
    continue
```

The logging is at DEBUG level; for a sector with tickers that have no indicators this invocation, blocks may disappear without operator visibility.

**Impact:**
- Operators cannot easily debug why a sector's expected blocks are absent
- Invocation archives will show different block counts across sectors without explanation
- Difficult to distinguish between "no indicators were computed" and "indicators were computed but filtered out"

**Root Cause:**
The assumption that every sector roster will have on-sector tickers for every indicator block is not validated. It's a reasonable assumption, but violations should be obvious.

**Fix:**
Promote the logging to INFO level when blocks are dropped, and include context (sector, block_id, how many tickers were in the block, how many are in roster):

```python
if restricted is None:
    logger.info(
        "Sector %s: block %s dropped (had %d tickers, sector roster %d, "
        "intersection empty)",
        sector_label,
        block.block_id,
        len(per_ticker),
        len(sector_roster),
    )
    continue
```

**Test Coverage:**
- Add a test where a block carries tickers not in a sector's roster and verify the INFO log is emitted
- Add integration test that validates block count across sectors is documented in the distillation output

---

### 6. MEDIUM: Regime Bootstrap Snapshot Carries Placeholder Values Without Upstream Visibility

**Location:** `orchestrator.py:504–542`

**Issue:**
When VIX (`VIXCLS`) is unavailable, the regime snapshot is populated with placeholder zeros:

```python
if vix is None:
    snapshot = RegimeSnapshot(
        vix_level=0.0,
        vx1_minus_vix=0.0,
        vvix_percentile=_VVIX_PERCENTILE_PLACEHOLDER,  # 50.0
        realized_vol_5d=0.0,
        realized_vol_20d=0.0,
        ...
    )
    return snapshot, "regime: VIXCLS observation missing"
```

The bootstrap reason is attached to the regime block, but upstream callers of `resolve_prediction_market_scope` and other Phase 1 operations do not know the regime will bootstrap, so they may emit blocks expecting a calibrated regime that won't appear.

**Impact:**
- Regime blocks will always carry `bootstrap` calibration state if VIX is unavailable, but this is not advertised to Phase 1 and Phase 2 producers
- Analysis agents downstream receive a regime block with an explanation, but sector agents may have emitted blocks that cite the regime context, creating a mismatch

**Root Cause:**
The regime bootstrap is determined late in Phase 3, after Phase 1/2 have completed. There's no early signaling to upstream phases.

**Fix:**
Add a pre-phase check (Phase 0) that validates macro dependencies and logs a WARNING if key series are missing:

```python
def _validate_macro_prerequisites(session: Session) -> list[str]:
    """Return list of missing macro series critical for calibrated output."""
    missing: list[str] = []
    required = ["VIXCLS", "VVIXCLS", "VX1"]  # Example
    for series_id in required:
        if _latest_macro_value(session, series_id) is None:
            missing.append(series_id)
    return missing

# In run_external_distillation, before Phase 1:
missing_series = await asyncio.to_thread(_validate_macro_prerequisites, session)
if missing_series:
    logger.warning(
        "Pre-distillation: missing macro series %s — regime block will bootstrap",
        missing_series,
    )
```

Then Phase 1/2 can log their own awareness, and the archive will contain the warning.

**Test Coverage:**
- Add a test fixture with missing VIX and verify the regime block is tagged `bootstrap` and a log warning appears
- Add an integration test that validates invocation logs contain a pre-phase warning when macro series are missing

---

### 7. MEDIUM: Welford's Algorithm Eviction (baselines.py) Not Property-Tested

**Location:** `baselines.py:145–177`

**Issue:**
The rolling-baseline refresh uses Welford's algorithm to incrementally update mean and variance. The eviction path (when observations slide out of the window) is implemented via `_welford_evict`, but it has no property-based tests:

```python
def _welford_evict(
    *,
    prior_n: int,
    prior_mean: float,
    prior_m2: float,
    evicted_values: Sequence[float],
) -> tuple[int, float, float]:
    """Evict oldest values from a rolling window."""
    n = prior_n
    mean = prior_mean
    m2 = prior_m2
    for value in evicted_values:
        if n <= 1:
            return 0, 0.0, 0.0
        delta = value - mean
        mean -= delta / (n - 1)
        delta2 = value - mean
        m2 -= delta * delta2
        n -= 1
    return n, mean, m2
```

There is no test verifying that `extend` followed by `evict` with the same values returns to the initial state, or that the stdev computed from `m2` matches a direct computation.

**Impact:**
- Silent numerical errors in baseline updates could accumulate over time
- Subtle bugs in stdev calculations lead to incorrect anomaly thresholds
- Hard to detect without statistical validation tests

**Root Cause:**
Unit tests exercise happy paths but not invariants that Welford's algorithm should satisfy.

**Fix:**
Add hypothesis-based property tests:

```python
from hypothesis import given, strategies as st

@given(
    prior_n=st.integers(min_value=10, max_value=1000),
    values_to_add=st.lists(st.floats(allow_nan=False, allow_infinity=False), 
                            min_size=1, max_size=100),
    values_to_evict=st.lists(st.floats(allow_nan=False, allow_infinity=False), 
                              min_size=1, max_size=100),
)
def test_welford_extend_then_evict_all_matches_direct_computation(
    prior_n: int, values_to_add: list[float], values_to_evict: list[float]
) -> None:
    """Verify extend+evict matches direct stdev on the same dataset."""
    n, mean, m2 = 0, 0.0, 0.0
    for v in values_to_add:
        n, mean, m2 = _welford_extend(
            prior_n=n, prior_mean=mean, prior_m2=m2, new_values=[v]
        )
    
    # Direct computation on same values
    direct_stdev = statistics.pstdev(values_to_add)
    welford_stdev = _stdev_from_m2(n=n, m2=m2)
    assert abs(welford_stdev - direct_stdev) < 1e-10
```

**Test Coverage:**
- No existing property-based tests in `tests/distillation/baselines/test_refresh_*.py`
- Add Hypothesis-based tests to each refresh function's test file

---

### 8. MEDIUM: Correlation Brief Reference Index Not Validated for Uniqueness

**Location:** `correlation_brief.py:106–142`, `orchestrator.py:850–851`

**Issue:**
The `CorrelationRegimeBrief` carries a `reference_index` dict that maps reference IDs (e.g., "CR-1", "CR-2") to block payloads. The reference IDs are generated deterministically, but there's no guard against duplicate keys if the generation logic is refactored:

```python
# In correlation_brief.py, the reference index is built from findings:
reference_index: dict[str, dict[str, Any]] = {}
for finding in findings:
    cr_id = _compute_reference_id(finding)  # Opaque generation
    reference_index[cr_id] = finding.to_dict()
```

If two findings accidentally have the same reference ID, the later one silently overwrites the first.

**Impact:**
- Silent data loss if a generation bug causes collisions
- Difficult to debug because the CR brief looks correct (it contains some of the findings)

**Root Cause:**
Dict insertion does not validate key uniqueness at the application level.

**Fix:**
Add an explicit uniqueness check:

```python
reference_index: dict[str, dict[str, Any]] = {}
for finding in findings:
    cr_id = _compute_reference_id(finding)
    if cr_id in reference_index:
        raise ValueError(
            f"Duplicate reference ID {cr_id!r}: {finding.source_block_id} and "
            f"{reference_index[cr_id]['source_block_id']}"
        )
    reference_index[cr_id] = finding.to_dict()
```

Then add a test:

```python
def test_correlation_brief_reference_index_unique():
    """Verify no two findings share the same reference ID."""
    brief = assemble_correlation_brief(...)
    cr_ids = set(brief.reference_index.keys())
    assert len(cr_ids) == len(brief.reference_index)
```

---

### 9. LOW: Empty `__init__.py` in Distillation Subpackage

**Location:** `src/alphamind/distillation/__init__.py` (0 lines)

**Issue:**
The distillation package's `__init__.py` is empty. This makes it impossible to do:

```python
from alphamind.distillation import OutputBlock, DistillationOutputs, run_external_distillation
```

Consumers must use absolute imports:

```python
from alphamind.distillation.output import OutputBlock
from alphamind.distillation.orchestrator import DistillationOutputs, run_external_distillation
```

**Impact:**
- Minor usability issue; external consumers need to know the internal module layout
- Conflicts with public-API documentation if the docs suggest `from alphamind.distillation import ...`

**Root Cause:**
The package is young; public API has not been formally exported.

**Fix:**
Add to `src/alphamind/distillation/__init__.py`:

```python
"""Distillation layer — story 02-distillation, external API."""

from alphamind.distillation.orchestrator import DistillationOutputs, run_external_distillation
from alphamind.distillation.output import OutputBlock, OutputAudience, AnomalyFlag, AnomalySeverity

__all__ = [
    "DistillationOutputs",
    "run_external_distillation",
    "OutputBlock",
    "OutputAudience",
    "AnomalyFlag",
    "AnomalySeverity",
]
```

**Test Coverage:**
- Add a test in `tests/test_imports.py` that verifies the public API is importable from the package root

---

## Summary Table

| Priority | Issue | Category | Effort | Risk |
|----------|-------|----------|--------|------|
| CRITICAL | Session flushing serialization (Phase 2) | Runtime | 4h | High (latency/maintainability) |
| HIGH | Implicit tuple ordering in sector assembly | Data modeling | 2h | Medium (correctness) |
| HIGH | Null-safety in contract scope window join | Data modeling | 1h | Medium (audit trail) |
| HIGH | OutputBlock audience empty-check runtime-only | Data modeling | 2h | Low (rare, caught early) |
| MEDIUM | Per-ticker payload silent filtering | Observability | 1h | Low (cosmetic) |
| MEDIUM | Regime bootstrap placeholder visibility | Runtime | 2h | Medium (downstream alignment) |
| MEDIUM | Welford's algorithm not property-tested | Testing | 3h | Medium (numerical stability) |
| MEDIUM | Correlation brief reference uniqueness | Data modeling | 1h | Low (defensive check) |
| LOW | Empty package `__init__.py` | API | 0.5h | Negligible (UX) |

---

## Testing Assessment

**Strengths:**
- 61 test files dedicated to distillation; orchestrator has end-to-end fixture tests
- Story-driven test structure with clear acceptance criteria per story
- Determinism tests verify byte-identical outputs across runs
- Contract scope and prediction-market filtering have dedicated test coverage

**Gaps:**
1. No property-based tests for Welford's algorithm (Issue 7)
2. No concurrent execution tests for Phase 2 parallelization (Issue 1)
3. No tests validating sector roster ↔ output mapping consistency (Issue 2)
4. No tests for missing macro series pre-flight checks (Issue 6)
5. Reference index uniqueness validation missing (Issue 8)

**Recommendations:**
- Adopt Hypothesis for property-based testing of stateful primitives (Welford's, rolling baselines)
- Add parametrized concurrency tests that exercise Phase 2 in parallel mode
- Add integration tests that validate invocation output structure (block counts, audience routing) across sectors

---

## Notes on Architecture

The distillation layer is well-structured as seven phases with clear separation of concerns:
- Phase 1 (Class B refresh): isolated, no downstream data dependencies
- Phases 2–5 (indicator computation → assembly): data-flow clean, but Phase 2 serialization is a bottleneck
- Phases 6–7 (archive write, brief-store population): simple, deterministic

The output contract (OutputBlock envelope, anomaly flags, regime context) is sound and well-documented. The main architectural debt is the Session flushing constraint in Q3/Q6, which forces sequential execution of otherwise independent computations.

**No issues found with SQLite or APScheduler integration.**

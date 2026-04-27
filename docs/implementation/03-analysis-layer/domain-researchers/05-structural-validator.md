---
status: not_started
completed_date:
commit_id:
---

# 05 — Structural validator

## Goal

Implement the hand-written structural validator for domain researcher output — the Layer 2 + Layer 3 producer-side check from [llm-output-validation.md](../../../design/testing/llm-output-validation.md) for this agent class. Runs on a `SectorBrief` (already parsed by story 04) and reports the structural and referential failures that schema-style validation would catch on JSON-output agents.

## Reading

- `docs/design/testing/llm-output-validation.md` § Layer 2 — Schema validation — informal-schema agents stance, named-checks shape
- `docs/design/testing/llm-output-validation.md` § Layer 3 — Referential integrity — producer-side format checks for domain researchers (sequential indexing, no gaps, unique within section)
- `docs/design/testing/llm-output-validation.md` § Reference-ID taxonomy — producer-side format rules
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Domain researcher output contract — the prose contract whose constraints the validator codifies
- `docs/design/llm-agent-failure-handling.md` § Recovery semantics — the corrective-retry runtime policy this validator's failures feed

## Depends on

- 03 (`SectorBrief` data model)

The parser (story 04) produces the `SectorBrief` instances the validator inspects in production, but the validator's contract is "given a `SectorBrief`, return a `ValidationResult`" — its unit tests construct `SectorBrief` instances directly via the Pydantic model constructors, so no parser dependency. 04 and 05 can land in parallel.

## Scope

In scope: under `src/alphamind/analysis/domain_researchers/` —

- `validation.py` defining:
  - `class ValidationError` — a Pydantic v2 `BaseModel` (not an exception) carrying `field_path: str`, `rule: str`, `message: str`. Mirrors the JSON Schema validator's error-record shape so the harness story 07 can construct corrective-retry messages from a uniform error type regardless of whether the underlying validator is formal-schema or hand-written.
  - `class ValidationResult` — a Pydantic v2 `BaseModel` with `is_valid: bool`, `errors: tuple[ValidationError, ...]`. The harness halts on `is_valid is False`; success carries an empty error tuple.
  - `validate_brief(brief: SectorBrief) -> ValidationResult` — runs every check below and returns the aggregate result. Per [llm-output-validation.md § Fail-fast, fail-once](../../../design/testing/llm-output-validation.md#fail-fast-fail-once), the harness only consumes the *first* error for the corrective-retry message; the validator nonetheless returns the full list so the diagnostic record receives the complete error inventory.
- The named checks (each implemented as a small private function `_check_<name>(brief) -> Iterable[ValidationError]`):
  - **`reference_prefix_consistency`**: every `Finding.finding_id`, `Anomaly.anomaly_id`, `ThesisCandidate.thesis_candidate_id` carries the prefix matching `brief.sector` per `SECTOR_PREFIX`. Pydantic regex on the records covers format; this check enforces sector consistency across records of a single brief.
  - **`findings_sequential_indexing`**: findings IDs are `SA-{PREFIX}-1`, `SA-{PREFIX}-2`, ... with no gaps and no duplicates, starting at 1.
  - **`anomalies_sequential_indexing`**: anomaly IDs are `SA-{PREFIX}-ANOM-1`, `SA-{PREFIX}-ANOM-2`, ... with the same starting and gap rules.
  - **`thesis_candidates_sequential_indexing`**: thesis-candidate IDs are `SA-{PREFIX}-TC-1`, `SA-{PREFIX}-TC-2`, ... same rules.
  - **`findings_count_minimum`**: at least one `Finding` is present. The design doc's "3–5 findings per brief" advisory is a system-prompt concern, not validator territory; here we enforce only that a brief with zero findings is structurally invalid (a brief that has nothing to say is not a brief).
  - **`tickers_in_universe`**: every ticker in any `Finding.tickers`, `Anomaly.tickers`, or `ThesisCandidate.ticker` is present in `config/sectors.yaml`'s ticker list for `brief.sector`. Cross-sector tickers (e.g., a tech researcher mentioning JPM) are a structural error — the agent's mandate is intra-sector. The check loads the sector ticker list via `_common.load_config()` (configuration-management.md cross-reference invariant); a stub-friendly seam allows tests to inject a fixture sector membership without reading YAML.
  - **`signal_quality_reason_consistency`**: redundant safety check — the Pydantic model's `model_validator` already enforces `signal_quality_reason` presence/absence against `signal_quality`, but a `ValidationError` here surfaces if the parser somehow constructs an inconsistent brief (defensive belt-and-braces because the parser receives untrusted LLM output).
  - **`unique_thesis_candidate_tickers`**: no two `ThesisCandidate` records share both `ticker` and `direction`. Two long-NVDA thesis candidates is intra-brief duplication — the agent should pick its best expression, not surface variants. (Cross-direction variants — long NVDA + short NVDA — are allowed per the prose contract's "long | short" enum without same-direction restriction.)
- Unit tests covering each check at the boundary:
  - Each named check fires exactly when its rule is violated and not otherwise.
  - Sequential-indexing tests cover gap (1, 3 missing 2), duplicate (1, 2, 2), wrong-start (2, 3 missing 1), and empty-section (vacuously satisfied).
  - Reference-prefix-consistency test covers a tech-semis brief containing an `SA-FIN-` ID (mismatched sector) — should fire even though the per-record Pydantic regex accepted the ID independently.
  - Tickers-in-universe test uses a fixture `Sector → tuple[str, ...]` membership map (not the live YAML) so the test does not depend on `config/sectors.yaml`.
  - Multi-error case: a brief with both an indexing gap and an out-of-universe ticker returns both errors in `ValidationResult.errors`.
  - The Pydantic `ValidationError` type is also defined as a Pydantic model with `model_config = {"frozen": True}` so it is hashable and equality works in test assertions.

Out of scope:
- Layer 1 parse failures (story 04 owns; raised as `ParseError` before the validator runs).
- Layer 4 stop-reason classification (story 07 owns; the harness re-classifies parse-or-validation failures with `max_tokens` as `context_overflow`).
- The corrective-retry message construction itself (story 07 owns; the validator only produces error records).
- Consumer-side reference resolution — domain researchers produce IDs but do not consume any (their column in `llm-output-validation.md § Per-agent surface mapping` says "References consumed: —"). No Layer 3 consumer-side logic in this story.
- LLM reasoning quality (e.g., is the catalyst plausible, is the conviction calibrated): out of scope for the validator and reserved for the Phase 4 feedback loop.

## Notes

The `ValidationError` is a Pydantic model (a value type) rather than a Python `Exception` because validation produces a *list* of errors, not a control-flow signal. The harness consumes the list as data.

The named-check pattern (one private function per rule, each yielding zero-or-more `ValidationError`s) lets tests target individual checks without running the whole validator pipeline. It also makes the rule set greppable for the design doc cross-reference: each rule name appears as `_check_<name>` and as a string literal in the `rule:` field.

The `tickers_in_universe` check has a config-injection seam — the validator does not call `_common.load_config()` directly, but instead receives a `sector_membership: Mapping[Sector, frozenset[str]]` dependency (via constructor or call argument). In the harness story 07, the membership map is built from `config/sectors.yaml` once per invocation and passed in. This keeps the validator deterministic and unit-testable without YAML.

The `findings_count_minimum` check is the only "size" check the validator imposes. The 3–5 findings advisory range from the design doc is *not* a validator rule because:
1. A tightly-scoped sector universe on a quiet day genuinely has fewer than 3 findings worth surfacing — forcing 3 produces padding, the failure mode the LLM-agent-uniformly-Critical principle is designed to detect via downstream conviction calibration, not structural rejection.
2. A volatile day genuinely produces more than 5 findings worth surfacing — the agent's job is to triage to the highest-signal subset, but a brief with 6 findings is not structurally malformed.
3. The per-finding token cost bounds runaway output already (the harness sets `max_tokens` per the configured budget).
The rule is encoded in the system prompt (story 09a/b/c) and tracked in the feedback loop (Phase 4), not enforced by the validator. This matches the analyst's "no maximum recommendations per invocation" decision in [analyst.md](../../../design/04-decision-layer/analyst.md).

## Acceptance criteria

- [ ] `validate_brief(brief)` returns a `ValidationResult` with `is_valid=True` and empty `errors` for every canonical valid brief fixture (one per sector).
- [ ] `reference_prefix_consistency` rejects a tech-semis brief whose `findings` contains an `SA-FIN-1`-prefixed ID.
- [ ] `findings_sequential_indexing` rejects gaps, duplicates, and wrong-start cases on findings IDs.
- [ ] `anomalies_sequential_indexing` and `thesis_candidates_sequential_indexing` apply the same rules to their respective sections.
- [ ] `findings_count_minimum` rejects a brief with zero findings.
- [ ] `tickers_in_universe` rejects a tech-semis brief whose findings reference a ticker not in the tech-semis sector membership map.
- [ ] `signal_quality_reason_consistency` rejects a brief with `signal_quality: DEGRADED` and `signal_quality_reason: None` (defensive — duplicates the Pydantic model's check).
- [ ] `unique_thesis_candidate_tickers` rejects a brief with two long-NVDA thesis candidates; accepts a brief with long-NVDA and short-NVDA.
- [ ] A brief with multiple violations returns multiple `ValidationError`s in `ValidationResult.errors`.
- [ ] `ValidationError` carries `field_path`, `rule`, `message` and is hashable.
- [ ] The validator accepts an injected `sector_membership` dependency rather than reading `config/sectors.yaml` directly.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

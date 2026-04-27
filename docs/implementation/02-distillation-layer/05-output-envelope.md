---
status: not_started
completed_date:
commit_id:
---

# 05 — Output envelope and structured-text formatter

## Goal

Implement the per-block envelope every distillation output carries (freshness timestamp, calibration state, anomaly flags, regime context) plus the structured-text formatter that renders it for downstream LLM consumption. Per-consumer partition tagging is part of this story so per-category computations can declare their target audience inline.

## Reading

- `docs/design/02-distillation-layer/external.md` § Output format — authoritative shape: every output block carries freshness timestamp, confidence/reliability tier, anomaly flags (binary plus magnitude grouped for easy scanning), regime context. Partitioned by consumer (sector analyst agents / synthesizer / all agents)
- `docs/design/03-analysis-layer/domain-researchers/tech-semis.md` § Inputs — first row describes what the distillation output for sector analyst agents contains
- `docs/design/03-analysis-layer/synthesizer.md` § Inputs — `CR` row describes the correlation/regime brief shape (synthesizer's distillation input)
- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — the `calibration_state` tag the envelope carries
- Story 04's `CalibratedValue` dataclass — the calibration tag wraps that

## Depends on

- 03 (state schema — output blocks may reference state row identifiers in audit fields)
- 04 (calibration framework — the envelope's calibration field is a `CalibrationState` value)

## Scope

In scope: under `src/alphamind/distillation/` —

- A new `output.py` module defining:
  - `class OutputAudience(StrEnum)` with members `SECTOR_TECH_SEMIS`, `SECTOR_FINANCIALS`, `SECTOR_ENERGY`, `CORRELATION_REGIME_BRIEF`, `UNIVERSAL_BROADCAST`. The members map 1:1 to the partition targets named in `external.md § Output format`.
  - `@dataclass(frozen=True) class OutputBlock` with fields:
    - `block_id: str` — short identifier the formatter renders as a heading (e.g., `"q1.volume_anomaly"`, `"q7.intra_sector_correlation"`).
    - `audience: frozenset[OutputAudience]` — one or more partition targets; an indicator may go to multiple consumers (e.g., a regime-relevant flag goes to a sector audience and to `UNIVERSAL_BROADCAST`).
    - `freshness_ts: datetime` — UTC timestamp of the underlying data (not invocation start; this is the most recent data point that fed the block).
    - `calibration_state: CalibrationState` — from story 04.
    - `bootstrap_reason: str | None` — passthrough from `CalibratedValue` when state is `BOOTSTRAP`/`UNAVAILABLE`.
    - `payload: Mapping[str, Any]` — the actual content. Free-shape so per-category computations decide their own structure; the formatter renders it deterministically.
    - `anomaly_flags: tuple[AnomalyFlag, ...]` — zero or more (see below).
    - `regime_context: str | None` — populated only on blocks where the regime label is load-bearing for interpretation; otherwise `None` (regime is delivered separately as the universal broadcast and would be redundant on every block).
  - `@dataclass(frozen=True) class AnomalyFlag` with fields `name: str`, `magnitude: float`, `severity: Literal["investigate_now", "investigate_if_persists", "note_for_context"]`. The three severity strings match the analyst-side severity taxonomy in [tech-semis.md § Domain researcher output contract](../../../design/03-analysis-layer/domain-researchers/tech-semis.md#domain-researcher-output-contract) so domain researchers can pass the level through unchanged.
- A `format_block(block: OutputBlock) -> str` function rendering one block as structured text matching the shape implied by `external.md § Output format`. Format suggestion:

  ```
  ### {block_id}
  Freshness: {freshness_ts ISO8601 UTC}
  Calibration: {calibration_state}{ — bootstrap_reason: <reason> if not CALIBRATED}
  {Regime: <regime_context> if not None}

  {payload rendered as key: value lines, nested dicts indented}

  Anomaly flags ({count}):
    - {name} | magnitude {magnitude:.2f} | severity {severity}
    - ...
  ```

  Determinism is load-bearing — the same `OutputBlock` must render to byte-identical text on every call so the invocation archive diffs cleanly across runs. Iterate dictionaries in sorted-key order; format floats with a fixed precision; do not embed the current wall clock anywhere.

- A `format_blocks_for_audience(blocks: Iterable[OutputBlock], audience: OutputAudience) -> str` helper that filters blocks by audience membership and concatenates them in deterministic order (sort by `block_id` ascending). Story 11a/11b consume this when assembling the per-consumer outputs.
- Unit tests:
  - `OutputBlock` rejects an empty `audience` set (every block must declare at least one consumer).
  - `format_block` produces byte-identical text on repeated calls for the same block.
  - `format_block` includes the `bootstrap_reason` line when calibration is `BOOTSTRAP` or `UNAVAILABLE`; omits it when `CALIBRATED`.
  - `format_block` omits the `Regime:` line when `regime_context is None`.
  - `format_blocks_for_audience` filters and orders deterministically; given a fixture set of blocks tagged with mixed audiences, the rendered output for each audience contains exactly the expected blocks in `block_id` order.

Out of scope:
- The actual per-category outputs (stories 08*, 09).
- Per-consumer assembly into a sector brief / `CR` brief — the formatter renders one block at a time and concatenates; semantic grouping (e.g., "category 7 findings" headings in the `CR` brief) belongs to story 11b.
- Persistence of rendered blocks to the invocation archive — covered by the orchestrator (story 12) and the existing archive mechanism in [`infrastructure.md § Invocation archive`](../../../architecture/infrastructure.md#layer-2-invocation-archive-files).

## Notes

The `block_id` namespacing convention: `<category>.<short_name>` where category mirrors the `external.md § 2` heading shorthand (`q1`, `q3`, `q6`, `q7`, `q12`, `qual`). Per-category stories pick the `<short_name>` parts; this story just establishes the convention so the IDs are greppable and consistently prefixed.

`anomaly_flags` are part of the envelope rather than a separate output channel because the design doc explicitly groups them inline with the indicator block they accompany (see `external.md § 3` — anomalies belong with the data that triggered them, not in a separate document). Story 10 will collect flags across blocks for the per-consumer summary view, but the source of truth is per-block.

The `regime_context` field is intentionally `str | None` rather than a structured object: it's a one-sentence label like `"low_vol_compression (early-strong)"`, used as a reading-aid annotation. The full regime payload is the universal broadcast (story 09), not duplicated into every block.

`format_block` precision for floats: use `f"{x:.4g}"` for general-purpose; use `f"{x:.2f}"` for percentages; use `f"{x:.0f}"` for share counts. Keep these rules in one constants module so the convention is visible.

The "byte-identical on repeated calls" property is what makes the invocation archive diffable — operators reading historical archives expect the format to be stable per `infrastructure.md § Invocation archive`. Lock determinism behaviorally via the dictionary-iteration test, and via a doc comment that cites why it matters.

## Acceptance criteria

- [ ] `OutputAudience` enum exists with five members exactly matching the partition targets in `external.md § Output format`.
- [ ] `OutputBlock` is frozen, validates `audience` is non-empty in `__post_init__`, and contains all fields listed above.
- [ ] `AnomalyFlag` is frozen and uses a Literal for the severity field.
- [ ] `format_block` renders the documented shape including `Freshness:`, `Calibration:`, and (conditional) `Regime:` and `bootstrap_reason:` lines.
- [ ] `format_block` produces byte-identical output on repeated calls (verified by a deterministic-rendering unit test).
- [ ] Float-precision rules are centralized in named constants, not scattered through the formatter.
- [ ] `format_blocks_for_audience` filters and concatenates in `block_id`-ascending order; tested against a multi-audience fixture.
- [ ] Unit test rejects an `OutputBlock` constructed with an empty `audience`.
- [ ] Unit test verifies `bootstrap_reason` line appears for `BOOTSTRAP` / `UNAVAILABLE` and is absent for `CALIBRATED`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

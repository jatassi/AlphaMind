---
status: not_started
completed_date:
commit_id:
---

# 05 — Replay execution engine

## Goal

Implement the core replay primitive: take a `FixtureSlice` and a `LoadedCandidateConfig`, spin up an isolated SQLAlchemy session derived from the slice's `raw_inputs.sqlite`, populate Class B distillation state from the slice's own history, run the live `run_external_distillation` orchestrator across the slice's invocation timestamps, and capture every anomaly flag, regime label, and Class B baseline emitted. The engine handles single-config and dual-config (diff-mode) execution by being called once per config — this story's surface is one slice × one config; story 08's CLI loops over slices and configs.

## Reading

- `docs/design/02-distillation-layer/replay-harness.md` § Process — the five-step process per fixture slice; especially "Compute Class B baselines from the slice's own history" (state isolation invariant) and "Reuse the live distillation orchestrator on the slice's invocation timestamps"
- `docs/design/02-distillation-layer/replay-harness.md` § Versioning — "imports its distillation code paths from `src/alphamind/distillation/` rather than copying"; the harness's runtime distillation logic must equal the live layer's exactly
- `docs/design/02-distillation-layer/threshold-calibration.md` § Class B refresh — "Refresh at the start of each invocation's distillation phase, before anomaly detection runs"
- `docs/design/02-distillation-layer/threshold-calibration.md` § Bootstrap policy — the engine's first-invocation outputs are typically `bootstrap`-tagged until enough observations accumulate; this is by design
- `docs/design/01-data-layer/collector/storage.md` § Cross-cutting rules — pragmas, FK enforcement, UTC timestamps; the isolated session uses these
- Stories 04 (calibration framework), 07 (rolling baseline refresh), and 12 (`run_external_distillation` orchestrator) of the parent distillation work tree — the primitives the engine reuses
- Story 03 (state schema) — the distillation state tables the engine spins up empty and lets the refresh primitives populate
- Stories 03 and 04 of this work tree — `FixtureSlice`, `load_fixture_slice`, `LoadedCandidateConfig`, `load_candidate_config`

## Depends on

- 03 (fixture loader)
- 04 (config loader)
- **Cross-feature dependency:** the parent `02-distillation-layer/` work tree's stories 02, 03, 04, 05, 06, 07, 08a–f, 09, 10, 11a, 11b, 12 must all be `done` — the engine reuses every primitive those stories produce. The orchestrator's release predates harness story 05 dispatch; do not pick this story up otherwise.

## Scope

In scope: under `src/alphamind/distillation/replay_harness/engine.py` —

- **`InvocationOutputs` frozen dataclass** capturing what the engine retains per invocation (per-orchestrator-call):
  - `as_of: datetime` — the invocation's effective UTC timestamp.
  - `invocation_id: str` — synthesized identifier (see Notes for the synthesis rule).
  - `regime_label: str` — value from the `regime.label` block (`low_vol_compression` / `vol_expansion` / `crisis_spike` / `vol_normalization` per story 09's enum).
  - `regime_transition_state: str` — `stable` / `early-weak` / `early-strong` / `confirmed`.
  - `anomaly_flags: tuple[AnomalyFlagRecord, ...]` — every anomaly flag produced by the orchestrator across all sectors and audiences, deduplicated by `(flag_type, ticker_or_pair_key)` tuple. Each `AnomalyFlagRecord` carries: `flag_type: str` (e.g., `volume_anomaly`, `price_move_atr`), `ticker_or_pair_key: str | None`, `magnitude: float | None`, `calibration_state: str` (CALIBRATED/BOOTSTRAP/UNAVAILABLE).
  - `class_b_baseline_values: tuple[BaselineValueRecord, ...]` — every Class B baseline value snapshot produced this invocation. Each `BaselineValueRecord` carries: `baseline_kind: str` (e.g., `volume`, `atr`, `pair_lag`, `contract_history`), `entity_key: str` (ticker, pair name, or contract id), `value: float | None`, `calibration_state: str`.

  Both inner records are frozen dataclasses defined in this module.

- **`SliceReplayResult` frozen dataclass** aggregating across the slice's invocations:
  - `slice_id: str` — passed through from the manifest.
  - `regime_label: str` — passed through (the slice's *exemplary* regime, distinct from the per-invocation `regime_label` the orchestrator classifies).
  - `invocations: tuple[InvocationOutputs, ...]` — one entry per invocation timestamp, ordered by `as_of`.
  - `config_content_hash: str` — passed through from `LoadedCandidateConfig`, so the aggregator can attribute outputs to the right config in diff mode.

- **`replay_slice(slice: FixtureSlice, candidate: LoadedCandidateConfig) -> SliceReplayResult`** function. Procedure:

  1. **Spin up an isolated session.** Create an in-memory SQLite database (or, if memory is a concern for slices > ~500MB, a temp-file copy under `tempfile.TemporaryDirectory()` that is removed on function exit). Apply the same pragmas the runtime uses (`journal_mode=WAL` is irrelevant for in-memory; set `foreign_keys=ON`). Run `alembic upgrade head` against the isolated DB to land the full schema (raw-input tables + distillation-state tables). The harness must NOT touch the runtime database.

  2. **Copy raw-input tables** from `slice.slice_dir / RAW_INPUTS_FILENAME` into the isolated session. Implementation: use `sqlite3` `ATTACH DATABASE` to attach the slice file read-only, then `INSERT INTO main.{table} SELECT * FROM source.{table}` for every raw-input table the runtime distillation reads. The set of tables to copy is enumerated in a module-level constant `RAW_INPUT_TABLE_NAMES: tuple[str, ...]` listing every table from `storage.md § Tables` that distillation depends on (`asset_universe`, `sector_classification`, `ohlcv_bars`, `corporate_actions`, `options_contracts`, `options_contract_snapshots`, `short_interest_snapshots`, `short_volume_daily`, `borrow_cost_daily`, `borrow_cost_intraday`, `earnings_estimate_revisions`, `macro_observations`, `treasury_auctions`, `event_calendar`, `earnings_event_details`, `news_articles`, `news_article_tickers`, `news_article_clusters`, `prediction_market_contracts`, `prediction_market_snapshots`).

  3. **Leave distillation state tables empty.** The Class B refresh primitives populate them from the raw-input history per the design's "compute from the slice itself, not loaded from the runtime DB" invariant.

  4. **Iterate `slice.manifest.invocation_timestamps` in ascending order.** For each `as_of`:
     a. Synthesize `invocation_id = f"replay_{slice.manifest.slice_id}_{as_of.strftime('%Y%m%dT%H%M%SZ')}"`.
     b. Determine `ticker_scope` — the universe of tickers the orchestrator sees this invocation. For replay, this is every ticker in `asset_universe` with `asset_role='universe'` and `is_active=1` as of `as_of`. (The runtime resolves this via the configuration cascade; the harness uses the universe-as-it-stood-at-curation as recorded in the slice's `asset_universe` snapshot.)
     c. Call `run_external_distillation(session, candidate.config, ticker_scope, as_of, invocation_id)`. The orchestrator's signature is fixed by parent story 12; this story passes through. Phase 7 (brief-store population) writes to the isolated session's `briefs` table — fine; the engine ignores those writes.
     d. Capture outputs: extract anomaly flags from each `SectorOutput` and the `CorrelationRegimeBrief`, the regime label from `universal_regime_label`, the Class B baseline values from the just-refreshed state tables in the isolated session.

     The orchestrator's `Phase 6 — invocation-archive write` is replay-incompatible (it writes to `%USERPROFILE%\AlphaMind\archive\...`). The engine MUST suppress this side effect — either by setting an env-var or kwarg the orchestrator supports for archive suppression, or by patching the archive-writer at module level via `unittest.mock.patch` for the duration of the replay. The story author chooses; if the orchestrator doesn't expose a suppress flag, this story adds one (file an addendum to story 12 if so) — the harness must run without polluting the operator's archive directory.

  5. **Return** `SliceReplayResult` with the captured invocations.

  6. **Tear down** the isolated session and any temp files (use a `try/finally` with `tempfile.TemporaryDirectory` context manager when applicable).

- **Anomaly flag extraction helper** `_extract_anomaly_flags(outputs: DistillationOutputs) -> tuple[AnomalyFlagRecord, ...]`:
  - Walks `outputs.sector_outputs` (3 entries) and `outputs.correlation_regime_brief`, finds every `OutputBlock` carrying anomaly content, extracts the flags into `AnomalyFlagRecord` instances.
  - Deduplicates by `(flag_type, ticker_or_pair_key)` — the same anomaly can appear in both a sector output and the correlation brief; the engine retains one entry, not both.
  - The exact extraction logic depends on story 10's `AnomalySummary` shape. The engine reads structured data (not parsed Markdown text); the test cases verify against the orchestrator's actual emitted shape.

- **Class B baseline extraction helper** `_extract_class_b_baselines(session: Session, as_of: datetime) -> tuple[BaselineValueRecord, ...]`:
  - Queries the four Class B state tables (`distillation_ticker_baseline`, `distillation_pair_lag`, `distillation_contract_history`, `distillation_event_history`) for rows whose `as_of` equals the invocation's `as_of`.
  - Maps each row to a `BaselineValueRecord`. Tickers/pairs/contracts the slice does not cover produce no record.

- **Logging.** The engine emits one `INFO` line per invocation with `slice_id`, `as_of`, anomaly count, regime label, and elapsed milliseconds. One `INFO` line at slice start (count of invocations to replay) and one at slice end (cumulative anomaly count, total wall time).

- **Errors.** If the orchestrator raises, `replay_slice` propagates without partial cleanup — the isolated session is torn down in a `finally`, but the result is not returned. Subsequent slices are unaffected; the CLI (story 08) handles per-slice failure by logging and continuing.

- **Unit tests** under `tests/distillation/replay_harness/test_engine.py`:
  - Fixture: a tiny synthesized slice with 5 invocation timestamps spaced 1 hour apart, ~5 tickers, ~10 days of `ohlcv_bars`, a fabricated `manifest.json`. Built at test-fixture time, not committed as a binary file (`test_engine.py` constructs the slice in a `tmp_path`).
  - Happy path: `replay_slice(slice, canonical_config)` returns a `SliceReplayResult` with 5 invocation entries; `regime_label` per invocation is set; anomaly counts are non-zero across the slice.
  - Isolation invariant: `replay_slice` does not touch the runtime database. The test creates a fixture pointing at a runtime DB path that does not exist; the call still succeeds (proving no runtime read).
  - Determinism: two calls of `replay_slice` on the same `(slice, candidate)` produce byte-identical `SliceReplayResult` (compare via `dataclasses.asdict` deep equality).
  - Empty slice: a slice with one invocation timestamp but minimally-populated raw-input tables produces a result whose anomaly counts are mostly zero and whose Class B baselines are mostly `BOOTSTRAP`-tagged.
  - Archive suppression: the engine does not write to `%USERPROFILE%\AlphaMind\archive\` during replay (test against a mocked path resolver or by checking the directory is not created).
  - Orchestrator failure: a fault-injected per-category failure in the orchestrator propagates; `replay_slice` raises and the temp directory is cleaned up.
  - Different config: replaying the same slice under a config with a substantially looser `volume_anomaly_sigma` (e.g., 5.0 instead of 2.5) produces fewer anomalies — proves the candidate config is actually applied.

Out of scope:
- Aggregating across slices (story 06).
- Rendering the report (story 07).
- The CLI loop over multiple slices and configs (story 08).
- Computing regime-stratified statistics — the engine returns per-invocation outputs; aggregation is story 06.
- Diffing two configs in a single function call — diff mode is achieved by calling `replay_slice` once per config in story 08, not by changing this engine's surface.
- Validating that the slice's raw-input tables match the runtime schema — the migration runs at session startup, and a schema mismatch fails loudly when the orchestrator's queries hit unexpected columns. The harness surfaces the error rather than swallowing it.
- Persisting `SliceReplayResult` to disk — the result is a Python object consumed in-memory by stories 06 and 07.

## Notes

State isolation is the central invariant. The engine MUST NOT write to or read from the runtime distillation tables — every per-slice run is a fresh in-memory database. The runtime SQLAlchemy session at `%USERPROFILE%\AlphaMind\data\alphamind.db` is irrelevant to this code path. Tests should fail loudly if the engine accidentally reaches for the runtime DB.

The orchestrator's archive-write side effect (Phase 6 in story 12) is the most likely accidental coupling. If story 12 does not already accept a `suppress_archive_write: bool = False` parameter, this story files an addendum to story 12 to add one (and updates story 12's test expectations accordingly). Do not silently `os.remove` the archive after writing — that's destructive on the runtime archive if the env-var resolution accidentally lands there. Suppression is the right primitive.

The brief-store write (Phase 7 in story 12) targets the same isolated session, so it lands harmlessly in the in-memory DB and is torn down with the session. No suppression needed.

The `ticker_scope` derivation reads `asset_universe` from the slice's snapshot, not from the runtime — a `LIVE_ARCHIVE` slice captures the universe-as-it-stood-at-curation. Ticker churn between the slice's curation and now is irrelevant; the harness reasons about regime behavior at the time of curation, not now.

The `RAW_INPUT_TABLE_NAMES` constant is the authoritative list of tables the harness copies. When the data-layer schema adds a new table that distillation reads, this constant must be updated — fail loudly is preferable to silently-missing-data, so the constant is checked against story 12's actual reads via a test that walks the orchestrator's queries (or, simpler, a smoke test that runs the orchestrator and asserts no `OperationalError: no such table` is raised). The simpler smoke test is acceptable; the schema is small enough that the maintainer will catch a drift quickly.

Per `feedback_simplify_before_building.md`, do not introduce a `ReplayContext` god-object. The engine's surface is one function. The session, the config, the slice, and the captured outputs are all explicit arguments or return values — no hidden state.

Per `feedback_avoid_numeric_anchors.md`, the tests' "substantially looser" sigma comparison (`5.0 vs 2.5`) is a structural sanity check (more permissive threshold ⇒ fewer flags), not a calibration target. Do not assert specific anomaly counts; assert the directional comparison only.

The `harness_calibrated_not_pessimistic` memory is operative here: the harness uses the same primitives the live distillation phase uses, with no added conservatism. Do not insert "but be safer" defaults; if the live primitive produces output X, the harness produces output X.

The cross-feature dependency on the parent distillation work tree is load-bearing. Do not pick this story up speculatively before story 12 lands, because the engine's surface depends on `run_external_distillation`'s exact signature. The orchestrator's coordinator handles cross-tree gating.

`InvocationOutputs.invocation_id` synthesis — `replay_{slice_id}_{as_of_compact}` — uses an `as_of` formatting that is filesystem-safe and lexicographically sortable. The `replay_` prefix distinguishes harness-synthesized IDs from real-invocation IDs in any logs that conflate them.

## Acceptance criteria

- [ ] `InvocationOutputs`, `AnomalyFlagRecord`, `BaselineValueRecord`, `SliceReplayResult` frozen dataclasses exist with the documented fields.
- [ ] `RAW_INPUT_TABLE_NAMES` named constant lists every raw-input table distillation reads.
- [ ] `replay_slice(slice, candidate)` runs the documented six-step procedure end-to-end.
- [ ] The function spins up an isolated SQLite session — no read or write to the runtime database during replay.
- [ ] Class B distillation state tables start empty and are populated from the slice's own history.
- [ ] The orchestrator's archive-write side effect is suppressed during replay.
- [ ] Synthesized `invocation_id` follows the documented pattern.
- [ ] `_extract_anomaly_flags` deduplicates anomalies that appear in both sector outputs and the correlation brief.
- [ ] `_extract_class_b_baselines` queries every Class B state table and returns the documented record shape.
- [ ] Logging emits one INFO line per invocation plus per-slice start/end lines.
- [ ] An orchestrator exception propagates; the temp directory is cleaned up.
- [ ] Determinism: two replays of the same `(slice, candidate)` produce equal `SliceReplayResult`.
- [ ] Different-config sanity test: a looser `volume_anomaly_sigma` produces fewer anomalies on the same slice.
- [ ] Isolation test: replay succeeds even when the runtime DB path does not exist.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

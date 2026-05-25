## Context

`run_external_distillation` hardcodes `contract_scope=()` at two call sites — `src/alphamind/distillation/orchestrator.py:250` (passed to `refresh_contract_history`) and `src/alphamind/distillation/orchestrator.py:437` (passed to `compute_prediction_market_deltas`).

Result: `distillation_contract_history` is never populated despite the data layer carrying \~50K `prediction_market_contracts` rows and \~2.6M snapshots. Per `docs/design/01-data-layer/external/qualitative.md` § 3, every invocation should refresh deltas for in-scope contracts so the analysis pipeline sees trajectory, not just current level.

This issue was originally drafted assuming the `category` column on `prediction_market_contracts` was populated. It is not — 100% of rows are `category='other'` because the upstream categorizer is broken (see [ALP-283](https://linear.app/alphamind-jatassi/issue/ALP-283/rebuild-prediction-market-categorizer-consolidate-vendors-vendor-pre)). The categorizer rebuild lands first; once categories converge over 1–2 ingestion cycles, this issue's scope-resolution wiring becomes unblocked.

## Scope

**(1) Canonical taxonomy (matches** [ALP-283](https://linear.app/alphamind-jatassi/issue/ALP-283/rebuild-prediction-market-categorizer-consolidate-vendors-vendor-pre)**).** 11 categories plus sentinel `other`: `monetary_policy`, `antitrust`, `trade`, `financial_regulation`, `fiscal_policy`, `election`, `opec`, `conflict`, `sanctions`, `china_policy`, `corporate_action`.

**(2) New YAML config keys.** Add to `config/distillation.yaml` under the existing `prediction_market` block:

```yaml
prediction_market:
  prediction_market_delta_pp_threshold:           5.0
  prediction_market_low_liquidity_volume_min_usd: 10000
  tracked_default_min_volume_24h_usd:             5000   # new — default liquidity floor
  tracked_categories:                                    # new — map form, category-as-key
    monetary_policy: {}
    opec: {min_volume_24h_usd: 1000}
    election: {min_volume_24h_usd: 25000}
    china_policy: {}
    conflict: {}
```

Map form (category as YAML key) with optional per-category `min_volume_24h_usd` override of the default floor. Pydantic key REQUIRED — operator must explicitly write `tracked_categories: {}` to disable. Update `src/alphamind/config/models/distillation.py` accordingly.

**(3) New scope-resolution module.** New `src/alphamind/distillation/contract_scope.py` exposing `resolve_prediction_market_scope(session, *, config, as_of) -> tuple[str, ...]`. SQL: `category IN tracked_categories` AND `(resolution_date > as_of OR resolution_date IS NULL)`, then per-category `min_volume_24h_usd` floor applied against the latest `prediction_market_snapshots.volume_24h_usd` per contract. ISO 8601 `Z`-suffix lex comparison is safe — both Polymarket and Kalshi emit consistent format (verified against production DB).

**(4) Orchestrator wiring.** Resolve scope ONCE at the top of `run_external_distillation` and thread the resulting `tuple[str, ...]` through to BOTH call sites — `_refresh_class_b_state` (for `refresh_contract_history` at line 250) AND `_compute_qualitative_blocks` (for `compute_prediction_market_deltas` at line 437). Both consumers MUST receive the same tuple — else `refresh_contract_history` writes for set X but `compute_prediction_market_deltas` reads for set Y.

**(5) Drop deferred flag.** In `src/alphamind/scripts/verify_distillation.py:105`, remove `deferred=True` on the `distillation_contract_history` probe. Once orchestrator wiring lands, the probe joins the same STALE/HEALTHY regime as its siblings.

## Acceptance criteria

`tests/distillation/test_contract_scope.py` covers: empty `tracked_categories`, single-category resolution, per-category `min_volume_24h_usd` override (overrides default), missing-category in DB, ISO 8601 `as_of` boundary comparison.

`run_external_distillation` end-to-end fixture updated to populate `tracked_categories` and asserts the resolved scope reaches both `refresh_contract_history` and `compute_prediction_market_deltas`.

After a real invocation against the production DB (post-[ALP-283](https://linear.app/alphamind-jatassi/issue/ALP-283/rebuild-prediction-market-categorizer-consolidate-vendors-vendor-pre)-merge and post-category-convergence), `SELECT COUNT(*) FROM distillation_contract_history` is non-zero.

`uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest -n auto` clean.

`uv run python -m alphamind.scripts.verify_distillation` reports `distillation_contract_history` HEALTHY (no longer DEFERRED).

## Stage 2 of staged-B PR sequencing

Ship [ALP-283](https://linear.app/alphamind-jatassi/issue/ALP-283/rebuild-prediction-market-categorizer-consolidate-vendors-vendor-pre) (categorizer rebuild) PR first. Wait 1–2 ingestion cycles for category populate. Verify category distribution looks sensible via `SELECT category, COUNT(*) FROM prediction_market_contracts GROUP BY category`. Then ship this PR.

## Reference — production scope sizing (sanity check)

Description-keyword counts on currently-active contracts (resolution_date > now or null), as of investigation time. `monetary_policy` \~179, `opec` \~4, `election` \~1,880 (dominant — per-category liquidity floor is the right tool), `china_policy` \~107, `conflict` \~249, `sanctions` \~1, `antitrust` \~3, `corporate_action` \~32.

Worst case all 11 categories enabled with default floor = \~2,455 contracts/cycle. Each refresh cycle does \~3 DB ops/contract = \~7,500 ops/cycle. Trivial compute.
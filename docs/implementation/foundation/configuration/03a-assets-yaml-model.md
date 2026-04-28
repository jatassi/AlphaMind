---
status: in_progress
completed_date:
commit_id:
---

# 03a — `assets.yaml` Pydantic model

## Goal

Add a Pydantic model for the existing `config/assets.yaml` file and wire it into the configuration package. The file is the authoritative ticker universe scoping all external data collection (already shipped); this story formalizes its schema so every consumer that reads `assets.yaml` does so through a typed model with parse-time validation.

## Reading

- `config/assets.yaml` — existing file; do not modify in this story
- `docs/design/configuration-management.md` § `assets.yaml` — schema description
- `docs/design/asset-universe-validation.md` — operator-driven validation procedure (semantic checks land in story 06b, not here)
- `docs/design/configuration-management.md` § Validation — semantic-self-test rules that touch `assets.yaml` (ticker uniqueness, format regex, `last_full_validation` ≤ today, sector non-emptiness)
- `src/alphamind/config/models/__init__.py` — re-export pattern from story 02

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `src/alphamind/config/models/assets.py` defining:
  - `AssetRole` (StrEnum: `broad_market`, `breadth`, `sector_etf`, `intermarket` per the existing `assets.yaml` `benchmarks:` map)
  - `DiscoveryVendor` (StrEnum: `spdr`, `ishares` per the existing `discovery_sources:` map)
  - `DiscoverySource` (BaseModel: `etf: str`, `vendor: DiscoveryVendor`, `ishares_product_id: str | None = None`)
  - `Benchmark` (BaseModel: `role: AssetRole`, `description: str`)
  - `AssetsConfig` (BaseModel: `last_full_validation: date | None`, `discovery_sources: dict[str, DiscoverySource]`, `sectors: dict[str, list[str]]`, `benchmarks: dict[str, Benchmark]`)
- Field-level validators inside the model — type checks only, no cross-file or whole-tree coherence:
  - Every ticker symbol (in any sector list, in any benchmark key) matches `^[A-Z][A-Z0-9.]*$`. Reject lowercase, leading digits, embedded spaces.
  - `last_full_validation`, when present, parses as a date.
- Re-export `AssetsConfig` (and the supporting enums + nested models) from `models/__init__.py`.
- Unit tests covering: shipped `config/assets.yaml` parses cleanly; ticker-format validation rejects each bad shape; missing `last_full_validation` is acceptable (the field is optional); a malformed date is rejected.

Out of scope:
- Cross-file invariants — every sector in any profile's `active_sectors` must be a key in `assets.yaml.sectors:` (story 06a).
- Universe-validation invariants — every ticker unique across all sectors and benchmarks; `last_full_validation ≤ today`; every sector has ≥1 ticker (story 06b).
- Wiring `AssetsConfig` into `_common.py`'s `load_config()` aggregate (story 08; the runtime aggregate is reshaped end-to-end at integration time).
- Modifying `config/assets.yaml` — this story formalizes the existing file, not edits it.

## Notes

Pydantic v2 supports `date` natively via `datetime.date`. Use `date | None` for `last_full_validation` since the field may be absent on a freshly seeded universe.

The `discovery_sources` keys are sector names (`tech`, `semis`, `financials`, `energy`); they overlap with `sectors:` keys but the model should not enforce that — `discovery_sources` is a metadata aid for the offline universe-discovery script and may legitimately omit sectors that don't have a discovery ETF, or carry historical sectors no longer in `sectors:`. Whole-tree coherence belongs in 06a.

`AssetsConfig.sectors` is `dict[str, list[str]]` rather than typed by sector enum because the master sector list is not closed by this feature — adding a sector is an operator edit. Cross-reference checks against any closed enum (e.g., per-profile `active_sectors`) belong in 06a.

The `benchmarks:` map's key is the ticker; the value carries `role` + `description`. Validate the ticker key against the same regex as sector tickers.

Take care with YAML 1.1 footguns: bare `ON`, `OFF`, `YES`, `NO`, `TRUE`, `FALSE` parse as booleans. The existing file already quotes `"ON"` for that reason. The Pydantic validator does not need to handle the unquoted case — `yaml.safe_load` raises before the model sees the value, and the existing file is correct.

Use `model_config = ConfigDict(frozen=True)` on every model in this story to match the immutability convention from the existing collector models.

## Acceptance criteria

- [ ] `src/alphamind/config/models/assets.py` exists and defines `AssetRole`, `DiscoveryVendor`, `DiscoverySource`, `Benchmark`, `AssetsConfig`.
- [ ] `models/__init__.py` re-exports `AssetsConfig`, `AssetRole`, `DiscoveryVendor`, `DiscoverySource`, `Benchmark`.
- [ ] `AssetsConfig.model_validate(yaml.safe_load(open("config/assets.yaml")))` parses the shipped file cleanly.
- [ ] A unit test asserts the shipped `config/assets.yaml` parses and the resulting `AssetsConfig` exposes the expected sector list (tech contains `AAPL`, semis contains `NVDA`, financials contains `JPM`, energy contains `COP`).
- [ ] A unit test asserts a sector containing a lowercase ticker raises `ValidationError`.
- [ ] A unit test asserts a sector containing `"123XYZ"` (digit-leading) raises `ValidationError`.
- [ ] A unit test asserts a benchmark key containing a space raises `ValidationError`.
- [ ] A unit test asserts an absent `last_full_validation` is accepted; a malformed date string is rejected.
- [ ] All `assets.py` Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

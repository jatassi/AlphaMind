---
status: not_started
completed_date:
commit_id:
---

# 04d — `overlays/` bundle (pre-event / stress)

## Goal

Land the two overlay files under `config/overlays/` and a Pydantic model that validates each one. Overlays are runtime-resolved (zero or more active per invocation) and apply additive multiplicative tightening on top of the active regime — pre-event tightens before scheduled catalysts (FOMC, CPI, earnings); stress tightens on funding-stress / liquidity-distress signals from the distillation layer.

## Reading

- `docs/design/configuration-management.md` § `overlays/pre-event.yaml` — schema and worked example
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Override conditions — pre-event and stress overlay design
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Distillation layer anomaly alerts — stress overlay activation criteria
- `src/alphamind/config/models/guardrails.py` (post-story-03f) — rule IDs the multipliers reference
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)
- 03f (guardrails.yaml — rule IDs the multipliers reference)

## Scope

In scope:
- `config/overlays/pre-event.yaml` and `config/overlays/stress.yaml`.
- `pre-event.yaml` populated with the design-doc worked example:
  - `activation:` block with `windows_before_event: 2` (invocations) and `events: [fomc, cpi, ppi, pce, nfp, earnings]`
  - `multipliers:` block with the position-size tightening (e.g., `position_max_size_pct: 0.80`)
  - `final_invocation_before_event:` block with `block_new_positions: true`
- `stress.yaml` populated with the values from `regime-adaptation.md § Distillation layer anomaly alerts`:
  - `activation:` block with `triggers:` listing the distillation alerts that activate the overlay (e.g., `funding_stress_composite`, `market_liquidity_score`)
  - `multipliers:` block tightening exposure-related limits by ~15% (sector_concentration_pct: 0.85, net_long_pct: 0.85, net_short_pct: 0.85, gross_exposure_pct: 0.85 per the design doc; transcribe verbatim)
  - No `final_invocation_before_event` block (stress overlays have no pre-deactivation step)
- `src/alphamind/config/models/overlays.py` defining:
  - `Overlay` (StrEnum: `pre_event`, `stress` — closed set; member values match filename stems with `_` replacing `-`)
  - `EventType` (StrEnum: `fomc`, `cpi`, `ppi`, `pce`, `nfp`, `earnings`)
  - `StressTrigger` (StrEnum: `funding_stress_composite`, `market_liquidity_score`)
  - `PreEventActivation` (BaseModel: `windows_before_event: int = Field(ge=1)`, `events: list[EventType]`)
  - `StressActivation` (BaseModel: `triggers: list[StressTrigger]`)
  - `FinalInvocationBeforeEvent` (BaseModel: `block_new_positions: bool`)
  - `PreEventOverlay` (BaseModel: `activation: PreEventActivation`, `multipliers: dict[str, float]`, `final_invocation_before_event: FinalInvocationBeforeEvent`)
  - `StressOverlay` (BaseModel: `activation: StressActivation`, `multipliers: dict[str, float]`)
- A model validator on every `multipliers` field enforcing: non-empty; every key matches `^[a-z][a-z0-9_]*$`; every value is `> 0`.
- A loader helper `load_overlays(config_dir: Path) -> dict[Overlay, PreEventOverlay | StressOverlay]` (extending `src/alphamind/config/loaders.py`) that reads every `overlays/{name}.yaml` for every `Overlay` enum member, parses each into the matching variant, and returns a frozen mapping.
- Filename-to-enum mapping in `load_overlays`: enum members use underscores (`pre_event`); filenames use hyphens (`pre-event.yaml`) per the design doc's worked example. The loader bridges the two.
- Re-export `Overlay`, `PreEventOverlay`, `StressOverlay`, `PreEventActivation`, `StressActivation`, `EventType`, `StressTrigger`, `FinalInvocationBeforeEvent`, and `load_overlays` from `models/__init__.py` / `loaders` namespace.
- Unit tests covering: both shipped overlay files parse cleanly; an empty `multipliers` map raises; a zero or negative multiplier raises; an unknown event type raises; an unknown stress trigger raises; `windows_before_event: 0` raises.

Out of scope:
- Cross-reference — every key in `multipliers` is a rule ID in `guardrails.yaml` (story 06a).
- Activation logic — reading the event calendar to determine whether `pre-event` is active for the current invocation; reading distillation output to determine whether `stress` is active. Both are pipeline-runtime concerns, not config.
- Composition arithmetic — applying overlay multipliers on top of the regime cascade (story 05).

## Notes

**`Overlay` StrEnum value vs. filename.** Same hyphen-vs-underscore convention as `regimes/` (story 04b). The loader bridges via a one-line mapping; do not invent a more general transform.

**Different overlays have different shapes.** `PreEventOverlay` has a `final_invocation_before_event` block that `StressOverlay` does not. The loader returns a discriminated union (`PreEventOverlay | StressOverlay`); the resolver reads each by name and dispatches accordingly.

**`EventType` and `StressTrigger` are closed enums.** Adding a new event type or stress trigger is a coordinated change touching both the enum and the upstream activation logic; the closed enum forces that coordination.

**Stress-overlay multiplier values.** `regime-adaptation.md` says "tightens exposure-related limits by an additional 15%" — implement as `0.85` for the four exposure rules (`sector_concentration_pct`, `net_long_pct`, `net_short_pct`, `gross_exposure_pct`). Other rules carry no multiplier in stress (omit them; do not add `1.0` no-op entries — overlay multipliers are explicitly a *partial* map applied additively, unlike regime multipliers which cover every rule).

**Contrast with `regimes/`.** Regime files declare every rule's multiplier (including 1.0 for `normal`). Overlays declare only the rules they actually tighten — partial maps composed multiplicatively against the regime-resolved values. The model accepts any non-empty `multipliers` map without insisting on full coverage.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/overlays/pre-event.yaml` and `config/overlays/stress.yaml` exist.
- [ ] `pre-event.yaml` declares `activation` (with `windows_before_event` and `events`), `multipliers`, `final_invocation_before_event`.
- [ ] `stress.yaml` declares `activation` (with `triggers`) and `multipliers`. Has no `final_invocation_before_event`.
- [ ] Stress multipliers tighten the four exposure rules by 0.85 per the design doc.
- [ ] `src/alphamind/config/models/overlays.py` defines `Overlay`, `PreEventOverlay`, `StressOverlay`, the two activation types, the two trigger enums, and `FinalInvocationBeforeEvent`.
- [ ] `src/alphamind/config/loaders.py` defines `load_overlays(config_dir: Path) -> dict[Overlay, PreEventOverlay | StressOverlay]`.
- [ ] `models/__init__.py` re-exports the names.
- [ ] A unit test asserts both shipped overlay files parse cleanly via `load_overlays()`.
- [ ] A unit test asserts an empty `multipliers` map raises `ValidationError`.
- [ ] A unit test asserts `multipliers: {x: 0}` raises `ValidationError`.
- [ ] A unit test asserts `multipliers: {x: -0.5}` raises `ValidationError`.
- [ ] A unit test asserts `events: [eclipse]` raises `ValidationError`.
- [ ] A unit test asserts `triggers: [scary_news]` raises `ValidationError`.
- [ ] A unit test asserts `windows_before_event: 0` raises `ValidationError`.
- [ ] A unit test asserts the loader maps filename `pre-event.yaml` to enum member `Overlay.pre_event`.
- [ ] A unit test asserts the loader returns `PreEventOverlay` for `pre-event.yaml` and `StressOverlay` for `stress.yaml`.
- [ ] A unit test asserts `load_overlays()` raises when an overlay file is missing.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

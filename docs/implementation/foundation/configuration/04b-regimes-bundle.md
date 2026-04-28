---
status: not_started
completed_date:
commit_id:
---

# 04b — `regimes/` bundle (low-vol / normal / elevated / crisis)

## Goal

Land the four regime files under `config/regimes/` and a Pydantic model that validates each one. Regimes are runtime-resolved by the distillation layer based on VIX and supporting indicators; each file declares the multiplier table applied to every guardrail rule plus the transition mechanics (immediate tightening, gradual loosening over three invocations).

## Reading

- `docs/design/configuration-management.md` § `regimes/elevated.yaml` — schema and worked example
- `docs/design/06-risk-guardrails/regime-adaptation.md` — full multiplier tables per regime, transition mechanics, design rationale for the multiplier choices
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Parameter sets per regime — authoritative multiplier values for all four regimes
- `docs/design/06-risk-guardrails/regime-adaptation.md` § Transition mechanics — `tighten_on_entry`, `loosen_on_exit` semantics
- `src/alphamind/config/models/guardrails.py` (post-story-03f) — the canonical 19 rule IDs the multipliers must cover
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)
- 03f (guardrails.yaml — rule IDs the multipliers reference)

## Scope

In scope:
- `config/regimes/low-vol.yaml`, `config/regimes/normal.yaml`, `config/regimes/elevated.yaml`, `config/regimes/crisis.yaml` — one file per regime, populated with the values from `regime-adaptation.md § Parameter sets per regime`.
- Each regime file declares:
  - `vix_range: [float, float]` — the VIX band that maps to this regime per `regime-adaptation.md`
  - `multipliers:` — `dict[str, float]` keyed by guardrail rule ID. Every key is a rule ID from `guardrails.yaml`. Values are the multiplicative adjustment to the base limit (< 1.0 tightens, > 1.0 loosens).
  - `transition:` block with `tighten_on_entry: str` (`immediate` per the design) and `loosen_on_exit: str` (`linear_over_invocations_3` per the design)
- The `normal` regime is the **base** — its `multipliers` map should declare every rule ID at exactly `1.0`. The model does not skip the base regime; uniform-1.0 entries make the cascade arithmetic in story 05 simpler (it composes every regime the same way).
- `src/alphamind/config/models/regimes.py` defining:
  - `Regime` (StrEnum: `low_vol`, `normal`, `elevated`, `crisis` — closed set; member values match filename stems with `_` replacing `-` per Python naming)
  - `TightenOnEntry` (StrEnum: `immediate`)
  - `LoosenOnExit` (StrEnum: `linear_over_invocations_3`)
  - `TransitionPolicy` (BaseModel: `tighten_on_entry: TightenOnEntry`, `loosen_on_exit: LoosenOnExit`)
  - `RegimeConfig` (BaseModel: `vix_range: tuple[float, float]`, `multipliers: dict[str, float]`, `transition: TransitionPolicy`)
- A model validator on `RegimeConfig` enforcing:
  - `vix_range[0] ≤ vix_range[1]`, both ≥ 0
  - `multipliers` is non-empty; every key matches `^[a-z][a-z0-9_]*$`; every value is `> 0` (zero or negative multipliers would invert the rule)
- A loader helper `load_regimes(config_dir: Path) -> dict[Regime, RegimeConfig]` (extending `src/alphamind/config/loaders.py` from story 04a) that reads every `regimes/{name}.yaml` for every `Regime` enum member, validates each, and returns a frozen mapping.
- Filename-to-enum mapping in `load_regimes`: enum members use underscores (`low_vol`); filenames use hyphens (`low-vol.yaml`) per the design doc's worked example. The loader bridges the two.
- Re-export `RegimeConfig`, `Regime`, `TransitionPolicy`, `TightenOnEntry`, `LoosenOnExit`, and `load_regimes` from `models/__init__.py` / `loaders` namespace.
- Unit tests covering: every shipped regime file parses cleanly; `vix_range: [22, 14]` raises; a `multipliers` map containing a zero or negative value raises; an unknown `tighten_on_entry` value raises; the `normal` regime's `multipliers` are all 1.0.

Out of scope:
- Cross-reference — every key in `multipliers` is a rule ID in `guardrails.yaml`; every regime's `multipliers` covers every rule present in any profile (story 06a).
- Semantic invariants — the four regimes' `vix_range` values are contiguous and monotonic (`low_vol.upper == normal.lower`, etc.); no multiplier drives a rule to zero or negative for any profile (story 06b).
- Pre-event and stress overlays — owned by story 04d (`overlays/`).
- Transition arithmetic (linear interpolation over three invocations) — pipeline runtime, owned by the resolver in story 05 + invocation-state stores not yet built.
- Composition arithmetic (multiply profile base × regime multiplier × overlays) — story 05.

## Notes

**`Regime` StrEnum value vs. filename.** The design doc uses hyphenated filenames (`low-vol.yaml`) for readability. Python identifiers must be underscore-named. The loader bridges via a one-line mapping; do not invent a more general transform. The four members are a closed set.

**The `normal` regime as 1.0 base.** This is a deliberate decision: storing 1.0 multipliers explicitly for every rule makes the resolver's cascade arithmetic uniform across regimes and avoids special-casing `normal`. The cost is repetition in the YAML — acceptable for a one-line review surface.

**Multiplier-table source-of-truth.** `regime-adaptation.md § Parameter sets per regime` is the authoritative table. Quote multipliers verbatim. The doc shows multiplier values like `×1.2`, `×0.7`, etc.; transcribe as `1.2`, `0.7`. Drawdown-related rules show "no change" in low-vol — encode as `1.0`, not as a magic value.

**`transition.tighten_on_entry` and `loosen_on_exit`** are universally `immediate` and `linear_over_invocations_3` for the four canonical regimes. The enum members are present so the model rejects typos (e.g., `tightening: immediate` would raise). Future regimes might use other strategies; adding a new enum member is the contract for that change.

**`vix_range` overlap on boundaries.** `regime-adaptation.md`'s table shows VIX boundaries as touching, not overlapping (`14, 22, 35`). The classification rule (which regime a VIX value of exactly 14 falls into) is a distillation-layer concern, not a config concern; the model accepts any pair and stores them literally.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model.

## Acceptance criteria

- [ ] `config/regimes/low-vol.yaml`, `normal.yaml`, `elevated.yaml`, `crisis.yaml` exist.
- [ ] Each regime declares `vix_range`, `multipliers`, `transition`.
- [ ] The `normal` regime's `multipliers` are all `1.0`.
- [ ] Multipliers in `low-vol`, `elevated`, `crisis` match the values in `regime-adaptation.md § Parameter sets per regime`.
- [ ] `src/alphamind/config/models/regimes.py` defines `RegimeConfig`, `Regime`, `TransitionPolicy`, `TightenOnEntry`, `LoosenOnExit`.
- [ ] `src/alphamind/config/loaders.py` defines `load_regimes(config_dir: Path) -> dict[Regime, RegimeConfig]`.
- [ ] `models/__init__.py` re-exports the five names.
- [ ] A unit test asserts every shipped regime file parses cleanly via `load_regimes()` and the resulting mapping has four entries.
- [ ] A unit test asserts `vix_range: [22, 14]` raises `ValidationError`.
- [ ] A unit test asserts `multipliers: {position_max_size_pct: 0}` raises `ValidationError`.
- [ ] A unit test asserts `multipliers: {position_max_size_pct: -0.5}` raises `ValidationError`.
- [ ] A unit test asserts `transition.tighten_on_entry: tightening` raises `ValidationError`.
- [ ] A unit test asserts `transition.loosen_on_exit: linear_over_invocations_5` raises `ValidationError`.
- [ ] A unit test asserts the loader maps filename `low-vol.yaml` to enum member `Regime.low_vol`.
- [ ] A unit test asserts `load_regimes()` raises when a regime file is missing.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

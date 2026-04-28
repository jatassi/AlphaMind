---
status: in_progress
completed_date:
commit_id:
---

# 03b — `main.yaml` + Pydantic model

## Goal

Land `config/main.yaml` (the composition root that pins operator-selected identity dimensions and declares filesystem paths) and a Pydantic model that types-checks it. This is the file the resolver loads first to discover which profile, execution mode, and paths are active.

## Reading

- `docs/design/configuration-management.md` § `main.yaml` — schema and worked example
- `docs/design/configuration-management.md` § Composition model — confirms `active_profile` is operator-pinned and sourced from this file
- `docs/design/configuration-management.md` § Runtime vs. deploy-time classification — `paths:` are deploy-time-only; `active_profile` and `execution_mode` are invocation-time-reload
- `docs/architecture/infrastructure.md` § Process layout — the `%USERPROFILE%\AlphaMind\` path conventions on Windows
- `src/alphamind/config/models/__init__.py` — re-export pattern from story 02
- `docs/design/06-risk-guardrails/rules-and-limits.md` § Portfolio size profiles — names the four valid profile values (micro, small, medium, large)

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/main.yaml` populated with the design-doc worked example values, adjusted to match the current development environment:
  - `active_profile: medium` (default; operator edits to switch)
  - `execution_mode: paper`
  - `paths:` block with `database`, `logs`, `archive`, `prompts` keys
- `src/alphamind/config/models/main.py` defining:
  - `ExecutionMode` (StrEnum: `paper`, `live`)
  - `Profile` (StrEnum: `micro`, `small`, `medium`, `large` — the closed set from `rules-and-limits.md`)
  - `Paths` (BaseModel: `database: str`, `logs: str`, `archive: str`, `prompts: str` — all strings, not `Path`, since the values may carry Windows environment-variable expansions like `%USERPROFILE%` that are resolved at OS boundary, not at YAML parse)
  - `MainConfig` (BaseModel: `active_profile: Profile`, `execution_mode: ExecutionMode`, `paths: Paths`)
- Re-export `MainConfig`, `ExecutionMode`, `Profile`, `Paths` from `models/__init__.py`.
- Unit tests covering: shipped `config/main.yaml` parses cleanly; an unknown profile name (e.g., `huge`) raises `ValidationError`; an unknown execution mode raises; a missing `paths` block raises; each missing path key raises.

Out of scope:
- Path *resolution* (expanding `%USERPROFILE%`, normalizing slashes, asserting existence) — the model carries the literal string. Resolution is a runtime concern owned by callers.
- Cross-reference invariant — `active_profile` must name an existing file under `profiles/` (story 06a).
- Wiring `MainConfig` into the loader aggregate (story 08).

## Notes

Path values in `main.yaml` are written with backslashes on Windows (`%USERPROFILE%\AlphaMind\data\alphamind.db`). YAML accepts backslashes inside single-quoted scalars; the design doc's example uses single quotes for that reason. The model should not normalize or re-escape — it stores the raw string for downstream resolution.

The `Profile` enum's value list is duplicated between `main.yaml`'s validator and the per-profile schema (story 04a). That is intentional: closing the set at the model layer catches typos in `active_profile` before any cross-file resolution runs. The per-profile story validates that each enum member has a corresponding file.

Do not validate that the `prompts/` path exists — tests run on macOS where `%USERPROFILE%` does not resolve, and the design doc explicitly defers existence checks to runtime callers.

Use `model_config = ConfigDict(frozen=True)` on every Pydantic model in this story.

The shipped `main.yaml` should default to `active_profile: medium` and `execution_mode: paper`. These are reasonable starting values for a development checkout; the operator overrides on the trading machine. Do not commit a value of `live` for `execution_mode`.

## Acceptance criteria

- [ ] `config/main.yaml` exists and contains `active_profile`, `execution_mode`, and a `paths:` block with `database`, `logs`, `archive`, `prompts` keys.
- [ ] `config/main.yaml` parses cleanly via `yaml.safe_load` and validates against `MainConfig`.
- [ ] `src/alphamind/config/models/main.py` defines `ExecutionMode`, `Profile`, `Paths`, `MainConfig`.
- [ ] `models/__init__.py` re-exports the four names.
- [ ] A unit test asserts the shipped `config/main.yaml` parses and exposes the expected fields.
- [ ] A unit test asserts `active_profile: huge` raises `ValidationError`.
- [ ] A unit test asserts `execution_mode: backtest` raises `ValidationError`.
- [ ] A unit test asserts a missing `paths.database` key raises `ValidationError`.
- [ ] A unit test asserts a `paths.database` value containing `%USERPROFILE%` is preserved verbatim in the model (no normalization).
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

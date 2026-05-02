---
status: done
completed_date: 2026-04-27
commit_id: 9424b34
---

# 03d — `venue.yaml` + Pydantic model

## Goal

Land `config/venue.yaml` (Alpaca venue parameters and equity-session hours) and a Pydantic model. The file is the broker-adapter's source of truth for paper/live URLs, API-key environment-variable references, rate limits, and session windows; the model gives every downstream consumer typed access.

## Reading

- `docs/design/configuration-management.md` § `venue.yaml` — schema and worked example
- `docs/design/05-execution-layer/venue-configuration.md` — full venue contract (settlement, sessions, PDT, Reg T margin tiers); only the operator-tunable subset lives in YAML, the rest is in code per `configuration-management.md § In code`
- `docs/design/05-execution-layer/broker-adapter.md` — the broker adapter is the primary consumer
- `src/alphamind/config/models/data_sources.py` (post-story-02) — pattern for `*_env` reference fields
- `src/alphamind/config/models/__init__.py` — re-export pattern

## Depends on

- 02 (models package skeleton)

## Scope

In scope:
- `config/venue.yaml` populated with the design-doc worked example:
  - `alpaca:` block with `paper:` and `live:` sub-blocks each carrying `rest_url`, `ws_url`, `api_key_env`, `api_secret_env`
  - `alpaca.rate_limit_per_minute`
  - `session_hours:` with `regular`, `pre_market`, `after_hours` blocks each carrying `open` and `close` strings
- `src/alphamind/config/models/venue.py` defining:
  - `AlpacaCredentials` (BaseModel: `rest_url: str`, `ws_url: str`, `api_key_env: str`, `api_secret_env: str`)
  - `Alpaca` (BaseModel: `paper: AlpacaCredentials`, `live: AlpacaCredentials`, `rate_limit_per_minute: int = Field(ge=1)`)
  - `SessionWindow` (BaseModel: `open: str`, `close: str` — both strings of form `HH:MM`)
  - `SessionHours` (BaseModel: `regular: SessionWindow`, `pre_market: SessionWindow`, `after_hours: SessionWindow`)
  - `VenueConfig` (BaseModel: `alpaca: Alpaca`, `session_hours: SessionHours`)
- Field-level validators:
  - `rest_url` and `ws_url` start with `https://` and `wss://` respectively. Reject any other scheme.
  - `api_key_env` and `api_secret_env` match `^[A-Z][A-Z0-9_]*$` (env var name convention).
  - `SessionWindow.open` / `.close` match `^[0-2][0-9]:[0-5][0-9]$`. The model does not enforce the wider rules (e.g., open ≤ close, hours within 0–23) — the regex is sufficient to catch typos; finer validation belongs to runtime callers if they need it.
- Re-export `VenueConfig`, `AlpacaCredentials`, `Alpaca`, `SessionWindow`, `SessionHours` from `models/__init__.py`.
- Unit tests covering: shipped `config/venue.yaml` parses cleanly; non-https `rest_url` raises; lowercase `api_key_env` raises; malformed `open: 9:30` (single-digit hour) raises; missing `live:` block raises.

Out of scope:
- Cross-reference invariant — `api_key_env` and `api_secret_env` references must resolve in `.env` (story 06a, runs against the loaded env, not against `.env.example`).
- Validating `paper`/`live` selection — the active sub-block is selected at composition time using `MainConfig.execution_mode` (story 05).
- IANA timezone validation on session hours — sessions are local to the venue's market timezone (NYSE in `US/Eastern` from `scheduler.yaml`); the model carries the literal string.
- Settlement rules, PDT thresholds, Reg T margin tiers — all in code per `configuration-management.md § In code`.
- Wiring `VenueConfig` into the loader aggregate (story 08).

## Notes

The session-hours regex (`^[0-2][0-9]:[0-5][0-9]$`) accepts `25:00`, `26:13`, etc. Tightening to `^([01][0-9]|2[0-3]):[0-5][0-9]$` is reasonable and rejects those — use the tighter form. The pre-market window starts at `04:00`, the after-hours window ends at `20:00`, both are within 0–23.

`AlpacaCredentials.rest_url` and `ws_url` are validated by scheme prefix only — full URL parsing is deferred. The values are passed to the Alpaca SDK which performs its own validation.

`api_key_env` and `api_secret_env` are *names of env vars*, not the values. The values resolve at runtime by `os.environ[name]` lookup. Do not validate against `.env` here — that cross-reference belongs in story 06a.

The `live:` block must always be present, even on a paper-only checkout. Missing values for `api_key_env` resolution are caught at runtime when the operator switches `execution_mode` to `live`. Allowing the file to omit `live:` would mask a configuration gap that matters at the moment the operator goes live.

For the shipped file, use placeholder env-var names that follow the convention (e.g., `ALPACA_PAPER_KEY`, `ALPACA_PAPER_SECRET`, `ALPACA_LIVE_KEY`, `ALPACA_LIVE_SECRET`). Do not put real keys in the file.

Use `model_config = ConfigDict(frozen=True)` on every model.

## Acceptance criteria

- [ ] `config/venue.yaml` exists with the structure described in Scope.
- [ ] `config/venue.yaml` parses cleanly via `yaml.safe_load` and validates against `VenueConfig`.
- [ ] `src/alphamind/config/models/venue.py` defines `VenueConfig` and the four nested models.
- [ ] `models/__init__.py` re-exports the five names.
- [ ] A unit test asserts the shipped `config/venue.yaml` parses and the resulting `VenueConfig` exposes both `paper.api_key_env` and `live.api_key_env`.
- [ ] A unit test asserts an `http://` `rest_url` raises `ValidationError`.
- [ ] A unit test asserts a `ws://` (non-`wss`) `ws_url` raises `ValidationError`.
- [ ] A unit test asserts a lowercase `api_key_env` value raises `ValidationError`.
- [ ] A unit test asserts a `regular.open: "25:00"` raises `ValidationError`.
- [ ] A unit test asserts a missing `alpaca.live` block raises `ValidationError`.
- [ ] A unit test asserts a missing `session_hours.pre_market` block raises `ValidationError`.
- [ ] All Pydantic models declare `model_config = ConfigDict(frozen=True)`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy && uv run pytest` all pass.

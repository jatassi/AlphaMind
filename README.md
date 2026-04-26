# AlphaMind

Autonomous LLM-powered swing trading system. 4–72h horizon, 60–80 US equities across tech/semis/financials/energy.

See [`docs/architecture/`](docs/architecture/) and [`docs/design/`](docs/design/) for the full architecture and design.

## Setup

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
brew install ta-lib       # native dependency for the TA-Lib Python wrapper
uv sync                   # creates .venv and installs all dependencies
```

Secrets live in `.env` (referenced by env-var name from `config/*.yaml`). See [`docs/design/01-data-layer/api-key-checklist.md`](docs/design/01-data-layer/api-key-checklist.md) for the inventory.

## Scripts

```bash
uv run python scripts/validate_universe.py
```

Validates `config/assets.yaml` against the five inclusion criteria documented in [`docs/design/asset-universe-validation.md`](docs/design/asset-universe-validation.md).

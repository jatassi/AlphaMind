# data_sources/ — external vendor API clients

Data layer (external). One subpackage per vendor (`polygon/`, `finnhub/`, `fred/`,
`bls/`, `eia/`, `finra/`, `sec_edgar/`, `treasury/`, `marketaux/`, `news/`,
`iborrowdesk/`, `prediction_market/{kalshi,polymarket}/`); shared HTTP/retry/parse
helpers in `_common/`. Each client fetches raw quantitative or qualitative data; the
`collector/` service drives them on a schedule. Design intent (historical, verify against
code): `docs/design/01-data-layer/`.

## Key invariants

- Clients are I/O shells: fetch + normalize, no business logic. Keep transformation in the distillation layer.
- Secrets are referenced by env-var name from `config/*.yaml`; never hard-code keys.

## Gotchas

Append gotchas here as you hit them — non-obvious traps not evident from one file. Keep
each to a line or two; delete any that no longer hold.

- Scripts/tests that exercise a live client need `.env` loaded (`load_dotenv()` / `source .env`); production does not auto-source it.

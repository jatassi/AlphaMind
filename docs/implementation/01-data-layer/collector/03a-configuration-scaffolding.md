---
status: in_progress
completed_date:
commit_id:
---

# 03a — Configuration scaffolding

## Goal

Land the YAML configuration files the collector and shared library read at startup, plus their Pydantic schemas and a `.env.example` template.

## Reading

- `docs/design/configuration-management.md` — particularly the `data_sources.yaml` and validation sections
- `docs/design/01-data-layer/collector/data-sources.md` — retry-shape consumers
- `docs/design/01-data-layer/collector/runner.md` § Cadence — collector schedule contents
- `docs/design/01-data-layer/api-key-checklist.md` — env var inventory
- `docs/design/01-data-layer/api-failure-handling.md` — tier-to-shape mapping
- `docs/design/01-data-layer/external/qualitative.md` § 1 — outlet credibility tiers
- `config/assets.yaml` — already exists, do not modify

## Depends on

- 02 (project skeleton — `src/alphamind/config/` directory)

## Scope

In scope:
- `config/data_sources.yaml` — providers + retry_shapes + categories per the shape in `configuration-management.md § data_sources.yaml`. Coverage: every vendor in `data-sources.md`'s module layout.
- `config/collector_schedule.yaml` — per-collector cron expressions per the table in `runner.md § Cadence`. Use the form below.
- `config/news_outlets.yaml` — outlet → credibility tier mapping (`tier_1` / `tier_2` / `tier_3`).
- `.env.example` at the repo root — `KEY=` lines for every env var name in `api-key-checklist.md` § Summary.
- Pydantic schema models under `src/alphamind/config/models.py` — one model per YAML file. Implement parse-time validation of types, enums, required fields, and ranges.
- Unit tests verifying each YAML file parses cleanly into its Pydantic model and that obvious malformations are rejected.

Out of scope:
- Loader code that calls these models at runtime (story 04).
- Real API keys in `.env` (operator concern).
- Configs for other parts of the system (`profiles/`, `regimes/`, `modes/`, `overlays/`, `guardrails.yaml`) — downstream of the data layer.

## Notes

`collector_schedule.yaml` shape (no canonical form yet):

```yaml
timezone: US/Eastern
collectors:
  polygon.equity:        { cron: "*/15 9-16 * * mon-fri" }
  polygon.equity_offhrs: { cron: "0 17-23,0-8 * * mon-fri" }
  polygon.options:       { cron: "0,30 9-16 * * mon-fri" }
  # ... one entry per row in runner.md § Cadence
```

`news_outlets.yaml` shape:

```yaml
outlets:
  Reuters:    { tier: tier_1 }
  Bloomberg:  { tier: tier_1 }
  WSJ:        { tier: tier_1 }
  CNBC:       { tier: tier_2 }
  # ... seed with examples from qualitative.md § 1
```

The Pydantic models should also implement the data-layer-relevant cross-reference validators from `configuration-management.md § Cross-reference`: every category's `primary` and `failover` references an existing provider; every provider's `retry_shape` references an existing shape; every `api_key_env` is present in `.env.example`. (Other cross-references in that doc apply to layers not built yet — skip them for now.)

`data_sources.yaml` schema must support `failover` per category, even though failover dispatch is deferred per `data-sources.md § Multi-source failover` — only the schema slot is needed in v1.

## Acceptance criteria

- [ ] `config/data_sources.yaml` exists, lists every vendor in `data-sources.md`'s module layout, parses cleanly, validates against its Pydantic model.
- [ ] `config/collector_schedule.yaml` exists with one cron entry per collector listed in `runner.md § Cadence`, parses cleanly, validates.
- [ ] `config/news_outlets.yaml` exists with at least the tier_1, tier_2, tier_3 examples from `qualitative.md`, parses cleanly, validates.
- [ ] `.env.example` lists every env var name from `api-key-checklist.md` § Summary as `KEY=` lines (no real values).
- [ ] `src/alphamind/config/models.py` defines one Pydantic model per YAML file.
- [ ] Unit tests load each YAML via its Pydantic model and assert successful parse.
- [ ] Pydantic validators reject unknown `retry_shape` references in `data_sources.yaml`.
- [ ] Pydantic validators reject malformed cron expressions in `collector_schedule.yaml`.
- [ ] Pydantic validators reject unknown `tier` values in `news_outlets.yaml`.
- [ ] Pydantic cross-validation: every `categories[*].primary` resolves to an existing `providers` entry.
- [ ] Pydantic cross-validation: every `providers[*].api_key_env` is a key in `.env.example`.
- [ ] `uv run pytest` passes.

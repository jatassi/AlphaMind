# Source-to-target mapping YAMLs

One YAML per data category (`price_volume`, `derivatives`, `cross_asset`, …) plus `_common.yaml` for shared supporting types. Each file enumerates the entities defined in the matching `schema/<category>.py` module and documents how each field maps from a source system (vendor API, derived computation, LLM extraction) to its target schema field.

These files are documentation artifacts — no runtime code consumes them. They exist so the field-level provenance of every entity is auditable in one place.

## Per-file structure

```yaml
domain: <category>
spec_prefix: <e.g., Q1>
module_description: <short paragraph>
common_fields_note: <reminder that ticker + metadata are inherited>
entities:
  - entity: <ClassName>
    spec_id: <e.g., Q1:1a, REF:UNIVERSE, or "N/A (supporting type)">
    description: <one-line>
    is_primary_entity: <true | false>
    primary_source: <only on primary entities — see below>
    fallback_source: <only on primary entities>
    cadence: <only on primary entities>
    feasibility: <only on primary entities>
    fields:
      - target_field: <name>
        target_type: <Python type>
        description: <one-line>
        source: <vendor or "Derived">
        source_endpoint: <API path or computation reference>
        source_field: <upstream field name or "See transform">
        source_type: <upstream type>
        transform: <how the source maps to the target>
audit_notes: <free-form audit log>
```

## Conventions

**`is_primary_entity`** — `true` when the entity has its own row in [`schema/_registry.py`](../schema/_registry.py) (and is the producer of an invocation-scoped record); `false` for supporting types nested inside primary entities (e.g., `CorrelationPair` inside `IntraSectorCorrelation`, `ETFMembership` inside `SectorClassification`).

**Producer-question columns** — `primary_source`, `fallback_source`, `cadence`, and `feasibility` answer *"what writes this entity, on what schedule, with what risk?"* They appear **only on `is_primary_entity: true` rows**. Supporting types inherit those answers from the parent primary entity that nests them; carrying the columns on supporting types would communicate a decision that does not exist (a supporting type is a field shape, not an independent producer).

**`spec_id`** — Q-bucket identifier from the source-to-target spreadsheet (`Q1:1a`, `Qual4:4b`) or `REF:UNIVERSE` / `REF:SECTOR` for reference-data primaries; `N/A (supporting type)` for supporting types. Must agree with [`schema/_registry.py`](../schema/_registry.py) for primary entities.

**`fields`** — Every public field on the Pydantic class gets one entry. Field-level `transform` is short prose describing the mapping ("Direct map. USD float.", "Distillation: rolling correlation over Polygon OHLCV."). Where the mapping is non-obvious, `See entity docstring for computation logic` defers to the schema source.

## Maintenance

When adding a new entity to a `schema/<category>.py` module, mirror it into the corresponding YAML under `entities`. When the registry changes (`schema/_registry.py`), the affected YAML's `is_primary_entity` and `spec_id` flags must be updated to match. The producer-question columns belong on the primary-entity row only.

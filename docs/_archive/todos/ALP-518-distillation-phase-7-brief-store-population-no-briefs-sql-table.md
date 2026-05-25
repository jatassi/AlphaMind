## Symptom

`src/alphamind/distillation/orchestrator.py:786-820` Phase 7 is a no-op:

```python
def _populate_brief_store(session, *, correlation_regime_brief, invocation_id) -> None:
    """Phase 7 — populate the brief store.

    No-op today: the ``briefs`` table is not in the persistence schema.
    The brief still travels through :class:`DistillationOutputs` so an
    in-process consumer (the synthesizer, the sector analysts) can read
    it without the persistent store.
    """
    ...
    logger.info(
        "phase 7 (brief-store population) skipped: briefs table not yet implemented; "
        "CR brief carried in DistillationOutputs"
    )
```

The only `TODO()` marker in production code (`distillation/orchestrator.py:790`):

> *"TODO(story 12): the brief store (briefs SQL table) per docs/architecture/data-and-state.md § Brief store is not yet implemented in alphamind.persistence.models."*

## Production impact

**None today.** The `CorrelationRegimeBrief` rides in-process via `DistillationOutputs.correlation_regime_brief`; the synthesizer and sector analysts read it from there without persistence. The Phase 7 log line is the only visible effect.

## Why file this anyway

If the architecture later splits the distillation orchestrator from analysis (separate processes, separate restart cycles, replay-after-crash semantics), the in-process passthrough fails — every consumer would need the brief reconstituted from a persistent store. That's a foreseeable but not-yet-real need.

This is tracking the design intent so it isn't lost; not a current bug.

## Scope

**(A) Schema.** Add `briefs` table to `alphamind/persistence/models.py` with columns: `invocation_id` (FK), `brief_kind` (e.g., `correlation_regime`), `reference_index_json`, `text`, `created_at`. Migration in `persistence/migrations/versions/`.

**(B) INSERT.** Replace Phase 7's no-op body with one INSERT per `CR-N` reference in `CorrelationRegimeBrief.reference_index`, keyed by invocation_id.

**(C) Read API.** Add a repository helper to fetch a brief by `invocation_id` and `brief_kind` so future cross-process consumers (replay harness, command-center diagnostic) can hydrate.

## Acceptance criteria

- [ ] `briefs` table exists with migration applied.
- [ ] Phase 7 INSERTs rows per CR reference (or one row per brief, depending on storage shape decision).
- [ ] Repository read helper exists and is covered by a test.
- [ ] `DistillationOutputs.correlation_regime_brief` continues to surface the in-process brief for hot-path consumers (no regression).
- [ ] `uv run pytest -n auto` passes; `uv run lint-imports` passes.

## Verification

* The Phase 7 log line no longer says "skipped"; reports inserted row count.

## Priority

Low — no current production bug. File and leave Todo until a downstream consumer needs cross-process access.
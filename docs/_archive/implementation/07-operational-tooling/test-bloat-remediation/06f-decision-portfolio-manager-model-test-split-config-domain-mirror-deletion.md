# 06f — decision/portfolio_manager model-test split + config_domain mirror deletion

## Goal

Two precise consolidations the verifier scoped exactly: split a mixed discriminator-test bundle in the PM models suite (retire the half covered canonically, keep the half that is a sole guardian), parametrize a 16-method schema-parity class, and delete the per-section `to_domain()` mirror tests subsumed by an exhaustive round-trip.

## Reading

* `tests/decision/portfolio_manager/test_models.py` — `TestOMSCommandDiscriminator` (L957–998), `TestDiscriminatedUnion` (L732–779), `TestPMEnvelopeSchemaParity`, `TestEnvelopeConstruction` (L279).
* `tests/execution/oms/test_command_models.py` — `TestDiscriminatedUnion::test_round_trip` (L749, the canonical OMSCommand-union home).
* `tests/distillation/test_config_domain.py` — the 7 per-section mirrors + `test_yaml_to_pydantic_to_domain_preserves_every_field`.
* Parent `ALP-783`.

## Depends on

* none.

## Scope

`test_models.py` **(verifier — split, do not blanket-consolidate):**

* RETIRE `TestOMSCommandDiscriminator` (L957) — the open/close/adjust/cancel/add routing via `TypeAdapter(OMSCommand)` is duplicated canonically by `tests/execution/oms/test_command_models.py::TestDiscriminatedUnion::test_round_trip` (same five variants, same union).
* KEEP `TestDiscriminatedUnion` (L732–779) — it tests the **PM-envelope** `source_provenance` discriminator (payload → `PMAnalystEnvelope`/`PMStrategistEnvelope`); the surveyor's `subsumed_by` (`TestEnvelopeConstruction`, L279) is FALSE (that builds via `_make_*_envelope()` and never exercises discriminator dispatch). This is the sole guardian — do not delete.
* Parametrize the 16-method `TestPMEnvelopeSchemaParity` over JSON-pointer rows (preserve every method's assertion as a row).

`test_config_domain.py`**:**

* DELETE the 7 per-section `to_domain()` mirror tests — fully subsumed by `test_yaml_to_pydantic_to_domain_preserves_every_field` (exhaustive `model_dump()`-driven round-trip).

## Acceptance criteria

- [ ] `TestOMSCommandDiscriminator` is gone; `TestDiscriminatedUnion` (PM-envelope) is retained.
- [ ] `TestPMEnvelopeSchemaParity` is parametrized with one row per former method.
- [ ] The 7 `config_domain` per-section mirrors are gone; the exhaustive round-trip remains.
- [ ] `coverage report` for `src/alphamind/decision/portfolio_manager/` and `src/alphamind/distillation/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/decision/portfolio_manager/test_models.py tests/distillation/test_config_domain.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; the PM-envelope discriminator test still present (grep).
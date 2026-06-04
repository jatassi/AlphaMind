# 06k — distillation/external q1_assemble fan-out shape consolidation

## Goal

`tests/distillation/external/test_q1_assemble_blocks.py` has five shape-checking classes that each re-run `assemble_q1_blocks` against `populated_session` and assert one structural fact. The surveyor's "covered by `TestDeterminism`" claim is **false** (the verifier confirmed `TestDeterminism::test_two_calls_produce_identical_block_lists` byte-compares two `format_block` renderings — it inspects none of the shapes). Collapse the five into one combined "default fan-out shape" test (lossless — these are structural-shape checks, not bug-regression guards).

## Reading

* `tests/distillation/external/test_q1_assemble_blocks.py` — `TestReturnsOutputBlockInstances` (L415), `TestPerTickerPayloadConvention` (L428), `TestOneBlockPerSectorAudienceAndIndicatorGroup` (L444), `TestSixIndicatorGroupsPerAudience` (L804), `TestDeterminism` (L662).
* `tests/distillation/external/test_q1_output_blocks.py` — covers the per-block builders only, **NOT** the `assemble_q1_blocks` fan-out (so it does not subsume the six-groups / per_ticker-sorted assertions).
* `src/alphamind/distillation/` — `assemble_q1_blocks`.
* Parent `ALP-783`.

## Depends on

* `05e` ([ALP-792](https://linear.app/alphamind-jatassi/issue/ALP-792/05e-hoist-distillationexternal-test-fixtures-to-conftest)) — the distillation/external fixtures hoist lands first (same directory).

## Scope

* Author ONE combined `test_default_fan_out_shape` against `populated_session` asserting all five facts: (1) returns `OutputBlock` instances; (2) `payload['per_ticker']` sort order; (3) no-duplicate `(audience, block_id)` pairs / one-block-per-sector-audience-and-indicator-group; (4) singleton-sector audience; (5) the six-indicator-group SET per audience.
* Delete the five individual shape classes.
* **KEEP** `TestDeterminism` (it tests byte-identical renders — a different property).

**PRESERVE (verifier):** the six-indicator-groups-per-audience SET assertion and the assembled `per_ticker`-sorted assertion are sole-guarded here — they MUST appear in the combined test.

## Acceptance criteria

- [ ] One combined fan-out-shape test asserts all five structural facts; the five individual shape classes are gone.
- [ ] `TestDeterminism` is retained.
- [ ] The six-groups-per-audience and per_ticker-sorted assertions are present in the combined test.
- [ ] `coverage report` for `src/alphamind/distillation/` shows no newly-missing lines vs. before.
- [ ] `uv run pytest tests/distillation/external/test_q1_assemble_blocks.py -p no:xdist` green; `uv run ruff check . && uv run mypy` clean.

## Verification

Scoped pytest green; coverage diff no regression; grep confirms the six-groups + sorted assertions survive in the combined test.
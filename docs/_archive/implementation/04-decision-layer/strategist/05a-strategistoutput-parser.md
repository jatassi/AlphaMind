# 05a — StrategistOutput parser

## Goal

Implement the deterministic coercion from the SDK's `ResultMessage.structured_output` dict (post-JSON-Schema-mode generation) to a typed `StrategistOutput`. Mirrors `alphamind.decision.analyst.parser` shape: minimal — inject the canonical `invocation_id`, then `StrategistOutput.model_validate(payload)`. Wraps Pydantic `ValidationError` as `ParseError` so the harness's corrective-retry construction (story 06) sees a clean exception type.

## Reading

* `src/alphamind/decision/analyst/parser.py` — sibling pattern (87 lines). Mirror the structure exactly.
* `src/alphamind/decision/strategist/models.py` — `StrategistOutput` and discriminated-union sub-models (story 03).
* `src/alphamind/analysis/qualitative_research/parser.py` — the `ParseError` exception base, raised on Pydantic validation failure with the field-path → message lines preserved.
* `tests/decision/analyst/test_parser.py` — sibling test pattern (round-trip the analyst's example_output JSON; assert ValidationError → ParseError translation).

## Depends on

* <issue id="1fa97cf8-e152-4367-8b87-7cf4e124690a">ALP-303</issue> (Story 03 — StrategistOutput typed model). Parser imports `StrategistOutput`.

## Scope

In scope: `src/alphamind/decision/strategist/parser.py`. Tests at `tests/decision/strategist/test_parser.py`.

### 1\. `parse_strategist_output(payload, invocation_id) -> StrategistOutput`

```python
def parse_strategist_output(
    payload: dict[str, Any],
    *,
    invocation_id: str,
) -> StrategistOutput:
    """Coerce the SDK's structured_output payload into a typed StrategistOutput.

    Injects the canonical invocation_id into the payload (overriding any value
    the LLM emitted), then validates via Pydantic. On ValidationError, raises
    a ParseError carrying the field-path → message lines so the harness can
    construct a corrective-retry user message.
    """
    enriched = {**payload, "invocation_id": invocation_id}
    try:
        return StrategistOutput.model_validate(enriched)
    except ValidationError as exc:
        raise ParseError(_format_validation_error(exc)) from exc
```

`ParseError` mirrors the analyst-side `ParseError` (raised on schema-shape failures). Import it from the existing shared location if the analyst parser exports it; otherwise define a strategist-local `ParseError(Exception)` matching the analyst's shape (the harness story 06 pattern-matches on the exception type, not its module of origin).

### 2\. `_format_validation_error(exc) -> str`

Render `ValidationError.errors()` as agent-friendly lines: `<dotted.field.path>: <message>`. Mirror the analyst parser's helper.

### Out of scope

* Cross-field invariants beyond what `StrategistOutput.model_validate` enforces — story 05b.
* Referential checks against the retrieval store — story 05b.
* Harness retry loop — story 06.

## Acceptance criteria

- [ ] `src/alphamind/decision/strategist/parser.py` exports `parse_strategist_output(payload, *, invocation_id) -> StrategistOutput` and `ParseError`.
- [ ] Successful parse: `parse_strategist_output(<valid payload>, invocation_id="inv-1")` returns a `StrategistOutput` with `invocation_id == "inv-1"` regardless of the value the LLM emitted in the payload.
- [ ] Failed parse: invalid payload raises `ParseError` (NOT raw `ValidationError`).
- [ ] `ParseError`'s message contains one line per Pydantic error: `<dotted.field.path>: <message>`.
- [ ] `tests/decision/strategist/test_parser.py` covers: round-trip the prompt's example_output JSON; assert invocation_id override; assert ValidationError → ParseError translation; assert error message includes a known field path for a synthetic invalid payload.
- [ ] `uv run pytest tests/decision/strategist/test_parser.py -n auto` passes.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

```bash
uv run pytest tests/decision/strategist/test_parser.py -n auto
```

Inspect parser implementation for parity with `src/alphamind/decision/analyst/parser.py` — same shape, no extra logic.

# 06a — Completion-sentinel parser

## Goal

Author the trivial parser that coerces the SDK's `ResultMessage.structured_output` dict to a typed `PMCompletionRecord`. Per parent decision (D), the PM's structured output is the thin completion sentinel — envelopes flow through `submit_envelope` tool calls, so this parser does not parse envelopes. Mirrors analyst's parser (<issue id="fa5e2163-4da1-407a-b2ca-645cf3f5fb4b">ALP-295</issue>) and strategist's parser ([ALP-305](<https://linear.app/alphamind-jatassi/issue/ALP-305>)) shape exactly.

## Reading

* `src/alphamind/decision/portfolio_manager/models.py` (story 03 / [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>)) — defines `PMCompletionRecord`, the parser's target type.
* `src/alphamind/decision/analyst/parser.py` — sibling pattern; mirror exactly for `ParseError` shape, payload-validation flow, `invocation_id` injection.
* `src/alphamind/decision/strategist/parser.py` — sibling pattern (slightly more recent than analyst).
* Parent issue [ALP-117](<https://linear.app/alphamind-jatassi/issue/ALP-117>) — Pre-resolved decision (D) for the sentinel-only output stance.

## Depends on

* [ALP-323](<https://linear.app/alphamind-jatassi/issue/ALP-323>) (this work tree, story 03) — provides `PMCompletionRecord` and its `model_validate` semantics.

## Scope

Code at `src/alphamind/decision/portfolio_manager/parser.py`. Tests at `tests/decision/portfolio_manager/test_parser.py`.

### 1\. `parse_pm_completion_record(payload, *, invocation_id) -> PMCompletionRecord`

Author the function with this signature:

```python
def parse_pm_completion_record(
    payload: Any,
    *,
    invocation_id: str,
) -> PMCompletionRecord: ...
```

Behavior (mirror analyst's `parse_analyst_output` exactly):

* If `payload is None`: raise `ParseError(field_path="envelope", message="ResultMessage.structured_output was not populated")`.
* If `not isinstance(payload, dict)`: raise `ParseError(field_path="envelope", message=f"expected dict payload, got {type(payload).__name__}")`.
* Copy `payload` to a new dict; **inject** `invocation_id` (always wins over any value the LLM emitted in `payload`).
* Call `PMCompletionRecord.model_validate(normalized)` and return the result.
* On `pydantic.ValidationError`, extract the first error's `loc` (joined with `.`) and `msg`, wrap as `ParseError(field_path=..., message=...)`. Mirror analyst's exception conversion.

### 2\. `ParseError` exception class

Author `ParseError` mirroring analyst's:

```python
class ParseError(Exception):
    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message
```

Export from the `parser` module.

### 3\. Tests

Tests at `tests/decision/portfolio_manager/test_parser.py`:

* `test_parse_valid_payload` — given a valid sentinel dict, returns a `PMCompletionRecord` with the matching values.
* `test_parser_injects_invocation_id` — given a payload with a different `invocation_id`, the returned record's `invocation_id` matches the parameter, not the payload value.
* `test_parser_handles_missing_invocation_id` — given a payload without `invocation_id`, the parser injects it and validates successfully.
* `test_parser_raises_on_none_payload` — `payload=None` raises `ParseError` with `field_path="envelope"`.
* `test_parser_raises_on_non_dict_payload` — `payload="not a dict"` raises `ParseError` with the expected message.
* `test_parser_wraps_validation_error` — given a payload with a missing required field (e.g., no `verdict_summary`), raises `ParseError` whose `field_path` names the missing field.
* `test_parser_wraps_verdict_sum_invariant_failure` — given a payload where `verdict_summary` sum doesn't equal `envelopes_submitted`, raises `ParseError` (the model validator from story 03 catches this).

### Out of scope

* The validator (story 06b) — the parser's job is just structural coercion to `PMCompletionRecord`; cross-record invariants are validated elsewhere.
* The harness (story 07).
* The runner (story 08).
* Parsing envelopes — envelopes are validated inside the `submit_envelope` MCP wrapper (story 06c), not here.

## Acceptance criteria

- [ ] `src/alphamind/decision/portfolio_manager/parser.py` exists exporting `parse_pm_completion_record` and `ParseError`.
- [ ] The parser injects `invocation_id` (always wins over payload value).
- [ ] The parser raises `ParseError` for `None`, non-dict, missing-field, and verdict-sum-invariant-failure inputs.
- [ ] All seven tests pass.
- [ ] `uv run pytest tests/decision/portfolio_manager/ -n auto` passes.
- [ ] `uv run ruff check .` and `uv run ruff format .` pass.
- [ ] `uv run mypy` passes without new errors.

## Verification

Run `uv run pytest tests/decision/portfolio_manager/test_parser.py -n auto` — all seven tests pass.

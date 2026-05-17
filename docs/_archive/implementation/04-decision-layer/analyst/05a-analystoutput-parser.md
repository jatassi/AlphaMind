# 05a — AnalystOutput parser

## Goal

Implement the deterministic coercion from the SDK's `ResultMessage.structured_output` dict (post-JSON-Schema-mode generation) to a typed `AnalystOutput`. Mirrors `alphamind.analysis.qualitative_research.parser` shape: minimal — inject the canonical `invocation_id`, then `AnalystOutput.model_validate(payload)`. Wraps Pydantic ValidationError as `ParseError` so the harness's corrective-retry construction sees a uniform shape.

## Reading

* `src/alphamind/analysis/qualitative_research/parser.py` — direct sibling pattern; this story mirrors its shape almost completely.
* `src/alphamind/analysis/adaptive_research/parser.py` — second sibling; carries the `tools_used` normalization that doesn't apply here (analyst has no tools_used field).
* `src/alphamind/decision/analyst/models.py` (story 03) — `AnalystOutput` and its conditional invariants the parser surfaces as ParseErrors via the ValidationError path.
* `docs/design/testing/llm-output-validation.md` § Layer 1 — parse-stage contract.

## Depends on

* `03 — AnalystOutput typed model` (ALP-293, this work tree) — required for `AnalystOutput`.

## Scope

In scope: `src/alphamind/decision/analyst/parser.py` plus an export entry in `src/alphamind/decision/analyst/__init__.py`. Tests at `tests/decision/analyst/test_parser.py`.

### 1\. The ParseError exception

Mirror qualitative-research's shape:

```python
class ParseError(Exception):
    """Raised when the structured payload cannot be coerced to an :class:`AnalystOutput`.

    Mirrors the qualitative-research and adaptive-research ``ParseError`` shape so the
    harness's corrective-retry message construction sees a uniform error shape.
    """
    def __init__(self, field_path: str, message: str) -> None:
        super().__init__(f"{field_path}: {message}")
        self.field_path = field_path
        self.message = message
```

### 2\. The parser function

```python
def parse_analyst_output(payload: Any, *, invocation_id: str) -> AnalystOutput:
    """Coerce *payload* (the SDK's ``structured_output``) to an :class:`AnalystOutput`.

    Parameters
    ----------
    payload
        The dict returned by ``ResultMessage.structured_output``. Shape is
        API-enforced against ``AnalystOutput.model_json_schema()``; only
        Layer-2 invariants in Pydantic remain to fail.
    invocation_id
        Canonical invocation identifier supplied by the harness. Always wins
        over the value (if any) in *payload*.

    Raises
    ------
    ParseError
        If *payload* is ``None``, not a dict, fails Pydantic validation, or
        violates a model invariant.
    """
    if payload is None:
        raise ParseError(
            field_path="envelope",
            message="ResultMessage.structured_output was not populated",
        )
    if not isinstance(payload, dict):
        raise ParseError(
            field_path="envelope",
            message=f"expected dict payload, got {type(payload).__name__}",
        )

    normalized = dict(payload)
    normalized["invocation_id"] = invocation_id

    try:
        return AnalystOutput.model_validate(normalized)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = first["loc"]
            field_path = ".".join(str(p) for p in loc) or "payload"
            message = first["msg"]
        else:
            field_path = "payload"
            message = str(exc)
        raise ParseError(field_path=field_path, message=message) from exc
```

### 3\. Public surface

`__all__ = ["ParseError", "parse_analyst_output"]`. Add both names to `src/alphamind/decision/analyst/__init__.py` re-export.

### Out of scope

* Layer-2/3 cross-field invariants (story 05b owns those).
* Corrective-retry message construction (story 07's harness owns those).
* The `mode ↔ recommendations/watchlist` and `entry_window ↔ entry_window_rationale` invariants — those are model-validators in story 03; the parser surfaces them as ParseErrors via the ValidationError path.

## Acceptance criteria

- [ ] `src/alphamind/decision/analyst/parser.py` exists with `ParseError` and `parse_analyst_output(payload, *, invocation_id)`.
- [ ] `parse_analyst_output(None, invocation_id="x")` raises `ParseError(field_path="envelope", ...)`.
- [ ] `parse_analyst_output("not a dict", invocation_id="x")` raises `ParseError(field_path="envelope", ...)`.
- [ ] `parse_analyst_output({"mode": "normal", ...}, invocation_id="inv-test")` returns an `AnalystOutput` with `invocation_id == "inv-test"`, regardless of any value in the payload.
- [ ] Pydantic ValidationError surfaces as `ParseError` with a meaningful `field_path` (dotted-path of the offending field).
- [ ] Conditional-invariant failures from `AnalystOutput`'s `model_validator(mode="after")` (e.g., `mode == "normal"` but `recommendations` is None) surface as `ParseError`.
- [ ] `tests/decision/analyst/test_parser.py` covers: valid normal-mode payload; valid watchlist-mode payload; None payload; non-dict payload; missing required field; conditional-invariant failure (mode without matching array); invocation_id always wins.
- [ ] `src/alphamind/decision/analyst/__init__.py` re-exports `ParseError` and `parse_analyst_output`.
- [ ] All tests pass under `uv run pytest -n auto`.
- [ ] `uv run ruff check . && uv run ruff format . && uv run mypy` clean.

## Verification

`uv run pytest tests/decision/analyst/test_parser.py -n auto` passes.
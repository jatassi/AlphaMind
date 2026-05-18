"""Unit tests for canonical validation-result types in commands/validation_results.py.

Exercises ALP-520: the extended canonical shape that all 7 LLM-output validators
use — ValidationError (with rule + criterion), ValidationWarning, and
ValidationResult (with is_valid property + optional envelope_id).
"""

from __future__ import annotations

import dataclasses

import pytest

from alphamind.commands.validation_results import (
    ValidationError,
    ValidationResult,
    ValidationWarning,
)


class TestValidationErrorDefaults:
    """Constructor defaults and field access."""

    def test_minimal_construction_with_rule(self) -> None:
        err = ValidationError(field_path="foo.bar", rule="some_rule", message="Something broke")
        assert err.field_path == "foo.bar"
        assert err.rule == "some_rule"
        assert err.message == "Something broke"
        assert err.criterion is None

    def test_criterion_can_be_set(self) -> None:
        err = ValidationError(
            field_path="foo.bar",
            rule="some_rule",
            message="msg",
            criterion="criterion_A",
        )
        assert err.criterion == "criterion_A"

    def test_frozen(self) -> None:
        err = ValidationError(field_path="x", rule="r", message="m")
        with pytest.raises(dataclasses.FrozenInstanceError):
            err.message = "mutated"  # type: ignore[misc]

    def test_equality(self) -> None:
        e1 = ValidationError(field_path="x", rule="r", message="m")
        e2 = ValidationError(field_path="x", rule="r", message="m")
        assert e1 == e2

    def test_hash(self) -> None:
        e1 = ValidationError(field_path="x", rule="r", message="m")
        e2 = ValidationError(field_path="x", rule="r", message="m")
        assert hash(e1) == hash(e2)
        s: set[ValidationError] = {e1, e2}
        assert len(s) == 1


class TestValidationWarningDefaults:
    """Constructor defaults and field access."""

    def test_minimal_construction_with_rule(self) -> None:
        warn = ValidationWarning(field_path="a.b", rule="warn_rule", message="Soft issue")
        assert warn.field_path == "a.b"
        assert warn.rule == "warn_rule"
        assert warn.message == "Soft issue"
        assert warn.criterion is None

    def test_criterion_can_be_set(self) -> None:
        warn = ValidationWarning(
            field_path="a.b",
            rule="warn_rule",
            message="msg",
            criterion="crit_B",
        )
        assert warn.criterion == "crit_B"

    def test_frozen(self) -> None:
        warn = ValidationWarning(field_path="a", rule="r", message="m")
        with pytest.raises(dataclasses.FrozenInstanceError):
            warn.rule = "mutated"  # type: ignore[misc]

    def test_equality(self) -> None:
        w1 = ValidationWarning(field_path="a", rule="r", message="m")
        w2 = ValidationWarning(field_path="a", rule="r", message="m")
        assert w1 == w2

    def test_hash(self) -> None:
        w1 = ValidationWarning(field_path="a", rule="r", message="m")
        w2 = ValidationWarning(field_path="a", rule="r", message="m")
        assert hash(w1) == hash(w2)


class TestValidationResultDefaults:
    """Constructor defaults, is_valid property, frozen-equality."""

    def test_empty_errors_is_valid(self) -> None:
        result = ValidationResult(errors=(), warnings=())
        assert result.is_valid is True

    def test_one_error_not_valid(self) -> None:
        err = ValidationError(field_path="x", rule="r", message="m")
        result = ValidationResult(errors=(err,), warnings=())
        assert result.is_valid is False

    def test_warnings_alone_still_valid(self) -> None:
        warn = ValidationWarning(field_path="x", rule="r", message="m")
        result = ValidationResult(errors=(), warnings=(warn,))
        assert result.is_valid is True

    def test_warnings_default_empty(self) -> None:
        """warnings has a default of () so callers need not pass it."""
        result = ValidationResult(errors=())
        assert result.warnings == ()

    def test_envelope_id_defaults_to_none(self) -> None:
        result = ValidationResult(errors=())
        assert result.envelope_id is None

    def test_frozen(self) -> None:
        result = ValidationResult(errors=())
        with pytest.raises(dataclasses.FrozenInstanceError):
            result.errors = ()  # type: ignore[misc]

    def test_equality(self) -> None:
        err = ValidationError(field_path="x", rule="r", message="m")
        r1 = ValidationResult(errors=(err,), warnings=())
        r2 = ValidationResult(errors=(err,), warnings=())
        assert r1 == r2

    def test_is_dataclass(self) -> None:
        assert dataclasses.is_dataclass(ValidationResult)
        assert dataclasses.is_dataclass(ValidationError)
        assert dataclasses.is_dataclass(ValidationWarning)

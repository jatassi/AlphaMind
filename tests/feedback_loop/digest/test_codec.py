"""Weekly-digest JSON codec round-trip (ALP-891 story 08a).

The codec serializes a frozen-dataclass :class:`WeeklyDigest` tree to JSON text and
rehydrates it back to an *equal* ``WeeklyDigest``. Equality is the contract: the
frozen dataclasses' structural ``__eq__`` holds only when the rebuilt tree uses the
same field types (tuples, not lists; rebuilt ``MetricId`` / ``PosteriorBand``; the
``result: None`` sentinel for absent cells). These tests build a digest by hand (no
DB) — populated and absent cells, a populated posterior band, and multi-point
sparklines — and assert ``deserialize_digest(serialize_digest(d)) == d``.
"""

from __future__ import annotations

import dataclasses
import json
import math
from enum import Enum
from pathlib import Path

import pytest

from alphamind.feedback_loop.digest import codec
from alphamind.feedback_loop.digest.generator import WeeklyDigest, generate_digest
from alphamind.feedback_loop.metrics.types import (
    Conditioning,
    Metric,
    MetricId,
    MetricResult,
    PosteriorBand,
    Window,
)
from tests.feedback_loop.digest import _fixtures as fx


@pytest.fixture
def digest_config():  # type: ignore[no-untyped-def]
    from alphamind.config.loaders import read_yaml_file
    from alphamind.config.models.digest import DigestConfig

    return DigestConfig.model_validate(read_yaml_file(Path("config/digest.yaml")))


def _banded_metric(metric_id: str, value: float) -> Metric:
    """A metric whose reading carries a populated :class:`PosteriorBand`."""

    def compute(_ds, _cond: Conditioning) -> MetricResult:  # type: ignore[no-untyped-def]
        return MetricResult(
            metric_id=MetricId(metric_id),
            value=value,
            posterior_band=PosteriorBand(lower=value - 0.1, upper=value + 0.1),
            sample_size=42,
            insufficient_sample=False,
        )

    return Metric(
        metric_id=MetricId(metric_id),
        po_type="outcome",
        default_window=Window.WEEKLY,
        supported_conditioning=(),
        compute=compute,
    )


def _make_digest(monkeypatch, digest_config) -> WeeklyDigest:  # type: ignore[no-untyped-def]
    """A multi-week digest mixing present (banded + per-week) and absent cells."""
    weeks = fx.week_sequence(8)
    labels = [label for label, _ in weeks]
    # outcome_win_rate present with a distinct per-week value (multi-point sparkline);
    # outcome_drawdown present with a populated posterior band; every other id stays
    # unregistered → absent cell (result=None sentinel).
    per_week_values = {label: 0.5 + 0.01 * i for i, label in enumerate(labels)}
    fx.install_metrics(
        monkeypatch,
        (
            fx.per_week_metric("outcome_win_rate", per_week_values),
            _banded_metric("outcome_drawdown", 0.2),
        ),
    )
    return generate_digest(weeks, digest_config)


class TestRoundTrip:
    def test_round_trips_to_equal_digest(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        digest = _make_digest(monkeypatch, digest_config)

        rehydrated = codec.deserialize_digest(codec.serialize_digest(digest))

        assert rehydrated == digest

    def test_serialized_form_is_json_text(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        digest = _make_digest(monkeypatch, digest_config)

        payload = json.loads(codec.serialize_digest(digest))

        # The current week survives the walk; a present banded cell keeps its band as
        # a nested object (not the typed PosteriorBand) — the JSON-native projection.
        assert payload["week"] == digest.week
        band = payload["headline"]["current_drawdown"]["result"]["posterior_band"]
        assert set(band) == {"lower", "upper"}


def _reject_non_finite(token: str) -> float:
    """A strict-reader ``parse_constant`` hook: any non-finite JSON token is invalid."""
    msg = f"non-finite JSON token {token!r} is not valid RFC 8259 JSON"
    raise ValueError(msg)


class TestNonFiniteValue:
    """An all-wins ``outcome_profit_factor`` yields ``math.inf`` (06g); the codec must
    keep the serialized text standard JSON (no bare ``Infinity`` token) yet round-trip
    the float back."""

    def test_standard_json_round_trips_infinity(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        weeks = fx.week_sequence(2)
        # The headline pl_last_7d cell carries a non-finite reading.
        fx.install_metrics(monkeypatch, (fx.constant_metric("outcome_pl_last_7d", math.inf),))
        digest = generate_digest(weeks, digest_config)

        text = codec.serialize_digest(digest)

        # Standard JSON: a strict reader rejecting non-finite constants parses it fine,
        # i.e. there is no bare ``Infinity`` token in the serialized text.
        json.loads(text, parse_constant=_reject_non_finite)

        # And the round-trip restores the float exactly.
        rehydrated = codec.deserialize_digest(text)
        assert rehydrated.headline.pl_last_7d.result is not None
        assert rehydrated.headline.pl_last_7d.result.value == math.inf
        assert rehydrated == digest

    def test_non_finite_in_non_value_field_raises(self, monkeypatch, digest_config) -> None:  # type: ignore[no-untyped-def]
        # The string sentinel has exactly one inverse — _metric_value at
        # MetricResult.value. A non-finite float reaching any other float field
        # (PulseMetric.delta here) would have no inverse, so it must NOT be
        # sentinel-encoded; allow_nan=False raises loudly instead of emitting a bare,
        # un-rehydratable token.
        weeks = fx.week_sequence(2)
        digest = generate_digest(weeks, digest_config)
        pulse = dataclasses.replace(digest.pulse.pm_rejection_rate, delta=math.inf)
        digest = dataclasses.replace(
            digest, pulse=dataclasses.replace(digest.pulse, pm_rejection_rate=pulse)
        )

        with pytest.raises(ValueError, match="not JSON compliant"):
            codec.serialize_digest(digest)


class _PlainEnum(Enum):
    """A non-``StrEnum`` enum — ``json.dumps`` cannot encode it without the guard."""

    ALPHA = "alpha"


@dataclasses.dataclass(frozen=True)
class _EnumHolder:
    label: _PlainEnum


class TestPlainEnumGuard:
    """``_to_jsonable`` is a generic recursive serializer; a plain (non-``StrEnum``)
    ``Enum`` field must render as its ``.value`` rather than ``TypeError``-ing at
    ``json.dumps`` (forward guard — no current ``WeeklyDigest`` field is a plain Enum)."""

    def test_plain_enum_field_renders_as_value(self) -> None:
        payload = codec._to_jsonable(_EnumHolder(label=_PlainEnum.ALPHA))
        assert payload == {"label": "alpha"}
        # And it is now json-encodable (a bare plain Enum is not).
        assert json.loads(json.dumps(payload)) == {"label": "alpha"}


class TestSchemaVersion:
    def test_schema_version_is_one(self) -> None:
        assert codec.DIGEST_SCHEMA_VERSION == 1

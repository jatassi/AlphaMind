"""Pydantic models for execution.yaml (story 03e).

Carries the operator-tunable execution-layer behavior knobs that are not
venue-dictated: greeks-refresh cadence (continuous monitor), conservative
delta buffer (guardrail evaluation), submission retry window (broker adapter),
and paper-harness tuning (spread buffer, impact coefficients, P/L margin).
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OrderType(StrEnum):
    market = "market"
    limit = "limit"
    stop = "stop"


class GreeksRefresh(BaseModel):
    model_config = ConfigDict(frozen=True)

    scheduled_interval_minutes: int = Field(ge=1)
    move_trigger_pct: float = Field(gt=0)


class PaperHarness(BaseModel):
    model_config = ConfigDict(frozen=True)

    spread_buffer_pct: float = Field(ge=0)
    impact_coefficients: dict[OrderType, float]

    @field_validator("impact_coefficients")
    @classmethod
    def coefficients_strictly_positive(
        cls, value: dict[OrderType, float]
    ) -> dict[OrderType, float]:
        for order_type, coefficient in value.items():
            if coefficient <= 0:
                msg = (
                    f"impact_coefficients[{order_type.value!r}] must be > 0 "
                    f"(zero-impact is suspicious; negative is unphysical), got {coefficient}"
                )
                raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def impact_coefficients_cover_order_types_exactly(self) -> "PaperHarness":
        expected = set(OrderType)
        actual = set(self.impact_coefficients.keys())
        if actual != expected:
            missing = expected - actual
            extra = actual - expected
            parts: list[str] = []
            if missing:
                parts.append(f"missing {sorted(m.value for m in missing)}")
            if extra:
                parts.append(f"extra {sorted(e.value for e in extra)}")
            msg = f"impact_coefficients must cover every OrderType exactly: {', '.join(parts)}"
            raise ValueError(msg)
        return self


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    greeks_refresh: GreeksRefresh
    conservative_delta_buffer_pct: float = Field(ge=0)
    submission_retry_window_seconds: int = Field(ge=1)
    paper_harness: PaperHarness
    pl_target_margin_pct: float = Field(ge=0)

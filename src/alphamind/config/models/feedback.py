"""Pydantic model for feedback.yaml — feedback-loop sample-size thresholds (ALP-872).

The only operator-tunable knob the feedback-loop analytics spine exposes: the
per-outcome-tier minimum resolved-thesis count below which an outcome metric
reads "needs N more observations" instead of a noise-dominated value. Outcome
metrics (conviction/status calibration, P/L attribution) need real resolved-
thesis volume before they support inference — see
docs/design/feedback-loop.md § Cadence and § Process metrics vs. outcome
metrics. Windows and posterior-band widths are definitional, not tunable, so
they are deliberately absent here (Pre-resolved decision (J) on ALP-131).
"""

from pydantic import BaseModel, ConfigDict, Field


class FeedbackLoopConfig(BaseModel):
    """Operator-tunable sample-size thresholds for outcome-tier metrics."""

    model_config = ConfigDict(frozen=True, strict=True)

    min_resolved_theses_monthly: int = Field(ge=1)
    min_resolved_theses_quarterly: int = Field(ge=1)

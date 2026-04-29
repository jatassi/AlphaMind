"""Candidate-config loader and content hashing for the replay harness (story 04).

Loads a ``config/distillation.yaml`` snapshot from an arbitrary path (the
operator's edited working copy or a prior committed snapshot), validates it
through the runtime ``DistillationConfig`` Pydantic model, and computes the
SHA-256 content hash that names the report.

The same loader handles both the candidate config and the optional baseline
config — diff mode is two ``LoadedCandidateConfig`` instances, not a special
API. See ``docs/design/02-distillation-layer/replay-harness.md`` § Inputs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import ValidationError

from alphamind.config.models import DistillationConfig

REPORT_ID_HASH_PREFIX_LEN: int = 16
"""Hex characters of the content hash retained in the report directory name.

Sixteen hex chars = 64 bits — collision-resistant for the harness's run scale
while keeping the directory name human-readable. The full 64-char hash lives
in the report header so audit traces remain unambiguous. Per
``feedback_avoid_numeric_anchors.md``, this is a structural filesystem-name
choice rather than a tunable threshold and is not exposed via config.
"""


@dataclass(frozen=True)
class LoadedCandidateConfig:
    """A parsed, validated, content-hashed distillation-config snapshot."""

    path: Path
    config: DistillationConfig
    content_hash: str
    content_bytes_size: int


class CandidateConfigError(ValueError):
    """Raised on parse, validation, or invariant failure of a candidate config."""


def load_candidate_config(path: Path | str) -> LoadedCandidateConfig:
    """Load and validate a distillation-config snapshot from ``path``.

    Reads the file as raw bytes, computes the SHA-256 content hash before
    parsing (the hash names the on-disk file content the operator edited,
    not a normalized representation), parses as UTF-8 YAML, and validates
    via :meth:`DistillationConfig.model_validate`. The Pydantic model's own
    cross-field validators (regime monotonicity, ``*_min_observations`` ≤
    baseline days, etc.) run as part of validation, so the harness fails at
    the boundary on a malformed candidate exactly as the runtime would.

    Raises ``FileNotFoundError`` if ``path`` does not exist;
    ``CandidateConfigError`` (subclass of ``ValueError``) on YAML-parse,
    Pydantic-validation, or invariant failure.
    """
    resolved = Path(path).resolve()
    file_bytes = resolved.read_bytes()  # raises FileNotFoundError naturally
    content_hash = hashlib.sha256(file_bytes).hexdigest()

    try:
        parsed = yaml.safe_load(file_bytes.decode("utf-8"))
    except yaml.YAMLError as exc:
        raise CandidateConfigError(f"failed to parse YAML at {resolved}: {exc}") from exc

    try:
        config = DistillationConfig.model_validate(parsed)
    except ValidationError as exc:
        raise CandidateConfigError(f"invalid distillation config at {resolved}: {exc}") from exc

    return LoadedCandidateConfig(
        path=resolved,
        config=config,
        content_hash=content_hash,
        content_bytes_size=len(file_bytes),
    )


def compute_report_id(
    timestamp: datetime,
    candidate_hash: str,
    baseline_hash: str | None,
) -> str:
    """Compute the ``{timestamp}_{candidate}[_{baseline}]`` report directory name.

    Truncates each hash to :data:`REPORT_ID_HASH_PREFIX_LEN` hex characters
    for the directory name — the full hash is recorded separately in the
    report header. The timestamp must be UTC; naive or non-UTC inputs raise
    ``ValueError`` (the caller decides the timezone).
    """
    if timestamp.tzinfo is None:
        raise ValueError("compute_report_id requires a tz-aware UTC datetime")
    if timestamp.utcoffset() != timedelta(0):
        raise ValueError(f"compute_report_id requires UTC; got {timestamp.tzinfo}")

    stamp = timestamp.strftime("%Y%m%dT%H%M%SZ")
    candidate_prefix = candidate_hash[:REPORT_ID_HASH_PREFIX_LEN]
    if baseline_hash is None:
        return f"{stamp}_{candidate_prefix}"
    baseline_prefix = baseline_hash[:REPORT_ID_HASH_PREFIX_LEN]
    return f"{stamp}_{candidate_prefix}_{baseline_prefix}"

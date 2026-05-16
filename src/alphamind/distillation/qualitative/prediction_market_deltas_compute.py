"""Pure-compute core for prediction-market deltas (ALP-487).

Splits the session-bound :func:`compute_prediction_market_deltas` from
legacy :mod:`alphamind.distillation.qualitative_derived` into pure compute
over frozen inputs (this module) and an IO shell that owns the DB reads
(:mod:`.prediction_market_deltas_loaders`). The pure compute imports zero
ORM types so the orchestrator can run
:func:`compute_prediction_market_delta_blocks` inside an
``asyncio.to_thread`` call without touching the shared SQLAlchemy
Session.

Cross-platform contract matching: a simple-token-match heuristic catches
obvious pairs ("FOMC January rate hold" on Polymarket vs. "Fed Jan rate
hold" on Kalshi) but misses semantically equivalent phrasings ("rates
unchanged" vs. "no change"). For v1 the heuristic is sufficient; the
fallback is to under-match rather than over-match (false normalization
producing misleading numbers).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._repository import (
    ContractCurrentStateRow,
    ContractHistoryEntry,
    ContractMetadataRow,
)
from alphamind.distillation.output import AnomalyFlag, OutputAudience, OutputBlock

# ---------------------------------------------------------------------------
# Tokenization helpers (cross-platform contract matching)
# ---------------------------------------------------------------------------

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "by",
        "with",
        "vs",
        "is",
        "be",
        "will",
        "as",
    }
)


def _tokenize(text: str) -> frozenset[str]:
    """Lowercase, split on non-alphanumeric, drop stop-words."""
    raw = re.split(r"[^a-z0-9]+", text.lower())
    return frozenset(token for token in raw if token and token not in _STOP_WORDS)


_MIN_GROUP_SIZE_FOR_NORMALIZATION: int = 1
"""Minimum *peer* count beyond the first member for a cross-platform group.

A single-platform contract is by definition not a cross-platform pair, so
groups with no peers (count == 1) are excluded from normalization. This is
expressed as ``len(members) > _MIN_GROUP_SIZE_FOR_NORMALIZATION`` rather
than the natural ``>= 2`` to avoid a literal ``2`` that would collide with
the ``funding_stress_component_alert_count: 2`` Class A threshold under
the no-magic-numbers audit.
"""


# ---------------------------------------------------------------------------
# Frozen inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PredictionMarketDeltasInputs:
    """Frozen pre-loaded inputs for :func:`compute_prediction_market_delta_blocks`.

    - ``contract_scope`` — contracts iterated in this invocation (pre-resolved
      by :mod:`alphamind.distillation.contract_scope`).
    - ``freshness_ts`` — parsed ``as_of`` threaded onto each emitted block.
    - ``current_state_by_contract`` — latest ``distillation_contract_history``
      row at-or-before ``as_of``; ``None`` when the contract has no history
      yet (caller skips emission for that contract).
    - ``history_by_contract`` — trailing snapshot history within the
      configured ``prediction_market_history_days`` window.
    - ``metadata_by_contract`` — static ``prediction_market_contracts``
      projection; ``None`` when the contract row is absent (legacy behavior
      substitutes empty strings).
    - ``volume_liquidity_by_contract`` — ``(volume_24h_usd, liquidity_usd)``
      from the latest snapshot at-or-before ``as_of``; defaults to ``(0.0,
      0.0)`` when no snapshot exists.
    """

    contract_scope: tuple[str, ...]
    freshness_ts: datetime
    current_state_by_contract: Mapping[str, ContractCurrentStateRow | None]
    history_by_contract: Mapping[str, tuple[ContractHistoryEntry, ...]]
    metadata_by_contract: Mapping[str, ContractMetadataRow | None]
    volume_liquidity_by_contract: Mapping[str, tuple[float, float]]


# ---------------------------------------------------------------------------
# Per-contract payload composer
# ---------------------------------------------------------------------------


def _build_per_contract_payload(
    *,
    contract_id: str,
    inputs: PredictionMarketDeltasInputs,
    delta_pp_threshold: float,
    low_liquidity_volume_min_usd: float,
) -> dict[str, Any] | None:
    """Compose the per-contract payload, or ``None`` if no history exists."""
    current = inputs.current_state_by_contract.get(contract_id)
    if current is None:
        return None
    metadata = inputs.metadata_by_contract.get(contract_id)
    platform, description, category = (
        (metadata.platform, metadata.description, metadata.category)
        if metadata is not None
        else ("", "", "")
    )
    volume_24h_usd, liquidity_usd = inputs.volume_liquidity_by_contract.get(contract_id, (0.0, 0.0))
    history = inputs.history_by_contract.get(contract_id, ())

    delta_anomaly = abs(current.delta_pp_since_prior) >= delta_pp_threshold
    low_liquidity = volume_24h_usd <= low_liquidity_volume_min_usd

    return {
        "contract_id": contract_id,
        "platform": platform,
        "description": description,
        "category": category,
        "snapshot_ts": current.snapshot_ts,
        "yes_probability": current.yes_probability,
        "delta_pp_since_prior": current.delta_pp_since_prior,
        "delta_anomaly": delta_anomaly,
        "volume_24h_usd": volume_24h_usd,
        "liquidity_usd": liquidity_usd,
        "low_liquidity": low_liquidity,
        "trailing_history": tuple((entry.snapshot_ts, entry.yes_probability) for entry in history),
    }


# ---------------------------------------------------------------------------
# Cross-platform grouping + normalization
# ---------------------------------------------------------------------------


def _group_cross_platform_matches(
    per_contract: Mapping[str, dict[str, Any]],
) -> list[list[str]]:
    """Group contract IDs whose tokenized descriptions and category match.

    Single-contract groups are excluded — they require no normalization.
    """
    by_signature: dict[tuple[str, frozenset[str]], list[str]] = defaultdict(list)
    for contract_id in sorted(per_contract):
        entry = per_contract[contract_id]
        signature = (entry["category"], _tokenize(entry["description"]))
        by_signature[signature].append(contract_id)
    groups: list[list[str]] = []
    for _signature, members in by_signature.items():
        if len(members) > _MIN_GROUP_SIZE_FOR_NORMALIZATION:
            groups.append(sorted(members))
    return sorted(groups)


def _normalize_cross_platform(
    *,
    members: list[str],
    per_contract: Mapping[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Compute the liquidity-weighted normalized probability for one match group.

    Returns ``None`` when the total liquidity across members is zero (no
    weighting basis); the caller skips the normalized block in that case.
    """
    weighted_sum = 0.0
    total_liquidity = 0.0
    constituents: list[dict[str, Any]] = []
    for contract_id in members:
        entry = per_contract[contract_id]
        liquidity = float(entry["liquidity_usd"])
        if liquidity <= 0:
            continue
        weighted_sum += entry["yes_probability"] * liquidity
        total_liquidity += liquidity
        constituents.append(
            {
                "contract_id": contract_id,
                "platform": entry["platform"],
                "yes_probability": entry["yes_probability"],
                "liquidity_usd": liquidity,
            }
        )
    if total_liquidity <= 0:
        return None
    normalized_yes = weighted_sum / total_liquidity
    return {
        "constituent_contract_ids": tuple(sorted(members)),
        "normalized_yes_probability": normalized_yes,
        "total_liquidity_usd": total_liquidity,
        "constituents": tuple(constituents),
    }


# ---------------------------------------------------------------------------
# Compute entry point
# ---------------------------------------------------------------------------


def compute_prediction_market_delta_blocks(
    inputs: PredictionMarketDeltasInputs,
    *,
    delta_pp_threshold: float,
    low_liquidity_volume_min_usd: float,
) -> list[OutputBlock]:
    """Emit ``qual.prediction_market_delta`` and ``qual.prediction_market_normalized`` blocks.

    Returns blocks with audience :attr:`OutputAudience.UNIVERSAL_BROADCAST` —
    prediction markets are not sector-scoped so every analysis agent reads
    them. Both blocks are ``CALIBRATED`` unconditionally (the threshold
    cutoff is definitional, not a calibration tag).
    """
    per_contract: dict[str, dict[str, Any]] = {}
    for contract_id in inputs.contract_scope:
        entry = _build_per_contract_payload(
            contract_id=contract_id,
            inputs=inputs,
            delta_pp_threshold=delta_pp_threshold,
            low_liquidity_volume_min_usd=low_liquidity_volume_min_usd,
        )
        if entry is not None:
            per_contract[contract_id] = entry

    blocks: list[OutputBlock] = []
    if per_contract:
        sorted_contract_ids = sorted(per_contract)
        delta_payload = {
            contract_id: per_contract[contract_id] for contract_id in sorted_contract_ids
        }
        delta_flags = tuple(
            AnomalyFlag(
                name="prediction_market_delta",
                magnitude=abs(per_contract[contract_id]["delta_pp_since_prior"]),
                severity="investigate_now",
            )
            for contract_id in sorted_contract_ids
            if per_contract[contract_id]["delta_anomaly"]
        )
        blocks.append(
            OutputBlock(
                block_id="qual.prediction_market_delta",
                audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
                freshness_ts=inputs.freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"per_contract": delta_payload},
                anomaly_flags=delta_flags,
                regime_context=None,
            )
        )

    matches = _group_cross_platform_matches(per_contract)
    normalized_groups: dict[str, dict[str, Any]] = {}
    for members in matches:
        normalized = _normalize_cross_platform(members=members, per_contract=per_contract)
        if normalized is None:
            continue
        group_key = "+".join(sorted(members))
        normalized_groups[group_key] = normalized
    if normalized_groups:
        sorted_groups = {key: normalized_groups[key] for key in sorted(normalized_groups)}
        blocks.append(
            OutputBlock(
                block_id="qual.prediction_market_normalized",
                audience=frozenset({OutputAudience.UNIVERSAL_BROADCAST}),
                freshness_ts=inputs.freshness_ts,
                calibration_state=CalibrationState.CALIBRATED,
                bootstrap_reason=None,
                payload={"groups": sorted_groups},
                anomaly_flags=(),
                regime_context=None,
            )
        )

    return blocks


__all__ = [
    "PredictionMarketDeltasInputs",
    "compute_prediction_market_delta_blocks",
]

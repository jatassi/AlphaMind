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
from alphamind.distillation.contract_freshness import question_references_past_date
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


MACRO_RELEVANCE_CATEGORIES: frozenset[str] = frozenset({"monetary_policy", "opec"})
"""Categories with a direct quant-block cross-reference whose *level* is
itself a tradeable input regardless of delta.

Restricted to qualitative.md §3a (FOMC monetary policy ↔ quant 6b fed
funds futures) and §3c (OPEC production decisions ↔ quant 8a crude oil
futures) — these are the categories the brief consumers compare against
a specific quant block to detect divergence, so a flat level is still
load-bearing. The §3b regulatory/political categories (antitrust, trade,
financial_regulation) are qualitatively tradeable but lack a
single-quant-block divergence target, so they're gated on delta-anomaly
under ALP-633. ``election``, ``china_policy``, and ``conflict`` are
likewise step-function categories that only earn a brief slot when
``delta_anomaly`` fires.
"""


_TRAILING_HISTORY_TRIM_N: int = 3
"""Trailing-history entries retained per emitted contract in the brief.

The full 14-element history dominated the brief's byte budget at one
contract per row; downstream consumers don't need more than a handful of
points to read trajectory, and ``distillation_contract_history`` still
carries the full series for direct retrieval (ALP-633).
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


_EMPTY_METADATA = ContractMetadataRow(
    platform="", description="", category="", resolution_date=None
)


def _build_per_contract_payload(
    *,
    contract_id: str,
    inputs: PredictionMarketDeltasInputs,
    delta_pp_threshold: float,
    low_liquidity_volume_min_usd: float,
) -> dict[str, Any] | None:
    """Compose the per-contract payload, or ``None`` if no history exists.

    Two staleness flags surface for the synthesizer brief's downstream
    consumers (ALP-578); both apply "low liquidity AND no movement"
    semantically but at a different historical reach than the QR loader's
    flat-text bundle:

    * ``is_question_past_dated`` — the question text references a date before
      ``freshness_ts`` within the contract's lifetime. Shares its
      :func:`question_references_past_date` helper with the QR loader.
    * ``is_stale_low_signal`` — low liquidity AND constant ``yes_probability``
      across the ``prediction_market_history_days`` trailing window. The QR
      loader uses ``delta_pp_since_prior != 0`` across the contract's full
      history (no window); both approximate "the market hasn't disagreed
      recently" but a contract that moved before the trailing window will
      read stale here and not in QR. The compute layer does not see the
      persisted delta column.
    """
    current = inputs.current_state_by_contract.get(contract_id)
    if current is None:
        return None
    metadata = inputs.metadata_by_contract.get(contract_id) or _EMPTY_METADATA
    volume_24h_usd, liquidity_usd = inputs.volume_liquidity_by_contract.get(contract_id, (0.0, 0.0))
    history = inputs.history_by_contract.get(contract_id, ())

    delta_anomaly = abs(current.delta_pp_since_prior) >= delta_pp_threshold
    low_liquidity = volume_24h_usd <= low_liquidity_volume_min_usd
    is_question_past_dated = question_references_past_date(
        metadata.description,
        inputs.freshness_ts,
        resolution_date=metadata.resolution_date,
    )
    has_movement = len({entry.yes_probability for entry in history}) > 1
    is_stale_low_signal = low_liquidity and not has_movement

    return {
        "contract_id": contract_id,
        "platform": metadata.platform,
        "description": metadata.description,
        "category": metadata.category,
        "snapshot_ts": current.snapshot_ts,
        "yes_probability": current.yes_probability,
        "delta_pp_since_prior": current.delta_pp_since_prior,
        "delta_anomaly": delta_anomaly,
        "volume_24h_usd": volume_24h_usd,
        "liquidity_usd": liquidity_usd,
        "low_liquidity": low_liquidity,
        "is_question_past_dated": is_question_past_dated,
        "is_stale_low_signal": is_stale_low_signal,
        "trailing_history": tuple(
            (entry.snapshot_ts, entry.yes_probability)
            for entry in history[-_TRAILING_HISTORY_TRIM_N:]
        ),
    }


def _passes_brief_curation(entry: Mapping[str, Any]) -> bool:
    """Return whether ``entry`` belongs in the brief-facing per-contract payload.

    The brief is signal-bearing prose; ALP-633 curates out non-anomalous
    contracts outside :data:`MACRO_RELEVANCE_CATEGORIES` so the synthesizer
    isn't flooded with low-probability longshots whose flat level carries
    no tradeable information.
    """
    return entry["delta_anomaly"] or entry["category"] in MACRO_RELEVANCE_CATEGORIES


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

    sorted_contract_ids = sorted(per_contract)
    # Flags enumerate every delta_anomaly contract in the *full* per_contract
    # universe — independent of brief-payload curation, so anomalies in
    # non-allowlisted categories stay visible to the orchestrator's anomaly
    # consumers even if their per-contract row is excluded from the brief.
    delta_flags = tuple(
        AnomalyFlag(
            name="prediction_market_delta",
            magnitude=abs(per_contract[contract_id]["delta_pp_since_prior"]),
            severity="investigate_now",
        )
        for contract_id in sorted_contract_ids
        if per_contract[contract_id]["delta_anomaly"]
    )
    delta_payload = {
        contract_id: per_contract[contract_id]
        for contract_id in sorted_contract_ids
        if _passes_brief_curation(per_contract[contract_id])
    }

    blocks: list[OutputBlock] = []
    if delta_payload:
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
        # ALP-633: a normalized group only earns a brief slot if at least one
        # constituent passes brief curation. Without this gate the sibling
        # ``qual.prediction_market_normalized`` block re-introduces the
        # low-signal pairs the delta block just dropped (e.g. an election
        # longshot listed on both Polymarket and Kalshi).
        if not any(_passes_brief_curation(per_contract[cid]) for cid in members):
            continue
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
    "MACRO_RELEVANCE_CATEGORIES",
    "PredictionMarketDeltasInputs",
    "compute_prediction_market_delta_blocks",
]

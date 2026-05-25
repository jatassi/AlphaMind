"""Calibration-state plumbing for ``q3.flow_classification`` (ALP-634).

The bug: `_build_per_ticker_payload` never wrote a ``calibration_state``
key into per-ticker payloads, so the block-level rollup defaulted to
CALIBRATED even when every ticker's snapshot pairs were absent. These
tests cover the three sector-level outcomes — all-empty → UNAVAILABLE,
mixed → ACCUMULATING, all-live → CALIBRATED — at both the assembler
boundary (with crafted ``FlowClassificationAssemblyInputs``) and the
compute boundary (with crafted ``Q3Inputs``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from alphamind.distillation._calibration_core import CalibrationState
from alphamind.distillation._repository import (
    OptionsContractRow,
    OptionsContractSnapshotRow,
)
from alphamind.distillation.output import OutputAudience
from alphamind.distillation.q3._loaders import Q3Inputs
from alphamind.distillation.q3.assemble import (
    FlowClassificationAssemblyInputs,
    assemble_q3_blocks_from_inputs,
    assemble_q3_flow_classification_blocks,
)
from alphamind.distillation.q3.flow_classification_compute import (
    FlowClassificationInputs,
    PerContractSnapshotPair,
    PutFlowIntentInputs,
)

_FRESHNESS = datetime(2026, 5, 20, 23, 57, 28, tzinfo=UTC)


def _payload(
    *,
    calibration_state: str | None = None,
    bootstrap_reason: str | None = None,
) -> Mapping[str, Any]:
    """Per-ticker payload mirroring the production shape from `_build_per_ticker_payload`."""
    payload: dict[str, Any] = {
        "call_bto_volume": 0,
        "call_sto_volume": 0,
        "put_bto_volume": 0,
        "put_sto_volume": 0,
        "put_intent": "speculative",
        "attribution_method": "snapshot_oi_delta",
    }
    if calibration_state is not None:
        payload["calibration_state"] = calibration_state
        payload["bootstrap_reason"] = bootstrap_reason
    return payload


# ---------------------------------------------------------------------------
# Assembler-level rollup — `assemble_q3_flow_classification_blocks`
# ---------------------------------------------------------------------------


def test_all_tickers_empty_rolls_up_to_unavailable() -> None:
    """Every ticker in a sector with no today-snapshot → block UNAVAILABLE."""
    blocks = assemble_q3_flow_classification_blocks(
        inputs=FlowClassificationAssemblyInputs(
            sector_to_tickers={"energy": ("APA", "BKR", "COP")},
            per_ticker_payload={
                "APA": _payload(
                    calibration_state=CalibrationState.UNAVAILABLE.value,
                    bootstrap_reason="q3.flow_classification: no options snapshots for APA",
                ),
                "BKR": _payload(
                    calibration_state=CalibrationState.UNAVAILABLE.value,
                    bootstrap_reason="q3.flow_classification: no options snapshots for BKR",
                ),
                "COP": _payload(
                    calibration_state=CalibrationState.UNAVAILABLE.value,
                    bootstrap_reason="q3.flow_classification: no options snapshots for COP",
                ),
            },
            sector_to_audience={"energy": OutputAudience.SECTOR_ENERGY},
        ),
        freshness_ts=_FRESHNESS,
    )

    assert len(blocks) == 1
    block = blocks[0]
    assert block.calibration_state is CalibrationState.UNAVAILABLE
    assert block.bootstrap_reason is not None
    # All three missing tickers named in the reason.
    for ticker in ("APA", "BKR", "COP"):
        assert ticker in block.bootstrap_reason


def test_mixed_tickers_roll_up_to_accumulating_naming_missing() -> None:
    """Some tickers live + some empty → ACCUMULATING listing the missing ones."""
    blocks = assemble_q3_flow_classification_blocks(
        inputs=FlowClassificationAssemblyInputs(
            sector_to_tickers={"energy": ("APA", "BKR", "COP")},
            per_ticker_payload={
                # APA has live snapshot data.
                "APA": _payload(
                    calibration_state=CalibrationState.CALIBRATED.value,
                    bootstrap_reason=None,
                ),
                # BKR and COP are missing today-snapshot data.
                "BKR": _payload(
                    calibration_state=CalibrationState.UNAVAILABLE.value,
                    bootstrap_reason="q3.flow_classification: no options snapshots for BKR",
                ),
                "COP": _payload(
                    calibration_state=CalibrationState.UNAVAILABLE.value,
                    bootstrap_reason="q3.flow_classification: no options snapshots for COP",
                ),
            },
            sector_to_audience={"energy": OutputAudience.SECTOR_ENERGY},
        ),
        freshness_ts=_FRESHNESS,
    )

    assert len(blocks) == 1
    block = blocks[0]
    assert block.calibration_state is CalibrationState.ACCUMULATING
    assert block.bootstrap_reason is not None
    # The missing tickers are named; the live ticker is not.
    assert "BKR" in block.bootstrap_reason
    assert "COP" in block.bootstrap_reason
    assert "APA" not in block.bootstrap_reason


def test_all_tickers_live_rolls_up_to_calibrated() -> None:
    """Every ticker has snapshot data → block CALIBRATED."""
    blocks = assemble_q3_flow_classification_blocks(
        inputs=FlowClassificationAssemblyInputs(
            sector_to_tickers={"energy": ("APA", "BKR")},
            per_ticker_payload={
                "APA": _payload(
                    calibration_state=CalibrationState.CALIBRATED.value,
                ),
                "BKR": _payload(
                    calibration_state=CalibrationState.CALIBRATED.value,
                ),
            },
            sector_to_audience={"energy": OutputAudience.SECTOR_ENERGY},
        ),
        freshness_ts=_FRESHNESS,
    )

    assert len(blocks) == 1
    block = blocks[0]
    assert block.calibration_state is CalibrationState.CALIBRATED
    assert block.bootstrap_reason is None


def test_legacy_payloads_without_calibration_state_default_to_calibrated() -> None:
    """Backward-compatible: per-ticker payloads with no ``calibration_state`` key
    remain CALIBRATED (so external test fixtures and other call sites that
    haven't been migrated keep working)."""
    blocks = assemble_q3_flow_classification_blocks(
        inputs=FlowClassificationAssemblyInputs(
            sector_to_tickers={"energy": ("APA",)},
            per_ticker_payload={"APA": _payload()},
            sector_to_audience={"energy": OutputAudience.SECTOR_ENERGY},
        ),
        freshness_ts=_FRESHNESS,
    )

    assert len(blocks) == 1
    assert blocks[0].calibration_state is CalibrationState.CALIBRATED


# ---------------------------------------------------------------------------
# Compute boundary — `_build_per_ticker_payload` via `assemble_q3_blocks_from_inputs`
# ---------------------------------------------------------------------------


def _contract(ticker: str, contract_id: str, contract_type: str = "call") -> OptionsContractRow:
    return OptionsContractRow(
        contract_ticker=contract_id,
        underlying_ticker=ticker,
        contract_type=contract_type,
    )


def _snapshot(
    contract_id: str,
    *,
    volume_today: int | None = 100,
    open_interest: int | None = 50,
) -> OptionsContractSnapshotRow:
    return OptionsContractSnapshotRow(
        contract_ticker=contract_id,
        snapshot_ts="2026-05-20T23:57:28Z",
        volume_today=volume_today,
        open_interest=open_interest,
    )


def _q3_inputs(
    *,
    per_ticker_pairs: Mapping[str, tuple[PerContractSnapshotPair, ...]],
    sector_to_tickers: Mapping[str, tuple[str, ...]],
) -> Q3Inputs:
    """Minimal `Q3Inputs` for exercising `assemble_q3_blocks_from_inputs`."""
    ticker_scope = tuple(per_ticker_pairs.keys())
    ticker_to_sector = {
        ticker: sector for sector, tickers in sector_to_tickers.items() for ticker in tickers
    }
    return Q3Inputs(
        ticker_scope=ticker_scope,
        as_of=_FRESHNESS,
        as_of_iso="2026-05-20T23:57:28Z",
        sector_to_tickers=sector_to_tickers,
        sector_to_audience={"energy": OutputAudience.SECTOR_ENERGY},
        ticker_to_sector=ticker_to_sector,
        flow_classification_inputs=FlowClassificationInputs(
            per_ticker_pairs=per_ticker_pairs,
        ),
        put_flow_intent_inputs=PutFlowIntentInputs(
            system_long_positions={},
            adv_by_ticker={ticker: None for ticker in ticker_scope},
        ),
        flow_zscores={},
        sector_etf_zscores={},
        index_zscores={},
        etf_iv_inputs={},
        iv_rank_results={},
        pair_correlations=None,
    )


def _flow_classification_block(blocks: Any) -> Any:
    for block in blocks:
        if block.block_id == "q3.flow_classification":
            return block
    raise AssertionError("expected a q3.flow_classification block in the output")


def test_build_per_ticker_payload_marks_ticker_unavailable_when_no_today_snapshot() -> None:
    """A ticker whose contracts all carry ``today_snapshot=None`` → UNAVAILABLE."""
    inputs = _q3_inputs(
        per_ticker_pairs={
            "APA": (
                PerContractSnapshotPair(
                    contract=_contract("APA", "APA-CALL-1"),
                    today_snapshot=None,
                    prior_snapshot=None,
                ),
                PerContractSnapshotPair(
                    contract=_contract("APA", "APA-PUT-1", contract_type="put"),
                    today_snapshot=None,
                    prior_snapshot=None,
                ),
            ),
        },
        sector_to_tickers={"energy": ("APA",)},
    )

    block = _flow_classification_block(assemble_q3_blocks_from_inputs(inputs))
    assert block.calibration_state is CalibrationState.UNAVAILABLE
    per_ticker = block.payload["per_ticker"]
    assert per_ticker["APA"]["calibration_state"] == CalibrationState.UNAVAILABLE.value
    assert per_ticker["APA"]["bootstrap_reason"] is not None


def test_build_per_ticker_payload_marks_ticker_calibrated_when_today_snapshot_present() -> None:
    """A ticker with any contract carrying today_snapshot → CALIBRATED.

    Today snapshot present even with ``volume_today=0`` is a real zero-volume
    read — not a missing-data state. Per-ticker label is CALIBRATED.
    """
    inputs = _q3_inputs(
        per_ticker_pairs={
            "APA": (
                PerContractSnapshotPair(
                    contract=_contract("APA", "APA-CALL-1"),
                    today_snapshot=_snapshot("APA-CALL-1", volume_today=0, open_interest=10),
                    prior_snapshot=None,
                ),
            ),
        },
        sector_to_tickers={"energy": ("APA",)},
    )

    block = _flow_classification_block(assemble_q3_blocks_from_inputs(inputs))
    assert block.calibration_state is CalibrationState.CALIBRATED
    per_ticker = block.payload["per_ticker"]
    assert per_ticker["APA"]["calibration_state"] == CalibrationState.CALIBRATED.value
    assert per_ticker["APA"]["bootstrap_reason"] is None


def test_build_per_ticker_payload_sector_mix_escalates_to_accumulating() -> None:
    """Sector with one live + one empty ticker → block ACCUMULATING."""
    inputs = _q3_inputs(
        per_ticker_pairs={
            "APA": (
                PerContractSnapshotPair(
                    contract=_contract("APA", "APA-CALL-1"),
                    today_snapshot=_snapshot("APA-CALL-1", volume_today=100, open_interest=50),
                    prior_snapshot=_snapshot("APA-CALL-1", volume_today=None, open_interest=40),
                ),
            ),
            "BKR": (
                PerContractSnapshotPair(
                    contract=_contract("BKR", "BKR-CALL-1"),
                    today_snapshot=None,
                    prior_snapshot=None,
                ),
            ),
        },
        sector_to_tickers={"energy": ("APA", "BKR")},
    )

    block = _flow_classification_block(assemble_q3_blocks_from_inputs(inputs))
    assert block.calibration_state is CalibrationState.ACCUMULATING
    assert block.bootstrap_reason is not None
    assert "BKR" in block.bootstrap_reason
    assert "APA" not in block.bootstrap_reason
    per_ticker = block.payload["per_ticker"]
    assert per_ticker["APA"]["calibration_state"] == CalibrationState.CALIBRATED.value
    assert per_ticker["BKR"]["calibration_state"] == CalibrationState.UNAVAILABLE.value

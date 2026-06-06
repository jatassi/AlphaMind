"""Regression coverage for ``StatePersistenceConfig`` threading — ALP-653.

Before ALP-653 the submit_envelope Phase-2 wrappers fell back to a hardcoded
``_stub_state_persistence_config()`` when the caller didn't supply one — and
the harness path never supplied one, so any Phase-2 knob read would have
silently landed on ``/tmp`` and ``window=1`` regardless of the operator's
real config.

These tests pin the contract that:

1. The Phase-2 wrappers (``_persist_envelope_outcome_via_phase2`` and friends)
   forward the supplied :class:`StatePersistenceConfig` instance verbatim to
   the engine's ``persist_envelope_outcome`` / ``persist_envelope_parse_failure``
   / ``persist_envelope_rejection`` entrypoints — no substitution, no defaulting.
2. The runner's :func:`run_portfolio_manager` forwards its
   ``state_persistence_config`` argument unchanged into the subprocess wrapper
   (which is the immediate downstream that ultimately reaches the harness +
   MCP closure + persist wrapper). Combined with the mypy-enforced required-
   keyword chain at every intermediate hop, that pins identity preservation
   end-to-end on the in-process path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from alphamind.decision.portfolio_manager.submit_envelope import persist as persist_module
from alphamind.state.config import StatePersistenceConfig


def _config() -> StatePersistenceConfig:
    return StatePersistenceConfig.model_validate(
        {
            "pm_decision_log_sliding_window_invocations": 3,
            "snapshot_read_timeout_seconds": 5.0,
            "pip_freeze_snapshot_root": "/tmp/alp-653",
            "invocation_provenance_root": "/tmp/alp-653",
        }
    )


@pytest.mark.asyncio
async def test_persist_envelope_outcome_via_phase2_forwards_config_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper passes the caller's :class:`StatePersistenceConfig` straight
    through to :func:`persist_envelope_outcome` — never a stub or default."""
    captured: dict[str, Any] = {}

    async def _spy(*args: Any, **kwargs: Any) -> None:
        captured["config"] = kwargs.get("config")

    monkeypatch.setattr(persist_module, "persist_envelope_outcome", _spy)

    cfg = _config()
    await persist_module._persist_envelope_outcome_via_phase2(
        invocation_handle=object(),
        envelope=object(),  # type: ignore[arg-type]  # spy ignores it
        submission_results=(),
        state_persistence_config=cfg,
        originating_proposal_json={"recommendation_id": "REC-1"},
    )

    assert captured["config"] is cfg


@pytest.mark.asyncio
async def test_persist_envelope_parse_failure_via_phase2_forwards_config_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror coverage for the parse-failure wrapper."""
    captured: dict[str, Any] = {}

    async def _spy(*args: Any, **kwargs: Any) -> None:
        captured["config"] = kwargs.get("config")

    monkeypatch.setattr(persist_module, "persist_envelope_parse_failure", _spy)

    cfg = _config()
    await persist_module._persist_envelope_parse_failure_via_phase2(
        invocation_handle=object(),
        failed_entry=object(),  # type: ignore[arg-type]  # spy ignores it
        state_persistence_config=cfg,
    )

    assert captured["config"] is cfg


@pytest.mark.asyncio
async def test_persist_envelope_rejection_via_phase2_forwards_config_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirror coverage for the rejection wrapper."""
    captured: dict[str, Any] = {}

    async def _spy(*args: Any, **kwargs: Any) -> None:
        captured["config"] = kwargs.get("config")

    monkeypatch.setattr(persist_module, "persist_envelope_rejection", _spy)

    cfg = _config()
    errors: Sequence[Any] = ()
    await persist_module._persist_envelope_rejection_via_phase2(
        invocation_handle=object(),
        envelope=object(),  # type: ignore[arg-type]  # spy ignores it
        errors=errors,
        state_persistence_config=cfg,
    )

    assert captured["config"] is cfg


def test_persist_module_has_no_stub_state_persistence_config() -> None:
    """The pre-ALP-653 default-substitution helper is gone — its presence would
    re-introduce the silent override risk this issue retired."""
    assert not hasattr(persist_module, "_stub_state_persistence_config")
    # And ``__all__`` no longer advertises it.
    assert "_stub_state_persistence_config" not in persist_module.__all__


def test_state_persistence_config_roundtrips_through_subprocess_payload_serialization() -> None:
    """The subprocess path encodes the config as JSON in the worker payload
    (``model_dump(mode="json")``) and the worker rehydrates it via
    ``model_validate``. A field that doesn't survive this roundtrip would
    silently land production on a value-altered config — pin field equality
    so any future schema addition that breaks roundtrip fidelity surfaces here.
    """
    import json

    cfg = _config()
    # Mirror exactly what the parent process sends and the worker reads
    # (``_sdk_subprocess.py`` line ~781 and ``_sdk_subprocess_worker.py`` line ~439).
    serialized = json.dumps(cfg.model_dump(mode="json"))
    rehydrated = StatePersistenceConfig.model_validate(json.loads(serialized))

    assert rehydrated == cfg


@pytest.mark.asyncio
async def test_runner_forwards_state_persistence_config_to_subprocess_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """The runner's ``state_persistence_config`` argument reaches the immediate
    downstream (``invoke_portfolio_manager_in_subprocess``) as the same instance.

    Combined with the wrapper's in-process branch (taken when ``sdk_query_fn``
    is supplied) and the persist-wrapper coverage above, this pins identity
    preservation end-to-end through the in-process path.
    """
    from alphamind.decision.portfolio_manager import runner as runner_module
    from tests.decision.portfolio_manager.test_runner import (
        _agent_config,
        _completion_payload,
        _make_sdk_response,
        _make_stub_query,
        _runner_kwargs,
    )

    captured: dict[str, Any] = {}

    async def _capturing_wrapper(**kwargs: Any) -> Any:
        captured["config"] = kwargs["state_persistence_config"]
        # Return a minimal HarnessSuccess-shaped object — the runner just
        # forwards it into PMResult.
        from alphamind.analysis._shared import TokensUsed
        from alphamind.commands.pm_envelope import PMCompletionRecord
        from alphamind.decision.portfolio_manager.harness import HarnessSuccess

        payload = _completion_payload()
        return HarnessSuccess(
            output=PMCompletionRecord.model_validate(payload),
            retry_count=0,
            tokens_used=TokensUsed(
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
            ),
            tool_calls_used=0,
            wall_clock_seconds=0.0,
            stop_reason="end_turn",
            submission_log=(),
        )

    monkeypatch.setattr(runner_module, "invoke_portfolio_manager_in_subprocess", _capturing_wrapper)

    stub = _make_stub_query([_make_sdk_response(_completion_payload())])

    supplied_config = _config()
    kwargs = _runner_kwargs(
        mode="normal",
        sdk_query_fn=stub,
        agent_config=_agent_config(),
        archive_root=tmp_path / "archive",
    )
    # Override the helper's default with the instance we want to pin.
    kwargs["state_persistence_config"] = supplied_config

    await runner_module.run_portfolio_manager(**kwargs)

    assert captured["config"] is supplied_config

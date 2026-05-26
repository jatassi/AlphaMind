"""``PipelineClient`` — loopback HTTP client for the pipeline ``/control`` surface.

Story 04a wires five verb proxies (``pause`` / ``resume`` /
``trigger_emergency_invocation`` / ``switch_profile`` /
``run_universe_validation``) over the pipeline control surface shipped
in ALP-664. The seam is a :class:`PipelineClient` Protocol with two
implementations:

* :class:`RealPipelineClient` — :class:`httpx.AsyncClient`-backed,
  targets the loopback URL from
  :class:`alphamind.command_center.config.PipelineClientConfig`.
* :class:`FakePipelineClient` — in-memory canned-response store used
  by every unit test. The story-07 verify script is the only place
  ``RealPipelineClient`` is exercised end-to-end.

Per the ALP-128 architectural invariants:

* **No direct cross-process imports.** This module does NOT import from
  :mod:`alphamind.scheduler`; the wire shape is mirrored locally
  through Pydantic at the HTTP boundary, then converted to the
  frozen-dataclass result records below.
* **Pydantic at boundaries only.** The verb methods return frozen
  dataclasses (:class:`PipelinePauseResult`, etc.); the boundary models
  in :mod:`alphamind.command_center.control.models` live exclusively
  in the route layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

from alphamind.command_center._kernel.control import (
    ControlErrorCode,
    ControlResult,
)
from alphamind.command_center.control._envelope import (
    extract_str_field,
    failure_from_transport_error,
    parse_error_envelope,
    safe_json,
)
from alphamind.config.control_handlers.profile_switch import (
    ProfileSwitchOutcome,
)
from alphamind.config.models.main import Profile

__all__ = [
    "FakePipelineClient",
    "PipelineClient",
    "PipelineRunUniverseValidationResult",
    "PipelineSwitchProfileResult",
    "PipelineTriggerEmergencyResult",
    "PipelineUniverseValidationCriterion",
    "PipelineUniverseValidationReport",
    "PipelineUniverseValidationTicker",
    "RealPipelineClient",
]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frozen-dataclass result records — internal mirrors of the verb-specific
# response envelopes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PipelineTriggerEmergencyResult:
    """Result of ``trigger_emergency_invocation`` — envelope + invocation id.

    The ``ok=False`` failure path carries no ``applied_at`` / ``invocation_id``;
    inspect ``result.error_code`` / ``result.error_detail``.
    """

    result: ControlResult
    invocation_id: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineSwitchProfileResult:
    """Result of ``switch_profile`` — envelope + outcome.

    The upstream ``SwitchProfileResult`` carries both ``applied_at`` and
    the :class:`ProfileSwitchOutcome` so story 04a can call
    :func:`alphamind.state.invocation_context.config_change.emit_profile_switch_entry`
    inside the open operator-invocation handle without re-deriving
    ``previous_profile`` / ``new_profile`` / ``is_no_op``. On failure,
    ``outcome`` is ``None`` and ``result`` carries the error envelope.
    """

    result: ControlResult
    outcome: ProfileSwitchOutcome | None = None


@dataclass(frozen=True, slots=True)
class PipelineUniverseValidationCriterion:
    """One criterion row in the universe-validation report (internal mirror)."""

    criterion: str
    verdict: str
    computed_value: float | dict[str, float] | None = None
    threshold: float | dict[str, float] | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PipelineUniverseValidationTicker:
    """One ticker row in the universe-validation report (internal mirror)."""

    ticker: str
    verdict: str
    criteria: tuple[PipelineUniverseValidationCriterion, ...]


@dataclass(frozen=True, slots=True)
class PipelineUniverseValidationReport:
    """Universe-validation report (internal mirror).

    The wire shape carries an aware datetime; the internal mirror keeps
    the same shape so the route layer can re-validate via the Pydantic
    boundary model.
    """

    validated_at: datetime
    tickers: tuple[PipelineUniverseValidationTicker, ...]


@dataclass(frozen=True, slots=True)
class PipelineRunUniverseValidationResult:
    """Result of ``run_universe_validation`` — envelope + report.

    On failure ``report`` is ``None`` and ``result`` carries the error
    envelope.
    """

    result: ControlResult
    report: PipelineUniverseValidationReport | None = None


# ---------------------------------------------------------------------------
# Protocol seam.
# ---------------------------------------------------------------------------


class PipelineClient(Protocol):
    """Async interface to the pipeline ``/control/*`` surface.

    Every method returns either a success record (whose ``result`` field
    carries :meth:`ControlResult.success`) or a failure record (whose
    ``result`` field carries :meth:`ControlResult.failure`). Transport
    errors (httpx timeouts, connection failures, malformed JSON) map to
    :data:`ControlErrorCode.INTERNAL_ERROR`.
    """

    async def pause(self, *, reason: str) -> ControlResult: ...

    async def resume(self) -> ControlResult: ...

    async def trigger_emergency_invocation(
        self, *, reason: str
    ) -> PipelineTriggerEmergencyResult: ...

    async def switch_profile(
        self, *, profile_name: str
    ) -> PipelineSwitchProfileResult: ...

    async def run_universe_validation(self) -> PipelineRunUniverseValidationResult: ...


# ---------------------------------------------------------------------------
# RealPipelineClient — httpx-backed.
# ---------------------------------------------------------------------------


_SOURCE_TAG = "pipeline"


class RealPipelineClient:
    """httpx-backed :class:`PipelineClient` for production.

    Constructed with a shared :class:`httpx.AsyncClient` whose lifetime
    is owned by the composition root (typically the FastAPI lifespan).
    Each verb posts to the corresponding ``/control/*`` path under the
    configured ``control_url``.
    """

    def __init__(self, *, base_url: str, http_client: httpx.AsyncClient) -> None:
        # Strip a trailing slash so path concatenation yields a single ``/``.
        self._base_url = base_url.rstrip("/")
        self._http_client = http_client

    async def pause(self, *, reason: str) -> ControlResult:
        return await self._call_envelope_only(
            path="/control/pause", body={"reason": reason}
        )

    async def resume(self) -> ControlResult:
        return await self._call_envelope_only(path="/control/resume", body={})

    async def trigger_emergency_invocation(
        self, *, reason: str
    ) -> PipelineTriggerEmergencyResult:
        try:
            response = await self._http_client.post(
                self._url("/control/trigger_emergency_invocation"),
                json={"reason": reason},
            )
        except httpx.RequestError as exc:
            return PipelineTriggerEmergencyResult(
                result=failure_from_transport_error(exc, source=_SOURCE_TAG)
            )
        if response.is_success:
            payload = safe_json(response)
            applied_at = extract_str_field(payload, "applied_at")
            invocation_id = extract_str_field(payload, "invocation_id")
            if applied_at is None or invocation_id is None:
                return PipelineTriggerEmergencyResult(
                    result=ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail=(
                            "upstream trigger_emergency_invocation response "
                            "missing applied_at or invocation_id"
                        ),
                    )
                )
            return PipelineTriggerEmergencyResult(
                result=ControlResult.success(applied_at=applied_at),
                invocation_id=invocation_id,
            )
        return PipelineTriggerEmergencyResult(result=parse_error_envelope(response, source=_SOURCE_TAG))

    async def switch_profile(
        self, *, profile_name: str
    ) -> PipelineSwitchProfileResult:
        try:
            response = await self._http_client.post(
                self._url("/control/switch_profile"),
                json={"profile_name": profile_name},
            )
        except httpx.RequestError as exc:
            return PipelineSwitchProfileResult(
                result=failure_from_transport_error(exc, source=_SOURCE_TAG)
            )
        if response.is_success:
            payload = safe_json(response)
            applied_at = extract_str_field(payload, "applied_at")
            outcome = _extract_profile_switch_outcome(payload, profile_name=profile_name)
            if applied_at is None or outcome is None:
                return PipelineSwitchProfileResult(
                    result=ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail=(
                            "upstream switch_profile response missing applied_at "
                            "or the outcome payload"
                        ),
                    )
                )
            return PipelineSwitchProfileResult(
                result=ControlResult.success(applied_at=applied_at), outcome=outcome
            )
        return PipelineSwitchProfileResult(result=parse_error_envelope(response, source=_SOURCE_TAG))

    async def run_universe_validation(self) -> PipelineRunUniverseValidationResult:
        try:
            response = await self._http_client.post(
                self._url("/control/run_universe_validation"), json={}
            )
        except httpx.RequestError as exc:
            return PipelineRunUniverseValidationResult(
                result=failure_from_transport_error(exc, source=_SOURCE_TAG)
            )
        if response.is_success:
            payload = safe_json(response)
            applied_at = extract_str_field(payload, "applied_at")
            report = _extract_universe_report(payload)
            if applied_at is None or report is None:
                return PipelineRunUniverseValidationResult(
                    result=ControlResult.failure(
                        error_code=ControlErrorCode.INTERNAL_ERROR,
                        error_detail=(
                            "upstream run_universe_validation response missing "
                            "applied_at or report"
                        ),
                    )
                )
            return PipelineRunUniverseValidationResult(
                result=ControlResult.success(applied_at=applied_at), report=report
            )
        return PipelineRunUniverseValidationResult(result=parse_error_envelope(response, source=_SOURCE_TAG))

    async def _call_envelope_only(
        self, *, path: str, body: dict[str, object]
    ) -> ControlResult:
        """Wire shape for the verbs that return the bare ``ControlResponseEnvelope``."""
        try:
            response = await self._http_client.post(self._url(path), json=body)
        except httpx.RequestError as exc:
            return failure_from_transport_error(exc, source=_SOURCE_TAG)
        if response.is_success:
            payload = safe_json(response)
            applied_at = extract_str_field(payload, "applied_at")
            if applied_at is None:
                return ControlResult.failure(
                    error_code=ControlErrorCode.INTERNAL_ERROR,
                    error_detail=f"upstream {path} response missing applied_at",
                )
            return ControlResult.success(applied_at=applied_at)
        return parse_error_envelope(response, source=_SOURCE_TAG)

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self._base_url + path


# ---------------------------------------------------------------------------
# Verb-specific payload reconstruction (private to pipeline_client).
# ---------------------------------------------------------------------------


def _extract_profile_switch_outcome(
    payload: object, *, profile_name: str
) -> ProfileSwitchOutcome | None:
    """Reconstruct a :class:`ProfileSwitchOutcome` from the upstream response.

    The pipeline schema's ``switch_profile`` response carries only the
    envelope (``status`` + ``applied_at``) on the wire — the
    ``ProfileSwitchOutcome`` is server-side state the upstream's verb
    layer produced. The proxy MUST faithfully reconstruct it to drive
    ``emit_profile_switch_entry``.

    The pre-resolved approach: upstream serializes the outcome as a
    nested dict on the success envelope. If the upstream omits the
    outcome (the ALP-664-shipped shape today), the client returns
    ``None`` and the proxy surfaces an internal-error envelope rather
    than guessing. The story-07 verify script confirms the upstream
    surface is sending the outcome; until then, fakes drive the proxy.
    """
    if not isinstance(payload, dict):
        return None
    outcome_raw = payload.get("outcome")
    if not isinstance(outcome_raw, dict):
        return None
    try:
        previous = Profile(outcome_raw["previous_profile"])
        new = Profile(outcome_raw["new_profile"])
    except (KeyError, ValueError):
        return None
    main_yaml_path_raw = outcome_raw.get("main_yaml_path")
    is_no_op_raw = outcome_raw.get("is_no_op")
    if not isinstance(main_yaml_path_raw, str) or not isinstance(is_no_op_raw, bool):
        return None
    from pathlib import Path

    return ProfileSwitchOutcome(
        previous_profile=previous,
        new_profile=new,
        main_yaml_path=Path(main_yaml_path_raw),
        is_no_op=is_no_op_raw,
    )


def _extract_universe_report(payload: object) -> PipelineUniverseValidationReport | None:
    """Reconstruct the universe-validation report from the upstream JSON."""
    if not isinstance(payload, dict):
        return None
    report_raw = payload.get("report")
    if not isinstance(report_raw, dict):
        return None
    validated_at_raw = report_raw.get("validated_at")
    tickers_raw = report_raw.get("tickers")
    if not isinstance(validated_at_raw, str) or not isinstance(tickers_raw, list):
        return None
    try:
        validated_at = datetime.fromisoformat(validated_at_raw)
    except ValueError:
        return None
    tickers: list[PipelineUniverseValidationTicker] = []
    for ticker_raw in tickers_raw:
        if not isinstance(ticker_raw, dict):
            return None
        ticker = ticker_raw.get("ticker")
        verdict = ticker_raw.get("verdict")
        criteria_raw = ticker_raw.get("criteria")
        if (
            not isinstance(ticker, str)
            or not isinstance(verdict, str)
            or not isinstance(criteria_raw, list)
        ):
            return None
        criteria: list[PipelineUniverseValidationCriterion] = []
        for c_raw in criteria_raw:
            if not isinstance(c_raw, dict):
                return None
            c_name = c_raw.get("criterion")
            c_verdict = c_raw.get("verdict")
            if not isinstance(c_name, str) or not isinstance(c_verdict, str):
                return None
            criteria.append(
                PipelineUniverseValidationCriterion(
                    criterion=c_name,
                    verdict=c_verdict,
                    computed_value=c_raw.get("computed_value"),  # type: ignore[arg-type]
                    threshold=c_raw.get("threshold"),  # type: ignore[arg-type]
                    note=c_raw.get("note") if isinstance(c_raw.get("note"), str) else None,
                )
            )
        tickers.append(
            PipelineUniverseValidationTicker(
                ticker=ticker, verdict=verdict, criteria=tuple(criteria)
            )
        )
    return PipelineUniverseValidationReport(
        validated_at=validated_at, tickers=tuple(tickers)
    )


# ---------------------------------------------------------------------------
# FakePipelineClient — in-memory canned responses for unit tests.
# ---------------------------------------------------------------------------


class FakePipelineClient:
    """In-memory :class:`PipelineClient` for unit tests.

    Records every call so tests can assert on ordering / arguments.
    Canned responses are set per-verb via the ``set_*_response`` /
    ``set_*_failure`` setters. The default response for each verb is a
    successful envelope with a fixed ``applied_at``; tests override
    selectively.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._pause_response: ControlResult = ControlResult.success(
            applied_at="2026-05-26T12:00:00Z"
        )
        self._resume_response: ControlResult = ControlResult.success(
            applied_at="2026-05-26T12:00:00Z"
        )
        self._trigger_emergency_response: PipelineTriggerEmergencyResult = (
            PipelineTriggerEmergencyResult(
                result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
                invocation_id="inv-fake",
            )
        )
        self._switch_profile_response: PipelineSwitchProfileResult | None = None
        self._universe_validation_response: PipelineRunUniverseValidationResult | None = None

    # ---- response setters --------------------------------------------------

    def set_pause_response(self, response: ControlResult) -> None:
        self._pause_response = response

    def set_resume_response(self, response: ControlResult) -> None:
        self._resume_response = response

    def set_trigger_emergency_response(
        self, response: PipelineTriggerEmergencyResult
    ) -> None:
        self._trigger_emergency_response = response

    def set_switch_profile_response(self, response: PipelineSwitchProfileResult) -> None:
        self._switch_profile_response = response

    def set_universe_validation_response(
        self, response: PipelineRunUniverseValidationResult
    ) -> None:
        self._universe_validation_response = response

    # ---- Protocol surface --------------------------------------------------

    async def pause(self, *, reason: str) -> ControlResult:
        self.calls.append(("pause", {"reason": reason}))
        return self._pause_response

    async def resume(self) -> ControlResult:
        self.calls.append(("resume", {}))
        return self._resume_response

    async def trigger_emergency_invocation(
        self, *, reason: str
    ) -> PipelineTriggerEmergencyResult:
        self.calls.append(("trigger_emergency_invocation", {"reason": reason}))
        return self._trigger_emergency_response

    async def switch_profile(
        self, *, profile_name: str
    ) -> PipelineSwitchProfileResult:
        self.calls.append(("switch_profile", {"profile_name": profile_name}))
        if self._switch_profile_response is not None:
            return self._switch_profile_response
        # Default: success with a synthesized non-no-op outcome.
        try:
            new_profile = Profile(profile_name)
        except ValueError:
            return PipelineSwitchProfileResult(
                result=ControlResult.failure(
                    error_code=ControlErrorCode.VALIDATION_FAILED,
                    error_detail=f"profile_name {profile_name!r} is not valid",
                )
            )
        from pathlib import Path as _Path

        # The default outcome treats every requested switch as a real
        # transition from ``Profile.medium`` to the requested profile so
        # tests of the proxy's emit_profile_switch_entry path get a
        # non-no-op outcome by default. Tests that need a no-op or a
        # different previous-profile call ``set_switch_profile_response``
        # explicitly.
        outcome = ProfileSwitchOutcome(
            previous_profile=Profile.medium,
            new_profile=new_profile,
            main_yaml_path=_Path("config/main.yaml"),
            is_no_op=new_profile == Profile.medium,
        )
        return PipelineSwitchProfileResult(
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
            outcome=outcome,
        )

    async def run_universe_validation(self) -> PipelineRunUniverseValidationResult:
        self.calls.append(("run_universe_validation", {}))
        if self._universe_validation_response is not None:
            return self._universe_validation_response
        return PipelineRunUniverseValidationResult(
            result=ControlResult.success(applied_at="2026-05-26T12:00:00Z"),
            report=PipelineUniverseValidationReport(
                validated_at=datetime.fromisoformat("2026-05-26T12:00:00+00:00"),
                tickers=(
                    PipelineUniverseValidationTicker(
                        ticker="AAPL",
                        verdict="pass",
                        criteria=(
                            PipelineUniverseValidationCriterion(
                                criterion="adv", verdict="pass"
                            ),
                            PipelineUniverseValidationCriterion(
                                criterion="analyst_coverage", verdict="pass"
                            ),
                            PipelineUniverseValidationCriterion(
                                criterion="beta", verdict="pass"
                            ),
                            PipelineUniverseValidationCriterion(
                                criterion="market_cap", verdict="pass"
                            ),
                            PipelineUniverseValidationCriterion(
                                criterion="options_oi", verdict="pass"
                            ),
                        ),
                    ),
                ),
            ),
        )

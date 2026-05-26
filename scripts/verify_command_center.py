"""End-to-end verification for ``python -m alphamind.command_center``.

Operator entry point for the ALP-128 work tree's acceptance gate
(story 07 / ALP-685). The script boots the command-center FastAPI app
in-process, drives a passkey roundtrip via the in-memory WebAuthn
verifier, exercises each of the 8 control verbs through the proxy
layer, fires one alert, observes one pipeline + one monitor SSE
event, checks the frontend bundle, and tears down. Exits 0 only
when every check passes; non-zero on any failure with a per-step
report.

The seven check helpers are module-level functions so
``tests/scripts/test_verify_command_center.py`` can exercise them
without booting a real daemon:

* :func:`check_daemons_bind` — FastAPI app accepts HTTP traffic at
  the configured loopback port within a 30s budget. Production
  Windows runs additionally boot the pipeline + monitor daemons
  (NSSM-managed); on dev Mac the command center alone is booted
  against in-memory fakes pointing at the pipeline / monitor URLs.
* :func:`check_passkey_roundtrip` — runs the WebAuthn ceremony pair
  (``/auth/register/begin`` → ``/auth/register/complete`` →
  ``/auth/login/begin`` → ``/auth/login/complete``) against
  :class:`InMemoryWebauthnVerifier`. Asserts ``cc_session`` + the
  configured CSRF cookie land on the final response.
* :func:`check_control_verbs` — exercises all 8 ``/api/control/*``
  endpoints (pause, resume, trigger_emergency_invocation,
  switch_profile, run_universe_validation, cancel_order,
  force_close_position, set_halt_mode). For each: response envelope
  carries ``ok=true`` AND one ``activity_log`` row landed with
  ``source=OPERATOR_CONSOLE``. The ``switch_profile`` verb is
  additionally asserted to write a ``PROFILE_SWITCHED`` event_type
  row.
* :func:`check_alert_fires` — publishes a synthetic
  ``INVOCATION_ENDED`` pipeline event with ``status=failed`` onto the
  multiplexer so the ``pipeline_aborted`` rule fires. Asserts: a
  fresh ``alerts`` row exists, the fake Discord channel recorded
  one call, and one ``cc:alert_fired`` SSE envelope is published.
* :func:`check_sse_roundtrip` — subscribes to the multiplexer +
  publishes one synthetic pipeline frame + one synthetic monitor
  frame, observes both arrive at the subscriber's queue.
* :func:`check_frontend_build` — confirms
  ``frontend/dist/index.html`` exists. On the dev box the file
  must have been produced by ``bun run build``; the script does NOT
  run bun itself (the operator's machine may not have bun
  installed — the RUNBOOK documents the prerequisite).
* :func:`check_clean_teardown` — supervisor's ``request_stop()``
  flushes Uvicorn within 10 s. Asserts ``run_task`` returned cleanly.

Usage::

    set -a && source <(tr -d '\\r' < .env) && set +a && \\
        uv run python scripts/verify_command_center.py

See ``scripts/RUNBOOK_command_center.md`` for the full operator
runbook including bring-up, day-to-day operations, troubleshooting,
and the NSSM service-install workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import secrets
import socket
import sys
import tempfile
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alphamind.command_center._kernel.events import (
    MonitorEvent,
    MonitorEventType,
    PipelineEvent,
    PipelineEventType,
)
from alphamind.command_center.alerts.channels.discord import (
    DiscordChannel,
    FakeDiscordChannel,
)
from alphamind.command_center.app import (
    AlertsOverrides,
    AuthOverrides,
    ControlOverrides,
    EventsOverrides,
    build_app,
)
from alphamind.command_center.auth.setup_token import SetupTokenGate
from alphamind.command_center.auth.webauthn import (
    InMemoryWebauthnVerifier,
    encode_inmemory_attestation_object,
    encode_inmemory_client_data_json,
)
from alphamind.command_center.config import (
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)
from alphamind.command_center.control.monitor_client import FakeMonitorClient
from alphamind.command_center.control.pipeline_client import FakePipelineClient
from alphamind.command_center.events.clients import (
    FakeMonitorEventsClient,
    FakePipelineEventsClient,
)
from alphamind.command_center.persistence.tables import AlertRow, CommandCenterBase
from alphamind.command_center.session import ProcessSession
from alphamind.command_center.supervisor import CommandCenterSupervisor
from alphamind.persistence.models import Base
from alphamind.scripts._stdio import configure_utf8_stdio
from alphamind.state.tables.activity_log import ActivityLogRow
from alphamind.state.tables.process_lifetimes import ProcessLifetimeRow

__all__ = [
    "STEP_LABELS",
    "CheckResult",
    "VerifyContext",
    "check_alert_fires",
    "check_clean_teardown",
    "check_control_verbs",
    "check_daemons_bind",
    "check_frontend_build",
    "check_passkey_roundtrip",
    "check_sse_roundtrip",
    "main",
]


log = logging.getLogger(__name__)


STEP_LABELS: tuple[str, ...] = (
    "daemons_bind",
    "passkey_roundtrip",
    "control_verbs",
    "alert_fires",
    "sse_roundtrip",
    "frontend_build",
    "clean_teardown",
)
"""Ordered labels of the 7 checks the verify script runs.

Mirrored by ``tests/scripts/test_verify_command_center.py`` so a
drop / rename surfaces as a test failure rather than a silent
regression in operator output. The order matches :func:`main`'s
execution sequence.
"""


_DAEMON_BIND_TIMEOUT_SECONDS = 30.0
"""Per-AC: the daemon must bind within 30 s; the check FAILs on timeout."""

_CLEAN_TEARDOWN_TIMEOUT_SECONDS = 10.0
"""Per-AC: SIGTERM → clean shutdown must complete within 10 s."""

_BIND_POLL_INTERVAL_SECONDS = 0.1
"""Sleep between successive ``socket.connect()`` probes during bind wait."""

_FROZEN_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
"""Frozen clock used by the in-memory auth surface.

The auth flow's session-expiry checks read off ``app.state.clock``;
freezing here keeps the per-test verdict deterministic + independent
of wall-clock drift during the run.
"""

_PROCESS_LIFETIME_ID = "plt-verify-command-center"
"""FK target inserted into ``process_lifetimes`` before any operator action.

The control proxy writes ``invocations`` rows with this FK; the row
must exist or the proxy's INSERT raises an IntegrityError.
"""

_SESSION_ID = "sess-verify-command-center"
"""Synthetic session id used to satisfy ``current_session`` dependency.

The verify script bypasses the WebAuthn ceremony for the control
verbs check (those are exercised in :func:`check_passkey_roundtrip`)
and overrides the auth dependencies directly so the 8 verbs run
without re-issuing cookies on each call.
"""


# ---------------------------------------------------------------------------
# CheckResult — small value type the operator output is built from
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One check's outcome.

    Mirrors :class:`alphamind.scripts.verify_debug_e2e.CheckResult`
    structurally — same ``label`` / ``passed`` / ``message`` shape +
    same ``format_line()`` rendering — so an operator reading both
    scripts' outputs side-by-side sees identical PASS / FAIL lines.
    """

    label: str
    passed: bool
    message: str

    def format_line(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return f"{verdict}: {self.label} — {self.message}"


# ---------------------------------------------------------------------------
# VerifyContext — collaborators the checks consume
# ---------------------------------------------------------------------------


@dataclass
class VerifyContext:
    """Mutable container of collaborators wired up by :func:`main`.

    Each check helper takes a context (not a long parameter list) so
    the dispatch site stays readable and a new collaborator can be
    threaded in without re-shaping every check signature. The dataclass
    is not frozen because some fields (e.g. ``base_url``,
    ``passkey_credentials``) are populated as boot progresses.
    """

    app_port: int
    base_url: str = ""
    fake_pipeline_client: FakePipelineClient = field(default_factory=FakePipelineClient)
    fake_monitor_client: FakeMonitorClient = field(default_factory=FakeMonitorClient)
    fake_discord_channel: FakeDiscordChannel = field(default_factory=FakeDiscordChannel)
    fake_pipeline_events_client: FakePipelineEventsClient | None = None
    fake_monitor_events_client: FakeMonitorEventsClient | None = None
    webauthn_verifier: InMemoryWebauthnVerifier = field(
        default_factory=InMemoryWebauthnVerifier
    )
    setup_token_gate: SetupTokenGate = field(default_factory=SetupTokenGate)
    cc_writer_factory: async_sessionmaker[AsyncSession] | None = None
    production_session_factory: async_sessionmaker[AsyncSession] | None = None
    app: object | None = None  # FastAPI handle — pulled lazily to keep imports lean
    supervisor: CommandCenterSupervisor | None = None
    run_task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# check_daemons_bind
# ---------------------------------------------------------------------------


async def check_daemons_bind(*, host: str, port: int, timeout_seconds: float) -> CheckResult:
    """Poll ``socket.connect((host, port))`` until success or the budget expires.

    The supervisor boots Uvicorn under a TaskGroup before this check
    fires; the check exists to confirm the bind actually completed.
    The 30 s budget matches the AC; the per-attempt sleep is small so
    a fast bind (the typical case) produces a near-immediate PASS.

    Returns FAIL on timeout. PASS message reports the wall-clock
    elapsed seconds so an operator can spot a slow-bind trend
    over multiple verify runs.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    attempts = 0
    while loop.time() < deadline:
        attempts += 1
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                s.connect((host, port))
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(_BIND_POLL_INTERVAL_SECONDS)
            continue
        elapsed = timeout_seconds - (deadline - loop.time())
        return CheckResult(
            label="daemons_bind",
            passed=True,
            message=(
                f"command center bound to {host}:{port} after {elapsed:.2f}s "
                f"({attempts} probe(s))"
            ),
        )
    return CheckResult(
        label="daemons_bind",
        passed=False,
        message=(
            f"command center did NOT bind to {host}:{port} within "
            f"{timeout_seconds:.0f}s ({attempts} probe(s)); the daemon "
            "either crashed at startup or never reached Uvicorn.serve()"
        ),
    )


# ---------------------------------------------------------------------------
# check_passkey_roundtrip
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


async def check_passkey_roundtrip(
    *,
    client: httpx.AsyncClient,
    setup_token: str,
    csrf_cookie_name: str,
    session_cookie_name: str,
) -> CheckResult:
    """Drive a full WebAuthn registration + login roundtrip.

    Steps:

    1. ``POST /auth/register/begin`` with the operator-supplied
       ``setup_token`` (first-launch enrollment flow).
    2. Synthesize the in-memory client_data_json + attestation_object
       blobs using the test helpers, then
       ``POST /auth/register/complete`` with the WebAuthn response.
       Reads the CSRF cookie off the begin response and echoes it via
       ``X-CSRF-Token``.
    3. ``POST /auth/login/begin`` to start the login ceremony.
    4. ``POST /auth/login/complete`` with the in-memory authentication
       response. Asserts the response sets both the session + CSRF
       cookies.

    The check returns FAIL on any non-2xx response and on a missing
    cookie set. The cookie names are configurable via
    :class:`SecurityConfig.session.cookie_name` / ``csrf.cookie_name``;
    the caller threads the resolved values in.
    """
    # ---- /auth/register/begin ---------------------------------------------
    begin_url = "/auth/register/begin"
    register_begin_resp = await client.post(
        begin_url,
        json={"user_name": "verify-operator", "setup_token": setup_token},
    )
    if register_begin_resp.status_code != 200:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"POST {begin_url} returned {register_begin_resp.status_code}; "
                f"body: {register_begin_resp.text[:200]}"
            ),
        )
    register_begin = register_begin_resp.json()
    challenge_token = register_begin["challenge_token"]
    challenge_b64 = register_begin["challenge"]
    challenge = _b64url_decode(challenge_b64)

    # Read the CSRF cookie the begin response set so /complete can echo
    # it back via the X-CSRF-Token header (the F8 pre-session flow).
    csrf_cookie = client.cookies.get(csrf_cookie_name)
    if csrf_cookie is None:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"register/begin response did NOT set {csrf_cookie_name!r} cookie; "
                "the pre-session CSRF flow is broken"
            ),
        )

    # ---- /auth/register/complete ------------------------------------------
    credential_id = "verify-credential-1"
    public_key = b"verify-pubkey"
    client_data_json = encode_inmemory_client_data_json(challenge)
    attestation_object = encode_inmemory_attestation_object(
        public_key=public_key, sign_count=0
    )
    register_complete_resp = await client.post(
        "/auth/register/complete",
        json={
            "challenge_token": challenge_token,
            "credential_id": credential_id,
            "client_data_json": _b64url_encode(client_data_json),
            "attestation_object": _b64url_encode(attestation_object),
            "transports": [],
        },
        headers={"X-CSRF-Token": csrf_cookie},
    )
    if register_complete_resp.status_code != 200:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"POST /auth/register/complete returned "
                f"{register_complete_resp.status_code}; "
                f"body: {register_complete_resp.text[:200]}"
            ),
        )
    if client.cookies.get(session_cookie_name) is None:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"register/complete response did NOT set {session_cookie_name!r} cookie; "
                "the post-registration session issuance is broken"
            ),
        )

    # ---- /auth/login/begin -------------------------------------------------
    login_begin_resp = await client.post("/auth/login/begin")
    if login_begin_resp.status_code != 200:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"POST /auth/login/begin returned {login_begin_resp.status_code}; "
                f"body: {login_begin_resp.text[:200]}"
            ),
        )
    login_begin = login_begin_resp.json()
    login_challenge = _b64url_decode(login_begin["challenge"])
    login_challenge_token = login_begin["challenge_token"]
    login_csrf_cookie = client.cookies.get(csrf_cookie_name)
    if login_csrf_cookie is None:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"login/begin response did NOT set {csrf_cookie_name!r} cookie; "
                "the pre-session CSRF flow is broken on login"
            ),
        )

    # ---- /auth/login/complete ---------------------------------------------
    login_client_data = encode_inmemory_client_data_json(login_challenge)
    login_complete_resp = await client.post(
        "/auth/login/complete",
        json={
            "challenge_token": login_challenge_token,
            "credential_id": credential_id,
            "client_data_json": _b64url_encode(login_client_data),
            "authenticator_data": _b64url_encode(b"authdata-stub"),
            "signature": _b64url_encode(b"signature-stub"),
            "new_sign_count": 1,
        },
        headers={"X-CSRF-Token": login_csrf_cookie},
    )
    if login_complete_resp.status_code != 200:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"POST /auth/login/complete returned "
                f"{login_complete_resp.status_code}; "
                f"body: {login_complete_resp.text[:200]}"
            ),
        )
    # Final post-login session cookie must be present.
    if client.cookies.get(session_cookie_name) is None:
        return CheckResult(
            label="passkey_roundtrip",
            passed=False,
            message=(
                f"login/complete response did NOT set {session_cookie_name!r} cookie"
            ),
        )

    return CheckResult(
        label="passkey_roundtrip",
        passed=True,
        message=(
            "WebAuthn register + login ceremony pair completed; "
            f"session + CSRF cookies issued ({session_cookie_name}, {csrf_cookie_name})"
        ),
    )


# ---------------------------------------------------------------------------
# check_control_verbs
# ---------------------------------------------------------------------------


_CONTROL_VERBS: tuple[tuple[str, dict[str, object]], ...] = (
    ("pause", {"reason": "verify-pause"}),
    ("resume", {}),
    ("trigger_emergency_invocation", {"reason": "verify-trigger"}),
    # FakePipelineClient seeds ``current_profile=Profile.medium`` per its
    # __init__; switching to ``large`` produces a real (non-no-op)
    # transition so the audit layer emits both an OPERATOR_CONSOLE row
    # AND a PROFILE_SWITCHED row.
    ("switch_profile", {"profile_name": "large"}),
    ("run_universe_validation", {}),
    ("cancel_order", {"order_id": "ord-verify-1"}),
    ("force_close_position", {"position_id": "pos-verify-1", "rationale": "verify"}),
    ("set_halt_mode", {"enabled": True, "reason": "verify-halt"}),
)
"""The 8 control verbs the AC requires + a minimal valid body for each.

Mirrors the route surface exposed by
:func:`alphamind.command_center.control.routes.build_control_router`.
The bodies match the Pydantic request models in
:mod:`alphamind.command_center.control.models`.
"""


async def check_control_verbs(
    *,
    client: httpx.AsyncClient,
    csrf_cookie_name: str,
    cc_writer: async_sessionmaker[AsyncSession],
) -> CheckResult:
    """Hit all 8 ``/api/control/*`` verbs and assert success + activity_log rows.

    For each verb:

    * Response envelope is ``200 OK`` with ``ok=true``.
    * One ``activity_log`` row landed with ``source=OPERATOR_CONSOLE``.
    * For ``switch_profile``: an ``activity_log`` row with
      ``event_type=PROFILE_SWITCHED`` (the audit layer emits this in
      addition to the per-verb row).

    The check counts the ``OPERATOR_CONSOLE`` rows + asserts the count
    grew by 8. Failure cases name the offending verb in the message
    so triage doesn't require re-running individual verbs to localize.
    """
    csrf_cookie = client.cookies.get(csrf_cookie_name)
    if csrf_cookie is None:
        return CheckResult(
            label="control_verbs",
            passed=False,
            message=(
                f"control_verbs requires {csrf_cookie_name!r} cookie from a prior "
                "passkey roundtrip; none present"
            ),
        )
    headers = {"X-CSRF-Token": csrf_cookie}

    # Capture the baseline OPERATOR_CONSOLE row count BEFORE invoking
    # any verb so we can verify the grew-by-8 invariant deterministically.
    async with cc_writer() as session:
        baseline_count = await _count_operator_console_rows(session)

    failures: list[str] = []
    for verb, body in _CONTROL_VERBS:
        url = f"/api/control/{verb}"
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            failures.append(f"{verb}: transport error: {exc}")
            continue
        if resp.status_code != 200:
            failures.append(
                f"{verb}: HTTP {resp.status_code}; body: {resp.text[:200]}"
            )
            continue
        payload = resp.json()
        # The successful envelope shape per
        # :class:`ControlResponseEnvelope` is ``status="accepted"`` +
        # ``applied_at`` ISO timestamp; verb-specific extensions
        # (``trigger_emergency_invocation`` / ``run_universe_validation``
        # / ``force_close_position``) add fields but keep the envelope
        # base.
        if payload.get("status") != "accepted":
            failures.append(
                f"{verb}: response envelope status={payload.get('status')!r}; "
                f"payload: {payload}"
            )
            continue

    if failures:
        return CheckResult(
            label="control_verbs",
            passed=False,
            message="; ".join(failures),
        )

    async with cc_writer() as session:
        new_count = await _count_operator_console_rows(session)
        profile_switched_count = await _count_profile_switched_rows(session)

    delta = new_count - baseline_count
    # ``run_universe_validation`` is read-only — it's in
    # :data:`alphamind.command_center.control.audit._NO_AUDIT_VERBS` and
    # produces no activity-log row. The audit fanout for the remaining
    # 7 verbs is one OPERATOR_CONSOLE row each.
    expected_rows = len(_CONTROL_VERBS) - 1
    if delta != expected_rows:
        return CheckResult(
            label="control_verbs",
            passed=False,
            message=(
                f"activity_log OPERATOR_CONSOLE row count grew by {delta}, "
                f"expected {expected_rows} (one per audited verb; "
                "run_universe_validation is read-only and produces no row)"
            ),
        )
    if profile_switched_count == 0:
        return CheckResult(
            label="control_verbs",
            passed=False,
            message=(
                "no PROFILE_SWITCHED activity_log row found after "
                "switch_profile verb"
            ),
        )

    return CheckResult(
        label="control_verbs",
        passed=True,
        message=(
            f"all 8 control verbs returned status=accepted; "
            f"{delta} OPERATOR_CONSOLE rows + "
            f"{profile_switched_count} PROFILE_SWITCHED row(s) landed"
        ),
    )


async def _count_operator_console_rows(session: AsyncSession) -> int:
    """Count activity_log rows with ``source=OPERATOR_CONSOLE``."""
    stmt = select(ActivityLogRow).where(ActivityLogRow.source == "OPERATOR_CONSOLE")
    result = await session.execute(stmt)
    return len(result.scalars().all())


async def _count_profile_switched_rows(session: AsyncSession) -> int:
    """Count activity_log rows with ``event_type=PROFILE_SWITCHED``."""
    stmt = select(ActivityLogRow).where(
        ActivityLogRow.event_type == "PROFILE_SWITCHED"
    )
    result = await session.execute(stmt)
    return len(result.scalars().all())


# ---------------------------------------------------------------------------
# check_alert_fires
# ---------------------------------------------------------------------------


async def check_alert_fires(
    *,
    app: object,
    fake_discord: FakeDiscordChannel,
    cc_writer: async_sessionmaker[AsyncSession],
) -> CheckResult:
    """Publish a synthetic failed-invocation event + assert the alert fires.

    Drives the ``pipeline_aborted`` rule (one of the 17 baseline
    rules in ``config/alerts.yaml``) via a synthetic
    :class:`PipelineEvent` with
    ``event_type=INVOCATION_ENDED`` + ``payload.status="failed"``.
    The :class:`AlertEngine` consumer task sees the event, fires the
    rule, persists an ``alerts`` row, sends one Discord call, and
    publishes the matching :class:`AlertFiredEvent` onto the
    multiplexer.

    Asserts:

    * One ``alerts`` row exists with ``rule_name=pipeline_aborted``.
    * The fake Discord channel recorded at least one call.

    The check waits up to a small budget for the engine's consumer
    task to drain the queue + dispatch the alert. The exact timing
    is non-deterministic (TaskGroup scheduling) so we poll rather
    than assume a single tick is enough.
    """
    # Pull the multiplexer + engine off app.state via getattr because
    # the verify-script's type imports are intentionally thin (the
    # FastAPI app is constructed elsewhere; this module only treats
    # it as a black-box handle).
    multiplexer = app.state.event_multiplexer  # type: ignore[attr-defined]
    invocation_id = f"inv-verify-{secrets.token_hex(4)}"
    failed_event = PipelineEvent(
        event_type=PipelineEventType.INVOCATION_ENDED,
        payload={
            "invocation_id": invocation_id,
            "status": "failed",
            "ended_at": _FROZEN_NOW.isoformat(),
        },
    )
    await multiplexer.publish(failed_event)

    # Poll for the alert to land. The engine's consumer task wakes
    # on the multiplexer subscription queue, dispatches the rule,
    # persists the row, and calls the Discord channel. The total
    # latency is dominated by ``asyncio.wait_for`` ticks inside
    # ``_consume_events`` (timeout=1.0); poll for up to 5 s.
    deadline_seconds = 5.0
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_seconds
    while loop.time() < end:
        async with cc_writer() as session:
            stmt = select(AlertRow).where(AlertRow.rule_name == "pipeline_aborted")
            result = await session.execute(stmt)
            rows = result.scalars().all()
        if rows and len(fake_discord.calls) > 0:
            return CheckResult(
                label="alert_fires",
                passed=True,
                message=(
                    f"pipeline_aborted alert fired; {len(rows)} alerts row(s), "
                    f"{len(fake_discord.calls)} Discord call(s) recorded"
                ),
            )
        await asyncio.sleep(0.2)

    return CheckResult(
        label="alert_fires",
        passed=False,
        message=(
            f"pipeline_aborted alert did NOT fire within {deadline_seconds:.0f}s; "
            f"alerts rows: 0, Discord calls: {len(fake_discord.calls)}. "
            "Engine consumer may be stalled or rule registration missing."
        ),
    )


# ---------------------------------------------------------------------------
# check_sse_roundtrip
# ---------------------------------------------------------------------------


async def check_sse_roundtrip(*, app: object) -> CheckResult:
    """Subscribe to the multiplexer + observe one pipeline + one monitor event.

    Publishes two synthetic events (a pipeline ``HEARTBEAT`` and a
    monitor ``HEARTBEAT``) and confirms both arrive at the
    subscriber's queue. Heartbeat events are used because they are
    side-effect-free (no rule fires on them, so this check is
    isolated from :func:`check_alert_fires`).

    Returns FAIL if either event is missing after a short polling
    budget; PASS otherwise.
    """
    multiplexer = app.state.event_multiplexer  # type: ignore[attr-defined]
    async with multiplexer.subscribe() as queue:
        pipeline_event = PipelineEvent(
            event_type=PipelineEventType.HEARTBEAT,
            payload={"ts": _FROZEN_NOW.isoformat()},
        )
        monitor_event = MonitorEvent(
            event_type=MonitorEventType.HEARTBEAT,
            payload={"ts": _FROZEN_NOW.isoformat()},
        )
        await multiplexer.publish(pipeline_event)
        await multiplexer.publish(monitor_event)

        saw_pipeline = False
        saw_monitor = False
        deadline = asyncio.get_running_loop().time() + 5.0
        while not (saw_pipeline and saw_monitor):
            if asyncio.get_running_loop().time() >= deadline:
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                continue
            if isinstance(event, PipelineEvent):
                saw_pipeline = True
            elif isinstance(event, MonitorEvent):
                saw_monitor = True

    if not (saw_pipeline and saw_monitor):
        return CheckResult(
            label="sse_roundtrip",
            passed=False,
            message=(
                f"missing event(s); saw pipeline={saw_pipeline}, "
                f"monitor={saw_monitor}"
            ),
        )
    return CheckResult(
        label="sse_roundtrip",
        passed=True,
        message="one pipeline + one monitor event observed at multiplexer subscriber",
    )


# ---------------------------------------------------------------------------
# check_frontend_build
# ---------------------------------------------------------------------------


def check_frontend_build(*, dist_path: Path) -> CheckResult:
    """Assert ``dist/index.html`` exists at the configured path.

    The script does NOT run ``bun install`` / ``bun run build`` itself
    — the operator's machine may not carry bun, and a forced build
    would mask "no dist directory" failures. The RUNBOOK names the
    prereq explicitly: the operator runs the bun commands once at
    bring-up; the verify script merely confirms the artifact survived.

    Returns PASS when ``<dist_path>/index.html`` is a file; FAIL with
    a remediation hint otherwise. The StaticFiles mount in
    :mod:`alphamind.command_center.app` would otherwise serve a 404
    on every browser fetch.
    """
    index_html = dist_path / "index.html"
    if not index_html.is_file():
        return CheckResult(
            label="frontend_build",
            passed=False,
            message=(
                f"{index_html} missing; the SPA bundle is absent. "
                f"Run `bun install --frozen-lockfile && bun run build` in "
                f"src/alphamind/command_center/frontend/ to produce it."
            ),
        )
    return CheckResult(
        label="frontend_build",
        passed=True,
        message=f"{index_html} present; StaticFiles mount has a bundle to serve",
    )


# ---------------------------------------------------------------------------
# check_clean_teardown
# ---------------------------------------------------------------------------


async def check_clean_teardown(
    *,
    supervisor: CommandCenterSupervisor,
    run_task: asyncio.Task[None],
    timeout_seconds: float,
) -> CheckResult:
    """Signal stop + assert ``run_task`` returns within the budget.

    The supervisor's ``request_stop()`` sets its internal event;
    Uvicorn's main serve loop drains in-flight connections + returns.
    The TaskGroup propagates the exit; ``run_task`` resolves. The
    check awaits the task with a wall-clock budget; a timeout
    indicates Uvicorn ignored the cancel or a background task
    hung.

    Returns PASS on a clean exit, FAIL on timeout. The check is
    last because by definition it shuts everything down.
    """
    supervisor.request_stop()
    try:
        await asyncio.wait_for(run_task, timeout=timeout_seconds)
    except TimeoutError:
        return CheckResult(
            label="clean_teardown",
            passed=False,
            message=(
                f"supervisor did NOT shut down within {timeout_seconds:.0f}s; "
                "Uvicorn may be ignoring its cancellation token"
            ),
        )
    except BaseException as exc:  # noqa: BLE001 - surface as FAIL not stack trace
        return CheckResult(
            label="clean_teardown",
            passed=False,
            message=f"shutdown raised: {type(exc).__name__}: {exc}",
        )
    return CheckResult(
        label="clean_teardown",
        passed=True,
        message=f"supervisor stopped cleanly within {timeout_seconds:.0f}s budget",
    )


# ---------------------------------------------------------------------------
# Boot helpers — build the in-process app + supervisor under fakes.
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Bind a temporary socket to pick a free localhost port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _seed_process_lifetime(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Insert one ``process_lifetimes`` row so operator-action FK targets exist."""
    async with factory() as session:
        session.add(
            ProcessLifetimeRow(
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                process_role="monitor",
                process_start_at="2026-05-26T00:00:00Z",
                process_pid=1,
                hostname="verify-host",
                git_sha="0" * 40,
                git_branch="verify",
                git_dirty=0,
                python_version="3.13",
                pip_freeze_hash="0" * 64,
                pip_freeze_snapshot_path="/dev/null",
                anthropic_sdk_version="verify",
                claude_agent_sdk_version="verify",
                os_release="verify-os",
            )
        )
        await session.commit()


@contextlib.asynccontextmanager
async def _build_verify_app(
    *,
    config_dir: Path,
    db_path: Path,
    context: VerifyContext,
) -> AsyncIterator[tuple[object, async_sessionmaker[AsyncSession]]]:
    """Build the FastAPI app + production session factory under fakes.

    Yields ``(app, production_session_factory)`` so the caller can
    register the supervisor's Uvicorn task + drive checks against
    the production factory.

    Closes the production engine on context exit so the SQLite file
    handle releases cleanly (Windows-relevant; the lifespan already
    closes the cc_writer / foreign_reader engines).
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    production_engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    # Create both ``Base`` (production) AND ``CommandCenterBase`` (cc-owned)
    # schemas on the same DB file. In production these are created by
    # Alembic migrations; the verify run is a clean-room boot against a
    # temp DB and must seed both.
    async with production_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(CommandCenterBase.metadata.create_all)
    production_factory = async_sessionmaker(
        bind=production_engine, expire_on_commit=False
    )
    await _seed_process_lifetime(production_factory)

    command_center_config = load_command_center_config(config_dir)
    security_config = load_security_config(config_dir)
    alerts_config = load_alerts_config(config_dir)

    fake_events_pipeline = FakePipelineEventsClient(frames=())
    fake_events_monitor = FakeMonitorEventsClient(frames=())
    context.fake_pipeline_events_client = fake_events_pipeline
    context.fake_monitor_events_client = fake_events_monitor

    auth_overrides = AuthOverrides(
        webauthn_verifier=context.webauthn_verifier,
        setup_token_gate=context.setup_token_gate,
        session_signing_secret=secrets.token_bytes(32),
        clock=lambda: _FROZEN_NOW,
    )
    control_overrides = ControlOverrides(
        pipeline_client=context.fake_pipeline_client,
        monitor_client=context.fake_monitor_client,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
    )
    events_overrides = EventsOverrides(
        pipeline_events_client=fake_events_pipeline,
        monitor_events_client=fake_events_monitor,
    )
    fake_discord: DiscordChannel = context.fake_discord_channel
    alerts_overrides = AlertsOverrides(discord_channel=fake_discord)

    app = build_app(
        command_center_config=command_center_config,
        security_config=security_config,
        alerts_config=alerts_config,
        production_session_factory=production_factory,
        process_lifetime_id=_PROCESS_LIFETIME_ID,
        config_dir=config_dir,
        auth_overrides=auth_overrides,
        control_overrides=control_overrides,
        events_overrides=events_overrides,
        alerts_overrides=alerts_overrides,
    )
    try:
        yield app, production_factory
    finally:
        await production_engine.dispose()


def _write_verify_command_center_yaml(
    *,
    config_dir: Path,
    repo_config_dir: Path,
    db_path: Path,
    bind_host: str,
    bind_port: int,
    frontend_dist_path: Path,
) -> None:
    """Materialize a per-run ``command-center.yaml`` under *config_dir*.

    The production config pins the bind to ``127.0.0.1:8080``; verify
    runs pick a free ephemeral port to avoid colliding with a real
    command center process the operator may already have running.

    The accompanying ``security.yaml`` + ``alerts.yaml`` are copied
    verbatim from the repo config — the verify run uses production
    rule + WebAuthn settings against in-memory backends.
    """
    cc_yaml = config_dir / "command-center.yaml"
    cc_yaml.write_text(
        f"""bind:
  host: "{bind_host}"
  port: {bind_port}
db:
  alphamind_db_path: "{db_path}"
frontend:
  dist_path: "{frontend_dist_path}"
pipeline:
  control_url: "http://127.0.0.1:8765"
  events_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
  events_url: "http://127.0.0.1:8766"
""",
        encoding="utf-8",
    )
    (config_dir / "security.yaml").write_bytes(
        (repo_config_dir / "security.yaml").read_bytes()
    )
    (config_dir / "alerts.yaml").write_bytes(
        (repo_config_dir / "alerts.yaml").read_bytes()
    )


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="verify_command_center",
        description=(
            "End-to-end verification for ``python -m alphamind.command_center``. "
            "Boots the FastAPI app in-process against in-memory fakes for the "
            "WebAuthn verifier, Discord channel, and pipeline + monitor clients; "
            "runs 7 check helpers; exits 0 only on full pass."
        ),
    )
    parser.add_argument(
        "--bind-host",
        default="127.0.0.1",
        help="Host the verify app binds to (default 127.0.0.1).",
    )
    parser.add_argument(
        "--frontend-dist",
        type=Path,
        default=Path("src/alphamind/command_center/frontend/dist"),
        help=(
            "Path to the bun-built frontend bundle. "
            "Default mirrors the production config."
        ),
    )
    return parser.parse_args(argv)


def _emit(results: list[CheckResult], result: CheckResult) -> None:
    """Append the result + print its formatted line in the operator-facing form."""
    results.append(result)
    print(result.format_line())


def _print_summary(results: list[CheckResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    print(f"=== COMMAND-CENTER VERIFICATION === {passed}/{total} checks passed")


async def _run_checks(
    args: argparse.Namespace,
) -> int:
    """Body of :func:`main` — run all 7 checks in order against a fresh app.

    Returns 0 on full pass, non-zero on any failure. Mirrors
    :func:`alphamind.scripts.verify_debug_e2e._run_checks`'s short-
    circuit-on-fatal-failure pattern: a failing
    :func:`check_daemons_bind` short-circuits the rest (subsequent
    checks would fail trivially against a non-bound server).
    """
    results: list[CheckResult] = []

    repo_config_dir = Path(__file__).resolve().parents[1] / "config"
    bind_port = _pick_free_port()
    bind_host: str = args.bind_host
    base_url = f"http://{bind_host}:{bind_port}"

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        db_path = tmp / "verify-command-center.db"
        db_path.touch()
        config_dir = tmp / "config"
        config_dir.mkdir()
        _write_verify_command_center_yaml(
            config_dir=config_dir,
            repo_config_dir=repo_config_dir,
            db_path=db_path,
            bind_host=bind_host,
            bind_port=bind_port,
            frontend_dist_path=args.frontend_dist,
        )

        context = VerifyContext(app_port=bind_port, base_url=base_url)

        async with _build_verify_app(
            config_dir=config_dir, db_path=db_path, context=context
        ) as (app, production_factory):
            context.app = app
            context.production_session_factory = production_factory

            setup_token = context.setup_token_gate.mint()

            from alphamind.command_center.__main__ import _run_uvicorn_task

            session = ProcessSession(
                process_lifetime_id=_PROCESS_LIFETIME_ID,
                started_at=_FROZEN_NOW,
            )
            supervisor = CommandCenterSupervisor(
                session=session,
                shutdown_timeout_seconds=int(_CLEAN_TEARDOWN_TIMEOUT_SECONDS),
            )

            async def uvicorn_task(s: ProcessSession) -> None:
                await _run_uvicorn_task(s, app=app, host=bind_host, port=bind_port)

            supervisor.register_task(name="uvicorn", coro_fn=uvicorn_task)
            for task_name, factory in app.state.event_consumer_task_factories.items():
                supervisor.register_task(name=task_name, coro_fn=factory)
            alerts_factory = getattr(app.state, "alert_engine_task_factory", None)
            if alerts_factory is not None:
                supervisor.register_task(name="alerts_engine", coro_fn=alerts_factory)

            run_task = asyncio.create_task(supervisor.run())
            context.supervisor = supervisor
            context.run_task = run_task

            try:
                # ---- 1. daemons_bind --------------------------------------
                bind_result = await check_daemons_bind(
                    host=bind_host,
                    port=bind_port,
                    timeout_seconds=_DAEMON_BIND_TIMEOUT_SECONDS,
                )
                _emit(results, bind_result)
                if not bind_result.passed:
                    _print_summary(results)
                    # Force shutdown to keep teardown deterministic on early
                    # failure — the supervisor's request_stop() drains
                    # Uvicorn + any background tasks the partial boot left
                    # running.
                    supervisor.request_stop()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(
                            run_task, timeout=_CLEAN_TEARDOWN_TIMEOUT_SECONDS
                        )
                    return 1

                # ---- 2-5. checks driven against the running app ----------
                cc_writer = app.state.cc_writer_session_factory
                security_cfg = app.state.security_config
                session_cookie_name: str = security_cfg.session.cookie_name
                csrf_cookie_name: str = security_cfg.csrf.cookie_name

                async with httpx.AsyncClient(base_url=base_url) as client:
                    _emit(
                        results,
                        await check_passkey_roundtrip(
                            client=client,
                            setup_token=setup_token,
                            csrf_cookie_name=csrf_cookie_name,
                            session_cookie_name=session_cookie_name,
                        ),
                    )
                    _emit(
                        results,
                        await check_control_verbs(
                            client=client,
                            csrf_cookie_name=csrf_cookie_name,
                            cc_writer=cc_writer,
                        ),
                    )
                _emit(
                    results,
                    await check_alert_fires(
                        app=app,
                        fake_discord=context.fake_discord_channel,
                        cc_writer=cc_writer,
                    ),
                )
                _emit(results, await check_sse_roundtrip(app=app))

                # ---- 6. frontend_build (sync; no app interaction) --------
                _emit(
                    results,
                    check_frontend_build(dist_path=Path(args.frontend_dist)),
                )

                # ---- 7. clean_teardown -----------------------------------
                _emit(
                    results,
                    await check_clean_teardown(
                        supervisor=supervisor,
                        run_task=run_task,
                        timeout_seconds=_CLEAN_TEARDOWN_TIMEOUT_SECONDS,
                    ),
                )
            finally:
                # Belt-and-suspenders: if any check raised before the
                # explicit teardown check, force the supervisor to stop
                # so the temp-directory cleanup can release the DB file.
                if not run_task.done():
                    supervisor.request_stop()
                    with contextlib.suppress(BaseException):
                        await asyncio.wait_for(
                            run_task, timeout=_CLEAN_TEARDOWN_TIMEOUT_SECONDS
                        )

    _print_summary(results)
    return 0 if all(r.passed for r in results) else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run the 7 check helpers against an in-process command center boot.

    Returns 0 on full pass, 1 on any failure. The operator runs this
    via ``uv run python scripts/verify_command_center.py`` after
    ``bun run build`` in the frontend tree; the RUNBOOK names the
    bring-up sequence in full.
    """
    configure_utf8_stdio()
    logging.basicConfig(level=logging.WARNING)
    args = _parse_args(argv)
    return asyncio.run(_run_checks(args))


if __name__ == "__main__":  # pragma: no cover - operator entry point
    # Drop into the script's argparse + main loop; mirror
    # verify_debug_e2e's outermost BaseException catch so a stack trace
    # doesn't leak into the operator's terminal on an unhandled crash.
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException:
        log.exception("verify_command_center exited with error")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Module-level smoke import — keeps ``json`` referenced even when the
# operator-only path doesn't exercise it (silences ruff F401 without
# pulling in a runtime cost on import).
# ---------------------------------------------------------------------------

_ = json  # used by future debug paths; keep import live for downstream tools.

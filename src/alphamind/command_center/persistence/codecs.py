"""Row ↔ frozen-dataclass codecs for the three command-center tables.

The dataclasses are the type downstream code (alert engine, auth flow)
holds; the rows are the storage shape. Adapters are pure and zero-side-
effect — the boundary between the ORM rows and the rest of the package
(parse-don't-validate, python-architecture §D2). Vocabulary enums are
StrEnums declared here; the ``_*_record_from_row`` adapters validate the
enum value at the boundary so a direct-SQL writer that bypassed the
schema CHECK surfaces as :class:`ValueError` rather than silently
flowing through.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from alphamind.command_center._kernel.ids import (
    AlertId,
    AlertRuleName,
    OperatorSessionId,
    WebauthnCredentialId,
    alert_id,
    alert_rule_name,
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.persistence.tables import (
    AlertRow,
    OperatorSessionRow,
    WebauthnCredentialRow,
)

__all__ = [
    "AlertRecord",
    "AlertSeverity",
    "AlertStatus",
    "OperatorSessionRecord",
    "WebauthnCredentialRecord",
    "alert_record_from_row",
    "alert_record_to_row",
    "operator_session_record_from_row",
    "operator_session_record_to_row",
    "webauthn_credential_record_from_row",
    "webauthn_credential_record_to_row",
]


class AlertSeverity(StrEnum):
    """Alert severity tier vocabulary.

    Per :doc:`docs/design/command-center.md` § Alerting — Severity tiers.
    The tier drives the notification channels (critical + important fire
    the Discord webhook; operational is in-app only) and the dashboard
    banner color.
    """

    CRITICAL = "critical"
    IMPORTANT = "important"
    OPERATIONAL = "operational"


class AlertStatus(StrEnum):
    """Alert status vocabulary.

    Per :doc:`docs/design/command-center.md` § Acknowledge and snooze.
    A fired alert is ``firing`` until the operator acknowledges or
    snoozes it. A snoozed alert whose underlying condition cleared and
    re-armed during the snooze re-fires as soon as the window expires —
    that re-firing inserts a new ``alerts`` row (rather than mutating
    this one) so the alert history is append-only on the rule's
    firing events.
    """

    FIRING = "firing"
    ACKNOWLEDGED = "acknowledged"
    SNOOZED = "snoozed"


# ---------------------------------------------------------------------------
# AlertRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AlertRecord:
    """Frozen typed handle for an ``alerts`` row.

    Mirrors :class:`AlertRow` field-for-field. The ``severity`` and
    ``status`` fields are :class:`AlertSeverity` / :class:`AlertStatus`
    StrEnum values; the ``alert_id`` and ``rule_name`` fields carry the
    NewType-typed identifier aliases from
    :mod:`alphamind.command_center._kernel.ids`.
    """

    alert_id: AlertId
    rule_name: AlertRuleName
    severity: AlertSeverity
    status: AlertStatus
    fired_at: str
    acknowledged_at: str | None
    snoozed_until: str | None
    context_json: str


def alert_record_to_row(record: AlertRecord) -> AlertRow:
    """Build an :class:`AlertRow` from an :class:`AlertRecord`."""
    return AlertRow(
        alert_id=record.alert_id,
        rule_name=record.rule_name,
        severity=record.severity.value,
        status=record.status.value,
        fired_at=record.fired_at,
        acknowledged_at=record.acknowledged_at,
        snoozed_until=record.snoozed_until,
        context_json=record.context_json,
    )


def alert_record_from_row(row: AlertRow) -> AlertRecord:
    """Build an :class:`AlertRecord` from an :class:`AlertRow`.

    Validates the ``severity`` and ``status`` enum vocabularies at the
    boundary so a direct-SQL writer that bypassed the schema CHECK
    surfaces as :class:`ValueError` instead of silently flowing through.
    """
    return AlertRecord(
        alert_id=alert_id(row.alert_id),
        rule_name=alert_rule_name(row.rule_name),
        severity=_enum_member(AlertSeverity, row.severity, field="severity"),
        status=_enum_member(AlertStatus, row.status, field="status"),
        fired_at=row.fired_at,
        acknowledged_at=row.acknowledged_at,
        snoozed_until=row.snoozed_until,
        context_json=row.context_json,
    )


# ---------------------------------------------------------------------------
# WebauthnCredentialRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WebauthnCredentialRecord:
    """Frozen typed handle for a ``webauthn_credentials`` row.

    The ``public_key`` field carries the CBOR-encoded public key from the
    WebAuthn registration ceremony as a base64url-encoded string. The
    relying-party logic (story 03) decodes it on demand for assertion
    verification.
    """

    credential_id: WebauthnCredentialId
    public_key: str
    sign_count: int
    transports: str
    created_at: str


def webauthn_credential_record_to_row(
    record: WebauthnCredentialRecord,
) -> WebauthnCredentialRow:
    """Build a :class:`WebauthnCredentialRow` from a record."""
    return WebauthnCredentialRow(
        credential_id=record.credential_id,
        public_key=record.public_key,
        sign_count=record.sign_count,
        transports=record.transports,
        created_at=record.created_at,
    )


def webauthn_credential_record_from_row(
    row: WebauthnCredentialRow,
) -> WebauthnCredentialRecord:
    """Build a record from a :class:`WebauthnCredentialRow`."""
    return WebauthnCredentialRecord(
        credential_id=webauthn_credential_id(row.credential_id),
        public_key=row.public_key,
        sign_count=row.sign_count,
        transports=row.transports,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# OperatorSessionRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OperatorSessionRecord:
    """Frozen typed handle for an ``operator_sessions`` row.

    The ``csrf_token_hash`` is the SHA-256 of the raw CSRF token; the
    raw token lives only in the signed cookie the browser holds. The
    issuance path (story 03) writes the hash here so a DB compromise
    doesn't yield session tokens directly.
    """

    session_id: OperatorSessionId
    credential_id: WebauthnCredentialId
    expires_at: str
    csrf_token_hash: str
    created_at: str


def operator_session_record_to_row(record: OperatorSessionRecord) -> OperatorSessionRow:
    """Build an :class:`OperatorSessionRow` from a record."""
    return OperatorSessionRow(
        session_id=record.session_id,
        credential_id=record.credential_id,
        expires_at=record.expires_at,
        csrf_token_hash=record.csrf_token_hash,
        created_at=record.created_at,
    )


def operator_session_record_from_row(row: OperatorSessionRow) -> OperatorSessionRecord:
    """Build a record from an :class:`OperatorSessionRow`."""
    return OperatorSessionRecord(
        session_id=operator_session_id(row.session_id),
        credential_id=webauthn_credential_id(row.credential_id),
        expires_at=row.expires_at,
        csrf_token_hash=row.csrf_token_hash,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Vocabulary helpers
# ---------------------------------------------------------------------------


def _enum_member[E: StrEnum](enum_cls: type[E], value: str, *, field: str) -> E:
    """Coerce ``value`` to a member of *enum_cls* or raise :class:`ValueError`.

    Mirrors the pattern in :func:`alphamind.state.invocation_context.records._assert_member`:
    every read-path adapter validates its enum vocabulary at the boundary
    so a direct-SQL writer that bypassed the schema CHECK surfaces here
    rather than silently flowing through.
    """
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = sorted(m.value for m in enum_cls)
        msg = f"unknown {field}={value!r} (expected one of {valid})"
        raise ValueError(msg) from exc

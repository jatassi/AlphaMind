"""Tests for ``command_center.persistence.codecs`` (story 02 / ALP-666).

Round-trip the three row ↔ frozen-dataclass converters: the dataclass is
the type downstream code holds; the row is the storage shape. The
adapters are pure (no I/O) so the tests are simple value comparisons.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from alphamind.command_center._kernel.ids import (
    AlertRuleName,
    DiscordWebhookUrl,
    OperatorSessionId,
    WebauthnCredentialId,
    alert_id,
    alert_rule_name,
    discord_webhook_url,
    operator_session_id,
    webauthn_credential_id,
)
from alphamind.command_center.persistence.codecs import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    OperatorSessionRecord,
    WebauthnCredentialRecord,
    alert_record_from_row,
    alert_record_to_row,
    operator_session_record_from_row,
    operator_session_record_to_row,
    webauthn_credential_record_from_row,
    webauthn_credential_record_to_row,
)
from alphamind.command_center.persistence.tables import (
    AlertRow,
    OperatorSessionRow,
    WebauthnCredentialRow,
)


class TestAlertSeverity:
    def test_has_three_documented_members(self) -> None:
        # Per docs/design/command-center.md § Alerting — Severity tiers.
        members = {m.value for m in AlertSeverity}
        assert members == {"critical", "important", "operational"}


class TestAlertStatus:
    def test_has_documented_members(self) -> None:
        # Per design doc § Acknowledge and snooze — an alert is either
        # firing (active), acknowledged, or snoozed. (Snoozed alerts that
        # re-arm after the snooze window go back to firing.)
        members = {m.value for m in AlertStatus}
        assert members == {"firing", "acknowledged", "snoozed"}


class TestAlertRecord:
    def test_round_trip_through_row(self) -> None:
        record = AlertRecord(
            alert_id=alert_id("alert-001"),
            rule_name=alert_rule_name("pipeline_aborted"),
            severity=AlertSeverity.CRITICAL,
            status=AlertStatus.FIRING,
            fired_at="2026-05-26T00:00:00Z",
            acknowledged_at=None,
            snoozed_until=None,
            context_json='{"invocation_id": "inv-1"}',
        )
        row = alert_record_to_row(record)
        roundtrip = alert_record_from_row(row)
        assert roundtrip == record

    def test_record_is_frozen(self) -> None:
        record = AlertRecord(
            alert_id=alert_id("alert-001"),
            rule_name=alert_rule_name("pipeline_aborted"),
            severity=AlertSeverity.CRITICAL,
            status=AlertStatus.FIRING,
            fired_at="2026-05-26T00:00:00Z",
            acknowledged_at=None,
            snoozed_until=None,
            context_json="{}",
        )
        with pytest.raises(FrozenInstanceError):
            record.status = AlertStatus.ACKNOWLEDGED  # type: ignore[misc]

    def test_round_trip_carries_optional_timestamps(self) -> None:
        record = AlertRecord(
            alert_id=alert_id("alert-001"),
            rule_name=alert_rule_name("schedule_miss"),
            severity=AlertSeverity.IMPORTANT,
            status=AlertStatus.SNOOZED,
            fired_at="2026-05-26T00:00:00Z",
            acknowledged_at="2026-05-26T00:00:10Z",
            snoozed_until="2026-05-26T01:00:00Z",
            context_json='{"trigger": "pre_open"}',
        )
        row = alert_record_to_row(record)
        roundtrip = alert_record_from_row(row)
        assert roundtrip == record

    def test_unknown_severity_on_row_raises(self) -> None:
        # The read path defends against a direct-SQL writer that bypassed
        # the schema CHECK — an unknown severity surfaces as ValueError
        # at the boundary instead of silently flowing through.
        row = AlertRow(
            alert_id="alert-001",
            rule_name="pipeline_aborted",
            severity="nuclear",  # not in the StrEnum
            status="firing",
            fired_at="2026-05-26T00:00:00Z",
            acknowledged_at=None,
            snoozed_until=None,
            context_json="{}",
        )
        with pytest.raises(ValueError, match="severity"):
            alert_record_from_row(row)


class TestWebauthnCredentialRecord:
    def test_round_trip_through_row(self) -> None:
        record = WebauthnCredentialRecord(
            credential_id=webauthn_credential_id("ABCDef-_0123"),
            public_key="cbor-encoded-key-bytes-base64url",
            sign_count=42,
            transports="usb,nfc",
            created_at="2026-05-26T00:00:00Z",
        )
        row = webauthn_credential_record_to_row(record)
        roundtrip = webauthn_credential_record_from_row(row)
        assert roundtrip == record


class TestOperatorSessionRecord:
    def test_round_trip_through_row(self) -> None:
        record = OperatorSessionRecord(
            session_id=operator_session_id("session-abc"),
            credential_id=webauthn_credential_id("ABCDef-_0123"),
            expires_at="2026-05-26T12:00:00Z",
            csrf_token_hash="0" * 64,
            created_at="2026-05-26T00:00:00Z",
        )
        row = operator_session_record_to_row(record)
        roundtrip = operator_session_record_from_row(row)
        assert roundtrip == record


class TestNewtypeAliasesUsedInTests:
    """Smoke: the NewType-typed inputs the test fixtures construct survive
    re-import."""

    def test_aliases_referenced(self) -> None:
        assert AlertRuleName is not None
        assert DiscordWebhookUrl is not None
        assert OperatorSessionId is not None
        assert WebauthnCredentialId is not None
        # discord_webhook_url is referenced by the validation test downstream;
        # keep an explicit construction here so the import isn't unused.
        url = discord_webhook_url("https://discord.com/api/webhooks/1/abc")
        assert url == "https://discord.com/api/webhooks/1/abc"

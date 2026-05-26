"""Tests for ``command_center._kernel.ids`` (story 02 / ALP-666).

Covers the NewType aliases the command-center package exposes for typed
identifiers + the validated constructors for the patterned values
(``WebauthnCredentialId`` is base64url; ``DiscordWebhookUrl`` is an https
URL targeting Discord).
"""

from __future__ import annotations

import pytest

from alphamind.command_center._kernel.ids import (
    AlertId,
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


class TestWebauthnCredentialId:
    def test_accepts_base64url_string(self) -> None:
        # base64url uses A-Z, a-z, 0-9, -, _ with no padding.
        value = webauthn_credential_id("ABCDef-_0123456789")
        assert isinstance(value, str)
        assert value == "ABCDef-_0123456789"

    def test_rejects_empty_string(self) -> None:
        with pytest.raises(ValueError, match="webauthn_credential_id"):
            webauthn_credential_id("")

    def test_rejects_standard_base64_padding(self) -> None:
        # base64url does NOT use '+' / '/' / '=' — those are standard base64.
        with pytest.raises(ValueError, match="webauthn_credential_id"):
            webauthn_credential_id("AB+CD/EF==")

    def test_rejects_whitespace(self) -> None:
        with pytest.raises(ValueError, match="webauthn_credential_id"):
            webauthn_credential_id("AB CD")


class TestDiscordWebhookUrl:
    def test_accepts_discord_webhook_url(self) -> None:
        url = discord_webhook_url("https://discord.com/api/webhooks/123/abc")
        assert url == "https://discord.com/api/webhooks/123/abc"

    def test_accepts_discordapp_legacy_host(self) -> None:
        # discordapp.com is the legacy host name; both resolve to the same
        # webhook surface.
        url = discord_webhook_url("https://discordapp.com/api/webhooks/123/abc")
        assert url == "https://discordapp.com/api/webhooks/123/abc"

    def test_rejects_http_scheme(self) -> None:
        with pytest.raises(ValueError, match="discord_webhook_url"):
            discord_webhook_url("http://discord.com/api/webhooks/123/abc")

    def test_rejects_non_discord_host(self) -> None:
        with pytest.raises(ValueError, match="discord_webhook_url"):
            discord_webhook_url("https://example.com/api/webhooks/123/abc")


class TestOperatorSessionId:
    def test_accepts_uuid_like_string(self) -> None:
        value = operator_session_id("abc-123")
        assert value == "abc-123"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="operator_session_id"):
            operator_session_id("")


class TestAlertId:
    def test_accepts_nonempty_string(self) -> None:
        value = alert_id("alert-2026-05-26-001")
        assert value == "alert-2026-05-26-001"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="alert_id"):
            alert_id("")


class TestAlertRuleName:
    def test_accepts_snake_case_rule_name(self) -> None:
        value = alert_rule_name("pipeline_aborted")
        assert value == "pipeline_aborted"

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="alert_rule_name"):
            alert_rule_name("")


class TestNewTypesAreDistinct:
    """NewType isn't a runtime check, but the module exports must exist.

    A negative test of mypy distinctness is out of scope for pytest; verify
    the symbols are importable and assignable to ``str`` at runtime.
    """

    def test_all_aliases_importable(self) -> None:
        # If any symbol is missing the import at the top of the file fails
        # collection; this assertion just keeps the imports referenced so
        # the linter doesn't flag unused names.
        assert AlertId is not None
        assert AlertRuleName is not None
        assert DiscordWebhookUrl is not None
        assert OperatorSessionId is not None
        assert WebauthnCredentialId is not None

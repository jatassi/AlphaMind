"""Tests for :mod:`alphamind.command_center.alerts.config` (story 05a / ALP-671)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from alphamind.command_center.alerts.config import (
    bind_rules_from_config,
    resolve_discord_webhook,
)
from alphamind.command_center.alerts.rules import AlertSeverity
from alphamind.command_center.config import (
    AlertsChannels,
    AlertsConfig,
    DiscordChannelConfig,
    load_alerts_config,
)

_CONFIG_DIR = Path(__file__).parents[3] / "config"


# ---------------------------------------------------------------------------
# bind_rules_from_config
# ---------------------------------------------------------------------------


class TestBindRulesFromConfig:
    def test_yaml_overrides_apply(self) -> None:
        config = AlertsConfig(
            rules=[
                {
                    "name": "pipeline_aborted",
                    "severity": "critical",
                    "debounce_minutes": 99,
                    "channels": ["in_app"],
                },
            ],
            channels=AlertsChannels(
                discord=DiscordChannelConfig(webhook_url_env="ALPHAMIND_DISCORD_WEBHOOK")
            ),
        )
        rules = bind_rules_from_config(config)
        match = next(r for r in rules if str(r.name) == "pipeline_aborted")
        assert match.debounce_window == timedelta(minutes=99)
        assert match.channels == ("in_app",)
        assert match.severity == AlertSeverity.CRITICAL

    def test_unknown_yaml_rule_name_raises(self) -> None:
        config = AlertsConfig(
            rules=[
                {
                    "name": "not_a_real_rule",
                    "severity": "critical",
                    "debounce_minutes": 1,
                    "channels": ["in_app"],
                },
            ],
            channels=AlertsChannels(
                discord=DiscordChannelConfig(webhook_url_env="ALPHAMIND_DISCORD_WEBHOOK")
            ),
        )
        with pytest.raises(ValueError, match="not_a_real_rule"):
            bind_rules_from_config(config)

    def test_missing_yaml_rule_uses_default(self) -> None:
        config = AlertsConfig(
            rules=[],
            channels=AlertsChannels(
                discord=DiscordChannelConfig(webhook_url_env="ALPHAMIND_DISCORD_WEBHOOK")
            ),
        )
        rules = bind_rules_from_config(config)
        assert len(rules) == 17

    def test_extra_keys_in_yaml_row_rejected(self) -> None:
        # Story 06b typed ``AlertsConfig.rules`` as ``list[AlertRuleSpec]``
        # (was ``list[dict[str, object]]``), so the rejection moves from
        # :func:`bind_rules_from_config` (which used to re-validate via
        # :class:`AlertRuleYaml`) to :class:`AlertsConfig` construction
        # itself — the loader's ``extra='forbid'`` posture rejects the
        # row before bind ever sees it.
        payload = {
            "rules": [
                {
                    "name": "pipeline_aborted",
                    "severity": "critical",
                    "debounce_minutes": 5,
                    "channels": ["in_app"],
                    "unknown_key": "value",
                },
            ],
            "channels": {
                "discord": {"webhook_url_env": "ALPHAMIND_DISCORD_WEBHOOK"},
            },
        }
        with pytest.raises(Exception, match="unknown_key"):
            AlertsConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# resolve_discord_webhook
# ---------------------------------------------------------------------------


class TestResolveDiscordWebhook:
    def test_env_var_present_returns_url(self) -> None:
        config = AlertsConfig(
            rules=[],
            channels=AlertsChannels(discord=DiscordChannelConfig(webhook_url_env="MY_WEBHOOK_VAR")),
        )
        env = {
            "MY_WEBHOOK_VAR": "https://discord.com/api/webhooks/123/abc-def_token-123",
        }
        result = resolve_discord_webhook(config, env=env)
        assert result.env_name == "MY_WEBHOOK_VAR"
        assert result.webhook_url is not None

    def test_env_var_absent_returns_none(self) -> None:
        config = AlertsConfig(
            rules=[],
            channels=AlertsChannels(discord=DiscordChannelConfig(webhook_url_env="MISSING_VAR")),
        )
        result = resolve_discord_webhook(config, env={})
        assert result.webhook_url is None

    def test_invalid_url_raises_at_resolution(self) -> None:
        config = AlertsConfig(
            rules=[],
            channels=AlertsChannels(discord=DiscordChannelConfig(webhook_url_env="BAD_VAR")),
        )
        env = {"BAD_VAR": "http://notdiscord.example/bad"}
        with pytest.raises(ValueError):
            resolve_discord_webhook(config, env=env)


# ---------------------------------------------------------------------------
# config/alerts.yaml shape end-to-end.
# ---------------------------------------------------------------------------


class TestConfigAlertsYamlShape:
    """Loads the live ``config/alerts.yaml`` to confirm shape + 17 rule names."""

    def test_alerts_yaml_loads_with_17_rules(self) -> None:
        config = load_alerts_config(_CONFIG_DIR)
        assert len(config.rules) == 17

    def test_alerts_yaml_binds_to_all_17_rules(self) -> None:
        config = load_alerts_config(_CONFIG_DIR)
        rules = bind_rules_from_config(config)
        assert len(rules) == 17
        names = {str(r.name) for r in rules}
        # Spot-check a few key rule names.
        assert "pipeline_aborted" in names
        assert "monitor_websocket_disconnected" in names
        assert "agent_malformed_output" in names
        assert "thesis_resolved" in names

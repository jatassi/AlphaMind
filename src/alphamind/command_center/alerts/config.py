"""Bind ``config/alerts.yaml`` to the engine's :class:`AlertRule` list.

Story 05a / ALP-671 — the YAML loader (story 02) ships ``AlertsConfig``
carrying ``rules: list[dict[str, object]]``; this module is the
per-rule typed schema + the :func:`bind_rules_from_config` helper the
composition root uses to render the YAML's per-rule overrides onto the
:func:`build_default_rules` factory.

Per-rule YAML row shape:

::

    - name: pipeline_aborted
      severity: critical | important | operational
      debounce_minutes: 15
      channels: [in_app, discord]

* ``name`` MUST match one of the 17 default rule names. The 17 names
  are derived from :func:`build_default_rules`'s output so the YAML can
  never reference a name the engine doesn't know about.
* ``severity`` MUST match a :class:`AlertSeverity` member.
* ``debounce_minutes`` MUST be a positive integer; rendered into a
  :class:`timedelta`.
* ``channels`` MUST be a list of strings; the engine accepts
  ``"in_app"`` and ``"discord"`` in v1.

Unknown rule names raise :class:`ValueError` so a typo in YAML fails
loud at startup. Missing rules (a YAML that defines only a subset)
fall through to the :func:`build_default_rules` defaults — the YAML
acts as an overlay, not a wholesale replacement.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from alphamind.command_center._kernel.ids import (
    AlertRuleName,
    DiscordWebhookUrl,
    alert_rule_name,
    discord_webhook_url,
)
from alphamind.command_center.alerts.conditions import build_default_rules
from alphamind.command_center.alerts.rules import AlertRule, AlertSeverity
from alphamind.command_center.config import AlertsConfig

__all__ = [
    "AlertRuleYaml",
    "DiscordWiringResult",
    "bind_rules_from_config",
    "resolve_discord_webhook",
]


class AlertRuleYaml(BaseModel):
    """Per-rule YAML row schema (Pydantic boundary)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    severity: AlertSeverity
    debounce_minutes: int = Field(ge=1)
    channels: list[str] = Field(min_length=1)


def bind_rules_from_config(
    config: AlertsConfig,
    *,
    data_dir: Path | None = None,
    data_directory_threshold_bytes: int = 50 * 1024 * 1024 * 1024,
    data_directory_min_free_bytes: int = 0,
    capital_range_low_usd: float = 0.0,
    capital_range_high_usd: float = 0.0,
) -> tuple[AlertRule, ...]:
    """Render the YAML's per-rule overrides onto :func:`build_default_rules`.

    Steps:

    1. Validate every YAML rule row through :class:`AlertRuleYaml`.
    2. Build the per-rule overrides mappings (debounce / severity /
       channels) keyed by :class:`AlertRuleName`.
    3. Call :func:`build_default_rules` with the overrides — the factory
       owns the predicate identities, the YAML only owns tunables.
    4. Reject unknown rule names — every YAML entry MUST map onto one
       of the 17 default rule names.
    """
    validated = tuple(AlertRuleYaml.model_validate(row) for row in config.rules)
    debounce_overrides: dict[AlertRuleName, timedelta] = {}
    severity_overrides: dict[AlertRuleName, AlertSeverity] = {}
    channel_overrides: dict[AlertRuleName, tuple[str, ...]] = {}
    for row in validated:
        name = alert_rule_name(row.name)
        debounce_overrides[name] = timedelta(minutes=row.debounce_minutes)
        severity_overrides[name] = row.severity
        channel_overrides[name] = tuple(row.channels)
    rules = build_default_rules(
        data_dir=data_dir,
        data_directory_threshold_bytes=data_directory_threshold_bytes,
        data_directory_min_free_bytes=data_directory_min_free_bytes,
        capital_range_low_usd=capital_range_low_usd,
        capital_range_high_usd=capital_range_high_usd,
        debounce_overrides=debounce_overrides,
        severity_overrides=severity_overrides,
        channel_overrides=channel_overrides,
    )
    # Check every YAML entry names a known rule.
    known: set[str] = {str(r.name) for r in rules}
    yaml_names = {row.name for row in validated}
    unknown = yaml_names - known
    if unknown:
        msg = (
            f"alerts.yaml carries unknown rule names {sorted(unknown)!r}; "
            f"known rule names are {sorted(known)!r}"
        )
        raise ValueError(msg)
    return rules


def _is_truthy(value: str | None) -> bool:
    """Return True when *value* names a non-empty configured webhook URL."""
    return bool(value and value.strip())


class DiscordWiringResult:
    """The resolved Discord webhook URL + the env-var name it came from."""

    __slots__ = ("env_name", "webhook_url")

    def __init__(self, *, env_name: str, webhook_url: DiscordWebhookUrl | None) -> None:
        self.env_name = env_name
        self.webhook_url = webhook_url

    def __repr__(self) -> str:  # pragma: no cover — defensive
        configured = self.webhook_url is not None
        return f"DiscordWiringResult(env_name={self.env_name!r}, configured={configured})"


def resolve_discord_webhook(
    config: AlertsConfig,
    *,
    env: Mapping[str, str] | None = None,
) -> DiscordWiringResult:
    """Resolve the Discord webhook URL from the env var named in YAML.

    Reads ``config.channels.discord.webhook_url_env`` and looks it up
    in the provided environment mapping (``os.environ`` by default).
    Returns a :class:`DiscordWiringResult` carrying the parsed-validated
    URL when present, or ``None`` when the env var is unset / empty —
    the channel then logs WARNING + skips at send time.

    Invalid URLs (failing the
    :func:`alphamind.command_center._kernel.ids.discord_webhook_url`
    boundary check) raise :class:`ValueError` so a misconfigured webhook
    fails loud at startup rather than silently dropping every alert
    fan-out at runtime.
    """
    source_env = env if env is not None else os.environ
    env_name = config.channels.discord.webhook_url_env
    raw = source_env.get(env_name, "").strip()
    if not _is_truthy(raw):
        return DiscordWiringResult(env_name=env_name, webhook_url=None)
    return DiscordWiringResult(
        env_name=env_name,
        webhook_url=discord_webhook_url(raw),
    )

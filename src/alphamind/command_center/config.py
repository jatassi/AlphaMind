"""Pydantic config models for the command center (story 02 / ALP-666).

Three models map 1:1 to three YAML files under ``config/``:

* :class:`CommandCenterConfig` ← ``config/command-center.yaml`` —
  bind host / port + DB path + frontend dist path.
* :class:`SecurityConfig` ← ``config/security.yaml`` — session cookie /
  CSRF cookie / WebAuthn relying-party settings.
* :class:`AlertsConfig` ← ``config/alerts.yaml`` — alert rule list +
  notification channel registry. Story 05a populates the rule list;
  story 02 ships an empty list as the loader-validation seam.

All models use ``ConfigDict(extra='forbid', frozen=True)`` so:

* A typo in YAML fails loud at load time (``extra='forbid'``) rather
  than silently flowing through as an ignored key — pinned in the
  parent issue's scope ("Each YAML maps 1:1 to a Pydantic v2 model
  with extra='forbid'").
* The loaded config can't be mutated downstream (``frozen=True``);
  passes through the entire process unchanged.

The three loader functions are paired with the three models:
``load_command_center_config`` / ``load_security_config`` /
``load_alerts_config``. Each reads its respective file from the given
config directory and returns a validated model. Missing files raise
``FileNotFoundError`` so the daemon fails closed at startup rather than
running with a half-loaded config.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from alphamind.config.loaders import read_yaml_file


class ReloadPolicy(Enum):
    """Per-field reload classification — drives the config editor badge.

    Attached to a Pydantic model field via :class:`typing.Annotated` so the
    metadata travels with the type hint and is extractable by the form-
    schema endpoint (:mod:`alphamind.command_center.views.configuration`).

    * :attr:`INVOCATION_TIME` — picked up at the next scheduled trigger;
      the resolver re-reads the entire YAML tree before each invocation.
      Default for every field that does not carry an explicit annotation.
    * :attr:`DEPLOY_TIME` — requires process restart; covers paths in
      ``main.yaml``, SQLite pragmas set at connection open, Python/
      package versions, the ``.env`` file location, and the bind
      socket (Uvicorn binds at startup).
    """

    INVOCATION_TIME = "invocation_time"
    DEPLOY_TIME = "deploy_time"


class ControlHint(Enum):
    """Per-field UI-control hint for the config editor form schema.

    Annotates a field via :class:`typing.Annotated` so the form-schema
    derivation in :mod:`alphamind.command_center.views.configuration` can
    pick the right per-control component (cron picker, path input, …)
    even for fields whose Python type is plain ``str``. The structural
    classifier (bool → boolean, int → number, list → array) handles the
    type-driven cases; this enum covers the semantic refinements.
    """

    PATH = "path"
    CRON = "cron"


__all__ = [
    "AlertRuleSpec",
    "AlertsChannels",
    "AlertsConfig",
    "BindConfig",
    "CommandCenterConfig",
    "ControlHint",
    "DbConfig",
    "DiscordChannelConfig",
    "FrontendConfig",
    "MonitorUpstreamConfig",
    "PipelineUpstreamConfig",
    "ReloadPolicy",
    "SecurityConfig",
    "SessionConfig",
    "WebauthnConfig",
    "load_alerts_config",
    "load_command_center_config",
    "load_security_config",
]


# ---------------------------------------------------------------------------
# CommandCenterConfig
# ---------------------------------------------------------------------------


class BindConfig(BaseModel):
    """``bind`` block — host + port the Uvicorn server listens on.

    ``host`` is ``127.0.0.1`` in v1 (loopback-only per the parent issue's
    pre-resolved B); the operator can switch to a LAN IP for off-machine
    access by editing the YAML. The VPS Caddy + WireGuard remote-access
    wiring is deferred to a separate operator-handover follow-on per
    pre-resolved B.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)


class DbConfig(BaseModel):
    """``db`` block — path to ``alphamind.db``.

    ``alphamind_db_path`` carries the raw path string with ``%USERPROFILE%``
    substitution; the engine-construction path
    (``alphamind.persistence.session._resolve_path``) resolves the
    variable expansion when opening the DB.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    alphamind_db_path: Annotated[str, ReloadPolicy.DEPLOY_TIME, ControlHint.PATH] = Field(
        min_length=1
    )


class FrontendConfig(BaseModel):
    """``frontend`` block — path to the Vite-built ``dist/`` directory.

    FastAPI's ``StaticFiles`` mount serves this directory at ``/`` per
    the parent issue's pre-resolved N. The path is relative to the repo
    root in dev; in production the install-time
    ``bun install && bun run build`` step produces the directory in
    place.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    dist_path: Annotated[str, ReloadPolicy.DEPLOY_TIME, ControlHint.PATH] = Field(min_length=1)


class PipelineUpstreamConfig(BaseModel):
    """``pipeline`` block — loopback URLs of the pipeline upstream surfaces.

    Combines the two pipeline-facing URLs the command center forwards
    to:

    * ``control_url`` — story 04a (ALP-668). The /api/control proxy's
      httpx-backed :class:`PipelineClient` targets this URL for the
      five pipeline verbs.
    * ``events_url`` — story 04b (ALP-669). The SSE multiplexer's
      upstream consumer subscribes to this URL.

    Both default to ``http://127.0.0.1:8765`` per
    ``config/scheduler.yaml`` § ``control_port``; the config keeps the
    two URLs distinct so the verb proxy and the SSE multiplexer can be
    redirected independently if the upstream ever splits.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    control_url: str = Field(min_length=1)
    events_url: str = Field(min_length=1)


class MonitorUpstreamConfig(BaseModel):
    """``monitor`` block — loopback URLs of the monitor upstream surfaces.

    Mirrors :class:`PipelineUpstreamConfig`'s shape. ``control_url``
    (story 04a / ALP-668) routes the three monitor verbs; ``events_url``
    (story 04b / ALP-669) feeds the SSE multiplexer. Both default to
    ``http://127.0.0.1:8766`` per ``config/continuous_monitor.yaml``
    § ``control_port``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    control_url: str = Field(min_length=1)
    events_url: str = Field(min_length=1)


class CommandCenterConfig(BaseModel):
    """Top-level ``command-center.yaml`` model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ``bind`` is deploy-time: the Uvicorn socket binds at startup so
    # mid-flight edits do not rebind. The path-bearing ``db`` /
    # ``frontend`` sub-fields carry their own DEPLOY_TIME annotations.
    bind: Annotated[BindConfig, ReloadPolicy.DEPLOY_TIME]
    db: DbConfig
    frontend: FrontendConfig
    pipeline: PipelineUpstreamConfig
    monitor: MonitorUpstreamConfig


def load_command_center_config(config_dir: Path) -> CommandCenterConfig:
    """Load ``config_dir/command-center.yaml`` into a validated model.

    Raises :class:`FileNotFoundError` if the file is missing so the
    daemon fails closed at startup. ``extra='forbid'`` ensures a typo
    in the YAML surfaces as :class:`pydantic.ValidationError` rather
    than silently flowing through.
    """
    payload = read_yaml_file(config_dir / "command-center.yaml")
    return CommandCenterConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# SecurityConfig
# ---------------------------------------------------------------------------


class SessionConfig(BaseModel):
    """``session`` block — session-cookie shape.

    ``duration_hours`` caps a single session's lifetime; the operator
    re-authenticates with a passkey when the session expires. No
    "remember me" beyond the session lifetime per the design doc.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_hours: int = Field(ge=1)
    cookie_name: str = Field(min_length=1)


class CsrfConfig(BaseModel):
    """``csrf`` block — CSRF-cookie name.

    The cookie carries the raw CSRF token; the DB carries the SHA-256
    hash on the corresponding ``operator_sessions`` row (story 03's
    issuance flow handles the hash storage).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cookie_name: str = Field(min_length=1)


class WebauthnConfig(BaseModel):
    """``webauthn`` block — relying-party settings.

    The relying-party ID is the hostname the operator authenticates
    against (``localhost`` for v1 loopback; ``commandcenter.atassi.org``
    after the remote-access follow-on lands). The relying-party name
    is the user-facing string the browser's passkey UI displays.

    ``relying_party_id`` is DEPLOY_TIME — the WebAuthn verifier is
    constructed at lifespan startup against this hostname; an in-flight
    edit only takes effect after a process restart (already-issued
    passkeys remain bound to the previous hostname).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    relying_party_id: Annotated[str, ReloadPolicy.DEPLOY_TIME] = Field(min_length=1)
    relying_party_name: str = Field(min_length=1)


class SecurityConfig(BaseModel):
    """Top-level ``security.yaml`` model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session: SessionConfig
    csrf: CsrfConfig
    webauthn: WebauthnConfig


def load_security_config(config_dir: Path) -> SecurityConfig:
    """Load ``config_dir/security.yaml`` into a validated model."""
    payload = read_yaml_file(config_dir / "security.yaml")
    return SecurityConfig.model_validate(payload)


# ---------------------------------------------------------------------------
# AlertsConfig
# ---------------------------------------------------------------------------


class DiscordChannelConfig(BaseModel):
    """``channels.discord`` block — Discord webhook channel settings.

    ``webhook_url_env`` is the .env variable name carrying the actual
    webhook URL; the secret never lands in YAML so a YAML commit can
    never leak it. The alert-engine wiring (story 05a) reads the env
    var at engine-construction time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    webhook_url_env: str = Field(min_length=1)


class AlertsChannels(BaseModel):
    """``channels`` block — notification channel registry.

    Story 05a populates this with the Discord channel per parent
    issue's pre-resolved F. Additional channels (email, SMS) are out
    of scope for v1 per the design doc.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    discord: DiscordChannelConfig


class AlertRuleSpec(BaseModel):
    """Per-rule YAML row — the typed schema the config editor surfaces.

    Mirrors :class:`alphamind.command_center.alerts.config.AlertRuleYaml`
    (story 05a's per-rule validation surface). Hoisting the model here
    lets :class:`AlertsConfig.rules` carry a typed ``list[AlertRuleSpec]``
    rather than ``list[dict[str, object]]``, which in turn lets the
    config-editor framework's ``_element_columns`` walker derive typed
    columns for the rules table (one column per field below).

    The four fields match the YAML row shape:

    * ``name`` — names one of the 17 default rule names (validated as
      a member of the registry at engine construction in
      :func:`alphamind.command_center.alerts.config.bind_rules_from_config`).
    * ``severity`` — Critical / Important / Operational tier.
    * ``debounce_minutes`` — positive integer; rendered into a
      :class:`datetime.timedelta` by the engine.
    * ``channels`` — list of channel names; ``in_app`` and ``discord``
      are accepted in v1.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    severity: str = Field(min_length=1)
    debounce_minutes: int = Field(ge=1)
    channels: list[str] = Field(min_length=1)


class AlertsConfig(BaseModel):
    """Top-level ``alerts.yaml`` model.

    The ``rules`` list carries the operator-edited overlay over the 17
    default rules (story 05a populates the shipped values). Typed as
    ``list[AlertRuleSpec]`` so the config editor's
    :class:`ObjectArrayTableEditor` receives typed column metadata
    from the schema endpoint.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: list[AlertRuleSpec] = Field(default_factory=list)
    channels: AlertsChannels


def load_alerts_config(config_dir: Path) -> AlertsConfig:
    """Load ``config_dir/alerts.yaml`` into a validated model.

    The ``rules`` list is intentionally untyped at this story (story
    05a defines the per-rule Pydantic model); the alerts engine
    re-validates the list against the typed schema at engine-
    construction time.
    """
    payload = read_yaml_file(config_dir / "alerts.yaml")
    return AlertsConfig.model_validate(payload)

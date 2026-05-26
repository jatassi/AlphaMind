"""Tests for the config editor framework (story 05i / ALP-679).

Three layers:

* :class:`ReloadPolicy` enum + :func:`reload_policy_of` extraction from
  ``Annotated`` Pydantic field hints — applied to ``CommandCenterConfig``,
  ``SecurityConfig``, ``AlertsConfig`` per acceptance criterion.
* ``GET /api/views/config/schema/{config_file}`` — form-schema metadata
  derived from the file's Pydantic model + reload-policy decorators.
* ``PUT /api/views/config/{config_file}`` — atomic file write via
  ``_kernel/atomic_io`` after all three validation layers pass.

Scoped pytest: ``uv run pytest tests/command_center/views/ -n auto``.
"""

from __future__ import annotations

from alphamind.command_center.views.configuration import (
    ReloadPolicy,
    reload_policy_of,
)


class TestReloadPolicyEnum:
    def test_has_invocation_time_and_deploy_time_members(self) -> None:
        # Two policies per the design — invocation-time-reload (most fields)
        # and deploy-time-only (paths in main.yaml, SQLite pragmas, package
        # versions, .env location). The badge UI dispatches on the enum.
        assert ReloadPolicy.INVOCATION_TIME != ReloadPolicy.DEPLOY_TIME

    def test_invocation_time_default_extracted_when_no_annotation(self) -> None:
        from alphamind.command_center.config import SessionConfig

        # SessionConfig.duration_hours carries no ReloadPolicy annotation in
        # the type hint by default; the extractor returns INVOCATION_TIME
        # because every YAML knob reloads at next invocation unless
        # explicitly marked deploy-time-only. This test pins the default
        # behavior on a field that has no explicit annotation.
        policy = reload_policy_of(SessionConfig, "duration_hours")
        assert policy is ReloadPolicy.INVOCATION_TIME

    def test_deploy_time_extracted_when_annotated(self) -> None:
        from alphamind.command_center.config import (
            CommandCenterConfig,
            DbConfig,
            FrontendConfig,
        )

        # Per docs/design/configuration-management.md § Runtime vs. deploy-
        # time classification, "paths in main.yaml" are deploy-time-only:
        # the daemon resolves them at startup, so an in-flight edit only
        # takes effect after a process restart.
        assert reload_policy_of(DbConfig, "alphamind_db_path") is ReloadPolicy.DEPLOY_TIME
        assert reload_policy_of(FrontendConfig, "dist_path") is ReloadPolicy.DEPLOY_TIME
        # bind.host + bind.port are deploy-time because the Uvicorn server
        # binds to them at startup; changing the YAML mid-flight does not
        # rebind the socket.
        assert reload_policy_of(CommandCenterConfig, "bind") is ReloadPolicy.DEPLOY_TIME

    def test_session_duration_invocation_time(self) -> None:
        from alphamind.command_center.config import SessionConfig

        # Session cookie duration is an invocation-time knob: existing
        # sessions are unaffected, but new sessions issued after the
        # next config reload pick up the new value.
        assert reload_policy_of(SessionConfig, "duration_hours") is ReloadPolicy.INVOCATION_TIME

    def test_alerts_rules_is_invocation_time(self) -> None:
        from alphamind.command_center.config import AlertsConfig

        # Alert rule edits reload at next invocation per the design.
        assert reload_policy_of(AlertsConfig, "rules") is ReloadPolicy.INVOCATION_TIME

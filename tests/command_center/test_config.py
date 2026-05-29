"""Tests for ``command_center.config`` (story 02 / ALP-666).

The three Pydantic configuration models — ``CommandCenterConfig`` /
``SecurityConfig`` / ``AlertsConfig`` — load the three YAML files
(``command-center.yaml`` / ``security.yaml`` / ``alerts.yaml``) with
``extra='forbid'`` so a typo in the YAML fails loud at load time.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_type_hints

import pytest
import yaml
from pydantic import ValidationError

from alphamind.command_center.config import (
    AccessConfig,
    AlertsConfig,
    CommandCenterConfig,
    ReloadPolicy,
    SecurityConfig,
    load_alerts_config,
    load_command_center_config,
    load_security_config,
)


@pytest.fixture
def config_dir() -> Path:
    # The repo's ``config/`` directory carries the production yaml files;
    # the loader tests round-trip through the shipped files so a fresh
    # checkout's YAML is a valid input to the loader.
    return Path(__file__).parents[2] / "config"


class TestCommandCenterConfig:
    def test_loads_shipped_yaml(self, config_dir: Path) -> None:
        config = load_command_center_config(config_dir)
        assert isinstance(config, CommandCenterConfig)

    def test_bind_host_and_port_are_present(self, config_dir: Path) -> None:
        config = load_command_center_config(config_dir)
        assert config.bind.host == "127.0.0.1"
        assert config.bind.port == 8080

    def test_db_alphamind_db_path_present(self, config_dir: Path) -> None:
        config = load_command_center_config(config_dir)
        # The shipped value uses %USERPROFILE% substitution; the loader
        # carries the raw string and lets the engine-construction path
        # resolve it (matches the alphamind.persistence.session
        # _resolve_path convention).
        assert "%USERPROFILE%" in config.db.alphamind_db_path

    def test_frontend_dist_path_present(self, config_dir: Path) -> None:
        config = load_command_center_config(config_dir)
        assert config.frontend.dist_path

    def test_pipeline_and_monitor_control_urls_present(self, config_dir: Path) -> None:
        config = load_command_center_config(config_dir)
        # Shipped defaults pin the upstream control surfaces' loopback URLs
        # (ALP-664 / ALP-665). Operators may switch the ports by editing
        # the YAML; the model only validates non-empty strings.
        assert config.pipeline.control_url == "http://127.0.0.1:8765"
        assert config.monitor.control_url == "http://127.0.0.1:8766"

    def test_pipeline_and_monitor_events_urls_present(self, config_dir: Path) -> None:
        """Story 04b adds events_url alongside 04a's control_url."""
        config = load_command_center_config(config_dir)
        assert config.pipeline.events_url.startswith("http://127.0.0.1:")
        assert config.monitor.events_url.startswith("http://127.0.0.1:")

    def test_extra_field_in_yaml_rejected(self, tmp_path: Path) -> None:
        bad = {
            "bind": {"host": "127.0.0.1", "port": 8080},
            "db": {"alphamind_db_path": "/tmp/x.db"},
            "frontend": {"dist_path": "/dist"},
            "pipeline": {
                "control_url": "http://127.0.0.1:8765",
                "events_url": "http://127.0.0.1:8765",
            },
            "monitor": {
                "control_url": "http://127.0.0.1:8766",
                "events_url": "http://127.0.0.1:8766",
            },
            "bogus_top_level_key": True,
        }
        bad_yaml = tmp_path / "command-center.yaml"
        bad_yaml.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(ValidationError, match="bogus_top_level_key"):
            load_command_center_config(tmp_path)


class TestSecurityConfig:
    def test_loads_shipped_yaml(self, config_dir: Path) -> None:
        config = load_security_config(config_dir)
        assert isinstance(config, SecurityConfig)

    def test_session_cookie_name_present(self, config_dir: Path) -> None:
        config = load_security_config(config_dir)
        assert config.session.cookie_name == "cc_session"
        assert config.session.duration_hours == 12

    def test_csrf_cookie_name_present(self, config_dir: Path) -> None:
        config = load_security_config(config_dir)
        assert config.csrf.cookie_name == "cc_csrf"

    def test_webauthn_rp_id_and_name(self, config_dir: Path) -> None:
        config = load_security_config(config_dir)
        assert config.webauthn.relying_party_id == "localhost"
        assert config.webauthn.relying_party_name == "AlphaMind Command Center"

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        bad = {
            "session": {"duration_hours": 12, "cookie_name": "x"},
            "csrf": {"cookie_name": "x"},
            "webauthn": {
                "relying_party_id": "localhost",
                "relying_party_name": "AlphaMind",
            },
            "bogus": True,
        }
        bad_yaml = tmp_path / "security.yaml"
        bad_yaml.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(ValidationError, match="bogus"):
            load_security_config(tmp_path)


class TestAlertsConfig:
    def test_loads_shipped_yaml(self, config_dir: Path) -> None:
        config = load_alerts_config(config_dir)
        assert isinstance(config, AlertsConfig)

    def test_rules_populated_with_default_set(self, config_dir: Path) -> None:
        config = load_alerts_config(config_dir)
        # Story 05a populated the rule list with the 17 default rules; ALP-739
        # added entry_no_fill as the 18th. Story 06b typed the rows as
        # :class:`AlertRuleSpec`; access via attribute rather than the previous
        # ``.get('name')`` dict path.
        assert len(config.rules) == 18
        names = [row.name for row in config.rules]
        assert "pipeline_aborted" in names
        assert "monitor_websocket_disconnected" in names
        assert "thesis_resolved" in names
        assert "entry_no_fill" in names

    def test_discord_webhook_url_env_present(self, config_dir: Path) -> None:
        config = load_alerts_config(config_dir)
        assert config.channels.discord.webhook_url_env == "ALPHAMIND_DISCORD_WEBHOOK"

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        bad = {
            "rules": [],
            "channels": {"discord": {"webhook_url_env": "X"}},
            "bogus": True,
        }
        bad_yaml = tmp_path / "alerts.yaml"
        bad_yaml.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(ValidationError, match="bogus"):
            load_alerts_config(tmp_path)


class TestLoadersFailLoudWhenFileMissing:
    def test_command_center_loader_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"command-center\.yaml"):
            load_command_center_config(tmp_path)

    def test_security_loader_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"security\.yaml"):
            load_security_config(tmp_path)

    def test_alerts_loader_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match=r"alerts\.yaml"):
            load_alerts_config(tmp_path)


# ---------------------------------------------------------------------------
# ALP-725: AccessConfig + cookies_secure tests (TDD red phase first)
# ---------------------------------------------------------------------------


class TestAccessConfig:
    """Tests for the new access: block (ALP-725)."""

    def test_access_config_model_exists_with_fields_and_forbid(self) -> None:
        # AC1: model exists with scheme, host, port, extra='forbid'
        acc = AccessConfig(scheme="http", host="alphamind.local", port=8080)
        assert acc.scheme == "http"
        assert acc.host == "alphamind.local"
        assert acc.port == 8080

        # frozen (direct mutation raises; model_copy creates new so does not prove it)
        with pytest.raises(ValidationError, match="frozen_instance"):
            acc.host = "other"

        # extra forbid
        with pytest.raises(
            ValidationError, match=r"Extra inputs are not permitted|extra_forbidden|bogus"
        ):
            AccessConfig(scheme="http", host="h", port=1, bogus=42)  # type: ignore[call-arg]

    def test_access_config_defaults_for_omitted_port(self) -> None:
        # port omitted inside block -> None (scheme default semantics)
        acc = AccessConfig(scheme="https", host="example.com")
        assert acc.port is None

    def test_access_config_port_none_explicit(self) -> None:
        acc = AccessConfig(scheme="http", host="localhost", port=None)
        assert acc.port is None

    def test_command_center_config_includes_access_with_deploy_time(self) -> None:
        # AC2 + AC6: field present with DEPLOY_TIME metadata
        # Manual construct of full minimal (required fields)
        minimal = {
            "bind": {"host": "127.0.0.1", "port": 8080},
            "db": {"alphamind_db_path": "/tmp/x.db"},
            "frontend": {"dist_path": "/dist"},
            "pipeline": {
                "control_url": "http://127.0.0.1:8765",
                "events_url": "http://127.0.0.1:8765",
            },
            "monitor": {
                "control_url": "http://127.0.0.1:8766",
                "events_url": "http://127.0.0.1:8766",
            },
            "access": {"scheme": "http", "host": "alphamind.local", "port": 8080},
        }
        cfg = CommandCenterConfig.model_validate(minimal)
        assert cfg.access.host == "alphamind.local"
        assert cfg.access.port == 8080
        # frozen at top level too (direct setattr)
        with pytest.raises(ValidationError, match="frozen_instance"):
            cfg.bind = cfg.bind

    def test_access_field_has_deploy_time_metadata(self) -> None:
        # AC6: metadata extractable (matches how config editor badges work)
        hints = get_type_hints(CommandCenterConfig, include_extras=True)
        access_hint = hints["access"]
        metadata = getattr(access_hint, "__metadata__", ())
        assert ReloadPolicy.DEPLOY_TIME in metadata

    def test_load_cc_succeeds_on_old_style_omitted_access(self, config_dir: Path) -> None:
        # AC4: old-style (no access key) still loads cleanly with sensible default
        cfg = load_command_center_config(config_dir)
        assert isinstance(cfg.access, AccessConfig)
        # sensible localhost-derived for zero-config v1 installs
        assert cfg.access.scheme == "http"
        assert cfg.access.host == "localhost"
        assert cfg.access.port == 8080

    def test_load_cc_succeeds_on_new_style_lan_access(self, tmp_path: Path) -> None:
        # AC4: new-style LAN YAMLs load
        cc_content = """\
bind:
  host: "192.168.1.42"
  port: 8080
db:
  alphamind_db_path: "/tmp/db.db"
frontend:
  dist_path: "dist"
pipeline:
  control_url: "http://127.0.0.1:8765"
  events_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
  events_url: "http://127.0.0.1:8766"
access:
  scheme: "http"
  host: "alphamind.local"
  port: 8080
"""
        (tmp_path / "command-center.yaml").write_text(cc_content, encoding="utf-8")
        cfg = load_command_center_config(tmp_path)
        assert cfg.access.scheme == "http"
        assert cfg.access.host == "alphamind.local"
        assert cfg.access.port == 8080

    def test_load_cc_with_access_port_null_http(self, tmp_path: Path) -> None:
        # ALP-730 (03d): edge case port omitted (null) in access block for LAN http
        cc_content = """\
bind:
  host: "192.168.1.99"
  port: 8080
db:
  alphamind_db_path: "/tmp/db.db"
frontend:
  dist_path: "dist"
pipeline:
  control_url: "http://127.0.0.1:8765"
  events_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
  events_url: "http://127.0.0.1:8766"
access:
  scheme: "http"
  host: "lan.example"
"""
        (tmp_path / "command-center.yaml").write_text(cc_content, encoding="utf-8")
        cfg = load_command_center_config(tmp_path)
        assert cfg.access.scheme == "http"
        assert cfg.access.host == "lan.example"
        assert cfg.access.port is None

    def test_load_cc_with_access_port_null_https(self, tmp_path: Path) -> None:
        # ALP-730 (03d): edge case port null + https (resolver will pick 443)
        cc_content = """\
bind:
  host: "10.0.0.50"
  port: 8443
db:
  alphamind_db_path: "/tmp/db.db"
frontend:
  dist_path: "dist"
pipeline:
  control_url: "http://127.0.0.1:8765"
  events_url: "http://127.0.0.1:8765"
monitor:
  control_url: "http://127.0.0.1:8766"
  events_url: "http://127.0.0.1:8766"
access:
  scheme: "https"
  host: "secure.lan"
"""
        (tmp_path / "command-center.yaml").write_text(cc_content, encoding="utf-8")
        cfg = load_command_center_config(tmp_path)
        assert cfg.access.scheme == "https"
        assert cfg.access.port is None


class TestSecurityConfigCookiesSecure:
    """Tests for cookies_secure addition to SecurityConfig (ALP-725)."""

    def test_cookies_secure_defaults_false_on_shipped_old_yaml(self, config_dir: Path) -> None:
        # AC3: default false, old YAMLs (no key) succeed
        cfg = load_security_config(config_dir)
        assert cfg.cookies_secure is False

    def test_cookies_secure_can_be_set_true_in_new_yaml(self, tmp_path: Path) -> None:
        # AC3 + AC4
        sec_content = """\
session:
  duration_hours: 12
  cookie_name: "cc_session"
csrf:
  cookie_name: "cc_csrf"
webauthn:
  relying_party_id: "localhost"
  relying_party_name: "AlphaMind Command Center"
cookies_secure: true
"""
        (tmp_path / "security.yaml").write_text(sec_content, encoding="utf-8")
        cfg = load_security_config(tmp_path)
        assert cfg.cookies_secure is True

    def test_cookies_secure_has_deploy_time_metadata(self) -> None:
        # AC6
        hints = get_type_hints(SecurityConfig, include_extras=True)
        cs_hint = hints["cookies_secure"]
        metadata = getattr(cs_hint, "__metadata__", ())
        assert ReloadPolicy.DEPLOY_TIME in metadata

    def test_extra_field_rejected_still_works_with_new_field(self, tmp_path: Path) -> None:
        # extra forbid continues to protect the new field too
        bad = {
            "session": {"duration_hours": 12, "cookie_name": "x"},
            "csrf": {"cookie_name": "x"},
            "webauthn": {
                "relying_party_id": "localhost",
                "relying_party_name": "AlphaMind",
            },
            "cookies_secure": False,
            "bogus": True,
        }
        bad_yaml = tmp_path / "security.yaml"
        bad_yaml.write_text(yaml.safe_dump(bad), encoding="utf-8")
        with pytest.raises(ValidationError, match="bogus"):
            load_security_config(tmp_path)

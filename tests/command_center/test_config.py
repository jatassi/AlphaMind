"""Tests for ``command_center.config`` (story 02 / ALP-666).

The three Pydantic configuration models — ``CommandCenterConfig`` /
``SecurityConfig`` / ``AlertsConfig`` — load the three YAML files
(``command-center.yaml`` / ``security.yaml`` / ``alerts.yaml``) with
``extra='forbid'`` so a typo in the YAML fails loud at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from alphamind.command_center.config import (
    AlertsConfig,
    CommandCenterConfig,
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

    def test_extra_field_in_yaml_rejected(self, tmp_path: Path) -> None:
        bad = {
            "bind": {"host": "127.0.0.1", "port": 8080},
            "db": {"alphamind_db_path": "/tmp/x.db"},
            "frontend": {"dist_path": "/dist"},
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

    def test_rules_default_empty(self, config_dir: Path) -> None:
        config = load_alerts_config(config_dir)
        # Story 05a populates the rule list; story 02 ships an empty list.
        assert config.rules == []

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
        with pytest.raises(FileNotFoundError, match="command-center.yaml"):
            load_command_center_config(tmp_path)

    def test_security_loader_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="security.yaml"):
            load_security_config(tmp_path)

    def test_alerts_loader_raises_on_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="alerts.yaml"):
            load_alerts_config(tmp_path)

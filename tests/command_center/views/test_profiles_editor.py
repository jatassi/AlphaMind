"""Tests for the profiles config editor (story 06a / ALP-682).

Covers:
* ``GET /api/views/config/files?family=profiles`` — returns slug list.
* ``GET /api/views/config/schema/profiles/{name}`` — form-schema for ProfileConfig.
* ``PUT /api/views/config/profiles/{name}`` — atomic write after validation.
* ``rule_values`` dict field emits ``object-array`` with key/value columns.
* Reload policy defaults to ``invocation_time`` for profile fields.

Scoped pytest: ``uv run pytest tests/command_center/views/ -n auto``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.dependencies import csrf_required, current_session
from alphamind.command_center.views.configuration import (
    build_configuration_router,
    derive_form_schema,
)
from alphamind.config.models.profiles import ProfileConfig

_TEST_SESSION_ID = operator_session_id("sess-profiles-test")

_SMALL_YAML = textwrap.dedent("""\
    capital_range_usd: [5000, 15000]
    risk_priority: concentration_management
    feature_flags:
      options_enabled: false
      short_selling_enabled: false
      fractional_shares_required: true
    active_sectors: [tech, semis]
    min_position_size_usd: 75
    rule_values:
      position_max_size_pct: 5
      daily_drawdown_pct: 2.5
    agent_token_budgets:
      analyst:
        context: [1500, 2500]
        output: [1200, 2500]
""")


def _make_client(config_dir: Path | None = None) -> TestClient:
    app = FastAPI()
    if config_dir is not None:
        app.state.config_dir = config_dir
    app.include_router(build_configuration_router(), prefix="/api/views/config")
    app.dependency_overrides[current_session] = lambda: _TEST_SESSION_ID
    app.dependency_overrides[csrf_required] = lambda: None
    return TestClient(app)


class TestFilesEndpoint:
    """``GET /api/views/config/files?family=profiles``."""

    def test_returns_profile_slug_list(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/files?family=profiles")
        assert response.status_code == 200
        body = response.json()
        assert body["family"] == "profiles"
        slugs = body["slugs"]
        assert "profiles/small" in slugs
        assert "profiles/medium" in slugs
        assert "profiles/micro" in slugs
        assert "profiles/large" in slugs

    def test_unknown_family_returns_404(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/files?family=not-a-family")
        assert response.status_code == 404

    def test_requires_session_dependency(self) -> None:
        """Files endpoint must declare current_session dependency.

        Without a session override the real dependency raises or returns
        an error status. The exact code varies by framework version; the
        key invariant is that the endpoint does not return 200 without
        a valid session.
        """
        app = FastAPI()
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/views/config/files?family=profiles")
        assert response.status_code != 200


class TestSchemaEndpoint:
    """``GET /api/views/config/schema/profiles/{name}``."""

    def test_profiles_small_schema_returned(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/schema/profiles/small")
        assert response.status_code == 200
        body = response.json()
        paths = {f["path"] for f in body["fields"]}
        assert "capital_range_usd" in paths
        assert "risk_priority" in paths
        assert "min_position_size_usd" in paths

    def test_rule_values_emits_object_array(self) -> None:
        """``rule_values: dict[str, float]`` must render as object-array table."""
        from alphamind.command_center.views.configuration import ConfigFile

        entry = ConfigFile(slug="t", model=ProfileConfig, filename="t.yaml")
        schema = derive_form_schema(entry)
        rule_values_field = next(f for f in schema.fields if f.path == "rule_values")
        assert rule_values_field.control_type == "object-array"

    def test_rule_values_has_key_value_columns(self) -> None:
        """Columns for ``dict[str, float]`` are ``key`` (string) + ``value`` (number)."""
        from alphamind.command_center.views.configuration import ConfigFile

        entry = ConfigFile(slug="t", model=ProfileConfig, filename="t.yaml")
        schema = derive_form_schema(entry)
        rule_values_field = next(f for f in schema.fields if f.path == "rule_values")
        assert rule_values_field.columns is not None
        col_paths = [c.path for c in rule_values_field.columns]
        assert col_paths == ["key", "value"]
        col_types = {c.path: c.control_type for c in rule_values_field.columns}
        assert col_types["key"] == "string"
        assert col_types["value"] == "number"

    def test_risk_priority_emits_enum_with_choices(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/schema/profiles/small")
        body = response.json()
        rp_field = next(f for f in body["fields"] if f["path"] == "risk_priority")
        assert rp_field["control_type"] == "enum"
        assert "concentration_management" in rp_field["constraints"]["enum_choices"]

    def test_all_profile_fields_are_invocation_time(self) -> None:
        """No deploy-time fields in profiles — everything reloads at invocation."""
        client = _make_client()
        response = client.get("/api/views/config/schema/profiles/small")
        body = response.json()
        for field in body["fields"]:
            assert field["reload_policy"] == "invocation_time", (
                f"Field {field['path']!r} unexpectedly has deploy_time reload policy"
            )

    def test_unknown_profile_slug_returns_404(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/schema/profiles/nonexistent")
        assert response.status_code == 404


class TestPutEndpoint:
    """``PUT /api/views/config/profiles/{name}``."""

    def test_valid_yaml_writes_file(self, tmp_path: Path) -> None:
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        client = _make_client(config_dir=tmp_path)
        response = client.put(
            "/api/views/config/profiles/small",
            json={"yaml": _SMALL_YAML},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["slug"] == "profiles/small"
        assert body["deploy_time_fields_changed"] is False
        written = (profiles_dir / "small.yaml").read_text(encoding="utf-8")
        assert "concentration_management" in written

    def test_invalid_yaml_rejected_at_parse_layer(self, tmp_path: Path) -> None:
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        client = _make_client(config_dir=tmp_path)
        response = client.put(
            "/api/views/config/profiles/small",
            json={"yaml": ": invalid: yaml: [unclosed"},
        )
        assert response.status_code == 422

    def test_schema_violation_rejected_at_parse_layer(self, tmp_path: Path) -> None:
        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        client = _make_client(config_dir=tmp_path)
        # Missing required fields — Pydantic parse error.
        response = client.put(
            "/api/views/config/profiles/small",
            json={"yaml": "capital_range_usd: [0, 100]\n"},
        )
        assert response.status_code == 422

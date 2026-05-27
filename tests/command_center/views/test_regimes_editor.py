"""Tests for the regimes config editor (story 06a / ALP-682).

Covers:
* ``GET /api/views/config/files?family=regimes`` — returns slug list.
* ``GET /api/views/config/schema/regimes/{name}`` — form-schema for RegimeConfig.
* ``PUT /api/views/config/regimes/{name}`` — atomic write after validation.
* ``multipliers`` dict field emits ``object-array`` with key/value columns.
* Reload policy defaults to ``invocation_time`` for regime fields.

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
from alphamind.config.models.regimes import RegimeConfig

_TEST_SESSION_ID = operator_session_id("sess-regimes-test")

_NORMAL_YAML = textwrap.dedent("""\
    vix_range: [14, 22]
    multipliers:
      position_max_size_pct: 1.00
      daily_drawdown_pct: 1.00
      gross_exposure_pct: 1.00
    transition:
      tighten_on_entry: immediate
      loosen_on_exit: linear_over_invocations_3
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
    """``GET /api/views/config/files?family=regimes``."""

    def test_returns_regime_slug_list(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/files?family=regimes")
        assert response.status_code == 200
        body = response.json()
        assert body["family"] == "regimes"
        slugs = body["slugs"]
        assert "regimes/normal" in slugs
        assert "regimes/crisis" in slugs
        assert "regimes/elevated" in slugs
        assert "regimes/low-vol" in slugs


class TestSchemaEndpoint:
    """``GET /api/views/config/schema/regimes/{name}``."""

    def test_regimes_normal_schema_returned(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/schema/regimes/normal")
        assert response.status_code == 200
        body = response.json()
        paths = {f["path"] for f in body["fields"]}
        assert "vix_range" in paths
        assert "multipliers" in paths

    def test_multipliers_emits_object_array(self) -> None:
        """``multipliers: dict[str, float]`` must render as object-array table."""
        from alphamind.command_center.views.configuration import ConfigFile

        entry = ConfigFile(slug="t", model=RegimeConfig, filename="t.yaml")
        schema = derive_form_schema(entry)
        mults_field = next(f for f in schema.fields if f.path == "multipliers")
        assert mults_field.control_type == "object-array"

    def test_multipliers_has_key_value_columns(self) -> None:
        """Columns for ``dict[str, float]`` are ``key`` (string) + ``value`` (number)."""
        from alphamind.command_center.views.configuration import ConfigFile

        entry = ConfigFile(slug="t", model=RegimeConfig, filename="t.yaml")
        schema = derive_form_schema(entry)
        mults_field = next(f for f in schema.fields if f.path == "multipliers")
        assert mults_field.columns is not None
        col_paths = [c.path for c in mults_field.columns]
        assert col_paths == ["key", "value"]
        col_types = {c.path: c.control_type for c in mults_field.columns}
        assert col_types["key"] == "string"
        assert col_types["value"] == "number"

    def test_tighten_on_entry_emits_enum(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/schema/regimes/normal")
        body = response.json()
        tighten_field = next(f for f in body["fields"] if "tighten_on_entry" in f["path"])
        assert tighten_field["control_type"] == "enum"
        assert "immediate" in tighten_field["constraints"]["enum_choices"]

    def test_all_regime_fields_are_invocation_time(self) -> None:
        """No deploy-time fields in regimes — everything reloads at invocation."""
        client = _make_client()
        response = client.get("/api/views/config/schema/regimes/normal")
        body = response.json()
        for field in body["fields"]:
            assert field["reload_policy"] == "invocation_time", (
                f"Field {field['path']!r} unexpectedly has deploy_time reload policy"
            )

    def test_unknown_regime_slug_returns_404(self) -> None:
        client = _make_client()
        response = client.get("/api/views/config/schema/regimes/nonexistent")
        assert response.status_code == 404


class TestPutEndpoint:
    """``PUT /api/views/config/regimes/{name}``."""

    def test_valid_yaml_writes_file(self, tmp_path: Path) -> None:
        regimes_dir = tmp_path / "regimes"
        regimes_dir.mkdir()
        client = _make_client(config_dir=tmp_path)
        response = client.put(
            "/api/views/config/regimes/normal",
            json={"yaml": _NORMAL_YAML},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["slug"] == "regimes/normal"
        assert body["deploy_time_fields_changed"] is False
        written = (regimes_dir / "normal.yaml").read_text(encoding="utf-8")
        assert "position_max_size_pct" in written

    def test_invalid_yaml_rejected(self, tmp_path: Path) -> None:
        regimes_dir = tmp_path / "regimes"
        regimes_dir.mkdir()
        client = _make_client(config_dir=tmp_path)
        response = client.put(
            "/api/views/config/regimes/normal",
            json={"yaml": ": bad: yaml ["},
        )
        assert response.status_code == 422

    def test_schema_violation_rejected(self, tmp_path: Path) -> None:
        regimes_dir = tmp_path / "regimes"
        regimes_dir.mkdir()
        client = _make_client(config_dir=tmp_path)
        # Missing required multipliers field.
        response = client.put(
            "/api/views/config/regimes/normal",
            json={"yaml": "vix_range: [14, 22]\n"},
        )
        assert response.status_code == 422

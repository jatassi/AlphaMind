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

from enum import Enum
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from alphamind.command_center._kernel.ids import operator_session_id
from alphamind.command_center.auth.dependencies import csrf_required, current_session
from alphamind.command_center.views.configuration import (
    ReloadPolicy,
    build_configuration_router,
    reload_policy_of,
)


class _Color(Enum):
    """Module-scope enum used by the enum-classifier test.

    Pydantic resolves field annotations via ``typing.get_type_hints``,
    which looks up names against the model class's containing module.
    Locally-defined classes inside a test method aren't visible to
    that lookup; defining at module scope keeps the schema derivation
    happy.
    """

    RED = "red"
    GREEN = "green"
    BLUE = "blue"


class _ColorModel(BaseModel):
    """Module-scope model for the enum classifier test."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    color: _Color


class _Rule(BaseModel):
    """Module-scope inner element for the object-array columns test."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    severity: int


class _RulesContainer(BaseModel):
    """Module-scope outer model for the object-array columns test."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    rules: list[_Rule]


_TEST_SESSION_ID = operator_session_id("sess-config-test")


def _override_auth(app: FastAPI) -> None:
    """Wire fake auth dependencies for the test app.

    The configuration router gates every endpoint on
    :func:`current_session` (read) + :func:`csrf_required` (write); we
    bypass both here so the test focuses on the configuration logic.
    The auth-positive tests live in their own class below.
    """
    app.dependency_overrides[current_session] = lambda: _TEST_SESSION_ID
    app.dependency_overrides[csrf_required] = lambda: None


class TestReloadPolicyEnum:
    def test_has_invocation_time_and_deploy_time_members(self) -> None:
        # Two policies per the design — invocation-time-reload (most fields)
        # and deploy-time-only (paths in main.yaml, SQLite pragmas, package
        # versions, .env location). The badge UI dispatches on the enum.
        members = {ReloadPolicy.INVOCATION_TIME, ReloadPolicy.DEPLOY_TIME}
        assert len(members) == 2

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

    def test_webauthn_relying_party_id_is_deploy_time(self) -> None:
        """Story 06b: WebAuthn relying-party ID requires restart.

        The WebAuthn verifier resolves the relying-party hostname at
        lifespan startup; a mid-flight edit does not re-bind already-
        issued passkeys' relying-party scope.
        """
        from alphamind.command_center.config import WebauthnConfig

        assert reload_policy_of(WebauthnConfig, "relying_party_id") is ReloadPolicy.DEPLOY_TIME

    def test_webauthn_relying_party_name_is_invocation_time(self) -> None:
        """The user-facing RP name only affects the browser passkey UI."""
        from alphamind.command_center.config import WebauthnConfig

        assert (
            reload_policy_of(WebauthnConfig, "relying_party_name") is ReloadPolicy.INVOCATION_TIME
        )


class TestSchemaEndpoint:
    """``GET /api/views/config/schema/{config_file}``.

    Returns the form-schema metadata derived from the file's Pydantic
    model + reload-policy decorators. The frontend consumes this to
    dispatch per-control-type renders.
    """

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        _override_auth(app)
        return TestClient(app)

    def test_unknown_config_file_returns_404(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/not-a-real-file")
        assert response.status_code == 404

    def test_command_center_schema_returned(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        assert response.status_code == 200
        body = response.json()
        # Each field has a dotted path + control_type + reload_policy.
        paths = {field["path"] for field in body["fields"]}
        assert "bind.host" in paths
        assert "bind.port" in paths
        assert "db.alphamind_db_path" in paths

    def test_command_center_db_path_is_deploy_time(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        db_path_field = next(
            field for field in body["fields"] if field["path"] == "db.alphamind_db_path"
        )
        assert db_path_field["reload_policy"] == "deploy_time"

    def test_command_center_pipeline_url_is_invocation_time(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        url_field = next(
            field for field in body["fields"] if field["path"] == "pipeline.control_url"
        )
        # Unannotated → default INVOCATION_TIME per the framework contract.
        assert url_field["reload_policy"] == "invocation_time"

    def test_number_control_type_for_int_field(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        port_field = next(field for field in body["fields"] if field["path"] == "bind.port")
        assert port_field["control_type"] == "number"
        # Field constraints (ge=1, le=65535) surface for client-side bounds
        # enforcement before the PUT lands at the backend.
        assert port_field["constraints"]["minimum"] == 1
        assert port_field["constraints"]["maximum"] == 65535

    def test_string_control_type_for_str_field(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        host_field = next(field for field in body["fields"] if field["path"] == "bind.host")
        assert host_field["control_type"] == "string"

    def test_security_schema_returned(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/security")
        assert response.status_code == 200
        body = response.json()
        paths = {field["path"] for field in body["fields"]}
        assert "session.duration_hours" in paths
        assert "csrf.cookie_name" in paths

    def test_alerts_schema_returned(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/alerts")
        assert response.status_code == 200
        body = response.json()
        paths = {field["path"] for field in body["fields"]}
        assert "channels.discord.webhook_url_env" in paths

    def test_alerts_rules_carry_typed_object_array_columns(self) -> None:
        """Story 06b: ``rules`` now typed ``list[AlertRuleSpec]``.

        The framework's ``_element_columns`` walker emits one column per
        sub-field of the row's Pydantic model — so the frontend's
        :class:`ObjectArrayTableEditor` can render typed cells (name /
        severity / debounce_minutes / channels) rather than inferring
        columns from row data.
        """
        client = self._client()
        response = client.get("/api/views/config/schema/alerts")
        body = response.json()
        rules_field = next(field for field in body["fields"] if field["path"] == "rules")
        assert rules_field["control_type"] == "object-array"
        columns = rules_field["columns"]
        assert columns is not None
        column_paths = {col["path"] for col in columns}
        assert {"name", "severity", "debounce_minutes", "channels"} <= column_paths

    def test_webauthn_relying_party_id_is_deploy_time(self) -> None:
        """Story 06b: WebAuthn relying-party ID requires restart.

        The verifier is constructed at lifespan startup against the
        configured hostname; an in-flight edit only takes effect after
        process restart. Surface as DEPLOY_TIME so the editor renders
        the restart-required badge.
        """
        client = self._client()
        response = client.get("/api/views/config/schema/security")
        body = response.json()
        rp_id_field = next(
            field for field in body["fields"] if field["path"] == "webauthn.relying_party_id"
        )
        assert rp_id_field["reload_policy"] == "deploy_time"

    def test_digest_schema_returned(self) -> None:
        """Story 06b registers ``digest.yaml`` as an editor target."""
        client = self._client()
        response = client.get("/api/views/config/schema/digest")
        assert response.status_code == 200
        body = response.json()
        paths = {field["path"] for field in body["fields"]}
        # One field per per-detector block — pin a representative cross-
        # section so the registration covers the shipped digest.yaml shape.
        assert "anti_pattern_spike.baseline_window_weeks" in paths
        assert "regime_change.enabled" in paths
        assert "validation_window_end.days_before_due" in paths

    def test_db_path_carries_path_control_type(self) -> None:
        """Regression for finding #13 (Wave-5 review).

        ``DbConfig.alphamind_db_path`` carries a ``ControlHint.PATH``
        annotation so the frontend's :class:`PathInput` component
        renders for the field. The previous classifier emitted ``string``
        for every plain-``str`` field, leaving the path / cron / enum
        controls unreachable.
        """
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        db_path_field = next(
            field for field in body["fields"] if field["path"] == "db.alphamind_db_path"
        )
        assert db_path_field["control_type"] == "path"

    def test_dist_path_carries_path_control_type(self) -> None:
        client = self._client()
        response = client.get("/api/views/config/schema/command-center")
        body = response.json()
        dist_path_field = next(
            field for field in body["fields"] if field["path"] == "frontend.dist_path"
        )
        assert dist_path_field["control_type"] == "path"

    def test_enum_field_emits_control_type_and_choices(self) -> None:
        """Enum-typed fields surface ``control_type='enum'`` + ``enum_choices``.

        Uses the module-scope :class:`_ColorModel` so the schema
        derivation can resolve the annotation via ``get_type_hints``.
        """
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            FormSchema,
            derive_form_schema,
        )

        entry = ConfigFile(slug="t", model=_ColorModel, filename="t.yaml")
        schema: FormSchema = derive_form_schema(entry)
        (field_schema,) = schema.fields
        assert field_schema.control_type == "enum"
        assert field_schema.constraints["enum_choices"] == ["red", "green", "blue"]

    def test_object_array_field_emits_columns(self) -> None:
        """Regression for finding #12 (Wave-5 review).

        Typed ``list[BaseModel]`` fields surface per-column
        FormFieldSchema entries so the frontend's table editor can
        render cells. Previously the wire carried ``columns=[]`` and
        the table rendered zero columns (alerts.yaml editor was
        non-functional).
        """
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            derive_form_schema,
        )

        entry = ConfigFile(slug="t", model=_RulesContainer, filename="t.yaml")
        schema = derive_form_schema(entry)
        (rules_field,) = schema.fields
        assert rules_field.control_type == "object-array"
        assert rules_field.columns is not None
        column_paths = [c.path for c in rules_field.columns]
        assert column_paths == ["name", "severity"]
        # Per-column control types match the element field types.
        types_by_path = {c.path: c.control_type for c in rules_field.columns}
        assert types_by_path["name"] == "string"
        assert types_by_path["severity"] == "number"


class TestPathExistsEndpoint:
    """``GET /api/views/config/path-exists`` — PathInput probe.

    Finding #2 (Wave-5 review): the probe must be authenticated AND
    sandboxed to the configured ``config_dir`` so it isn't a generic
    filesystem oracle.
    """

    def _client(self, config_dir: Path) -> TestClient:
        app = FastAPI()
        app.state.config_dir = config_dir
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        _override_auth(app)
        return TestClient(app)

    def test_existing_path_inside_config_dir_returns_true(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        existing_file = tmp_path / "real.txt"
        existing_file.write_text("data", encoding="utf-8")
        response = client.get(f"/api/views/config/path-exists?path={existing_file}")
        assert response.status_code == 200
        assert response.json() == {"exists": True}

    def test_missing_path_inside_config_dir_returns_false(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        missing = tmp_path / "does-not-exist.txt"
        response = client.get(f"/api/views/config/path-exists?path={missing}")
        assert response.status_code == 200
        assert response.json() == {"exists": False}

    def test_path_outside_config_dir_rejected(self, tmp_path: Path) -> None:
        """Regression: paths above the config directory return 400.

        The previous implementation accepted any filesystem path and
        leaked existence — a curl loop could probe ``/etc/shadow`` or
        any operator-readable file on the host.
        """
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        client = self._client(sandbox)
        response = client.get(f"/api/views/config/path-exists?path={outside}")
        assert response.status_code == 400

    def test_traversal_path_rejected(self, tmp_path: Path) -> None:
        """``..`` traversal escapes resolve to outside config_dir → 400."""
        sandbox = tmp_path / "sandbox"
        sandbox.mkdir()
        client = self._client(sandbox)
        escape = sandbox / ".." / "outside.txt"
        response = client.get(f"/api/views/config/path-exists?path={escape}")
        assert response.status_code == 400


class TestPutEndpoint:
    """``PUT /api/views/config/{config_file}``.

    Atomic file write via :func:`alphamind._kernel.atomic_io.atomic_write_text`
    after the three validation layers (parse / cross-ref / semantic) pass.
    Rejects with the layered error envelope on failure.
    """

    def _client(self, config_dir: Path) -> TestClient:
        app = FastAPI()
        app.state.config_dir = config_dir
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        _override_auth(app)
        return TestClient(app)

    def _valid_command_center_yaml(self, db_path: Path) -> str:
        # All values match the shipped command-center.yaml shape so the
        # parse layer accepts the body.
        return (
            "bind:\n"
            '  host: "127.0.0.1"\n'
            "  port: 8080\n"
            "db:\n"
            f'  alphamind_db_path: "{db_path}"\n'
            "frontend:\n"
            '  dist_path: "dist"\n'
            "pipeline:\n"
            '  control_url: "http://127.0.0.1:8765"\n'
            '  events_url: "http://127.0.0.1:8765"\n'
            "monitor:\n"
            '  control_url: "http://127.0.0.1:8766"\n'
            '  events_url: "http://127.0.0.1:8766"\n'
        )

    def test_unknown_config_file_returns_404(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        response = client.put(
            "/api/views/config/not-a-real-file",
            json={"yaml": "x: 1\n"},
        )
        assert response.status_code == 404

    def test_parse_failure_returns_422_with_parse_layer_envelope(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        # bind.port out of range — Pydantic Field(ge=1, le=65535) rejects.
        bad_yaml = (
            "bind:\n"
            '  host: "127.0.0.1"\n'
            "  port: 999999\n"
            "db:\n"
            '  alphamind_db_path: "/tmp/x.db"\n'
            "frontend:\n"
            '  dist_path: "dist"\n'
            "pipeline:\n"
            '  control_url: "http://127.0.0.1:8765"\n'
            '  events_url: "http://127.0.0.1:8765"\n'
            "monitor:\n"
            '  control_url: "http://127.0.0.1:8766"\n'
            '  events_url: "http://127.0.0.1:8766"\n'
        )
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": bad_yaml},
        )
        assert response.status_code == 422
        body = response.json()
        # Layered envelope — parse failures carry a `parse` key with
        # per-field error rows.
        assert "parse" in body["detail"]
        assert body["detail"]["parse"], "parse layer must list the offending field"
        # The other layers are present-but-empty so the frontend can render
        # the three-bucket banner unconditionally.
        assert body["detail"]["cross_reference"] == []
        assert body["detail"]["semantic"] == []

    def test_malformed_yaml_returns_422(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": ":::not-valid-yaml:::"},
        )
        assert response.status_code == 422

    def test_successful_put_atomically_writes_file(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        # Seed an existing file so the atomic-replace is exercised.
        target_path.write_text("placeholder: true\n", encoding="utf-8")

        good_yaml = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": good_yaml},
        )

        assert response.status_code == 200
        body = response.json()
        # Response carries the deploy-time-only field flag so the
        # frontend can surface a restart-needed reminder.
        assert "deploy_time_fields_changed" in body
        # And the file was atomically replaced — no .tmp lingering.
        assert target_path.read_text(encoding="utf-8") == good_yaml
        assert not (tmp_path / "command-center.yaml.tmp").exists()

    def test_successful_put_creates_file_when_missing(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        assert not target_path.exists()

        good_yaml = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": good_yaml},
        )

        assert response.status_code == 200
        assert target_path.read_text(encoding="utf-8") == good_yaml

    def test_failed_put_does_not_touch_existing_file(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        existing = "existing: value\n"
        target_path.write_text(existing, encoding="utf-8")

        bad_yaml = "bind:\n  host: ''\n  port: 0\n"  # parse failure
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": bad_yaml},
        )

        assert response.status_code == 422
        # The existing file is unchanged — rejection happens before the
        # atomic write fires.
        assert target_path.read_text(encoding="utf-8") == existing
        assert not (tmp_path / "command-center.yaml.tmp").exists()

    def test_deploy_time_flag_false_when_only_invocation_time_changed(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        # Seed an existing valid file.
        original = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        target_path.write_text(original, encoding="utf-8")

        # Edit only the (invocation-time) pipeline.control_url.
        new_yaml = original.replace(
            'control_url: "http://127.0.0.1:8765"',
            'control_url: "http://127.0.0.1:9999"',
        )

        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": new_yaml},
        )
        assert response.status_code == 200
        assert response.json()["deploy_time_fields_changed"] is False

    def test_deploy_time_flag_true_when_db_path_changed(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        target_path = tmp_path / "command-center.yaml"
        original = self._valid_command_center_yaml(tmp_path / "alphamind.db")
        target_path.write_text(original, encoding="utf-8")

        # Change DB path — deploy-time field.
        new_yaml = self._valid_command_center_yaml(tmp_path / "different.db")
        response = client.put(
            "/api/views/config/command-center",
            json={"yaml": new_yaml},
        )
        assert response.status_code == 200
        assert response.json()["deploy_time_fields_changed"] is True


class TestLayeredValidationHooks:
    """Cross-reference + semantic hooks produce layered envelope rejections.

    The framework registers per-file hooks; per-file editors in 06a/06b
    wire the existing ``config/validation/`` chain. This test exercises
    the framework's hook contract using a fake hook attached to a stand-
    alone :class:`ConfigFile` registration.
    """

    def test_cross_reference_failure_returns_envelope(self) -> None:
        from alphamind.command_center.config import SecurityConfig
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            ConfigUpdateRequest,
            run_validation,
        )

        entry = ConfigFile(
            slug="security",
            model=SecurityConfig,
            filename="security.yaml",
            cross_reference_hook=lambda _m: ["bogus cross-reference mismatch"],
        )

        # Valid YAML for SecurityConfig.
        valid_yaml = (
            "session:\n"
            "  duration_hours: 12\n"
            '  cookie_name: "cc_session"\n'
            "csrf:\n"
            '  cookie_name: "cc_csrf"\n'
            "webauthn:\n"
            '  relying_party_id: "localhost"\n'
            '  relying_party_name: "AlphaMind"\n'
        )
        _model, report = run_validation(entry, valid_yaml)
        assert report.parse == []
        assert len(report.cross_reference) == 1
        assert report.cross_reference[0].message == "bogus cross-reference mismatch"
        assert report.semantic == []
        # Type confirmation: the request body model exists and parses cleanly.
        req = ConfigUpdateRequest(yaml=valid_yaml)
        assert req.yaml == valid_yaml

    def test_semantic_failure_returns_envelope(self) -> None:
        from alphamind.command_center.config import SecurityConfig
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            run_validation,
        )

        entry = ConfigFile(
            slug="security",
            model=SecurityConfig,
            filename="security.yaml",
            semantic_hook=lambda _m: ["invariant violated"],
        )

        valid_yaml = (
            "session:\n"
            "  duration_hours: 12\n"
            '  cookie_name: "cc_session"\n'
            "csrf:\n"
            '  cookie_name: "cc_csrf"\n'
            "webauthn:\n"
            '  relying_party_id: "localhost"\n'
            '  relying_party_name: "AlphaMind"\n'
        )
        _model, report = run_validation(entry, valid_yaml)
        assert report.parse == []
        assert report.cross_reference == []
        assert len(report.semantic) == 1
        assert report.semantic[0].message == "invariant violated"

    def test_router_mounted_under_views_config_prefix(self, tmp_path: Path) -> None:
        """build_app wires build_configuration_router under /api/views/config."""
        import shutil

        from alphamind.command_center.app import build_app
        from alphamind.command_center.config import (
            load_alerts_config,
            load_command_center_config,
            load_security_config,
        )

        # Mirror the shipped config tree into tmp_path so build_app's
        # loaders find their three YAML files.
        repo_config = Path(__file__).parents[3] / "config"
        cc_yaml = tmp_path / "command-center.yaml"
        # Substitute an isolated DB path so build_app's lifespan can later
        # construct engines without touching the host's data dir.
        cc_text = (
            (repo_config / "command-center.yaml")
            .read_text(encoding="utf-8")
            .replace(
                '"%USERPROFILE%/AlphaMind/data/alphamind.db"',
                f'"{tmp_path}/alphamind.db"',
            )
        )
        cc_yaml.write_text(cc_text, encoding="utf-8")
        shutil.copy(repo_config / "security.yaml", tmp_path / "security.yaml")
        shutil.copy(repo_config / "alerts.yaml", tmp_path / "alerts.yaml")

        app = build_app(
            command_center_config=load_command_center_config(tmp_path),
            security_config=load_security_config(tmp_path),
            alerts_config=load_alerts_config(tmp_path),
            config_dir=tmp_path,
        )

        # build_app threads config_dir onto app.state for the PUT handler.
        assert app.state.config_dir == tmp_path
        # The schema endpoint requires an authenticated session per finding
        # #1 (Wave-5 review); bypass via dependency_overrides so the
        # router-mount assertion focuses on routing, not auth.
        _override_auth(app)
        # The schema endpoint surfaces under the mounted prefix without
        # exercising the lifespan (it has no DB dependency).
        client = TestClient(app)
        response = client.get("/api/views/config/schema/security")
        assert response.status_code == 200
        assert response.json()["slug"] == "security"

    def test_cross_reference_short_circuits_semantic(self) -> None:
        from alphamind.command_center.config import SecurityConfig
        from alphamind.command_center.views.configuration import (
            ConfigFile,
            run_validation,
        )

        # If cross-reference fails, the semantic hook does not run.
        semantic_calls: list[str] = []

        def semantic_hook(_m: object) -> list[str]:
            semantic_calls.append("called")
            return ["should not be reached"]

        entry = ConfigFile(
            slug="security",
            model=SecurityConfig,
            filename="security.yaml",
            cross_reference_hook=lambda _m: ["fails first"],
            semantic_hook=semantic_hook,
        )
        valid_yaml = (
            "session:\n"
            "  duration_hours: 12\n"
            '  cookie_name: "cc_session"\n'
            "csrf:\n"
            '  cookie_name: "cc_csrf"\n'
            "webauthn:\n"
            '  relying_party_id: "localhost"\n'
            '  relying_party_name: "AlphaMind"\n'
        )
        _model, report = run_validation(entry, valid_yaml)
        assert report.cross_reference and not report.semantic
        assert semantic_calls == []


class TestConfigurationAuth:
    """Regression for findings #1 and #2 (Wave-5 review).

    The configuration router previously had zero auth wiring — every
    endpoint was reachable by an unauthenticated curl. Mutating verbs
    must require both ``current_session`` and ``csrf_required`` per the
    project's auth contract (see ``alerts/routes.py:225``,
    ``control/routes.py:332``); read endpoints require ``current_session``
    only.
    """

    def _unauthed_client(self, config_dir: Path) -> TestClient:
        """A client whose app does NOT bypass auth — every call hits
        the real ``current_session`` dependency, which raises 401
        without a session cookie.
        """
        app = FastAPI()
        app.state.config_dir = config_dir
        app.include_router(build_configuration_router(), prefix="/api/views/config")
        # Stub the bits ``current_session`` reads off app.state so the
        # failure surface is the cookie-missing path (401) rather than
        # an unrelated AttributeError.
        from datetime import UTC, datetime

        app.state.security_config = type(
            "_SC",
            (),
            {
                "session": type("_S", (), {"cookie_name": "cc_session"})(),
                "csrf": type("_C", (), {"cookie_name": "cc_csrf"})(),
            },
        )()
        app.state.session_signing_secret = b"x" * 32
        app.state.clock = lambda: datetime.now(UTC)
        return TestClient(app)

    def test_put_requires_session(self, tmp_path: Path) -> None:
        client = self._unauthed_client(tmp_path)
        response = client.put(
            "/api/views/config/security",
            json={"yaml": "x: 1\n"},
        )
        # 401 (no session cookie) — not 422, not 200.
        assert response.status_code == 401

    def test_schema_requires_session(self, tmp_path: Path) -> None:
        client = self._unauthed_client(tmp_path)
        response = client.get("/api/views/config/schema/security")
        assert response.status_code == 401

    def test_path_exists_requires_session(self, tmp_path: Path) -> None:
        client = self._unauthed_client(tmp_path)
        response = client.get(
            f"/api/views/config/path-exists?path={tmp_path / 'x'}",
        )
        assert response.status_code == 401

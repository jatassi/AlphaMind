"""End-to-end tests for the configuration loader entry point (story 08).

The loader function is the single call the pipeline makes at invocation start
to read every YAML file, run all three validation layers (parse, cross-reference,
semantic), compose the resolved snapshot, and persist it. These tests exercise
the happy path against the shipped ``config/`` tree and inject one synthetic
break per validation layer to assert the right exception type propagates.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from alphamind.config import (
    PipelineConfig,
    load_full_config,
)
from alphamind.config.loaders import (
    load_profiles,
    load_regimes,
    load_run_types,
)
from alphamind.config.models import (
    FeedbackLoopConfig,
    Mode,
    Profile,
    Regime,
    ResolvedConfig,
    RuntimeDimensions,
    RunType,
    SnapshotResult,
)
from alphamind.config.snapshot import (
    compute_snapshot_hash,
    serialize_resolved_config,
)
from alphamind.config.validation.cross_reference import CrossReferenceError
from alphamind.config.validation.semantic import SemanticInvariantError

REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"

# Today is fixed so the semantic invariant on ``last_full_validation`` does not
# depend on calendar drift between test runs.
TODAY = date(2026, 4, 27)
# Canonical test invocation timestamp; date partition is "2026-04-27".
_AS_OF = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC)

# Env-var names referenced by ``config/venue.yaml`` — the cross-reference
# validator must see every one in ``.env`` for a clean load.
_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)


def _write_placeholder_env(env_path: Path) -> None:
    """Drop a ``.env`` file containing placeholder values for every venue ref."""
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    """A throwaway ``.env`` carrying every key the venue config references."""
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    """A throwaway archive root for snapshot persistence."""
    return tmp_path / "archive"


@pytest.fixture
def shipped_runtime() -> RuntimeDimensions:
    """The canonical happy-path runtime dimensions for the shipped tree."""
    return RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.market_open,
    )


@pytest.fixture
def fixture_config_tree(tmp_path: Path) -> Path:
    """A copy of the shipped ``config/`` tree the test may mutate freely."""
    target = tmp_path / "config"
    shutil.copytree(SHIPPED_CONFIG_DIR, target)
    return target


# ---------------------------------------------------------------------------
# Happy path: the shipped tree loads cleanly under (medium, normal, (), market_open).
# ---------------------------------------------------------------------------


def test_load_full_config_against_shipped_tree_returns_pipeline_config(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-happy-001",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    assert isinstance(loaded, PipelineConfig)
    assert isinstance(loaded.resolved, ResolvedConfig)
    assert isinstance(loaded.snapshot, SnapshotResult)


def test_load_full_config_resolves_to_expected_identity_dimensions(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    """The resolved snapshot reflects (medium, normal, (), market_open).

    ``ResolvedConfig`` carries the *projected* ``ProfileConfig`` /
    ``RegimeConfig`` / ``RunTypeConfig`` rather than the enum identifiers, so
    identity is verified by comparing each composed sub-model to the bundle
    entry the loader's YAML parsing produced. Equivalence implies the resolver
    routed (medium, normal, market_open) into the cascade.
    """
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-happy-002",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    # Re-read the bundles directly to spot-check projected identity. Comparing
    # the composed snapshot against the loaded bundle entry is stronger than
    # spot-checking individual scalars because it covers every field.
    profiles = load_profiles(SHIPPED_CONFIG_DIR)
    regimes = load_regimes(SHIPPED_CONFIG_DIR)
    run_types = load_run_types(SHIPPED_CONFIG_DIR)

    assert loaded.resolved.profile == profiles[Profile.medium]
    assert loaded.resolved.regime == regimes[Regime.normal]
    assert loaded.resolved.run_type == run_types[RunType.market_open]
    assert loaded.resolved.active_overlays == ()
    assert loaded.resolved.execution_mode.value == "paper"


def test_load_full_config_threads_shipped_feedback_config(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    """The composition root loads config/feedback.yaml into ResolvedConfig.feedback."""
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-feedback-001",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    expected = FeedbackLoopConfig.model_validate(
        yaml.safe_load((SHIPPED_CONFIG_DIR / "feedback.yaml").read_text())
    )
    assert loaded.resolved.feedback == expected


def test_load_full_config_enabled_agents_count_is_nine(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-happy-003",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    # AgentName has nine members; market_open enables every one of them.
    assert len(loaded.resolved.enabled_agents) == 9


def test_load_full_config_writes_snapshot_to_archive_root(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-happy-004",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    assert loaded.snapshot.path.exists()
    payload = json.loads(loaded.snapshot.path.read_text())
    assert isinstance(payload, dict)
    assert "rule_values" in payload


def test_load_full_config_snapshot_hash_matches_canonical_serialization(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-happy-005",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    expected = compute_snapshot_hash(serialize_resolved_config(loaded.resolved))
    assert loaded.snapshot.hash == expected
    assert len(loaded.snapshot.hash) == 64
    assert all(c in "0123456789abcdef" for c in loaded.snapshot.hash)


# ---------------------------------------------------------------------------
# PipelineConfig dataclass shape: frozen + slotted means immutable + no __dict__.
# ---------------------------------------------------------------------------


def test_pipeline_config_disallows_attribute_assignment(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    """frozen=True surfaces as a FrozenInstanceError on attribute assignment."""
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-frozen",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        loaded.__setattr__("resolved", loaded.resolved)


def test_pipeline_config_has_no_instance_dict(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    """slots=True surfaces as the absence of ``__dict__`` on the instance."""
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-08-slots",
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    assert not hasattr(loaded, "__dict__")


# ---------------------------------------------------------------------------
# Failure injection: parse-time validation
# ---------------------------------------------------------------------------


def test_malformed_yaml_raises_pydantic_validation_error(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
    fixture_config_tree: Path,
) -> None:
    """Mutate one YAML to violate its Pydantic schema; expect ValidationError."""
    # Drop ``rest_url`` from venue.yaml's paper block; AlpacaCredentials lists
    # it as required so the parser raises ValidationError on the missing field.
    venue_yaml = fixture_config_tree / "venue.yaml"
    payload = venue_yaml.read_text()
    broken = payload.replace("    rest_url: https://paper-api.alpaca.markets\n", "")
    assert broken != payload
    venue_yaml.write_text(broken)

    with pytest.raises((ValueError, TypeError)):
        load_full_config(
            config_dir=fixture_config_tree,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id="inv-08-parse-fail",
            runtime=shipped_runtime,
            today=TODAY,
            as_of=_AS_OF,
        )


# ---------------------------------------------------------------------------
# Failure injection: cross-reference validation
# ---------------------------------------------------------------------------


def test_missing_env_var_raises_cross_reference_error(
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
    tmp_path: Path,
) -> None:
    """An empty ``.env`` causes venue env-var refs to fail cross-reference."""
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# no keys defined\n")

    with pytest.raises(CrossReferenceError, match="ALPACA_PAPER_KEY"):
        load_full_config(
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=empty_env,
            archive_root=archive_root,
            invocation_id="inv-08-xref-fail",
            runtime=shipped_runtime,
            today=TODAY,
            as_of=_AS_OF,
        )


# ---------------------------------------------------------------------------
# Failure injection: semantic-self-test validation
# ---------------------------------------------------------------------------


def test_future_last_full_validation_raises_semantic_invariant_error(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
    fixture_config_tree: Path,
) -> None:
    """A ``last_full_validation`` later than ``today`` trips the semantic layer."""
    assets_yaml = fixture_config_tree / "assets.yaml"
    payload = assets_yaml.read_text()
    broken = payload.replace("last_full_validation: 2026-04-25", "last_full_validation: 2099-01-01")
    assert broken != payload, "Shipped assets.yaml is expected to carry the original date"
    assets_yaml.write_text(broken)

    with pytest.raises(SemanticInvariantError, match="2099-01-01"):
        load_full_config(
            config_dir=fixture_config_tree,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id="inv-08-semantic-fail",
            runtime=shipped_runtime,
            today=TODAY,
            as_of=_AS_OF,
        )


# ---------------------------------------------------------------------------
# Pure-function contract: every native exception escapes unwrapped.
# ---------------------------------------------------------------------------


def test_loader_propagates_native_exception_types_unchanged(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
    fixture_config_tree: Path,
    tmp_path: Path,
) -> None:
    """The three failure layers each surface their native exception type.

    A wrapping-exception bug (catch + re-raise as a different type) would slip
    past the per-layer ``pytest.raises`` checks above because subclass matches
    succeed. The strict ``type(exc) is X`` assertion forces detection.
    """
    # Layer 1: parse-time → pydantic.ValidationError
    venue_yaml = fixture_config_tree / "venue.yaml"
    venue_yaml.write_text(
        venue_yaml.read_text().replace("    rest_url: https://paper-api.alpaca.markets\n", "")
    )
    with pytest.raises((ValueError, TypeError)) as parse_exc:
        load_full_config(
            config_dir=fixture_config_tree,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id="inv-08-no-swallow-parse",
            runtime=shipped_runtime,
            today=TODAY,
            as_of=_AS_OF,
        )
    assert type(parse_exc.value) is ValidationError

    # Layer 2: cross-reference → CrossReferenceError
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("\n")
    with pytest.raises(CrossReferenceError) as xref_exc:
        load_full_config(
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=empty_env,
            archive_root=archive_root,
            invocation_id="inv-08-no-swallow-xref",
            runtime=shipped_runtime,
            today=TODAY,
            as_of=_AS_OF,
        )
    assert type(xref_exc.value) is CrossReferenceError

    # Layer 3: semantic → SemanticInvariantError. The fixture tree has been
    # mutated for the parse-time test; rebuild a clean copy with only the
    # semantic mutation.
    semantic_tree = tmp_path / "semantic_tree"
    shutil.copytree(SHIPPED_CONFIG_DIR, semantic_tree)
    assets_yaml = semantic_tree / "assets.yaml"
    assets_yaml.write_text(
        assets_yaml.read_text().replace(
            "last_full_validation: 2026-04-25", "last_full_validation: 2099-01-01"
        )
    )
    with pytest.raises(SemanticInvariantError) as semantic_exc:
        load_full_config(
            config_dir=semantic_tree,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id="inv-08-no-swallow-semantic",
            runtime=shipped_runtime,
            today=TODAY,
            as_of=_AS_OF,
        )
    assert type(semantic_exc.value) is SemanticInvariantError


# ---------------------------------------------------------------------------
# Snapshot path layout: invocation-id directory under archive_root.
# ---------------------------------------------------------------------------


def test_snapshot_path_uses_invocation_id_directory_under_archive_root(
    env_path: Path,
    archive_root: Path,
    shipped_runtime: RuntimeDimensions,
) -> None:
    invocation_id = "inv-08-path-001"
    loaded = load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id=invocation_id,
        runtime=shipped_runtime,
        today=TODAY,
        as_of=_AS_OF,
    )
    expected = archive_root / "2026-04-27" / invocation_id / "resolved_config.json"
    assert loaded.snapshot.path == expected

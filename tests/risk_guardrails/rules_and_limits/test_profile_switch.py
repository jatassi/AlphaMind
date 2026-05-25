"""Tests for the profile-switch handler (story 01d).

These tests exercise ``switch_active_profile`` over a temporary copy of the
shipped ``config/`` tree.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest
import yaml

from alphamind.config.models.main import ExecutionMode, MainConfig, Profile
from alphamind.risk_guardrails.rules_and_limits import (
    ProfileNotFoundError,
    ProfileSwitchOutcome,
    switch_active_profile,
)

REPO_ROOT = Path(__file__).parent.parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Copy the shipped config tree into a tmp path so tests can mutate it."""
    dest = tmp_path / "config"
    shutil.copytree(SHIPPED_CONFIG_DIR, dest)
    return dest


def _load_main(config_dir: Path) -> MainConfig:
    payload = yaml.safe_load((config_dir / "main.yaml").read_text(encoding="utf-8"))
    return MainConfig.model_validate(payload)


def test_switch_to_different_profile_rewrites_main_yaml(config_dir: Path) -> None:
    """Happy path: switching from medium → large updates main.yaml in place."""
    pre = _load_main(config_dir)
    assert pre.active_profile == Profile.medium  # sanity check on shipped tree

    outcome = switch_active_profile(new_profile=Profile.large, config_dir=config_dir)

    assert outcome.previous_profile == Profile.medium
    assert outcome.new_profile == Profile.large
    assert outcome.is_no_op is False
    assert outcome.main_yaml_path == config_dir / "main.yaml"

    post = _load_main(config_dir)
    assert post.active_profile == Profile.large
    # Non-active_profile fields are untouched.
    assert post.execution_mode == pre.execution_mode
    assert post.paths == pre.paths


def test_no_op_switch_leaves_main_yaml_byte_identical(config_dir: Path) -> None:
    """Switching to the current profile is a legitimate operator action.

    Returns a no-op outcome and leaves main.yaml untouched (byte-identical).
    """
    main_yaml_path = config_dir / "main.yaml"
    pre_bytes = main_yaml_path.read_bytes()
    pre_mtime_ns = main_yaml_path.stat().st_mtime_ns

    outcome = switch_active_profile(new_profile=Profile.medium, config_dir=config_dir)

    assert outcome.previous_profile == Profile.medium
    assert outcome.new_profile == Profile.medium
    assert outcome.is_no_op is True
    assert main_yaml_path.read_bytes() == pre_bytes
    assert main_yaml_path.stat().st_mtime_ns == pre_mtime_ns


def test_missing_profile_yaml_raises_profile_not_found(config_dir: Path) -> None:
    """A request for a profile whose YAML file is absent raises ProfileNotFoundError."""
    target = config_dir / "profiles" / "large.yaml"
    target.unlink()

    with pytest.raises(ProfileNotFoundError) as excinfo:
        switch_active_profile(new_profile=Profile.large, config_dir=config_dir)

    message = str(excinfo.value)
    assert "large.yaml" in message
    # LookupError subclass — confirms the HTTP 404 mapping path.
    assert isinstance(excinfo.value, LookupError)


def test_atomic_rename_failure_leaves_main_yaml_intact(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-rewrite failure must leave main.yaml byte-identical to its prior state."""
    main_yaml_path = config_dir / "main.yaml"
    pre_bytes = main_yaml_path.read_bytes()

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("simulated rename failure")

    # Path.replace dispatches to os.replace; patching the stdlib reference
    # makes the simulated mid-write failure visible to the handler.
    monkeypatch.setattr("os.replace", _boom)

    with pytest.raises(OSError, match="simulated rename failure"):
        switch_active_profile(new_profile=Profile.large, config_dir=config_dir)

    assert main_yaml_path.read_bytes() == pre_bytes


def test_rewrite_preserves_every_non_active_profile_field(config_dir: Path) -> None:
    """Atomic rewrite must leave execution_mode and every paths.* value intact."""
    pre = _load_main(config_dir)

    switch_active_profile(new_profile=Profile.small, config_dir=config_dir)

    post = _load_main(config_dir)
    assert post.active_profile == Profile.small
    assert post.execution_mode == pre.execution_mode == ExecutionMode.paper
    assert post.paths.database == pre.paths.database
    assert post.paths.logs == pre.paths.logs
    assert post.paths.archive == pre.paths.archive
    assert post.paths.prompts == pre.paths.prompts


def test_outcome_dataclass_is_frozen_and_uses_slots() -> None:
    """ProfileSwitchOutcome must be a frozen, slots=True dataclass.

    The dataclass shape is verified by ``__dataclass_fields__`` (documented
    attribute on every ``@dataclass``-decorated class). Frozen is verified by
    catching ``FrozenInstanceError`` on attribute assignment. Slots is verified
    by checking ``__slots__`` is defined and instances lack ``__dict__``.
    """
    assert hasattr(ProfileSwitchOutcome, "__dataclass_fields__")
    assert hasattr(ProfileSwitchOutcome, "__slots__")

    instance = ProfileSwitchOutcome(
        previous_profile=Profile.medium,
        new_profile=Profile.medium,
        main_yaml_path=Path("/dev/null"),
        is_no_op=True,
    )
    assert not hasattr(instance, "__dict__")

    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.__setattr__("is_no_op", False)


def test_profile_not_found_error_subclasses_lookup_error() -> None:
    """ProfileNotFoundError must subclass LookupError so HTTP 404 mapping fires."""
    assert issubclass(ProfileNotFoundError, LookupError)

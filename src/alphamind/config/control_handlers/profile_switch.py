"""Profile-switch handler backing ``POST /control/switch_profile`` (story 01d).

Validates that the requested profile name corresponds to an existing
``config/profiles/{name}.yaml`` file, atomically rewrites ``main.yaml``'s
``active_profile`` field while preserving comments and formatting via
``ruamel.yaml``, and returns a structured outcome carrying the previous and
new profile labels for downstream activity-log emission.

The change "takes effect at next invocation" per
``docs/design/pipeline-control-and-events-schema.md`` — the scheduler
re-reads the YAML tree before each invocation, so this handler's job is purely
the file edit, with no in-process hot-reload.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from alphamind._kernel.atomic_io import atomic_write_bytes
from alphamind.config.loaders import read_yaml_file
from alphamind.config.models.main import MainConfig, Profile


@dataclass(frozen=True, slots=True)
class ProfileSwitchOutcome:
    """Structured result of a profile-switch attempt.

    ``is_no_op`` is ``True`` when the requested profile equals the previous;
    in that case ``main.yaml`` is left byte-identical (no rewrite, no fsync),
    but the outcome is still returned so callers can record the operator
    action (e.g., "I confirmed the current profile") in the activity log.
    """

    previous_profile: Profile
    new_profile: Profile
    main_yaml_path: Path
    is_no_op: bool


class ProfileNotFoundError(LookupError):
    """Raised when the requested profile has no shipped YAML file.

    Subclasses ``LookupError`` so the upstream HTTP layer can map it to the
    ``not_found`` (HTTP 404) error code per
    ``docs/design/pipeline-control-and-events-schema.md`` § Per-verb summary.
    Distinct from ``FileNotFoundError`` (an ``OSError`` subclass), which would
    conflate the missing-name case with permission/IO failures.
    """


def switch_active_profile(*, new_profile: Profile, config_dir: Path) -> ProfileSwitchOutcome:
    """Rewrite ``main.yaml``'s ``active_profile`` field atomically.

    Steps:
    1. Read ``config_dir / "main.yaml"`` and parse it via ``MainConfig`` to
       discover the previous profile.
    2. Verify ``config_dir / "profiles" / f"{new_profile.value}.yaml"`` exists.
       Raise :class:`ProfileNotFoundError` if not.
    3. If the requested profile equals the previous, return a no-op outcome
       without touching the file.
    4. Otherwise, rewrite ``main.yaml`` atomically (tmp file → fsync → rename
       → fsync parent dir), preserving all other fields and comments via
       ``ruamel.yaml`` round-trip.
    """
    main_yaml_path = config_dir / "main.yaml"
    main_payload = read_yaml_file(main_yaml_path)
    previous_profile = MainConfig.model_validate(main_payload).active_profile

    target_profile_path = config_dir / "profiles" / f"{new_profile.value}.yaml"
    if not target_profile_path.exists():
        raise ProfileNotFoundError(
            f"Profile YAML not found for {new_profile.value!r}: {target_profile_path}"
        )

    if previous_profile == new_profile:
        return ProfileSwitchOutcome(
            previous_profile=previous_profile,
            new_profile=new_profile,
            main_yaml_path=main_yaml_path,
            is_no_op=True,
        )

    _rewrite_active_profile(main_yaml_path, new_profile)

    return ProfileSwitchOutcome(
        previous_profile=previous_profile,
        new_profile=new_profile,
        main_yaml_path=main_yaml_path,
        is_no_op=False,
    )


def _rewrite_active_profile(main_yaml_path: Path, new_profile: Profile) -> None:
    """Atomically rewrite ``main.yaml`` with the new ``active_profile``.

    Uses ``ruamel.yaml`` round-trip mode to preserve comments and formatting
    of every other field; delegates the durable rename to the kernel atomic-
    write helper (write to sibling tmp + fsync + replace + parent-dir fsync
    on POSIX).
    """
    yaml_rt = YAML(typ="rt")
    yaml_rt.preserve_quotes = True
    document = yaml_rt.load(main_yaml_path.read_text(encoding="utf-8"))
    document["active_profile"] = new_profile.value

    buffer = io.StringIO()
    yaml_rt.dump(document, buffer)
    atomic_write_bytes(main_yaml_path, buffer.getvalue().encode("utf-8"))

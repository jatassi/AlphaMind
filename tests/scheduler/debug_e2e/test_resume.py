"""Tests for ``scheduler/debug_e2e/resume.py`` (story ALP-693).

Covers:
- ``ResumeContext`` is a frozen + slotted dataclass with the three documented fields.
- ``_PHASE_DEPENDENCIES`` covers exactly the 9 SDK-phase names.
- ``phases_to_replay`` closure correctness for the three named targets (per AC).
- ``ResumeValidationError`` named-cause arms (missing dir, unknown phase,
  missing upstream phase output) and happy-path return shape.
- The missing-upstream error names the earliest valid resume target the
  source archive does cover.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# ResumeContext shape
# ---------------------------------------------------------------------------


def test_resume_context_is_frozen_and_slotted() -> None:
    """:class:`ResumeContext` is a frozen, slotted dataclass."""
    from alphamind.scheduler.debug_e2e.resume import ResumeContext

    ctx = ResumeContext(
        source_archive_dir=Path("/tmp/whatever"),
        resume_phase="analyst",
        phases_to_replay=frozenset(),
    )

    with pytest.raises(FrozenInstanceError):
        ctx.resume_phase = "pm"  # type: ignore[misc]

    # ``slots=True`` removes ``__dict__`` so unknown attribute names cannot
    # be silently attached.  Python 3.13 raises ``TypeError`` from
    # ``__setattr__`` super-call before reaching the frozen guard;
    # earlier minor versions raise ``AttributeError``.
    with pytest.raises((AttributeError, TypeError, FrozenInstanceError)):
        ctx.unknown_field = "x"  # type: ignore[attr-defined]

    assert hasattr(type(ctx), "__slots__")
    assert not hasattr(ctx, "__dict__")


def test_resume_context_has_three_named_fields() -> None:
    """:class:`ResumeContext` declares exactly the three story-named fields."""
    from alphamind.scheduler.debug_e2e.resume import ResumeContext

    field_names = tuple(f.name for f in fields(ResumeContext))
    assert field_names == ("source_archive_dir", "resume_phase", "phases_to_replay")


# ---------------------------------------------------------------------------
# Phase-dependency DAG
# ---------------------------------------------------------------------------


def test_phase_dependencies_covers_exactly_nine_sdk_phases() -> None:
    """``_PHASE_DEPENDENCIES`` has exactly one entry per SDK phase."""
    from alphamind.scheduler.debug_e2e.phase_outputs import SDK_PHASE_NAMES
    from alphamind.scheduler.debug_e2e.resume import _PHASE_DEPENDENCIES

    assert frozenset(_PHASE_DEPENDENCIES.keys()) == SDK_PHASE_NAMES
    assert len(_PHASE_DEPENDENCIES) == 9


def test_phase_dependencies_shape_matches_parent_design() -> None:
    """The DAG shape mirrors the parent design's composition shape exactly.

    See story ALP-693 description (Phase-dependency DAG) for the source
    of truth — this test pins the shape so a refactor of pipeline
    composition surfaces here at unit-test time, per parent decision
    "hard-coded DAG in resume.py".
    """
    from alphamind.scheduler.debug_e2e.resume import _PHASE_DEPENDENCIES

    domain_set = frozenset({"tech_semis", "financials", "energy"})

    assert _PHASE_DEPENDENCIES["tech_semis"] == frozenset()
    assert _PHASE_DEPENDENCIES["financials"] == frozenset()
    assert _PHASE_DEPENDENCIES["energy"] == frozenset()
    assert _PHASE_DEPENDENCIES["qualitative"] == frozenset()
    assert _PHASE_DEPENDENCIES["adaptive"] == domain_set | {"qualitative"}
    assert _PHASE_DEPENDENCIES["synthesizer"] == domain_set | {"qualitative", "adaptive"}
    assert _PHASE_DEPENDENCIES["analyst"] == frozenset({"synthesizer"})
    assert _PHASE_DEPENDENCIES["strategist"] == frozenset({"synthesizer"})
    assert _PHASE_DEPENDENCIES["pm"] == frozenset({"analyst", "strategist"})


# ---------------------------------------------------------------------------
# phases_to_replay closure correctness
# ---------------------------------------------------------------------------


def test_phases_to_replay_qualitative_is_empty() -> None:
    """``qualitative`` has no SDK upstreams → empty closure."""
    from alphamind.scheduler.debug_e2e.resume import phases_to_replay

    assert phases_to_replay("qualitative") == frozenset()


def test_phases_to_replay_for_tech_semis_is_empty() -> None:
    """``tech_semis`` (a domain researcher) has no SDK upstreams."""
    from alphamind.scheduler.debug_e2e.resume import phases_to_replay

    assert phases_to_replay("tech_semis") == frozenset()


def test_phases_to_replay_for_analyst_matches_ac() -> None:
    """``analyst`` closure == the six AC-named phases."""
    from alphamind.scheduler.debug_e2e.resume import phases_to_replay

    expected = frozenset(
        {
            "tech_semis",
            "financials",
            "energy",
            "qualitative",
            "adaptive",
            "synthesizer",
        }
    )
    assert phases_to_replay("analyst") == expected


def test_phases_to_replay_for_strategist_matches_analyst() -> None:
    """``strategist`` shares the analyst's six-upstream closure (parallel siblings)."""
    from alphamind.scheduler.debug_e2e.resume import phases_to_replay

    assert phases_to_replay("strategist") == phases_to_replay("analyst")


def test_phases_to_replay_for_pm_is_analyst_plus_analyst_strategist() -> None:
    """``pm`` closure == analyst's result + ``{analyst, strategist}``."""
    from alphamind.scheduler.debug_e2e.resume import phases_to_replay

    analyst_replay = phases_to_replay("analyst")
    assert phases_to_replay("pm") == analyst_replay | {"analyst", "strategist"}


def test_phases_to_replay_unknown_phase_raises_value_error() -> None:
    """An unknown phase raises ValueError naming the rejected phase."""
    from alphamind.scheduler.debug_e2e.resume import phases_to_replay

    with pytest.raises(ValueError, match="not_a_real_phase"):
        phases_to_replay("not_a_real_phase")


# ---------------------------------------------------------------------------
# ResumeValidationError
# ---------------------------------------------------------------------------


def test_resume_validation_error_carries_message() -> None:
    """``ResumeValidationError`` is a typed exception with a single message arg."""
    from alphamind.scheduler.debug_e2e.resume import ResumeValidationError

    err = ResumeValidationError("source archive missing")
    assert isinstance(err, Exception)
    assert str(err) == "source archive missing"


# ---------------------------------------------------------------------------
# load_resume_context — happy path
# ---------------------------------------------------------------------------


def _seed_source_archive(
    *,
    archive_root: Path,
    invocation_id: str,
    phase_files: list[str],
) -> Path:
    """Materialize an archive directory with the named phase_outputs files.

    The contents are non-empty JSON so that any incidental read attempt
    succeeds; this story does not validate file contents — only their
    presence.
    """
    invocation_dir = archive_root / "invocations" / invocation_id
    phase_outputs_dir = invocation_dir / "phase_outputs"
    phase_outputs_dir.mkdir(parents=True)
    for phase in phase_files:
        (phase_outputs_dir / f"{phase}.json").write_text("{}", encoding="utf-8")
    return invocation_dir


def test_load_resume_context_happy_path_returns_valid_context(
    tmp_path: Path,
) -> None:
    """A complete source archive yields a ResumeContext with the right fields."""
    from alphamind.scheduler.debug_e2e.resume import load_resume_context, phases_to_replay

    invocation_id = "inv-2026-05-26T00:00:00-abc"
    # analyst needs the 6 upstream SDK outputs present
    upstream = sorted(phases_to_replay("analyst"))
    invocation_dir = _seed_source_archive(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase_files=upstream,
    )

    ctx = load_resume_context(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase="analyst",
    )

    assert ctx.source_archive_dir == invocation_dir
    assert ctx.resume_phase == "analyst"
    assert ctx.phases_to_replay == phases_to_replay("analyst")


def test_load_resume_context_for_pm_requires_all_eight_upstreams(
    tmp_path: Path,
) -> None:
    """Resume target ``pm`` requires the eight SDK phases strictly upstream."""
    from alphamind.scheduler.debug_e2e.resume import load_resume_context, phases_to_replay

    invocation_id = "inv-pm-happy"
    upstream = sorted(phases_to_replay("pm"))
    _seed_source_archive(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase_files=upstream,
    )

    ctx = load_resume_context(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase="pm",
    )
    assert ctx.phases_to_replay == phases_to_replay("pm")
    assert len(ctx.phases_to_replay) == 8


# ---------------------------------------------------------------------------
# load_resume_context — error arms
# ---------------------------------------------------------------------------


def test_load_resume_context_missing_source_dir_raises(tmp_path: Path) -> None:
    """A missing source-invocation directory raises ``ResumeValidationError``."""
    from alphamind.scheduler.debug_e2e.resume import (
        ResumeValidationError,
        load_resume_context,
    )

    # No files seeded — invocations/<id> directory does not exist.
    with pytest.raises(ResumeValidationError, match="missing-inv"):
        load_resume_context(
            archive_root=tmp_path,
            invocation_id="missing-inv",
            phase="analyst",
        )


def test_load_resume_context_unknown_phase_raises(tmp_path: Path) -> None:
    """An unknown phase name raises ``ResumeValidationError`` naming the phase."""
    from alphamind.scheduler.debug_e2e.resume import (
        ResumeValidationError,
        load_resume_context,
    )

    invocation_id = "inv-unknown-phase"
    _seed_source_archive(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase_files=[],
    )

    with pytest.raises(ResumeValidationError, match="bogus_phase"):
        load_resume_context(
            archive_root=tmp_path,
            invocation_id=invocation_id,
            phase="bogus_phase",
        )


def test_load_resume_context_missing_upstream_raises(tmp_path: Path) -> None:
    """A missing upstream phase output raises ``ResumeValidationError``.

    Targeting analyst requires the 6 upstream SDK outputs.  Seed only 5
    of them — the missing ``synthesizer`` is the AC-named "upstream
    output missing" arm.
    """
    from alphamind.scheduler.debug_e2e.resume import (
        ResumeValidationError,
        load_resume_context,
    )

    invocation_id = "inv-missing-synthesizer"
    _seed_source_archive(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase_files=[
            "tech_semis",
            "financials",
            "energy",
            "qualitative",
            "adaptive",
            # missing: synthesizer
        ],
    )

    with pytest.raises(ResumeValidationError, match="synthesizer"):
        load_resume_context(
            archive_root=tmp_path,
            invocation_id=invocation_id,
            phase="analyst",
        )


def test_load_resume_context_missing_upstream_names_earliest_valid_target(
    tmp_path: Path,
) -> None:
    """The missing-upstream error names the earliest valid resume target.

    Per spec: when an upstream phase output is missing, the error names
    the earliest phase the operator could resume from given what the
    source archive actually covers.

    Setup: targeting ``pm``, but only the 4 leaf-SDK outputs are present.
    The missing phases are ``adaptive``, ``synthesizer``, ``analyst``,
    ``strategist``.  The earliest target whose own ``phases_to_replay``
    closure is fully satisfied by the archive is ``adaptive`` (its
    closure is {tech_semis, financials, energy, qualitative}).
    """
    from alphamind.scheduler.debug_e2e.resume import (
        ResumeValidationError,
        load_resume_context,
    )

    invocation_id = "inv-only-leaves"
    _seed_source_archive(
        archive_root=tmp_path,
        invocation_id=invocation_id,
        phase_files=["tech_semis", "financials", "energy", "qualitative"],
    )

    with pytest.raises(ResumeValidationError) as excinfo:
        load_resume_context(
            archive_root=tmp_path,
            invocation_id=invocation_id,
            phase="pm",
        )

    msg = str(excinfo.value)
    assert "adaptive" in msg, f"earliest-valid target 'adaptive' not in message: {msg}"


# ---------------------------------------------------------------------------
# DebugE2ESettings.resume_context extension
# ---------------------------------------------------------------------------


def test_debug_e2e_settings_carries_resume_context_field() -> None:
    """:class:`DebugE2ESettings` exposes a ``resume_context`` field."""
    from alphamind.scheduler.debug_e2e.settings import DebugE2ESettings

    field_names = tuple(f.name for f in fields(DebugE2ESettings))
    assert "resume_context" in field_names


def test_debug_e2e_settings_resume_context_defaults_to_none(tmp_path: Path) -> None:
    """``resume_context`` defaults to ``None`` when not supplied (no-resume runs)."""
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.settings import configure_debug_e2e

    settings = configure_debug_e2e(archive_root=tmp_path, portfolio=SYNTHETIC_PORTFOLIO)
    assert settings.resume_context is None


def test_configure_debug_e2e_threads_resume_context(tmp_path: Path) -> None:
    """``configure_debug_e2e(resume_context=...)`` threads the value onto the bundle."""
    from alphamind.scheduler.debug_e2e.portfolio import SYNTHETIC_PORTFOLIO
    from alphamind.scheduler.debug_e2e.resume import ResumeContext
    from alphamind.scheduler.debug_e2e.settings import configure_debug_e2e

    ctx = ResumeContext(
        source_archive_dir=tmp_path / "invocations" / "inv-source",
        resume_phase="analyst",
        phases_to_replay=frozenset(
            {"tech_semis", "financials", "energy", "qualitative", "adaptive", "synthesizer"}
        ),
    )

    settings = configure_debug_e2e(
        archive_root=tmp_path,
        portfolio=SYNTHETIC_PORTFOLIO,
        resume_context=ctx,
    )
    assert settings.resume_context is ctx

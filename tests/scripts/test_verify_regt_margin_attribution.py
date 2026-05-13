"""Tests for ``scripts/verify_regt_margin_attribution.py`` (ALP-430).

The verify script exercises the Reg T margin attribution work tree end-to-end
against a freshly-migrated SQLite DB: seeds a representative four-position
portfolio + two unprocessed fills, drives ``process_unprocessed_fills`` inside
an ``InvocationContext``, rehydrates the per-fill ``RegTMarginAttribution``
records, asserts the algebra (``regt_excess_over_pm == regt_marginal_consumption
- pm_marginal_consumption``), and compares the assembler's trailing-30d
aggregate to the per-fill sum.

The script's logic lives in ``alphamind.scripts.verify_regt_margin_attribution``
so tests can import it directly and inject a per-test on-disk SQLite DB; the
shim at ``scripts/verify_regt_margin_attribution.py`` defers to ``main()`` here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from alphamind._kernel.money import signed_money
from alphamind.persistence.models import Base
from alphamind.persistence.session import make_engine

_SHIM_PATH = Path(__file__).parents[2] / "scripts" / "verify_regt_margin_attribution.py"
_RUNBOOK_PATH = Path(__file__).parents[2] / "scripts" / "RUNBOOK_regt_margin_attribution.md"
_CENTRAL_RUNBOOK_PATH = Path(__file__).parents[2] / "scripts" / "RUNBOOK_end_to_end_verification.md"


@pytest.fixture()
def fresh_db(tmp_path: Path) -> Iterator[Path]:
    """Yield the path of a freshly-migrated on-disk SQLite DB (state-persistence schema applied)."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: registers state-persistence tables on Base.metadata.
    import alphamind.execution.state_persistence.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()
    yield db_path


# ---------------------------------------------------------------------------
# Tracer: shim loads and ``main`` returns 0 against a fresh DB
# ---------------------------------------------------------------------------


def test_shim_exists() -> None:
    assert _SHIM_PATH.exists(), f"Shim not found at {_SHIM_PATH}"


def test_runbook_exists() -> None:
    assert _RUNBOOK_PATH.exists(), f"Runbook not found at {_RUNBOOK_PATH}"


def test_main_returns_zero_on_fresh_db(fresh_db: Path) -> None:
    """Tracer: the script loads, runs all phases against a fresh DB, exits 0."""
    from alphamind.scripts.verify_regt_margin_attribution import main

    rc = main(["--db-path", str(fresh_db), "--invocation-id", "verify-regt-test-001"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Verify result shape: both fills surface attribution + algebra holds
# ---------------------------------------------------------------------------


async def test_run_verify_returns_two_attribution_rows(fresh_db: Path) -> None:
    """End-to-end: both seeded fills end with a populated attribution payload,
    in fill_timestamp order (buy NVDA before sell AMD)."""
    from alphamind.scripts.verify_regt_margin_attribution import run_verify

    result = await run_verify(fresh_db, invocation_id="verify-regt-attr-001")
    assert result.ok, result.failures
    assert len(result.attributions) == 2
    fill_ids = tuple(row.fill_id for row in result.attributions)
    assert fill_ids == ("verify-regt-fill-nvda-buy", "verify-regt-fill-amd-sell")


async def test_run_verify_algebra_holds_per_fill(fresh_db: Path) -> None:
    """For each fill: regt_excess_over_pm == regt_marginal_consumption - pm_marginal_consumption."""
    from alphamind.scripts.verify_regt_margin_attribution import run_verify

    result = await run_verify(fresh_db, invocation_id="verify-regt-alg-001")
    assert result.ok, result.failures
    for row in result.attributions:
        attr = row.attribution
        assert (
            abs(
                attr.regt_excess_over_pm
                - (attr.regt_marginal_consumption - attr.pm_marginal_consumption)
            )
            < 1e-6
        ), f"algebra violated for {row.fill_id}: {attr}"


async def test_run_verify_trailing_30d_equals_sum_of_per_fill_excess(fresh_db: Path) -> None:
    """The trailing-30d aggregate equals the sum of the per-fill regt_excess_over_pm."""
    from decimal import Decimal

    from alphamind.scripts.verify_regt_margin_attribution import run_verify

    result = await run_verify(fresh_db, invocation_id="verify-regt-agg-001")
    assert result.ok, result.failures
    # ALP-462 — ``regt_excess_over_pm`` is ``Money``; sum in Decimal space and
    # compare against the float trailing-30d aggregate via Decimal coercion.
    expected = sum(
        (row.attribution.regt_excess_over_pm for row in result.attributions),
        start=Decimal(0),
    )
    assert abs(Decimal(str(result.trailing_30d_usd)) - expected) < Decimal("1e-6")


async def test_run_verify_all_attribution_fields_finite(fresh_db: Path) -> None:
    """All 8 attribution fields (7 numeric + 1 version string) are well-formed per fill."""
    import math

    from alphamind.scripts.verify_regt_margin_attribution import run_verify

    result = await run_verify(fresh_db, invocation_id="verify-regt-finite-001")
    assert result.ok, result.failures
    for row in result.attributions:
        attr = row.attribution
        for value in (
            attr.regt_margin_before,
            attr.regt_margin_after,
            attr.regt_marginal_consumption,
            attr.pm_equivalent_before,
            attr.pm_equivalent_after,
            attr.pm_marginal_consumption,
            attr.regt_excess_over_pm,
        ):
            assert math.isfinite(value), f"non-finite field on {row.fill_id}: {attr}"
        assert isinstance(attr.pm_model_version, str)
        assert attr.pm_model_version, "pm_model_version must be non-empty"


# ---------------------------------------------------------------------------
# Structured-FAIL exit path: assertion-failure messages name the failing
# assertion in the runbook's failure-mode triage vocabulary.
# ---------------------------------------------------------------------------


def test_collect_assertion_failures_returns_empty_on_clean_pass() -> None:
    """The pure assertion-collector returns ``()`` when fields are finite,
    algebra holds, and the trailing-30d aggregate equals the sum."""
    from alphamind.execution.state_persistence.write_paths.records import (
        RegTMarginAttribution,
    )
    from alphamind.scripts.verify_regt_margin_attribution import (
        AttributionRow,
        _collect_assertion_failures,
    )

    attr_a = RegTMarginAttribution(
        regt_margin_before=signed_money(100.0),
        regt_margin_after=signed_money(150.0),
        regt_marginal_consumption=signed_money(50.0),
        pm_equivalent_before=signed_money(40.0),
        pm_equivalent_after=signed_money(60.0),
        pm_marginal_consumption=signed_money(20.0),
        regt_excess_over_pm=signed_money(30.0),
        pm_model_version="ibkr_mirror_v1_2026Q2",
    )
    attr_b = RegTMarginAttribution(
        regt_margin_before=signed_money(200.0),
        regt_margin_after=signed_money(180.0),
        regt_marginal_consumption=signed_money(-20.0),
        pm_equivalent_before=signed_money(80.0),
        pm_equivalent_after=signed_money(70.0),
        pm_marginal_consumption=signed_money(-10.0),
        regt_excess_over_pm=signed_money(-10.0),
        pm_model_version="ibkr_mirror_v1_2026Q2",
    )
    rows = (
        AttributionRow(fill_id="fill-a", attribution=attr_a),
        AttributionRow(fill_id="fill-b", attribution=attr_b),
    )
    failures = _collect_assertion_failures(rows, trailing_30d_usd=20.0)
    assert failures == ()


def test_collect_assertion_failures_flags_algebra_violation() -> None:
    """A regt_excess_over_pm not equal to (regt_marginal - pm_marginal) surfaces a
    structured FAIL message in the runbook's vocabulary."""
    from alphamind.execution.state_persistence.write_paths.records import (
        RegTMarginAttribution,
    )
    from alphamind.scripts.verify_regt_margin_attribution import (
        AttributionRow,
        _collect_assertion_failures,
    )

    broken = RegTMarginAttribution(
        regt_margin_before=signed_money(100.0),
        regt_margin_after=signed_money(150.0),
        regt_marginal_consumption=signed_money(50.0),
        pm_equivalent_before=signed_money(40.0),
        pm_equivalent_after=signed_money(60.0),
        pm_marginal_consumption=signed_money(20.0),
        # Algebra says this should be 30.0; we set 99.0 to force the assertion to fail.
        regt_excess_over_pm=signed_money(99.0),
        pm_model_version="ibkr_mirror_v1_2026Q2",
    )
    sane = RegTMarginAttribution(
        regt_margin_before=signed_money(200.0),
        regt_margin_after=signed_money(180.0),
        regt_marginal_consumption=signed_money(-20.0),
        pm_equivalent_before=signed_money(80.0),
        pm_equivalent_after=signed_money(70.0),
        pm_marginal_consumption=signed_money(-10.0),
        regt_excess_over_pm=signed_money(-10.0),
        pm_model_version="ibkr_mirror_v1_2026Q2",
    )
    rows = (
        AttributionRow(fill_id="fill-broken", attribution=broken),
        AttributionRow(fill_id="fill-sane", attribution=sane),
    )
    failures = _collect_assertion_failures(rows, trailing_30d_usd=89.0)
    assert any("algebra mismatch" in msg for msg in failures), failures
    assert any("fill-broken" in msg for msg in failures), failures


def test_collect_assertion_failures_flags_trailing_30d_mismatch() -> None:
    """A trailing-30d aggregate that disagrees with the sum of per-fill excess
    surfaces the runbook's 'trailing-30d aggregate mismatch' message."""
    from alphamind.execution.state_persistence.write_paths.records import (
        RegTMarginAttribution,
    )
    from alphamind.scripts.verify_regt_margin_attribution import (
        AttributionRow,
        _collect_assertion_failures,
    )

    attr = RegTMarginAttribution(
        regt_margin_before=signed_money(100.0),
        regt_margin_after=signed_money(150.0),
        regt_marginal_consumption=signed_money(50.0),
        pm_equivalent_before=signed_money(40.0),
        pm_equivalent_after=signed_money(60.0),
        pm_marginal_consumption=signed_money(20.0),
        regt_excess_over_pm=signed_money(30.0),
        pm_model_version="ibkr_mirror_v1_2026Q2",
    )
    rows = (
        AttributionRow(fill_id="fill-x", attribution=attr),
        AttributionRow(fill_id="fill-y", attribution=attr),
    )
    # Expected sum is 60.0; we pass 999.0 to force a mismatch.
    failures = _collect_assertion_failures(rows, trailing_30d_usd=999.0)
    assert any("trailing-30d aggregate" in msg for msg in failures), failures


# ---------------------------------------------------------------------------
# Runbook + central runbook insertions
# ---------------------------------------------------------------------------


def test_runbook_has_five_required_sections() -> None:
    """The feature runbook contains Purpose, Prerequisites, Invocation,
    Expected output, and Failure-mode triage — the five sections named in
    Scope §2 of the story."""
    body = _RUNBOOK_PATH.read_text()
    required_sections = (
        "## Purpose",
        "## Prerequisites",
        "## Invocation",
        "## Expected output",
        "## Failure-mode triage",
    )
    for section in required_sections:
        assert section in body, f"runbook missing section: {section}"


def test_runbook_documents_env_loading() -> None:
    """The runbook documents `source .env` (per memory feedback_handover_env_loading)."""
    body = _RUNBOOK_PATH.read_text()
    assert "source .env" in body, "runbook must document `source .env` step"


def test_central_runbook_contains_phase_entry_for_this_feature() -> None:
    """``RUNBOOK_end_to_end_verification.md`` has been extended with a Reg T
    margin attribution phase entry referencing the verify script."""
    body = _CENTRAL_RUNBOOK_PATH.read_text()
    assert "verify_regt_margin_attribution.py" in body, (
        "central runbook does not reference the verify script"
    )
    assert "RUNBOOK_regt_margin_attribution.md" in body, (
        "central runbook does not link to the feature runbook for triage"
    )


def test_central_runbook_phase_entry_positioned_after_corporate_actions() -> None:
    """The new phase entry comes after Phase 1e (corporate actions) and before
    Phase 2 (distillation), per Scope §3 of the story."""
    body = _CENTRAL_RUNBOOK_PATH.read_text()
    ca_pos = body.find("## Phase 1e — Corporate actions")
    regt_pos = body.find("verify_regt_margin_attribution.py")
    distillation_pos = body.find("## Phase 2 — Distillation layer")
    assert ca_pos != -1, "Phase 1e Corporate actions section missing"
    assert distillation_pos != -1, "Phase 2 Distillation section missing"
    assert regt_pos != -1, "Reg T margin attribution reference missing"
    assert ca_pos < regt_pos < distillation_pos, (
        f"positions: corporate_actions={ca_pos}, regt={regt_pos}, distillation={distillation_pos}"
    )

"""Unit tests for ``scripts/verify_domain_researcher_failure_modes.py``.

The failure-mode verification exercises three injection scenarios end-to-end
against a stubbed harness — no real SDK calls, deterministic. The tests
confirm each scenario's exit-code contract and that the diagnostic-archive
files are written for each injection.

Coverage map per ALP-186:

- Scenario 1: parse failure on first call → corrective retry succeeds → ``retry_count == 1``.
- Scenario 2: two consecutive parse failures → ``MalformedOutputFailure`` raised with
  the sector tag.
- Scenario 3: validation failure on first call → corrective retry succeeds → ``retry_count == 1``.
- All three scenarios passing → exit code 0.
- Per-scenario diagnostic archive written under ``invocations/<id>/analysis/<agent>/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alphamind.analysis._shared import Sector
from alphamind.analysis.domain_researchers.harness import MalformedOutputFailure
from alphamind.scripts.verify_domain_researcher_failure_modes import (
    main,
    make_parse_failure_then_success_stub,
    make_two_parse_failures_stub,
    make_validation_failure_then_success_stub,
    run_parse_failure_then_success_scenario,
    run_two_parse_failures_scenario,
    run_validation_failure_then_success_scenario,
)

# ---------------------------------------------------------------------------
# Tracer bullet — scenario 1: parse failure → corrective retry → success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_parse_failure_recovers_with_retry(tmp_path: Path) -> None:
    """Scenario 1: parse failure on the first call, success on the retry, ``retry_count == 1``."""
    outcome = await run_parse_failure_then_success_scenario(
        sector=Sector.TECH_SEMIS,
        invocation_id="inv-test-001",
        archive_root=tmp_path,
        sdk_query_fn=make_parse_failure_then_success_stub(),
    )

    assert outcome.passed is True
    assert outcome.retry_count == 1
    assert outcome.exception_type is None


# ---------------------------------------------------------------------------
# Scenario 2: two parse failures → MalformedOutputFailure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_two_parse_failures_propagates_malformed(tmp_path: Path) -> None:
    """Scenario 2: two consecutive parse failures surface ``MalformedOutputFailure``
    carrying the sector's agent-name tag."""
    outcome = await run_two_parse_failures_scenario(
        sector=Sector.TECH_SEMIS,
        invocation_id="inv-test-002",
        archive_root=tmp_path,
        sdk_query_fn=make_two_parse_failures_stub(),
    )

    assert outcome.passed is True
    assert outcome.exception_type is MalformedOutputFailure
    # The failure carries the sector's agent name (tech_semis_researcher).
    assert outcome.agent_name == "tech_semis_researcher"


# ---------------------------------------------------------------------------
# Scenario 3: validation failure → retry succeeds → retry_count == 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_validation_failure_recovers_with_retry(tmp_path: Path) -> None:
    """Scenario 3: validation failure on first call, clean response on retry."""
    outcome = await run_validation_failure_then_success_scenario(
        sector=Sector.TECH_SEMIS,
        invocation_id="inv-test-003",
        archive_root=tmp_path,
        sdk_query_fn=make_validation_failure_then_success_stub(),
    )

    assert outcome.passed is True
    assert outcome.retry_count == 1
    assert outcome.exception_type is None


# ---------------------------------------------------------------------------
# Diagnostic-archive presence per scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_scenario_writes_diagnostic_archive(tmp_path: Path) -> None:
    """Every scenario writes the diagnostic-archive files the harness produces.

    The harness writes ``prompt.md``, ``user_message.md``, ``response_initial.md``,
    ``errors.json`` and ``metadata.json`` at minimum; ``response_retry.md`` appears
    when a retry fires.
    """
    archive = tmp_path / "archive"
    inv_id_1 = "inv-archive-1"
    inv_id_2 = "inv-archive-2"
    inv_id_3 = "inv-archive-3"

    await run_parse_failure_then_success_scenario(
        sector=Sector.TECH_SEMIS,
        invocation_id=inv_id_1,
        archive_root=archive,
        sdk_query_fn=make_parse_failure_then_success_stub(),
    )
    await run_two_parse_failures_scenario(
        sector=Sector.FINANCIALS,
        invocation_id=inv_id_2,
        archive_root=archive,
        sdk_query_fn=make_two_parse_failures_stub(),
    )
    await run_validation_failure_then_success_scenario(
        sector=Sector.ENERGY,
        invocation_id=inv_id_3,
        archive_root=archive,
        sdk_query_fn=make_validation_failure_then_success_stub(),
    )

    diag_root = archive / "invocations"
    s1_dir = diag_root / inv_id_1 / "analysis" / "tech_semis_researcher"
    s2_dir = diag_root / inv_id_2 / "analysis" / "financials_researcher"
    s3_dir = diag_root / inv_id_3 / "analysis" / "energy_researcher"

    for d in (s1_dir, s2_dir, s3_dir):
        assert (d / "prompt.md").exists()
        assert (d / "user_message.md").exists()
        assert (d / "response_initial.md").exists()
        assert (d / "metadata.json").exists()


# ---------------------------------------------------------------------------
# main() exit codes
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_all_scenarios_pass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` returns 0 when every injected scenario behaves as expected."""
    exit_code = main(["--archive-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "RESULT: PASS" in captured.out


def test_main_emits_per_scenario_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``main`` prints a per-scenario summary line."""
    main(["--archive-root", str(tmp_path)])

    captured = capsys.readouterr()
    # Each scenario name appears in the rendered summary.
    assert "parse-failure-then-success" in captured.out
    assert "two-parse-failures" in captured.out
    assert "validation-failure-then-success" in captured.out

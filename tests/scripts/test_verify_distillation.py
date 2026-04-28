"""Unit tests for ``scripts/verify_distillation.py``.

The verification script is exercised through its public entry points
:func:`alphamind.scripts.verify_distillation.compute_report` and
:func:`alphamind.scripts.verify_distillation.format_report` against
mock orchestrator outputs and a mock SQLAlchemy session. The script does
NOT run the orchestrator itself in these tests — the orchestrator's own
contract is covered by ``tests/distillation/external/test_orchestrator.py``.

Coverage map per story 13:

- All-pass run: structural assertions hold; report exits 0.
- Missing sector output: structural assertion fails with a clear failure code.
- Missing archive file: structural assertion fails with a clear failure code.
- Missing state-table row: a per-table assertion fails, exit code 1.
- Placeholder-gap section is populated (q1/q3/q6/q7 narration is surfaced).
- Summary table renders the documented columns.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import (
    OutputAudience,
)
from alphamind.distillation.sector_assembly import SectorOutput
from alphamind.persistence.models import (
    AssetUniverse,
    DistillationCompositeState,
    DistillationContractHistory,
    DistillationEventHistory,
    DistillationPairLag,
    DistillationRegimeState,
    DistillationTickerBaseline,
    PredictionMarketContracts,
)
from alphamind.scripts.verify_distillation import (
    compute_report,
    format_report,
    run_verification,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_AS_OF = datetime(2026, 4, 25, 12, 0, 0, tzinfo=UTC)
_INVOCATION_ID = "20260425T120000Z-test"


def _make_sector_output(audience: OutputAudience, label: str) -> SectorOutput:
    return SectorOutput(
        audience=audience,
        sector_label=label,
        text=f"# Sector brief: {label}\n\nblock content.\n",
        tickers=("AAA", "BBB"),
        block_ids=("regime.label",),
        freshness_min=_AS_OF,
    )


def _make_brief(text: str = "Regime [CR-1]: vol_normalization.\n") -> CorrelationRegimeBrief:
    return CorrelationRegimeBrief(
        text=text,
        reference_index={"CR-1": "regime.label"},
        freshness_min=_AS_OF,
    )


def _make_outputs(
    *,
    include_all_sectors: bool = True,
    brief: CorrelationRegimeBrief | None = None,
    regime_label: str = "vol_normalization",
) -> DistillationOutputs:
    sector_outputs: dict[OutputAudience, SectorOutput] = {}
    if include_all_sectors:
        sector_outputs[OutputAudience.SECTOR_TECH_SEMIS] = _make_sector_output(
            OutputAudience.SECTOR_TECH_SEMIS, "Tech & Semis"
        )
        sector_outputs[OutputAudience.SECTOR_FINANCIALS] = _make_sector_output(
            OutputAudience.SECTOR_FINANCIALS, "Financials"
        )
        sector_outputs[OutputAudience.SECTOR_ENERGY] = _make_sector_output(
            OutputAudience.SECTOR_ENERGY, "Energy"
        )
    return DistillationOutputs(
        sector_outputs=sector_outputs,
        correlation_regime_brief=brief if brief is not None else _make_brief(),
        universal_regime_label={
            "regime_label": regime_label,
            "transition_state": "stable",
            "indicator_agreement_count": 4,
        },
        invocation_id=_INVOCATION_ID,
        as_of=_AS_OF,
        total_blocks=4,
        total_anomalies=1,
        bootstrap_block_count=1,
        all_blocks=(),
    )


def _seed_universe(session: Session, ticker: str = "AAA") -> None:
    session.add(
        AssetUniverse(
            asset_id=f"asset-{ticker.lower()}",
            ticker=ticker,
            full_name=f"{ticker} Holdings",
            asset_class="equity",
            asset_role="universe",
            exchange="NASDAQ",
            avg_daily_volume_shares=1_000_000,
            is_active=1,
            added_date="2020-01-01",
            last_updated="2026-04-26T00:00:00Z",
        )
    )
    session.commit()


def _seed_state_tables(
    session: Session,
    *,
    now: datetime,
    skip: tuple[str, ...] = (),
) -> None:
    """Seed at least one row in each distillation state table dated ``now``.

    ``skip`` is a tuple of table names to omit so a test can drive the
    "missing state-table row" failure.
    """
    _seed_universe(session, "AAA")
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    if "distillation_ticker_baseline" not in skip:
        session.add(
            DistillationTickerBaseline(
                ticker="AAA",
                baseline_kind="volume",
                as_of=ts,
                mean=1_000_000.0,
                stdev=100_000.0,
                n_observations=20,
                window_days=20,
                calibration_state="calibrated",
                ingested_at=ts,
            )
        )
    if "distillation_pair_lag" not in skip:
        session.add(
            AssetUniverse(
                asset_id="asset-bbb",
                ticker="BBB",
                full_name="BBB",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                avg_daily_volume_shares=1_000_000,
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
        session.add(
            DistillationPairLag(
                lead_ticker="AAA",
                lag_ticker="BBB",
                as_of=ts,
                lead_lag_days_estimate=1.0,
                n_pair_events=10,
                last_overdue_flag=0,
                calibration_state="calibrated",
                ingested_at=ts,
            )
        )
    if "distillation_contract_history" not in skip:
        session.add(
            PredictionMarketContracts(
                contract_id="C1",
                platform="polymarket",
                description="Will X happen?",
                category="politics",
                resolution_date="2026-12-31",
                resolution_outcome=None,
                created_at=ts,
                last_seen_at=ts,
            )
        )
        session.flush()
        session.add(
            DistillationContractHistory(
                contract_id="C1",
                snapshot_ts=ts,
                yes_probability=0.5,
                delta_pp_since_prior=0.0,
                liquidity_usd=10_000.0,
                calibration_state="bootstrap",
                ingested_at=ts,
            )
        )
    if "distillation_event_history" not in skip:
        session.add(
            DistillationEventHistory(
                ticker="AAA",
                event_kind="gap",
                event_ts=ts,
                direction="up",
                magnitude_atr_multiple=2.0,
                outcome="filled_same_session",
                outcome_observed_at=ts,
                ingested_at=ts,
            )
        )
    if "distillation_regime_state" not in skip:
        session.add(
            DistillationRegimeState(
                as_of=ts,
                regime_label="vol_normalization",
                vix_level=18.0,
                term_structure_basis=0.5,
                vvix_percentile=50.0,
                realized_vol=12.0,
                indicator_agreement_count=3,
                invocations_held=1,
                transition_state="stable",
                prior_label="vol_normalization",
                ingested_at=ts,
            )
        )
    if "distillation_composite_state" not in skip:
        session.add(
            DistillationCompositeState(
                composite_kind="funding_stress",
                as_of=ts,
                composite_value=0.5,
                component_breakdown_json="{}",
                percentile_60d=50.0,
                alert_active=0,
                calibration_state="calibrated",
                ingested_at=ts,
            )
        )
    session.commit()


def _write_archive(archive_dir: Path, *, missing: tuple[str, ...] = ()) -> Path:
    """Write the five expected archive files; ``missing`` names files to skip."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    files = (
        "tech_semis_sector.md",
        "financials_sector.md",
        "energy_sector.md",
        "correlation_regime_brief.md",
        "regime.md",
    )
    for fname in files:
        if fname in missing:
            continue
        (archive_dir / fname).write_text(f"content of {fname}\n", encoding="utf-8")
    return archive_dir


# ---------------------------------------------------------------------------
# Tracer-bullet tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def populated_session_factory(engine: Engine) -> Any:
    """Factory that yields a fresh session bound to the shared engine."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def _make() -> Session:
        return factory()

    return _make


def test_compute_report_passes_when_all_assertions_hold(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """All structural assertions hold → report.passed is True, no failures."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive")
    outputs = _make_outputs()

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )

    assert report.passed is True
    assert report.failures == ()


def test_missing_sector_output_fails_with_documented_code(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """A missing sector audience surfaces as a ``sector-outputs-missing`` failure."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive")
    outputs = _make_outputs(include_all_sectors=False)

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "sector-outputs-missing" in codes


def test_missing_archive_file_fails(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """A missing archive file surfaces as ``archive-file-missing``."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive", missing=("correlation_regime_brief.md",))
    outputs = _make_outputs()

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "archive-file-missing" in codes
    assert "correlation_regime_brief.md" in report.archive_files_missing


def test_stale_state_table_fails(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """A state table with no rows in the freshness window surfaces ``state-table-stale``."""
    sess = populated_session_factory()
    # Seed every state table EXCEPT regime_state. The regime state row will
    # be absent → freshness probe sees no row → fail.
    _seed_state_tables(sess, now=_AS_OF, skip=("distillation_regime_state",))
    archive_dir = _write_archive(tmp_path / "archive")
    outputs = _make_outputs()

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )

    assert report.passed is False
    failures_with_table = [f for f in report.failures if f.code == "state-table-stale"]
    assert any("distillation_regime_state" in f.message for f in failures_with_table)


def test_unknown_regime_label_fails(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """An out-of-vocabulary regime label surfaces ``regime-label-unknown``."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive")
    outputs = _make_outputs(regime_label="not_a_real_label")

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "regime-label-unknown" in codes


def test_missing_cr1_reference_fails(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """A correlation/regime brief without ``[CR-1]`` surfaces ``cr-brief-missing-cr1-reference``."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive")
    brief = _make_brief(text="Just narrative text without a marker.\n")
    outputs = _make_outputs(brief=brief)

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )

    assert report.passed is False
    codes = {f.code for f in report.failures}
    assert "cr-brief-missing-cr1-reference" in codes


def test_format_report_renders_summary_table(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """The formatted summary contains every documented section."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive")
    outputs = _make_outputs()

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )
    rendered = format_report(report)

    # Banner + every documented section heading.
    assert "AlphaMind Distillation End-to-End Verification" in rendered
    assert "[ Sector outputs ]" in rendered
    assert "[ Universal regime ]" in rendered
    assert "[ Aggregate counts ]" in rendered
    assert "[ State-table freshness ]" in rendered
    assert "[ Invocation archive ]" in rendered
    assert "[ PLACEHOLDER GAPS ]" in rendered
    # Final result line.
    assert "RESULT: PASS" in rendered


def test_placeholder_gaps_section_renders_all_integrated_when_table_empty(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """With every Phase-2 category integrated, the section renders an "all integrated" line."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive")
    outputs = _make_outputs()

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )
    rendered = format_report(report)

    # All four follow-up entry points landed (q1/q3/q6/q7), so the table
    # is empty. The verification script renders an "all integrated"
    # placeholder line in that case so the section shape stays stable.
    assert report.placeholder_gaps == ()
    assert "[ PLACEHOLDER GAPS ]" in rendered
    assert "all six Phase 2 categories integrated" in rendered


def test_format_report_failed_run_reports_failures(
    populated_session_factory: Any,
    tmp_path: Path,
) -> None:
    """A failed run renders ``RESULT: FAIL`` and lists every failure code."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_dir = _write_archive(tmp_path / "archive", missing=("regime.md",))
    outputs = _make_outputs()

    report = compute_report(
        outputs=outputs,
        session=sess,
        archive_dir=archive_dir,
        now=_AS_OF,
    )
    rendered = format_report(report)

    assert "RESULT: FAIL" in rendered
    assert "[ Failures ]" in rendered
    assert "archive-file-missing" in rendered


# ---------------------------------------------------------------------------
# run_verification — wires up the orchestrator and returns an exit code
# ---------------------------------------------------------------------------


def test_run_verification_returns_zero_on_pass(
    populated_session_factory: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_verification`` returns 0 when every assertion passes.

    The orchestrator is mocked so the test does not exercise its DB
    surface a second time — that's already covered by
    ``tests/distillation/external/test_orchestrator.py``. The verification
    script's job is to assert against the orchestrator's outputs, not
    re-test the orchestrator.
    """
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_root = tmp_path / "archive_root"
    archive_dir = archive_root / _AS_OF.strftime("%Y-%m-%d") / _INVOCATION_ID / "distillation"
    _write_archive(archive_dir)

    outputs = _make_outputs()

    async def _fake_run_external_distillation(*_args: Any, **_kwargs: Any) -> DistillationOutputs:
        return outputs

    exit_code = run_verification(
        session=sess,
        ticker_scope=("AAA", "BBB"),
        archive_root=archive_root,
        as_of=_AS_OF,
        invocation_id=_INVOCATION_ID,
        orchestrator=_fake_run_external_distillation,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "RESULT: PASS" in captured.out


def test_run_verification_returns_one_on_fail(
    populated_session_factory: Any,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run_verification`` returns 1 when any assertion fails."""
    sess = populated_session_factory()
    _seed_state_tables(sess, now=_AS_OF)
    archive_root = tmp_path / "archive_root"
    # Don't write archive files at all → archive-file-missing failure.

    outputs = _make_outputs()

    async def _fake_run_external_distillation(*_args: Any, **_kwargs: Any) -> DistillationOutputs:
        return outputs

    exit_code = run_verification(
        session=sess,
        ticker_scope=("AAA", "BBB"),
        archive_root=archive_root,
        as_of=_AS_OF,
        invocation_id=_INVOCATION_ID,
        orchestrator=_fake_run_external_distillation,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "RESULT: FAIL" in captured.out

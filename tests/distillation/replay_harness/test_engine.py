"""Tests for the replay execution engine — story 02-distillation-layer/replay-harness/05.

Exercises ``replay_slice`` against a synthesized fixture slice that lives entirely
under ``tmp_path`` (no committed binary fixtures). The synthesized slice carries a
manifest, a SQLite snapshot of the raw-input tables the orchestrator reads, and a
small number of invocation timestamps spaced one hour apart. The candidate config
is the canonical ``config/distillation.yaml``.
"""

from __future__ import annotations

import dataclasses
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from alphamind._kernel.ids import Symbol
from alphamind.distillation.calibration import CalibrationState
from alphamind.distillation.correlation_brief import CorrelationRegimeBrief
from alphamind.distillation.orchestrator import DistillationOutputs
from alphamind.distillation.output import (
    AnomalyFlag,
    OutputAudience,
    OutputBlock,
)
from alphamind.distillation.replay_harness.candidate_config import (
    LoadedCandidateConfig,
    load_candidate_config,
)
from alphamind.distillation.replay_harness.engine import (
    SliceReplayResult,
    _extract_anomaly_flags,
    _extract_class_b_baselines,
    _migrate_isolated_db,
    replay_slice,
)
from tests.distillation.replay_harness.conftest import (
    build_synthesized_slice as _build_synthesized_slice,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CANONICAL_CONFIG_PATH = REPO_ROOT / "config" / "distillation.yaml"


@pytest.fixture
def candidate() -> LoadedCandidateConfig:
    return load_candidate_config(CANONICAL_CONFIG_PATH)


# ---------------------------------------------------------------------------
# Tracer bullet — replay_slice exists and returns the documented shape
# ---------------------------------------------------------------------------


def test_replay_slice_returns_slice_replay_result_shape(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """Tracer bullet: ``replay_slice`` returns a ``SliceReplayResult`` with one invocation."""
    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)

    result = replay_slice(fixture_slice, candidate)

    assert isinstance(result, SliceReplayResult)
    assert result.slice_id == fixture_slice.manifest.slice_id
    assert result.regime_label == fixture_slice.manifest.regime_label
    assert result.config_content_hash == candidate.content_hash
    assert len(result.invocations) == 1


# ---------------------------------------------------------------------------
# Per-invocation outputs — five invocations one hour apart
# ---------------------------------------------------------------------------


def test_replay_slice_returns_one_invocation_per_manifest_timestamp(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """A 5-invocation slice produces a 5-entry ``invocations`` tuple ordered by ``as_of``."""
    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=5, days_of_history=30)

    result = replay_slice(fixture_slice, candidate)

    assert len(result.invocations) == 5
    timestamps = [inv.as_of for inv in result.invocations]
    assert timestamps == sorted(timestamps), "invocations not ordered by as_of"


def test_invocation_id_follows_documented_pattern(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """Synthesized invocation_id is ``replay_<slice_id>_<as_of_compact>``."""
    fixture_slice = _build_synthesized_slice(
        tmp_path,
        invocation_count=1,
        days_of_history=30,
        slice_id="synthetic_normal_b",
    )

    result = replay_slice(fixture_slice, candidate)

    invocation = result.invocations[0]
    expected_compact = invocation.as_of.strftime("%Y%m%dT%H%M%SZ")
    assert invocation.invocation_id == f"replay_synthetic_normal_b_{expected_compact}"


def test_invocation_carries_regime_label_and_transition_state(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """The orchestrator's universal regime label flows into ``InvocationOutputs``.

    The synthesized fixture seeds VIX in the normal band; the orchestrator's
    regime classifier returns ``normal``. The transition state on a
    first-of-its-kind invocation is ``confirmed`` (initial bootstrap).
    """
    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)

    result = replay_slice(fixture_slice, candidate)
    invocation = result.invocations[0]

    assert invocation.regime_label  # non-empty
    assert invocation.regime_transition_state in {
        "stable",
        "early-weak",
        "early-strong",
        "confirmed",
    }


# ---------------------------------------------------------------------------
# Class B baselines populated from the slice's own history
# ---------------------------------------------------------------------------


def test_class_b_baselines_populated_from_slice_history(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """The Class B refresh primitives populate ``class_b_baseline_values``.

    The harness leaves the distillation state tables empty; the orchestrator's
    Phase 1 refresh fills them from the slice's OHLCV / macro / event history.
    A slice with a few weeks of bars produces baseline rows for at least the
    seeded sector tickers.
    """
    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)

    result = replay_slice(fixture_slice, candidate)
    invocation = result.invocations[0]

    assert len(invocation.class_b_baseline_values) > 0
    seen_baseline_kinds = {record.baseline_kind for record in invocation.class_b_baseline_values}
    # The Phase 1 refresh covers the volume / ATR / spread / sentiment kinds
    # for the per-ticker baseline plus pair_lag for lead-lag pairs. The
    # sentiment baseline may be empty when no news rows seed the slice; the
    # volume + ATR pair is the always-on signal that proves the refresh ran.
    assert "volume" in seen_baseline_kinds
    assert "atr" in seen_baseline_kinds


# ---------------------------------------------------------------------------
# Archive-write suppression — the operator's home archive is never touched
# ---------------------------------------------------------------------------


def test_replay_does_not_write_to_operator_archive_root(
    tmp_path: Path,
    candidate: LoadedCandidateConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The orchestrator's archive write lands in the harness temp dir, not the operator's home.

    Re-routes ``$HOME`` (and ``%USERPROFILE%``) to a sentinel directory so the
    orchestrator's :func:`_default_archive_root` would resolve under the
    sentinel if the harness failed to override it. Asserts that
    ``<sentinel>/AlphaMind/archive`` and ``<sentinel>/AlphaMind/data/provenance``
    are absent after the replay.
    """
    sentinel_home = tmp_path / "operator_home"
    sentinel_home.mkdir()
    monkeypatch.setenv("HOME", str(sentinel_home))
    monkeypatch.setenv("USERPROFILE", str(sentinel_home))

    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)

    replay_slice(fixture_slice, candidate)

    operator_archive = sentinel_home / "AlphaMind" / "archive"
    operator_provenance = sentinel_home / "AlphaMind" / "data" / "provenance"
    assert not operator_archive.exists(), (
        f"replay touched operator archive root at {operator_archive}"
    )
    assert not operator_provenance.exists(), (
        f"replay touched operator provenance root at {operator_provenance}"
    )


def test_replay_succeeds_when_runtime_database_path_missing(
    tmp_path: Path,
    candidate: LoadedCandidateConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolation invariant: the engine never reads the runtime DB.

    The test points ``DATABASE_PATH`` at a file that does not exist. If the
    engine accidentally reaches for the runtime session factory the SQLite
    open would fail. The call must succeed because the engine builds its
    own in-temp-dir SQLite and never resolves the runtime path.
    """
    runtime_db = tmp_path / "does_not_exist.sqlite"
    assert not runtime_db.exists()
    monkeypatch.setenv("DATABASE_PATH", str(runtime_db))

    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)

    result = replay_slice(fixture_slice, candidate)

    assert len(result.invocations) == 1
    assert not runtime_db.exists(), "engine touched the runtime DB path"


# ---------------------------------------------------------------------------
# Determinism — two replays produce equal results
# ---------------------------------------------------------------------------


def test_replay_slice_is_deterministic_across_two_calls(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """Two ``replay_slice`` calls on the same ``(slice, candidate)`` produce equal results.

    The orchestrator's structured-text rendering plus the harness's
    extraction layer must be deterministic — same inputs yield the same
    captured anomaly tuples / baseline tuples / regime labels.
    """
    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=2, days_of_history=30)

    first = replay_slice(fixture_slice, candidate)
    second = replay_slice(fixture_slice, candidate)

    assert dataclasses.asdict(first) == dataclasses.asdict(second)


# ---------------------------------------------------------------------------
# Orchestrator failure — exception propagates and temp dir is cleaned up
# ---------------------------------------------------------------------------


def test_orchestrator_exception_propagates_with_cleanup(
    tmp_path: Path,
    candidate: LoadedCandidateConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fault-injected per-category exception escapes ``replay_slice``.

    The harness does not catch and continue; subsequent slices (story 08)
    handle per-slice failure at a higher layer. The temp dir is torn down
    via :func:`_rmtree_with_retry` after the orchestrator unwinds — verified
    by patching :func:`tempfile.mkdtemp` to record the path it produces, then
    asserting that path no longer exists once the exception unwinds.
    """
    from alphamind.distillation import orchestrator as orch_mod

    class _InjectedReplayError(RuntimeError):
        """Marker exception so the assertion is unambiguous."""

    def _failing_q12(*_args: object, **_kwargs: object) -> object:
        raise _InjectedReplayError("simulated orchestrator failure")

    monkeypatch.setattr(orch_mod, "_compute_q12_blocks", _failing_q12)

    # Capture the path of every temp directory the harness opens during the
    # call. Wraps the real :func:`tempfile.mkdtemp` so the production cleanup
    # path runs unchanged.
    opened_paths: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def _tracking_mkdtemp(prefix: str | None = None) -> str:
        produced = real_mkdtemp(prefix=prefix)
        opened_paths.append(Path(produced))
        return produced

    monkeypatch.setattr(tempfile, "mkdtemp", _tracking_mkdtemp)

    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)

    with pytest.raises(_InjectedReplayError, match="simulated orchestrator failure"):
        replay_slice(fixture_slice, candidate)

    assert opened_paths, "harness did not open a temp directory during replay"
    for path in opened_paths:
        assert not path.exists(), f"harness left temp directory behind: {path}"


# ---------------------------------------------------------------------------
# Different config — looser sigma threshold produces fewer (or equal) anomalies
# ---------------------------------------------------------------------------


def _build_looser_candidate(tmp_path: Path) -> LoadedCandidateConfig:
    """Return a candidate-config snapshot with a substantially looser volume sigma."""
    import yaml

    raw = yaml.safe_load(CANONICAL_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["anomaly_detection"]["volume_anomaly_sigma"] = 5.0
    looser_path = tmp_path / "looser_distillation.yaml"
    looser_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return load_candidate_config(looser_path)


def _count_flag_type(result: SliceReplayResult, flag_type: str) -> int:
    """Count anomaly flags of ``flag_type`` across every invocation in ``result``."""
    return sum(
        1
        for invocation in result.invocations
        for flag in invocation.anomaly_flags
        if flag.flag_type == flag_type
    )


def test_looser_sigma_produces_fewer_or_equal_anomalies(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """A more permissive ``volume_anomaly_sigma`` produces at most as many flags.

    Per ``feedback_avoid_numeric_anchors.md``, this is a structural sanity
    check (more permissive threshold ⇒ fewer flags), not a calibration
    target — assert the directional comparison only, never an absolute count.
    """
    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=1, days_of_history=30)
    looser = _build_looser_candidate(tmp_path)

    baseline = replay_slice(fixture_slice, candidate)
    relaxed = replay_slice(fixture_slice, looser)

    baseline_volume_count = _count_flag_type(baseline, "volume_anomaly")
    relaxed_volume_count = _count_flag_type(relaxed, "volume_anomaly")
    assert relaxed_volume_count <= baseline_volume_count, (
        f"relaxed sigma produced more anomalies ({relaxed_volume_count} > {baseline_volume_count})"
    )


# ---------------------------------------------------------------------------
# Anomaly dedup — same flag in two blocks collapses to one record
# ---------------------------------------------------------------------------


def _make_outputs_with_blocks(*blocks: OutputBlock, anomaly_count: int) -> DistillationOutputs:
    """Build a :class:`DistillationOutputs` carrying ``blocks`` for extractor tests."""
    return DistillationOutputs(
        sector_outputs={},
        correlation_regime_brief=CorrelationRegimeBrief(
            text="",
            reference_index={},
            freshness_min=datetime(2026, 4, 25, tzinfo=UTC),
        ),
        universal_regime_label={"regime_label": "normal", "transition_state": "stable"},
        invocation_id="test",
        as_of=datetime(2026, 4, 25, tzinfo=UTC),
        total_blocks=len(blocks),
        total_anomalies=anomaly_count,
        non_calibrated_block_count=0,
        all_blocks=blocks,
    )


def test_extract_anomaly_flags_deduplicates_by_flag_type_and_entity() -> None:
    """``_extract_anomaly_flags`` collapses duplicates by ``(flag_type, entity_key)``.

    Constructs two distinct :class:`OutputBlock` instances carrying the same
    ``AnomalyFlag`` (same name including the entity suffix). The extractor
    must keep one record, not two, regardless of which block it iterated
    first.
    """
    flag = AnomalyFlag(
        name="overdue_lag_flag:HYG_SPY",
        magnitude=2.5,
        severity="investigate_now",
    )
    freshness_ts = datetime(2026, 4, 25, tzinfo=UTC)
    sector_block = OutputBlock(
        block_id="q7.lead_lag",
        audience=frozenset({OutputAudience.SECTOR_FINANCIALS}),
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={},
        anomaly_flags=(flag,),
        regime_context=None,
    )
    cr_block = OutputBlock(
        block_id="q7.lead_lag",
        audience=frozenset({OutputAudience.CORRELATION_REGIME_BRIEF}),
        freshness_ts=freshness_ts,
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={},
        anomaly_flags=(flag,),
        regime_context=None,
    )

    records = _extract_anomaly_flags(
        _make_outputs_with_blocks(sector_block, cr_block, anomaly_count=2)
    )

    assert len(records) == 1
    assert records[0].flag_type == "overdue_lag_flag"
    assert records[0].ticker_or_pair_key == "HYG_SPY"


def test_extract_anomaly_flags_handles_bare_flag_name_without_entity() -> None:
    """A flag whose name carries no ``:`` separator records ``ticker_or_pair_key=None``."""
    block = OutputBlock(
        block_id="q1.volume_anomaly",
        audience=frozenset({OutputAudience.SECTOR_TECH_SEMIS}),
        freshness_ts=datetime(2026, 4, 25, tzinfo=UTC),
        calibration_state=CalibrationState.CALIBRATED,
        bootstrap_reason=None,
        payload={},
        anomaly_flags=(
            AnomalyFlag(name="volume_anomaly", magnitude=3.0, severity="investigate_now"),
        ),
        regime_context=None,
    )

    records = _extract_anomaly_flags(_make_outputs_with_blocks(block, anomaly_count=1))

    assert len(records) == 1
    assert records[0].flag_type == "volume_anomaly"
    assert records[0].ticker_or_pair_key is None


# ---------------------------------------------------------------------------
# Logging — start / per-invocation / end lines emit
# ---------------------------------------------------------------------------


def test_replay_emits_per_slice_and_per_invocation_log_lines(
    tmp_path: Path,
    candidate: LoadedCandidateConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One INFO at slice start, one per invocation, one at slice end."""
    from alphamind.distillation.replay_harness import engine as engine_mod

    fixture_slice = _build_synthesized_slice(tmp_path, invocation_count=2, days_of_history=30)

    captured: list[str] = []

    class _SpyLogger:
        """Mirror enough of :class:`logging.Logger` for the engine's call sites."""

        def info(self, msg: str, *args: object, **_kwargs: object) -> None:
            captured.append(msg % args if args else msg)

        def __getattr__(self, name: str) -> object:
            # Forward unused level methods (warning/error) to a no-op so the
            # spy stays drop-in for any other logger calls the engine adds.
            return lambda *_a, **_kw: None

    monkeypatch.setattr(engine_mod, "logger", _SpyLogger())

    replay_slice(fixture_slice, candidate)

    start_lines = [m for m in captured if m.startswith("replay slice start")]
    invocation_lines = [m for m in captured if m.startswith("replay invocation complete")]
    end_lines = [m for m in captured if m.startswith("replay slice end")]
    assert len(start_lines) == 1, f"expected one slice-start INFO, got {captured!r}"
    assert len(invocation_lines) == 2, f"expected two per-invocation INFO, got {captured!r}"
    assert len(end_lines) == 1, f"expected one slice-end INFO, got {captured!r}"


# ---------------------------------------------------------------------------
# _extract_class_b_baselines — each of the four state tables surfaces
# ---------------------------------------------------------------------------


def test_extract_class_b_baselines_queries_each_state_table(tmp_path: Path) -> None:
    """The extractor pulls rows from all four Class B state tables.

    Builds an isolated session, seeds one row in each of the four state
    tables (ticker baseline, pair lag, contract history, event history)
    keyed at a known ``as_of``, and verifies the extractor returns one
    record per table with the documented ``baseline_kind`` shape.
    """
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from alphamind.persistence.models import (
        AssetUniverse,
        DistillationContractHistory,
        DistillationEventHistory,
        DistillationPairLag,
        DistillationTickerBaseline,
        PredictionMarketContracts,
    )

    isolated_db = tmp_path / "isolated.sqlite"
    _migrate_isolated_db(isolated_db)

    engine = create_engine(f"sqlite:///{isolated_db}")

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    as_of = datetime(2026, 4, 25, 12, tzinfo=UTC)
    as_of_iso = as_of.strftime("%Y-%m-%dT%H:%M:%SZ")
    ingested_at_iso = as_of_iso

    with factory() as session:
        session.add(
            AssetUniverse(
                asset_id="asset-aapl",
                ticker=Symbol("AAPL"),
                full_name="Apple",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
        session.add(
            AssetUniverse(
                asset_id="asset-msft",
                ticker=Symbol("MSFT"),
                full_name="Microsoft",
                asset_class="equity",
                asset_role="universe",
                exchange="NASDAQ",
                is_active=1,
                added_date="2020-01-01",
                last_updated="2026-04-26T00:00:00Z",
            )
        )
        session.add(
            PredictionMarketContracts(
                contract_id="POL-CON-A",
                platform="polymarket",
                description="Test contract",
                category="general",
                created_at="2026-04-26T00:00:00Z",
                last_seen_at="2026-04-26T00:00:00Z",
            )
        )
        session.commit()

        session.add(
            DistillationTickerBaseline(
                ticker=Symbol("AAPL"),
                baseline_kind="volume",
                as_of=as_of_iso,
                mean=1_000_000.0,
                stdev=10000.0,
                n_observations=20,
                window_days=20,
                calibration_state="calibrated",
                ingested_at=ingested_at_iso,
            )
        )
        session.add(
            DistillationPairLag(
                lead_ticker="AAPL",
                lag_ticker="MSFT",
                as_of=as_of_iso,
                lead_lag_days_estimate=1.5,
                n_pair_events=5,
                last_overdue_flag=0,
                calibration_state="calibrated",
                ingested_at=ingested_at_iso,
            )
        )
        session.add(
            DistillationContractHistory(
                contract_id="POL-CON-A",
                snapshot_ts=as_of_iso,
                yes_probability=0.62,
                delta_pp_since_prior=0.5,
                liquidity_usd=15000.0,
                calibration_state="calibrated",
                ingested_at=ingested_at_iso,
            )
        )
        session.add(
            DistillationEventHistory(
                ticker=Symbol("AAPL"),
                event_kind="gap",
                event_ts=as_of_iso,
                direction="up",
                magnitude_atr_multiple=2.0,
                outcome="filled",
                ingested_at=ingested_at_iso,
            )
        )
        session.commit()

        records = _extract_class_b_baselines(session, as_of)

    engine.dispose()

    by_kind = {record.baseline_kind: record for record in records}
    assert "volume" in by_kind
    assert "pair_lag" in by_kind
    assert "contract_history" in by_kind
    assert "event_history_gap" in by_kind
    assert by_kind["pair_lag"].entity_key == "AAPL_MSFT"
    assert by_kind["contract_history"].entity_key == "POL-CON-A"


# ---------------------------------------------------------------------------
# Empty / minimal slice — Class B baselines lean toward bootstrap
# ---------------------------------------------------------------------------


def test_minimal_slice_yields_few_anomalies_and_bootstrap_baselines(
    tmp_path: Path, candidate: LoadedCandidateConfig
) -> None:
    """A slice with only a handful of bars produces mostly-bootstrap baselines.

    The Class B refresh tags a baseline ``BOOTSTRAP`` until enough
    observations accumulate (story 04). A 5-day slice falls below the
    20-day volume baseline minimum, so the volume baseline carries
    ``bootstrap`` calibration. Anomaly counts are typically zero.
    """
    fixture_slice = _build_synthesized_slice(
        tmp_path,
        invocation_count=1,
        days_of_history=5,
        slice_id="synthetic_normal_minimal",
    )

    result = replay_slice(fixture_slice, candidate)
    invocation = result.invocations[0]

    volume_baselines = [
        record for record in invocation.class_b_baseline_values if record.baseline_kind == "volume"
    ]
    assert volume_baselines, "expected at least one volume baseline row"
    assert all(record.calibration_state == "accumulating" for record in volume_baselines), (
        "minimal slice should produce bootstrap-tagged volume baselines, got "
        f"{[r.calibration_state for r in volume_baselines]}"
    )

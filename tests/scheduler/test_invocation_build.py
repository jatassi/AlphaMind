"""Tests for ``alphamind.scheduler.invocation.build_invocation_record`` (story 03a).

The helper composes all 22 ``InvocationRecord`` fields from caller-supplied
trigger metadata, runtime dimensions, the resolved :class:`PipelineConfig`,
and the freshness/calibration state derived from the session and archive.
Each test pins one populated column (or one ``None`` column) so a regression
in any per-field population is caught immediately.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from alphamind.config import load_full_config
from alphamind.config.load import PipelineConfig
from alphamind.config.models.modes import Mode
from alphamind.config.models.overlays import Overlay
from alphamind.config.models.regimes import Regime
from alphamind.config.models.run_types import RunType
from alphamind.config.resolver import RuntimeDimensions
from alphamind.persistence.models import Base, CollectionRuns
from alphamind.persistence.session import (
    make_async_engine,
    make_async_session_factory,
    make_engine,
)
from alphamind.scheduler.invocation import (
    _compute_data_source_freshness_json,
    _persist_data_calibration_snapshot,
    build_invocation_record,
)
from alphamind.state.invocation_context.records import (
    InvocationRecord,
)

REPO_ROOT = Path(__file__).parent.parent.parent
SHIPPED_CONFIG_DIR = REPO_ROOT / "config"

_VENUE_ENV_KEYS: tuple[str, ...] = (
    "ALPACA_PAPER_KEY",
    "ALPACA_PAPER_SECRET",
    "ALPACA_LIVE_KEY",
    "ALPACA_LIVE_SECRET",
)


def _write_placeholder_env(env_path: Path) -> None:
    env_path.write_text("\n".join(f"{key}=placeholder" for key in _VENUE_ENV_KEYS) + "\n")


@pytest.fixture
def env_path(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    _write_placeholder_env(path)
    return path


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    return tmp_path / "archive"


def _read_repo_head_sha() -> str:
    """Return the SHA at ``HEAD`` for the repository hosting these tests.

    Wrapped in a sync helper so async tests can call it without tripping the
    ``ASYNC221`` rule against blocking subprocess calls in ``async`` bodies.
    """
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.strip()


def _baseline_runtime() -> RuntimeDimensions:
    return RuntimeDimensions(
        active_regime=Regime.normal,
        active_mode=Mode.normal,
        active_overlays=(),
        firing_trigger=RunType.pre_open,
    )


_AS_OF = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)


@pytest.fixture
def pipeline_config(env_path: Path, archive_root: Path) -> PipelineConfig:
    """Load the shipped config tree once for tests that don't need to vary it."""
    return load_full_config(
        config_dir=SHIPPED_CONFIG_DIR,
        env_path=env_path,
        archive_root=archive_root,
        invocation_id="inv-build-fixture",
        runtime=_baseline_runtime(),
        today=_AS_OF.date(),
        as_of=_AS_OF,
    )


@pytest.fixture
async def async_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Yield an async session factory bound to a freshly initialized SQLite DB."""
    db_path = tmp_path / "alphamind.db"

    # Side-effect import: register the state-persistence tables on Base.metadata
    # (needed when build_invocation_record's freshness query touches collection_runs
    # and we want all tables created up front).
    import alphamind.state.tables  # noqa: F401

    sync_engine = make_engine(str(db_path))
    try:
        Base.metadata.create_all(sync_engine)
    finally:
        sync_engine.dispose()

    async_engine: AsyncEngine = make_async_engine(str(db_path))
    factory = make_async_session_factory(async_engine)
    try:
        yield factory
    finally:
        await async_engine.dispose()


async def _build(
    *,
    pipeline_config: PipelineConfig,
    archive_root: Path,
    session: AsyncSession,
    runtime: RuntimeDimensions | None = None,
    invocation_id: str = "inv-build-test",
    now: datetime | None = None,
) -> InvocationRecord:
    """Convenience wrapper that pins all the inputs each test does not vary."""
    return await build_invocation_record(
        session=session,
        process_lifetime_id="proc-build-1",
        trigger_type="scheduled",
        trigger_source="morning-cron",
        trigger_reason="0 9 * * 1-5",
        firing_run_type=RunType.pre_open,
        runtime=runtime if runtime is not None else _baseline_runtime(),
        pipeline_config=pipeline_config,
        archive_root=archive_root,
        invocation_id=invocation_id,
        now=now if now is not None else datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC),
    )


class TestBuildInvocationRecord:
    async def test_populates_start_at_from_now_with_z_suffix(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
            )

        assert record.start_at == "2026-05-07T14:30:00Z"

    async def test_mode_normal_translates_to_active_mode_normal(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
                runtime=RuntimeDimensions(
                    active_regime=Regime.normal,
                    active_mode=Mode.normal,
                    active_overlays=(),
                    firing_trigger=RunType.pre_open,
                ),
            )

        assert record.active_mode == "normal"

    async def test_mode_halt_translates_to_active_mode_halted(
        self,
        env_path: Path,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        # Reload config under (medium, halt, (), pre_open) — the cascade
        # composes a different snapshot for halt mode.
        halt_runtime = RuntimeDimensions(
            active_regime=Regime.normal,
            active_mode=Mode.halt,
            active_overlays=(),
            firing_trigger=RunType.pre_open,
        )
        halt_config = load_full_config(
            config_dir=SHIPPED_CONFIG_DIR,
            env_path=env_path,
            archive_root=archive_root,
            invocation_id="inv-build-halt-fixture",
            runtime=halt_runtime,
            today=_AS_OF.date(),
            as_of=_AS_OF,
        )

        async with async_factory() as session:
            record = await _build(
                pipeline_config=halt_config,
                archive_root=archive_root,
                session=session,
                runtime=halt_runtime,
            )

        assert record.active_mode == "halted"

    async def test_active_overlays_json_serializes_runtime_overlays(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        runtime = RuntimeDimensions(
            active_regime=Regime.normal,
            active_mode=Mode.normal,
            active_overlays=(Overlay.pre_event, Overlay.stress),
            firing_trigger=RunType.pre_open,
        )

        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
                runtime=runtime,
            )

        assert record.active_overlays_json == '["pre_event", "stress"]'

    async def test_active_overlays_json_empty_when_no_overlays(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
            )

        assert record.active_overlays_json == "[]"

    async def test_feature_flags_snapshot_json_is_sorted_json_of_snapshot_flags(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
            )

        expected = json.dumps(pipeline_config.snapshot.feature_flags_snapshot, sort_keys=True)
        assert record.feature_flags_snapshot_json == expected

    async def test_git_sha_at_invocation_matches_repo_head(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        expected_sha = _read_repo_head_sha()

        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
            )

        assert record.git_sha_at_invocation == expected_sha

    async def test_git_rev_parse_nonzero_exit_raises_called_process_error(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force ``git rev-parse HEAD`` to fail and confirm the error propagates.

        Patches ``subprocess.run`` on the module so the failure surfaces from
        the helper without depending on the host filesystem layout.
        """

        def _failing_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.CalledProcessError(
                returncode=128, cmd=["git", "rev-parse", "HEAD"], stderr="fatal"
            )

        monkeypatch.setattr("alphamind.scheduler.invocation.subprocess.run", _failing_run)

        async with async_factory() as session:
            with pytest.raises(subprocess.CalledProcessError):
                await _build(
                    pipeline_config=pipeline_config,
                    archive_root=archive_root,
                    session=session,
                )

    async def test_data_source_freshness_json_populated_from_session(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """build_invocation_record runs the freshness query against the supplied session."""
        async with async_factory() as session:
            session.add(
                CollectionRuns(
                    run_id="r-poly-build",
                    collector="polygon.equity",
                    started_at="2026-05-07T09:55:00Z",
                    completed_at="2026-05-07T10:00:00Z",
                    status="success",
                )
            )
            await session.commit()

        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
            )

        payload = json.loads(record.data_source_freshness_json)
        assert payload["polygon"] == "2026-05-07T10:00:00Z"

    async def test_data_calibration_state_snapshot_path_under_archive_root(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """build_invocation_record persists the calibration snapshot under archive_root."""
        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
                invocation_id="inv-20260507T143000Z-feedface",
            )

        expected = (
            archive_root
            / "2026-05-07"
            / "inv-20260507T143000Z-feedface"
            / "data_calibration_state.json"
        )
        assert Path(record.data_calibration_state_snapshot_path) == expected
        assert expected.exists()

    async def test_record_has_22_fields_with_correct_none_set(
        self,
        pipeline_config: PipelineConfig,
        archive_root: Path,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """All 22 fields populated; 5 designated columns are None at insert."""
        async with async_factory() as session:
            record = await _build(
                pipeline_config=pipeline_config,
                archive_root=archive_root,
                session=session,
            )

        # The five fields that must be ``None`` at insert per the spec.
        expected_none = {
            "phase1_completed_at",
            "phase2_completed_at",
            "fill_collection_summary_json",
            "command_execution_summary_json",
            "staleness_flag",
            "snapshot_metadata_json",
        }
        record_fields = record.model_dump(mode="json")
        assert set(record_fields) == {
            "invocation_id",
            "process_lifetime_id",
            "start_at",
            "phase1_completed_at",
            "phase2_completed_at",
            "trigger_type",
            "trigger_source",
            "trigger_reason",
            "git_sha_at_invocation",
            "active_profile",
            "active_regime",
            "active_mode",
            "active_overlays_json",
            "resolved_config_hash",
            "resolved_config_snapshot_path",
            "feature_flags_snapshot_json",
            "data_calibration_state_snapshot_path",
            "data_source_freshness_json",
            "fill_collection_summary_json",
            "command_execution_summary_json",
            "staleness_flag",
            "snapshot_metadata_json",
        }
        for field_name, value in record_fields.items():
            if field_name in expected_none:
                assert value is None, f"{field_name} must be None at insert; got {value!r}"
            else:
                assert value is not None, f"{field_name} must be populated; got None"


class TestPersistDataCalibrationSnapshot:
    # Canonical as_of for tests in this class; date partition is "2026-05-07".
    _AS_OF_CURRENT = datetime(2026, 5, 7, 14, 30, 0, tzinfo=UTC)

    def test_writes_empty_object_when_no_prior_snapshot_exists(
        self,
        tmp_path: Path,
    ) -> None:
        archive_root = tmp_path / "archive"
        invocation_id = "inv-20260507T143000Z-abcdef01"

        result = _persist_data_calibration_snapshot(
            archive_root=archive_root,
            invocation_id=invocation_id,
            as_of=self._AS_OF_CURRENT,
        )

        expected = archive_root / "2026-05-07" / invocation_id / "data_calibration_state.json"
        assert result == expected
        assert result.exists()
        assert result.read_text() == "{}"

    def test_copies_prior_snapshot_content_when_one_exists(
        self,
        tmp_path: Path,
    ) -> None:
        archive_root = tmp_path / "archive"
        # Prior invocation lives under its own date partition (date-partitioned layout).
        prior_id = "inv-20260506T143000Z-deadbeef"
        prior_dir = archive_root / "2026-05-06" / prior_id
        prior_dir.mkdir(parents=True)
        prior_content = '{"schema_version": "1", "summary": {"total_blocks": 7}}'
        (prior_dir / "data_calibration_state.json").write_text(prior_content)

        current_id = "inv-20260507T143000Z-cafef00d"
        result = _persist_data_calibration_snapshot(
            archive_root=archive_root,
            invocation_id=current_id,
            as_of=self._AS_OF_CURRENT,
        )

        assert result == archive_root / "2026-05-07" / current_id / "data_calibration_state.json"
        assert result.read_text() == prior_content

    def test_picks_lexicographically_greatest_prior_id_when_multiple_priors(
        self,
        tmp_path: Path,
    ) -> None:
        archive_root = tmp_path / "archive"

        # Three prior invocations with deterministic id-suffixes; the
        # lexicographically-greatest matches the chronologically-most-recent
        # because the id prefix sorts on the timestamp. Each prior lives under
        # its own date partition (date-partitioned canonical layout).
        priors = {
            "inv-20260505T090000Z-00000001": ("2026-05-05", '{"label": "oldest"}'),
            "inv-20260506T090000Z-00000002": ("2026-05-06", '{"label": "middle"}'),
            "inv-20260507T090000Z-00000003": ("2026-05-07", '{"label": "newest"}'),
        }
        for prior_id, (date_part, content) in priors.items():
            d = archive_root / date_part / prior_id
            d.mkdir(parents=True)
            (d / "data_calibration_state.json").write_text(content)

        current_id = "inv-20260507T143000Z-feedface"
        result = _persist_data_calibration_snapshot(
            archive_root=archive_root,
            invocation_id=current_id,
            as_of=self._AS_OF_CURRENT,
        )

        assert result.read_text() == '{"label": "newest"}'

    def test_skips_current_invocation_directory_during_scan(
        self,
        tmp_path: Path,
    ) -> None:
        """A pre-existing current-invocation directory must not be used as the source."""
        archive_root = tmp_path / "archive"
        current_id = "inv-20260507T143000Z-feedface"
        prior_id = "inv-20260506T090000Z-deadbeef"

        # Even though the current_id sorts greater, the scan must skip it.
        current_dir = archive_root / "2026-05-07" / current_id
        current_dir.mkdir(parents=True)
        (current_dir / "data_calibration_state.json").write_text('{"stale": "from-self"}')

        prior_dir = archive_root / "2026-05-06" / prior_id
        prior_dir.mkdir(parents=True)
        prior_content = '{"label": "expected-prior"}'
        (prior_dir / "data_calibration_state.json").write_text(prior_content)

        result = _persist_data_calibration_snapshot(
            archive_root=archive_root,
            invocation_id=current_id,
            as_of=self._AS_OF_CURRENT,
        )

        assert result.read_text() == prior_content

    def test_ignores_non_invocation_id_subdirectories(
        self,
        tmp_path: Path,
    ) -> None:
        """Directories whose names do not match the invocation-id regex are skipped."""
        archive_root = tmp_path / "archive"

        # Bogus directory with a calibration file under a date partition — must not be selected.
        bogus = archive_root / "2026-05-07" / "not-an-invocation-id"
        bogus.mkdir(parents=True)
        (bogus / "data_calibration_state.json").write_text('{"label": "bogus"}')

        current_id = "inv-20260507T143000Z-cafef00d"
        result = _persist_data_calibration_snapshot(
            archive_root=archive_root,
            invocation_id=current_id,
            as_of=self._AS_OF_CURRENT,
        )

        # No legitimate prior, so the file must be a fresh "{}".
        assert result.read_text() == "{}"


class TestComputeDataSourceFreshnessJson:
    async def test_empty_db_returns_every_provider_mapped_to_null(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            freshness_json = await _compute_data_source_freshness_json(session)

        payload = json.loads(freshness_json)
        # Provider list mirrors config/data_sources.yaml's top-level providers map.
        expected_providers = {
            "alpaca",
            "bls",
            "eia",
            "finnhub",
            "finra",
            "fred",
            "iborrowdesk",
            "kalshi",
            "marketaux",
            "polygon",
            "polymarket",
            "sec_edgar",
            "stocktwits",
            "treasury",
        }
        assert set(payload) == expected_providers
        assert all(value is None for value in payload.values())

    async def test_keys_sorted_alphabetically(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        async with async_factory() as session:
            freshness_json = await _compute_data_source_freshness_json(session)

        # ``sort_keys=True`` produces keys in lexicographic order regardless of
        # insertion order — assert that the raw JSON string carries them sorted.
        payload = json.loads(freshness_json)
        keys = list(payload)
        assert keys == sorted(keys)

    async def test_returns_most_recent_completed_at_per_provider(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        polygon_today = "2026-05-07T14:00:00Z"
        fred_yesterday = "2026-05-06T22:00:00Z"

        async with async_factory() as session:
            session.add(
                CollectionRuns(
                    run_id="r-polygon-1",
                    collector="polygon.equity",
                    started_at="2026-05-07T13:55:00Z",
                    completed_at=polygon_today,
                    status="success",
                )
            )
            session.add(
                CollectionRuns(
                    run_id="r-fred-1",
                    collector="fred.macro",
                    started_at="2026-05-06T21:55:00Z",
                    completed_at=fred_yesterday,
                    status="success",
                )
            )
            await session.commit()

        async with async_factory() as session:
            freshness_json = await _compute_data_source_freshness_json(session)

        payload = json.loads(freshness_json)
        assert payload["polygon"] == polygon_today
        assert payload["fred"] == fred_yesterday
        # Providers without runs remain null.
        assert payload["eia"] is None

    async def test_only_success_rows_count_toward_freshness(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """A failed or running row must not influence the freshness timestamp."""
        async with async_factory() as session:
            # Running row (no completed_at) and failed row — both must be skipped.
            session.add(
                CollectionRuns(
                    run_id="r-poly-running",
                    collector="polygon.options",
                    started_at="2026-05-07T15:00:00Z",
                    completed_at=None,
                    status="running",
                )
            )
            session.add(
                CollectionRuns(
                    run_id="r-poly-failed",
                    collector="polygon.options",
                    started_at="2026-05-07T15:05:00Z",
                    completed_at="2026-05-07T15:06:00Z",
                    status="failed",
                )
            )
            # One successful row — only this one should set the freshness.
            session.add(
                CollectionRuns(
                    run_id="r-poly-success",
                    collector="polygon.options",
                    started_at="2026-05-06T12:55:00Z",
                    completed_at="2026-05-06T12:56:00Z",
                    status="success",
                )
            )
            await session.commit()

        async with async_factory() as session:
            freshness_json = await _compute_data_source_freshness_json(session)

        payload = json.loads(freshness_json)
        assert payload["polygon"] == "2026-05-06T12:56:00Z"

    async def test_multiple_collectors_under_one_provider_picks_most_recent(
        self,
        async_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        """``polygon.options`` and ``polygon.equity`` both roll up under ``polygon``."""
        async with async_factory() as session:
            session.add(
                CollectionRuns(
                    run_id="r-poly-equity",
                    collector="polygon.equity",
                    started_at="2026-05-06T09:00:00Z",
                    completed_at="2026-05-06T09:05:00Z",
                    status="success",
                )
            )
            session.add(
                CollectionRuns(
                    run_id="r-poly-options",
                    collector="polygon.options",
                    started_at="2026-05-07T11:00:00Z",
                    completed_at="2026-05-07T11:05:00Z",
                    status="success",
                )
            )
            await session.commit()

        async with async_factory() as session:
            freshness_json = await _compute_data_source_freshness_json(session)

        payload = json.loads(freshness_json)
        assert payload["polygon"] == "2026-05-07T11:05:00Z"

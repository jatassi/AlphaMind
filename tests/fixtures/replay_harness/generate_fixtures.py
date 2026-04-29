"""One-shot, deterministic generator for the replay-harness E2E fixture slices.

Run from the repo root:

    python tests/fixtures/replay_harness/generate_fixtures.py

Produces two committed SQLite snapshots under
``tests/fixtures/replay_harness/fixture_store/{regime}/{slice}/`` that the
E2E test (``tests/distillation/replay_harness/test_e2e.py``) replays
against. The script is operator-invoked when the runtime schema or fixture
format changes — `test_generator_output_matches_committed_fixture` is the
drift canary that surfaces a missed re-run.

Determinism contract: identical inputs produce byte-identical output.
The script seeds a stdlib :class:`random.Random` per slice and writes the
in-memory database out via SQLite ``VACUUM INTO`` so internal pages are
compact and stable across re-runs.
"""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alphamind.distillation.replay_harness.engine import _migrate_isolated_db

FIXTURES_ROOT = Path(__file__).resolve().parent
FIXTURE_STORE_ROOT = FIXTURES_ROOT / "fixture_store"

# ---------------------------------------------------------------------------
# Universe — five tickers covering Q1/Q3/Q6/Q7 plus the lead-lag proxy ETFs
# the orchestrator's correlation primitives expect. Kept small so each slice
# stays well under 1 MB while still producing every code path.
# ---------------------------------------------------------------------------

SECTOR_TICKERS: dict[str, tuple[str, str, str]] = {
    "NVDA": ("semis", "tech_semis", "SMH"),
    "AAPL": ("tech", "tech_semis", "XLK"),
    "JPM": ("financials", "financials", "XLF"),
    "XOM": ("energy", "energy", "XLE"),
    "SPY": ("market", "market", "SPY"),
}
LEAD_LAG_TICKERS: tuple[str, ...] = ("HYG", "SOXX", "QQQ", "USO", "XLK", "XLF", "XLE", "SMH")
ALL_TICKERS: tuple[str, ...] = tuple(SECTOR_TICKERS) + LEAD_LAG_TICKERS

# Macro series the orchestrator's regime classifier reads. Constant per slice
# except for VIX which carries the regime signal.
MACRO_NON_VIX_SERIES: dict[str, float] = {
    "T10YIE": 2.4,
    "DGS3MO": 4.5,
    "DGS2": 4.6,
    "DGS5": 4.4,
    "DGS10": 4.3,
    "DGS30": 4.6,
}

# Slice shape — invocations are spaced one trading day apart and history
# extends 80 calendar days back so the longest persistence window
# (correlation_long_days = 60) is fully covered.
INVOCATION_COUNT = 25
DAYS_OF_HISTORY = 80
SLICE_END_UTC = datetime(2026, 4, 25, 14, 30, 0, tzinfo=UTC)
SLICE_CREATED_AT = "2026-04-28T00:00:00Z"

# Per-regime VIX bands and volume-volatility levels. The candidate config's
# volume_anomaly_sigma = 2.5 versus the baseline's 4.0 produces visibly
# different flag rates because the per-ticker volume distribution carries
# enough spread to straddle 2.5 sigma but not 4.0.
LOW_VOL_VIX_MEAN = 12.5
LOW_VOL_VOLUME_REL_STD = 0.05
NORMAL_VIX_MEAN = 17.5
NORMAL_VOLUME_REL_STD = 0.05

# Deliberate volume-spike multiple injected on the normal slice's spike days
# so the candidate config's volume_anomaly_sigma=2.5 fires while the baseline
# config's 4.0 stays silent. Sized so the resulting z-score (computed against
# a trailing-20 baseline that includes the spike day) lands above 2.5 but
# below 4.0 across the seeded history.
NORMAL_SPIKE_MULTIPLE = 1.18
# Day offsets (from history's last day, which is the day before slice end)
# at which volume spikes are planted on the normal slice's primary ticker.
# Chosen so each lands on a trading-calendar invocation day — only those
# days show up in the orchestrator's "today bar" — and outside the
# trailing-20 warmup so the baseline is fully populated.
NORMAL_SPIKE_DAY_OFFSETS: frozenset[int] = frozenset({0, 3, 7, 14, 21})
# The ticker the spikes are planted on — picked from the universe so the
# sector-level Q1 anomaly check sees them.
NORMAL_SPIKE_TICKER = "NVDA"

BASE_VOLUME_SHARES = 1_000_000
BASE_PRICE = 100.0


@dataclass(frozen=True)
class SliceSpec:
    """Per-slice generator parameters."""

    slice_id: str
    regime_label: str
    vix_mean: float
    volume_rel_std: float
    rng_seed: int
    spike_day_offsets: frozenset[int]
    spike_ticker: str | None


SLICE_SPECS: tuple[SliceSpec, ...] = (
    SliceSpec(
        slice_id="slice_e2e_lowvol_0001",
        regime_label="low_vol",
        vix_mean=LOW_VOL_VIX_MEAN,
        volume_rel_std=LOW_VOL_VOLUME_REL_STD,
        rng_seed=20260428,
        spike_day_offsets=frozenset(),
        spike_ticker=None,
    ),
    SliceSpec(
        slice_id="slice_e2e_normal_0001",
        regime_label="normal",
        vix_mean=NORMAL_VIX_MEAN,
        volume_rel_std=NORMAL_VOLUME_REL_STD,
        rng_seed=20260429,
        spike_day_offsets=NORMAL_SPIKE_DAY_OFFSETS,
        spike_ticker=NORMAL_SPIKE_TICKER,
    ),
)


def _trading_day_calendar(end: datetime, *, count: int) -> list[datetime]:
    """Return ``count`` trading-day timestamps ending at ``end`` (inclusive)."""
    days: list[datetime] = []
    cursor = end
    while len(days) < count:
        if cursor.weekday() < 5:  # Mon-Fri
            days.append(cursor)
        cursor = cursor - timedelta(days=1)
    days.reverse()
    return days


def _history_calendar(end: datetime, *, days: int) -> list[datetime]:
    """Return ``days`` consecutive calendar-day midnights ending one day before ``end``."""
    return [
        (end - timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        for offset in range(days, 0, -1)
    ]


def _insert_assets(conn: sqlite3.Connection) -> None:
    """Insert ``asset_universe`` + ``sector_classification`` rows for every ticker."""
    for ticker, (alphamind_sector, domain_researcher, etf) in SECTOR_TICKERS.items():
        conn.execute(
            "INSERT INTO asset_universe (asset_id, ticker, full_name, asset_class, "
            "asset_role, exchange, avg_daily_volume_shares, is_active, added_date, "
            "last_updated) VALUES (?, ?, ?, 'equity', 'universe', 'NASDAQ', "
            "1000000, 1, '2020-01-01', '2026-04-26T00:00:00Z')",
            (f"asset-{ticker.lower()}", ticker, f"{ticker} Holdings"),
        )
        conn.execute(
            "INSERT INTO sector_classification (ticker, asset_id, alphamind_sector, "
            "domain_researcher, sector_etf, classification_source, last_updated) "
            "VALUES (?, ?, ?, ?, ?, 'fixture', '2026-04-26T00:00:00Z')",
            (ticker, f"asset-{ticker.lower()}", alphamind_sector, domain_researcher, etf),
        )
    for ticker in LEAD_LAG_TICKERS:
        conn.execute(
            "INSERT INTO asset_universe (asset_id, ticker, full_name, asset_class, "
            "asset_role, exchange, avg_daily_volume_shares, is_active, added_date, "
            "last_updated) VALUES (?, ?, ?, 'etf', 'proxy', 'NYSE', 10000000, 1, "
            "'2020-01-01', '2026-04-26T00:00:00Z')",
            (f"asset-{ticker.lower()}", ticker, f"{ticker} ETF"),
        )


def _insert_ohlcv(
    conn: sqlite3.Connection,
    *,
    history: Sequence[datetime],
    rng: random.Random,
    spec: SliceSpec,
) -> None:
    """Insert ``ohlcv_bars`` for every ticker across ``history`` days.

    Both ``adj_*`` and ``unadj_*`` paired columns are populated per
    ``storage.md`` § Cross-cutting rules. Price walks deterministically with
    the seeded RNG; volume carries per-day jitter at the spec'd relative
    standard deviation. On the spike ticker (when set), specific day offsets
    receive a deterministic ``NORMAL_SPIKE_MULTIPLE`` boost so the candidate
    config's volume_anomaly_sigma=2.5 fires there while the baseline's 4.0
    stays silent — that's the source of the diff-mode delta the E2E test
    asserts is non-zero.
    """
    end_day = history[-1]
    spike_dates: set[str] = (
        {
            (end_day - timedelta(days=offset)).strftime("%Y-%m-%dT00:00:00Z")
            for offset in spec.spike_day_offsets
        }
        if spec.spike_ticker is not None
        else set()
    )
    for ticker in ALL_TICKERS:
        price = BASE_PRICE
        for bar_date in history:
            price = price + rng.gauss(0.0, 0.5)
            volume_jitter = rng.gauss(1.0, spec.volume_rel_std)
            base_volume = BASE_VOLUME_SHARES * volume_jitter
            day_iso = bar_date.strftime("%Y-%m-%dT00:00:00Z")
            if ticker == spec.spike_ticker and day_iso in spike_dates:
                base_volume *= NORMAL_SPIKE_MULTIPLE
            volume = max(int(base_volume), 1)
            conn.execute(
                "INSERT INTO ohlcv_bars (ticker, period_start, timeframe, period_end, "
                "session, adj_open, adj_high, adj_low, adj_close, adj_volume, adj_vwap, "
                "unadj_open, unadj_high, unadj_low, unadj_close, unadj_volume, "
                "unadj_vwap, trade_count, source, ingested_at) VALUES (?, ?, '1d', ?, "
                "'regular', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100, 'fixture', ?)",
                (
                    ticker,
                    bar_date.strftime("%Y-%m-%dT00:00:00Z"),
                    bar_date.strftime("%Y-%m-%dT23:59:59Z"),
                    price,
                    price + 1.0,
                    price - 1.0,
                    price + 0.5,
                    volume,
                    price,
                    price,
                    price + 1.0,
                    price - 1.0,
                    price + 0.5,
                    volume,
                    price,
                    bar_date.strftime("%Y-%m-%dT00:00:00Z"),
                ),
            )


def _insert_macro(
    conn: sqlite3.Connection,
    *,
    history: Sequence[datetime],
    vix_mean: float,
    rng: random.Random,
) -> None:
    """Insert ``macro_observations`` rows.

    VIX walks around ``vix_mean`` with small jitter so the regime
    classifier consistently picks the slice's exemplary regime. Other
    series stay near their typical levels — the harness only reads VIX,
    DGS curves, and breakeven from this table.
    """
    for ts in history:
        date_str = ts.strftime("%Y-%m-%d")
        ingested_iso = ts.strftime("%Y-%m-%dT00:00:00Z")
        vix_value = vix_mean + rng.gauss(0.0, 0.3)
        series_values: list[tuple[str, float]] = [
            ("VIXCLS", vix_value),
            *MACRO_NON_VIX_SERIES.items(),
        ]
        for series_id, value in series_values:
            conn.execute(
                "INSERT INTO macro_observations (source, series_id, observation_date, "
                "revision_number, release_date, value, units, frequency, ingested_at) "
                "VALUES ('FRED', ?, ?, 0, ?, ?, 'pct', 'd', ?)",
                (series_id, date_str, date_str, value, ingested_iso),
            )


def _insert_event_calendar(
    conn: sqlite3.Connection,
    *,
    history: Sequence[datetime],
) -> None:
    """Insert one earnings event per sector ticker mid-window.

    The orchestrator's Q12 module reads from this table. A few rows are
    enough to exercise the code path; the orchestrator handles empty
    event windows gracefully.
    """
    midpoint = history[len(history) // 2]
    for ticker in SECTOR_TICKERS:
        scheduled_iso = midpoint.strftime("%Y-%m-%dT16:00:00Z")
        ingested_iso = midpoint.strftime("%Y-%m-%dT00:00:00Z")
        conn.execute(
            "INSERT INTO event_calendar (event_id, event_type, ticker, scheduled_at, "
            "description, status, source, ingested_at, last_updated) VALUES "
            "(?, 'earnings', ?, ?, ?, 'scheduled', 'fixture', ?, ?)",
            (
                f"evt-{ticker.lower()}-q1",
                ticker,
                scheduled_iso,
                f"{ticker} Q1 earnings",
                ingested_iso,
                ingested_iso,
            ),
        )


def _insert_prediction_market(
    conn: sqlite3.Connection,
    *,
    history: Sequence[datetime],
) -> None:
    """Insert one prediction-market contract with daily snapshots.

    The orchestrator's Q3 prediction-market module reads from this table;
    a single contract with a stable probability series is enough to
    exercise the code path.
    """
    contract_id = "pm-fixture-fed-rate-cut"
    creation_iso = history[0].strftime("%Y-%m-%dT00:00:00Z")
    last_seen_iso = history[-1].strftime("%Y-%m-%dT00:00:00Z")
    conn.execute(
        "INSERT INTO prediction_market_contracts (contract_id, platform, description, "
        "category, resolution_date, resolution_outcome, created_at, last_seen_at) "
        "VALUES (?, 'kalshi', 'Fed cuts rates by July 2026?', 'macro', "
        "'2026-07-31', NULL, ?, ?)",
        (contract_id, creation_iso, last_seen_iso),
    )
    for ts in history:
        snapshot_iso = ts.strftime("%Y-%m-%dT00:00:00Z")
        conn.execute(
            "INSERT INTO prediction_market_snapshots (contract_id, snapshot_ts, "
            "yes_probability, volume_24h_usd, liquidity_usd, bid, ask, ingested_at) "
            "VALUES (?, ?, 0.42, 250000.0, 100000.0, 0.41, 0.43, ?)",
            (contract_id, snapshot_iso, snapshot_iso),
        )


def _populate_inmemory_db(db_path: Path, spec: SliceSpec) -> None:
    """Migrate ``db_path`` to the runtime schema and seed the slice's tables."""
    _migrate_isolated_db(db_path)
    rng = random.Random(spec.rng_seed)
    history = _history_calendar(SLICE_END_UTC, days=DAYS_OF_HISTORY)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_assets(conn)
        _insert_ohlcv(conn, history=history, rng=rng, spec=spec)
        _insert_macro(conn, history=history, vix_mean=spec.vix_mean, rng=rng)
        _insert_event_calendar(conn, history=history)
        _insert_prediction_market(conn, history=history)
        conn.commit()
    finally:
        conn.close()


def _vacuum_into(source_db: Path, target_db: Path) -> None:
    """SQLite ``VACUUM INTO`` produces a stable, compact byte layout."""
    target_db.unlink(missing_ok=True)
    conn = sqlite3.connect(str(source_db))
    try:
        conn.execute("VACUUM INTO ?", (str(target_db),))
    finally:
        conn.close()


def _write_manifest(slice_dir: Path, spec: SliceSpec) -> None:
    """Emit the slice's manifest.json with explicit invocation timestamps."""
    invocation_days = _trading_day_calendar(SLICE_END_UTC, count=INVOCATION_COUNT)
    invocation_timestamps = [d.strftime("%Y-%m-%dT%H:%M:%SZ") for d in invocation_days]
    payload = {
        "slice_id": spec.slice_id,
        "regime_label": spec.regime_label,
        "source": "live_archive",
        "invocation_timestamps": invocation_timestamps,
        "source_table_commit_hashes": {
            "ohlcv_bars": "f" * 64,
            "macro_observations": "e" * 64,
        },
        "original_ingestion_timestamps": None,
        "replaces": [],
        "curation_notes": (
            f"Synthesized {spec.regime_label} slice for replay-harness E2E "
            f"(rng_seed={spec.rng_seed})."
        ),
        "created_at": SLICE_CREATED_AT,
    }
    manifest_path = slice_dir / "manifest.json"
    # Stable, sorted-key JSON so re-runs are byte-identical.
    manifest_path.write_text(
        json.dumps(payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def generate_slice(spec: SliceSpec, *, fixture_store_root: Path) -> Path:
    """Produce one slice under ``fixture_store_root`` and return its directory.

    Public entry point — callable from the drift-detection test against a
    tmp_path so the test compares the freshly-generated bytes against the
    committed fixture without touching the committed copy.
    """
    slice_dir = fixture_store_root / spec.regime_label / spec.slice_id
    slice_dir.mkdir(parents=True, exist_ok=True)
    _write_manifest(slice_dir, spec)
    raw_inputs_path = slice_dir / "raw_inputs.sqlite"
    with tempfile.TemporaryDirectory(prefix="alphamind_fixture_gen_") as tmp_dir_str:
        unvacuumed = Path(tmp_dir_str) / "raw_inputs_unvacuumed.sqlite"
        _populate_inmemory_db(unvacuumed, spec)
        _vacuum_into(unvacuumed, raw_inputs_path)
    return slice_dir


def main() -> None:
    """Generate every committed slice into the canonical fixture store."""
    FIXTURE_STORE_ROOT.mkdir(parents=True, exist_ok=True)
    for spec in SLICE_SPECS:
        slice_dir = generate_slice(spec, fixture_store_root=FIXTURE_STORE_ROOT)
        size_kb = (slice_dir / "raw_inputs.sqlite").stat().st_size / 1024
        print(f"wrote {slice_dir} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()

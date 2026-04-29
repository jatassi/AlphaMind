"""Shared fixture-builder helpers for the replay-harness test suite.

The engine tests (story 05) and the CLI tests (story 08) both need a
synthesized fixture slice on disk — a manifest plus a SQLite snapshot of
the raw-input tables the orchestrator queries. Pulling the builders into
a conftest avoids two near-identical copies of the same boilerplate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alphamind.distillation.replay_harness.engine import _migrate_isolated_db
from alphamind.distillation.replay_harness.fixtures import (
    FixtureSlice,
    load_fixture_slice,
)

SECTOR_TICKERS: dict[str, tuple[str, str, str]] = {
    "NVDA": ("semis", "tech_semis", "SMH"),
    "AAPL": ("tech", "tech_semis", "XLK"),
    "JPM": ("financials", "financials", "XLF"),
    "XOM": ("energy", "energy", "XLE"),
}
LEAD_LAG_TICKERS: tuple[str, ...] = ("HYG", "SPY", "SOXX", "QQQ", "XLF", "USO", "XLE")


def build_raw_inputs_sqlite(path: Path, *, end: datetime, days: int) -> None:
    """Build a SQLite file at ``path`` carrying the raw-input tables the engine reads."""
    _migrate_isolated_db(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
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
                "VALUES (?, ?, ?, ?, ?, 'test', '2026-04-26T00:00:00Z')",
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

        all_tickers = tuple(SECTOR_TICKERS) + LEAD_LAG_TICKERS
        for ticker in all_tickers:
            base_price = 100.0
            for offset in range(days, 0, -1):
                bar_date = end - timedelta(days=offset)
                perturbation = ((offset * 7) % 11) * 0.5
                price = base_price + perturbation
                volume = 1_000_000 + ((offset * 13) % 17) * 10_000
                conn.execute(
                    "INSERT INTO ohlcv_bars (ticker, period_start, timeframe, period_end, "
                    "session, adj_open, adj_high, adj_low, adj_close, adj_volume, adj_vwap, "
                    "unadj_open, unadj_high, unadj_low, unadj_close, unadj_volume, "
                    "unadj_vwap, trade_count, source, ingested_at) VALUES (?, ?, '1d', ?, "
                    "'regular', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100, 'test', ?)",
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

        for offset in range(days, 0, -1):
            ts = end - timedelta(days=offset)
            date_str = ts.strftime("%Y-%m-%d")
            for series_id, value in (
                ("VIXCLS", 17.0 + ((offset * 3) % 7) * 0.1),
                ("T10YIE", 2.4),
                ("DGS3MO", 4.5),
                ("DGS2", 4.6),
                ("DGS5", 4.4),
                ("DGS10", 4.3),
                ("DGS30", 4.6),
            ):
                conn.execute(
                    "INSERT INTO macro_observations (source, series_id, observation_date, "
                    "revision_number, release_date, value, units, frequency, ingested_at) "
                    "VALUES ('FRED', ?, ?, 0, ?, ?, 'pct', 'd', ?)",
                    (
                        series_id,
                        date_str,
                        date_str,
                        value,
                        ts.strftime("%Y-%m-%dT00:00:00Z"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


def write_manifest(
    slice_dir: Path,
    *,
    slice_id: str,
    regime_label: str,
    invocation_timestamps: list[str],
    created_at: str = "2026-04-28T00:00:00Z",
) -> None:
    payload = {
        "slice_id": slice_id,
        "regime_label": regime_label,
        "source": "live_archive",
        "invocation_timestamps": invocation_timestamps,
        "source_table_commit_hashes": {
            "ohlcv_bars": "a" * 64,
            "macro_observations": "b" * 64,
        },
        "original_ingestion_timestamps": None,
        "replaces": [],
        "curation_notes": "Synthesized harness fixture for engine unit tests.",
        "created_at": created_at,
    }
    (slice_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def build_synthesized_slice(
    tmp_path: Path,
    *,
    invocation_count: int,
    days_of_history: int,
    slice_id: str = "synthetic_normal_a",
    regime_label: str = "normal",
    fixture_root_dirname: str = "fixtures",
) -> FixtureSlice:
    """Construct a fixture slice on disk and return the loaded :class:`FixtureSlice`."""
    end = datetime(2026, 4, 25, tzinfo=UTC)
    fixture_root = tmp_path / fixture_root_dirname
    slice_dir = fixture_root / regime_label / slice_id
    slice_dir.mkdir(parents=True)

    invocation_timestamps = [
        (end - timedelta(hours=invocation_count - 1 - i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(invocation_count)
    ]
    write_manifest(
        slice_dir,
        slice_id=slice_id,
        regime_label=regime_label,
        invocation_timestamps=invocation_timestamps,
    )
    build_raw_inputs_sqlite(slice_dir / "raw_inputs.sqlite", end=end, days=days_of_history)
    return load_fixture_slice(slice_dir)

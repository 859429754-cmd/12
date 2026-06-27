from __future__ import annotations

import csv
import io
import json
import zipfile
from argparse import Namespace
from datetime import date
from pathlib import Path

import pytest

from scripts.historical_orderflow_backfill import (
    BinanceAggTradeArchive,
    evaluate_coverage,
    feature_orderflow_windows,
    run_backfill,
    summarize_alignment,
    summarize_numeric,
)


def _feature(**overrides):
    base = {
        "signal_idx": 1,
        "signal_time": "2024-01-01T00:00:00+00:00",
        "entry_time": "2024-01-01T01:00:00+00:00",
        "side": "long",
        "pnl": 10.0,
    }
    base.update(overrides)
    return base


def _write_aggtrade_zip(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "agg_trade_id",
            "price",
            "quantity",
            "first_trade_id",
            "last_trade_id",
            "transact_time",
            "is_buyer_maker",
        ],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(path.with_suffix(".csv").name, buffer.getvalue())


def test_orderflow_window_excludes_trades_at_or_after_entry_time(tmp_path: Path) -> None:
    archive = BinanceAggTradeArchive(symbol="ETHUSDT", cache_dir=tmp_path, download=False)
    _write_aggtrade_zip(
        archive.zip_path(date(2024, 1, 1)),
        [
            {
                "agg_trade_id": 1,
                "price": 100,
                "quantity": 2,
                "first_trade_id": 1,
                "last_trade_id": 1,
                "transact_time": 1704069000000,  # 2024-01-01 00:30 UTC
                "is_buyer_maker": "false",
            },
            {
                "agg_trade_id": 2,
                "price": 100,
                "quantity": 10,
                "first_trade_id": 2,
                "last_trade_id": 2,
                "transact_time": 1704070800000,  # exactly 01:00 UTC, must be excluded
                "is_buyer_maker": "true",
            },
        ],
    )

    rows = feature_orderflow_windows(_feature(), archive, windows=[60], large_trade_usdt=250_000)

    assert rows[0].trade_count == 1
    assert rows[0].taker_buy_quote == pytest.approx(200)
    assert rows[0].taker_sell_quote == pytest.approx(0)
    assert rows[0].alignment == "aligned"


def test_missing_archives_are_reported_not_interpreted_as_signal(tmp_path: Path) -> None:
    archive = BinanceAggTradeArchive(symbol="ETHUSDT", cache_dir=tmp_path, download=False)

    rows = feature_orderflow_windows(_feature(), archive, windows=[60], large_trade_usdt=250_000)
    alignment = summarize_alignment(rows)

    assert rows[0].missing_days == ["2024-01-01"]
    assert alignment == [{"alignment": "missing", "count": 1, "win_rate_pct": 100.0, "avg_pnl": 10.0, "total_pnl": 10.0}]


def test_numeric_summary_uses_direction_adjusted_cvd_for_winners_and_losers(tmp_path: Path) -> None:
    archive = BinanceAggTradeArchive(symbol="ETHUSDT", cache_dir=tmp_path, download=False)
    _write_aggtrade_zip(
        archive.zip_path(date(2024, 1, 1)),
        [
            {
                "agg_trade_id": 1,
                "price": 100,
                "quantity": 4,
                "first_trade_id": 1,
                "last_trade_id": 1,
                "transact_time": 1704069000000,
                "is_buyer_maker": "false",
            },
            {
                "agg_trade_id": 2,
                "price": 100,
                "quantity": 1,
                "first_trade_id": 2,
                "last_trade_id": 2,
                "transact_time": 1704069300000,
                "is_buyer_maker": "true",
            },
        ],
    )
    _write_aggtrade_zip(
        archive.zip_path(date(2024, 1, 2)),
        [
            {
                "agg_trade_id": 3,
                "price": 100,
                "quantity": 1,
                "first_trade_id": 3,
                "last_trade_id": 3,
                "transact_time": 1704155400000,
                "is_buyer_maker": "false",
            },
            {
                "agg_trade_id": 4,
                "price": 100,
                "quantity": 5,
                "first_trade_id": 4,
                "last_trade_id": 4,
                "transact_time": 1704155700000,
                "is_buyer_maker": "true",
            },
        ],
    )

    rows = []
    rows.extend(feature_orderflow_windows(_feature(entry_time="2024-01-01T01:00:00+00:00", pnl=5), archive, windows=[60], large_trade_usdt=250_000))
    rows.extend(feature_orderflow_windows(_feature(entry_time="2024-01-02T01:00:00+00:00", pnl=-3), archive, windows=[60], large_trade_usdt=250_000))
    summary = summarize_numeric(rows)

    assert summary["directional_cvd_quote_ratio"]["winner_median"] > summary["directional_cvd_quote_ratio"]["loser_median"]
    assert summary["directional_cvd_quote_ratio"]["effect_size"] > 0


def test_backfill_outputs_coverage_without_downloading_missing_days(tmp_path: Path) -> None:
    input_path = tmp_path / "features.json"
    input_path.write_text(json.dumps({"features": [_feature()]}), encoding="utf-8")
    output_path = tmp_path / "out.json"

    summary = run_backfill(
        Namespace(
            input=str(input_path),
            output=str(output_path),
            symbol="ETHUSDT",
            cache_dir=str(tmp_path / "cache"),
            windows="60",
            download=False,
            max_features=0,
            large_trade_usdt=250_000.0,
        )
    )

    assert summary["coverage"]["60"]["missing"] == 1
    assert summary["coverage"]["60"]["usable"] == 0
    assert summary["coverage_verdict"]["status"] == "incomplete"
    assert summary["coverage_verdict"]["research_gate"] == "blocked_until_backfill_complete"
    assert summary["limits"]


def test_coverage_verdict_requires_minimum_usable_ratio() -> None:
    verdict = evaluate_coverage(
        {
            "60": {"total": 10, "usable": 8, "missing": 1, "empty": 1, "usable_ratio": 0.8},
            "240": {"total": 10, "usable": 6, "missing": 4, "empty": 0, "usable_ratio": 0.6},
        },
        min_usable_ratio=0.75,
    )

    assert verdict["status"] == "incomplete"
    assert verdict["windows"]["60"]["status"] == "ok"
    assert verdict["windows"]["240"]["status"] == "incomplete"
    assert verdict["research_gate"] == "blocked_until_backfill_complete"

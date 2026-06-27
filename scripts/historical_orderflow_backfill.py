from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
import urllib.error
import urllib.request
import zipfile
from collections import OrderedDict
from dataclasses import asdict, dataclass, fields
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from tempfile import NamedTemporaryFile
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


SYMBOL = "ETHUSDT"
DEFAULT_INPUT = "data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json"
DEFAULT_OUTPUT = "data/research/historical_orderflow_proxy_eth_2022_2026.json"
DAILY_URL = "https://data.binance.vision/data/futures/um/daily/aggTrades/{symbol}/{symbol}-aggTrades-{day}.zip"
FIELDS = ["agg_trade_id", "price", "quantity", "first_trade_id", "last_trade_id", "transact_time", "is_buyer_maker"]
NUMERIC_FIELDS = [
    "trade_count",
    "total_quote",
    "taker_buy_quote",
    "taker_sell_quote",
    "cvd_quote",
    "cvd_quote_ratio",
    "directional_cvd_quote_ratio",
    "buy_sell_ratio",
    "large_trade_count",
    "large_trade_quote",
    "directional_large_trade_ratio",
    "max_trade_quote",
    "coverage_minutes",
]


@dataclass(frozen=True)
class AggTrade:
    price: float
    quantity: float
    timestamp_ms: int
    buyer_is_maker: bool

    @property
    def quote(self) -> float:
        return self.price * self.quantity


@dataclass(frozen=True)
class OrderflowWindow:
    signal_idx: int
    signal_time: str
    entry_time: str
    side: str
    pnl: float
    window_minutes: int
    start_time: str
    end_time: str
    trade_count: int
    taker_buy_quote: float
    taker_sell_quote: float
    cvd_quote: float
    cvd_quote_ratio: float
    directional_cvd_quote_ratio: float
    buy_sell_ratio: float
    large_trade_count: int
    large_trade_quote: float
    directional_large_trade_ratio: float
    max_trade_quote: float
    coverage_minutes: float
    alignment: str
    missing_days: list[str]

    @property
    def total_quote(self) -> float:
        return self.taker_buy_quote + self.taker_sell_quote


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill Binance futures aggTrades orderflow proxy before each pure strategy entry."
    )
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--symbol", default=SYMBOL)
    parser.add_argument("--cache-dir", default="data/orderflow_cache/binance_vision")
    parser.add_argument("--windows", default="60,240", help="Comma separated lookback windows in minutes.")
    parser.add_argument("--download", action="store_true", help="Download missing Binance Vision daily aggTrades zips.")
    parser.add_argument("--max-features", type=int, default=0, help="Limit features for smoke runs; 0 means all.")
    parser.add_argument("--large-trade-usdt", type=float, default=250_000.0)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument(
        "--min-usable-ratio",
        type=float,
        default=0.80,
        help="Minimum usable ratio per window before orderflow is eligible for research weighting.",
    )
    parser.add_argument(
        "--strict-coverage",
        action="store_true",
        help="Exit non-zero when any requested window is below --min-usable-ratio.",
    )
    parser.add_argument("--no-resume", action="store_true", help="Ignore existing output checkpoints.")
    return parser.parse_args()


def parse_time(value: str) -> datetime:
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def day_range(start: datetime, end: datetime) -> Iterable[date]:
    current = start.date()
    final = end.date()
    while current <= final:
        yield current
        current += timedelta(days=1)


def is_truthy(value: str) -> bool:
    return str(value).strip().lower() in {"true", "1", "t"}


def parse_aggtrade_csv(raw: bytes) -> list[AggTrade]:
    text = raw.decode("utf-8", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []
    first = [cell.strip() for cell in rows[0]]
    has_header = any(cell in FIELDS for cell in first)
    if has_header:
        reader = csv.DictReader(io.StringIO(text))
        records = list(reader)
    else:
        records = [dict(zip(FIELDS, row)) for row in rows if row]

    trades: list[AggTrade] = []
    for row in records:
        try:
            trades.append(
                AggTrade(
                    price=float(row.get("price") or row.get("p") or 0.0),
                    quantity=float(row.get("quantity") or row.get("qty") or row.get("q") or 0.0),
                    timestamp_ms=int(float(row.get("transact_time") or row.get("time") or row.get("T") or 0)),
                    buyer_is_maker=is_truthy(row.get("is_buyer_maker") or row.get("m") or ""),
                )
            )
        except (TypeError, ValueError):
            continue
    return trades


class BinanceAggTradeArchive:
    def __init__(self, *, symbol: str, cache_dir: Path, download: bool):
        self.symbol = symbol.upper()
        self.cache_dir = cache_dir
        self.download = download
        self._day_cache: OrderedDict[date, list[AggTrade]] = OrderedDict()
        self._day_cache_size = 6

    def zip_path(self, day: date) -> Path:
        return self.cache_dir / self.symbol / "aggTrades" / f"{self.symbol}-aggTrades-{day.isoformat()}.zip"

    def ensure_day(self, day: date) -> Path | None:
        path = self.zip_path(day)
        if path.exists():
            return path
        if not self.download:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        url = DAILY_URL.format(symbol=self.symbol, day=day.isoformat())
        try:
            with urllib.request.urlopen(url, timeout=30) as response, NamedTemporaryFile(delete=False) as tmp:
                tmp.write(response.read())
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        except (urllib.error.URLError, TimeoutError, OSError):
            try:
                tmp_path.unlink(missing_ok=True)  # type: ignore[name-defined]
            except Exception:
                pass
            return None
        return path

    def load_window(self, start: datetime, end: datetime) -> tuple[list[AggTrade], list[str]]:
        trades: list[AggTrade] = []
        missing: list[str] = []
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        for day in day_range(start, end - timedelta(milliseconds=1)):
            path = self.ensure_day(day)
            if path is None:
                missing.append(day.isoformat())
                continue
            try:
                day_trades = self._load_day(day, path)
            except (zipfile.BadZipFile, OSError):
                missing.append(day.isoformat())
                continue
            trades.extend(trade for trade in day_trades if start_ms <= trade.timestamp_ms < end_ms)
        trades.sort(key=lambda item: item.timestamp_ms)
        return trades, missing

    def _load_day(self, day: date, path: Path) -> list[AggTrade]:
        cached = self._day_cache.get(day)
        if cached is not None:
            self._day_cache.move_to_end(day)
            return cached
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            if not names:
                return []
            day_trades = parse_aggtrade_csv(zf.read(names[0]))
        self._day_cache[day] = day_trades
        self._day_cache.move_to_end(day)
        while len(self._day_cache) > self._day_cache_size:
            self._day_cache.popitem(last=False)
        return day_trades


def summarize_window(
    feature: dict[str, Any],
    trades: list[AggTrade],
    *,
    window_minutes: int,
    start: datetime,
    end: datetime,
    missing_days: list[str],
    large_trade_usdt: float,
) -> OrderflowWindow:
    side = str(feature["side"])
    buy_quote = 0.0
    sell_quote = 0.0
    buy_large = 0.0
    sell_large = 0.0
    large_count = 0
    max_trade_quote = 0.0
    first_ms: int | None = None
    last_ms: int | None = None
    end_ms = int(end.timestamp() * 1000)
    for trade in trades:
        if trade.timestamp_ms >= end_ms:
            raise ValueError("lookahead detected: trade timestamp is at or after entry_time")
        quote = trade.quote
        max_trade_quote = max(max_trade_quote, quote)
        first_ms = trade.timestamp_ms if first_ms is None else min(first_ms, trade.timestamp_ms)
        last_ms = trade.timestamp_ms if last_ms is None else max(last_ms, trade.timestamp_ms)
        if trade.buyer_is_maker:
            sell_quote += quote
            if quote >= large_trade_usdt:
                sell_large += quote
                large_count += 1
        else:
            buy_quote += quote
            if quote >= large_trade_usdt:
                buy_large += quote
                large_count += 1

    total = buy_quote + sell_quote
    cvd = buy_quote - sell_quote
    cvd_ratio = cvd / total if total > 0 else 0.0
    directional_cvd = cvd_ratio if side == "long" else -cvd_ratio
    large_total = buy_large + sell_large
    large_ratio_raw = (buy_large - sell_large) / large_total if large_total > 0 else 0.0
    directional_large = large_ratio_raw if side == "long" else -large_ratio_raw
    if directional_cvd >= 0.05:
        alignment = "aligned"
    elif directional_cvd <= -0.05:
        alignment = "conflict"
    else:
        alignment = "neutral"
    coverage_minutes = ((last_ms - first_ms) / 60000.0) if first_ms is not None and last_ms is not None else 0.0
    return OrderflowWindow(
        signal_idx=int(feature["signal_idx"]),
        signal_time=str(feature["signal_time"]),
        entry_time=str(feature["entry_time"]),
        side=side,
        pnl=float(feature["pnl"]),
        window_minutes=window_minutes,
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        trade_count=len(trades),
        taker_buy_quote=buy_quote,
        taker_sell_quote=sell_quote,
        cvd_quote=cvd,
        cvd_quote_ratio=cvd_ratio,
        directional_cvd_quote_ratio=directional_cvd,
        buy_sell_ratio=buy_quote / max(sell_quote, 1e-9),
        large_trade_count=large_count,
        large_trade_quote=large_total,
        directional_large_trade_ratio=directional_large,
        max_trade_quote=max_trade_quote,
        coverage_minutes=coverage_minutes,
        alignment=alignment,
        missing_days=missing_days,
    )


def feature_orderflow_windows(
    feature: dict[str, Any],
    archive: BinanceAggTradeArchive,
    *,
    windows: list[int],
    large_trade_usdt: float,
) -> list[OrderflowWindow]:
    end = parse_time(feature["entry_time"])
    rows: list[OrderflowWindow] = []
    for minutes in windows:
        start = end - timedelta(minutes=minutes)
        trades, missing = archive.load_window(start, end)
        rows.append(
            summarize_window(
                feature,
                trades,
                window_minutes=minutes,
                start=start,
                end=end,
                missing_days=missing,
                large_trade_usdt=large_trade_usdt,
            )
        )
    return rows


def effect_size(wins: list[float], losses: list[float]) -> float:
    values = wins + losses
    if not values:
        return 0.0
    center = mean(values)
    variance = mean([(value - center) ** 2 for value in values]) if len(values) > 1 else 0.0
    if variance <= 1e-12:
        return 0.0
    return (mean(wins) - mean(losses)) / math.sqrt(variance) if wins and losses else 0.0


def summarize_numeric(rows: list[OrderflowWindow]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    winners = [row for row in rows if row.pnl > 0 and not row.missing_days and row.trade_count > 0]
    losers = [row for row in rows if row.pnl <= 0 and not row.missing_days and row.trade_count > 0]
    for name in NUMERIC_FIELDS:
        win_values = [float(getattr(row, name)) for row in winners]
        loss_values = [float(getattr(row, name)) for row in losers]
        output[name] = {
            "winner_median": median(win_values) if win_values else None,
            "loser_median": median(loss_values) if loss_values else None,
            "winner_mean": mean(win_values) if win_values else None,
            "loser_mean": mean(loss_values) if loss_values else None,
            "effect_size": effect_size(win_values, loss_values),
        }
    return output


def summarize_alignment(rows: list[OrderflowWindow]) -> list[dict[str, Any]]:
    groups: dict[str, list[OrderflowWindow]] = {}
    for row in rows:
        if row.missing_days or row.trade_count <= 0:
            key = "missing"
        else:
            key = row.alignment
        groups.setdefault(key, []).append(row)
    output = []
    for key, group in groups.items():
        wins = [row for row in group if row.pnl > 0]
        output.append(
            {
                "alignment": key,
                "count": len(group),
                "win_rate_pct": len(wins) / max(len(group), 1) * 100,
                "avg_pnl": sum(row.pnl for row in group) / max(len(group), 1),
                "total_pnl": sum(row.pnl for row in group),
            }
        )
    return sorted(output, key=lambda item: (item["alignment"] == "missing", -item["count"]))


def build_report(summary: dict[str, Any]) -> str:
    lines = [
        "# Historical Orderflow Proxy Backfill",
        "",
        "This report uses Binance futures aggTrades before each strategy entry. It is an orderflow proxy, not full historical order book depth.",
        "",
        "## Scope",
        f"- Input: `{summary['input']}`",
        f"- Symbol: `{summary['symbol']}`",
        f"- Features requested: `{summary['feature_count']}`",
        f"- Download enabled: `{summary['download_enabled']}`",
        f"- Partial: `{summary.get('partial', False)}`",
        f"- Processed feature windows: `{summary.get('processed_feature_windows', 0)}`",
        f"- Lookahead guard: `{summary['lookahead_guard']}`",
        "",
        "## Coverage",
    ]
    for window, stats in summary["coverage"].items():
        lines.append(
            f"- `{window}m`: usable `{stats['usable']}` / total `{stats['total']}`, "
            f"missing `{stats['missing']}`, empty `{stats['empty']}`"
        )
    lines.extend(["", "## Numeric Effects"])
    for window, stats in summary["windows"].items():
        lines.append(f"### {window}m")
        ranked = sorted(
            stats["numeric"].items(),
            key=lambda pair: abs(pair[1].get("effect_size") or 0.0),
            reverse=True,
        )
        for name, item in ranked[:8]:
            lines.append(
                f"- `{name}`: win_median `{item['winner_median']}`, "
                f"loss_median `{item['loser_median']}`, effect `{item['effect_size']:.3f}`"
            )
        lines.append("")
        lines.append("Alignment:")
        for row in stats["alignment"]:
            lines.append(
                f"- `{row['alignment']}`: count `{row['count']}`, win_rate `{row['win_rate_pct']:.2f}%`, "
                f"avg_pnl `{row['avg_pnl']:.2f}`"
            )
        lines.append("")
    lines.extend(["## Limits", *[f"- {item}" for item in summary["limits"]]])
    return "\n".join(lines) + "\n"


def parse_windows(value: str) -> list[int]:
    return [int(item.strip()) for item in str(value).split(",") if item.strip()]


def empty_rows_by_window(windows: list[int]) -> dict[int, list[OrderflowWindow]]:
    return {window: [] for window in windows}


def orderflow_window_from_dict(row: dict[str, Any]) -> OrderflowWindow:
    allowed = {item.name for item in fields(OrderflowWindow)}
    return OrderflowWindow(**{key: value for key, value in row.items() if key in allowed})


def load_checkpoint(output: Path, windows: list[int]) -> dict[int, list[OrderflowWindow]]:
    rows_by_window = empty_rows_by_window(windows)
    if not output.exists():
        return rows_by_window
    try:
        raw = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return rows_by_window
    for window, rows in (raw.get("rows") or {}).items():
        try:
            window_int = int(window)
        except (TypeError, ValueError):
            continue
        if window_int not in rows_by_window:
            continue
        for row in rows or []:
            try:
                rows_by_window[window_int].append(orderflow_window_from_dict(row))
            except (TypeError, ValueError):
                continue
    return rows_by_window


def summarize_rows(
    *,
    args: argparse.Namespace,
    input_path: Path,
    features: list[dict[str, Any]],
    rows_by_window: dict[int, list[OrderflowWindow]],
    partial: bool,
) -> dict[str, Any]:
    window_stats: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    for window, rows in rows_by_window.items():
        usable = [row for row in rows if not row.missing_days and row.trade_count > 0]
        missing = [row for row in rows if row.missing_days]
        empty = [row for row in rows if not row.missing_days and row.trade_count <= 0]
        coverage[str(window)] = {
            "total": len(rows),
            "usable": len(usable),
            "missing": len(missing),
            "empty": len(empty),
            "usable_ratio": round(len(usable) / len(rows), 6) if rows else 0.0,
        }
        window_stats[str(window)] = {
            "numeric": summarize_numeric(rows),
            "alignment": summarize_alignment(rows),
        }
    min_usable_ratio = float(getattr(args, "min_usable_ratio", 0.80) or 0.80)
    coverage_verdict = evaluate_coverage(coverage, min_usable_ratio=min_usable_ratio)

    return {
        "created_at": datetime.now(UTC).isoformat(),
        "input": str(input_path),
        "symbol": str(args.symbol).upper(),
        "feature_count": len(features),
        "download_enabled": bool(args.download),
        "partial": partial,
        "processed_feature_windows": sum(len(rows) for rows in rows_by_window.values()),
        "lookahead_guard": "Each window ends at entry_time exclusive; trades with timestamp >= entry_time raise an error.",
        "coverage": coverage,
        "coverage_verdict": coverage_verdict,
        "windows": window_stats,
        "rows": {str(window): [asdict(row) | {"total_quote": row.total_quote} for row in rows] for window, rows in rows_by_window.items()},
        "limits": [
            "aggTrades proxy reconstructs active buy/sell pressure and CVD but does not reconstruct full historical order book depth.",
            "Missing daily archives are reported as missing, never interpreted as bearish or bullish evidence.",
            "This output is research-only and must pass walk-forward validation before changing live RiskManager weights.",
        ],
    }


def evaluate_coverage(coverage: dict[str, Any], *, min_usable_ratio: float) -> dict[str, Any]:
    windows: dict[str, Any] = {}
    status = "ok"
    warnings: list[str] = []
    for window, stats in coverage.items():
        total = int(stats.get("total") or 0)
        usable = int(stats.get("usable") or 0)
        missing = int(stats.get("missing") or 0)
        empty = int(stats.get("empty") or 0)
        usable_ratio = float(stats.get("usable_ratio") if stats.get("usable_ratio") is not None else 0.0)
        window_status = "ok" if total > 0 and usable_ratio >= min_usable_ratio else "incomplete"
        if window_status != "ok":
            status = "incomplete"
            warnings.append(
                f"{window}m usable_ratio={usable_ratio:.3f} below required {min_usable_ratio:.3f}; "
                f"missing={missing}, empty={empty}, total={total}"
            )
        windows[str(window)] = {
            "status": window_status,
            "total": total,
            "usable": usable,
            "missing": missing,
            "empty": empty,
            "usable_ratio": usable_ratio,
        }
    return {
        "status": status,
        "min_usable_ratio": min_usable_ratio,
        "windows": windows,
        "warnings": warnings,
        "research_gate": "eligible" if status == "ok" else "blocked_until_backfill_complete",
    }


def write_outputs(output: Path, summary: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    output.with_suffix(".md").write_text(build_report(summary), encoding="utf-8")


def run_backfill(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input)
    output = Path(args.output)
    source = json.loads(input_path.read_text(encoding="utf-8"))
    features = list(source.get("features") or [])
    if args.max_features and args.max_features > 0:
        features = features[: args.max_features]
    windows = parse_windows(args.windows)
    archive = BinanceAggTradeArchive(symbol=args.symbol, cache_dir=Path(args.cache_dir), download=bool(args.download))
    progress_every = int(getattr(args, "progress_every", 0) or 0)
    checkpoint_every = int(getattr(args, "checkpoint_every", 0) or 0)
    rows_by_window = empty_rows_by_window(windows) if getattr(args, "no_resume", False) else load_checkpoint(output, windows)
    completed = {(row.signal_idx, row.window_minutes) for rows in rows_by_window.values() for row in rows}

    for index, feature in enumerate(features, start=1):
        if progress_every > 0 and (index == 1 or index % progress_every == 0 or index == len(features)):
            print(f"[historical_orderflow_backfill] feature {index}/{len(features)}", file=sys.stderr)
        needed_windows = [window for window in windows if (int(feature["signal_idx"]), window) not in completed]
        if not needed_windows:
            continue
        for row in feature_orderflow_windows(
            feature,
            archive,
            windows=needed_windows,
            large_trade_usdt=float(args.large_trade_usdt),
        ):
            rows_by_window[row.window_minutes].append(row)
            completed.add((row.signal_idx, row.window_minutes))
        if checkpoint_every > 0 and index % checkpoint_every == 0:
            write_outputs(
                output,
                summarize_rows(
                    args=args,
                    input_path=input_path,
                    features=features,
                    rows_by_window=rows_by_window,
                    partial=True,
                ),
            )

    return summarize_rows(
        args=args,
        input_path=input_path,
        features=features,
        rows_by_window=rows_by_window,
        partial=False,
    )


def main() -> None:
    args = parse_args()
    summary = run_backfill(args)
    output = Path(args.output)
    write_outputs(output, summary)
    report = output.with_suffix(".md")
    print(
        json.dumps(
            {
                "output": str(output),
                "report": str(report),
                "feature_count": summary["feature_count"],
                "coverage": summary["coverage"],
                "coverage_verdict": summary["coverage_verdict"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if bool(getattr(args, "strict_coverage", False)) and summary["coverage_verdict"]["status"] != "ok":
        raise SystemExit(2)


if __name__ == "__main__":
    main()

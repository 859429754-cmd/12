from __future__ import annotations

import asyncio
import io
import math
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import ccxt.async_support as ccxt
import pandas as pd
import requests


MarketSource = Literal["auto", "gateio", "binance", "okx", "cryptocompare", "synthetic"]


SYMBOL_MAP: dict[str, dict[str, str]] = {
    "gateio": {
        "BTC/USDT:USDT": "BTC/USDT:USDT",
        "ETH/USDT:USDT": "ETH/USDT:USDT",
        "SOL/USDT:USDT": "SOL/USDT:USDT",
    },
    "binance": {
        "BTC/USDT:USDT": "BTC/USDT:USDT",
        "ETH/USDT:USDT": "ETH/USDT:USDT",
        "SOL/USDT:USDT": "SOL/USDT:USDT",
    },
    "okx": {
        "BTC/USDT:USDT": "BTC/USDT:USDT",
        "ETH/USDT:USDT": "ETH/USDT:USDT",
        "SOL/USDT:USDT": "SOL/USDT:USDT",
    },
}


TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


class MarketDataClient:
    """多交易所行情客户端。

    Gate.io 是实盘执行交易所；回测和行情扫描可以从 Gate.io、Binance、OKX
    读取公开 K 线。优先使用交易所原生 REST，再降级到 ccxt。只有行情面板
    兜底展示才允许使用合成数据；深度回测由 API 层拦截，不能用假数据冒充。
    """

    def __init__(self, config: Any | None = None):
        self.config = config
        self._exchanges: dict[str, Any] = {}

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1h",
        limit: int = 240,
        source: MarketSource = "auto",
        closed_only: bool = True,
    ) -> pd.DataFrame:
        end_ms = self._now_ms()
        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        start_ms = end_ms - max(limit, 1) * tf_ms
        frame = await self.fetch_ohlcv_history(
            symbol=symbol,
            timeframe=timeframe,
            start=start_ms,
            end=end_ms,
            source=source,
            max_candles=limit,
        )
        if closed_only:
            frame = self._drop_incomplete_tail(frame, timeframe)
        return frame.tail(limit).reset_index(drop=True)

    async def fetch_ohlcv_history(
        self,
        symbol: str,
        timeframe: str = "1h",
        start: int | str | datetime | None = None,
        end: int | str | datetime | None = None,
        source: MarketSource = "auto",
        limit_per_call: int = 1000,
        max_candles: int = 50_000,
    ) -> pd.DataFrame:
        start_ms = self._to_ms(start) if start is not None else self._now_ms() - max_candles * TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        end_ms = self._to_ms(end) if end is not None else self._now_ms()
        if end_ms <= start_ms:
            raise ValueError("回测结束时间必须晚于开始时间")

        sources = [source] if source != "auto" else ["binance", "gateio", "okx"]
        last_error = ""
        for item in sources:
            try:
                frame = await self._fetch_from_exchange(
                    source=item,
                    symbol=symbol,
                    timeframe=timeframe,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    limit_per_call=limit_per_call,
                    max_candles=max_candles,
                )
                if len(frame) >= 10:
                    frame.attrs["requested_source"] = source
                    frame.attrs["data_source"] = item
                    frame.attrs["data_warning"] = ""
                    return frame
                last_error = f"{item} 返回K线数量不足"
            except Exception as exc:  # noqa: BLE001 - 保持控制台可用，错误写入 warning
                last_error = f"{item}: {type(exc).__name__}: {exc}"

        try:
            frame = await self._fetch_from_cryptocompare(symbol, timeframe, start_ms, end_ms, max_candles)
            if len(frame) >= 10:
                frame.attrs["requested_source"] = source
                frame.attrs["data_source"] = "cryptocompare"
                frame.attrs["data_warning"] = "主交易所K线接口不可用，已使用 CryptoCompare 免费公开小时K线作为回测数据源。"
                return frame
        except Exception as exc:  # noqa: BLE001
            last_error = f"{last_error}; cryptocompare: {type(exc).__name__}: {exc}"

        frame = self._synthetic_ohlcv(symbol, timeframe, start_ms, end_ms, min(max_candles, 1500))
        frame.attrs["requested_source"] = source
        frame.attrs["data_source"] = "synthetic"
        frame.attrs["data_warning"] = f"真实交易所K线暂不可用，已使用合成数据占位：{last_error}"
        return frame

    async def close(self) -> None:
        for exchange in list(self._exchanges.values()):
            try:
                await exchange.close()
            except Exception:
                pass
        self._exchanges.clear()

    async def _fetch_from_exchange(
        self,
        source: str,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        limit_per_call: int,
        max_candles: int,
    ) -> pd.DataFrame:
        direct_error = ""
        if source in {"binance", "gateio", "okx"}:
            try:
                return await self._fetch_from_public_rest(source, symbol, timeframe, start_ms, end_ms, limit_per_call, max_candles)
            except Exception as exc:  # noqa: BLE001
                direct_error = f"REST {type(exc).__name__}: {exc}"

        try:
            return await self._fetch_from_ccxt_exchange(source, symbol, timeframe, start_ms, end_ms, limit_per_call, max_candles)
        except Exception as exc:  # noqa: BLE001
            if direct_error:
                raise RuntimeError(f"{direct_error}; ccxt {type(exc).__name__}: {exc}") from exc
            raise

    async def _fetch_from_ccxt_exchange(
        self,
        source: str,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        limit_per_call: int,
        max_candles: int,
    ) -> pd.DataFrame:
        exchange = self._get_exchange(source)
        exchange_symbol = SYMBOL_MAP.get(source, {}).get(symbol, symbol)
        await exchange.load_markets()
        if exchange_symbol not in exchange.markets and symbol in exchange.markets:
            exchange_symbol = symbol

        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        since = start_ms
        rows: list[list[float]] = []
        seen: set[int] = set()
        empty_rounds = 0

        while since < end_ms and len(rows) < max_candles:
            batch = await exchange.fetch_ohlcv(exchange_symbol, timeframe=timeframe, since=since, limit=limit_per_call)
            if not batch:
                empty_rounds += 1
                if empty_rounds >= 2:
                    break
                since += limit_per_call * tf_ms
                continue

            progressed = False
            for candle in batch:
                ts = int(candle[0])
                if ts < start_ms:
                    continue
                if ts > end_ms:
                    continue
                if ts in seen:
                    continue
                seen.add(ts)
                rows.append(candle[:6])
                progressed = True
            last_ts = int(batch[-1][0])
            next_since = last_ts + tf_ms
            if next_since <= since:
                next_since = since + tf_ms
            since = next_since
            if not progressed and last_ts >= end_ms:
                break
            await asyncio.sleep(max(getattr(exchange, "rateLimit", 100), 50) / 1000)

        if not rows:
            return self._empty_frame()
        rows.sort(key=lambda item: int(item[0]))
        return self._frame_from_rows(rows)

    async def _fetch_from_public_rest(
        self,
        source: str,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        limit_per_call: int,
        max_candles: int,
    ) -> pd.DataFrame:
        if source == "binance":
            return await self._fetch_binance_rest(symbol, timeframe, start_ms, end_ms, limit_per_call, max_candles)
        if source == "gateio":
            return await self._fetch_gate_rest(symbol, timeframe, start_ms, end_ms, limit_per_call, max_candles)
        if source == "okx":
            return await self._fetch_okx_rest(symbol, timeframe, start_ms, end_ms, limit_per_call, max_candles)
        raise ValueError(f"不支持的原生行情源：{source}")

    async def _fetch_binance_rest(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        limit_per_call: int,
        max_candles: int,
    ) -> pd.DataFrame:
        if timeframe == "1h" and max_candles > 1500:
            vision = await asyncio.to_thread(self._fetch_binance_vision, symbol, timeframe, start_ms, end_ms, max_candles)
            if len(vision) >= 10:
                return vision

        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        interval = timeframe
        market = f"{symbol.split('/')[0].upper()}USDT"
        since = start_ms
        rows: list[list[float]] = []
        seen: set[int] = set()
        while since < end_ms and len(rows) < max_candles:
            params = {
                "symbol": market,
                "interval": interval,
                "startTime": since,
                "endTime": end_ms,
                "limit": min(1500, limit_per_call, max_candles - len(rows)),
            }
            payload = await asyncio.to_thread(self._requests_payload, "https://fapi.binance.com/fapi/v1/klines", params)
            if not isinstance(payload, list) or not payload:
                break
            for item in payload:
                ts = int(item[0])
                if ts in seen or ts < start_ms or ts > end_ms:
                    continue
                seen.add(ts)
                rows.append([ts, float(item[1]), float(item[2]), float(item[3]), float(item[4]), float(item[5])])
            last_ts = int(payload[-1][0])
            next_since = last_ts + tf_ms
            if next_since <= since:
                break
            since = next_since
            await asyncio.sleep(0.08)
        rows.sort(key=lambda item: int(item[0]))
        return self._frame_from_rows(rows[-max_candles:])

    def _fetch_binance_vision(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        max_candles: int,
    ) -> pd.DataFrame:
        market = f"{symbol.split('/')[0].upper()}USDT"
        cache_dir = Path("data/market_cache/binance_vision") / market / timeframe
        cache_dir.mkdir(parents=True, exist_ok=True)

        rows: list[list[float]] = []
        for year, month in self._iter_months(start_ms, end_ms):
            rows.extend(self._load_binance_vision_zip(cache_dir, market, timeframe, f"monthly/klines/{market}/{timeframe}/{market}-{timeframe}-{year:04d}-{month:02d}.zip"))

        current_month = datetime.fromtimestamp(end_ms / 1000, tz=UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for day in self._iter_days(max(start_ms, int(current_month.timestamp() * 1000)), end_ms):
            rows.extend(self._load_binance_vision_zip(cache_dir, market, timeframe, f"daily/klines/{market}/{timeframe}/{market}-{timeframe}-{day}.zip"))

        filtered = [row for row in rows if start_ms <= int(row[0]) <= end_ms]
        if not filtered:
            return self._empty_frame()
        dedup = {int(row[0]): row for row in filtered}
        ordered = [dedup[key] for key in sorted(dedup)]
        return self._frame_from_rows(ordered[-max_candles:])

    def _load_binance_vision_zip(self, cache_dir: Path, market: str, timeframe: str, remote_path: str) -> list[list[float]]:
        filename = remote_path.split("/")[-1]
        cache_file = cache_dir / filename
        if not cache_file.exists():
            url = f"https://data.binance.vision/data/futures/um/{remote_path}"
            response = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
            if response.status_code == 404:
                return []
            response.raise_for_status()
            cache_file.write_bytes(response.content)

        with zipfile.ZipFile(io.BytesIO(cache_file.read_bytes())) as archive:
            csv_name = archive.namelist()[0]
            raw_csv = archive.read(csv_name)
            with archive.open(csv_name) as handle:
                frame = pd.read_csv(handle)
        if "open_time" not in frame.columns:
            frame = pd.read_csv(
                io.BytesIO(raw_csv),  # pragma: no cover - legacy no-header Binance files
                header=None,
                names=["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"],
            )
        return [
            [
                int(row.open_time),
                float(row.open),
                float(row.high),
                float(row.low),
                float(row.close),
                float(row.volume),
            ]
            for row in frame.itertuples(index=False)
        ]

    async def _fetch_gate_rest(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        limit_per_call: int,
        max_candles: int,
    ) -> pd.DataFrame:
        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        contract = f"{symbol.split('/')[0].upper()}_USDT"
        since = start_ms
        rows: list[list[float]] = []
        seen: set[int] = set()
        while since < end_ms and len(rows) < max_candles:
            window_end = min(end_ms, since + min(limit_per_call, 1000) * tf_ms)
            params = {
                "contract": contract,
                "interval": timeframe,
                "from": int(since / 1000),
                "to": int(window_end / 1000),
                "limit": min(1000, limit_per_call, max_candles - len(rows)),
            }
            payload = await asyncio.to_thread(self._requests_payload, "https://api.gateio.ws/api/v4/futures/usdt/candlesticks", params)
            if not isinstance(payload, list) or not payload:
                since = window_end + tf_ms
                continue
            for item in payload:
                ts = int(item.get("t", 0)) * 1000
                if ts in seen or ts < start_ms or ts > end_ms:
                    continue
                seen.add(ts)
                rows.append(
                    [
                        ts,
                        float(item["o"]),
                        float(item["h"]),
                        float(item["l"]),
                        float(item["c"]),
                        float(item.get("sum") or item.get("v") or 0.0),
                    ]
                )
            since = window_end + tf_ms
            await asyncio.sleep(0.08)
        rows.sort(key=lambda item: int(item[0]))
        return self._frame_from_rows(rows[-max_candles:])

    async def _fetch_okx_rest(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        limit_per_call: int,
        max_candles: int,
    ) -> pd.DataFrame:
        bar = {"1h": "1H", "4h": "4H", "1d": "1D"}.get(timeframe, timeframe)
        inst_id = f"{symbol.split('/')[0].upper()}-USDT-SWAP"
        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        before = end_ms + tf_ms
        rows: list[list[float]] = []
        seen: set[int] = set()
        while before > start_ms and len(rows) < max_candles:
            params = {"instId": inst_id, "bar": bar, "before": before, "limit": min(100, limit_per_call, max_candles - len(rows))}
            payload = await asyncio.to_thread(self._requests_payload, "https://www.okx.com/api/v5/market/history-candles", params)
            data = payload.get("data", []) if isinstance(payload, dict) else []
            if not data:
                break
            oldest = before
            for item in data:
                ts = int(item[0])
                oldest = min(oldest, ts)
                if ts in seen or ts < start_ms or ts > end_ms:
                    continue
                seen.add(ts)
                rows.append([ts, float(item[1]), float(item[2]), float(item[3]), float(item[4]), float(item[7] or item[5] or 0.0)])
            next_before = oldest - tf_ms
            if next_before >= before:
                break
            before = next_before
            await asyncio.sleep(0.08)
        rows.sort(key=lambda item: int(item[0]))
        return self._frame_from_rows(rows[-max_candles:])

    def _get_exchange(self, source: str) -> Any:
        if source in self._exchanges:
            return self._exchanges[source]
        if source == "gateio":
            exchange = ccxt.gateio(
                {
                    "enableRateLimit": True,
                    "options": {"defaultType": "swap", "defaultSettle": "USDT"},
                }
            )
        elif source == "binance":
            exchange = ccxt.binanceusdm({"enableRateLimit": True})
        elif source == "okx":
            exchange = ccxt.okx({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        else:
            raise ValueError(f"不支持的行情源：{source}")
        self._exchanges[source] = exchange
        return exchange

    async def _fetch_from_cryptocompare(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
        max_candles: int,
    ) -> pd.DataFrame:
        if timeframe != "1h":
            raise ValueError("CryptoCompare 兜底源当前只用于 1h 回测")
        fsym = symbol.split("/")[0].upper()
        rows_by_ts: dict[int, list[float]] = {}
        to_ts = int(end_ms / 1000)
        start_ts = int(start_ms / 1000)
        while to_ts > start_ts and len(rows_by_ts) < max_candles:
            limit = min(2000, max_candles - len(rows_by_ts))
            url = "https://min-api.cryptocompare.com/data/v2/histohour"
            params = {"fsym": fsym, "tsym": "USDT", "limit": limit, "toTs": to_ts}
            payload = await asyncio.to_thread(self._requests_json, url, params)
            if payload.get("Response") != "Success":
                raise RuntimeError(str(payload.get("Message") or payload)[:200])
            data = payload.get("Data", {}).get("Data", [])
            if not data:
                break
            earliest = to_ts
            for item in data:
                ts = int(item["time"])
                earliest = min(earliest, ts)
                if ts < start_ts or ts * 1000 > end_ms:
                    continue
                rows_by_ts[ts] = [
                    ts * 1000,
                    float(item["open"]),
                    float(item["high"]),
                    float(item["low"]),
                    float(item["close"]),
                    float(item.get("volumeto") or item.get("volumefrom") or 0.0),
                ]
            next_to_ts = earliest - 3600
            if next_to_ts >= to_ts:
                break
            to_ts = next_to_ts
            await asyncio.sleep(0.25)
        rows = list(rows_by_ts.values())
        rows.sort(key=lambda item: int(item[0]))
        return self._frame_from_rows(rows[-max_candles:])

    def _requests_payload(self, url: str, params: dict[str, Any]) -> Any:
        response = requests.get(url, params=params, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()
        return response.json()

    def _requests_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        payload = self._requests_payload(url, params)
        if not isinstance(payload, dict):
            raise RuntimeError("接口返回格式不是 JSON 对象")
        return payload

    def _iter_months(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = datetime.fromtimestamp(end_ms / 1000, tz=UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        months: list[tuple[int, int]] = []
        current = start
        while current <= end:
            months.append((current.year, current.month))
            year = current.year + (1 if current.month == 12 else 0)
            month = 1 if current.month == 12 else current.month + 1
            current = current.replace(year=year, month=month)
        return months

    def _iter_days(self, start_ms: int, end_ms: int) -> list[str]:
        start = datetime.fromtimestamp(start_ms / 1000, tz=UTC).date()
        end = datetime.fromtimestamp(end_ms / 1000, tz=UTC).date()
        days: list[str] = []
        current = start
        while current <= end:
            days.append(current.isoformat())
            current += timedelta(days=1)
        return days

    def _frame_from_rows(self, rows: list[list[float]]) -> pd.DataFrame:
        frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame.dropna().reset_index(drop=True)

    def _drop_incomplete_tail(self, frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
        if frame.empty or "timestamp" not in frame.columns:
            return frame
        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        attrs = dict(frame.attrs)
        now_ms = self._now_ms()
        mask = [
            int(pd.Timestamp(value).timestamp() * 1000) + tf_ms <= now_ms
            for value in frame["timestamp"]
        ]
        closed = frame.loc[mask].copy()
        closed.attrs.update(attrs)
        return closed.reset_index(drop=True)

    def _empty_frame(self) -> pd.DataFrame:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    def _synthetic_ohlcv(self, symbol: str, timeframe: str, start_ms: int, end_ms: int, limit: int) -> pd.DataFrame:
        tf_ms = TIMEFRAME_MS.get(timeframe, TIMEFRAME_MS["1h"])
        total = max(120, min(limit, max(120, int((end_ms - start_ms) / tf_ms))))
        seed_price = 80_000.0 if symbol.startswith("BTC") else 3_000.0 if symbol.startswith("ETH") else 160.0
        rows: list[list[float]] = []
        price = seed_price
        for idx in range(total):
            ts = start_ms + idx * tf_ms
            drift = math.sin(idx / 19) * 0.004 + math.cos(idx / 53) * 0.002
            close = max(price * (1 + drift), 0.01)
            high = max(price, close) * (1 + 0.004)
            low = min(price, close) * (1 - 0.004)
            volume = 1000 + 400 * (1 + math.sin(idx / 11))
            rows.append([ts, price, high, low, close, volume])
            price = close
        return self._frame_from_rows(rows)

    def _to_ms(self, value: int | str | datetime) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, datetime):
            dt = value if value.tzinfo else value.replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        text = str(value).strip()
        if text.isdigit():
            return int(text)
        if len(text) == 10:
            dt = datetime.fromisoformat(text).replace(tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp() * 1000)

    def _now_ms(self) -> int:
        return int(datetime.now(tz=UTC).timestamp() * 1000)


def default_history_window() -> tuple[str, str]:
    end = datetime.now(tz=UTC).date()
    start = end - timedelta(days=365)
    return start.isoformat(), end.isoformat()

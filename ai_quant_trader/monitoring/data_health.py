from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ai_quant_trader.core.models import AggregatedOrderflow, DataHealthCheck, DataHealthReport, HealthStatus, NewsDigest


class DataHealthMonitor:
    """Freshness gate for market, news, orderflow and local clock inputs."""

    def __init__(self, *, stale_data_seconds: int = 300, news_max_age_hours: int = 6) -> None:
        self.stale_data_seconds = stale_data_seconds
        self.news_max_age_hours = news_max_age_hours

    def evaluate_symbol(
        self,
        *,
        symbol: str,
        timeframe: str,
        candles: Any,
        news: NewsDigest,
        orderflow: AggregatedOrderflow,
    ) -> DataHealthReport:
        checks = [
            self._ohlcv_check(timeframe, candles),
            self._news_check(news),
            self._orderflow_check(orderflow),
            self._clock_check(),
        ]
        status = self._aggregate_status(checks)
        blocking = [check.reason for check in checks if check.status == HealthStatus.BLOCK]
        warning = [check.reason for check in checks if check.status == HealthStatus.WARN]
        return DataHealthReport(
            symbol=symbol,
            status=status,
            can_open_new_entries=status != HealthStatus.BLOCK,
            reason=";".join(blocking or warning or ["data_health_ok"]),
            checks=checks,
        )

    def _ohlcv_check(self, timeframe: str, candles: Any) -> DataHealthCheck:
        if candles is None or len(candles) == 0:
            return DataHealthCheck(name="ohlcv", status=HealthStatus.BLOCK, reason="ohlcv_empty")
        try:
            timestamp = candles.iloc[-1].get("timestamp")
            last_ts = self._parse_timestamp(timestamp)
        except Exception:  # noqa: BLE001
            return DataHealthCheck(name="ohlcv", status=HealthStatus.WARN, reason="ohlcv_timestamp_unreadable")
        if last_ts is None:
            return DataHealthCheck(name="ohlcv", status=HealthStatus.WARN, reason="ohlcv_timestamp_missing")
        age_seconds = max((datetime.now(UTC) - last_ts).total_seconds(), 0.0)
        threshold = max(self.stale_data_seconds, self._timeframe_seconds(timeframe) * 2.5)
        if age_seconds > threshold:
            return DataHealthCheck(name="ohlcv", status=HealthStatus.BLOCK, reason="ohlcv_stale", age_seconds=age_seconds)
        return DataHealthCheck(name="ohlcv", status=HealthStatus.OK, reason="ohlcv_fresh", age_seconds=age_seconds)

    def _news_check(self, news: NewsDigest) -> DataHealthCheck:
        age_seconds = max((datetime.now(UTC) - news.generated_at.astimezone(UTC)).total_seconds(), 0.0)
        threshold = self.news_max_age_hours * 3600
        if age_seconds > threshold:
            return DataHealthCheck(name="news", status=HealthStatus.WARN, reason="news_digest_stale", age_seconds=age_seconds)
        if not news.items and "news_cache_empty_trading_worker_degraded" in news.warnings:
            return DataHealthCheck(name="news", status=HealthStatus.WARN, reason="news_cache_empty", age_seconds=age_seconds)
        return DataHealthCheck(name="news", status=HealthStatus.OK, reason="news_digest_usable", age_seconds=age_seconds)

    def _orderflow_check(self, orderflow: AggregatedOrderflow) -> DataHealthCheck:
        if orderflow.source_count <= 0:
            return DataHealthCheck(name="orderflow", status=HealthStatus.BLOCK, reason="orderflow_source_empty", quality=orderflow.data_quality)
        if orderflow.data_quality < 0.5:
            return DataHealthCheck(name="orderflow", status=HealthStatus.BLOCK, reason="orderflow_quality_too_low", quality=orderflow.data_quality)
        if orderflow.data_quality < 0.7:
            return DataHealthCheck(name="orderflow", status=HealthStatus.WARN, reason="orderflow_quality_degraded", quality=orderflow.data_quality)
        return DataHealthCheck(name="orderflow", status=HealthStatus.OK, reason="orderflow_usable", quality=orderflow.data_quality)

    def _clock_check(self) -> DataHealthCheck:
        now = datetime.now(UTC)
        if abs((datetime.now(UTC) - now).total_seconds()) > 1:
            return DataHealthCheck(name="clock", status=HealthStatus.WARN, reason="clock_monotonicity_unusual")
        return DataHealthCheck(name="clock", status=HealthStatus.OK, reason="clock_utc_available")

    def _aggregate_status(self, checks: list[DataHealthCheck]) -> HealthStatus:
        if any(check.status == HealthStatus.BLOCK for check in checks):
            return HealthStatus.BLOCK
        if any(check.status == HealthStatus.WARN for check in checks):
            return HealthStatus.WARN
        return HealthStatus.OK

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def _timeframe_seconds(self, timeframe: str) -> int:
        raw = str(timeframe or "1h").strip()
        unit = raw[-1:]
        try:
            value = int(raw[:-1])
        except ValueError:
            return 3600
        if unit == "m":
            return value * 60
        if unit == "h":
            return value * 3600
        if unit == "d":
            return value * 86400
        if unit == "w":
            return value * 7 * 86400
        if unit == "M":
            return value * 30 * 86400
        return 3600

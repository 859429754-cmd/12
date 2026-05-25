from __future__ import annotations

from ai_quant_trader.core.models import ReconciliationReport
from ai_quant_trader.execution.safety import ExchangeSafetyMonitor
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.strategy.trend_state import TrendStateStore


async def run_read_only_reconciliation(
    *,
    gateway,
    store: SQLiteStore,
    symbols: list[str],
    trend_state: TrendStateStore | None = None,
    monitor: ExchangeSafetyMonitor | None = None,
    stale_after_seconds: int = 300,
    live: bool = True,
) -> ReconciliationReport:
    safety_monitor = monitor or ExchangeSafetyMonitor(stale_after_seconds=stale_after_seconds)
    report = await safety_monitor.reconcile(
        gateway,
        symbols,
        trend_state or TrendStateStore(),
        live=live,
    )
    store.insert("reconciliation_runs", report.model_dump(mode="json"))
    store.insert("exchange_health", safety_monitor.state.model_dump(mode="json"))
    return report

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_quant_trader.core.config import load_config
from ai_quant_trader.execution.gateway import create_exchange_gateway
from ai_quant_trader.execution.reconciliation import run_read_only_reconciliation
from ai_quant_trader.storage.sqlite import SQLiteStore


def _load_runtime_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


async def check(symbols: list[str], env_file: str, config_path: str = "config/config.yaml", write_store: bool = True) -> dict:
    _load_runtime_env(Path(env_file))
    config = load_config(config_path)
    gateway = create_exchange_gateway("live", account_slot="trend")
    store = SQLiteStore(config.runtime.database_path, config.runtime.audit_log_path) if write_store else None
    try:
        balance = await gateway.fetch_balance_summary()
        positions = await gateway.fetch_positions(symbols)
        report = None
        if store is not None:
            report = await run_read_only_reconciliation(
                gateway=gateway,
                store=store,
                symbols=symbols,
                stale_after_seconds=config.risk.stale_data_seconds,
                live=True,
            )
        return {
            "balance_ok": bool(balance.get("ok")),
            "account_slot": balance.get("account_slot"),
            "usdt_total": balance.get("usdt_total"),
            "usdt_free": balance.get("usdt_free"),
            "positions_flat": all(str(item.side) == "flat" and abs(item.qty) <= 0 for item in positions),
            "positions": [item.model_dump(mode="json") for item in positions],
            "reconciliation": report.model_dump(mode="json") if report else None,
            "readiness_written": store is not None,
        }
    finally:
        await gateway.close()
        if store is not None:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Gate.io live readiness check.")
    parser.add_argument("--env-file", default=".env.runtime")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--no-write-store", action="store_true", help="Only print read-only result; do not update SQLite readiness rows.")
    parser.add_argument("--symbols", nargs="+", default=["ETH/USDT:USDT", "BTC/USDT:USDT", "SOL/USDT:USDT"])
    args = parser.parse_args()
    print(json.dumps(asyncio.run(check(args.symbols, args.env_file, args.config, not args.no_write_store)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

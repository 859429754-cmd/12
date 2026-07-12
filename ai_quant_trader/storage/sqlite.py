from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SQLiteStore:
    """SQLite WAL 持久化与审计。

    FastAPI 会在线程池里执行同步端点，所以同一个 Store 可能被不同线程访问。
    这里使用 check_same_thread=False，并用锁保护连接，避免状态接口偶发 500。
    """

    def __init__(self, db_path: str, audit_log_path: str):
        self.db_path = Path(db_path)
        self.audit_log_path = Path(audit_log_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        tables = [
            "orders",
            "trades",
            "positions_snapshot",
            "ai_decisions",
            "live_factor_snapshots",
            "hourly_reports",
            "orderflow_summaries",
            "dense_zones",
            "news_summaries",
            "news_events",
            "market_background_snapshots",
            "strategy_params_versions",
            "optimization_proposals",
            "approval_events",
            "runtime_state",
            "secret_versions",
            "strategy_profiles",
            "backtest_runs",
            "ai_review_runs",
            "agent_audit_events",
            "exchange_health",
            "data_health",
            "ai_drift_checks",
            "reconciliation_runs",
            "order_lifecycle",
            "news_risk_reviews",
            "worker_heartbeats",
            "runtime_metrics",
            "maintenance_runs",
            "runtime_alerts",
            "security_events",
            "restore_drills",
            "release_runs",
            "ai_call_budget_events",
            "ai_call_usage_events",
            "follower_executions",
            "account_balance_snapshots",
            "position_reviews",
        ]
        with self._lock:
            for table in tables:
                self.conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        symbol TEXT,
                        payload TEXT NOT NULL
                    )
                    """
                )
            try:
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_positions_snapshot_account "
                    "ON positions_snapshot(symbol, json_extract(payload, '$.account_slot'), id DESC)"
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_order_lifecycle_client_order "
                    "ON order_lifecycle(symbol, json_extract(payload, '$.client_order_id'), id DESC)"
                )
                self.conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_follower_execution_primary_stop "
                    "ON follower_executions(symbol, json_extract(payload, '$.primary_stop_client_order_id'), id DESC)"
                )
            except sqlite3.OperationalError:
                # JSON1 is present in production CPython; minimal builds retain bounded scan compatibility.
                pass
            self.conn.commit()

    def next_secret_version(self, service: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT payload FROM secret_versions WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                (service,),
            ).fetchone()
        if not row:
            return 1
        try:
            payload = json.loads(row[0])
            return int(payload.get("version", 0)) + 1
        except (TypeError, ValueError, json.JSONDecodeError):
            return 1

    def fetch_secret_versions(self, service: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, created_at, payload FROM secret_versions WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (service, limit),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row_id, created_at, payload in rows:
            output.append({"id": row_id, "created_at": created_at, "payload": json.loads(payload)})
        return output

    def insert(self, table: str, payload: BaseModel | dict[str, Any], symbol: str | None = None) -> int:
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        with self._lock:
            cursor = self.conn.execute(
                f"INSERT INTO {table} (symbol, payload) VALUES (?, ?)",
                (symbol or data.get("symbol"), json.dumps(data, ensure_ascii=False)),
            )
            self.conn.commit()
            row_id = int(cursor.lastrowid)
        self.audit(table, data)
        return row_id

    def fetch_latest(self, table: str, symbol: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            if symbol is None:
                row = self.conn.execute(f"SELECT id, created_at, symbol, payload FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
            else:
                row = self.conn.execute(
                    f"SELECT id, created_at, symbol, payload FROM {table} WHERE symbol = ? ORDER BY id DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
        if not row:
            return None
        row_id, created_at, row_symbol, payload = row
        return {"id": row_id, "created_at": created_at, "symbol": row_symbol, "payload": json.loads(payload)}

    def fetch_by_id(self, table: str, row_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                f"SELECT id, created_at, symbol, payload FROM {table} WHERE id = ?",
                (row_id,),
            ).fetchone()
        if not row:
            return None
        item_id, created_at, symbol, payload = row
        return {"id": item_id, "created_at": created_at, "symbol": symbol, "payload": json.loads(payload)}

    def update_payload(self, table: str, row_id: int, payload: dict[str, Any]) -> None:
        with self._lock:
            self.conn.execute(
                f"UPDATE {table} SET payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), row_id),
            )
            self.conn.commit()
        self.audit(f"{table}_update", payload)

    def fetch_payloads(self, table: str, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if symbol is None:
                rows = self.conn.execute(
                    f"SELECT id, created_at, symbol, payload FROM {table} ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    f"SELECT id, created_at, symbol, payload FROM {table} WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
        return [
            {"id": row_id, "created_at": created_at, "symbol": row_symbol, "payload": json.loads(payload)}
            for row_id, created_at, row_symbol, payload in rows
        ]

    def fetch_latest_payload_by_value(
        self,
        table: str,
        key: str,
        value: Any,
        *,
        symbol: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any] | None:
        if not key.replace("_", "").isalnum():
            raise ValueError("payload_lookup_key_invalid")
        path = f"$.{key}"
        row = None
        try:
            with self._lock:
                if symbol is None:
                    row = self.conn.execute(
                        f"SELECT id, created_at, symbol, payload FROM {table} "
                        "WHERE json_extract(payload, ?) = ? ORDER BY id DESC LIMIT 1",
                        (path, value),
                    ).fetchone()
                else:
                    row = self.conn.execute(
                        f"SELECT id, created_at, symbol, payload FROM {table} "
                        "WHERE symbol = ? AND json_extract(payload, ?) = ? ORDER BY id DESC LIMIT 1",
                        (symbol, path, value),
                    ).fetchone()
        except sqlite3.OperationalError:
            # Some minimal SQLite builds omit JSON1; bounded scanning remains the compatibility fallback.
            row = None
        if row:
            row_id, created_at, row_symbol, payload = row
            return {"id": row_id, "created_at": created_at, "symbol": row_symbol, "payload": json.loads(payload)}
        # Keep compatibility with old rows and SQLite builds without JSON1.
        for candidate in self.fetch_payloads(table, limit=limit, symbol=symbol):
            payload = candidate.get("payload") or {}
            if payload.get(key) == value:
                return candidate
        return None

    def audit(self, event_type: str, payload: dict[str, Any]) -> None:
        record = {"event_type": event_type, "payload": payload}
        with self._lock:
            with self.audit_log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

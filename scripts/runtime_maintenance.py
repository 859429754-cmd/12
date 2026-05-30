from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_quant_trader.core.config import load_config
from ai_quant_trader.ops.maintenance import result_to_json, run_runtime_maintenance
from ai_quant_trader.storage.sqlite import SQLiteStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local runtime maintenance without printing secrets.")
    parser.add_argument("--config", default="config/config.yaml", help="Path to config YAML.")
    parser.add_argument("--backup-dir", default="data/backups", help="Directory for compressed SQLite backups.")
    parser.add_argument("--max-log-mb", type=float, default=10.0, help="Rotate audit log above this size.")
    parser.add_argument("--keep", type=int, default=5, help="Number of rotated audit logs to keep.")
    parser.add_argument("--backup-keep", type=int, default=24, help="Number of compressed SQLite backups to retain.")
    parser.add_argument("--min-free-gb", type=float, default=1.0, help="Warn when free disk space is below this value.")
    parser.add_argument("--min-free-ratio", type=float, default=0.10, help="Warn when free disk ratio is below this value.")
    args = parser.parse_args()

    config = load_config(args.config)
    result = run_runtime_maintenance(
        database_path=config.runtime.database_path,
        audit_log_path=config.runtime.audit_log_path,
        backup_dir=Path(args.backup_dir),
        max_log_bytes=max(int(args.max_log_mb * 1024 * 1024), 1),
        keep=args.keep,
        backup_keep=args.backup_keep,
        min_free_bytes=max(int(args.min_free_gb * 1024 * 1024 * 1024), 1),
        min_free_ratio=args.min_free_ratio,
    )
    store = SQLiteStore(config.runtime.database_path, config.runtime.audit_log_path)
    try:
        store.insert("maintenance_runs", result.to_dict())
    finally:
        store.close()
    print(result_to_json(result))


if __name__ == "__main__":
    main()

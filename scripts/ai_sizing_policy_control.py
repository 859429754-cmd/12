from __future__ import annotations

import argparse
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

POLICIES = {"legacy_factor_ranked", "calibrated_v1_controlled"}


def load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"config_root_must_be_mapping:{path}")
    return data


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def backup_config(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".ai-sizing-{stamp}.bak")
    shutil.copy2(path, backup)
    return backup


def set_policy(
    path: Path,
    policy: str,
    *,
    max_tier_lift: int | None = None,
    min_factor_coverage: float | None = None,
    backup: bool = True,
) -> dict[str, Any]:
    if policy not in POLICIES:
        raise ValueError(f"unsupported_ai_sizing_policy:{policy}")
    data = load_yaml(path)
    risk = data.setdefault("risk", {})
    if not isinstance(risk, dict):
        raise ValueError("risk_config_must_be_mapping")
    previous = str(risk.get("ai_sizing_policy") or "legacy_factor_ranked")
    backup_path = str(backup_config(path)) if backup else None
    risk["ai_sizing_policy"] = policy
    if max_tier_lift is not None:
        risk["calibrated_max_tier_lift"] = int(max_tier_lift)
    if min_factor_coverage is not None:
        risk["calibrated_min_factor_coverage"] = float(min_factor_coverage)
    write_yaml(path, data)
    return {
        "ok": True,
        "config": str(path),
        "previous_policy": previous,
        "new_policy": policy,
        "backup": backup_path,
        "rollback_command": f"python scripts/ai_sizing_policy_control.py --policy {previous}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Switch AI sizing policy with a config backup for rollback.")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--policy", choices=sorted(POLICIES), required=True)
    parser.add_argument("--max-tier-lift", type=int, choices=[0, 1, 2], default=None)
    parser.add_argument("--min-factor-coverage", type=float, default=None)
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    result = set_policy(
        Path(args.config),
        args.policy,
        max_tier_lift=args.max_tier_lift,
        min_factor_coverage=args.min_factor_coverage,
        backup=not args.no_backup,
    )
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

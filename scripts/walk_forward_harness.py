from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai_quant_trader.research.walk_forward import (
    DEFAULT_POLICIES,
    WalkForwardTrade,
    WalkForwardWindow,
    evaluate_walk_forward_harness,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline walk-forward overlay harness.")
    parser.add_argument("--input", required=True, help="JSON file with trades and windows.")
    parser.add_argument("--output", required=True, help="Path to write the JSON report.")
    parser.add_argument("--candidate", choices=sorted(DEFAULT_POLICIES), default=None)
    parser.add_argument("--min-validation-trades", type=int, default=20)
    parser.add_argument("--min-oos-trades", type=int, default=10)
    return parser.parse_args()


def load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("input_json_must_be_object")
    return payload


def build_trades(payload: dict[str, Any]) -> list[WalkForwardTrade]:
    trades = []
    for item in payload.get("trades") or []:
        trades.append(
            WalkForwardTrade(
                signal_time=str(item["signal_time"]),
                pnl=float(item["pnl"]),
                baseline_equity_before=float(item.get("baseline_equity_before") or payload.get("initial_equity") or 1000.0),
                fee_paid=float(item.get("fee_paid") or 0.0),
                slippage_paid=float(item.get("slippage_paid") or 0.0),
                funding_paid=float(item.get("funding_paid") or 0.0),
                max_adverse_excursion_pct=float(item.get("max_adverse_excursion_pct") or 0.0),
                params=item.get("params") or {},
                scores=item.get("scores") or {},
            )
        )
    return trades


def build_windows(payload: dict[str, Any]) -> list[WalkForwardWindow]:
    windows = []
    for item in payload.get("windows") or []:
        windows.append(
            WalkForwardWindow(
                train_start=str(item["train_start"]),
                train_end=str(item["train_end"]),
                validation_start=str(item["validation_start"]),
                validation_end=str(item["validation_end"]),
                out_of_sample_start=str(item["out_of_sample_start"]),
                out_of_sample_end=str(item["out_of_sample_end"]),
            )
        )
    return windows


def main() -> int:
    args = parse_args()
    payload = load_payload(Path(args.input))
    candidate_policy = DEFAULT_POLICIES[args.candidate] if args.candidate else None
    report = evaluate_walk_forward_harness(
        build_trades(payload),
        build_windows(payload),
        candidate_name=args.candidate or "candidate",
        candidate_policy=candidate_policy,
        min_validation_trades=args.min_validation_trades,
        min_oos_trades=args.min_oos_trades,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": report["status"] != "rejected", "status": report["status"], "output": str(output)}, ensure_ascii=False))
    return 0 if report["status"] != "rejected" else 2


if __name__ == "__main__":
    raise SystemExit(main())

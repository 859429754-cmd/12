from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AuditCase:
    id: str
    target: str
    requirement: str


CORE_CASES: tuple[AuditCase, ...] = (
    AuditCase(
        id="strategy_ai_risk_order_stop_exit",
        target="tests/test_trading_chain_smoke.py::test_trading_cycle_opens_places_stop_then_exits_and_cancels_stop",
        requirement="Synthetic trend signal reaches AI/RiskManager, opens a mock position, places native stop, exits, cancels stop, and clears trend state.",
    ),
    AuditCase(
        id="live_addon_replaces_net_position_stop",
        target="tests/test_trading_chain_smoke.py::test_trading_cycle_replaces_net_position_stop_after_live_addon_once",
        requirement="Position review live_addon replaces the merged Gate net-position stop instead of leaving split legacy stops.",
    ),
    AuditCase(
        id="software_atr_stop_mirrors_follower",
        target="tests/test_trading_chain_smoke.py::test_software_atr_stop_mirrors_exit_to_follower",
        requirement="Software ATR stop exits primary and mirrors the close to follower account.",
    ),
    AuditCase(
        id="native_stop_fill_mirrors_follower",
        target="tests/test_trading_chain_smoke.py::test_primary_native_stop_fill_mirrors_exit_to_follower",
        requirement="Primary native stop fill is detected and mirrored to follower account.",
    ),
    AuditCase(
        id="intent_before_submit",
        target="tests/test_order_lifecycle.py::test_order_lifecycle_records_intent_before_submission",
        requirement="Order intent is persisted before exchange submission, so timeout recovery is auditable.",
    ),
    AuditCase(
        id="unknown_submit_blocks_blind_retry",
        target="tests/test_order_lifecycle.py::test_order_lifecycle_unknown_after_submit_error_blocks_blind_retry",
        requirement="Unknown exchange submission state is recorded and duplicate retry is not submitted blindly.",
    ),
    AuditCase(
        id="partial_fill_refresh",
        target="tests/test_order_lifecycle.py::test_order_lifecycle_refresh_updates_partial_fill",
        requirement="Exchange order status refresh updates partial fills in order lifecycle.",
    ),
    AuditCase(
        id="cancel_failure_audited",
        target="tests/test_order_lifecycle.py::test_order_lifecycle_cancel_failure_is_audited",
        requirement="Cancel failure is persisted as cancel_failed for operator review.",
    ),
    AuditCase(
        id="unresolved_order_lifecycle_blocks_readiness",
        target="tests/test_console_api.py::test_live_readiness_blocks_unresolved_order_lifecycle_even_after_newer_ok_event",
        requirement="Unresolved unknown/cancel_failed lifecycle events keep readiness blocked even after newer normal events for other orders.",
    ),
    AuditCase(
        id="live_position_fetch_failure_blocks_cycle",
        target="tests/test_gateway_runtime.py::test_live_position_fetch_failure_blocks_trading_cycle",
        requirement="Live position fetch failure blocks trading cycle instead of opening with unknown state.",
    ),
    AuditCase(
        id="native_stop_unknown_requires_manual_gate",
        target="tests/test_gateway_runtime.py::test_live_native_stop_unknown_requires_manual_gate_without_auto_close",
        requirement="Unknown live native stop submission requires manual Gate handling and does not auto-close through the app.",
    ),
    AuditCase(
        id="native_stop_exchange_verification_required",
        target="tests/test_execution_safety.py::test_reconciliation_blocks_when_native_stop_id_is_not_found_on_exchange",
        requirement="Live reconciliation blocks when an open position has a local native stop id that cannot be verified on the exchange.",
    ),
    AuditCase(
        id="native_stop_amount_covers_net_position",
        target="tests/test_execution_safety.py::test_reconciliation_blocks_when_native_stop_amount_does_not_cover_position",
        requirement="Live reconciliation blocks when the verified native stop amount does not cover the current Gate net position.",
    ),
    AuditCase(
        id="native_stop_trigger_price_matches_atr_stop",
        target="tests/test_execution_safety.py::test_reconciliation_blocks_when_native_stop_trigger_price_drifted",
        requirement="Live reconciliation blocks when the verified native stop trigger price drifts from the local fixed ATR stop price.",
    ),
    AuditCase(
        id="stale_trend_state_repair_requires_terminal_stop",
        target="tests/test_gateway_runtime.py::test_terminal_stop_with_flat_exchange_position_repairs_stale_trend_state",
        requirement="Stale local trend state is repaired only after terminal stop status and flat exchange position are confirmed.",
    ),
)


EXTENDED_CASES: tuple[AuditCase, ...] = (
    AuditCase(
        id="follower_account_binding",
        target="tests/test_gateway_runtime.py::test_live_gateway_can_bind_follower_account_slot",
        requirement="Follower account uses its own Gate credential binding rather than trend account credentials.",
    ),
    AuditCase(
        id="hedged_position_blocks_live_mode",
        target="tests/test_gateway_runtime.py::test_gate_hedged_position_blocks_unsupported_live_mode",
        requirement="Unsupported hedged live positions fail closed.",
    ),
    AuditCase(
        id="close_follower_execution",
        target="tests/test_gateway_runtime.py::test_trading_app_close_closes_follower_execution",
        requirement="TradingApp shutdown closes follower execution resources as well as primary resources.",
    ),
    AuditCase(
        id="follower_entry_failure_fail_closed",
        target="tests/test_trading_chain_smoke.py::test_live_follower_entry_failure_marks_exchange_safety_failed",
        requirement="Live follower entry failure marks exchange safety failed instead of silently leaving new entries enabled.",
    ),
    AuditCase(
        id="follower_exit_failure_fail_closed",
        target="tests/test_trading_chain_smoke.py::test_live_follower_exit_failure_marks_exchange_safety_failed",
        requirement="Live follower exit failure marks exchange safety failed so account divergence requires operator review.",
    ),
    AuditCase(
        id="follower_order_status_refresh_failure_fail_closed",
        target="tests/test_trading_chain_smoke.py::test_live_follower_order_status_refresh_failure_marks_exchange_safety_failed",
        requirement="Follower order-status refresh failure blocks live readiness through exchange safety.",
    ),
)


def selected_cases(mode: str) -> tuple[AuditCase, ...]:
    if mode == "extended":
        return CORE_CASES + EXTENDED_CASES
    return CORE_CASES


def run_pytest(targets: Sequence[str], extra_args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest", "-q", *targets, *extra_args]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def build_report(cases: Sequence[AuditCase], result: subprocess.CompletedProcess[str], started_at: float) -> dict[str, object]:
    duration_seconds = round(time.time() - started_at, 3)
    return {
        "ok": result.returncode == 0,
        "mode": "trading_chain_audit",
        "case_count": len(cases),
        "duration_seconds": duration_seconds,
        "exit_code": result.returncode,
        "cases": [asdict(case) for case in cases],
        "stdout_tail": result.stdout[-6000:],
        "stderr_tail": result.stderr[-6000:],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic AI quant trading-chain audit tests without reading runtime secrets or submitting real orders.",
    )
    parser.add_argument("--mode", choices=["core", "extended"], default="core")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional path to write the JSON audit report.")
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER, help="Extra pytest args after --, for example -- -vv")
    args = parser.parse_args(argv)
    extra_args = list(args.pytest_args)
    if extra_args[:1] == ["--"]:
        extra_args = extra_args[1:]

    cases = selected_cases(args.mode)
    started_at = time.time()
    result = run_pytest([case.target for case in cases], extra_args)
    report = build_report(cases, result, started_at)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

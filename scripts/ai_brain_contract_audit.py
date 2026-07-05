from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
TAIL_CHARS = 6000

SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9]{12,}"), "sk-[REDACTED]"),
    (re.compile(r"(?i)(api[_-]?secret|secret[_-]?key|private[_-]?key|token|password)\s*[:=]\s*['\"]?[^'\"\s]{8,}"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)(gateio[_-]?api[_-]?key)\s*[:=]\s*['\"]?[^'\"\s]{8,}"), r"\1=[REDACTED]"),
)


@dataclass(frozen=True)
class AuditCase:
    id: str
    target: str
    requirement: str


CORE_CASES: tuple[AuditCase, ...] = (
    AuditCase(
        id="stable_prompt_contract",
        target="tests/test_deepseek_order_json.py::test_deepseek_request_messages_keep_stable_contract_before_dynamic_context",
        requirement="DeepSeek prompt keeps stable contract before dynamic market context.",
    ),
    AuditCase(
        id="structured_trade_prices",
        target="tests/test_deepseek_order_json.py::test_deepseek_decision_requires_structured_trade_prices",
        requirement="AI decision must keep structured entry, take-profit, and stop estimates.",
    ),
    AuditCase(
        id="news_direction_separated_from_alignment",
        target="tests/test_deepseek_order_json.py::test_deepseek_payload_separates_news_direction_from_strategy_alignment",
        requirement="Absolute news direction is separated from strategy-relative alignment.",
    ),
    AuditCase(
        id="five_score_normalization",
        target="tests/test_deepseek_order_json.py::test_deepseek_normalizes_five_score_fields_conservatively",
        requirement="Out-of-range or missing five-score fields normalize conservatively.",
    ),
    AuditCase(
        id="chinese_decision_normalization",
        target="tests/test_deepseek_order_json.py::test_deepseek_normalizes_chinese_decision_terms",
        requirement="Chinese AI output terms normalize to canonical enum values.",
    ),
    AuditCase(
        id="news_alignment_direction_hint",
        target="tests/test_deepseek_order_json.py::test_news_direction_hint_is_converted_relative_to_strategy_direction",
        requirement="Bearish news aligns with short signals and conflicts with long signals.",
    ),
    AuditCase(
        id="unavailable_ai_blocks_entry",
        target="tests/test_deepseek_order_json.py::test_deepseek_unavailable_blocks_entry_even_when_signal_is_strong",
        requirement="Missing DeepSeek credentials fail closed and cannot allow a strong local entry.",
    ),
    AuditCase(
        id="quota_failure_sticks_to_backup",
        target="tests/test_deepseek_order_json.py::test_deepseek_quota_failure_sticks_to_backup_key",
        requirement="Quota/auth exhaustion switches active credential to backup without repeatedly probing primary.",
    ),
    AuditCase(
        id="transient_failure_keeps_primary",
        target="tests/test_deepseek_order_json.py::test_deepseek_transient_failure_uses_backup_once_but_keeps_primary",
        requirement="Transient primary failures can use backup once but do not permanently demote primary.",
    ),
    AuditCase(
        id="budget_hourly_limit_blocks",
        target="tests/test_deepseek_budget.py::test_deepseek_budget_blocks_hourly_limit",
        requirement="Hourly DeepSeek call budget blocks excessive calls.",
    ),
    AuditCase(
        id="budget_event_dedup",
        target="tests/test_deepseek_budget.py::test_deepseek_budget_deduplicates_news_event_across_restarts",
        requirement="Major news events are deduplicated across process restarts.",
    ),
    AuditCase(
        id="budget_block_cannot_allow_entry",
        target="tests/test_deepseek_budget.py::test_budget_block_reason_cannot_allow_strong_entry_signal",
        requirement="Budget fallback cannot allow a strong entry signal.",
    ),
    AuditCase(
        id="skip_no_signal_no_position",
        target="tests/test_deepseek_budget.py::test_hourly_cycle_skips_deepseek_without_signal_or_position",
        requirement="Flat no-signal hourly cycles skip DeepSeek and write an audit record.",
    ),
    AuditCase(
        id="runtime_state_ignores_credentials",
        target="tests/test_control.py::test_runtime_state_ignores_deepseek_credential_rows",
        requirement="Runtime state snapshots do not leak DeepSeek credential routing rows.",
    ),
    AuditCase(
        id="live_sizing_policy_v2",
        target="tests/test_risk.py::test_calibrated_v2_loss_aware_can_be_selected_as_live_sizing_policy",
        requirement="Calibrated v2 loss-aware policy remains selectable by RiskManager.",
    ),
    AuditCase(
        id="hybrid_subjective_lift_guard",
        target="tests/test_risk.py::test_hybrid_subjective_guarded_v2_promotes_only_one_tier_above_v2_base",
        requirement="Hybrid subjective AI proposal can promote only one tier above v2 base.",
    ),
    AuditCase(
        id="hybrid_subjective_reduce",
        target="tests/test_risk.py::test_hybrid_subjective_guarded_v2_can_reduce_below_v2_base",
        requirement="Hybrid subjective AI proposal can reduce below v2 base.",
    ),
    AuditCase(
        id="major_news_no_signal_blocks",
        target="tests/test_risk.py::test_major_news_without_strategy_signal_blocks_explicitly",
        requirement="Major news without local strategy signal cannot create an entry.",
    ),
    AuditCase(
        id="major_news_conflict_hard_block",
        target="tests/test_risk.py::test_major_news_direction_conflict_with_direct_crypto_impact_is_hard_block",
        requirement="Direct high-impact news conflict remains a hard block.",
    ),
    AuditCase(
        id="major_news_aligned_needs_quality",
        target="tests/test_risk.py::test_major_news_aligned_full_size_requires_orderflow_and_dense_zone_quality",
        requirement="Aligned major news cannot reach full size without orderflow and dense-zone quality.",
    ),
)


EXTENDED_CASES: tuple[AuditCase, ...] = (
    AuditCase(
        id="btc_bearish_aligns_eth_short",
        target="tests/test_deepseek_budget.py::test_market_leader_context_aligns_btc_bearish_move_with_eth_short_signal",
        requirement="BTC leader bearish context aligns with ETH short signal.",
    ),
    AuditCase(
        id="eth_rotation_not_false_conflict",
        target="tests/test_deepseek_budget.py::test_market_leader_context_detects_eth_lagged_rotation_during_btc_pullback",
        requirement="ETH lagged rotation prevents false BTC pullback conflict.",
    ),
    AuditCase(
        id="btc_breakdown_conflicts_eth_long",
        target="tests/test_deepseek_budget.py::test_market_leader_context_keeps_btc_breakdown_as_conflict",
        requirement="BTC breakdown remains a conflict for ETH long signals.",
    ),
    AuditCase(
        id="major_news_low_crypto_impact_caps",
        target="tests/test_risk.py::test_major_news_conflict_with_low_crypto_impact_caps_weak_instead_of_blocking",
        requirement="Low crypto-impact news conflict caps weak instead of hard blocking.",
    ),
    AuditCase(
        id="major_news_unknown_caps_normal",
        target="tests/test_risk.py::test_major_news_unknown_direction_caps_position_at_normal",
        requirement="Unknown major-news direction caps position at normal.",
    ),
    AuditCase(
        id="aligned_extreme_event_caps_weak",
        target="tests/test_risk.py::test_major_news_aligned_extreme_event_risk_caps_weak_not_hard_block",
        requirement="Aligned but extreme event risk caps weak instead of blindly allowing size.",
    ),
    AuditCase(
        id="btc_leader_conflict_caps",
        target="tests/test_risk.py::test_btc_leader_conflict_caps_but_does_not_invent_direction_or_auto_block",
        requirement="BTC leader conflict caps risk but does not invent a direction or auto-block.",
    ),
    AuditCase(
        id="weak_pattern_caps_position",
        target="tests/test_risk.py::test_weak_pattern_confirmation_caps_position_even_with_strong_news_and_orderflow",
        requirement="Weak pattern confirmation caps position even with strong news and orderflow.",
    ),
)


def selected_cases(mode: str) -> tuple[AuditCase, ...]:
    if mode == "extended":
        return CORE_CASES + EXTENDED_CASES
    return CORE_CASES


def _redact(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    cleaned = text[-TAIL_CHARS:]
    for pattern, replacement in SECRET_REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def run_pytest(targets: Sequence[str], extra_args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "pytest", "-q", *targets, *extra_args]
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def build_report(cases: Sequence[AuditCase], result: subprocess.CompletedProcess[str], started_at: float) -> dict[str, object]:
    return {
        "ok": result.returncode == 0,
        "mode": "ai_brain_contract_audit",
        "case_count": len(cases),
        "duration_seconds": round(time.time() - started_at, 3),
        "exit_code": result.returncode,
        "cases": [asdict(case) for case in cases],
        "stdout_tail": _redact(result.stdout),
        "stderr_tail": _redact(result.stderr),
    }


def print_report(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe_text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic AI-brain contract audit tests without reading runtime secrets or calling real DeepSeek.",
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
    file_text = json.dumps(report, ensure_ascii=False, indent=2)
    console_text = json.dumps(report, ensure_ascii=True, indent=2)
    print_report(console_text)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(file_text + "\n", encoding="utf-8")
    return int(result.returncode)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ai_quant_trader.research.walk_forward import (
    WalkForwardTrade,
    WalkForwardWindow,
    ai_reduce_overlay,
    ai_veto_overlay,
    evaluate_walk_forward_harness,
    score_policy,
)


def _trade(day: str, pnl: float, *, risk: float = 0.2, consensus: float = 0.8, kc_scalar: float = 2.8) -> WalkForwardTrade:
    return WalkForwardTrade(
        signal_time=f"2025-01-{day}T00:00:00+00:00",
        pnl=pnl,
        baseline_equity_before=1000.0,
        fee_paid=0.4,
        slippage_paid=0.1,
        funding_paid=0.05,
        max_adverse_excursion_pct=-0.03 if pnl > 0 else -0.08,
        params={"kc_scalar": kc_scalar, "volume_multiple": 2.5},
        scores={
            "risk_score": risk,
            "loss_risk_score": risk,
            "consensus_score": consensus,
            "orderflow_confirmation_score": consensus,
            "pattern_confirmation_score": consensus,
        },
    )


def _window() -> WalkForwardWindow:
    return WalkForwardWindow(
        train_start="2025-01-01",
        train_end="2025-01-11",
        validation_start="2025-01-11",
        validation_end="2025-01-21",
        out_of_sample_start="2025-01-21",
        out_of_sample_end="2025-02-01",
    )


def test_score_policy_reports_required_realism_metrics() -> None:
    result = score_policy([_trade("01", 10), _trade("02", -5, risk=0.9)], ai_veto_overlay)

    assert result["return_pct"] == pytest.approx(1.0)
    assert result["signal_count"] == 2
    assert result["trade_count"] == 1
    assert result["blocked_count"] == 1
    assert result["win_rate_pct"] == pytest.approx(100.0)
    assert result["profit_factor"] == pytest.approx(999.0)
    assert result["cost_ratio"] > 0
    assert result["max_adverse_excursion_pct"] < 0
    assert result["parameter_stability"] > 0.9


def test_walk_forward_rejects_candidate_that_only_improves_in_sample() -> None:
    trades = []
    for index in range(1, 11):
        trades.append(_trade(f"{index:02d}", 10, risk=0.2))
    for index in range(11, 21):
        trades.append(_trade(f"{index:02d}", -10, risk=0.2))
    for index in range(21, 31):
        trades.append(_trade(f"{index:02d}", -8, risk=0.2))

    def overfit_candidate(trade: WalkForwardTrade) -> float:
        return 1.0 if trade.signal_time < "2025-01-11" else 0.0

    result = evaluate_walk_forward_harness(
        trades,
        [_window()],
        candidate_policy=overfit_candidate,
        min_validation_trades=5,
        min_oos_trades=5,
    )

    assert result["status"] == "rejected"
    assert "validation_insufficient_trades" in result["rejected_reasons"]
    assert "out_of_sample_insufficient_trades" in result["rejected_reasons"]
    assert result["aggregate"]["baseline_trend"]["validation"]["trade_count"] == 10


def test_walk_forward_accepts_candidate_with_validation_and_oos_improvement() -> None:
    trades = []
    for index in range(1, 31):
        high_risk_loser = index in {12, 15, 23, 26}
        trades.append(
            _trade(
                f"{index:02d}",
                -20 if high_risk_loser else 8,
                risk=0.9 if high_risk_loser else 0.2,
            )
        )

    result = evaluate_walk_forward_harness(
        trades,
        [_window()],
        candidate_name="risk_reduce_candidate",
        candidate_policy=ai_reduce_overlay,
        min_validation_trades=5,
        min_oos_trades=5,
    )

    assert result["status"] == "accepted"
    assert result["rejected_reasons"] == []
    assert (
        result["aggregate"]["risk_reduce_candidate"]["validation"]["return_pct"]
        > result["aggregate"]["baseline_trend"]["validation"]["return_pct"]
    )
    assert (
        result["aggregate"]["risk_reduce_candidate"]["out_of_sample"]["max_drawdown_pct"]
        >= result["aggregate"]["baseline_trend"]["out_of_sample"]["max_drawdown_pct"]
    )


def test_walk_forward_harness_cli_writes_report(tmp_path) -> None:
    payload = {
        "trades": [
            {
                "signal_time": f"2025-01-{index:02d}T00:00:00+00:00",
                "pnl": 5,
                "baseline_equity_before": 1000,
                "scores": {"risk_score": 0.2, "consensus_score": 0.8},
            }
            for index in range(1, 31)
        ],
        "windows": [
            {
                "train_start": "2025-01-01",
                "train_end": "2025-01-11",
                "validation_start": "2025-01-11",
                "validation_end": "2025-01-21",
                "out_of_sample_start": "2025-01-21",
                "out_of_sample_end": "2025-02-01",
            }
        ],
    }
    input_path = tmp_path / "ledger.json"
    output_path = tmp_path / "report.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/walk_forward_harness.py",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert '"status": "research_only"' in completed.stdout
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert report["aggregate"]["baseline_trend"]["validation"]["trade_count"] == 10

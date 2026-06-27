from __future__ import annotations

from pathlib import Path

from ai_quant_trader.core.models import AggregatedOrderflow, AiDecision, RiskDecision, SignalAction, StrategySignal
from ai_quant_trader.research.live_factor_archive import build_live_factor_snapshot
from ai_quant_trader.storage.sqlite import SQLiteStore


def _signal() -> StrategySignal:
    return StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.LONG,
        current_price=2000,
        signal_strength=0.82,
        technical_evidence={
            "volume_multiple": 2.8,
            "pattern_type": "ascending_triangle",
            "pnl": 12.0,
            "nested": {"mae_pct": -1.2, "entry_quality_score": 0.7},
        },
    )


def _ai() -> AiDecision:
    return AiDecision(
        symbol="ETH/USDT:USDT",
        regime="trend",
        direction="long",
        confidence=0.76,
        multiplier=1.0,
        news_alignment="aligned",
        orderflow_alignment="aligned",
        btc_leader_alignment="neutral",
        btc_leader_regime="btc_lead_trend",
        news_risk_score=0.2,
        news_direction_alignment_score=0.72,
        crypto_market_impact_score=0.65,
        symbol_news_impact_score=0.58,
        btc_leader_impact_score=0.45,
        eth_btc_rotation_score=0.55,
        orderflow_confirmation_score=0.8,
        dense_zone_position="above_value",
        dense_zone_breakout_score=0.7,
        pattern_type="ascending_triangle",
        pattern_confirmation_score=0.68,
        trend_confirmation_score=0.74,
        range_risk_score=0.18,
        veto_action="allow",
    )


def _risk() -> RiskDecision:
    return RiskDecision(
        allowed=True,
        action=SignalAction.LONG,
        symbol="ETH/USDT:USDT",
        target_qty=0.2,
        position_tier="strong",
        position_scale=0.75,
        score_breakdown={"volume_multiple": 0.82, "news_direction_alignment_score": 0.72},
    )


def test_live_factor_snapshot_excludes_outcome_leakage() -> None:
    snapshot = build_live_factor_snapshot(
        signal=_signal(),
        ai=_ai(),
        risk=_risk(),
        orderflow=AggregatedOrderflow(
            symbol="ETH/USDT:USDT",
            data_quality=0.9,
            source_count=3,
            spread_bps=1.2,
            depth_usd=1_000_000,
            large_trade_events=2,
        ),
    )

    evidence = snapshot.live_factors["technical_evidence"]
    assert snapshot.archive_status == "shadow_only"
    assert snapshot.position_tier == "strong"
    assert snapshot.position_scale == 0.75
    assert snapshot.live_factors["orderflow_data_quality"] == 0.9
    assert "pnl" not in evidence
    assert "mae_pct" not in evidence["nested"]
    assert evidence["nested"]["entry_quality_score"] == 0.7


def test_live_factor_snapshot_persists_to_sqlite(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "trader.sqlite3", tmp_path / "audit.jsonl")
    snapshot = build_live_factor_snapshot(signal=_signal(), ai=_ai(), risk=_risk())

    store.insert("live_factor_snapshots", snapshot, "ETH/USDT:USDT")
    latest = store.fetch_latest("live_factor_snapshots", "ETH/USDT:USDT")

    assert latest is not None
    assert latest["payload"]["source"] == "trading_cycle"
    assert latest["payload"]["position_tier"] == "strong"
    store.close()

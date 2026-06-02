from __future__ import annotations

import json

import pytest

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    Alignment,
    DenseZone,
    NewsDigest,
    NewsItem,
    PatternCandidate,
    RegimePattern,
    SignalAction,
    StrategySignal,
)


@pytest.mark.asyncio
async def test_deepseek_decision_requires_structured_trade_prices(monkeypatch) -> None:
    brain = DeepSeekBrain(api_key="test-key", model="deepseek-v4-pro")

    async def fake_chat_json(payload, timeout_seconds: int, retries: int):  # noqa: ANN001
        assert payload["technical_signal"]["action"] == "long"
        assert payload["regime_pattern"]["strategy_allowed"] == "trend"
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "symbol": "ETH/USDT:USDT",
                                "regime": "trend",
                                "direction": "long",
                                "confidence": 0.78,
                                "multiplier": 1.1,
                                "news_alignment": "aligned",
                                "orderflow_alignment": "aligned",
                                "dense_zone_position": "above_poc",
                                "entry_zone_estimate": 3500.0,
                                "tp_estimate": 3710.0,
                                "sl_estimate": 3415.0,
                                "action_suggestion": "open_long",
                                "veto_action": "allow",
                                "brief_reason": "技术突破、订单流和消息面同向。",
                                "reason_codes": ["trend_breakout", "news_aligned"],
                                "data_quality_warnings": [],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(brain, "_chat_json", fake_chat_json)
    decision = await brain.analyze_symbol(
        StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.LONG,
            current_price=3500.0,
            suggested_qty=0.5,
            signal_strength=0.82,
        ),
        AggregatedOrderflow(symbol="ETH/USDT:USDT", alignment_hint="aligned", data_quality=0.9, source_count=3),
        DenseZone(symbol="ETH/USDT:USDT", poc=3480.0, vah=3600.0, val=3360.0, current_position="above_value", strength=0.7),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="rectangle_breakout", confidence=0.72),
        NewsDigest(items=[NewsItem(title="Fed signals fewer cuts as core PCE holds at 2.8%", source="test")]),
        RegimePattern(
            symbol="ETH/USDT:USDT",
            regime_candidate="trend",
            strategy_allowed="trend",
            pattern_family="trend_continuation",
            pattern_name="rectangle_breakout",
            breakout_quality="strong",
            trend_score=0.78,
            range_score=0.22,
            reason_codes=["trend_score_dominant"],
        ),
    )

    assert decision.action_suggestion == "open_long"
    assert decision.tp_estimate == 3710.0
    assert decision.sl_estimate == 3415.0
    assert decision.entry_zone_estimate == 3500.0
    assert decision.veto_action == "allow"
    assert decision.trend_confirmation_score == 0.35
    assert decision.range_risk_score == 0.65
    assert decision.news_risk_score == 0.65
    assert decision.orderflow_confirmation_score == 0.35
    assert decision.dense_zone_breakout_score == 0.35


@pytest.mark.asyncio
async def test_deepseek_payload_separates_news_direction_from_strategy_alignment(monkeypatch) -> None:
    brain = DeepSeekBrain(api_key="test-key", model="deepseek-v4-pro")

    async def fake_chat_json(payload, timeout_seconds: int, retries: int):  # noqa: ANN001
        assert payload["technical_signal"]["action"] == "short"
        assert payload["news_direction_hint"] == "bearish"
        assert payload["news_strategy_alignment_hint"] == "aligned"
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "symbol": "ETH/USDT:USDT",
                                "regime": "trend",
                                "direction": "short",
                                "confidence": 0.72,
                                "multiplier": 0.8,
                                "news_alignment": "aligned",
                                "orderflow_alignment": "aligned",
                                "dense_zone_position": "below_value",
                                "action_suggestion": "open_short",
                                "veto_action": "reduce",
                                "brief_reason": "利空消息与做空信号同向，但事件风险较高，降仓。",
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(brain, "_chat_json", fake_chat_json)
    decision = await brain.analyze_symbol(
        StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.SHORT,
            current_price=1900.0,
            suggested_qty=1.0,
            signal_strength=0.82,
        ),
        AggregatedOrderflow(symbol="ETH/USDT:USDT", alignment_hint="aligned", data_quality=0.9, source_count=3),
        DenseZone(symbol="ETH/USDT:USDT", poc=1950.0, vah=2010.0, val=1920.0, current_position="below_value", strength=0.7),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="rectangle_breakdown", confidence=0.72),
        NewsDigest(
            items=[NewsItem(title="Fed hawkish comments pressure risk assets", source="test")],
            crypto_sentiment=Alignment.CONFLICT,
        ),
    )

    assert decision.direction == "short"
    assert decision.news_alignment == "aligned"
    assert decision.veto_action == "reduce"


def test_deepseek_normalizes_five_score_fields_conservatively() -> None:
    brain = DeepSeekBrain(api_key="test-key")

    parsed = brain._normalize_decision_json(
        {
            "regime": "trend",
            "direction": "long",
            "confidence": 0.8,
            "multiplier": 1.0,
            "veto_action": "allow",
            "trend_confirmation_score": 3.0,
            "range_risk_score": -1.0,
            "news_risk_score": "not-a-number",
            "orderflow_confirmation_score": None,
            "dense_zone_breakout_score": 0.72,
        }
    )

    decision = AiDecision.model_validate({"symbol": "ETH/USDT:USDT", **parsed})

    assert decision.trend_confirmation_score == 1.0
    assert decision.range_risk_score == 0.0
    assert decision.news_risk_score == 0.65
    assert decision.orderflow_confirmation_score == 0.35
    assert decision.dense_zone_breakout_score == 0.72


def test_deepseek_normalizes_chinese_decision_terms() -> None:
    brain = DeepSeekBrain(api_key="test-key")

    parsed = brain._normalize_decision_json(
        {
            "regime": "震荡",
            "direction": "做空",
            "news_alignment": "冲突",
            "orderflow_alignment": "同向",
            "veto_action": "降仓",
        }
    )

    assert parsed["regime"] == "range"
    assert parsed["direction"] == "short"
    assert parsed["news_alignment"] == "conflict"
    assert parsed["orderflow_alignment"] == "aligned"
    assert parsed["veto_action"] == "reduce"


def test_news_direction_hint_is_converted_relative_to_strategy_direction() -> None:
    brain = DeepSeekBrain(api_key="test-key")
    bearish_news = NewsDigest(summary="加息和制裁压制风险资产", crypto_sentiment=Alignment.CONFLICT)
    short_signal = StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.SHORT,
        current_price=1900.0,
        suggested_qty=1.0,
    )
    long_signal = short_signal.model_copy(update={"action": SignalAction.LONG})

    assert brain._news_direction_hint(bearish_news) == "bearish"
    assert brain._news_alignment_for_signal(bearish_news, short_signal) == Alignment.ALIGNED
    assert brain._news_alignment_for_signal(bearish_news, long_signal) == Alignment.CONFLICT


@pytest.mark.asyncio
async def test_deepseek_unavailable_blocks_entry_even_when_signal_is_strong(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    brain = DeepSeekBrain(api_key=None)

    decision = await brain.analyze_symbol(
        StrategySignal(
            symbol="ETH/USDT:USDT",
            timeframe="1h",
            action=SignalAction.LONG,
            current_price=3500.0,
            suggested_qty=0.5,
            signal_strength=0.9,
        ),
        AggregatedOrderflow(symbol="ETH/USDT:USDT", alignment_hint="aligned", data_quality=0.95, source_count=3),
        DenseZone(
            symbol="ETH/USDT:USDT",
            poc=3480.0,
            vah=3600.0,
            val=3360.0,
            current_position="above_value",
            strength=0.8,
            trend_score=0.8,
        ),
        PatternCandidate(symbol="ETH/USDT:USDT", pattern_type="rectangle_breakout", confidence=0.8),
        NewsDigest(items=[NewsItem(title="Macro calendar quiet", source="test")]),
        RegimePattern(
            symbol="ETH/USDT:USDT",
            regime_candidate="trend",
            strategy_allowed="trend",
            pattern_family="trend_continuation",
            pattern_name="rectangle_breakout",
            breakout_quality="strong",
            trend_score=0.85,
            range_score=0.15,
            reason_codes=["trend_score_dominant"],
        ),
    )

    assert decision.veto_action == "block"
    assert decision.action_suggestion == "block"
    assert decision.confidence == 0.0
    assert "missing_deepseek_api_key" in decision.reason_codes

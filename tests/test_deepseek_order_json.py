from __future__ import annotations

import json

import pytest
import requests

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    Alignment,
    DenseZone,
    MarketLeaderContext,
    NewsDigest,
    NewsDirection,
    NewsItem,
    PatternCandidate,
    RegimePattern,
    SignalAction,
    StrategySignal,
)
from ai_quant_trader.storage.sqlite import SQLiteStore


@pytest.mark.asyncio
async def test_deepseek_chat_json_falls_back_to_backup_api_key(monkeypatch) -> None:
    primary_key = "primary-" + "test-key"
    backup_key = "backup-" + "test-key"
    brain = DeepSeekBrain(api_key=primary_key, model="deepseek-v4-flash")
    brain.backup_api_key = backup_key
    seen_keys: list[str] = []

    def fake_chat_json_sync(payload, timeout_seconds: int, api_key: str):  # noqa: ANN001
        seen_keys.append(api_key)
        if api_key == primary_key:
            raise requests.RequestException("primary timeout")
        return {"choices": [{"message": {"content": "{}"}}]}

    monkeypatch.setattr(brain, "_chat_json_sync", fake_chat_json_sync)
    data = await brain._chat_json({"messages": []}, timeout_seconds=1, retries=1)

    assert data == {"choices": [{"message": {"content": "{}"}}]}
    assert seen_keys == [primary_key, backup_key]


@pytest.mark.asyncio
async def test_deepseek_chat_json_falls_back_when_primary_returns_invalid_json(monkeypatch) -> None:
    primary_key = "primary-" + "test-key"
    backup_key = "backup-" + "test-key"
    brain = DeepSeekBrain(api_key=primary_key, model="deepseek-v4-flash")
    brain.backup_api_key = backup_key
    seen_keys: list[str] = []

    def fake_chat_json_sync(payload, timeout_seconds: int, api_key: str):  # noqa: ANN001
        seen_keys.append(api_key)
        if api_key == primary_key:
            return {"choices": [{"message": {"content": "not-json"}}]}
        return {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}

    monkeypatch.setattr(brain, "_chat_json_sync", fake_chat_json_sync)
    data = await brain._chat_json({"messages": []}, timeout_seconds=1, retries=1)

    assert json.loads(data["choices"][0]["message"]["content"]) == {"ok": True}
    assert seen_keys == [primary_key, backup_key]


@pytest.mark.asyncio
async def test_deepseek_quota_failure_sticks_to_backup_key(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(str(tmp_path / "trader.sqlite3"), str(tmp_path / "audit.jsonl"))
    primary_key = "primary-" + "test-key"
    backup_key = "backup-" + "test-key"
    brain = DeepSeekBrain(api_key=primary_key, model="deepseek-v4-flash", store=store)
    brain.backup_api_key = backup_key
    seen_keys: list[str] = []

    def quota_error() -> requests.HTTPError:
        response = requests.Response()
        response.status_code = 402
        error = requests.HTTPError("Insufficient Balance")
        error.response = response
        return error

    def fake_chat_json_sync(payload, timeout_seconds: int, api_key: str):  # noqa: ANN001
        seen_keys.append(api_key)
        if api_key == primary_key:
            raise quota_error()
        return {
            "choices": [{"message": {"content": json.dumps({"ok": True})}}],
            "usage": {
                "prompt_tokens": 10,
                "prompt_cache_hit_tokens": 7,
                "prompt_cache_miss_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 12,
            },
        }

    try:
        monkeypatch.setattr(brain, "_chat_json_sync", fake_chat_json_sync)
        await brain._chat_json({"messages": ["first"]}, timeout_seconds=1, retries=1, call_type="trading_cycle", symbol="ETH/USDT:USDT")
        await brain._chat_json({"messages": ["second"]}, timeout_seconds=1, retries=1, call_type="trading_cycle", symbol="ETH/USDT:USDT")

        assert seen_keys == [primary_key, backup_key, backup_key]
        state = store.fetch_latest("runtime_state", "deepseek_credentials")
        assert state is not None
        assert state["payload"]["active_label"] == "backup"
        usage_rows = store.fetch_payloads("ai_call_usage_events", limit=10, symbol="ETH/USDT:USDT")
        assert any(row["payload"]["prompt_cache_hit_tokens"] == 7 for row in usage_rows)
        assert any(row["payload"]["credential_label"] == "backup" for row in usage_rows)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_deepseek_transient_failure_uses_backup_once_but_keeps_primary(monkeypatch) -> None:
    primary_key = "primary-" + "test-key"
    backup_key = "backup-" + "test-key"
    brain = DeepSeekBrain(api_key=primary_key, model="deepseek-v4-flash")
    brain.backup_api_key = backup_key
    seen_keys: list[str] = []
    primary_failures_remaining = 1

    def fake_chat_json_sync(payload, timeout_seconds: int, api_key: str):  # noqa: ANN001
        nonlocal primary_failures_remaining
        seen_keys.append(api_key)
        if api_key == primary_key and primary_failures_remaining > 0:
            primary_failures_remaining -= 1
            raise requests.Timeout("temporary timeout")
        return {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}

    monkeypatch.setattr(brain, "_chat_json_sync", fake_chat_json_sync)
    await brain._chat_json({"messages": ["first"]}, timeout_seconds=1, retries=1)
    await brain._chat_json({"messages": ["second"]}, timeout_seconds=1, retries=1)

    assert seen_keys == [primary_key, backup_key, primary_key]


def test_deepseek_request_messages_keep_stable_contract_before_dynamic_context() -> None:
    brain = DeepSeekBrain(api_key="test-key", model="deepseek-v4-flash")
    messages = brain._request_messages({"technical_signal": {"action": "long", "current_price": 1234.56}})

    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    payload = json.loads(messages[1]["content"])
    assert list(payload.keys()) == ["stable_contract", "dynamic_context"]
    assert payload["stable_contract"]["ai_role"] == "confirm_reduce_or_block_only"
    assert "btc_leader_impact_score" in payload["stable_contract"]["required_scores"]
    assert "eth_btc_rotation_score" in payload["stable_contract"]["required_scores"]
    assert "news_direction_alignment_score" in payload["stable_contract"]["required_scores"]
    assert payload["stable_contract"]["score_semantics"]["btc_leader_alignment"].startswith("BTC")
    assert "relative-strength" in payload["stable_contract"]["score_semantics"]["eth_btc_rotation_score"]
    assert "directional confirmation" in payload["stable_contract"]["score_semantics"]["news_direction_alignment_score"]
    assert "participation" in payload["stable_contract"]["score_semantics"]["orderflow_confirmation_score"]
    assert "not simple CVD direction" in payload["stable_contract"]["score_semantics"]["orderflow_confirmation_score"]
    assert payload["dynamic_context"]["technical_signal"]["action"] == "long"


@pytest.mark.asyncio
async def test_deepseek_decision_requires_structured_trade_prices(monkeypatch) -> None:
    brain = DeepSeekBrain(api_key="test-key", model="deepseek-v4-pro")

    async def fake_chat_json(payload, timeout_seconds: int, retries: int, **kwargs):  # noqa: ANN001, ANN003
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

    async def fake_chat_json(payload, timeout_seconds: int, retries: int, **kwargs):  # noqa: ANN001, ANN003
        assert payload["technical_signal"]["action"] == "short"
        assert payload["news_direction_hint"] == "bearish"
        assert payload["news_strategy_alignment_hint"] == "aligned"
        assert payload["market_leader_context"]["symbol"] == "BTC/USDT:USDT"
        assert payload["market_leader_context"]["strategy_alignment_hint"] == "aligned"
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
                                "btc_leader_alignment": "aligned",
                                "crypto_market_impact_score": 0.8,
                                "btc_leader_impact_score": 0.7,
                                "symbol_news_impact_score": 0.6,
                                "pattern_confirmation_score": 0.74,
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
        market_leader_context=MarketLeaderContext(
            available=True,
            price=100_000,
            change_1h_pct=-1.2,
            change_4h_pct=-2.4,
            change_24h_pct=-3.0,
            market_direction=NewsDirection.BEARISH,
            strategy_alignment_hint=Alignment.ALIGNED,
            impact_score=0.7,
        ),
    )

    assert decision.direction == "short"
    assert decision.news_alignment == "aligned"
    assert decision.btc_leader_alignment == "aligned"
    assert decision.crypto_market_impact_score == 0.8
    assert decision.btc_leader_impact_score == 0.7
    assert decision.symbol_news_impact_score == 0.6
    assert decision.pattern_confirmation_score == 0.74
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
            "crypto_market_impact_score": 2.0,
            "btc_leader_impact_score": -1.0,
            "symbol_news_impact_score": "bad",
            "pattern_confirmation_score": None,
            "orderflow_confirmation_score": None,
            "dense_zone_breakout_score": 0.72,
        }
    )

    decision = AiDecision.model_validate({"symbol": "ETH/USDT:USDT", **parsed})

    assert decision.trend_confirmation_score == 1.0
    assert decision.range_risk_score == 0.0
    assert decision.news_risk_score == 0.65
    assert decision.crypto_market_impact_score == 1.0
    assert decision.btc_leader_impact_score == 0.0
    assert decision.symbol_news_impact_score == 0.0
    assert decision.pattern_confirmation_score == 0.5
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


def test_news_direction_field_keeps_absolute_direction_separate_from_strategy_alignment() -> None:
    brain = DeepSeekBrain(api_key="test-key")
    bearish_news = NewsDigest(summary="bearish macro", news_direction=NewsDirection.BEARISH)
    short_signal = StrategySignal(
        symbol="ETH/USDT:USDT",
        timeframe="1h",
        action=SignalAction.SHORT,
        current_price=1900.0,
        suggested_qty=1.0,
    )
    long_signal = short_signal.model_copy(update={"action": SignalAction.LONG})

    assert bearish_news.crypto_sentiment == Alignment.CONFLICT
    assert brain._news_direction_hint(bearish_news) == "bearish"
    assert brain._news_alignment_for_signal(bearish_news, short_signal) == Alignment.ALIGNED
    assert brain._news_alignment_for_signal(bearish_news, long_signal) == Alignment.CONFLICT


def test_news_direction_alignment_score_is_zeroed_when_alignment_is_not_aligned() -> None:
    brain = DeepSeekBrain(api_key="test-key", model="deepseek-v4-pro")

    parsed = brain._normalize_decision_json(
        {
            "regime": "trend",
            "direction": "long",
            "confidence": 0.8,
            "multiplier": 1.0,
            "veto_action": "allow",
            "news_alignment": "neutral",
            "news_direction_alignment_score": 0.95,
        }
    )

    assert parsed["news_alignment"] == "neutral"
    assert parsed["news_direction_alignment_score"] == 0.0


def test_legacy_crypto_sentiment_still_normalizes_to_news_direction() -> None:
    bearish_news = NewsDigest(summary="legacy bearish", crypto_sentiment=Alignment.CONFLICT)
    bullish_news = NewsDigest(summary="legacy bullish", crypto_sentiment=Alignment.ALIGNED)

    assert bearish_news.news_direction == NewsDirection.BEARISH
    assert bullish_news.news_direction == NewsDirection.BULLISH


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

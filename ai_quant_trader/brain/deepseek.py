from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import aiohttp
import requests
from pydantic import ValidationError

from ai_quant_trader.brain.knowledge import TradingKnowledgeBase
from ai_quant_trader.core.models import (
    AggregatedOrderflow,
    AiDecision,
    Alignment,
    DenseZone,
    MarketRegime,
    NewsDigest,
    PatternCandidate,
    RegimePattern,
    Side,
    SignalAction,
    StrategySignal,
    VetoAction,
)
from ai_quant_trader.data.macro_entities import MacroEntityStore

logger = logging.getLogger(__name__)


class DeepSeekBrain:
    """DeepSeek 决策中枢。

    常规决策默认使用 deepseek-v4-pro。API 不可用时系统保守降级：
    不追单、不满仓，必要时阻断开仓。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        knowledge_base: TradingKnowledgeBase | None = None,
        macro_entities: MacroEntityStore | None = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_DECISION_MODEL") or "deepseek-v4-pro"
        self.knowledge_base = knowledge_base or TradingKnowledgeBase()
        self.macro_entities = macro_entities or MacroEntityStore()

    def reload_from_env(self) -> None:
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        self.base_url = (os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.model = os.getenv("DEEPSEEK_DECISION_MODEL") or self.model

    async def analyze_symbol(
        self,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        regime_pattern: RegimePattern | None = None,
    ) -> AiDecision:
        payload = self._compact_payload(self._build_payload(signal, orderflow, dense_zone, pattern, news, regime_pattern))
        if not self.api_key:
            return self._fallback_decision(signal, orderflow, dense_zone, pattern, news, "missing_deepseek_api_key", regime_pattern)

        try:
            data = await self._chat_json(payload, timeout_seconds=75, retries=3)
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            parsed = self._extract_decision_json(parsed)
            parsed = self._normalize_decision_json(parsed)
            parsed.setdefault("symbol", signal.symbol)
            return AiDecision.model_validate(parsed)
        except (aiohttp.ClientError, requests.RequestException, KeyError, json.JSONDecodeError, ValidationError, TimeoutError, asyncio.TimeoutError) as exc:
            logger.warning("DeepSeek 分析失败，使用保守降级决策: %r", exc)
            return self._fallback_decision(signal, orderflow, dense_zone, pattern, news, f"deepseek_error:{type(exc).__name__}", regime_pattern)

    def local_fallback_decision(
        self,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        reason: str,
        regime_pattern: RegimePattern | None = None,
    ) -> AiDecision:
        return self._fallback_decision(signal, orderflow, dense_zone, pattern, news, reason, regime_pattern)

    async def _chat_json(self, payload: dict[str, Any], timeout_seconds: int, retries: int) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return await asyncio.to_thread(self._chat_json_sync, payload, timeout_seconds)
            except (aiohttp.ClientError, requests.RequestException, TimeoutError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
        if last_exc:
            raise last_exc
        raise TimeoutError("deepseek_no_response")

    def _chat_json_sync(self, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _system_prompt(self) -> str:
        return (
            "你是客观、冷静、专业的加密货币量化交易员。"
            "你只能输出一个严格 JSON 对象，不允许输出 Markdown 或解释性正文。"
            "所有自然语言字段必须使用简体中文，尤其是 brief_reason、reason_codes、data_quality_warnings。"
            "JSON 必须直接包含 regime、direction、confidence、multiplier、veto_action、"
            "news_alignment、orderflow_alignment、dense_zone_position、entry_zone_estimate、"
            "tp_estimate、sl_estimate、action_suggestion、brief_reason、reason_codes、"
            "trend_confirmation_score、range_risk_score、news_risk_score、"
            "orderflow_confirmation_score、dense_zone_breakout_score。"
            "五个分数字段必须是 0 到 1 之间的小数："
            "trend_confirmation_score 越高代表趋势信号越可靠；"
            "range_risk_score 越高代表震荡/假突破风险越高；"
            "news_risk_score 越高代表事件和消息面风险越高；"
            "orderflow_confirmation_score 越高代表订单流越支持本地技术方向；"
            "dense_zone_breakout_score 越高代表密集区突破或迁移质量越好。"
            "枚举只能使用：regime=trend/range/uncertain；direction=long/short/flat；"
            "news_alignment 和 orderflow_alignment=aligned/conflict/neutral/unknown；"
            "veto_action=allow/reduce/block；action_suggestion=open_long/open_short/reduce/close/hold/block。"
            "entry_zone_estimate、tp_estimate、sl_estimate 没有明确价格时必须填 null，不能填字符串。"
            "你必须像交易员一样给出可执行主倾向：优先在 trend/range 中二选一，direction 优先在 long/short 中二选一；"
            "只有数据明显缺失、信号互相抵消或事件风险无法定价时，才允许使用 uncertain 或 flat。"
            "交易规则：本地技术策略信号是自动开仓必要条件。没有 LONG/SHORT 技术信号时，"
            "你不能建议自动开仓；如果信心超过65%，只能生成候选交易计划并等待人工审批。"
            "你的核心职责是识别趋势、震荡、混沌和事件风险，判断消息面、订单流、形态、交易密集区是否印证技术信号。"
            "交易密集区结构定义：价格在 dense_zone.zone_low 到 zone_high 内反复测试，且 range_score 高于 trend_score，优先视为震荡；"
            "价格脱离一个密集区并进入 vacuum_low 到 vacuum_high 的真空区，且 trend_score 高，说明趋势推进阻力较小；"
            "价格突破旧密集区上沿后回踩不破，旧阻力转为支撑，可提高多头确认度；"
            "价格跌破旧密集区下沿后反抽不过，旧支撑转为阻力，可提高空头确认度；"
            "zone_mid 是密集区内部强弱分界线，站上偏强，跌破偏弱。"
            "如果技术信号与 AI 综合判断同向，且消息面、订单流、密集区至少两项印证，可以 allow 或提高 multiplier。"
            "如果技术信号很强但消息面或订单流冲突，只能 reduce 或 block。"
            "如果出现央行意外、地缘冲突、监管黑天鹅、交易所风险、流动性恶化或数据质量差，必须 reduce 或 block。"
        )

    def _extract_decision_json(self, parsed: dict[str, Any]) -> dict[str, Any]:
        required = {"regime", "confidence", "multiplier"}
        if required.issubset(parsed):
            return parsed
        for key in ("decision", "ai_decision", "result", "output"):
            candidate = parsed.get(key)
            if isinstance(candidate, dict) and required.issubset(candidate):
                return candidate
        for candidate in parsed.values():
            if isinstance(candidate, dict) and required.issubset(candidate):
                return candidate
        return parsed

    def _normalize_decision_json(self, parsed: dict[str, Any]) -> dict[str, Any]:
        item = dict(parsed)
        item["regime"] = self._normalize_regime(item.get("regime"))
        item["direction"] = self._normalize_side(item.get("direction"))
        item["veto_action"] = self._normalize_veto(item.get("veto_action"))
        item["news_alignment"] = self._normalize_alignment(item.get("news_alignment"))
        item["orderflow_alignment"] = self._normalize_alignment(item.get("orderflow_alignment"))
        for key in ("entry_zone_estimate", "tp_estimate", "sl_estimate"):
            item[key] = self._normalize_optional_float(item.get(key))
        item["confidence"] = self._clip_float(item.get("confidence"), 0.0, 1.0, 0.35)
        item["multiplier"] = self._clip_float(item.get("multiplier"), 0.5, 1.5, 0.5)
        item["trend_confirmation_score"] = self._clip_float(item.get("trend_confirmation_score"), 0.0, 1.0, 0.35)
        item["range_risk_score"] = self._clip_float(item.get("range_risk_score"), 0.0, 1.0, 0.65)
        item["news_risk_score"] = self._clip_float(item.get("news_risk_score"), 0.0, 1.0, 0.65)
        item["orderflow_confirmation_score"] = self._clip_float(item.get("orderflow_confirmation_score"), 0.0, 1.0, 0.35)
        item["dense_zone_breakout_score"] = self._clip_float(item.get("dense_zone_breakout_score"), 0.0, 1.0, 0.35)
        if not isinstance(item.get("reason_codes"), list):
            item["reason_codes"] = [str(item.get("reason_codes"))] if item.get("reason_codes") else []
        if not isinstance(item.get("data_quality_warnings"), list):
            item["data_quality_warnings"] = [str(item.get("data_quality_warnings"))] if item.get("data_quality_warnings") else []
        return item

    def _normalize_regime(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"trend", "trending", "bull_trend", "bear_trend", "趋势", "趋势行情"}:
            return "trend"
        if text in {"range", "ranging", "sideways", "consolidation", "震荡", "震荡行情", "盘整"}:
            return "range"
        return "uncertain"

    def _normalize_side(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"long", "buy", "bull", "bullish", "up", "?", "??", "??"}:
            return "long"
        if text in {"short", "sell", "bear", "bearish", "down", "?", "??", "??"}:
            return "short"
        return "flat"

    def _normalize_alignment(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"aligned", "support", "supports", "confirm", "confirmed", "bullish", "bearish", "long", "short", "??", "??"}:
            return "aligned"
        if text in {"conflict", "conflicting", "against", "oppose", "opposite", "??", "??"}:
            return "conflict"
        if text in {"neutral", "flat", "mixed", "??", "??"}:
            return "neutral"
        return "unknown"

    def _normalize_veto(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"allow", "approve", "go", "??"}:
            return "allow"
        if text in {"reduce", "scale_down", "smaller", "??", "??"}:
            return "reduce"
        return "block" if text in {"block", "veto", "deny", "??", "??"} else "reduce"

    def _normalize_optional_float(self, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in {"", "none", "null", "no_signal", "n/a", "-", "?"}:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    def _clip_float(self, value: Any, low: float, high: float, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    async def propose_optimization(self, snapshot: dict[str, Any], days: int) -> dict[str, Any]:
        if not self.api_key:
            return self._fallback_optimization(snapshot, days, "missing_deepseek_api_key")
        prompt = {
            "task": "根据最近交易与决策数据，给出量化策略优化建议。只输出JSON。",
            "days": days,
            "allowed_parameter_paths": [
                "strategy.trend.ema_length",
                "strategy.trend.kc_length",
                "strategy.trend.kc_scalar",
                "strategy.trend.vma_length",
                "strategy.trend.atr_length",
                "strategy.trend.atr_stop_multiple",
                "strategy.trend.volume_multiple",
                "risk.min_confidence_to_trade",
                "risk.ai_full_size_confidence",
            ],
            "required_schema": {
                "summary": "中文摘要，说明是否值得调整",
                "logic_suggestions": ["中文策略逻辑建议"],
                "parameter_changes": [{"path": "参数路径", "new": "新值", "reason": "中文原因"}],
                "expected_effect": "预期改善",
                "risk_note": "风险提示",
            },
            "data": snapshot,
        }
        try:
            data = await self._chat_json(prompt, timeout_seconds=90, retries=2)
            parsed = json.loads(data["choices"][0]["message"]["content"])
            return parsed if isinstance(parsed, dict) else self._fallback_optimization(snapshot, days, "invalid_json_shape")
        except Exception as exc:  # noqa: BLE001
            logger.warning("DeepSeek 优化建议失败，使用保守复盘摘要: %r", exc)
            return self._fallback_optimization(snapshot, days, f"deepseek_error:{type(exc).__name__}")

    def _fallback_optimization(self, snapshot: dict[str, Any], days: int, reason: str) -> dict[str, Any]:
        return {
            "summary": f"最近{days}天样本不足或AI不可用，暂不建议自动调整参数。",
            "logic_suggestions": ["继续观察技术信号、AI判断、订单流和消息面是否持续一致。"],
            "parameter_changes": [],
            "expected_effect": "保持当前保守策略，避免在样本不足时过拟合。",
            "risk_note": reason,
        }

    def _build_payload(
        self,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        regime_pattern: RegimePattern | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_hint": {
                "regime": "trend|range|uncertain",
                "direction": "long|short|flat",
                "confidence": "0..1",
                "multiplier": "0.5..1.5",
                "veto_action": "allow|reduce|block",
                "action_suggestion": "open_long|open_short|reduce|close|hold|block",
                "trend_confirmation_score": "0..1, 趋势确认分，越高越支持本地趋势信号",
                "range_risk_score": "0..1, 震荡/假突破风险分，越高越危险",
                "news_risk_score": "0..1, 重大新闻/事件风险分，越高越危险",
                "orderflow_confirmation_score": "0..1, 订单流确认分，越高越支持本地技术方向",
                "dense_zone_breakout_score": "0..1, 密集区突破质量分，越高越支持趋势迁移",
            },
            "technical_signal": signal.model_dump(mode="json"),
            "orderflow": orderflow.model_dump(mode="json"),
            "dense_zone": dense_zone.model_dump(mode="json"),
            "pattern": pattern.model_dump(mode="json"),
            "regime_pattern": regime_pattern.model_dump(mode="json") if regime_pattern else None,
            "news": news.model_dump(mode="json"),
            "trading_knowledge": self.knowledge_base.build_context(
                ["market_regime", "trend_strategy", "orderflow_dense_zone", "macro_news", "risk_control"]
            ),
            "macro_entities": self.macro_entities.context_text(),
            "five_score_policy": {
                "position_tiers": {
                    "block": 0.0,
                    "weak": 0.25,
                    "normal": 0.5,
                    "strong": 0.75,
                    "full": 1.0,
                },
                "ai_role": "只确认、缩仓或否决本地趋势策略信号，不生成自动开仓方向。",
                "risk_scores_that_reduce_or_block": [
                    "range_risk_score 高代表震荡/假突破风险高，应缩仓或阻断。",
                    "news_risk_score 高代表重大新闻/事件风险高，应缩仓或阻断。",
                    "orderflow_confirmation_score 低代表订单流不支持本地技术方向，应缩仓或阻断。",
                    "dense_zone_breakout_score 低代表密集区突破质量差，应缩仓或阻断。",
                ],
            },
            "hard_rules": [
                "没有本地技术开仓信号时，不得自动开仓。",
                "AI信心超过65%但技术信号未触发时，只能生成候选交易计划并等待审批。",
                "AI只负责确认、缩仓或否决，不能发明交易方向。",
                "满仓只允许在技术面、AI趋势、消息面、订单流/密集区至少三项强同向且confidence>=0.75时出现。",
                "TP/SL只是估算汇报价格，不可用于扩大仓位。",
            ],
        }

    def _compact_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        compact = dict(payload)
        news = dict(compact.get("news") or {})
        items = []
        for item in (news.get("items") or [])[:16]:
            if isinstance(item, dict):
                items.append(
                    {
                        "title": str(item.get("title") or "")[:220],
                        "source": item.get("source"),
                        "published_at": item.get("published_at"),
                        "category": item.get("category"),
                        "summary": str(item.get("summary") or "")[:420],
                        "credibility": item.get("credibility"),
                    }
                )
        news["items"] = items
        compact["news"] = news
        return compact

    def _fallback_decision(
        self,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        reason: str,
        regime_pattern: RegimePattern | None = None,
    ) -> AiDecision:
        is_entry = signal.action in {SignalAction.LONG, SignalAction.SHORT}
        direction = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT if signal.action == SignalAction.SHORT else Side.FLAT
        data_ok = orderflow.data_quality >= 0.5 and not news.warnings
        aligned_orderflow = orderflow.alignment_hint == Alignment.ALIGNED
        strategy_allowed = (
            regime_pattern.strategy_allowed
            if regime_pattern is not None
            else str(signal.technical_evidence.get("strategy_allowed") or "trend")
        )
        regime_blocks_entry = is_entry and strategy_allowed != "trend"
        if regime_blocks_entry:
            veto = VetoAction.BLOCK
            confidence = 0.35
            regime = MarketRegime.RANGE if strategy_allowed == "range" else MarketRegime.UNCERTAIN
            action = "block"
            trend_confirmation_score = 0.2
            range_risk_score = 0.85 if strategy_allowed == "range" else 0.7
            news_risk_score = 0.65 if news.warnings else 0.45
            orderflow_confirmation_score = 0.35
            dense_zone_breakout_score = max(0.15, min(0.45, dense_zone.trend_score))
        elif is_entry and data_ok and aligned_orderflow and signal.signal_strength >= 0.7:
            veto = VetoAction.REDUCE
            confidence = min(0.65, max(0.5, signal.signal_strength))
            regime = MarketRegime.TREND
            action = "open_long" if direction == Side.LONG else "open_short"
            trend_confirmation_score = min(1.0, max(0.55, signal.signal_strength))
            range_risk_score = min(0.55, max(0.15, dense_zone.range_score))
            news_risk_score = 0.35
            orderflow_confirmation_score = min(0.8, max(0.6, orderflow.data_quality))
            dense_zone_breakout_score = min(0.75, max(0.45, dense_zone.trend_score or dense_zone.strength))
        else:
            veto = VetoAction.BLOCK if is_entry else VetoAction.ALLOW
            confidence = 0.35
            regime = MarketRegime.UNCERTAIN
            action = "block" if is_entry else "hold"
            trend_confirmation_score = 0.35 if is_entry else 0.0
            range_risk_score = 0.65
            news_risk_score = 0.65 if news.warnings else 0.5
            orderflow_confirmation_score = 0.35 if orderflow.alignment_hint == Alignment.ALIGNED else 0.2
            dense_zone_breakout_score = 0.35

        price = signal.current_price
        atr_value = float(signal.technical_evidence.get("atr") or 0.0)
        tp = price + atr_value * 2 if direction == Side.LONG else price - atr_value * 2 if direction == Side.SHORT else None
        atr_stop_multiple = float(signal.technical_evidence.get("atr_stop_multiple") or 3.0)
        sl = price - atr_value * atr_stop_multiple if direction == Side.LONG else price + atr_value * atr_stop_multiple if direction == Side.SHORT else None
        return AiDecision(
            symbol=signal.symbol,
            regime=regime,
            direction=direction,
            confidence=confidence,
            multiplier=0.5,
            news_alignment=news.crypto_sentiment,
            orderflow_alignment=orderflow.alignment_hint,
            dense_zone_position=dense_zone.current_position,
            pattern_type=pattern.pattern_type,
            trend_confirmation_score=trend_confirmation_score,
            range_risk_score=range_risk_score,
            news_risk_score=news_risk_score,
            orderflow_confirmation_score=orderflow_confirmation_score,
            dense_zone_breakout_score=dense_zone_breakout_score,
            entry_zone_estimate=price if is_entry else None,
            tp_estimate=tp,
            sl_estimate=sl,
            action_suggestion=action,
            veto_action=veto,
            brief_reason="DeepSeek不可用或数据不足，使用保守降级决策。",
            reason_codes=[
                reason,
                "fallback_conservative",
                *(regime_pattern.reason_codes[:4] if regime_pattern is not None else []),
                *(["local_regime_blocks_trend_strategy"] if regime_blocks_entry else []),
            ],
            data_quality_warnings=[
                *orderflow.warnings,
                *news.warnings,
                *(["local_regime_not_trend"] if regime_blocks_entry else []),
            ],
        )

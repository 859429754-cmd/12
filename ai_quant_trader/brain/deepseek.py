from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
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
    MarketLeaderContext,
    MarketRegime,
    NewsDigest,
    NewsDirection,
    PatternCandidate,
    RegimePattern,
    Side,
    SignalAction,
    StrategySignal,
    VetoAction,
)
from ai_quant_trader.data.macro_entities import MacroEntityStore
from ai_quant_trader.brain.credentials import DeepSeekCredentialRouter
from ai_quant_trader.storage.sqlite import SQLiteStore

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
        store: SQLiteStore | None = None,
    ):
        self.api_key = api_key or os.getenv("DEEPSEEK_API_KEY")
        self.backup_api_key = os.getenv("DEEPSEEK_BACKUP_API_KEY")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
        self.model = model or os.getenv("DEEPSEEK_DECISION_MODEL") or "deepseek-v4-pro"
        self.knowledge_base = knowledge_base or TradingKnowledgeBase()
        self.macro_entities = macro_entities or MacroEntityStore()
        self.store = store
        self.credential_router = DeepSeekCredentialRouter(store)

    def reload_from_env(self) -> None:
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        self.backup_api_key = os.getenv("DEEPSEEK_BACKUP_API_KEY")
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
        market_leader_context: MarketLeaderContext | None = None,
        call_type: str = "trading_cycle",
    ) -> AiDecision:
        payload = self._compact_payload(
            self._build_payload(signal, orderflow, dense_zone, pattern, news, regime_pattern, market_leader_context)
        )
        if not self._api_key_candidates():
            return self._fallback_decision(
                signal, orderflow, dense_zone, pattern, news, "missing_deepseek_api_key", regime_pattern, market_leader_context
            )

        try:
            data = await self._chat_json(payload, timeout_seconds=75, retries=3, call_type=call_type, symbol=signal.symbol)
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            parsed = self._extract_decision_json(parsed)
            parsed = self._normalize_decision_json(parsed)
            parsed.setdefault("symbol", signal.symbol)
            return AiDecision.model_validate(parsed)
        except (aiohttp.ClientError, requests.RequestException, KeyError, json.JSONDecodeError, ValidationError, TimeoutError, asyncio.TimeoutError) as exc:
            logger.warning("DeepSeek 分析失败，使用保守降级决策: %r", exc)
            return self._fallback_decision(
                signal,
                orderflow,
                dense_zone,
                pattern,
                news,
                f"deepseek_error:{type(exc).__name__}",
                regime_pattern,
                market_leader_context,
            )

    def local_fallback_decision(
        self,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        reason: str,
        regime_pattern: RegimePattern | None = None,
        market_leader_context: MarketLeaderContext | None = None,
    ) -> AiDecision:
        return self._fallback_decision(signal, orderflow, dense_zone, pattern, news, reason, regime_pattern, market_leader_context)

    async def _chat_json(
        self,
        payload: dict[str, Any],
        timeout_seconds: int,
        retries: int,
        call_type: str = "direct",
        symbol: str | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        request_fingerprint = self._request_fingerprint(payload)
        for credential_label, api_key in self._api_key_candidates():
            for attempt in range(retries):
                started = time.perf_counter()
                response: dict[str, Any] | None = None
                try:
                    response = await asyncio.to_thread(self._chat_json_sync, payload, timeout_seconds, api_key)
                    content = response["choices"][0]["message"]["content"]
                    json.loads(content)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    self.credential_router.record_success(credential_label)
                    self._record_usage_event(
                        payload=response,
                        status="success",
                        call_type=call_type,
                        symbol=symbol,
                        credential_label=credential_label,
                        latency_ms=latency_ms,
                        request_fingerprint=request_fingerprint,
                    )
                    return response
                except (aiohttp.ClientError, requests.RequestException, TimeoutError, asyncio.TimeoutError, KeyError, json.JSONDecodeError) as exc:
                    last_exc = exc
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    failure = self.credential_router.classify_exception(exc)
                    self.credential_router.record_failure(credential_label, failure)
                    self._record_usage_event(
                        payload=response,
                        status="failure",
                        call_type=call_type,
                        symbol=symbol,
                        credential_label=credential_label,
                        latency_ms=latency_ms,
                        request_fingerprint=request_fingerprint,
                        error_type=failure.error_type,
                        error_category=failure.category,
                        http_status=failure.http_status,
                    )
                    logger.warning(
                        "deepseek_call_failed",
                        extra={
                            "credential": credential_label,
                            "attempt": attempt + 1,
                            "retries": retries,
                            "error_type": type(exc).__name__,
                            "error_category": failure.category,
                            "http_status": failure.http_status,
                        },
                    )
                    if failure.category in {"quota_exhausted", "invalid_auth"}:
                        break
                    if attempt < retries - 1:
                        await asyncio.sleep(1.5 * (attempt + 1))
            if credential_label == "primary" and self.backup_api_key:
                logger.warning("deepseek_primary_failed_trying_backup", extra={"model": self.model})
        if last_exc:
            raise last_exc
        raise TimeoutError("deepseek_no_response")

    def _api_key_candidates(self) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        if self.api_key:
            candidates.append(("primary", self.api_key))
        if self.backup_api_key and self.backup_api_key != self.api_key:
            candidates.append(("backup", self.backup_api_key))
        return self.credential_router.candidates(candidates)

    def _chat_json_sync(self, payload: dict[str, Any], timeout_seconds: int, api_key: str | None = None) -> dict[str, Any]:
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key or self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": self._request_messages(payload),
                "response_format": {"type": "json_object"},
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
            },
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            data["_deepseek_transport"] = {"http_status": response.status_code}
        return data

    def _request_messages(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        user_payload = {
            "stable_contract": self._stable_request_contract(),
            "dynamic_context": payload,
        }
        return [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))},
        ]

    def _stable_request_contract(self) -> dict[str, Any]:
        return {
            "schema_version": "ai_quant_decision_v2",
            "ai_role": "confirm_reduce_or_block_only",
            "strategy_direction_source": "local_closed_1h_trend_strategy",
            "required_scores": [
                "trend_confirmation_score",
                "range_risk_score",
                "news_risk_score",
                "news_direction_alignment_score",
                "crypto_market_impact_score",
                "btc_leader_impact_score",
                "eth_btc_rotation_score",
                "symbol_news_impact_score",
                "pattern_confirmation_score",
                "orderflow_confirmation_score",
                "dense_zone_breakout_score",
            ],
            "position_tiers": {
                "block": 0.0,
                "weak": 0.25,
                "normal": 0.5,
                "strong": 0.75,
                "full": 1.0,
            },
            "score_semantics": {
                "news_alignment": "strategy-relative direction agreement: short+bearish or long+bullish is aligned; opposite is conflict.",
                "news_direction_alignment_score": "strategy-relative directional confirmation from news/background; high only when news direction clearly supports local strategy direction.",
                "news_risk_score": "event execution/volatility/liquidity risk, not absolute direction.",
                "crypto_market_impact_score": "broad crypto market impact from current and background news.",
                "btc_leader_alignment": "BTC leader context relative to local strategy direction.",
                "btc_leader_regime": "BTC/ETH structure: leader_uptrend, rotation_lag, leader_pullback, distribution_risk, leader_downtrend, or unknown.",
                "btc_leader_impact_score": "how much BTC context should affect ETH sizing.",
                "eth_btc_rotation_score": "ETH relative-strength or lagged catch-up quality versus BTC; supports ETH long only when local strategy already fired.",
                "symbol_news_impact_score": "direct impact on current trading symbol.",
                "pattern_confirmation_score": "chart pattern support for local strategy direction.",
                "orderflow_confirmation_score": "market participation, liquidity depth, impulse quality, and large-trade activity supporting breakout quality; not simple CVD direction.",
                "dense_zone_breakout_score": "dense-zone breakout or migration quality supporting local strategy direction.",
            },
            "hard_rules": [
                "no_local_entry_signal_no_auto_entry",
                "ai_cannot_invent_direction",
                "aligned_major_news_can_reduce_but_not_auto_block_without_execution_risk",
                "conflicting_news_or_orderflow_can_block",
                "orderflow_alignment_alone_cannot_create_full_size",
                "high_orderflow_confirmation_requires_pattern_dense_and_range_safety_for_full_size",
                "btc_leader_context_can_scale_or_cap_but_cannot_create_direction",
                "btc_pullback_with_eth_relative_strength_is_rotation_not_automatic_conflict",
                "pattern_confirmation_scales_position_but_cannot_create_direction",
            ],
        }

    def _record_usage_event(
        self,
        *,
        payload: dict[str, Any] | None,
        status: str,
        call_type: str,
        symbol: str | None,
        credential_label: str,
        latency_ms: int,
        request_fingerprint: str,
        error_type: str | None = None,
        error_category: str | None = None,
        http_status: int | None = None,
    ) -> None:
        if self.store is None:
            return
        usage = payload.get("usage") if isinstance(payload, dict) else {}
        usage = usage if isinstance(usage, dict) else {}
        transport = payload.get("_deepseek_transport") if isinstance(payload, dict) else {}
        transport = transport if isinstance(transport, dict) else {}
        event = {
            "symbol": symbol,
            "call_type": call_type,
            "model": self.model,
            "credential_label": credential_label,
            "status": status,
            "latency_ms": latency_ms,
            "http_status": http_status if http_status is not None else transport.get("http_status"),
            "error_type": error_type,
            "error_category": error_category,
            "request_fingerprint": request_fingerprint,
            "stable_prefix_hash": self._stable_prefix_hash(),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "prompt_cache_hit_tokens": int(usage.get("prompt_cache_hit_tokens") or 0),
            "prompt_cache_miss_tokens": int(usage.get("prompt_cache_miss_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "reasoning_tokens": int(((usage.get("completion_tokens_details") or {}) if isinstance(usage.get("completion_tokens_details"), dict) else {}).get("reasoning_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        self.store.insert("ai_call_usage_events", event, symbol)

    def _request_fingerprint(self, payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:24]

    def _stable_prefix_hash(self) -> str:
        prefix = json.dumps(
            {"system": self._system_prompt(), "stable_contract": self._stable_request_contract()},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:24]

    def _system_prompt(self) -> str:
        return (
            "你是客观、冷静、专业的加密货币量化交易员。"
            "你只能输出一个严格 JSON 对象，不允许输出 Markdown 或解释性正文。"
            "所有自然语言字段必须使用简体中文，尤其是 brief_reason、reason_codes、data_quality_warnings。"
            "JSON 必须直接包含 regime、direction、confidence、multiplier、veto_action、"
            "news_alignment、orderflow_alignment、dense_zone_position、entry_zone_estimate、"
            "tp_estimate、sl_estimate、action_suggestion、brief_reason、reason_codes、"
            "trend_confirmation_score、range_risk_score、news_risk_score、"
            "news_direction_alignment_score、orderflow_confirmation_score、dense_zone_breakout_score、"
            "subjective_position_tier、subjective_position_confidence。"
            "六个核心分数字段必须是 0 到 1 之间的小数："
            "trend_confirmation_score 越高代表趋势信号越可靠；"
            "range_risk_score 越高代表震荡/假突破风险越高；"
            "news_risk_score 越高代表事件执行、波动、滑点、流动性风险越高；"
            "news_direction_alignment_score 越高代表新闻/背景方向越明确支持本地策略方向；中性、未知或冲突时必须接近 0；"
            "orderflow_confirmation_score 越高代表市场参与度、流动性深度、冲击质量和大单活跃度越支持本地突破质量，不能简单等同 CVD 方向；"
            "dense_zone_breakout_score 越高代表密集区突破或迁移质量越好。"
            "枚举只能使用：regime=trend/range/uncertain；direction=long/short/flat；"
            "news_alignment 和 orderflow_alignment=aligned/conflict/neutral/unknown；"
            "veto_action=allow/reduce/block；action_suggestion=open_long/open_short/reduce/close/hold/block；"
            "subjective_position_tier=block/weak/normal/strong/full。"
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
            "如果技术信号与 AI 综合判断同向，且消息面、订单流活跃度、密集区、形态至少两项印证，可以 allow 或提高 multiplier。"
            "订单流同向只能作为质量确认之一，不能单独触发满仓；满仓必须同时具备形态确认、密集区突破质量、低震荡风险和足够置信度。"
            "重大新闻必须拆分为方向一致性和执行风险：做空信号遇到明确利空、做多信号遇到明确利多，应判定 news_alignment=aligned，"
            "不能仅因为它是重大新闻就标记为 conflict 或直接 block；同向重大新闻仍可提高 news_risk_score 并降仓，"
            "但只有流动性抽干、交易所/监管系统性风险、订单流明显反向或密集区突破质量极差等执行风险同时出现时，才允许 veto_action=block。"
            "如果技术信号很强但消息面或订单流冲突，只能 reduce 或 block。"
            "如果出现央行意外、地缘冲突、监管黑天鹅、交易所风险、流动性恶化或数据质量差，必须 reduce 或 block。"
            "subjective_position_tier 是你的主观五档仓位提案，不是最终下单仓位；"
            "它应体现你作为交易员对盈利扩张和亏损风险的综合判断。"
            "如果只是数据缺失、旧闻背景衰减或无法确认订单流，subjective_position_tier 不得高于 normal。"
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
        item["subjective_position_tier"] = self._normalize_position_tier(item.get("subjective_position_tier"))
        item["news_alignment"] = self._normalize_alignment(item.get("news_alignment"))
        item["orderflow_alignment"] = self._normalize_alignment(item.get("orderflow_alignment"))
        item["btc_leader_alignment"] = self._normalize_alignment(item.get("btc_leader_alignment"))
        item["btc_leader_regime"] = self._normalize_btc_leader_regime(item.get("btc_leader_regime"))
        for key in ("entry_zone_estimate", "tp_estimate", "sl_estimate"):
            item[key] = self._normalize_optional_float(item.get(key))
        item["confidence"] = self._clip_float(item.get("confidence"), 0.0, 1.0, 0.35)
        item["multiplier"] = self._clip_float(item.get("multiplier"), 0.5, 1.5, 0.5)
        item["subjective_position_confidence"] = self._clip_optional_float(
            item.get("subjective_position_confidence"),
            0.0,
            1.0,
        )
        item["trend_confirmation_score"] = self._clip_float(item.get("trend_confirmation_score"), 0.0, 1.0, 0.35)
        item["range_risk_score"] = self._clip_float(item.get("range_risk_score"), 0.0, 1.0, 0.65)
        item["news_risk_score"] = self._clip_float(item.get("news_risk_score"), 0.0, 1.0, 0.65)
        item["news_direction_alignment_score"] = self._clip_float(item.get("news_direction_alignment_score"), 0.0, 1.0, 0.0)
        if item["news_alignment"] != "aligned":
            item["news_direction_alignment_score"] = 0.0
        item["crypto_market_impact_score"] = self._clip_float(item.get("crypto_market_impact_score"), 0.0, 1.0, 0.0)
        item["btc_leader_impact_score"] = self._clip_float(item.get("btc_leader_impact_score"), 0.0, 1.0, 0.0)
        item["eth_btc_rotation_score"] = self._clip_float(item.get("eth_btc_rotation_score"), 0.0, 1.0, 0.0)
        item["symbol_news_impact_score"] = self._clip_float(item.get("symbol_news_impact_score"), 0.0, 1.0, 0.0)
        item["pattern_confirmation_score"] = self._clip_float(item.get("pattern_confirmation_score"), 0.0, 1.0, 0.5)
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
        if text in {"long", "buy", "bull", "bullish", "up", "多", "看多", "做多"}:
            return "long"
        if text in {"short", "sell", "bear", "bearish", "down", "空", "看空", "做空"}:
            return "short"
        return "flat"

    def _normalize_alignment(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"aligned", "support", "supports", "confirm", "confirmed", "bullish", "bearish", "long", "short", "同向", "一致"}:
            return "aligned"
        if text in {"conflict", "conflicting", "against", "oppose", "opposite", "冲突", "反向"}:
            return "conflict"
        if text in {"neutral", "flat", "mixed", "中性", "观望"}:
            return "neutral"
        return "unknown"

    def _normalize_veto(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"allow", "approve", "go", "允许"}:
            return "allow"
        if text in {"reduce", "scale_down", "smaller", "降仓", "减仓"}:
            return "reduce"
        return "block" if text in {"block", "veto", "deny", "阻断", "否决"} else "reduce"

    def _normalize_position_tier(self, value: Any) -> str | None:
        text = str(value or "").strip().lower()
        aliases = {
            "0": "block",
            "0%": "block",
            "block": "block",
            "blocked": "block",
            "阻断": "block",
            "25": "weak",
            "25%": "weak",
            "weak": "weak",
            "弱仓": "weak",
            "50": "normal",
            "50%": "normal",
            "normal": "normal",
            "standard": "normal",
            "标准仓": "normal",
            "75": "strong",
            "75%": "strong",
            "strong": "strong",
            "强仓": "strong",
            "100": "full",
            "100%": "full",
            "full": "full",
            "满仓": "full",
        }
        return aliases.get(text)

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

    def _clip_optional_float(self, value: Any, low: float, high: float) -> float | None:
        if value is None:
            return None
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return max(low, min(high, number))

    def _clip_float(self, value: Any, low: float, high: float, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    async def propose_optimization(self, snapshot: dict[str, Any], days: int) -> dict[str, Any]:
        if not self._api_key_candidates():
            return self._fallback_optimization(snapshot, days, "missing_deepseek_api_key")
        prompt = {
            "task": "根据最近交易与决策数据，给出量化策略优化建议。只输出JSON。",
            "days": days,
            "allowed_parameter_paths": [
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
            data = await self._chat_json(
                prompt,
                timeout_seconds=45,
                retries=1,
                call_type="optimization_proposal",
                symbol="ai_optimization",
            )
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
        market_leader_context: MarketLeaderContext | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_hint": {
                "regime": "trend|range|uncertain",
                "direction": "long|short|flat",
                "confidence": "0..1",
                "multiplier": "0.5..1.5",
                "veto_action": "allow|reduce|block",
                "subjective_position_tier": "block|weak|normal|strong|full, AI主观五档提案，最终仍由本地RiskManager裁剪",
                "subjective_position_confidence": "0..1, 对主观五档提案的置信度",
                "action_suggestion": "open_long|open_short|reduce|close|hold|block",
                "trend_confirmation_score": "0..1, 趋势确认分，越高越支持本地趋势信号",
                "range_risk_score": "0..1, 震荡/假突破风险分，越高越危险",
                "news_risk_score": "0..1, 重大新闻/事件执行风险分，越高越危险",
                "news_direction_alignment_score": "0..1, 消息面方向确认分；仅当新闻/背景方向明确支持本地策略方向时较高",
                "orderflow_confirmation_score": "0..1, 订单流确认分，越高代表市场参与度、流动性、冲击质量、大单活跃度越支持突破质量；不是简单 CVD 方向分",
                "dense_zone_breakout_score": "0..1, 密集区突破质量分，越高越支持趋势迁移",
            },
            "technical_signal": signal.model_dump(mode="json"),
            "orderflow": orderflow.model_dump(mode="json"),
            "dense_zone": dense_zone.model_dump(mode="json"),
            "pattern": pattern.model_dump(mode="json"),
            "regime_pattern": regime_pattern.model_dump(mode="json") if regime_pattern else None,
            "market_leader_context": market_leader_context.model_dump(mode="json") if market_leader_context else None,
            "market_background": news.market_background.model_dump(mode="json") if news.market_background else None,
            "news": news.model_dump(mode="json"),
            "news_direction_hint": self._news_direction_hint(news),
            "news_strategy_alignment_hint": self._news_alignment_for_signal(news, signal),
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
                    "news_alignment 是方向一致性：做空+利空、做多+利多为 aligned；做空+利多、做多+利空为 conflict。",
                    "news_direction_alignment_score 是方向确认加分，只能在 news_alignment=aligned 且新闻/背景影响明确时提高；中性或未知新闻不得加分。",
                    "range_risk_score 高代表震荡/假突破风险高，应缩仓或阻断。",
                    "news_risk_score 高代表重大新闻/事件执行风险高；若 news_alignment=aligned，优先缩仓，只有流动性/监管/交易所/订单流/密集区风险同时恶化时才阻断。",
                    "orderflow_confirmation_score 低代表市场参与度、流动性或冲击质量不足，应缩仓或阻断；订单流方向同向本身不得单独满仓。",
                    "dense_zone_breakout_score 低代表密集区突破质量差，应缩仓或阻断。",
                ],
            },
            "news_context_policy": {
                "market_background": "Long-lived factual context built from decayed high-impact events.",
                "realtime_news": "Short window news must be judged against market_background, not in isolation.",
                "direction_rule": "Short plus bearish news is aligned; long plus bullish news is aligned. Opposite combinations are conflict.",
                "btc_leader_rule": "ETH sizing must account for BTC leader context: BTC aligned supports, BTC conflict caps/reduces, BTC unknown is neutral.",
                "btc_rotation_rule": "If BTC is only pulling back or consolidating while ETH has strong relative strength, classify as rotation_lag/leader_pullback instead of automatic conflict.",
                "market_vs_event_rule": "Separate absolute news direction, broad crypto impact, symbol impact, and execution/volatility risk.",
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
        background = dict(compact.get("market_background") or {})
        for key, limit in (("active_events", 10), ("realtime_events", 6)):
            events = []
            for event in (background.get(key) or [])[:limit]:
                if isinstance(event, dict):
                    events.append(
                        {
                            "title": str(event.get("title") or "")[:220],
                            "source": event.get("source"),
                            "published_at": event.get("published_at"),
                            "direction": event.get("direction"),
                            "severity": event.get("severity"),
                            "risk_score": event.get("risk_score"),
                            "confidence": event.get("confidence"),
                            "summary": str(event.get("summary") or "")[:320],
                        }
                    )
            if background:
                background[key] = events
        if background:
            compact["market_background"] = background
        news = dict(compact.get("news") or {})
        news.pop("market_background", None)
        news.pop("active_news_events", None)
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

    def _news_direction_hint(self, news: NewsDigest) -> str:
        if news.news_direction == NewsDirection.BULLISH:
            return "bullish"
        if news.news_direction == NewsDirection.BEARISH:
            return "bearish"
        if news.news_direction == NewsDirection.NEUTRAL:
            return "neutral"
        return "unknown"

    def _normalize_btc_leader_regime(self, value: Any) -> str:
        text = str(value or "").strip().lower()
        allowed = {
            "leader_uptrend",
            "rotation_lag",
            "leader_pullback",
            "distribution_risk",
            "leader_downtrend",
            "unknown",
        }
        return text if text in allowed else "unknown"

    def _news_alignment_for_signal(self, news: NewsDigest, signal: StrategySignal) -> Alignment:
        if news.news_direction == NewsDirection.NEUTRAL:
            return Alignment.NEUTRAL
        if news.news_direction == NewsDirection.UNKNOWN:
            return Alignment.UNKNOWN
        if signal.action == SignalAction.LONG:
            return Alignment.ALIGNED if news.news_direction == NewsDirection.BULLISH else Alignment.CONFLICT
        if signal.action == SignalAction.SHORT:
            return Alignment.ALIGNED if news.news_direction == NewsDirection.BEARISH else Alignment.CONFLICT
        return Alignment.UNKNOWN

    def _news_direction_alignment_score(self, news: NewsDigest, signal: StrategySignal) -> float:
        if self._news_alignment_for_signal(news, signal) != Alignment.ALIGNED:
            return 0.0
        impact = max(
            self._background_impact_score(news),
            self._symbol_news_impact_score(news, signal.symbol),
        )
        if impact <= 0 and news.news_direction not in {NewsDirection.UNKNOWN, NewsDirection.NEUTRAL}:
            impact = 0.25
        return round(min(1.0, 0.45 + impact * 0.45), 4)

    def _background_impact_score(self, news: NewsDigest) -> float:
        events = list(news.active_news_events)
        if news.market_background is not None:
            events.extend(news.market_background.active_events)
            events.extend(news.market_background.realtime_events)
        if not events:
            return 0.0
        score = max((event.risk_score * max(event.confidence, 0.1) for event in events), default=0.0)
        crypto_events = [
            event for event in events
            if any(str(scope).lower() in {"crypto", "btc", "eth", "risk_assets"} for scope in event.asset_scope)
        ]
        if crypto_events:
            score = max(score, max(event.risk_score for event in crypto_events))
        return round(max(0.0, min(1.0, score)), 4)

    def _symbol_news_impact_score(self, news: NewsDigest, symbol: str) -> float:
        base = symbol.split("/", 1)[0].upper()
        events = list(news.active_news_events)
        if news.market_background is not None:
            events.extend(news.market_background.active_events)
            events.extend(news.market_background.realtime_events)
        if not events:
            return 0.0
        direct = [
            event for event in events
            if any(str(scope).upper() in {base, symbol.upper()} for scope in event.asset_scope)
        ]
        if not direct:
            return 0.0
        score = max(event.risk_score * max(event.confidence, 0.1) for event in direct)
        return round(max(0.0, min(1.0, score)), 4)

    def _fallback_decision(
        self,
        signal: StrategySignal,
        orderflow: AggregatedOrderflow,
        dense_zone: DenseZone,
        pattern: PatternCandidate,
        news: NewsDigest,
        reason: str,
        regime_pattern: RegimePattern | None = None,
        market_leader_context: MarketLeaderContext | None = None,
    ) -> AiDecision:
        is_entry = signal.action in {SignalAction.LONG, SignalAction.SHORT}
        direction = Side.LONG if signal.action == SignalAction.LONG else Side.SHORT if signal.action == SignalAction.SHORT else Side.FLAT
        data_ok = orderflow.data_quality >= 0.5 and not news.warnings
        aligned_orderflow = orderflow.alignment_hint == Alignment.ALIGNED
        ai_unavailable = (
            reason == "missing_deepseek_api_key"
            or reason.startswith("deepseek_error:")
            or reason.startswith("deepseek_budget_blocked:")
        )
        strategy_allowed = (
            regime_pattern.strategy_allowed
            if regime_pattern is not None
            else str(signal.technical_evidence.get("strategy_allowed") or "trend")
        )
        regime_blocks_entry = is_entry and strategy_allowed != "trend"
        if is_entry and ai_unavailable:
            veto = VetoAction.BLOCK
            confidence = 0.0
            regime = MarketRegime.UNCERTAIN
            action = "block"
            trend_confirmation_score = 0.0
            range_risk_score = 0.75
            news_risk_score = 0.75 if news.warnings else 0.55
            pattern_confirmation_score = 0.2
            orderflow_confirmation_score = 0.0
            dense_zone_breakout_score = 0.0
        elif regime_blocks_entry:
            veto = VetoAction.BLOCK
            confidence = 0.35
            regime = MarketRegime.RANGE if strategy_allowed == "range" else MarketRegime.UNCERTAIN
            action = "block"
            trend_confirmation_score = 0.2
            range_risk_score = 0.85 if strategy_allowed == "range" else 0.7
            news_risk_score = 0.65 if news.warnings else 0.45
            pattern_confirmation_score = max(0.2, min(0.45, pattern.confidence))
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
            pattern_confirmation_score = max(0.45, min(0.75, pattern.confidence))
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
            pattern_confirmation_score = max(0.25, min(0.55, pattern.confidence))
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
            news_alignment=self._news_alignment_for_signal(news, signal),
            orderflow_alignment=orderflow.alignment_hint,
            btc_leader_alignment=market_leader_context.strategy_alignment_hint if market_leader_context else Alignment.UNKNOWN,
            btc_leader_regime=market_leader_context.leader_regime if market_leader_context else "unknown",
            dense_zone_position=dense_zone.current_position,
            pattern_type=pattern.pattern_type,
            trend_confirmation_score=trend_confirmation_score,
            range_risk_score=range_risk_score,
            news_risk_score=news_risk_score,
            news_direction_alignment_score=self._news_direction_alignment_score(news, signal),
            crypto_market_impact_score=self._background_impact_score(news),
            btc_leader_impact_score=market_leader_context.impact_score if market_leader_context else 0.0,
            eth_btc_rotation_score=market_leader_context.eth_btc_rotation_score if market_leader_context else 0.0,
            symbol_news_impact_score=self._symbol_news_impact_score(news, signal.symbol),
            pattern_confirmation_score=pattern_confirmation_score,
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

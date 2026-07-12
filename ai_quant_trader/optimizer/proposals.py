from __future__ import annotations

from typing import Any

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.brain.budget import DeepSeekBudgetGuard
from ai_quant_trader.core.control import PARAM_RULES, RuntimeControlManager
from ai_quant_trader.core.models import AppConfig
from ai_quant_trader.storage.sqlite import SQLiteStore


class StrategyOptimizer:
    def __init__(self, store: SQLiteStore, brain: DeepSeekBrain, control: RuntimeControlManager):
        self.store = store
        self.brain = brain
        self.control = control

    async def create_ai_proposal(self, days: int, operator_id: str) -> tuple[int, dict[str, Any]]:
        days = 15 if days <= 20 else 30
        snapshot = self._build_snapshot(days)
        config = self.control.read_config()
        ai_config = AppConfig.model_validate(config).ai
        budget = DeepSeekBudgetGuard.from_config(self.store, ai_config)
        source = "deepseek"
        if not ai_config.enabled:
            budget.record_skipped(
                symbol="ai_optimization",
                call_type="optimization_proposal",
                reason="deepseek_disabled_pure_strategy",
            )
            suggestion = self._fallback_suggestion(days, "deepseek_disabled_pure_strategy")
            source = "local_fallback"
        else:
            reservation = budget.reserve(symbol="ai_optimization", call_type="optimization_proposal")
            if reservation.allowed:
                suggestion = await self.brain.propose_optimization(snapshot, days)
                budget.record_success(reservation.row_id, detail="optimization_proposal_created")
            else:
                suggestion = self._fallback_suggestion(days, f"deepseek_budget_blocked:{reservation.reason}")
                source = "local_fallback"
        changes: dict[str, dict[str, Any]] = {}
        for item in suggestion.get("parameter_changes", []) or []:
            path = item.get("path")
            if not isinstance(path, str):
                continue
            if path not in PARAM_RULES:
                continue
            try:
                new_value = self.control.cast_and_validate(path, item.get("new"))
            except Exception:
                continue
            changes[path] = {
                "old": self.control.get_config_value(config, path),
                "new": new_value,
                "reason": item.get("reason", ""),
            }
        proposal = {
            "type": "ai_optimization",
            "status": "pending",
            "operator_id": operator_id,
            "days": days,
            "summary": suggestion.get("summary", f"最近{days}天AI复盘建议"),
            "logic_suggestions": suggestion.get("logic_suggestions", []),
            "changes": changes,
            "expected_effect": suggestion.get("expected_effect", ""),
            "risk_note": suggestion.get("risk_note", ""),
            "source": source,
        }
        proposal_id = self.store.insert("optimization_proposals", proposal, symbol="ai_optimization")
        return proposal_id, proposal

    def _build_snapshot(self, days: int) -> dict[str, Any]:
        orders = self.store.fetch_payloads("orders", limit=200)
        ai_decisions = self.store.fetch_payloads("ai_decisions", limit=300)
        reports = self.store.fetch_payloads("hourly_reports", limit=days * 24)
        return {
            "days": days,
            "order_count": len(orders),
            "recent_orders": [self._compact_order(row.get("payload") or {}) for row in orders[:30]],
            "recent_ai_decisions": [self._compact_ai_decision(row.get("payload") or {}) for row in ai_decisions[:30]],
            "recent_report_count": len(reports),
        }

    def _compact_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        return {
            "symbol": payload.get("symbol"),
            "side": payload.get("side"),
            "amount": payload.get("amount"),
            "price": payload.get("price"),
            "status": payload.get("status"),
            "dry_run": payload.get("dry_run"),
            "created_at": payload.get("created_at"),
            "reason": payload.get("reason") or metadata.get("reason"),
            "account_slot": payload.get("account_slot") or metadata.get("account_slot"),
            "position_tier": metadata.get("position_tier"),
            "position_scale": metadata.get("position_scale"),
        }

    def _compact_ai_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "symbol",
            "regime",
            "direction",
            "confidence",
            "action_suggestion",
            "position_tier",
            "position_scale",
            "trend_confirmation_score",
            "range_risk_score",
            "news_risk_score",
            "news_alignment",
            "news_direction_alignment_score",
            "orderflow_confirmation_score",
            "dense_zone_breakout_score",
            "pattern_confirmation_score",
            "brief_reason",
            "reason_codes",
            "created_at",
        )
        compact = {key: payload.get(key) for key in keys if key in payload}
        if isinstance(compact.get("reason_codes"), list):
            compact["reason_codes"] = compact["reason_codes"][:8]
        if isinstance(compact.get("brief_reason"), str):
            compact["brief_reason"] = compact["brief_reason"][:240]
        return compact

    def _fallback_suggestion(self, days: int, reason: str) -> dict[str, Any]:
        return {
            "summary": f"最近{days}天优化提案被成本/预算闸拦截，暂不建议自动调整参数。",
            "logic_suggestions": ["保持当前实盘参数，等预算窗口恢复后再做AI复盘。"],
            "parameter_changes": [],
            "expected_effect": "避免非交易必要调用挤占DeepSeek预算。",
            "risk_note": reason,
        }

    def format_proposal(self, proposal_id: int, proposal: dict[str, Any]) -> str:
        lines = [
            f"### 策略优化提案 #{proposal_id}",
            f"> {proposal.get('summary', '无摘要')}",
        ]
        changes = proposal.get("changes") or {}
        if changes:
            lines.append("**参数建议**")
            for path, change in changes.items():
                label = self.control.param_label(path)
                reason = change.get("reason") or "AI复盘建议"
                lines.append(f"- {label}: {change.get('old')} -> {change.get('new')}；{reason}")
        else:
            lines.append("**参数建议**：暂不调整")
        logic = proposal.get("logic_suggestions") or []
        if logic:
            lines.append("**逻辑建议**")
            for item in logic[:3]:
                lines.append(f"- {item}")
        if proposal.get("risk_note"):
            lines.append(f"**风险提示**：{proposal['risk_note']}")
        lines.append("")
        lines.append(f"回复 `同意修改 {proposal_id}` 应用参数；回复 `拒绝 {proposal_id}` 放弃。")
        return "\n".join(lines)

from __future__ import annotations

from typing import Any

from ai_quant_trader.brain.deepseek import DeepSeekBrain
from ai_quant_trader.core.control import PARAM_RULES, RuntimeControlManager
from ai_quant_trader.storage.sqlite import SQLiteStore


class StrategyOptimizer:
    def __init__(self, store: SQLiteStore, brain: DeepSeekBrain, control: RuntimeControlManager):
        self.store = store
        self.brain = brain
        self.control = control

    async def create_ai_proposal(self, days: int, operator_id: str) -> tuple[int, dict[str, Any]]:
        days = 15 if days <= 20 else 30
        snapshot = self._build_snapshot(days)
        suggestion = await self.brain.propose_optimization(snapshot, days)
        config = self.control.read_config()
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
            "source": "deepseek",
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
            "recent_orders": [row["payload"] for row in orders[:50]],
            "recent_ai_decisions": [row["payload"] for row in ai_decisions[:80]],
            "recent_report_count": len(reports),
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

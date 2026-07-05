from __future__ import annotations

import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from ai_quant_trader.core.models import MAX_CONFIGURABLE_LEVERAGE, TrendStrategyConfig
from ai_quant_trader.core.state import RuntimeState
from ai_quant_trader.storage.sqlite import SQLiteStore


SYMBOL_ALIASES = {
    "btc": "BTC/USDT:USDT",
    "比特币": "BTC/USDT:USDT",
    "大饼": "BTC/USDT:USDT",
    "eth": "ETH/USDT:USDT",
    "以太": "ETH/USDT:USDT",
    "以太坊": "ETH/USDT:USDT",
    "sol": "SOL/USDT:USDT",
    "索拉纳": "SOL/USDT:USDT",
}

PARAM_RULES: dict[str, dict[str, Any]] = {
    "strategy.trend.kc_length": {"type": int, "min": 5, "max": 100, "label": "肯特纳通道周期"},
    "strategy.trend.kc_scalar": {"type": float, "min": 0.5, "max": 8.0, "label": "肯特纳通道ATR倍数"},
    "strategy.trend.vma_length": {"type": int, "min": 5, "max": 100, "label": "成交量均线周期"},
    "strategy.trend.atr_length": {"type": int, "min": 5, "max": 100, "label": "ATR周期"},
    "strategy.trend.atr_stop_multiple": {"type": float, "min": 0.2, "max": 20.0, "label": "ATR止损倍数"},
    "strategy.trend.volume_multiple": {"type": float, "min": 0.5, "max": 8.0, "label": "放量倍数"},
    "risk.min_confidence_to_trade": {"type": float, "min": 0.1, "max": 0.95, "label": "最低AI置信度"},
    "risk.ai_full_size_confidence": {"type": float, "min": 0.2, "max": 0.98, "label": "满仓AI置信度阈值"},
    "risk.small_position_notional_usdt": {"type": float, "min": 1.0, "max": 200.0, "label": "小仓单次名义金额"},
    "risk.max_total_leverage": {"type": float, "min": 0.5, "max": MAX_CONFIGURABLE_LEVERAGE, "label": "全局杠杆硬上限"},
}

PARAM_ALIASES: dict[str, str] = {
    "kc周期": "strategy.trend.kc_length",
    "肯特纳周期": "strategy.trend.kc_length",
    "kc长度": "strategy.trend.kc_length",
    "kc倍数": "strategy.trend.kc_scalar",
    "kc通道倍数": "strategy.trend.kc_scalar",
    "肯特纳倍数": "strategy.trend.kc_scalar",
    "atr倍数": "strategy.trend.kc_scalar",
    "vma": "strategy.trend.vma_length",
    "vma周期": "strategy.trend.vma_length",
    "均量线": "strategy.trend.vma_length",
    "均量周期": "strategy.trend.vma_length",
    "atr周期": "strategy.trend.atr_length",
    "atr长度": "strategy.trend.atr_length",
    "atr止损": "strategy.trend.atr_stop_multiple",
    "atr止损倍数": "strategy.trend.atr_stop_multiple",
    "止损atr": "strategy.trend.atr_stop_multiple",
    "atr_stop_multiple": "strategy.trend.atr_stop_multiple",
    "量能倍数": "strategy.trend.volume_multiple",
    "放量倍数": "strategy.trend.volume_multiple",
    "成交量倍数": "strategy.trend.volume_multiple",
    "最低置信度": "risk.min_confidence_to_trade",
    "开仓置信度": "risk.min_confidence_to_trade",
    "满仓置信度": "risk.ai_full_size_confidence",
    "小仓金额": "risk.small_position_notional_usdt",
    "杠杆上限": "risk.max_total_leverage",
    "最大杠杆": "risk.max_total_leverage",
    "全局杠杆": "risk.max_total_leverage",
}

SYMBOL_PARAM_TO_GLOBAL = {
    "kc_length": "strategy.trend.kc_length",
    "kc_scalar": "strategy.trend.kc_scalar",
    "vma_length": "strategy.trend.vma_length",
    "atr_length": "strategy.trend.atr_length",
    "atr_stop_multiple": "strategy.trend.atr_stop_multiple",
    "volume_multiple": "strategy.trend.volume_multiple",
}


class RuntimeControlManager:
    """运行期控制器：负责开关、参数提案和配置热更新。"""

    runtime_control_symbol = "runtime_control"
    runtime_control_keys = frozenset({"opening_paused", "enabled_symbols", "report_symbols", "major_news_only"})

    def __init__(self, store: SQLiteStore, config_path: str = "config/config.yaml"):
        self.store = store
        self.config_path = Path(config_path)

    def read_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        return yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}

    def write_config(self, config: dict[str, Any]) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def load_state(self, configured_symbols: list[str]) -> RuntimeState:
        latest = self._latest_runtime_control_row()
        state = RuntimeState(report_symbols=set(configured_symbols))
        if latest:
            payload = latest["payload"]
            state.opening_paused = bool(payload.get("opening_paused", True))
            state.enabled_symbols = set(payload.get("enabled_symbols", []))
            state.report_symbols = set(payload.get("report_symbols", configured_symbols))
            state.major_news_only = bool(payload.get("major_news_only", False))
        state.enabled_symbols.intersection_update(configured_symbols)
        state.report_symbols.intersection_update(configured_symbols)
        return state

    def _latest_runtime_control_row(self) -> dict[str, Any] | None:
        latest = self.store.fetch_latest("runtime_state", self.runtime_control_symbol)
        if latest:
            return latest
        for row in self.store.fetch_payloads("runtime_state", limit=500):
            payload = row.get("payload") or {}
            if isinstance(payload, dict) and self.runtime_control_keys.intersection(payload):
                return row
        return None

    def save_state(self, state: RuntimeState, operator_id: str, reason: str) -> None:
        self.store.insert(
            "runtime_state",
            {
                "opening_paused": state.opening_paused,
                "enabled_symbols": sorted(state.enabled_symbols),
                "report_symbols": sorted(state.report_symbols),
                "major_news_only": state.major_news_only,
                "operator_id": operator_id,
                "reason": reason,
            },
            symbol=self.runtime_control_symbol,
        )

    def pause(self, state: RuntimeState, symbols: list[str], operator_id: str) -> str:
        if not symbols:
            state.opening_paused = True
            state.enabled_symbols.clear()
            target = "全部标的"
        else:
            for symbol in symbols:
                state.pause_symbol(symbol)
            if not state.enabled_symbols:
                state.opening_paused = True
            target = "、".join(self.short_symbol(s) for s in symbols)
        self.save_state(state, operator_id, "pause_opening")
        return f"已暂停 {target} 的新开仓。已有持仓的平仓风控仍可执行。"

    def resume_simulation(self, state: RuntimeState, symbols: list[str], operator_id: str) -> str:
        state.opening_paused = False
        for symbol in symbols:
            state.authorize_symbol(symbol)
        self.save_state(state, operator_id, "resume_simulation")
        target = "、".join(self.short_symbol(s) for s in symbols)
        return f"已恢复 {target} 的模拟授权。当前仍按配置里的 dry_run 状态执行。"

    def authorize_opening(self, state: RuntimeState, symbols: list[str], operator_id: str, dry_run: bool) -> str:
        state.opening_paused = False
        for symbol in symbols:
            state.authorize_symbol(symbol)
        self.save_state(state, operator_id, "authorize_opening")
        target = "、".join(self.short_symbol(s) for s in symbols)
        mode = "模拟" if dry_run else "实盘"
        return f"已允许 {target} 在{mode}模式下开仓。真实开仓仍必须通过技术信号、AI判断、消息面、订单流和硬风控。"

    def disable_symbol_report(self, state: RuntimeState, symbols: list[str], operator_id: str) -> str:
        for symbol in symbols:
            state.disable_report(symbol)
        self.save_state(state, operator_id, "disable_symbol_report")
        target = "、".join(self.short_symbol(s) for s in symbols)
        return f"已关闭 {target} 的分析报告和新开仓授权；后续小时简报不再展示这些标的。"

    def enable_symbol_report(self, state: RuntimeState, symbols: list[str], operator_id: str) -> str:
        for symbol in symbols:
            state.enable_report(symbol)
        self.save_state(state, operator_id, "enable_symbol_report")
        target = "、".join(self.short_symbol(s) for s in symbols)
        return f"已开启 {target} 的分析报告。注意：这只恢复分析展示，不等于开启实盘交易。"

    def set_major_news_only(self, state: RuntimeState, enabled: bool, operator_id: str) -> str:
        state.major_news_only = enabled
        self.save_state(state, operator_id, "set_major_news_only")
        return "已切换为只推送重大消息模式。" if enabled else "已恢复普通消息推送模式。"

    def status_text(self, state: RuntimeState, dry_run: bool) -> str:
        mode = "模拟运行" if dry_run else "实盘运行"
        opening = "暂停开仓" if state.opening_paused else "允许已授权标的开仓"
        enabled = "、".join(self.short_symbol(s) for s in sorted(state.enabled_symbols)) or "无"
        reports = "、".join(self.short_symbol(s) for s in sorted(state.report_symbols)) or "无"
        news_mode = "只推送重大消息" if state.major_news_only else "普通消息模式"
        return "\n".join(
            [
                "## 当前运行状态",
                f"- 模式：{mode}",
                f"- 开仓总开关：{opening}",
                f"- 已授权标的：{enabled}",
                f"- 报告标的：{reports}",
                f"- 消息模式：{news_mode}",
                "- 安全规则：配置的总杠杆硬上限、AI可否决、本地技术信号必须确认、同方向已有持仓不重复加仓。",
            ]
        )

    def params_text(self, symbol: str | None = None) -> str:
        config = self.read_config()
        if symbol:
            params = self.effective_symbol_params(config, symbol)
            title = f"## {self.short_symbol(symbol)} 当前策略参数"
        else:
            params = self.effective_symbol_params(config, "")
            title = "## 全局默认策略参数"
        risk = config.get("risk", {})
        return "\n".join(
            [
                title,
                f"- 肯特纳通道：周期 {params['kc_length']}，ATR倍数 {params['kc_scalar']}",
                f"- 成交量均线周期：{params['vma_length']}",
                f"- ATR周期：{params['atr_length']}",
                f"- ATR止损倍数：{params['atr_stop_multiple']}",
                f"- 放量倍数：{params['volume_multiple']}",
                f"- 最低AI置信度：{risk.get('min_confidence_to_trade', 0.55)}",
                f"- 满仓AI置信度阈值：{risk.get('ai_full_size_confidence', 0.75)}",
                f"- 小仓测试名义金额：{risk.get('small_position_notional_usdt', 20.0)} USDT",
            ]
        )

    def news_sources_text(self, config: dict[str, Any]) -> str:
        news = config.get("news", {})
        rss = news.get("rss_sources", []) or []
        scrape = news.get("scrape_sources", []) or []
        lines = ["## 消息面采集渠道", "系统每小时采集公开免费来源，重点关注加密、宏观、央行、政治、地缘和美元相关信息。"]
        for url in rss:
            lines.append(f"- RSS：{url}")
        for url in scrape:
            lines.append(f"- 网页：{url}")
        return "\n".join(lines)

    def workflow_text(self) -> str:
        return "\n".join(
            [
                "## 系统工作流程",
                "1. 每小时读取已开启报告标的的 1 小时K线。",
                "2. 采集 Binance、OKX、Bybit 公开订单流并聚合。",
                "3. 采集公开消息面，筛选宏观、政治、地缘和加密重点新闻。",
                "4. 本地趋势策略先给候选信号，AI再判断趋势/震荡、消息面和订单流是否印证。",
                "5. 风控最终裁剪仓位：冷启动锁、逐标的授权、AI否决、同方向不加仓和配置的杠杆上限都不能绕过。",
            ]
        )

    def help_text(self) -> str:
        rows = [
            ("状态", "查询运行状态", "查看模式、开仓锁、授权标的、消息模式"),
            ("账户", "查询账户余额", "读取 Gate 账户 USDT 余额"),
            ("参数", "当前策略参数 / BTC参数", "查看全局或单标的策略参数"),
            ("流程", "消息渠道 / 系统流程", "查看新闻来源和系统运行流程"),
            ("报告", "关闭BTC标的 / 开启SOL标的", "控制 ETH/BTC/SOL 是否进入分析报告"),
            ("消息", "只推送重大消息 / 恢复普通消息推送", "控制新闻噪音"),
            ("开仓锁", "暂停开仓 / 暂停ETH", "暂停全部或单标的新开仓"),
            ("授权", "允许ETH实盘开仓", "逐标的解除开仓锁"),
            ("降风险", "一键全平并停止", "立即暂停开仓并尝试平掉 Gate 持仓"),
            ("降风险", "手动平仓ETH", "立即尝试平掉指定标的所有持仓"),
            ("模式", "恢复正常实盘", "生成实盘切换提案，需要审批"),
            ("小仓", "开启小仓模式测试20U", "生成小仓实盘模式提案，需要审批"),
            ("小仓", "开仓小仓测试ETH做多", "按交易所最低数量生成手动小仓开仓提案"),
            ("检查", "最小仓位测试检查", "只读检查当前开启标的的最低下单数量"),
            ("参数热更", "把BTC的KC倍数调到2.5", "支持KC、VMA、ATR、放量倍数等"),
            ("复盘", "优化策略15天 / 优化策略30天", "AI读取交易记录生成优化提案"),
            ("审批", "待审批 / 同意修改 12 / 拒绝 12", "查看、批准或拒绝提案"),
            ("API", "查询API状态 / 更新DeepSeek API为sk-xxx", "管理员可热更新API，回复会脱敏"),
        ]
        lines = ["## 控制台指令列表", "| 类别 | 示例 | 作用 |", "|---|---|---|"]
        lines.extend(f"| {kind} | {example} | {desc} |" for kind, example, desc in rows)
        return "\n".join(lines)

    def latest_pending_text(self) -> str:
        rows = self.store.fetch_payloads("optimization_proposals", limit=20)
        pending = [row for row in rows if row["payload"].get("status") == "pending"]
        if not pending:
            return "当前没有待审批提案。"
        lines = ["## 待审批提案"]
        for row in pending[:8]:
            payload = row["payload"]
            lines.append(f"- #{row['id']}：{payload.get('type', 'proposal')}：{payload.get('summary', '无摘要')}")
        return "\n".join(lines)

    def trend_config_for_symbol(self, config: dict[str, Any], symbol: str) -> TrendStrategyConfig:
        return TrendStrategyConfig.model_validate(self.effective_symbol_params(config, symbol))

    def effective_symbol_params(self, config: dict[str, Any], symbol: str) -> dict[str, Any]:
        base = deepcopy(config.get("strategy", {}).get("trend", {}))
        base.setdefault("enabled", True)
        base.setdefault("profile_name", "default")
        base.setdefault("kc_length", 20)
        base.setdefault("kc_scalar", 2.8)
        base.setdefault("vma_length", 20)
        base.setdefault("atr_length", 14)
        base.setdefault("atr_stop_multiple", 1.5)
        base.setdefault("volume_multiple", 2.5)
        base.setdefault("use_volume_filter", True)
        base.setdefault("momentum_filter", "kdj")
        base.setdefault("kdj_length", 9)
        base.setdefault("kdj_k_smooth", 3)
        base.setdefault("kdj_d_smooth", 3)
        base.setdefault("position_fraction", 1.0)
        base.setdefault("variant", "with_volume")
        if symbol:
            base.update(config.get("symbol_params", {}).get(symbol, {}) or {})
        return base

    def create_param_proposal(self, text: str, operator_id: str, configured_symbols: list[str]) -> int:
        config = self.read_config()
        symbols = self.resolve_symbols(text, configured_symbols)
        path, new_value = self.parse_param_change(text)
        changes: dict[str, dict[str, Any]] = {}
        if symbols:
            short_key = path.split(".")[-1]
            if short_key not in SYMBOL_PARAM_TO_GLOBAL:
                raise ValueError("该参数不支持逐标的覆盖，请修改全局参数")
            for symbol in symbols:
                final_path = f"symbol_params.{symbol}.{short_key}"
                old_value = self.effective_symbol_params(config, symbol).get(short_key)
                changes[final_path] = {"old": old_value, "new": new_value, "reason": "控制台参数更新"}
        else:
            old_value = self.get_config_value(config, path)
            changes[path] = {"old": old_value, "new": new_value, "reason": "控制台参数更新"}
        proposal = {
            "type": "parameter_update",
            "status": "pending",
            "operator_id": operator_id,
            "summary": "策略参数热更新",
            "changes": changes,
            "source": "console",
        }
        return self.store.insert("optimization_proposals", proposal, symbol="parameter_update")

    def parse_param_change(self, text: str) -> tuple[str, Any]:
        normalized = text.lower().replace(" ", "")
        path = None
        for alias in sorted(PARAM_ALIASES, key=len, reverse=True):
            if alias.lower().replace(" ", "") in normalized:
                path = PARAM_ALIASES[alias]
                break
        if not path:
            raise ValueError("没有识别到可更新的策略参数")
        matches = re.findall(r"-?\d+(?:\.\d+)?", normalized)
        if not matches:
            raise ValueError("没有识别到新的参数数值")
        return path, self.cast_and_validate(path, matches[-1])

    def create_live_mode_proposal(self, operator_id: str) -> int:
        config = self.read_config()
        proposal = {
            "type": "live_mode",
            "status": "pending",
            "operator_id": operator_id,
            "summary": "切换为正常实盘模式",
            "changes": {"runtime.dry_run": {"old": config.get("runtime", {}).get("dry_run", True), "new": False, "reason": "控制台申请恢复真实运行"}},
            "risk_note": "实盘切换仍不绕过逐标的授权、冷启动锁、AI否决和配置的杠杆上限。",
            "source": "console",
        }
        return self.store.insert("optimization_proposals", proposal, symbol="live_mode")

    def create_small_position_mode_proposal(self, operator_id: str, notional: float) -> int:
        notional = self.cast_and_validate("risk.small_position_notional_usdt", notional)
        config = self.read_config()
        proposal = {
            "type": "small_position_mode",
            "status": "pending",
            "operator_id": operator_id,
            "summary": f"开启小仓实盘测试，单次上限 {notional:g} USDT",
            "changes": {
                "runtime.dry_run": {"old": config.get("runtime", {}).get("dry_run", True), "new": False, "reason": "小仓实盘测试"},
                "risk.small_position_mode": {"old": config.get("risk", {}).get("small_position_mode", False), "new": True, "reason": "小仓实盘测试"},
                "risk.small_position_notional_usdt": {"old": config.get("risk", {}).get("small_position_notional_usdt", 20.0), "new": notional, "reason": "小仓实盘测试"},
            },
            "risk_note": "小仓模式只限制单次名义金额，不改变配置的总杠杆硬上限。",
            "source": "console",
        }
        return self.store.insert("optimization_proposals", proposal, symbol="small_position_mode")

    def create_manual_small_entry_proposal(self, operator_id: str, symbol: str, side: str) -> int:
        side = "long" if side == "long" else "short"
        proposal = {
            "type": "manual_small_entry",
            "status": "pending",
            "operator_id": operator_id,
            "summary": f"手动小仓测试开仓：{self.short_symbol(symbol)} {'做多' if side == 'long' else '做空'}",
            "symbol": symbol,
            "side": side,
            "notional": "exchange_minimum",
            "changes": {},
            "risk_note": "审批后按交易所最低可开数量尝试开仓；该仓位需要你手动平仓。仍受实盘模式、逐标的授权和硬风控限制。",
            "source": "console",
        }
        return self.store.insert("optimization_proposals", proposal, symbol=symbol)

    def approve_proposal(self, proposal_id: int, operator_id: str) -> str:
        row = self.store.fetch_by_id("optimization_proposals", proposal_id)
        if not row:
            raise ValueError("提案不存在")
        payload = row["payload"]
        if payload.get("status") != "pending":
            raise ValueError("提案不是待审批状态")
        config = self.read_config()
        for path, change in (payload.get("changes") or {}).items():
            self.set_config_value(config, path, change.get("new"))
        self.write_config(config)
        payload["status"] = "approved"
        payload["approved_by"] = operator_id
        self.store.update_payload("optimization_proposals", proposal_id, payload)
        self.store.insert("approval_events", {"proposal_id": proposal_id, "operator_id": operator_id, "action": "approved"})
        return f"已同意并应用提案 #{proposal_id}。配置和运行状态已立即热加载。"

    def reject_proposal(self, proposal_id: int, operator_id: str) -> str:
        row = self.store.fetch_by_id("optimization_proposals", proposal_id)
        if not row:
            return "提案不存在。"
        payload = row["payload"]
        payload["status"] = "rejected"
        payload["rejected_by"] = operator_id
        self.store.update_payload("optimization_proposals", proposal_id, payload)
        self.store.insert("approval_events", {"proposal_id": proposal_id, "operator_id": operator_id, "action": "rejected"})
        return f"已拒绝提案 #{proposal_id}，不会修改运行参数。"

    def resolve_symbols(self, text: str, configured_symbols: list[str]) -> list[str]:
        normalized = text.lower()
        found: list[str] = []
        for alias, symbol in SYMBOL_ALIASES.items():
            if alias in normalized and symbol in configured_symbols and symbol not in found:
                found.append(symbol)
        return found

    def resolve_symbols_or_all(self, text: str, configured_symbols: list[str]) -> list[str]:
        symbols = self.resolve_symbols(text, configured_symbols)
        return symbols or list(configured_symbols)

    def get_config_value(self, config: dict[str, Any], path: str) -> Any:
        cur: Any = config
        for part in path.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    def set_config_value(self, config: dict[str, Any], path: str, value: Any) -> None:
        parts = path.split(".")
        cur = config
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value

    def cast_and_validate(self, path: str, value: Any) -> Any:
        rule = PARAM_RULES.get(path)
        if not rule and path.startswith("symbol_params."):
            short_key = path.split(".")[-1]
            global_path = SYMBOL_PARAM_TO_GLOBAL.get(short_key)
            rule = PARAM_RULES.get(global_path or "")
        if not rule:
            raise ValueError(f"参数不在白名单：{path}")
        cast = rule["type"]
        new_value = cast(float(value)) if cast is int else cast(value)
        if new_value < rule["min"] or new_value > rule["max"]:
            raise ValueError(f"{self.param_label(path)} 超出允许范围 {rule['min']} - {rule['max']}")
        return new_value

    def param_label(self, path: str) -> str:
        if path.startswith("symbol_params."):
            short_key = path.split(".")[-1]
            global_path = SYMBOL_PARAM_TO_GLOBAL.get(short_key, path)
            symbol = path.split(".")[1]
            return f"{self.short_symbol(symbol)} {PARAM_RULES.get(global_path, {}).get('label', short_key)}"
        return PARAM_RULES.get(path, {}).get("label", path)

    def short_symbol(self, symbol: str) -> str:
        return symbol.split("/")[0] if "/" in symbol else symbol

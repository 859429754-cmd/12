# 2026-06-15 实盘持仓五档映射显示审计

## 当前目标

修复“交易所已有 ETH 实盘持仓，但控制台五档映射没有显示具体档位，且底部建议仍像无开仓信号”的误导性展示。

以后以本版本为准，忽略之前把已有持仓统一显示为 `持仓中 / 已执行` 的方案。已有实盘持仓必须映射到具体五档：`弱仓 / 标准仓 / 强仓 / 满仓`。如果没有可追溯入场档位，则必须明确标注为估算。

## 根因

- `console/src/App.tsx` 旧逻辑把“已有持仓”当作一个伪档位，导致顶部、AI 面板和右侧栏无法展示具体仓位档。
- 最新 AI 记录可能是 `hold/no_signal`，但它只代表“不追仓/不加仓”，不能覆盖已经存在的 Gate 实盘持仓。
- 只用最新 AI 的 `risk.position_tier` 不可靠，因为开仓后下一轮审计记录可能会变成 `block/hold`。
- 仅靠前端估算不够严谨，应优先使用开仓时 RiskManager 的真实档位。

## 修复

- `ai_quant_trader/core/models.py`
  - `OrderRequest` 新增 `metadata`。
  - `OrderLifecycleEvent` 新增 `metadata`。
- `ai_quant_trader/execution/lifecycle.py`
  - 下单 intent、submitting、filled、partial、refresh 事件都会保留 `request.metadata`。
- `ai_quant_trader/app.py`
  - 主账户开仓写入 `risk_position_tier`、`risk_position_scale`、`risk_decision_score`、`risk_clipped_qty`、`ai_confidence`、`strategy_action`。
  - 账户2 follower 开仓写入同一套共享 AI/RiskManager 元数据，并记录 follower sizing reason。
- `ai_quant_trader/api/server.py`
  - 新增只读 `/api/order-lifecycle`，支持 `symbol` 与 `account_slot` 过滤。
- `console/src/App.tsx`
  - 前端加载当前登录账户的 `/api/order-lifecycle`。
  - 五档显示优先级改为：入场订单 RiskManager 档位 > 当前名义仓位 / 权益 / 杠杆上限估算 > 最新 AI 分数估算 > 弱仓兜底。
  - 有持仓时，`hold/no_signal` 文案改为“不追仓/不加仓，并不否认既有持仓”。

## 验证

- `python -m pytest tests/test_order_lifecycle.py tests/test_console_api.py::test_order_lifecycle_endpoint_filters_account_slot -q` -> `11 passed`
- `cd console && npm.cmd run build` -> 通过
- Playwright 真实页面验收通过：
  - 模拟 ETH 多仓 `0.23`
  - 模拟入场订单 metadata：`risk_position_tier=weak`、`risk_position_scale=0.25`
  - 模拟最新 AI 记录：`hold`、`confidence=0.95`、`risk.position_tier=block`
  - 页面仍显示 `当前持仓映射档`、`弱仓 / 25%`
  - 页面没有显示 `当前 阻断 / 0%` 或 `持仓中 / 已执行`
- `python -m compileall ai_quant_trader tests scripts` -> 通过
- `python -m pytest -q` -> `223 passed`
- `python scripts/public_repo_preflight.py` -> `ok=true`

## 当前结论

这次修复后，控制台不会再被最新 `hold/no_signal` AI 审计记录误导为“无仓位档”或“阻断档”。已有 Gate 实盘持仓优先显示入场时 RiskManager 的五档结果；如果历史订单没有档位元数据，才按当前持仓暴露比例估算。

## 未决风险

- 历史订单在本次修复前没有 `metadata`，旧持仓会先走“持仓暴露比例估算”。这只影响展示来源，不影响真实持仓、止损、平仓或风控。
- 后续若要完全精准，应在持仓快照表中也持久化 `entry_risk_position_tier`，作为订单生命周期之外的第二来源。

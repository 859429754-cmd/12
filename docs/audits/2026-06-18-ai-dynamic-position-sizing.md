# 2026-06-18 AI 动态仓位权限审计

以后以本版本为准，忽略之前“AI 只能降低仓位或否决，不能放大仓位”的方案。

## 当前合同

- 本地 ETH 1h 趋势策略仍是唯一开仓方向来源。
- AI 不允许发明 `LONG/SHORT` 方向，不允许在本地策略没有开仓信号时主动开仓。
- AI 可以在策略触发后，把仓位映射到 `block/weak/normal/strong/full` 五档。
- 五档默认按账户当前风险上限解释：
  - `block = 0%`
  - `weak = 25%`
  - `normal = 50%`
  - `strong = 75%`
  - `full = 100%`
- `RiskManager` 最终仍会执行：
  - readiness 检查
  - 冷启动授权
  - 同向重复持仓禁止
  - 新闻冲突阻断
  - 订单流冲突阻断
  - 极端震荡/极端新闻/弱形态/弱订单流限仓
  - 账户最大杠杆上限
  - 已有持仓占用后的剩余名义额度裁剪
  - small position 实盘演练裁剪

## 审计字段

`RiskDecision` 现在持久化：

- `strategy_baseline_notional`: 策略原始建议名义仓位。
- `ai_desired_notional`: AI 五档映射后的目标名义仓位。
- `sizing_basis`: `account_risk_cap` 或 `strategy_signal`。

下单元数据同步写入：

- `strategy_baseline_notional`
- `ai_desired_notional`
- `sizing_basis`

这样控制台和日志可以判断 AI 是升档、维持还是降仓。

## 禁止项

- AI 不得绕过本地策略方向。
- AI 不得绕过 RiskManager。
- AI 不得突破账户最大杠杆上限。
- AI 不得在策略 `suggested_qty=0` 时凭空生成开仓数量。
- AI 不得因新闻同向就自动满仓；仍需订单流、密集区、形态和整体风险确认。

## 验证

新增测试：

- `tests/test_risk.py::test_ai_can_amplify_from_strategy_baseline_to_account_risk_tier`
- `tests/test_risk.py::test_ai_dynamic_sizing_does_not_create_qty_when_strategy_qty_is_zero`
- `tests/test_risk.py::test_legacy_strategy_signal_sizing_can_be_kept_by_config`

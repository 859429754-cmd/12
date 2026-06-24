# 2026-06-25 开仓/平仓/下单逻辑审计

本轮审计以当前代码和测试为准，不依赖聊天记忆。核心目标是检查 ETH 趋势策略从本地信号、AI 决策、RiskManager、订单状态机、Gate 下单、原生止损、软件止损到账户2跟随的完整链路。

## 当前有效执行合同

- 本地趋势方向只来自 `ETH 1h KC + VOL + KDJ` 策略。
- EMA89 可以计算和展示，但当前实盘参数 `use_ema_filter=false`，不是实盘入场过滤器。
- AI 不能发明交易方向；AI 只能在本地策略信号触发后，按五档仓位动态放大、维持、降档或阻断。
- 已有同方向 Gate 持仓或本地 `trend_state` 时，不允许重复同向加仓。
- 交易所私有状态、对账、订单状态、数据健康或 AI 漂移异常时，禁止新开仓；已有持仓不由该安全闸自动平仓。
- 账户2是 follower/mirror 账户，使用同一份 AI 决策，但按账户2自己的余额、杠杆上限、最小下单量重新计算数量。

## 已修复漏洞

### synthetic K 线不得进入实盘开仓

问题：`MarketDataClient.fetch_ohlcv_history()` 在真实交易所 K 线全部不可用时会生成 `data_source=synthetic` 的占位 K 线。此前 `DataHealthMonitor._ohlcv_check()` 只检查 K 线时间新鲜度，不检查数据源，因此 synthetic K 线如果时间新鲜、订单流可用，可能通过开仓数据闸。

修复：`DataHealthMonitor._ohlcv_check()` 现在遇到 `candles.attrs["data_source"] == "synthetic"` 直接返回 `BLOCK / ohlcv_synthetic_source`。

回归：新增 `tests/test_monitoring_gates.py::test_data_health_blocks_synthetic_ohlcv_source`。

### 同轮多标的风控快照必须在成交后刷新

问题：`TradingApp.run_once()` 在循环开始统一读取一次 `positions`，然后逐标的评估。当前实盘主合同只有 ETH 进入开仓授权，因此事故概率低；但如果未来同时开启 BTC/SOL 或震荡账户，同一轮第一笔成交后，后续标的可能用旧持仓和旧剩余额度评估总杠杆。

修复：主账户平仓、反手平仓和开仓成功后，立即重新读取交易所持仓快照，再进入后续标的风险评估。

配套修复：`MockExchangeGateway` 现在从订单元数据 `signal_current_price` 读取模拟成交参考价，使模拟持仓 `mark_price` 和 `notional` 能参与真实风险回归测试。

回归：新增 `tests/test_trading_chain_smoke.py::test_run_once_refreshes_positions_after_entry_before_next_symbol_risk`。该测试构造 ETH 与 BTC 同轮同时触发多头信号，ETH 用满 1x 总杠杆后，BTC 必须被 `max_total_leverage_reached` 阻断。

## 本轮未改但需要持续关注的风险

### 账户2跟随失败不会回滚账户1主订单

当前顺序是账户1先完成主订单和止损，再执行账户2镜像。账户2如果余额不足、最小下单量不足、API 失败或原生止损失败，会写入 `follower_executions`，但不会自动撤销或回滚账户1。该设计避免因为 follower 故障影响主账户执行；如果未来要求账户2必须强一致跟随，需要新增 `follower_required=true` 的开仓前预检和失败阻断策略。

### 动态 AI 仓位使用账户风险上限作为基准

当前 `ai_dynamic_position_sizing=true` 时，五档仓位基准是账户最大风险上限，而不是策略原始建议数量。这样 AI 有放大仓位能力，但也意味着 `weak/normal/strong/full` 的实际名义价值取决于账户权益和 `max_total_leverage`。大资金无人值守前必须确认每个账户的最大杠杆上限、单账户资金规模和小仓模式符合真实风险承受能力。

## 验证命令

```powershell
python -m pytest tests\test_trading_chain_smoke.py tests\test_gateway_runtime.py tests\test_order_lifecycle.py tests\test_risk.py tests\test_monitoring_gates.py tests\test_follower_execution.py tests\test_strategy_trend.py -q
```

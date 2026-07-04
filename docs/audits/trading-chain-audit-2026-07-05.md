# 交易链路审计记录 2026-07-05

## 结论

本轮 `extended` 交易链路审计通过。

- 当前代码 release：`c07d1a5`
- 审计脚本：`scripts/trading_chain_audit.py`
- 审计模式：`extended`
- 用例数量：`21`
- 测试结果：`21 passed`
- 是否调用真实 DeepSeek：否
- 是否提交真实 Gate.io 订单：否
- 是否读取或打印 `.env.runtime`：否

命令：

```powershell
python scripts\trading_chain_audit.py --mode extended --json-out output\audit\trading_chain_audit_latest.json
```

输出摘要：

```text
ok=true
case_count=21
stdout_tail="21 passed"
```

## 已验证链路

### 策略信号到订单闭环

- 伪造趋势策略信号进入 `TradingApp.run_once`
- 信号经过本地策略入口、AI 决策对象、`RiskManager`
- Mock 网关开仓
- 原生止损挂出
- 退出信号触发平仓
- 原生止损撤销
- 本地趋势状态清理

覆盖用例：

- `test_trading_cycle_opens_places_stop_then_exits_and_cancels_stop`

### live_addon 与 Gate 净仓止损

- 持仓闭 K 复评加仓后，Gate 同向仓位按净仓合并处理
- 新止损必须覆盖合并后的净仓
- 不允许遗留只覆盖旧仓位的分裂止损

覆盖用例：

- `test_trading_cycle_replaces_net_position_stop_after_live_addon_once`

### 止损和平仓 follower 同步

- 软件 ATR 止损触发时，主账户平仓动作同步到 follower 账户
- 主账户原生止损成交被检测后，同步 follower 平仓

覆盖用例：

- `test_software_atr_stop_mirrors_exit_to_follower`
- `test_primary_native_stop_fill_mirrors_exit_to_follower`

### 订单生命周期与幂等

- 下单前先落库 `intent_recorded`
- 提交异常后进入 `unknown`
- 重复 `client_order_id` 不允许盲目重复提交
- 部分成交状态可以从交易所回报刷新
- 撤单失败记录为 `cancel_failed`
- unresolved `unknown/cancel_failed` 会阻断 readiness

覆盖用例：

- `test_order_lifecycle_records_intent_before_submission`
- `test_order_lifecycle_unknown_after_submit_error_blocks_blind_retry`
- `test_order_lifecycle_refresh_updates_partial_fill`
- `test_order_lifecycle_cancel_failure_is_audited`
- `test_live_readiness_blocks_unresolved_order_lifecycle_even_after_newer_ok_event`

### 实盘安全闸

- live 持仓读取失败时，不允许继续交易循环开新仓
- live 原生止损状态未知时，不通过本系统自动补救平仓，要求人工去 Gate 官方端处理
- 本地 native stop id 在交易所查不到时阻断
- 交易所 native stop 数量不足以覆盖净仓时阻断
- 交易所 native stop 触发价偏离本地 ATR 固定止损时阻断
- 本地 stale trend state 只有在确认终态止损和平仓后才修复

覆盖用例：

- `test_live_position_fetch_failure_blocks_trading_cycle`
- `test_live_native_stop_unknown_requires_manual_gate_without_auto_close`
- `test_reconciliation_blocks_when_native_stop_id_is_not_found_on_exchange`
- `test_reconciliation_blocks_when_native_stop_amount_does_not_cover_position`
- `test_reconciliation_blocks_when_native_stop_trigger_price_drifted`
- `test_terminal_stop_with_flat_exchange_position_repairs_stale_trend_state`

### follower 账户隔离与失败降级

- follower 使用自己的 Gate credential binding，不复用 trend 账户密钥
- 不支持的 hedge position 在 live 模式 fail closed
- `TradingApp.close()` 会关闭 follower execution 资源
- follower 开仓失败、平仓失败、订单状态刷新失败都会写入 exchange safety，并阻断 live readiness

覆盖用例：

- `test_live_gateway_can_bind_follower_account_slot`
- `test_gate_hedged_position_blocks_unsupported_live_mode`
- `test_trading_app_close_closes_follower_execution`
- `test_live_follower_entry_failure_marks_exchange_safety_failed`
- `test_live_follower_exit_failure_marks_exchange_safety_failed`
- `test_live_follower_order_status_refresh_failure_marks_exchange_safety_failed`

## 明确边界

本审计证明的是本地 deterministic 链路和 mock/live-failure 模拟路径，不等于证明以下事项：

- DeepSeek 真实 API 当前余额充足
- Gate.io 当前网络、撮合、回报一定正常
- 当前策略具备长期正收益
- 当前小资金 live_addon 必然改善收益
- 真实行情下不会出现滑点、拒单、限流或资金费率冲击

这些事项需要继续通过云端 readiness、真实 Gate 只读对账、实盘小仓记录、AI 仓位分档效果审计、walk-forward / out-of-sample 研究来证明。

## 下一步审计重点

- 将 `scripts/trading_chain_audit.py --mode extended` 纳入更高层级发布前检查或管理员手动审计入口。
- 对真实云端最近订单、AI 决策、follower 镜像结果做只读一致性审计。
- 继续对 DeepSeek 成本、缓存命中率、主备 key failover 行为做运行期审计。

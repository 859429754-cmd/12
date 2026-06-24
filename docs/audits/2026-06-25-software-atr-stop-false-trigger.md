# 2026-06-25 软件 ATR 止损误触发审计

## 结论

以后以本版本为准，忽略之前“软件备用 ATR 止损可以直接使用外部 1m K 线 high/low 触发实盘平仓”的方案。

本次 ETH 空单在北京时间 `2026-06-25 00:01:58` 开仓，`00:02:19` 被系统以 `software_fixed_atr_stop` 平仓。该行为不正常。

## 证据

- `ai_decisions`：`2026-06-24 16:01:58 UTC` 生成 ETH `open_short`，`confidence=0.65`，消息面同向但高事件风险降仓。
- `orders`：
  - 开仓卖出：`2026-06-24T16:01:58Z`，成交价约 `1620.20`。
  - Gate 原生止损买入单：同一秒提交，止损价约 `1643.7248`，状态为 `open`。
  - 软件平仓买入：`2026-06-24T16:02:19Z`，成交价约 `1620.28`，reason=`software_fixed_atr_stop`。
- 对 ETH 空单而言，固定 ATR 止损价在 `1643.72` 上方，价格必须上穿该价位才应触发止损。
- 实际平仓价 `1620.28` 离止损价很远，且 Gate 原生止损没有触发，说明不是正常 ATR 止损。

## 根因

`TradingApp._enforce_fixed_atr_stop_once()` 使用 `MarketDataClient.fetch_ohlcv(..., "1m", closed_only=False)` 返回的 1m K 线，并由 `_fixed_atr_stop_signal()` 用该 K 线的 high/low 判断止损是否触发。

这个口径对实盘软件备用止损不安全：

- 1m K 线 high/low 可能包含当前分钟内早于开仓时刻的价格。
- `source="auto"` 可能来自非 Gate 的外部公开数据，不能直接作为 Gate 实盘止损触发源。
- Gate 原生止损才是交易所侧真实触发依据；软件备用止损必须使用交易所持仓 `mark_price` 或等价的当前价确认。

## 修复

- 软件备用 ATR 止损改为使用交易所持仓 `PositionSnapshot.mark_price` 判断触发。
- 只有当持仓 mark price 真正越过固定 ATR 止损价时，才提交软件平仓。
- 保留原有 `_fixed_atr_stop_signal()` 的 K 线 high/low 能力，用于非软件即时止损路径；`_enforce_fixed_atr_stop_once()` 显式传入 `use_candle_extremes=False`。
- 新增回归测试：空单止损价在 `1643` 附近，外部 1m K 线 high 异常到 `1650`，但 Gate mark price 仍在 `1620.28`，系统不得软件平仓。

## 验证

- `python -m pytest tests\test_trading_chain_smoke.py::test_software_atr_stop_uses_exchange_mark_not_external_1m_wick_for_short tests\test_trading_chain_smoke.py::test_software_atr_stop_mirrors_exit_to_follower -q` -> `2 passed`
- `python -m pytest tests\test_trading_chain_smoke.py tests\test_gateway_runtime.py tests\test_order_lifecycle.py -q` -> `42 passed`

## 风控边界

- Gate 原生止损仍是首选硬保护。
- 软件备用止损只作为原生止损之外的冗余保护，不得基于外部聚合 K 线影线误杀实盘仓位。
- 账户2跟随平仓仍应跟随账户1真实退出动作，但不能跟随误触发退出。

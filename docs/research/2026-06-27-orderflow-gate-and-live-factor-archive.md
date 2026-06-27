# 2026-06-27 订单流覆盖闸与实盘因子影子归档

## 结论

以后以本文件为准：订单流、新闻、BTC 龙头、ETH/BTC 轮动、盘口质量等 live-only 因子不能因为“逻辑上看起来合理”直接进入实盘五档权重。必须先满足两类证据：

- 历史订单流回填通过覆盖闸。
- 实盘 live factor snapshot 积累足够闭合交易样本，并与 trade ledger 做事后归因。

忽略之前“用部分订单流 checkpoint 或聊天记忆直接寻优上线”的方案。

## 已落地

- `scripts/historical_orderflow_backfill.py`
  - 新增 `coverage_verdict`。
  - 新增 `--min-usable-ratio`，默认 `0.80`。
  - 新增 `--strict-coverage`，覆盖不足时退出码为 `2`。
  - 覆盖不足的输出会标记 `research_gate=blocked_until_backfill_complete`。

- `ai_quant_trader/research/live_factor_archive.py`
  - 新增 `build_live_factor_snapshot`。
  - 只记录开仓前可见因子。
  - 显式过滤 `pnl / mae / mfe / exit_reason / exit_time` 等结果字段，避免未来函数。

- `ai_quant_trader/core/models.py`
  - 新增 `LiveFactorSnapshot`。

- `ai_quant_trader/storage/sqlite.py`
  - 新增 `live_factor_snapshots` 表。

- `ai_quant_trader/app.py`
  - 交易循环在本地策略产生非 `HOLD` 信号时写入 `live_factor_snapshots`。
  - 当前归档状态为 `shadow_only`，不改变实盘交易决策。

## 设计边界

这次没有修改：

- 趋势策略参数。
- TradingView 对齐回测合同。
- 云端实盘五档权重。
- AI 是否允许放大仓位的生产规则。

## 后续验收标准

订单流进入历史寻优前必须满足：

- 目标时间段所有 requested windows 的 usable ratio 达到阈值。
- missing daily archives 不被解释为多头或空头证据。
- 回填结果通过 walk-forward 年份分段验证。

实盘 live-only 因子进入调仓模型前必须满足：

- 至少积累 30 笔以上闭合交易样本，只作为早期观察。
- 更稳妥目标是 100 笔以上闭合交易样本。
- 每笔样本必须能关联：策略信号、AI 决策、RiskManager 档位、订单执行、退出原因、PnL、MAE/MFE。
- 任何结果字段不得出现在开仓前因子快照里。

## 验证

```powershell
python -m pytest tests\test_historical_orderflow_backfill.py tests\test_live_factor_archive.py -q
```

结果：`7 passed`。

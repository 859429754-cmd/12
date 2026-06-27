# 2026-06-27 实盘因子归因审计

## 结论

以后以本文件为准：AI 仓位模型是否有效，必须看闭合交易上的归因结果，而不是看单次 AI 文案是否“合理”。忽略之前“只看 AI 当时解释就判断调仓有效”的方案。

## 已落地

- `scripts/ai_position_tier_audit.py`
  - 原有能力：比较实际 AI 仓位和策略基准仓位的 PnL 差异。
  - 新增能力：把 `live_factor_snapshots` 绑定到闭合交易，输出 live factor coverage 和 winner/loser 因子均值差异。
  - 匹配规则：同一标的、同一方向、入场前 180 分钟内最近的 snapshot。
  - 绑定失败时保留 `factor_snapshot_not_matched` 警告，不强行推断。

- `tests/test_ai_position_tier_audit.py`
  - 新增测试覆盖：一笔盈利交易、一笔亏损交易分别绑定 snapshot，并验证：
    - 覆盖率。
    - `news_risk_score` 在赢家/输家上的差异。
    - `orderflow_confirmation_score` 在赢家/输家上的差异。

## 输出口径

新增字段：

- `live_factor_coverage`
  - `entries_with_snapshot`
  - `closed_with_snapshot`
  - `entry_coverage_rate`
  - `closed_coverage_rate`
  - `sample_warning`

- `live_factor_effects`
  - `winner_count`
  - `loser_count`
  - `winner_avg`
  - `loser_avg`
  - `winner_minus_loser`

## 设计边界

这次没有修改：

- 实盘策略参数。
- AI 五档映射。
- RiskManager 放大/降仓逻辑。
- 云端运行配置。

这次只增强审计能力。若后续要把 live factor effects 用于权重更新，必须先满足样本量要求，并走 walk-forward 或 shadow ledger 验证。

## 验证

```powershell
python -m pytest tests\test_ai_position_tier_audit.py -q
```

结果：`6 passed`。

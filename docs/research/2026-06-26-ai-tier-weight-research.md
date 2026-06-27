# 2026-06-26 AI 五档仓位权重研究

以后以本文为本轮研究记录准绳：使用当前 291 笔 ETH 1h 纯策略闭合交易样本，结合 Binance futures `aggTrades` 订单流 proxy，评估五档仓位逻辑。忽略此前“未复测就直接把实盘切到旧 balanced_candidate_v1”或“仅凭单切分最优结果直接上线”的方案。

## 研究边界

- 本研究不调用 DeepSeek，不把 DeepSeek 当历史回测引擎。
- 本研究只评估开仓前一次性五档仓位，不包含持仓后闭 K 加仓/减仓。
- 历史新闻方向归档不完整，`news_direction_alignment_score` 不能做 hindsight 优化。
- 订单流数据是 Binance futures `aggTrades` proxy，代表参与度、流动性、冲击质量和大单活跃度，不等于完整盘口深度。
- 结果只允许进入 shadow ledger / 小仓灰度候选，不允许直接大资金切换。

## 数据口径

- 纯策略样本：`data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json`
- 订单流 proxy：`data/research/historical_orderflow_proxy_eth_2022_2026.json`
- 样本数：`291` 笔 ETH 1h 趋势策略闭合交易
- 单切分：`2024-01-01`
- 训练集：`132` 笔
- 验证集：`159` 笔
- rolling walk-forward：7 个半年验证窗口，训练集只使用每个验证窗口之前的历史信号
- embargo：`7` 天

## 引擎修正

本轮修复了两个会误导结论的研究引擎问题：

1. walk-forward 子窗口不能直接用历史全路径绝对 `pnl / 当前窗口权益`。现在每笔交易重建 `baseline_equity_before`，任意验证窗口都使用 `pnl / baseline_equity_before`。
2. 半年验证窗口交易数可能不足 30，不能沿用全样本 `robust_objective` 的 30 笔最低要求。现在 rolling fold 使用独立小窗口目标函数。

回归测试：

```powershell
python -m pytest tests\test_ai_tier_weight_research.py -q
```

结果：`10 passed`。

## 基准结果

| 模型 | 全样本收益 | 最大回撤 | 交易数 | rolling 分数 | rolling 负收益窗口 | rolling 破产窗口 |
|---|---:|---:|---:|---:|---:|---:|
| `balanced_candidate_v1` | `2340.99%` | `-49.85%` | `288` | `-1.0140` | `2` | `0` |
| `factor_ranked_current_weights_zero_news` | `1682.30%` | `-34.75%` | `222` | `-0.1891` | `2` | `0` |
| `factor_ranked_current_weights_testable_renormalized` | `2664.97%` | `-49.90%` | `270` | `-1.8018` | `3` | `0` |

结论：当前偏保守实盘权重收益不最高，但 rolling walk-forward 稳健性最好，主要优势是最大回撤低。对“大资金无人值守”标准，不能只看全样本收益。

## 单切分最优候选

- train return：`189.31%`
- train DD：`-34.55%`
- validation return：`1603.20%`
- validation DD：`-41.97%`
- full sample return：`4827.60%`
- full sample DD：`-41.97%`
- trades：`278`
- thresholds：`full >= 0.80`, `strong >= 0.72`, `normal >= 0.60`, `weak >= 0.50`

单切分最优很强，但不能直接上线，因为它没有证明 rolling 稳健性优于当前实盘权重。

## rolling walk-forward 最优候选

- walk_forward_score：`-1.4001`
- ok_folds：`7`
- negative_folds：`2`
- ruined_folds：`0`
- min validation return：`-21.65%`
- median validation return：`35.14%`
- worst validation DD：`-44.24%`
- full sample return：`3545.03%`
- full sample DD：`-44.24%`
- trades：`273`

候选参数：

```text
full   >= 0.80
strong >= 0.72
normal >= 0.64
weak   >= 0.50
block  <  0.50
```

权重：

```text
technical_signal_score        0.16
orderflow_confirmation_score  0.24
pattern_confirmation_score    0.10
trend_confirmation_score      0.10
dense_zone_breakout_score     0.10
range_safety_score            0.11
htf_alignment_score           0.06
news_safety_score             0.04
btc_leader_score              0.02
eth_btc_rotation_score        0.01
```

该候选收益高于当前保守版，但 rolling 分数仍低于当前保守版，且存在 2 个负收益验证窗口。它只能作为 shadow ledger 候选，不能直接替换实盘默认。

## 因子有效性排序

按当前样本 winner/loser effect size：

1. `orderflow_confirmation_score`: `0.311`
2. `pattern_confirmation_score`: `0.182`
3. `trend_confirmation_score`: `0.129`
4. `range_safety_score`: `0.126`
5. `volume_score`: `0.125`
6. `regime_risk_safety_score`: `-0.093`
7. `technical_signal_score`: `0.085`
8. `breakout_score`: `0.085`
9. `dense_zone_breakout_score`: `0.055`
10. `htf_alignment_score`: `0.048`
11. `orderflow_direction_score`: `0.029`

订单流最有效，但强的是“参与度/流动性/冲击质量”，不是简单方向同向。消息面仍应在实盘中作为方向确认与执行风险 cap，但当前历史样本不能证明新闻方向权重。

## 结论

- 不建议直接把实盘切换到单切分最优或 rolling 最优候选。
- 当前实盘保守权重在 rolling walk-forward 上更稳，适合继续作为默认。
- 如果要降低“过保守”问题，下一步应做 shadow ledger：同一真实信号同时记录当前保守版、rolling 最优候选、单切分最优候选三套五档输出和后续盈亏。
- 只有 shadow ledger 证明候选版减少亏损单仓位、放大盈利单仓位，并且不显著恶化回撤后，才允许进入小仓灰度。

## 命令

```powershell
python scripts\ai_tier_weight_research.py --features data\research\pure_strategy_tier_research_eth_2022_2026_no_ema.json --orderflow data\research\historical_orderflow_proxy_eth_2022_2026.json --output data\research\ai_tier_weight_research_eth_2022_2026.json --top 20
python -m pytest tests\test_ai_tier_weight_research.py -q
```

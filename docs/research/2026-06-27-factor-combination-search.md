# 2026-06-27 因子组合受控寻优

以后以本文件为准：因子组合寻优必须是受控寻优，不允许把所有市面因子直接全排列后按全样本收益挑最优。忽略“因子越多、组合越多就越稳”的方案。

## 本轮目标

在 2020-01-01 至 2026-06-26 的 ETH 1h 趋势策略样本上，只使用因子注册表中 `BACKTESTABLE_NOW` 且当前样本已经存在的因子，测试小规模因子组合是否能得到稳定的五档仓位规则。

本轮不使用：

- 新闻方向和新闻风险。
- BTC 龙头和 ETH/BTC 轮动。
- 实时盘口、spread、depth。
- funding、OI、爆仓、链上数据。
- `pnl / mae_pct / mfe_pct` 等开仓后结果字段。

这些因子要么缺历史归档，要么是结果标签，不能进入当前历史寻优。

## 命令

```powershell
python scripts\factor_combination_research.py --input data\research\pure_strategy_tier_research_eth_2020_2026_no_ema.json --output data\research\factor_combination_research_eth_2020_2026.json --min-size 2 --max-size 4 --top 20 --max-candidates 10000
```

## 样本

- 数据源：Binance 真实 1h K 线。
- 样本：398 笔趋势策略交易。
- 候选组合数：1734。
- 参与组合的因子：
  - `volume_multiple`
  - `atr_pct`
  - `dense_zone_breakout_score`
  - `dense_range_score`
  - `regime_trend_score`
  - `regime_range_score`
  - `regime_risk_score`
  - `pattern_confirmation_score`
  - `strategy_signal_strength`
  - `higher_timeframe_alignment`
  - `higher_timeframe_trend_strength`
  - `kc_breakout_atr`

## 结果

本轮没有 `clean candidate`。

也就是说，没有任何组合同时通过：

- 年份分段稳定性。
- 年度回撤限制。
- 负收益年份限制。
- 全样本不过度依赖少数年份。
- Profit factor 基础门槛。

Top 候选：

| 排名 | 因子组合 | return | max DD | PF | trades | flags |
|---:|---|---:|---:|---:|---:|---|
| 1 | `volume_multiple + atr_pct` | 879.43% | -39.90% | 1.452 | 258 | `year_drawdown_too_deep`, `full_sample_dominated_by_few_years` |
| 2 | `dense_zone_breakout_score + volume_multiple + atr_pct` | 569.33% | -29.95% | 1.459 | 253 | `year_drawdown_too_deep`, `full_sample_dominated_by_few_years` |
| 3 | `regime_trend_score + higher_timeframe_trend_strength + atr_pct` | 462.87% | -37.35% | 1.671 | 211 | `year_drawdown_too_deep`, `full_sample_dominated_by_few_years` |
| 4 | `volume_multiple + higher_timeframe_trend_strength + atr_pct` | 302.65% | -28.83% | 1.447 | 214 | `too_many_negative_years`, `year_drawdown_too_deep` |

## 解释

1. `volume_multiple` 和 `atr_pct` 是当前最有用的可回测组合方向，但仍不足以构成可上线模型。
2. `dense_zone_breakout_score` 可以改善部分结构质量，但没有解决年度回撤问题。
3. 高周期强度在部分组合里提高 PF，但会牺牲覆盖率，并且仍有负年份。
4. 当前本地可回测因子不足以稳定做到“亏损单降仓、盈利单放大”。

## 结论

不能把本轮任何组合直接替换云端实盘五档。

下一步应做三件事：

1. 单独长任务补齐订单流 2020-2026 回填，避免订单流因子缺失。
2. 对新闻、BTC、ETH/BTC、盘口等 live-only 因子做实盘归档，至少形成 30 笔闭合交易 shadow ledger。
3. 对 `volume_multiple + atr_pct` 方向做参数邻域稳定性测试，而不是按当前最优阈值上线。

## 验证

- `python -m pytest tests\test_factor_combination_research.py -q` -> `4 passed`
- `python -m compileall scripts\factor_combination_research.py tests\test_factor_combination_research.py` -> passed


# calibrated_v2_loss_aware 减亏候选研究

以后以本文件为准：`calibrated_v2_loss_aware` 是离线研究候选，不是当前云端实盘主模型。忽略之前“直接用 v2 替换实盘五档”的方案。当前云端实盘仍以 `calibrated_v1_controlled` 为主，`legacy_factor_ranked` 保留为回滚基线。

## 研究目的

用户要求优化当前 AI 五档仓位模型，使它不只是提高收益，也要尽量做到：

- 盈利单可以适度放大仓位。
- 亏损单尽量少被放大。
- 部分亏损单能被降仓。
- 不破坏 TradingView 对齐的 ETH 1h 趋势策略回测合同。

本研究不调用 DeepSeek，不改变云端实盘参数，不部署。

## 数据和代码来源

- 样本：`data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json`
- 订单流 proxy：`data/research/historical_orderflow_proxy_eth_2022_2026.json`
- 样本规模：291 笔 ETH 1h 闭合交易，2022-01-05 至 2026-06-25
- 评估脚本：`scripts/ai_tier_weight_research.py`
- 候选函数：`ai_quant_trader/risk/sizing.py::calibrated_v2_loss_aware_policy`
- 生成结果：`data/research/ai_tier_weight_research_eth_2022_2026.json`

## 候选逻辑

`calibrated_v1_controlled` 的问题是历史样本里放大了很多亏损单：

- 盈利单升仓：83 笔
- 亏损单升仓：119 笔
- 亏损单降仓：0 笔

v2 不再使用抽象的 `loss_risk_score` 直接扣分，因为该分数对盈亏区分能力很弱。当前 v2 改成订单流门槛模型：

- `orderflow_confirmation_score < 0.74` 时，不允许相对旧五档升仓。
- `orderflow_confirmation_score < 0.30` 时，主动降一档。
- 满仓必须满足：订单流 >= 0.80、形态 >= 0.88、密集区 >= 0.45、区间安全 >= 0.62。
- 强仓必须满足：订单流 >= 0.74，且形态或密集区确认，同时趋势分 >= 0.45。
- 订单流低且成交量低时，最高只允许弱仓。

这个逻辑的核心判断是：历史样本里订单流确认度是当前可用因子中对盈亏分离最强的因子，但仍不是稳定 alpha，只能作为升仓门槛。

## 核心结果

| 模型 | 收益 | 最大回撤 | 交易数 | 盈利因子 | Trade Sharpe | WF Score |
|---|---:|---:|---:|---:|---:|---:|
| legacy_factor_ranked | 1682.30% | -34.75% | 222 | 1.51 | 1.24 | -0.1891 |
| calibrated_v1_controlled | 3686.62% | -41.87% | 245 | 1.41 | 1.24 | -1.1032 |
| calibrated_v2_loss_aware | 2876.74% | -36.53% | 216 | 1.46 | 1.23 | -1.1967 |

相对旧五档的仓位变化：

| 模型 | 盈利单升仓 | 盈利单降仓 | 亏损单升仓 | 亏损单降仓 |
|---|---:|---:|---:|---:|
| calibrated_v1_controlled | 83 | 0 | 119 | 0 |
| calibrated_v2_loss_aware | 43 | 11 | 50 | 14 |

## 结论

v2 的确更接近“减少亏损单被放大”的目标：

- 亏损单升仓从 119 笔降到 50 笔。
- 亏损单降仓从 0 笔提高到 14 笔。
- 最大回撤从 v1 的 -41.87% 改善到 -36.53%。
- 盈利因子从 v1 的 1.41 改善到 1.46。

但 v2 仍不能直接替换实盘：

- 总收益低于 v1：2876.74% vs 3686.62%。
- walk-forward 分数略弱于 v1，明显弱于旧保守五档。
- 负收益验证窗口增加到 3 个。
- 历史新闻方向不完整，新闻无法参与完整历史寻优。

## 决策

当前建议：

- 不切换云端实盘主模型。
- 保留 `calibrated_v2_loss_aware` 作为研究候选。
- 下一步应进入 shadow ledger：对每个真实信号同时记录 legacy、v1、v2 三套仓位建议和后续盈亏，至少累计 30-50 笔真实信号后再判断是否灰度上线。

如果用户明确要求小资金灰度，必须增加：

- 配置开关，不允许裸切。
- 回滚命令。
- 控制台显示 v1/v2/legacy 三套建议差异。
- 每笔信号的实际执行档位和候选档位审计。

## 验证

```powershell
python -m pytest tests\test_ai_tier_weight_research.py -q
# 13 passed

python scripts\ai_tier_weight_research.py --output data\research\ai_tier_weight_research_eth_2022_2026.json
# wrote data\research\ai_tier_weight_research_eth_2022_2026.json and .md
```

# 2026-06-27 calibrated_v21_profit_loss 研究

以后以这个版本为准：v2.1 是 `calibrated_v2_loss_aware` 的扩盈/减亏通道升级候选，先用于离线研究和 shadow ledger，不直接替换云端实盘。忽略之前“v2 一旦升级就直接上线大资金”的方案。

## 假设

v2 的问题不是收益不足，而是仍有较多亏损单被升仓。v2.1 的目标是：

- 盈利放大必须满足执行质量和结构质量同时强。
- 减亏必须用入场前信息判断：订单流弱、形态弱、密集区弱、趋势弱、震荡风险高、新闻/高周期背景差。
- 严禁使用 `pnl / mae / mfe / exit_reason` 等结果字段。

## 代码落点

- `ai_quant_trader/risk/sizing.py::profit_expansion_score`
- `ai_quant_trader/risk/sizing.py::calibrated_v21_profit_loss_policy`
- `scripts/ai_tier_weight_research.py::calibrated_v21_profit_loss_research_policy`
- `tests/test_ai_tier_weight_research.py`
- `tests/test_risk.py`

## 规则摘要

v2.1 在 v2 基础上新增两个通道：

### 扩盈通道

主要看：

- 订单流确认
- 形态确认
- 密集区突破
- 趋势确认
- 震荡安全
- 消息方向
- BTC/ETH 轮动背景

### 减亏通道

主要看：

- 订单流弱
- 形态弱
- 密集区弱
- 趋势弱
- 震荡风险
- 新闻安全性
- BTC/ETH 背景不佳
- 技术信号强但结构不跟随的假突破风险

强仓/满仓必须同时满足扩盈通道较强、减亏风险较低。

## 研究运行

命令：

```powershell
python scripts\ai_tier_weight_research.py --output data\research\ai_tier_weight_research_eth_2022_2026_v21.json
```

样本：

- ETH 1h
- 291 笔闭合交易研究样本
- 不调用 DeepSeek
- 不做历史新闻 hindsight 优化

## 结果对比

| 方案 | 收益 | 最大回撤 | PF | Sharpe | 交易数 | 阻断 | WF score | 负验证折 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 旧实盘五档 `factor_ranked_current_weights_zero_news` | 1682.30% | -34.75% | 1.511 | 1.240 | 222 | 69 | -0.1891 | 2 |
| v1 `calibrated_v1_controlled_live` | 3686.62% | -41.87% | 1.407 | 1.245 | 245 | 46 | -1.1032 | 2 |
| v2 `calibrated_v2_loss_aware` | 2876.74% | -36.53% | 1.458 | 1.227 | 216 | 75 | -1.1967 | 3 |
| v2.1 `calibrated_v21_profit_loss` | 2587.55% | -36.07% | 1.445 | 1.208 | 210 | 81 | -0.6906 | 2 |

相对旧实盘五档的升降仓统计：

| 方案 | 盈利单升仓 | 盈利单降仓 | 亏损单升仓 | 亏损单降仓 |
|---|---:|---:|---:|---:|
| v1 | 83 | 0 | 119 | 0 |
| v2 | 43 | 11 | 50 | 14 |
| v2.1 | 38 | 11 | 39 | 14 |

## 结论

1. v2.1 比 v2 更符合“减少亏损单升仓”的目标：亏损单升仓从 `50` 降到 `39`。
2. v2.1 的收益低于 v2，但仍高于旧保守五档。
3. v2.1 最大回撤 `-36.07%`，略高于旧保守五档 `-34.75%`，低于 v2 `-36.53%`。
4. v2.1 walk-forward `-0.6906`，好于 v1/v2，但仍弱于旧保守五档。
5. v2.1 没有达到“系统性亏损单降仓”的最终目标，因为亏损单升仓 `39` 仍高于亏损单降仓 `14`。

## 决策建议

- 大资金无人值守：继续旧实盘五档。
- 小资金灰度：v2.1 比 v2 更适合作为下一候选，因为它牺牲部分收益换来更低亏损单升仓和更好 walk-forward。
- v1 不建议实盘，因为亏损单升仓过多。
- v2.1 若要进入云端，只能先进入 shadow ledger 或小资金灰度，并保留旧五档回滚。

## 验证

```powershell
python -m pytest tests\test_ai_tier_weight_research.py tests\test_risk.py -q
python -m compileall ai_quant_trader\risk\sizing.py tests\test_risk.py scripts\ai_tier_weight_research.py tests\test_ai_tier_weight_research.py
python scripts\ai_tier_weight_research.py --output data\research\ai_tier_weight_research_eth_2022_2026_v21.json
```


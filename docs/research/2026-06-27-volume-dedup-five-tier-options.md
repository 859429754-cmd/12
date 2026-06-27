# 2026-06-27 VOL 去重与五档候选方案对比

以后以这个版本为准，忽略之前关于“VOL 在策略和 AI 五档里重复出现所以必须直接删除”的方案。

本轮只做离线研究，不修改云端实盘 RiskManager，不修改趋势策略参数，不修改 TradingView 对齐回测合同。

## 问题

当前趋势策略本身已经使用成交量过滤：

- `ai_quant_trader/strategy/trend.py::TrendStrategy.evaluate`
- `volume_ok = volume > vma * cfg.volume_multiple`
- `signal_strength` 也包含 `volume_multiple`

因此，如果 AI 五档模型再把 `volume_score` 当成独立大权重，会有重复计权风险。

源码核对后结论：

- 实盘 RiskManager 的 `ai_quant_trader/risk/manager.py::_decision_score` 只传入 `technical_signal_score`、订单流、新闻、形态、密集区、震荡安全、高周期等因子。
- `ai_quant_trader/risk/sizing.py::FACTOR_RANKED_SCORE_WEIGHTS` 没有直接的 `volume_score`。
- 重复计权主要存在于离线研究脚本 `scripts/ai_tier_weight_research.py::balanced_entry_score`，其中 `technical_signal_score` 已包含成交量影响，又额外使用 `volume_score`。

## 本轮新增候选

### `balanced_volume_dedup_v1`

保留技术强度，但移除额外 `volume_score`。

目标：减少 VOL 重复计权，同时保持趋势突破强度、形态、密集区、高周期确认。

### `structure_context_v1`

进一步弱化策略内技术强度，AI 主要看结构和上下文确认：

- 突破强度
- 趋势确认
- 密集区突破
- 形态确认
- 高周期对齐
- 震荡安全
- regime 风险安全

目标：把策略本身当成入场门槛，让 AI 更像质量过滤器，而不是重复评价策略内因子。

## 回测对比

命令：

```powershell
python scripts\ai_tier_weight_research.py --output data\research\ai_tier_weight_research_eth_2022_2026_volume_dedup.json
```

样本：

- ETH 1h
- 291 笔闭合交易研究样本
- 不调用 DeepSeek
- 不做历史新闻 hindsight 优化

| 方案 | 收益 | 最大回撤 | PF | Sharpe | 交易数 | 阻断 | WF score | 负验证折 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `factor_ranked_current_weights_zero_news` 旧实盘五档 | 1682.30% | -34.75% | 1.511 | 1.240 | 222 | 69 | -0.1891 | 2 |
| `balanced_candidate_v1` 旧平衡候选 | 2340.99% | -49.85% | 1.305 | 1.121 | 288 | 3 | -1.0140 | 2 |
| `balanced_volume_dedup_v1` VOL 去重候选 | 1369.68% | -45.20% | 1.438 | 1.036 | 272 | 19 | -0.3345 | 3 |
| `structure_context_v1` 结构上下文候选 | 1755.89% | -51.59% | 1.485 | 1.086 | 264 | 27 | -0.3721 | 3 |
| `calibrated_v1_controlled_live` 旧进攻候选 | 3686.62% | -41.87% | 1.407 | 1.245 | 245 | 46 | -1.1032 | 2 |
| `calibrated_v2_loss_aware` 旧减亏候选 | 2876.74% | -36.53% | 1.458 | 1.227 | 216 | 75 | -1.1967 | 3 |

相对旧实盘五档的升降仓统计：

| 方案 | 盈利单升仓 | 盈利单降仓 | 亏损单升仓 | 亏损单降仓 |
|---|---:|---:|---:|---:|
| `balanced_volume_dedup_v1` | 59 | 7 | 115 | 7 |
| `structure_context_v1` | 63 | 7 | 114 | 8 |
| `calibrated_v1_controlled_live` | 83 | 0 | 119 | 0 |
| `calibrated_v2_loss_aware` | 43 | 11 | 50 | 14 |

## 结论

1. `volume_score` 重复计权的确存在于离线 `balanced_candidate_v1`，但不直接存在于当前实盘 RiskManager 的主权重表。
2. 直接移除额外 VOL 后，`balanced_volume_dedup_v1` 收益和 Sharpe 下降，且负验证折增加到 3。不能直接替代旧五档。
3. `structure_context_v1` 收益略高于旧实盘五档，但回撤扩大到 `-51.59%`，大资金标准不合格。
4. `calibrated_v1_controlled_live` 收益最高，但亏损单升仓 `119`，说明它会放大很多亏损交易，不适合大资金无人值守。
5. `calibrated_v2_loss_aware` 在收益、回撤和亏损单控制之间更平衡，但 walk-forward 仍弱，不能直接上线。
6. 当前最稳健的保守基准仍是 `factor_ranked_current_weights_zero_news`，优点是 PF、Sharpe、回撤最好；缺点是偏保守，可能牺牲盈利单扩张。

## 可选方案

### A. 保守实盘继续版

使用 `factor_ranked_current_weights_zero_news` 作为云端实盘基准。

优点：回撤最低、PF 最高、walk-forward 相对最好。

缺点：偏保守，盈利单放大不足。

### B. 小资金灰度平衡版

使用 `calibrated_v2_loss_aware` 做 shadow/live-small 候选。

优点：收益明显高于旧五档，亏损单升仓大幅少于 v1。

缺点：仍有 50 笔亏损单升仓，walk-forward 不够强。

### C. 只做影子账本的进攻版

保留 `calibrated_v1_controlled_live` 和 `balanced_candidate_v1` 在 shadow ledger 中对比。

优点：收益弹性大。

缺点：亏损单放大和回撤风险明显，不应进入大资金无人值守。

### D. VOL 去重研究版

保留 `balanced_volume_dedup_v1` 和 `structure_context_v1` 做研究，不上线。

优点：逻辑更干净。

缺点：当前样本表现不如预期，不能替代实盘五档。

## 建议

短期选择 A 作为实盘安全基准，同时把 B/C/D 全部写入 shadow ledger，等有足够真实信号后再做闭合交易归因。

如果必须从收益角度前进，只允许小资金灰度 B，不建议直接上 v1 或结构上下文候选。


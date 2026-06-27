# 2026-06-27 AI 因子通道升级研究

以后以本文件为准：AI 因子升级先采用“离线研究 + shadow ledger”路径，不直接替换云端实盘 RiskManager。忽略“把新增因子直接上线实盘并扩大仓位”的方案。

## 背景

当前 AI 决策已经能输出趋势、新闻、BTC、形态、订单流、密集区等结构化字段，但实盘仓位链路主要还是把这些字段合成为一个分数，再通过硬风控 cap。这个结构的问题是：

- 放大盈利的因子和减少亏损的因子会互相抵消。
- 单一分数无法解释“为什么加仓”或“为什么降仓”。
- 历史上 `calibrated_v1_controlled` 提高了收益，但也放大了很多亏损单。
- `calibrated_v2_loss_aware` 改善亏损单升仓问题，但总收益和 walk-forward 仍不够稳。

## 数据和代码来源

- 研究样本：`data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json`
- 订单流 proxy：`data/research/historical_orderflow_proxy_eth_2022_2026.json`
- 样本数量：291 笔 ETH 1h 闭合交易
- 研究脚本：`scripts/ai_tier_weight_research.py`
- 通道函数：`scripts/ai_tier_weight_research.py::factor_channel_scores`
- 通道效果：`scripts/ai_tier_weight_research.py::factor_channel_effects`
- 当前实盘模型：`ai_quant_trader/risk/sizing.py::calibrated_v1_policy`
- 减亏候选：`ai_quant_trader/risk/sizing.py::calibrated_v2_loss_aware_policy`

## 新因子通道

### 1. 扩大利润通道：`profit_expansion`

目标：只在多因子明确支持趋势延续时允许提高仓位。

因子：

| 因子 | 权重 | 作用 |
|---|---:|---|
| `orderflow_confirmation_score` | 0.24 | 市场参与度、流动性、冲击质量 |
| `pattern_confirmation_score` | 0.18 | 形态是否支持策略方向 |
| `dense_zone_breakout_score` | 0.15 | 密集区突破或迁移质量 |
| `trend_confirmation_score` | 0.12 | 趋势结构确认 |
| `htf_alignment_score` | 0.10 | 高周期同向 |
| `technical_signal_score` | 0.09 | 策略信号强度 |
| `volume_score` | 0.07 | 放量质量 |
| `breakout_score` | 0.05 | 突破幅度 |

### 2. 减少亏损通道：`loss_suppression_risk`

目标：识别更容易变成亏损单的入场环境，用于限制升档、降档或阻断。

因子：

| 因子 | 权重 | 作用 |
|---|---:|---|
| `range_risk_score` | 0.22 | 震荡/箱体风险 |
| `orderflow_weakness_score` | 0.20 | 订单流弱确认 |
| `pattern_weakness_score` | 0.14 | 形态弱确认 |
| `dense_weakness_score` | 0.12 | 密集区突破弱 |
| `trend_weakness_score` | 0.10 | 趋势结构弱 |
| `regime_risk_score` | 0.10 | 市场状态风险 |
| `overextension_risk_score` | 0.08 | 突破过度但确认不足 |
| `htf_conflict_risk_score` | 0.04 | 高周期冲突 |

### 3. 执行质量通道：`execution_quality`

目标：避免“方向对但盘口/流动性/突破质量差”的订单放大仓位。

核心因子：订单流质量、成交量、密集区质量、区间安全、形态、突破不过度。

### 4. 背景质量通道：`context_quality`

目标：把新闻方向、BTC 龙头、ETH/BTC 轮动、高周期环境作为上下文加减分，而不是直接生成方向。

注意：历史新闻归档不完整，`news_direction_alignment_score` 只能作为实时因子和 shadow ledger 因子，不能用 hindsight 寻优。

## 291 笔样本通道效果

命令：

```powershell
python scripts\ai_tier_weight_research.py --output data\research\ai_tier_weight_research_eth_2022_2026.json
```

结果：

| 通道 | 期望方向 | 盈利单中位数 | 亏损单中位数 | 期望效果 |
|---|---|---:|---:|---:|
| `execution_quality` | 盈利单更高 | 0.759 | 0.700 | 0.301 |
| `profit_expansion` | 盈利单更高 | 0.717 | 0.683 | 0.291 |
| `loss_suppression_risk` | 亏损单更高 | 0.220 | 0.246 | 0.250 |
| `context_quality` | 盈利单更高 | 0.609 | 0.589 | 0.114 |

## 结论

- 最强有效方向不是单一“趋势强度”，而是执行质量 + 扩盈质量。
- 减亏通道有正向信号，但区分度中等，不能假装它能稳定提前识别所有亏损单。
- 背景质量历史效果弱，主要因为历史新闻、BTC 轮动、实时语义归档不完整；它在实盘里应保留，但必须做 shadow ledger。
- 下一步不应直接替换实盘模型，而应把 `profit_expansion / loss_suppression_risk / execution_quality / context_quality` 写入订单生命周期元数据和 AI 仓位审计，让真实闭合交易证明它是否正优化。

## 下一步设计

生产级路径：

1. 在每次 AI 决策落库时保存四个通道分数。
2. 在 `scripts/ai_position_tier_audit.py` 输出每笔交易的通道分数、真实盈亏、候选档位和实际档位。
3. 实盘至少积累 30 笔闭合交易后再评估是否允许模型升档。
4. 如果通道模型出现“亏损单升仓比例高于旧模型”，立即回滚到 `legacy_factor_ranked` 或保守 cap。

禁止：

- 禁止用 `pnl / mae_pct / mfe_pct` 作为入场前评分因子。
- 禁止把历史缺失新闻当作负面证据。
- 禁止因为全样本收益高就直接替换云端实盘。

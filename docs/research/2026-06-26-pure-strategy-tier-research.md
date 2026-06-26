# 2026-06-26 ETH 1h 纯策略仓位分档研究

以后以本文件记录的研究口径为准：本阶段只优化开仓前一次性 5 档仓位逻辑，不引入持仓后闭 K 复评加仓机制。

## 研究目标

验证当前 5 档仓位逻辑是否过于保守，并寻找更合理的开仓前分档方向。

## 数据口径

- 标的：`ETH/USDT:USDT`
- 周期：`1h`
- 时间：`2022-01-01` 到 `2026-06-26`
- 数据源：Binance 真实公开 K 线
- 样本：291 笔纯策略闭合交易，盈利 110 笔，亏损 181 笔
- 策略配置：读取当前 `config/config.yaml` 的 `strategy.trend`
- 成本：taker fee `0.0006`，基础滑点 `2 bps`

本研究只使用当前系统实际使用的数据口径：

- 本地趋势策略技术证据：`signal_strength`、`breakout_atr`、`volume_multiple`、`atr_pct`
- 本地形态库：`PatternDetector`
- VPVR 密集区：`DenseZoneAnalyzer`
- regime 结构：`RegimePatternAnalyzer`
- 4h 高周期结构：`higher_timeframe_context`

未使用项：

- 不调用 DeepSeek。
- 不使用历史新闻，因为当前没有完整可审计历史新闻归档。
- 不使用历史订单流，因为当前没有完整可审计历史订单流归档。
- 不使用开仓后的走势反推开仓仓位。

## 代码来源

- 研究脚本：`scripts/pure_strategy_tier_research.py`
- 回归测试：`tests/test_pure_strategy_tier_research.py`
- 本地实验输出：`data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.md`
- 本版本已删除 EMA89：策略代码、优化参数、图表层、DeepSeek 离线证据均不再使用 EMA89。

## 结果摘要

纯策略基准：

- 总收益率：`2463.45%`
- 最大回撤：`-76.16%`
- 胜率：`37.80%`
- Profit factor：`1.294`

反事实分档结果：

| 分档方案 | 收益率 | 最大回撤 | 交易数 | 说明 |
|---|---:|---:|---:|---|
| `structural_conservative_proxy` | `1913.91%` | `-46.29%` | `208` | 更防守，明显降低回撤，也牺牲收益 |
| `balanced_candidate_v1` | `2340.99%` | `-49.85%` | `288` | 接近纯策略收益，明显低于纯策略回撤 |
| `aggressive_candidate_v1` | `3417.63%` | `-57.89%` | `291` | 收益更高，但回撤仍大，不可直接上线 |

盈利/亏损订单中差异较明显的入场前特征：

- `volume_multiple`：盈利单中位数 `4.078`，亏损单中位数 `3.879`
- `pattern_aligned_score`：盈利单中位数 `0.931`，亏损单中位数 `0.928`
- `entry_quality_score`：盈利单中位数 `0.784`，亏损单中位数 `0.767`
- `regime_trend_score`：盈利单中位数 `0.716`，亏损单中位数 `0.662`
- `regime_range_score`：盈利单中位数 `0.072`，亏损单中位数 `0.078`

## 因子归类与有效性初判

| 因子组 | 当前结论 | 主要证据 | 处理建议 |
|---|---|---|---|
| 本地策略触发因子 | 弱有效候选 | `volume_multiple` effect `0.189`，`atr_pct` effect `0.137`，`breakout_atr` effect `0.092` | 保留为方向入口；AI 不能绕过 |
| 形态确认因子 | 弱有效候选 | `pattern_aligned_score` effect `0.182` | 可参与放仓，但不能单独决定满仓 |
| 密集区与突破质量 | 弱有效候选 | `dense_strength` effect `-0.163`，`dense_range_score` effect `-0.086` | 更适合做假突破/震荡风险约束 |
| 趋势/震荡状态 | 弱有效候选 | `regime_trend_score` effect `0.129`，`regime_range_score` effect `-0.126` | 适合决定 `weak/normal/strong`，不能机械满仓 |
| 4h 高周期结构 | 当前样本区分力低 | `htf_trend_strength` effect `0.054`，`htf_alignment_score` effect `0.048` | 只作为辅助因子，不应一票放大 |
| 入场质量综合分 | 弱有效候选 | `entry_quality_score` effect `0.163` | 适合作为 5 档初始分，但必须 walk-forward |
| 新闻与订单流实时因子 | 当前不能离线证明 | 缺完整历史新闻和订单流归档 | 实盘可用，但必须建立归档后再做统计检验 |

## 结论

1. 当前偏保守的判断成立，但不能直接改成激进版。
2. `balanced_candidate_v1` 是当前更合理的候选方向：收益接近纯策略基准，同时把最大回撤从 `-76.16%` 降到 `-49.85%`。
3. `aggressive_candidate_v1` 不建议直接上线。它收益高于基准，但回撤仍达到 `-57.89%`，且全样本优化有过拟合风险。
4. 缺失新闻和订单流历史归档时，不能把缺失证据当成负面证据；只能作为置信度折扣。
5. 下一步必须做 walk-forward，把 2022-2024 与 2024-2026 分开验证；不能把全样本最优结果直接上线。

## 候选优化方向

生产级更合理的 5 档方向：

- `block`：只用于硬冲突和灾难风险，例如无本地信号、方向冲突、数据过期、failed breakout、交易所状态不可验证。
- `weak`：有效信号但结构弱、震荡/假突破风险高、4h 明显反向、证据不足。
- `normal`：技术信号有效，趋势结构中等，新闻/订单流未知但不冲突。
- `strong`：技术、量能、形态、密集区、4h 至少 3 项确认，无硬风险。
- `full`：至少 4 项确认，且无高新闻风险、无订单流冲突、无密集区失败突破。

这不是最终上线规则。最终上线前还必须通过：

- walk-forward 样本外验证
- 分年份收益和回撤稳定性检查
- 当前实盘订单影子账本对照
- 云端小仓灰度，不直接大资金切换

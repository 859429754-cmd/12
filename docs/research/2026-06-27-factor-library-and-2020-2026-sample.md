# 2026-06-27 因子库重组与 2020-2026 扩样本检查

以后以本文件为准：因子库先按“可回测性、实盘可归档性、未来函数风险、扩盈/减亏角色”重组；不能把所有市面因子直接塞进模型寻优。忽略“因子越多越好、直接全量寻优上线”的方案。

## 结论先行

1. 扩样本到 2020-01-01 至 2026-06-26 是必要的，已用 Binance 真实 1h K 线生成 398 笔 ETH 趋势策略交易样本。
2. 单因子区分盈利/亏损的能力不强，不能承诺精准识别亏损单或盈利单。
3. 最有价值的方向是组合因子：执行质量、放量质量、形态确认、市场状态、密集区结构。
4. 新闻、BTC 龙头、ETH/BTC 轮动、实时盘口、资金费率、OI、爆仓、链上数据等应进入因子库，但在没有连续历史归档前不能参与历史寻优。
5. 因子寻优必须分两层：`backtestable_now` 离线寻优，`live_only_needs_archive` 只做实盘 shadow ledger。

## 因子库注册表

新增模块：

- `ai_quant_trader/research/factors.py`
- `tests/test_factor_library.py`

注册表把因子分成：

- `BACKTESTABLE_NOW`：当前 2020-2026 K 线样本可回测。
- `BACKTESTABLE_WITH_BACKFILL`：需要历史回填，例如 Binance aggTrades。
- `LIVE_ONLY_NEEDS_ARCHIVE`：实盘可用，但必须先归档，不能 hindsight 寻优。
- `NEEDS_NEW_DATA_SOURCE`：需要新增数据源，例如 OI、funding、爆仓、链上。
- `FORBIDDEN_OUTCOME_LEAKAGE`：禁止进入开仓前模型，例如 `pnl / mae_pct / mfe_pct`。

因子角色：

- `PROFIT_EXPANSION`：用于放大盈利概率更高的信号。
- `LOSS_SUPPRESSION`：用于降档、阻断、减少亏损暴露。
- `EXECUTION_QUALITY`：用于判断能不能以合理滑点和流动性执行。
- `CONTEXT_QUALITY`：用于新闻、BTC 龙头、ETH/BTC 轮动、高周期背景。
- `HARD_RISK_GATE`：只负责硬风控，不给收益加分。
- `RESEARCH_LABEL_ONLY`：只用于事后标签和审计。

## 当前覆盖到的主要因子族

| 因子族 | 当前状态 | 是否可进入历史寻优 |
|---|---|---|
| 策略触发因子 | 已有 | 可以 |
| KDJ / KC / VOL 技术因子 | 已有 | 可以 |
| ATR / 波动率 / 止损距离 | 已有 | 可以 |
| 成交量与放量质量 | 已有 | 可以 |
| 形态库 | 已有本地模型 | 可以 |
| 密集区 / VPVR | 已有本地模型 | 可以 |
| 趋势/震荡状态 | 已有本地模型 | 可以 |
| 4h 高周期结构 | 已有本地模型 | 可以 |
| Binance aggTrades 订单流 | 可回填但慢 | 回填完整后可以 |
| 实时盘口深度 / spread | 实盘可用 | 先归档，暂不寻优 |
| BTC 龙头 / ETH-BTC 轮动 | 实盘可用 | 先归档，暂不寻优 |
| 新闻方向 / 新闻风险 | 实盘可用 | 先归档，暂不寻优 |
| Funding / OI / basis | 需新增数据源 | 暂不寻优 |
| 爆仓数据 | 需新增数据源 | 暂不寻优 |
| 链上流入流出 / 稳定币流动性 | 需新增数据源 | 暂不寻优 |
| MAE / MFE / PnL | 已有结果字段 | 禁止作为入场因子 |

## 2020-2026 扩样本结果

命令：

```powershell
python scripts\pure_strategy_tier_research.py --start 2020-01-01 --end 2026-06-26 --source binance --output data\research\pure_strategy_tier_research_eth_2020_2026_no_ema.json
```

结果：

- 数据源：`binance`
- K 线数量：56832 根 1h K 线
- 交易数：398
- 盈利单：148
- 亏损单：250
- 纯策略基准收益：`750.29%`

2020-2026 样本下数值因子效果排序：

| 因子 | 盈利单中位数 | 亏损单中位数 | effect |
|---|---:|---:|---:|
| `dense_strength` | 0.6281 | 0.6419 | -0.203 |
| `volume_multiple` | 3.9074 | 3.7990 | 0.198 |
| `entry_quality_score` | 0.7814 | 0.7580 | 0.143 |
| `regime_trend_score` | 0.7083 | 0.6561 | 0.139 |
| `pattern_aligned_score` | 0.9307 | 0.9306 | 0.132 |
| `regime_range_score` | 0.0720 | 0.0785 | -0.128 |
| `regime_risk_score` | 0.0000 | 0.0000 | 0.105 |
| `dense_range_score` | 0.1800 | 0.1828 | -0.096 |
| `atr_pct` | 0.0105 | 0.0100 | 0.096 |
| `breakout_atr` | 0.4209 | 0.3679 | 0.081 |
| `signal_strength` | 0.8496 | 0.8372 | 0.071 |
| `dense_trend_score` | 0.7200 | 0.7200 | 0.067 |

因子组效果：

| 因子组 | 判断 | max effect | avg effect |
|---|---|---:|---:|
| `core_strategy_trigger` | weak_candidate | 0.198 | 0.111 |
| `pattern_structure` | weak_candidate | 0.132 | 0.068 |
| `dense_zone_breakout` | weak_candidate | 0.203 | 0.122 |
| `regime_filter` | weak_candidate | 0.139 | 0.124 |
| `higher_timeframe` | low_discrimination | 0.021 | 0.013 |
| `entry_quality_composite` | weak_candidate | 0.143 | 0.143 |
| `live_news_orderflow` | live_only | 0.000 | 0.000 |

## 反事实仓位结果

2020-2026 下三个旧候选表现：

| 候选 | return | max DD | trades | PF |
|---|---:|---:|---:|---:|
| `structural_conservative_proxy` | 1961.18% | -62.84% | 284 | 1.374 |
| `balanced_candidate_v1` | 1510.16% | -82.23% | 394 | 1.277 |
| `aggressive_candidate_v1` | 1309.94% | -87.59% | 397 | 1.292 |

含义：

- 扩样本后，平衡版和激进版回撤过深，不适合直接上线。
- 2020-2021 的行情会显著改变 2022-2026 的结论。
- 之前 2022-2026 上看起来更优的仓位逻辑，必须重新经过 2020-2026 walk-forward。

## 订单流回填状态

尝试使用新样本回填 2020-2026 aggTrades：

```powershell
python scripts\historical_orderflow_backfill.py --input data\research\pure_strategy_tier_research_eth_2020_2026_no_ema.json --output data\research\historical_orderflow_proxy_eth_2020_2026_probe.json --windows 60,240 --max-features 0 --progress-every 100 --checkpoint-every 50
```

结果：本地命令 300 秒超时，已产生部分 checkpoint：

- 已处理 feature windows：150
- `60m` usable：43，missing：107
- `240m` usable：43，missing：107

结论：订单流历史因子不能立刻参与 2020-2026 总寻优。需要单独长任务下载并校验 Binance aggTrades archive，否则会把缺失订单流误判为中性或负面。

## 下一步

1. 用 `ai_quant_trader/research/factors.py` 作为唯一因子注册表。
2. 只对 `BACKTESTABLE_NOW` 因子跑 2020-2026 第一轮组合寻优。
3. 对 `BACKTESTABLE_WITH_BACKFILL` 因子先做完整回填和覆盖率报告。
4. 对 `LIVE_ONLY_NEEDS_ARCHIVE` 因子新增实盘归档字段，至少 30 笔闭合交易后再评估。
5. 对 `NEEDS_NEW_DATA_SOURCE` 因子按优先级接数据源：funding/OI、盘口 spread/depth、BTC/ETH 轮动、新闻方向归档。
6. 任何组合必须通过 walk-forward、年份分段、交易数、回撤、PF、Sharpe、过拟合旗标后才允许进入 shadow ledger。

## 明确禁止

- 禁止用 `pnl / mae_pct / mfe_pct` 训练或调仓。
- 禁止把新闻历史缺失当成负面因子。
- 禁止在没有历史归档的情况下把实时因子拿去 hindsight 寻优。
- 禁止因为全样本收益高就直接替换云端实盘。

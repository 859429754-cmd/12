# 旧保守五档 2020-2026 全订单流补齐审计

以后以本报告为准：旧保守五档在 2020-2026 样本上的表现，必须使用 `historical_orderflow_proxy_eth_2020_2026_full.json` 的补齐版订单流；忽略之前基于 62.8% 订单流覆盖率的临时结论。

## 数据与代码来源

- 纯策略样本：`data/research/pure_strategy_tier_research_eth_2020_2026_no_ema.json`
- 订单流补齐输出：`data/research/historical_orderflow_proxy_eth_2020_2026_full.json`
- 订单流回填脚本：`scripts/historical_orderflow_backfill.py`
- 五档研究脚本：`scripts/ai_tier_weight_research.py`
- 旧保守五档函数：`scripts/ai_tier_weight_research.py::current_factor_policy`
- 缺失订单流中性处理：`scripts/ai_tier_weight_research.py::usable_orderflow_row`

本次没有调用 DeepSeek，没有修改云端实盘参数，没有修改 TradingView 对齐策略合同。

## 订单流覆盖

命令：

```powershell
python scripts\historical_orderflow_backfill.py --input data\research\pure_strategy_tier_research_eth_2020_2026_no_ema.json --output data\research\historical_orderflow_proxy_eth_2020_2026_full.json --symbol ETHUSDT --windows 60,240 --download --progress-every 5 --checkpoint-every 5 --min-usable-ratio 0.95 --strict-coverage
```

结果：

- 样本交易：`398`
- 60m 订单流：usable `385 / 398`，missing `13`，usable_ratio `0.967337`
- 240m 订单流：usable `390 / 398`，missing `8`，usable_ratio `0.979899`
- coverage_verdict：`ok`
- research_gate：`eligible`

剩余缺口不伪造；缺失或空订单流窗口按中性 `orderflow_confirmation_score=0.5`、`orderflow_direction_score=0.0` 处理。

## 旧保守五档表现

正式研究命令：

```powershell
python scripts\ai_tier_weight_research.py --features data\research\pure_strategy_tier_research_eth_2020_2026_no_ema.json --orderflow data\research\historical_orderflow_proxy_eth_2020_2026_full.json --output data\research\ai_tier_weight_research_eth_2020_2026_full_orderflow.json --split 2024-01-01 --top 12
```

| 模型 | 收益率 | 最大回撤 | PF | Sharpe | 成交 | 阻断 |
|---|---:|---:|---:|---:|---:|---:|
| 纯策略满仓基准 | 750.29% | -92.91% | 1.231 | 0.775 | 398 | 0 |
| 固定标准仓 50% | 643.19% | -68.80% | 1.370 | 0.775 | 398 | 0 |
| 旧保守五档 | 1061.07% | -39.36% | 1.402 | 0.885 | 300 | 98 |

旧保守五档 tier 分布：

- `block`: 98
- `weak`: 107
- `normal`: 110
- `strong`: 83
- `full`: 0

按盈亏拆分：

- 盈利单：`block 30` / `weak 37` / `normal 49` / `strong 32`
- 亏损单：`block 68` / `weak 70` / `normal 61` / `strong 51`

## 升仓与降仓表现

相对纯策略满仓基准：

- 盈利单降仓：`148`
- 亏损单降仓：`250`
- 盈利暴露减少：`-14.4965`
- 亏损暴露减少：`+11.6925`
- 总暴露变化：`-2.8039`

解释：旧保守五档没有 `full` 档，所有交易都低于满仓，所以相对满仓基准不存在升仓，只是减风险。

相对固定标准仓 50%：

- same：`110`
- 盈利单升仓：`32`
- 盈利单降仓：`67`
- 亏损单升仓：`51`
- 亏损单降仓：`138`
- 盈利端变化：`-2.4335`
- 亏损端变化：`+2.7573`
- 总变化：`+0.3238`

解释：相对标准仓，旧保守五档确实能净减少亏损暴露；但它并不能稳定识别盈利单并升仓，因为亏损单升仓 `51` 仍高于盈利单升仓 `32`。

## 年度表现

| 年份 | 收益率 | 最大回撤 | PF | 成交 | 阻断 |
|---|---:|---:|---:|---:|---:|
| 2020 | -7.49% | -19.16% | 0.822 | 25 | 37 |
| 2021 | -10.20% | -26.32% | 0.868 | 34 | 10 |
| 2022 | 111.63% | -18.39% | 1.600 | 46 | 16 |
| 2023 | -10.35% | -18.22% | 0.895 | 53 | 18 |
| 2024 | 50.75% | -23.22% | 1.504 | 52 | 9 |
| 2025 | 593.02% | -24.31% | 3.018 | 62 | 4 |
| 2026 | -29.48% | -37.45% | 0.638 | 28 | 4 |

## 同期候选对比

| 模型 | 收益率 | 最大回撤 | PF | Sharpe | WF score | 负折数 |
|---|---:|---:|---:|---:|---:|---:|
| 旧保守五档 | 1061.07% | -39.36% | 1.402 | 0.885 | -1.0162 | 3 |
| balanced_candidate_v1 | 1510.16% | -82.23% | 1.277 | 0.853 | -0.4345 | 2 |
| calibrated_v2_loss_aware | 2386.37% | -39.69% | 1.404 | 1.028 | -0.4974 | 2 |
| calibrated_v21_profit_loss | 1912.77% | -40.91% | 1.373 | 0.984 | -0.3674 | 2 |

## 结论

1. 订单流补齐后，旧保守五档不再是“只降收益”的结果；它相对满仓基准同时提高收益、显著降低回撤。
2. 旧保守五档的优势是防守和稳定：最大回撤从纯策略 `-92.91%` 降到 `-39.36%`，PF 从 `1.231` 提高到 `1.402`。
3. 旧保守五档仍不是理想的“放大盈利、减少亏损”模型：相对标准仓，它升仓了 `32` 笔盈利单，但也升仓了 `51` 笔亏损单。
4. `calibrated_v2_loss_aware` 在补齐订单流后比旧保守五档更像进攻候选：收益 `2386.37%`，回撤 `-39.69%`，PF `1.404`，但仍需要 shadow ledger 或小资金灰度，不应直接大资金替换。
5. 当前可以把补齐后的订单流作为 2020-2026 五档研究基线；剩余缺失窗口必须继续中性处理，不能因缺数据降仓或升仓。

## 验证

- `python -m pytest tests\test_ai_tier_weight_research.py tests\test_strategy_parameter_tier_grid_research.py tests\test_historical_orderflow_backfill.py -q` -> `29 passed`
- `python -m compileall scripts\ai_tier_weight_research.py scripts\strategy_parameter_tier_grid_research.py tests\test_ai_tier_weight_research.py tests\test_strategy_parameter_tier_grid_research.py` -> passed

# 旧 DeepSeek 五档与当前五档对照

以后以本报告为准：截图里的旧 DeepSeek 五档结果属于 `raw_advice_5level_guarded` 旧实验，参数为 `VOL=2.0`、样本 `2022-01-01` 到 `2026-05-21`、交易 `313` 笔。它不能直接和当前 `VOL=2.5`、`2022-01-01` 到 `2026-06-26`、交易 `291` 笔的实盘策略样本硬比。

本次没有调用 DeepSeek，没有修改云端实盘参数，没有修改 TradingView 对齐策略合同。

## 数据来源

- 旧 DeepSeek 实验：`data/optimization/deepseek_overlay_eval_eth_flash_thinking_4h_313.json`
- 当前 2022-2026 样本：`data/research/pure_strategy_tier_research_eth_2022_2026_no_ema.json`
- 当前 2022-2026 重跑输出：`data/research/legacy_comparison_current_2022_2026.json`
- 当前 2020-2026 扩样本：`data/research/pure_strategy_tier_research_eth_2020_2026_no_ema.json`
- 当前 2020-2026 重跑输出：`data/research/legacy_comparison_current_2020_2026.json`
- 当前 2020-2026 订单流：`data/research/historical_orderflow_proxy_eth_2020_2026_full.json`

## 旧 DeepSeek 313 样本

参数：

- 时间：`2022-01-01` 到 `2026-05-21`
- 数据源：`binance`
- 初始资金：`200`
- 杠杆：`4x`
- `position_fraction`: `1.0`
- `KC`: `20, 2.8, ATR14`
- `VOL`: `2.0`
- `KDJ`: `9,3,3`
- `ATR止损`: `1.5`
- 手续费：`0.0006`
- 滑点：`2.0 bps`

| 方案 | 交易数 | 收益率 | 最大回撤 | PF | 备注 |
|---|---:|---:|---:|---:|---|
| 旧纯策略 | 313 | 1276.21% | -83.02% | 1.289 | 旧截图基准 |
| 旧 DeepSeek 4档 | 313 | 3946.74% | -59.55% | 1.564 | `raw_advice_4level_guarded` |
| 旧 DeepSeek 5档 | 313 | 4898.28% | -58.51% | 1.619 | `raw_advice_5level_guarded` |

旧 DeepSeek 5档相对旧纯策略：

- 收益率提高 `+3622.07` 个百分点。
- 最大回撤改善 `24.51` 个百分点。
- PF 从 `1.289` 提高到 `1.619`。
- 阻断盈利单 `6` 笔，阻断亏损单 `10` 笔。
- 降仓盈利单 `69` 笔，降仓亏损单 `138` 笔。

结论：旧 DeepSeek 5档在旧样本里确实有效，不是截图误读。

## 当前 2022-2026 样本

参数：

- 时间：`2022-01-01` 到 `2026-06-26`
- 数据源：`binance`
- 初始资金：`10000`
- 杠杆：`4x`
- `position_fraction`: `1.0`
- `KC`: `20, 2.8, ATR14`
- `VOL`: `2.5`
- `KDJ`: `9,3,3`
- `ATR止损`: `1.5`
- 手续费：`0.0006`
- 滑点：`2.0 bps`

| 方案 | 交易/执行 | 阻断 | 收益率 | 最大回撤 | PF | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| 当前纯策略 | 291 | 0 | 2463.45% | -76.16% | 1.294 | - |
| 当前旧保守五档 | 222 | 69 | 1682.30% | -34.75% | 1.511 | 1.240 |
| `calibrated_v2_loss_aware` | 216 | 75 | 2876.74% | -36.53% | 1.458 | 1.227 |
| `calibrated_v21_profit_loss` | 210 | 81 | 2587.55% | -36.07% | 1.445 | 1.208 |

解释：

- 当前旧保守五档显著降低回撤和提高 PF，但牺牲收益。
- `calibrated_v2_loss_aware` 在当前 291 样本中同时超过纯策略收益，并把回撤从 `-76.16%` 降到 `-36.53%`。
- `calibrated_v21_profit_loss` 比 v2 更保守，收益低于 v2，但仍高于纯策略，回撤略优于 v2。

## 当前 2020-2026 扩样本

参数与当前实盘口径一致，扩展到 `2020-01-01` 到 `2026-06-26`，交易 `398` 笔，并使用补齐后的历史订单流。

| 方案 | 交易/执行 | 阻断 | 收益率 | 最大回撤 | PF | Sharpe |
|---|---:|---:|---:|---:|---:|---:|
| 当前纯策略 | 398 | 0 | 750.29% | -92.91% | 1.231 | - |
| 当前旧保守五档 | 300 | 98 | 1061.07% | -39.36% | 1.402 | 0.885 |
| `calibrated_v2_loss_aware` | 281 | 117 | 2386.37% | -39.69% | 1.404 | 1.028 |
| `calibrated_v21_profit_loss` | 274 | 124 | 1912.77% | -40.91% | 1.373 | 0.984 |

解释：

- 扩样本下，当前旧保守五档相对纯策略同时提高收益、降低回撤。
- `calibrated_v2_loss_aware` 在扩样本里收益最高，回撤接近旧保守五档，但它仍需要 shadow ledger 或小资金灰度验证。
- v2.1 更克制，回撤略差于 v2，收益低于 v2，但比旧保守五档高。

## 为什么旧结果和当前结果差很多

1. 旧实验是 `VOL=2.0`，当前实盘样本是 `VOL=2.5`。交易入口已经变了。
2. 旧实验截止到 `2026-05-21`，当前样本截止到 `2026-06-26`，并且后续还扩到 `2020-2026`。
3. 旧实验交易数 `313`，当前 2022-2026 样本交易数 `291`，当前扩样本交易数 `398`。
4. 旧 DeepSeek 五档是逐笔 DeepSeek 审查结果 replay；当前五档多数是离线因子代理模型，不是同一个 AI 决策源。
5. 初始资金不同，旧实验 `200`，当前研究 `10000`。最终权益不能直接比，只能看收益率、回撤、PF、Sharpe。
6. 当前回测和研究口径持续加严，包括订单流覆盖、缺失订单流中性处理、成本、滑点、同 K 悲观止损等。

## 专业结论

旧 DeepSeek 5档不是错的，但它证明的是：在旧 `VOL=2.0` 的 313 笔样本上，DeepSeek 原始建议映射确实显著优于旧纯策略。

当前结果也不是错的，它回答的是另一个问题：在当前 `VOL=2.5` 实盘策略和更严格样本下，哪种规则化五档更稳。

因此不能用旧截图直接否定当前研究，也不能用当前结果直接否定旧 DeepSeek。真正要判断“旧 DeepSeek 5档 vs 当前五档谁更好”，下一步必须做两件事：

1. 用当前代码复刻旧 `VOL=2.0 / 2022-01-01 到 2026-05-21 / 313` 样本，并在同一 ledger 上跑当前五档代理。
2. 在当前 `VOL=2.5 / 291` 样本上建立新的真实 DeepSeek shadow ledger，而不是用离线因子代理代替 DeepSeek。

## 本次运行命令

```powershell
python scripts\ai_tier_weight_research.py --features data\research\pure_strategy_tier_research_eth_2022_2026_no_ema.json --orderflow data\research\historical_orderflow_proxy_eth_2022_2026.json --output data\research\legacy_comparison_current_2022_2026.json --split 2024-01-01 --top 12

python scripts\ai_tier_weight_research.py --features data\research\pure_strategy_tier_research_eth_2020_2026_no_ema.json --orderflow data\research\historical_orderflow_proxy_eth_2020_2026_full.json --output data\research\legacy_comparison_current_2020_2026.json --split 2024-01-01 --top 12
```


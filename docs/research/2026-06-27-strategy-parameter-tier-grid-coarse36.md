# ETH 1h 策略参数 x AI 五档组合研究

以后以本文为本轮参数研究记录准绳：本研究只做离线实验，不调用 DeepSeek，不修改云端实盘参数。

## 研究目标

- 同时搜索趋势策略核心参数和 AI 五档仓位逻辑。
- 检查 AI 五档是否能在收益、回撤、rolling 稳定性上超过纯策略。
- 将过拟合排查作为硬过滤，不采用全样本收益最高但分段不稳的候选。

## 数据与口径
- 时间: `2022-01-01` 到 `2026-06-26`
- 数据源: `binance` / 实际 `binance`
- 初始权益: `10000.0`
- 杠杆: `4.0`
- 手续费: `0.0006`
- 滑点: `2.0 bps`
- 搜索组合数: `36`

## 搜索参数

```text
kc_length: 18 / 20 / 22
kc_scalar: 2.6 / 2.8 / 3.0
volume_multiple: 2.3 / 2.5 / 2.7
atr_stop_multiple: 1.2 / 1.5 / 1.8
固定: vma_length=20, atr_length=14, KDJ(9,3,3), position_fraction=当前配置
```

## 过拟合排查规则

- 训练/验证收益差距过大 -> `train_validation_gap_large`
- 验证集收益非正 -> `validation_return_non_positive`
- rolling 负收益窗口超过 2 个 -> `too_many_negative_walk_forward_folds`
- rolling 最差验证回撤深于 -50% -> `walk_forward_drawdown_too_deep`
- 邻近参数表现明显断崖 -> `neighborhood_instability`
- 订单流 proxy 覆盖率过低 -> `orderflow_proxy_coverage_low`

## Top 候选

1. `rolling_walk_forward_candidate` params `{'kc_length': 24, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1193.10%`, DD `-41.74%`, WF `-0.9308`, negative_folds `2`, beats_pure `True`, flags `[]`
2. `current_conservative` params `{'kc_length': 24, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.0, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1159.08%`, DD `-40.14%`, WF `-1.3418`, negative_folds `2`, beats_pure `True`, flags `[]`
3. `current_conservative` params `{'kc_length': 24, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `833.20%`, DD `-35.96%`, WF `-0.8808`, negative_folds `2`, beats_pure `True`, flags `[]`
4. `barbell_aggressive_guarded_v1` params `{'kc_length': 24, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `913.90%`, DD `-49.28%`, WF `-1.0479`, negative_folds `2`, beats_pure `True`, flags `[]`
5. `rolling_walk_forward_candidate` params `{'kc_length': 20, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `3545.03%`, DD `-44.24%`, WF `-1.4001`, negative_folds `2`, beats_pure `True`, flags `['neighborhood_instability']`
6. `current_conservative` params `{'kc_length': 20, 'kc_scalar': 2.2, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1512.01%`, DD `-28.55%`, WF `0.2465`, negative_folds `1`, beats_pure `True`, flags `['orderflow_proxy_coverage_low']`
7. `current_conservative` params `{'kc_length': 20, 'kc_scalar': 2.2, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.0, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1748.26%`, DD `-32.68%`, WF `-0.9820`, negative_folds `1`, beats_pure `True`, flags `['orderflow_proxy_coverage_low']`
8. `current_conservative` params `{'kc_length': 20, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.0, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1625.41%`, DD `-35.64%`, WF `-0.5139`, negative_folds `2`, beats_pure `True`, flags `['neighborhood_instability']`
9. `current_conservative` params `{'kc_length': 24, 'kc_scalar': 2.2, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1033.23%`, DD `-31.86%`, WF `0.4423`, negative_folds `1`, beats_pure `True`, flags `['orderflow_proxy_coverage_low']`
10. `current_conservative` params `{'kc_length': 24, 'kc_scalar': 2.2, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.0, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1083.73%`, DD `-37.24%`, WF `-0.2061`, negative_folds `1`, beats_pure `True`, flags `['orderflow_proxy_coverage_low']`
11. `single_split_high_return_candidate` params `{'kc_length': 24, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `1325.84%`, DD `-47.05%`, WF `-1.5436`, negative_folds `3`, beats_pure `True`, flags `['too_many_negative_walk_forward_folds']`
12. `single_split_high_return_candidate` params `{'kc_length': 20, 'kc_scalar': 2.2, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}` return `961.85%`, DD `-46.20%`, WF `-0.4802`, negative_folds `1`, beats_pure `True`, flags `['orderflow_proxy_coverage_low']`

## 结论
- 本轮最干净候选为 rolling_walk_forward_candidate + {'kc_length': 24, 'kc_scalar': 2.8, 'vma_length': 20, 'atr_length': 14, 'volume_multiple': 2.5, 'atr_stop_multiple': 1.5, 'momentum_filter': 'kdj', 'kdj_length': 9, 'kdj_k_smooth': 3, 'kdj_d_smooth': 3, 'position_fraction': 1.0, 'variant': 'with_volume', 'use_volume_filter': True}；仍需 shadow ledger 复核后才可灰度。
- 如果没有 beats_pure 且无过拟合旗标的候选，本轮不能给出替换实盘参数建议。
- 历史新闻方向归档不完整，新闻方向分仍只允许做实时确认和风险 cap，不能在本研究中 hindsight 寻优。
- 订单流 proxy 覆盖率会影响高订单流权重策略的可信度；覆盖不足时不能把结果视为完整实盘模拟。
- 任何候选最多进入 shadow ledger / 小仓灰度，不允许直接大资金切换。

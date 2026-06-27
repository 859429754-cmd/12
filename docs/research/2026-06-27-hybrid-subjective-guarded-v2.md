# 2026-06-27 hybrid_subjective_guarded_v2 融合仓位模型

以后以本文件和 `docs/current-operating-contract.md` 为准：当前小资金实盘测试允许使用 `hybrid_subjective_guarded_v2`。忽略之前“直接回退旧 DeepSeek 原始五档”以及“当前实盘只允许 calibrated_v1_controlled”的方案。

## 背景

旧 DeepSeek 原始五档在旧样本上表现强，但它直接依赖模型主观判断，实盘漂移和可审计性风险较高。

`calibrated_v2_loss_aware` 比 v1 更重视减亏，能减少明显弱订单流、弱结构和高尾部风险场景的升档，但它仍偏机械，可能错过 DeepSeek 对整体市场背景的主观判断。

## 决策

采用融合模型：

```text
策略信号
  -> DeepSeek 单次结构化分析
     -> 结构化因子分数
     -> subjective_position_tier 主观五档提案
  -> calibrated_v2_loss_aware 生成基准档位
  -> hybrid_subjective_guarded_v2 融合
  -> RiskManager 硬风控和账户上限裁剪
  -> 订单生命周期审计
```

## 规则

- v2 基准为 `block` 时，主观提案不能复活开仓。
- 主观提案低于 v2 基准时，允许更快降档。
- 主观提案高于 v2 基准时，最多只允许上调一档。
- 上调必须满足订单流、形态、密集区、趋势、新闻方向、BTC 风向标和数据质量条件。
- 后置共识逻辑不能再把 hybrid 的降档或限升结果重新拉满仓。
- 所有硬风控仍优先：无本地策略信号、AI 方向冲突、新闻冲突、订单流冲突、同向已有持仓、readiness 异常、杠杆上限不足均可阻断。

## 审计字段

`RiskDecision` 必须保留：

- `sizing_policy`
- `legacy_position_tier`
- `calibrated_position_tier`
- `calibrated_edge_score`
- `subjective_position_tier`
- `subjective_position_confidence`
- `score_breakdown.loss_risk_score`
- `score_breakdown.hybrid_base_position_tier_index`

## 回滚

保守回滚：

```powershell
python scripts/ai_sizing_policy_control.py --policy legacy_factor_ranked
```

退回 v2，不使用主观融合：

```powershell
python scripts/ai_sizing_policy_control.py --policy calibrated_v2_loss_aware
```

退回上一版 v1：

```powershell
python scripts/ai_sizing_policy_control.py --policy calibrated_v1_controlled --max-tier-lift 1 --min-factor-coverage 0.7
```

## 风险

本模型不是盈利承诺。它只是在工程上把“旧 DeepSeek 主观判断”和“当前可审计因子模型”融合起来。是否真正提高盈利因子、减少亏损单升仓，必须继续通过真实成交后的 AI 仓位分档效果审计验证。

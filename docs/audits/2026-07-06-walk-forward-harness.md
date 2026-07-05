# 2026-07-06 Walk-forward 对照 harness 审计记录

以后以本版本为准：walk-forward 只能用于离线研究和提案审计，不能自动修改云端实盘策略参数、五档规则或 AI 大脑逻辑。

## 本轮新增

- `ai_quant_trader/research/walk_forward.py`
  - 输入冻结历史交易样本，不逐 K 调用 DeepSeek。
  - 输出 baseline trend、AI veto、AI reduce、AI strict consensus 和可选 candidate 的 train / validation / out-of-sample 对照。
  - 统计 return、max drawdown、win rate、profit factor、trade count、cost ratio、MAE、parameter stability。
  - 候选如果只改善训练集、验证/OOS 交易数不足、PF 不达标、回撤恶化或参数稳定性差，会自动 `rejected`。
- `scripts/walk_forward_harness.py`
  - 从本地 JSON 信号账本读取 trades/windows，生成 JSON 报告。
  - 不读取 `.env.runtime`，不联网，不调用 DeepSeek。
- `tests/test_walk_forward_harness.py`
  - 覆盖统计口径、过拟合拒绝、验证/OOS 通过、CLI 报告输出。

## 重要口径

- `signal_count` 是历史信号数量。
- `trade_count` 是经过 overlay 后实际执行的交易数量。
- 胜率、盈利因子、cost ratio 按实际执行交易计算。
- 被 veto 的信号进入 `blocked_count`，不能污染盈利因子分母。

## 验证

```powershell
python -m pytest tests\test_walk_forward_harness.py tests\test_ai_tier_weight_research.py tests\test_console_api.py::test_walk_forward_proposal_is_needs_review_without_auto_apply tests\test_console_api.py::test_walk_forward_proposal_rejects_weak_validation_and_endpoint_filters -q
# 26 passed
```

## 未改变

- 未改变 ETH 1h 趋势策略参数。
- 未改变 TradingView 对齐合同。
- 未改变云端实盘 AI sizing policy。
- 未改变账号权限、实盘授权或 follower 账户逻辑。

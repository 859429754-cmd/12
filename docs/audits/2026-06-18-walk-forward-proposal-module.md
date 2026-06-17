# 2026-06-18 Walk-forward 提案模块审计记录

## 结论

本轮只实现“自动学习提案”可视化和审计链，不实现自动上线参数。

以后以本版本为准：参数寻优结果必须先生成 `walk_forward_parameter_proposal`，由控制台展示验证集、基准、参数差异和拒绝原因；忽略之前“寻优结果可直接作为实盘优化依据”的方案。

## 生产边界

- `status=needs_review`：只代表候选值得人工复核，不代表可直接实盘。
- `status=rejected`：验证集收益、交易数、PF、回撤或候选警告未通过。
- `changes={}`：不会进入现有 `pending` 审批链，不会被 `approve_proposal` 直接应用。
- `auto_apply=false`：禁止自动热更新实盘参数。

## 验收规则

- 验证集收益必须超过当前基准。
- 验证集交易数必须达到 `min_trades`。
- 验证集盈利因子必须不低于阈值。
- 验证集回撤不得比基准恶化超过 20%。
- 候选警告必须进入 `acceptance.risks`。

## 实现位置

- `ai_quant_trader/api/server.py::_record_walk_forward_proposal`
- `ai_quant_trader/api/server.py::_walk_forward_acceptance`
- `GET /api/walk-forward/proposals`
- `console/src/App.tsx::WalkForwardProposalPanel`

## 已跑测试

```powershell
python -m pytest tests\test_console_api.py::test_walk_forward_proposal_is_needs_review_without_auto_apply tests\test_console_api.py::test_walk_forward_proposal_rejects_weak_validation_and_endpoint_filters tests\test_risk.py tests\test_order_lifecycle.py::test_order_lifecycle_marks_exchange_not_found_terminal tests\test_gateway_runtime.py::test_trading_app_close_closes_follower_execution -q
cd console && npm.cmd run build
```

结果：25 个 Python 定向测试通过；前端 build 通过，仍有既有 Vite chunk 体积警告。

# 2026-06-15 实盘持仓五档映射显示审计

## 当前目标

修复“交易所已有 ETH 实盘持仓，但控制台五档映射没有显示具体档位，且底部建议仍像无开仓信号”的误导性展示。

以后以本版为准，忽略上一版把已有持仓统一显示为 `持仓中 / 已执行` 的方案。已有实盘持仓必须映射到具体五档：`弱仓 / 标准仓 / 强仓 / 满仓`，如果最新记录没有入场档位，则明确标记为“估算”。

## 根因

`console/src/App.tsx` 旧展示链路把“已有持仓”当成伪档位 `position`：

- 顶部指标显示 `持仓中 / 已执行`，没有落到真实五档。
- `AiSizingTierStrip` 和 `AiSizingRail` 在 `position` 模式下故意不高亮任何五档。
- `DecisionNarrative` 只读取最新 AI 审计记录，未结合当前 Gate 实盘持仓，所以会继续显示“当前技术未触发开仓信号”等文案。

这个逻辑适合避免误显示为 `阻断 / 0%`，但仍不满足实盘控制台对“当前仓位属于哪个档位”的可视化要求。

## 修复

- `decisionSizingForPosition` 改为持仓感知五档映射：
  - 优先使用后端 `risk.position_tier / position_scale`。
  - 如果最新记录没有入场档位，则优先按当前名义仓位 / 账户权益 / 杠杆上限映射实际持仓档位。
  - 如果账户权益或名义仓位缺失，再按最新 AI `confidence` 或五分制分数保守映射为持仓观察档。
  - 有持仓但缺少可追溯档位时，最低显示为 `弱仓 / 25% · 估算`，不再显示伪档位。
- `AiSizingTierStrip` 和 `AiSizingRail` 在持仓模式下继续高亮具体五档，而不是隐藏五档高亮。
- `DecisionNarrative` 和右侧 AI 摘要改为持仓感知说明：
  - 已有持仓时，说明“最新 AI 记录表示观察、不追仓或不加仓，并不否认既有持仓”。
  - 不再把“没有新开仓信号”解释成“当前仓位无效”。

## 已改文件

- `console/src/App.tsx`
- `docs/audits/2026-06-15-live-position-sizing-display.md`

## 验证

- `cd console && npm.cmd run build` 通过。
- `python -m pytest tests/test_console_api.py -q` 通过，结果为 `40 passed`。
- Playwright 真实页面验收通过：
  - 模拟已有 ETH 多仓 `0.23`。
  - 模拟最新 AI 记录为 `hold/no_signal`。
  - 页面包含 `当前持仓映射档`、`弱仓`、`25%`、`当前已有 Gate 实盘`。
  - 页面不包含 `当前 持仓中 / 已执行`、`当前 阻断 / 0%`、`不展示可开仓仓位`。
- `python -m compileall ai_quant_trader tests scripts` 通过。
- `python -m pytest -q` 通过，结果为 `221 passed`。
- `python scripts/public_repo_preflight.py` 通过，`ok=true` 且无敏感发现。

## 未决风险

- 如果后端历史订单没有持久化“入场时五档”，控制台会优先按实际仓位占用比例估算。这是展示降级，不影响实际仓位、下单或风控。
- 后续更强方案是把“入场时 RiskManager 档位”写入订单生命周期/持仓快照，由前端优先展示真实入场档，而不是估算档。

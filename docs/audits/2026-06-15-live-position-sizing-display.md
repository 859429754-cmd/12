# 2026-06-15 实盘持仓五档映射显示审计

## 当前目标

修复“交易所已有 ETH 实盘持仓，但控制台 AI 五档映射仍显示阻断 / 0%”的误导性显示。

## 根因

`console/src/App.tsx` 的 `DecisionSummary` 和 `AiSizingRail` 只根据最新 AI 观察记录计算仓位档。

当最新 AI 记录是 `hold` / `wait` / 不加仓观察时，旧逻辑会把它展示为“阻断 / 0%”。这适用于“下一次新开仓信号”，但不适用于“当前已经存在的实盘持仓”。

## 修复

- 新增持仓感知仓位展示逻辑。
- 当 Gate 实盘持仓存在时，首页和右侧栏显示“持仓中 / 已执行”。
- 五档条继续保留，但只作为下一次策略入场信号的仓位裁剪说明。
- 最新 AI 记录为观察或不加仓时，不再把既有实盘持仓显示成阻断。

## 验证

- `npm.cmd run build` 通过。
- `python -m compileall ai_quant_trader tests scripts` 通过。
- `python -m pytest -q` 通过，结果为 `221 passed`。
- `python scripts/public_repo_preflight.py` 通过，`ok=true` 且无敏感发现。
- 云端 `ai-quant-console.service` 与 `ai-quant-trader.service` 均为 `active`。
- 账户1 ETH 多仓 `0.23`，控制台显示“持仓中 / 已执行”，未出现“当前 阻断 / 0%”。
- 账户2 ETH 多仓 `0.3`，控制台显示“持仓中 / 已执行”，未出现“当前 阻断 / 0%”。

## 未决风险

- Gate 只读余额/持仓偶发超时，控制台会使用最近一次成功快照；这是安全降级，不等于空仓。
- OKX 公共订单流偶发采集异常已被捕获并降级，但会影响订单流确认质量，后续应单独治理。
- `handoff-pfr4qo5n.md` 尾部仍有历史乱码段落。本轮没有继续向该文件追加，避免扩大编码污染。

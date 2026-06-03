# AI 量化系统当前运行合同

更新时间：2026-06-04

本文件记录当前必须遵守的策略、AI、账户、控制台安全和部署合同。以后以本文件、`CONTEXT.md`、ADR-0005 和代码测试为准，忽略旧对话里关于 Trade PIN、操作验证码、EMA 前置过滤、`range` 账户兼容 `follower` 的方案。

## 1. 当前实盘策略

当前生产主策略是 ETH 1h 趋势突破：

- Keltner 中轨：EMA20
- Keltner 宽度：ATR14 * 2.8
- 成交量过滤：Volume > SMA20 * 2.5
- KDJ：9,3,3
- KDJ 多头过滤：K > D 且 J >= 50
- KDJ 空头过滤：K < D 且 J <= 50
- EMA89：不作为当前实盘开仓过滤器
- ATR 固定止损：1.5 * ATR14
- 出场：反向穿越 Keltner 中轨
- 同向已有仓位：禁止重复加仓
- 反手：先平旧方向，再开新方向

不得静默改变这套策略定义。任何替代参数或新指标组合必须走研究、回测、样本外验证和明确 ADR。

## 2. AI 大脑边界

DeepSeek 不发明交易方向。

执行链路：

```text
本地趋势策略信号
  -> 本地形态 / 密集区 / 订单流 / 新闻上下文
  -> DeepSeek 结构化 JSON 评估一次
  -> RiskManager 五档仓位裁剪
  -> Gateway 执行账户订单
  -> SQLite + JSONL 审计
```

五档仓位：

```text
block  = 0%
weak   = 25%
normal = 50%
strong = 75%
full   = 100%
```

AI 只能确认、降仓、阻断。AI 输出无效、超时、预算耗尽或 JSON 校验失败时，实盘新开仓必须保守降级或阻断。

## 3. 多账户执行模型

当前采用“一次策略信号 + 一次 DeepSeek 决策 + 多账户独立裁剪”：

- `trend`: 账户1，趋势策略主账户。
- `follower`: 账户2，跟随账户。复用账户1的策略信号和 AI 决策，不重复调用 DeepSeek。
- `range`: 震荡策略预留账户。当前不可执行，未来接入震荡策略后再启用。

账户2不得独立生成方向，不得绕过账户1的 AI/RiskManager 结论。每个账户仍按自己的余额、杠杆上限、持仓、挂单、最小下单量、API 状态和订单生命周期独立裁剪。

## 4. 控制台账号权限

当前控制台安全以账号登录/RBAC 为准，取代旧 Trade PIN 和操作验证码。

- `admin`: 可切换模拟/实盘、更新 API 密钥、修改策略参数、审批提案、授权/暂停标的、执行危险手动控制。
- `account1`: 只能查看趋势账户，并修改趋势账户杠杆上限。
- `account2`: 只能查看跟随账户，并修改跟随账户杠杆上限。
- `range`: 只能查看震荡预留账户，并修改该账户杠杆上限。

普通账户不能修改策略参数，不能更新 API 密钥，不能切换实盘/模拟，不能手动平仓，不能审批参数提案。

生产默认 fail-closed：除非显式设置 `CONSOLE_AUTH_DISABLED=1`，否则控制台必须要求登录；若未配置任何账号，特权 API 必须返回 `console_auth_not_configured`，不能退回本地管理员。`CONSOLE_AUTH_DISABLED=1` 只能用于本地开发或内网临时调试，不能用于公网或实盘云端。

## 5. 密钥与日志

- `.env.runtime` 不得提交。
- API Key、Secret、Webhook、DeepSeek Key 不得打印、写入普通日志、进入 SQLite 明文或显示在浏览器。
- 控制台 API 密钥更新只允许管理员操作，服务端只保存指纹和尾号审计。
- 公开仓库推送前必须运行 `python scripts/public_repo_preflight.py`。

## 6. 验证命令

每次重大修改后至少运行：

```powershell
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
cd console
npm.cmd run build
cd ..
python scripts/public_repo_preflight.py
```

如果交易逻辑、账户权限、AI 决策、Gateway 或云端部署发生变化，必须同步更新本文件、`CONTEXT.md`、相关 ADR 和 handoff。

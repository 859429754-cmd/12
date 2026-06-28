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
- EMA89：已从策略代码、优化参数、图表层和 AI 证据中删除
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

AI 不能发明方向；在本地策略已经触发后，AI 可以确认、升档、维持、降仓或阻断。AI 输出无效、超时、预算耗尽或 JSON 校验失败时，实盘新开仓必须保守降级或阻断。

## 2026-06-26 因子排序五档仓位合同

以后以本节为准，忽略之前把订单流同向直接等同于强方向确认、或把所有 AI 五分项近似等权处理的方案。

当前五档分数按 2022-2026 ETH 1h 纯策略、历史订单流 proxy 研究和实时消息面执行约束重排权重：

```text
orderflow_confirmation_score  20%
technical_signal_score        18%
news_direction_alignment_score 14%
pattern_confirmation_score    12%
range_safety_score            11%
trend_confirmation_score      10%
dense_zone_breakout_score      8%
news_safety_score              4%
btc_leader_score               2%
eth_btc_rotation_score         1%
```

解释：

- 订单流得分表示市场参与度、流动性、冲击质量和大单活跃度，不是简单 CVD 方向。
- 订单流同向不能单独触发满仓；满仓仍必须同时满足形态确认、密集区突破质量、低震荡风险、足够置信度和 RiskManager 硬风控。
- `news_direction_alignment_score` 是新闻/市场背景相对本地策略方向的确认分：做空+利空、做多+利多可以加分；中性、未知或冲突不得加分。
- `news_safety_score = 1 - news_risk_score` 只代表执行风险安全度，权重较低，但 `news_risk_score` 仍保留强 cap：高波动、滑点、流动性、监管或交易所风险可以降仓或阻断。
- 新闻当前缺完整 2022-2026 可审计历史归档，因此新闻方向确认采用实时上下文权重，但不得绕过本地策略信号、订单流、密集区和硬风控。
- 高周期/BTC 风向标只参与限仓、缩放和解释，不得生成方向。
- 当前研究是全样本结果，仍需 walk-forward / 样本外验证后才能进一步放松档位。

以后以本版本为准，忽略之前“AI 只能降仓或否决，不能升档”的方案。AI 不能发明交易方向，但在本地策略已经触发 `LONG/SHORT` 后，可以在 RiskManager 硬风控、账户杠杆上限、readiness、订单流、密集区、形态、新闻和 BTC/ETH 风向标约束内，把仓位档位动态上调、维持、下调或阻断。

仓位百分比默认按账户当前风险上限解释，而不是只按策略原始建议数量解释。策略层仍会生成 `strategy_baseline_notional`，RiskManager 会生成 `ai_desired_notional` 和 `sizing_basis`，用于审计 AI 是放大、维持还是降仓。若策略建议数量为 0，AI 不允许凭空生成开仓数量。

## 2026-06-27 受控校准仓位模型上线合同

以后以本节为准：小资金测试阶段允许 `hybrid_subjective_guarded_v2` 成为实盘主用仓位模型。忽略之前“当前实盘只使用 `calibrated_v1_controlled`”以及“直接让旧 DeepSeek 主观五档接管最终仓位”的方案。

当前仓位模型：

- `legacy_factor_ranked`：旧版因子排序五档，作为回滚基线和对照组长期保留。
- `calibrated_v1_controlled`：上一版小资金校准模型。保留用于对照和回滚研究。
- `calibrated_v2_loss_aware`：v2 减亏基准模型。它在 v1 基础上要求订单流、形态、密集区和趋势结构确认，降低明显亏损风险场景的升档概率。
- `hybrid_subjective_guarded_v2`：当前主用模型。以 `calibrated_v2_loss_aware` 为基准，额外接收 DeepSeek 的 `subjective_position_tier` 主观五档提案；主观提案可以更快降档，升档最多只能比 v2 基准高一档，并且必须通过订单流、形态、密集区、趋势、新闻和 BTC 风向标约束。

上线保护：

- `hybrid_subjective_guarded_v2` 不允许从 v2 的 `block` 复活为开仓。
- `hybrid_subjective_guarded_v2` 最多只允许在 v2 基准上调一档；如果 DeepSeek 主观五档高出两档以上，只记录提案，不直接照单执行。
- `hybrid_subjective_guarded_v2` 可以比 v2 基准更低，遇到尾部风险、数据缺失、弱订单流、弱形态时必须降档或回退。
- 结构化核心因子覆盖率低于 `calibrated_min_factor_coverage` 时自动回退旧五档。
- RiskManager 原有硬阻断、新闻冲突、极端风险、BTC 冲突、订单流弱、密集区弱、同向持仓禁止加仓、readiness、账户杠杆上限继续优先。
- 订单生命周期元数据必须记录 `risk_sizing_policy`、`legacy_position_tier`、`calibrated_position_tier`、`calibrated_edge_score`、`subjective_position_tier` 和 `subjective_position_confidence`，用于事后审计。

回滚命令：

```powershell
python scripts/ai_sizing_policy_control.py --policy legacy_factor_ranked
```

上线命令：

```powershell
python scripts/ai_sizing_policy_control.py --policy hybrid_subjective_guarded_v2 --max-tier-lift 1 --min-factor-coverage 0.7
```

本合同不是对新模型收益的承诺。新模型仍需通过真实交易后的 AI 仓位分档效果审计，验证是否真正减少亏损、保留盈利、提高盈利因子和夏普率。

## 2026-06-28 持仓闭 K 实盘复评合同

以后以本节为准：当前小资金实盘测试允许开启“持仓 1h 闭 K 复评 live_addon”。忽略之前“复评模块只能 shadow-only”的方案，也继续忽略“开仓时直接允许 DeepSeek 裸 +2 档”以及“盈利后无条件加仓”的方案。

复评模块定位：

- 首仓仍由本地趋势策略信号、DeepSeek 主观五档、`calibrated_v2_loss_aware` 和 `hybrid_subjective_guarded_v2` 决定。
- 持仓复评是持仓管理层，只在已经存在 Gate 持仓和本地 `state_trend.json` 趋势状态时运行。
- `shadow` 只写入 `position_reviews` 审计表和 readiness，不提交加仓订单。
- `live_addon` 可以提交一次加仓订单，但必须走订单生命周期、幂等 `client_order_id` 和净仓级原生止损。
- Gate.io 同向加仓是合仓净持仓，不是独立子仓。以后以净仓级止损为准：加仓后必须取消本系统管理的旧主仓止损和遗留加仓止损，并重新挂一张覆盖当前 Gate 真实净仓数量的 reduce-only 原生止损。忽略之前“加仓数量单独挂一张止损”的方案。
- 复评不替代 KC 中轨退出，不取消 ATR 固定止损口径，不放宽原趋势失效止损价。

加仓候选硬条件：

```text
1. 只在 1h 已收盘 K 线后评估。
2. 持仓浮盈已经被验证：>= 0.5R 或 >= 0.5 ATR。
3. 多单仍在 KC 中轨上方；空单仍在 KC 中轨下方。
4. Gate/readiness 数据状态允许开新仓。
5. Gate 原生止损必须存在并可验证。
6. 订单流、形态、密集区三项中至少两项继续同向。
7. 新闻不得与策略方向冲突，新闻风险不得超过阈值。
8. BTC 风向标不得构成高影响反向冲突。
```

当前云端建议配置：

```yaml
risk:
  position_review:
    enabled: true
    mode: live_addon
    max_additions_per_position: 1
    max_add_fraction: 0.25
```

`mode=live_addon` 仍然不是大资金无人值守配置。它只允许当前小资金实盘灰度使用，且必须满足：

- 同一趋势持仓最多加仓一次。
- 新增加仓数量必须立刻提交独立 reduce-only 原生止损。
- 如果加仓订单或加仓止损状态未知，readiness 必须阻断新开仓并要求人工检查 Gate 官方端。
- 账户2如启用跟随，只在账户2已有同向持仓且有可验证 follower 止损状态时按同等比例跟随加仓。

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

大资金无人值守模式还必须显式设置 `CONSOLE_PASSWORD_STRENGTH_CONFIRMED=1`。该变量只允许在所有控制台账号密码已经轮换为强密码、唯一密码后设置；否则 live readiness 的 `console_auth` 检查必须保持 `block`。临时弱密码可以用于联调登录，但不能作为无人值守绿灯。

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

## 7. 新闻背景层当前规则（2026-06-18）

以后以本节为准，忽略之前“策略信号触发后只读取最近 1 小时新闻给 DeepSeek”的方案。

当前新闻输入分为两层：

- `market_background`：事件级重大市场背景，保留 24-72 小时衰减窗口，包含方向、严重度、风险分、置信度、影响资产和事件摘要。
- `news`：实时新闻窗口，通常是最近约 1 小时的快讯。

DeepSeek 必须先理解 `market_background`，再判断 `news` 是否强化、削弱或冲突当前策略信号。消息面方向规则：

- 做空 + 利空背景 = `aligned`
- 做多 + 利多背景 = `aligned`
- 做空 + 利多背景 = `conflict`
- 做多 + 利空背景 = `conflict`
- 中立或方向不足 = `neutral/unknown`

重大新闻同向不等于自动满仓；仍必须经过订单流、密集区、流动性、风控上限和五档仓位映射。新闻背景层只提供事实和风险上下文，不允许发明交易方向，不允许绕过本地策略信号和 RiskManager。

相关实现与审计：

- `ai_quant_trader/data/news_context.py`
- `ai_quant_trader/core/models.py::NewsEvent`
- `ai_quant_trader/core/models.py::MarketBackgroundSnapshot`
- `docs/audits/2026-06-18-production-news-background.md`
## 2026-06-18 AI 新闻与 BTC 风向标决策合同

以后以本合同为准，忽略旧方案中“重大新闻风险本身近似等于禁止开仓”的粗口径。

1. 策略方向来源不变：ETH 1h 本地趋势策略产生 `LONG/SHORT/EXIT/HOLD`，AI 只确认、降仓或否决。
2. 新闻方向和新闻风险分离：
   - `news_alignment`：新闻方向是否支持本地策略方向。
   - `news_risk_score`：事件执行风险、波动风险、滑点风险、流动性风险。
3. AI 必须输出或被本地归一化为这些字段：
   - `crypto_market_impact_score`
   - `btc_leader_alignment`
   - `btc_leader_impact_score`
   - `symbol_news_impact_score`
   - `pattern_confirmation_score`
4. BTC 作为 ETH 风向标：BTC 同向可以提高确认度；BTC 强冲突只能限仓/降仓，不能让 AI 自己生成反向交易。
5. 形态确认用于仓位缩放：强形态支持仓位，弱形态限制仓位；形态不能绕过本地策略信号。
6. RiskManager 后置限仓不可被“高共识满仓提升”覆盖。范围风险、极端新闻风险、BTC 强冲突、形态弱、订单流弱等风险 cap 必须在最终档位上生效。

## 2026-06-18 BTC/ETH 轮动风向标合同

以后以本节为准，忽略之前“BTC 同向/反向作为 ETH 唯一风向标”的简化方案。

BTC 仍是 ETH 重要风向标，但不能只看绝对涨跌。系统必须同时判断：

- BTC 1h / 4h / 24h 变化。
- ETH 相对 BTC 的 1h / 4h 强弱。
- `btc_leader_regime`：`leader_uptrend`、`rotation_lag`、`leader_pullback`、`distribution_risk`、`leader_downtrend`、`unknown`。
- `eth_btc_rotation_score`：ETH 相对 BTC 补涨或轮动质量。

规则：

- ETH 没有本地 1h 策略信号时，BTC/ETH 轮动不得触发开仓。
- ETH 多头信号 + BTC 轻微回踩或震荡 + ETH 相对 BTC 明显走强，可识别为 `rotation_lag` 或 `leader_pullback`，不应被当成 BTC 强冲突。
- ETH 多头信号 + BTC 4h/24h 明确破位或分配风险，仍必须限仓；不能用“ETH 补涨”解释系统性风险。
- BTC/ETH 轮动只参与仓位缩放和风险解释，不允许 AI 发明方向，不允许绕过 RiskManager。

## 2026-06-18 Walk-forward 自动学习提案合同

以后以本节为准，忽略之前“参数寻优结果可以直接作为实盘优化依据”的粗口径。

当前规则：

- 参数寻优完成后只生成 `walk_forward_parameter_proposal` 审计提案，不自动修改实盘参数。
- 提案状态只能是 `needs_review` 或 `rejected`，不会进入现有 `pending` 审批链，避免误点审批后直接热更新实盘参数。
- 进入 `needs_review` 至少要求：验证集收益超过当前基准、验证集交易数达到 `min_trades`、验证集盈利因子过线、验证集回撤没有比基准恶化超过 20%。
- 任一条件失败必须写入 `acceptance.risks`，并在控制台 walk-forward 模块直接展示。
- 即使提案通过，也只能进入人工复核、小仓验证和后续单独参数提案流程，不得绕过 TradingView 对齐回测合同和 RiskManager。

相关实现：

- `ai_quant_trader/api/server.py::_record_walk_forward_proposal`
- `ai_quant_trader/api/server.py::_walk_forward_acceptance`
- `console/src/App.tsx::WalkForwardProposalPanel`
- `tests/test_console_api.py::test_walk_forward_proposal_is_needs_review_without_auto_apply`

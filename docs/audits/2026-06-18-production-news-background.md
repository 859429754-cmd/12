# 2026-06-18 生产级新闻背景层审计

## 结论

以后以本版本为准，忽略之前“策略信号发生后只读取最近 1 小时新闻交给 DeepSeek”的方案。

新闻输入必须分成两层：

1. **市场背景层**：过去 24-72 小时仍在衰减窗口内的重大事件，包含方向、严重度、风险分、置信度和影响资产。
2. **实时新闻层**：最近约 1 小时的新快讯，用来判断实时催化，但必须放在市场背景层下解释。

DeepSeek 不负责创造新闻事实，只能基于本地新闻事件、市场背景快照、订单流、密集区、形态和本地策略信号做确认、降仓或否决。

## 当前实现

- 事件级模型：`NewsEvent`
- 背景快照模型：`MarketBackgroundSnapshot`
- 深模块：`MarketNewsContextBuilder`
- 事件落库：`news_events`
- 背景快照落库：`market_background_snapshots`
- DeepSeek 顶层输入：`market_background`

相关文件：

- `ai_quant_trader/core/models.py`
- `ai_quant_trader/data/news_context.py`
- `ai_quant_trader/app.py`
- `ai_quant_trader/brain/deepseek.py`
- `ai_quant_trader/storage/sqlite.py`
- `tests/test_news_context.py`

## 交易语义

本次改动不改变生产策略：

- ETH 1h 趋势策略仍由 KC + VOL + KDJ 触发。
- EMA89 仍只作为证据和图表信息，不作为当前实盘入场过滤。
- AI 不能发明方向。
- 账户 2 仍只跟随账户 1 的同一策略信号和同一 AI/RiskManager 决策。

消息面方向判断规则：

- 做空 + 利空背景 = `aligned`
- 做多 + 利多背景 = `aligned`
- 做空 + 利多背景 = `conflict`
- 做多 + 利空背景 = `conflict`
- 中立或方向不足 = `neutral/unknown`

重大新闻同向不自动满仓；仍必须经过订单流、密集区、流动性、风控上限和五档仓位映射。

## 成本治理影响

市场背景层是确定性本地构建，不调用 DeepSeek。它的作用是：

- 减少每次 AI 调用重新读长文本摘要的成本。
- 提高 DeepSeek context cache 命中概率。
- 避免开仓时只看最近 1 小时新闻而遗漏前一两天的重要宏观背景。

DeepSeek payload 现在拆成：

- `stable_contract`
- `dynamic_context`
- `market_background`
- `news`

其中 `market_background` 会被压缩为有限数量的事件，避免重复塞进 `news` 字段造成 token 浪费。

## 风险边界

这个模块是确定性新闻背景标签，不是收益预测器。

仍然不能承诺：

- 长期盈利
- 消息面方向永远判断正确
- 免费新闻源永远完整
- 极端行情下完全无滑点

若新闻源失效，交易循环应降级为缓存新闻或技术/订单流路径，不允许全局崩溃。

## 验证

新增回归测试：

- 高影响利空事件会持久化到 `news_events`
- 后续交易循环即使实时新闻平淡，也会带上仍在衰减窗口内的利空背景
- 利空背景对做空信号返回 `aligned`，对做多信号返回 `conflict`
- DeepSeek payload 中 `market_background` 与 `news` 分层，避免重复背景事件

验证命令：

```powershell
python -m pytest tests\test_news_context.py tests\test_deepseek_order_json.py -q
```

当前结果：

```text
16 passed
```

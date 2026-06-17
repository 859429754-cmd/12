# 2026-06-18 DeepSeek 成本治理与主备路由审计

以后以本版本为准，忽略之前“每次 DeepSeek 调用都先尝试主 Key，失败后再尝试备用 Key”和“普通价格波动也频繁触发 DeepSeek 复评”的旧方案。

## 目标

- 降低 DeepSeek 日常调用和开仓后调用成本。
- 提高 DeepSeek context cache 命中率。
- 主 Key 明确余额不足后，粘性切换到备用 Key，避免每次调用都先失败一次。
- 保留实盘必要 AI 风控：策略信号、已有持仓、高危价格事件、重大新闻仍可触发 DeepSeek。

## 关键规则

- DeepSeek 官方 context caching 依赖重复前缀命中；系统必须让稳定规则、输出 schema、AI 角色和五档仓位合同位于请求前缀。
- DeepSeek 响应中的 `usage.prompt_cache_hit_tokens` 和 `usage.prompt_cache_miss_tokens` 必须落库，不能只看账单截图。
- HTTP 402 / Insufficient Balance 属于额度耗尽：标记该 Key 不可用并粘性切换到另一个 Key。
- HTTP 429 属于限流：短期冷却，不等同于余额耗尽。
- timeout / 5xx 属于临时故障：本次可使用备用 Key，但下一次仍优先主 Key。
- 401 / 403 属于认证问题：标记 Key 无效并触发告警，不得静默继续。

## 本轮代码落点

- `ai_quant_trader/brain/credentials.py`
  - 新增 `DeepSeekCredentialRouter`。
  - 只保存 `primary/backup` 状态，不保存 API Key 明文。
- `ai_quant_trader/brain/deepseek.py`
  - 新增主备 Key 路由。
  - 新增 `ai_call_usage_events` 记录。
  - 新增稳定前缀请求结构：`stable_contract` 在前，`dynamic_context` 在后。
- `ai_quant_trader/app.py`
  - 正式 AI 调用向 DeepSeek 传入 `call_type`。
  - 普通无持仓、无策略信号、非高危价格唤醒不再调用 DeepSeek，只写本地审计。
  - 有持仓、真实策略信号、高危价格事件仍强制允许 DeepSeek 复评。
- `scripts/deepseek_usage_audit.py`
  - 新增按日期、调用类型、DeepSeek Key 标签统计 cache hit/miss token。
- `ai_quant_trader/storage/sqlite.py`
  - 新增 `ai_call_usage_events` 表。

## 验收口径

必须能回答：

- 哪一天 DeepSeek 消耗最高。
- 哪类调用消耗最高。
- 哪个 Key 在消耗。
- cache hit/miss token 比例是多少。
- 主 Key 是否因 402 被粘性切换。
- 临时故障是否没有永久切走主 Key。

## 风险保留

- 本轮只优化调用治理，不改变 ETH 1h 趋势策略信号定义。
- 本轮不保证收益提升，只降低成本、延迟和 AI 预算耗尽风险。
- 本地 `data/trader.sqlite3` 当前没有真实 DeepSeek usage 事件；6 月 15 日费用暴涨的精确归因需要云端数据库或本轮上线后的新增 usage 记录。

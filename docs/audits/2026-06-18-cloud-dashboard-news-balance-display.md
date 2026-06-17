# 2026-06-18 云端首页余额与新闻显示修复审计

以后以这个版本为准，忽略之前“新闻背景层内部标记可以直接出现在首页新闻告警”和“余额短缓存失败后首页显示读取失败即可”的旧展示方案。

## 问题

- 云端首页新闻区把内部审计标记当成用户可见新闻告警展示，例如 `market_background_uses_decayed_events`、`realtime_news_window_attached`、`market_background_attached`。
- 首页余额在 Gate.io 余额接口慢或公网移动端延迟较高时容易显示 `读取失败` / `--`，即使本地已经有最近一次可信余额快照。

## 根因

- `ai_quant_trader/api/server.py::_news_latest_response` 直接返回 `NewsDigest.warnings`，没有区分用户可见错误和内部上下文附加标记。
- `console/src/App.tsx::isInternalNewsText` 只过滤旧的 `daily_news_flash_context_attached` 与 `news_context_48h_attached`，没有覆盖生产新闻背景层新增标记。
- `/api/account/balance` 的 `max_cache_age_seconds` 上限为 120 秒且前端固定请求 12 秒缓存，公网环境中容易频繁打到 Gate.io live balance，并被短超时拖成失败显示。

## 修复

- 后端新增 `_user_facing_news_warnings`，过滤内部新闻背景/窗口附加标记，只保留真实用户可见错误。
- 前端 `isInternalNewsText` 同步过滤 `market_background_*`、`*_context_attached`、`*_window_attached`。
- `/api/account/balance` 展示缓存默认窗口调整为 120 秒，上限调整为 900 秒。
- 前端余额请求使用 120 秒展示缓存，timeout 从 4.5 秒提高到 9 秒。
- 余额失败回退返回 `cache_age_seconds`，方便后续 UI 明确展示快照年龄。

## 风控边界

- 本次缓存只用于控制台展示层。
- 实盘新开仓仍必须通过 readiness、Gate 对账、订单状态机、RiskManager 和账户授权。
- 不允许交易执行路径复用控制台余额展示缓存。

## 验证

- `python -m pytest tests\test_console_api.py::test_account_balance_uses_fresh_cached_snapshot_without_gate_call tests\test_console_api.py::test_account_balance_returns_cached_snapshot_when_gate_is_slow tests\test_console_api.py::test_news_response_hides_internal_context_warnings tests\test_console_api.py::test_news_response_repairs_cached_mojibake -q` -> `4 passed`
- `python -m compileall ai_quant_trader tests scripts` -> passed
- `python -m pytest -q` -> `252 passed`
- `cd console && npm.cmd run build` -> passed
- `python scripts/public_repo_preflight.py` -> `ok=true`

## 未决风险

- 前端主 chunk 约 522KB，仍需后续做代码分包和首屏 API 聚合。
- 如果 Gate.io live balance 长时间失败且没有历史快照，首页仍会显示 `--`，这是正确行为，不能伪装成 0 或模拟余额。

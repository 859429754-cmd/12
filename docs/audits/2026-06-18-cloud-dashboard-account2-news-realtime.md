# 2026-06-18 云端控制台账号2余额与新闻实时性审计

## 结论

以后以本版本为准，忽略之前“账号2余额显示 0 就一定是读取失败或读到账户1”的判断。当前云端只读接口显示：账号2 follower Gate API 返回的是接近 0 的真实 USDT 权益，控制台旧 UI 将极小正数按 2 位小数格式化成 `0`，这是展示层问题，不是交易执行层账户串线。

以后以本版本为准，忽略之前“右侧新闻显示 UTC 字符串即可”的展示方案。新闻接口返回的是 UTC 时间，控制台面向中国用户必须显示北京时间和相对时间，否则最新金十快讯会被误读成旧新闻。

## 证据

- 云端 `/api/account/balance?account_slot=follower&max_cache_age_seconds=0` 返回 live follower 余额约 `0.0000000013 USDT`。
- 云端 `/api/news/latest?limit=8&compact=true&max_age_minutes=15` 返回 `source_status=fresh`，新闻 age 小于 1 分钟。
- `config/config.yaml` 中 `news.jin10_enabled=true`，`NewsCollector` 会优先读取 `flash-api.jin10.com/get_flash_list`。
- 金十公开接口的 `important` 字段此前只被转成 `credibility=0.86`，前端无法明确展示红字/加粗重要快讯。
- 部署后复验发现，采集器保留 `important` 后，`/api/news/latest?compact=true` 的 `_sanitize_news_item` 仍会裁掉该字段；这是第二层 API 展示 bug。

## 修复

- `console/src/App.tsx` 新增金额展示函数：极小非零余额显示为 `<0.01`，避免账号2真实近零余额被误判成固定 0。
- 账号2登录时复用 primary follower balance 请求结果，不再重复请求 follower 余额接口，降低公网加载压力和状态覆盖风险。
- `NewsItem` 新增 `important` 字段，金十 `important` 原样保留。
- `/api/news/latest` 轻量响应保留 `important` 字段，避免前端只能靠 source/credibility 猜测重要性。
- 新闻排序优先考虑 `important`，前端对重要新闻显示红色标记。
- 控制台新闻时间统一显示为北京时间，并附带“几分钟前”相对时间。
- 前端将 `MarketChart` 改为 lazy chunk，首页首屏不再静态加载 `lightweight-charts` 图表模块。

## 未决风险

- 如果用户预期账号2应该有大额资金，但 live API 返回接近 0，应在 Gate 官方后台检查 API 绑定账户、子账户、合约账户资产位置和 USDT 永续账户余额。本系统不能把交易所返回的 0 伪造成有资金。
- 当前已拆出 `MarketChart` chunk，主 JS 从约 `524 kB` 降到约 `348 kB`。后续若仍慢，应继续按 workspace 拆分 AI、回测、执行页，而不是增加接口超时掩盖问题。

## 验证

- `python -m pytest tests/test_news.py tests/test_console_api.py -q` -> `61 passed`
- `cd console && npm.cmd run build` -> passed，主包约 `347.74 kB`，`MarketChart` 单独约 `177.55 kB`。

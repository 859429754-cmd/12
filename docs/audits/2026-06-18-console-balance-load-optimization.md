# 2026-06-18 控制台余额加载与假 0 显示修复审计

## 当前目标

修复 AI 控制台网页端加载慢、账户余额偶发显示为 `0` 的问题。以当前代码与当前对话确定的生产规则为准：控制台展示可以使用最近成功余额快照，但交易执行不得使用展示缓存绕过 readiness、实盘对账和本地风控。

## 根因

1. 前端 `console/src/App.tsx::load` 把主账户余额请求放在大批量 `Promise.all` 中。余额接口慢或失败时，会拖慢整批状态更新。
2. 前端格式化函数 `console/src/ui.tsx::num` 使用 `Number(value)`，而 JavaScript 中 `Number(null) === 0`，导致未知余额被渲染为 `0`。
3. 前端数据提取函数 `console/src/App.tsx::numberValue` 同样把 `null` 转成 `0`，导致账户面板、执行页权益和最大名义价值出现假 0。
4. Gate 余额读取 `ai_quant_trader/execution/gateio.py::GateExecutionClient.fetch_balance_summary` 在 `total.USDT` 缺失时使用 `0.0` 回退，可能把交易所返回结构异常误显示成真实 0。
5. 后端 `/api/account/balance` 每次控制台刷新都优先直连 Gate，没有短 TTL 只读快照；这会放大公网移动端延迟和 Gate API 抖动。

## 修复

1. `ai_quant_trader/api/server.py:/api/account/balance`
   - 新增 `max_cache_age_seconds` 参数，默认 12 秒。
   - live / gate_readonly 场景下，如果存在新鲜 `account_balance_snapshots`，直接返回缓存快照，避免首屏重复等待 Gate。
   - 缓存响应标记 `cached=true`、`stale=false`、`balance_source=cached_live_balance`、`cache_age_seconds`。
   - 该缓存仅用于控制台展示，不改变交易执行余额校验。

2. `ai_quant_trader/execution/gateio.py::fetch_balance_summary`
   - `total.USDT` 缺失时抛出 `gate_balance_missing_usdt_total`。
   - 不再把缺失字段伪装成 `0`。

3. `console/src/App.tsx::load`
   - 主账户与 follower 余额请求脱离主 `Promise.all`。
   - 余额刷新失败时保留上一轮可信快照；无快照时显示读取失败与 `--`。
   - 其他核心区域不再被余额接口慢响应拖住。

4. `console/src/ui.tsx::num` 与 `console/src/App.tsx::numberValue`
   - `null`、`undefined`、空字符串统一显示为 `--`。
   - 只有真实数值 `0` 才显示为 `0`。

5. `console/src/App.tsx::ExecutionWorkspace`
   - 账号1权益、账号2权益、最大名义价值改为可空数值。
   - 未知时显示 `--`，不再显示假 0。

## 回归测试

新增或更新：

- `tests/test_console_api.py::test_account_balance_uses_fresh_cached_snapshot_without_gate_call`
- `tests/test_console_api.py::test_account_balance_returns_cached_snapshot_when_gate_is_slow`
- `tests/test_gateway_runtime.py::test_gate_balance_missing_usdt_total_does_not_return_fake_zero`

## 验证结果

本地验证：

```powershell
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
cd console
npm.cmd run build
python ..\scripts\public_repo_preflight.py
```

结果：

- `python -m pytest -q` -> `225 passed`
- `npm.cmd run build` -> 通过，仍有 Vite 首包大于 500KB 的性能提示
- `python scripts/public_repo_preflight.py` -> `ok=true`，无敏感发现

浏览器验收：

- 启动本地 API 与 Vite。
- 用 Playwright 拦截 `/api/account/balance`，强制延迟 3 秒并返回 `usdt_total=null`。
- 验收结果：
  - 首屏核心内容约 `151ms` 出现。
  - 账户面板显示 `USDT 权益 读取失败 --`。
  - 未出现 `USDT 权益 0` 假余额。

## 残余优化项

1. 前端构建产物主 chunk 约 `519KB`，后续应做路由级分包或将图表/回测/数据页懒加载。
2. 目前余额短缓存是控制台展示优化，不是交易执行优化；实盘开仓仍必须依赖 readiness 与 Gate 对账。
3. 如果公网移动端仍慢，下一步应拆分首页 API：首屏 `/api/dashboard/summary` 聚合核心小字段，历史订单、长列表和图表异步懒加载。

# 2026-06-21 开仓授权自动暂停审计

## 结论

以后以本版本为准，忽略之前“读取 `runtime_state` 整张表最新行即可代表开仓控制状态”的方案。

`runtime_state` 表已经被多个模块复用：开仓授权控制记录、DeepSeek 凭证主备状态记录都写入同一张表。控制台旧逻辑读取整张表最新行，当最新行是 `symbol=deepseek_credentials` 的凭证备份记录时，payload 中没有 `opening_paused` 和 `enabled_symbols` 字段，于是代码默认 `opening_paused=True`、授权标的为空，造成“网站每隔一段时间自动暂停开仓”的误判。

这不是策略主动暂停，也不是管理员误操作，而是运行状态读取口径错误。

## 证据

- 云端 `runtime_state` 最新多条记录中存在大量 `symbol=deepseek_credentials`，payload 原因是 `backup_success`，没有 `opening_paused` 字段。
- 云端最新一条真实控制记录为 `reason=authorize_opening`，`opening_paused=false`，`enabled_symbols=["ETH/USDT:USDT"]`。
- `logs/audit.jsonl` 未发现对应周期性的 `pause_opening` 操作记录。
- 旧代码 `RuntimeControlManager.load_state()` 调用 `SQLiteStore.fetch_latest("runtime_state")`，没有按控制记录过滤。

## 修复

- `RuntimeControlManager.save_state()` 以后写入 `symbol=runtime_control`，将真实开仓控制状态与 DeepSeek 凭证状态分离。
- `RuntimeControlManager.load_state()` 优先读取 `symbol=runtime_control` 的最新记录。
- 为兼容历史旧数据，如果没有 `runtime_control` 行，则回扫最近 500 条 `runtime_state`，只接受包含 `opening_paused`、`enabled_symbols`、`report_symbols` 或 `major_news_only` 的控制记录。
- 新增回归测试：授权开仓后再写入一条 `deepseek_credentials` 备份记录，重新加载状态必须保持 `opening_paused=false` 且 ETH 仍在授权标的中。

## 风控边界

- 本修复只解决“控制台和 API 读取错误状态导致误暂停”的问题。
- 实盘新开仓仍必须通过 runtime 授权、readiness、Gate 对账、RiskManager、订单生命周期和账户权限。
- 如果后续还有其他模块复用 `runtime_state`，必须继续使用独立 `symbol` 或独立表，不能让不同状态域通过“整表最新行”互相覆盖。

## 验证

- `python -m pytest tests\test_control.py::test_runtime_state_ignores_deepseek_credential_rows -q` -> `1 passed`
- `python -m pytest tests\test_control.py -q` -> `5 passed`
- `python -m pytest tests\test_deepseek_order_json.py::test_deepseek_quota_failure_sticks_to_backup_key tests\test_deepseek_order_json.py::test_deepseek_transient_failure_uses_backup_once_but_keeps_primary -q` -> `2 passed`
- `python -m pytest tests\test_console_api.py::test_console_account_rbac_replaces_operation_code_for_mutating_requests -q` -> `1 passed`

# 生产运行 Runbook

本文件是当前 AI 量化交易系统的生产运行约束。以后以本文件和代码/测试为准，忽略早期聊天里关于“公网只靠账号密码”“断连仍允许自动新开仓”的方案。

## 公网访问防护

- 控制台必须启用账号认证，不能公开裸奔。
- 登录失败默认按 `username + client_ip` 计数，达到 `CONSOLE_LOGIN_MAX_FAILURES` 后锁定 `CONSOLE_LOGIN_LOCKOUT_MINUTES`。
- 所有响应附加基础安全头：`X-Frame-Options=DENY`、`X-Content-Type-Options=nosniff`、`Referrer-Policy=no-referrer`。
- 管理操作必须经过账号权限控制；普通账户只能查看和调整允许范围内的杠杆，不得改策略核心参数。
- IP 白名单、HTTPS、WAF/fail2ban 属于云端网络层防护。没有固定管理端公网 IP 和域名前，不得硬启用 IP 白名单，避免锁死控制台。

## 发布与回滚

推荐发布方式：

```bash
python scripts/cloud_release_deploy_v2.py --restart
```

设计：

- 代码发布到 `/root/ai-quant-trader/releases/<git_sha>`。
- `/root/ai-quant-trader/current` 指向当前运行版本。
- systemd 通过 `PYTHONPATH=/root/ai-quant-trader/current` 加载代码。
- 发布后执行 HTTP health check。
- health check 失败时自动把 `current` 切回旧版本并重启服务。

保护项：

- 不覆盖 `/root/ai-quant-trader/.env.runtime`
- 不覆盖 `/root/ai-quant-trader/config/config.yaml`
- 不删除 `/root/ai-quant-trader/data`
- 不删除 `/root/ai-quant-trader/logs`

因此，代码发布不等于云端运行配置已切换。涉及 `risk.ai_sizing_policy` 这类实盘运行参数时，必须在发布后显式修改云端根配置：

```bash
cd /root/ai-quant-trader
.venv/bin/python current/scripts/ai_sizing_policy_control.py \
  --config config/config.yaml \
  --policy hybrid_subjective_guarded_v2 \
  --max-tier-lift 1 \
  --min-factor-coverage 0.7
systemctl restart ai-quant-trader.service ai-quant-console.service
```

开启持仓复评 live_addon 小资金灰度：

```bash
cd /root/ai-quant-trader
PYTHONPATH=/root/ai-quant-trader/current .venv/bin/python - <<'PY'
from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.storage.sqlite import SQLiteStore

store = SQLiteStore("data/trader.sqlite3", "logs/audit.jsonl")
control = RuntimeControlManager(store, "config/config.yaml")
config = control.read_config()
control.set_config_value(config, "risk.position_review.enabled", True)
control.set_config_value(config, "risk.position_review.mode", "live_addon")
control.set_config_value(config, "risk.position_review.max_additions_per_position", 1)
control.set_config_value(config, "risk.position_review.max_add_fraction", 0.25)
control.write_config(config)
store.close()
PY
systemctl restart ai-quant-trader.service ai-quant-console.service
```

如果要回退到只审计不加仓：

```bash
cd /root/ai-quant-trader
PYTHONPATH=/root/ai-quant-trader/current .venv/bin/python - <<'PY'
from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.storage.sqlite import SQLiteStore

store = SQLiteStore("data/trader.sqlite3", "logs/audit.jsonl")
control = RuntimeControlManager(store, "config/config.yaml")
config = control.read_config()
control.set_config_value(config, "risk.position_review.mode", "shadow")
control.write_config(config)
store.close()
PY
systemctl restart ai-quant-trader.service ai-quant-console.service
```

退回 v2 减亏基准但不使用 DeepSeek 主观五档融合：

```bash
cd /root/ai-quant-trader
.venv/bin/python current/scripts/ai_sizing_policy_control.py \
  --config config/config.yaml \
  --policy calibrated_v2_loss_aware
systemctl restart ai-quant-trader.service ai-quant-console.service
```

回滚旧五档：

```bash
cd /root/ai-quant-trader
.venv/bin/python current/scripts/ai_sizing_policy_control.py \
  --config config/config.yaml \
  --policy legacy_factor_ranked
systemctl restart ai-quant-trader.service ai-quant-console.service
```

## 异地备份与恢复演练

本地维护定时器必须至少执行：

```bash
python scripts/runtime_maintenance.py \
  --backup-dir data/backups \
  --restore-drill-dir data/restore-drills
```

生产级要求：

- SQLite 备份必须通过 `PRAGMA integrity_check`。
- 恢复演练必须能把 gzip 备份解压为 SQLite 并通过完整性检查。
- 可选 `--offsite-backup-dir` 应指向 OSS/rclone/另一台服务器挂载目录，而不是同机普通目录。
- 备份成功但恢复演练失败，readiness 必须 `block`。

## 告警事件

以下事件必须进入 `/api/system/alerts`、`/api/system/readiness` 和 `/metrics`：

- Gate 持仓/余额读取失败
- 交易所对账失败
- 订单状态 `unknown`
- 原生止损失败或状态未知
- 新闻缓存过期
- DeepSeek 主备都不可用或预算阻断
- worker heartbeat 过期
- 运行维护失败
- 磁盘空间低
- SQLite 备份或恢复演练失败

## 交易所灾难恢复

### Gate API 断连超过 5 分钟

- 保留已有持仓。
- 禁止自动新开仓。
- 不自动撤原生止损。
- 不通过本系统自动应急平仓。
- 操作员到 Gate 官方端检查真实持仓、委托和止损。
- 恢复后必须完成 reconciliation，readiness 变为 `ok` 后才允许新开仓。

### 交易所有持仓但本地状态缺失

- 进入 `reconciliation_required` 或 `block`。
- 禁止新开仓。
- 控制台显示持仓来自 Gate 快照，不以本地 `state_trend.json` 为准。
- 人工确认后修复本地状态或在 Gate 官方端处理。

### 原生止损查询不到

- 禁止新开仓。
- 不盲目补发市价平仓，避免止损单实际存在时形成反向风险。
- 操作员到 Gate 官方端确认止损单、持仓和未成交委托。

### follower 账户失败

- 主账户是否继续由 `follower_failure_policy` 决定。
- 当前生产建议：主账户允许继续，但控制台必须出现 follower 告警。
- follower 账户恢复后必须重新对账，不允许盲目补单。

## 密钥治理

- `.env.runtime` 不得进入 Git、日志、备份包、聊天记录。
- 文件权限应限制为 root-only：`chmod 600 /root/ai-quant-trader/.env.runtime`。
- Gate API key 必须最小权限，禁止提现。
- DeepSeek 主备 key 必须可视化主备状态和失败原因。
- 轮换 API key 后必须记录 `secret_versions` 审计事件。

## 数据库扩展路线

当前 SQLite 适合单机小规模账户。

触发 Postgres 迁移的条件：

- 多账户持续扩展
- 多策略并发运行
- 多人同时登录控制台
- 历史数据和审计表明显变大
- 需要跨机器高可用

迁移前必须先抽象存储接口，不能直接把 SQLite SQL 散落到业务代码。

## 策略收益验证

系统稳定不等于策略盈利。

大资金前必须持续执行：

- walk-forward
- 样本外验证
- 实盘小仓统计
- AI overlay 前后对比
- 信号后验复盘

任何未经过样本外验证的仓位放大规则，不得直接用于大资金无人值守。

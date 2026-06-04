# AI 量化交易系统复制部署汇总

这个文件是给另一台电脑或另一个 Codex 使用的“一页式部署摘要”。详细安装步骤见 `docs/clone-install-runbook.md`。

## 1. 仓库与入口

- GitHub 仓库：`https://github.com/859429754-cmd/12`
- 后端入口：`ai_quant_trader/api/server.py`
- 主交易循环：`ai_quant_trader/app.py`
- 控制台前端：`console/`
- 主配置：`config/config.yaml`
- 环境变量模板：`.env.example`
- 真实运行密钥：`.env.runtime`，每台机器必须单独配置，绝不复制旧机器文件。
- systemd 模板：`deploy/systemd/`

## 2. 可复制的内容

可以完整复制或通过 Git clone 获取：

- Python 后端框架
- React 控制台
- ETH 趋势策略逻辑
- DeepSeek AI 五档仓位裁剪逻辑
- RiskManager 硬风控
- Gateway 模拟/实盘隔离
- Gate.io 执行层
- 回测和 trade ledger
- 新闻记忆与 48h 新闻上下文
- 订单生命周期状态机
- readiness / watchdog / metrics / backup 运维模块
- systemd 服务模板
- 测试用例和验收命令

## 3. 不能复制的内容

不要复制：

- `.env.runtime`
- `data/`
- `logs/`
- `output/`
- SQLite 数据库
- 当前账户状态
- 当前订单、持仓、止损状态
- 当前 DeepSeek/Gate API Key
- 当前服务器 SSH Key
- 任意真实密钥截图、日志、备份

## 4. 新机器必须填写的环境变量

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BACKUP_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com

GATEIO_TREND_API_KEY=
GATEIO_TREND_API_SECRET=

GATEIO_FOLLOWER_API_KEY=
GATEIO_FOLLOWER_API_SECRET=

GATEIO_RANGE_API_KEY=
GATEIO_RANGE_API_SECRET=

CONSOLE_ADMIN_PASSWORD=
CONSOLE_ACCOUNT1_PASSWORD=
CONSOLE_ACCOUNT2_PASSWORD=
CONSOLE_RANGE_PASSWORD=
CONSOLE_AUTH_DISABLED=
CONSOLE_PASSWORD_STRENGTH_CONFIRMED=
```

可选：

```text
CRYPTOPANIC_API_KEY=
FRED_API_KEY=
CONSOLE_CORS_ORIGINS=
CONSOLE_COOKIE_SECURE=
```

当前控制台安全使用账号登录/RBAC，不再使用 Trade PIN 或操作验证码。
生产云端不要设置 `CONSOLE_AUTH_DISABLED=1`。系统默认 fail-closed：未配置账号时，特权 API 返回 `console_auth_not_configured`，不会自动开放本地管理员权限。
大资金无人值守前必须使用强密码并设置 `CONSOLE_PASSWORD_STRENGTH_CONFIRMED=1`；临时弱密码只能用于联调，不应让 live readiness 通过。

## 5. 当前策略合同

当前生产主策略：

```text
标的：ETH/USDT:USDT
周期：1h
KC 中轨：EMA20
KC 通道：EMA20 ± ATR14 * 2.8
成交量过滤：Volume > SMA20 * 2.5
KDJ：9,3,3
多头过滤：K > D 且 J >= 50
空头过滤：K < D 且 J <= 50
ATR 固定止损：1.5 * ATR14
退出：反向穿越 KC 中轨
同向加仓：禁止
反手：允许，先平旧方向再开新方向
```

EMA89 当前不作为实盘开仓过滤器。任何修改必须新增研究、回测、样本外验证和 ADR。

## 6. AI 大脑边界

```text
本地策略决定方向
DeepSeek 评估信号质量
RiskManager 五档仓位裁剪
Gateway 执行订单
SQLite + JSONL 审计
```

五档仓位：

```text
block  = 0%
weak   = 25%
normal = 50%
strong = 75%
full   = 100%
```

DeepSeek 只能确认、降仓或阻断，不能发明交易方向。

## 7. 多账户模型

- `trend`: 账户1，运行 ETH 趋势策略。
- `follower`: 账户2，复用账户1策略信号和一次 AI 决策跟随执行。
- `range`: 震荡策略预留账户，当前不执行。

账户2不独立调用 AI，不独立计算策略，不绕过账户1的 AI/RiskManager 结论。每个账户独立校验余额、持仓、挂单、最小下单量、杠杆上限、原生止损和订单生命周期。

## 8. 控制台权限

- `admin`: 可切换 mock/live、更新 API、修改策略参数、审批提案、授权/暂停、执行危险手动控制。
- `account1`: 查看趋势账户，只能修改自身杠杆上限。
- `account2`: 查看跟随账户，只能修改自身杠杆上限。
- `range`: 查看震荡预留账户，只能修改自身杠杆上限。

公网开放前必须配置强密码和反向代理安全策略。

## 9. 一键部署命令摘要

```bash
cd /root
git clone https://github.com/859429754-cmd/12.git ai-quant-trader
cd /root/ai-quant-trader

python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

cp .env.example .env.runtime
nano .env.runtime

cd console
npm install
npm run build
cd ..

python -m compileall ai_quant_trader tests scripts
python -m pytest -q
python scripts/public_repo_preflight.py
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime

sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-quant-console.service
sudo systemctl enable --now ai-quant-trader.service
sudo systemctl enable --now ai-quant-health-watchdog.timer
sudo systemctl enable --now ai-quant-maintenance.timer
```

验收：

```bash
curl -s http://127.0.0.1:8090/api/health
curl -s http://127.0.0.1:8090/api/system/readiness
```

## 10. 发布前验证

每次修改后运行：

```powershell
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
cd console
npm.cmd run build
cd ..
python scripts/public_repo_preflight.py
```

如果交易逻辑、账户权限、AI 决策、Gateway 或云端部署改变，必须同步更新 `docs/current-operating-contract.md`、`CONTEXT.md`、相关 ADR 和 handoff。

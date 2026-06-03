# AI 量化系统复制安装手册

本手册用于把当前 AI 量化交易系统复制到另一台电脑或云服务器。除 API 密钥、账号密码、公网地址、Gate.io IP 白名单和运行模式外，其他代码和框架应保持一致。

## 1. 绝对不能复制的内容

不要复制这些运行期文件：

- `.env.runtime`
- `data/`
- `logs/`
- `output/`
- `console/dist/` 以外的临时构建产物
- SQLite 数据库
- 当前订单、持仓、止损、审计状态
- 真实 Gate.io / DeepSeek API Key
- 当前服务器 SSH Key
- 任意密钥截图、日志、备份

原因：这些内容属于当前账户真实运行状态。复制给别人会造成账户串联、状态污染、误判持仓、误下单或密钥泄露。

## 2. 新机器必须重新填写的内容

在新机器上创建 `.env.runtime`，至少配置：

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
```

说明：

- `GATEIO_TREND_*` 是账户1，运行当前 ETH 趋势策略。
- `GATEIO_FOLLOWER_*` 是账户2，复用账户1策略信号和一次 DeepSeek 决策跟随执行。
- `GATEIO_RANGE_*` 是震荡策略预留账户，当前默认不执行。
- 控制台安全使用账号登录/RBAC，不再使用 Trade PIN 或操作验证码。
- 若公网开放控制台，必须使用强密码，不能使用弱口令。
- 生产默认 fail-closed：不要设置 `CONSOLE_AUTH_DISABLED=1`。如果未配置账号密码，控制台特权 API 会拒绝访问。

还需要按新环境修改：

- 公网 IP / 域名
- Nginx 或反向代理配置
- systemd `WorkingDirectory`
- Gate.io API IP 白名单
- mock/live 运行模式
- 每个账户的杠杆上限

## 3. Windows 本地安装

```powershell
git clone https://github.com/859429754-cmd/12.git ai-quant-trader
cd ai-quant-trader

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

Copy-Item .env.example .env.runtime
notepad .env.runtime

cd console
npm install
npm.cmd run build
cd ..

python -m compileall ai_quant_trader tests scripts
python -m pytest -q
python scripts/public_repo_preflight.py
```

启动控制台：

```powershell
uvicorn ai_quant_trader.api.server:app --host 127.0.0.1 --port 8090
```

打开：

```text
http://127.0.0.1:8090/
```

## 4. Linux 云服务器安装

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
cd /root/ai-quant-trader

python -m compileall ai_quant_trader tests scripts
python -m pytest -q
python scripts/public_repo_preflight.py
```

只读对账：

```bash
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime
```

必须确认：

```text
balance_ok: true
reconciliation.status: ok
issues: []
```

安装 systemd：

```bash
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

## 5. 首次运行安全流程

1. 保持 `config/config.yaml`：

```yaml
runtime:
  execution_mode: mock
  dry_run: true
```

2. 启动服务。
3. 登录控制台，确认：

```text
账号余额来源正确
DeepSeek 已接入
新闻刷新正常
K线加载正常
readiness 没有 block
Gate 只读对账正常
```

4. 只有管理员账号可以切换 live。
5. 切换 live 前必须确认 Gate.io API IP 白名单、最小权限、原生止损、订单生命周期和 readiness 均正常。

## 6. 当前策略合同

当前生产策略是 ETH 1h 趋势突破：

```text
KC 中轨 = EMA20
KC 通道 = EMA20 ± ATR14 * 2.8
成交量过滤 = Volume > SMA20 * 2.5
KDJ = 9,3,3
多头过滤 = K > D 且 J >= 50
空头过滤 = K < D 且 J <= 50
ATR 固定止损 = 1.5 * ATR14
退出 = 反向穿越 KC 中轨
同向加仓 = 禁止
反手 = 允许，先平旧方向再开新方向
```

EMA89 当前不作为开仓过滤器。

## 7. AI 和账户逻辑

```text
本地趋势策略产生方向
  -> DeepSeek 分析一次
  -> RiskManager 输出 block/weak/normal/strong/full
  -> 账户1 trend 执行
  -> 账户2 follower 复用同一决策跟随执行
  -> 账户3 range 当前预留
```

DeepSeek 不允许发明方向，不能绕过本地策略、RiskManager、账户权限、授权、冷启动和杠杆上限。

## 8. 交付给新机器使用者的信息

交付时只提供：

- GitHub 仓库地址
- 安装手册路径：`docs/clone-install-runbook.md`
- 需要自行填写的 `.env.runtime` 字段清单
- 控制台地址
- 当前运行模式 mock/live
- readiness 状态
- 当前策略合同

不要交付任何真实 API Key、日志、SQLite、运行数据或备份。

# AI量化交易系统复制安装手册

本文档用于把当前这套 AI 量化交易控制台完整复制到另一台电脑或云服务器。除交易所 API、DeepSeek API、公网域名/IP、运行账号不同外，代码框架、策略逻辑、AI 大脑、控制台、风控和运维脚本都可以照搬。

## 当前可复制交付物

- 仓库地址：`https://github.com/859429754-cmd/12`
- 后端：`Python 3.11+ / FastAPI / asyncio / SQLite WAL / ccxt async`
- 前端：`React / TypeScript / Vite / Tailwind / lightweight-charts`
- 交易所：`Gate.io USDT 永续`
- 默认实盘策略：`ETH/USDT:USDT` 趋势策略
- 默认运行目录：`/root/ai-quant-trader`
- 默认控制台端口：`127.0.0.1:8090`
- 云端服务模板：`deploy/systemd/`

## 当前实盘策略合同

当前 ETH 趋势策略只使用以下主信号参数：

```text
周期：1h
KC中轨：EMA20
KC通道：EMA20 ± ATR14 * 2.8
成交量过滤：volume > SMA(volume, 20) * 2.5
KDJ：9, 3, 3
ATR固定止损：1.5 * ATR14
退出：反向穿越 KC 中轨
仓位：按 risk.max_total_leverage 和 AI 五档裁剪
```

当前不使用 EMA89 作为开仓过滤：

```yaml
use_ema_filter: false
ema_length: 89
```

EMA 字段保留给历史兼容、回测研究和未来重新启用，但当前实盘主参数面板不展示 EMA。

## AI 大脑运行边界

DeepSeek 只做信号质量评估和仓位裁剪，不直接发明交易方向。

执行链路：

```text
本地趋势策略产生 1h 技术信号
-> 本地形态/密集区/订单流/新闻上下文整理
-> DeepSeek 输出五分制评分
-> RiskManager 映射仓位档
-> Gateway 执行 Gate.io 订单
-> SQLite + JSONL 审计落库
```

五档仓位：

```text
block  = 0%
weak   = 25%
normal = 50%
strong = 75%
full   = 100%
```

硬边界：

- AI 不能绕过策略信号。
- AI 不能绕过逐标的授权。
- AI 不能绕过开仓暂停。
- AI 不能绕过交易所对账。
- AI 不能突破杠杆硬上限。
- DeepSeek JSON 无效时禁止新开仓。

## 需要在新机器上替换的内容

只改这些：

- `.env.runtime`
- `config/config.yaml` 中的公网环境相关项
- Nginx / 反向代理域名或 IP
- systemd `WorkingDirectory`，如果不是 `/root/ai-quant-trader`
- Gate.io API Key / Secret
- DeepSeek API Key
- `TRADE_PIN`
- `CONSOLE_OPERATION_CODE`

不要复制这些运行期文件：

- 本机 `.env.runtime`
- `data/`
- `logs/`
- `output/`
- `console/dist/` 以外的临时构建产物
- 任何真实密钥截图或备份

## 本地开发机安装

Windows PowerShell 示例：

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
```

本地启动控制台：

```powershell
uvicorn ai_quant_trader.api.server:app --host 127.0.0.1 --port 8090
```

打开：

```text
http://127.0.0.1:8090/
```

## 云服务器安装

Linux 示例：

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
```

前端构建：

```bash
cd /root/ai-quant-trader/console
npm install
npm run build
cd /root/ai-quant-trader
```

验证：

```bash
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
python scripts/public_repo_preflight.py
```

## 必填环境变量

`.env.runtime` 必须至少配置：

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com

GATEIO_TREND_API_KEY=
GATEIO_TREND_API_SECRET=

TRADE_PIN=
CONSOLE_OPERATION_CODE=
```

如果后续启用震荡策略独立账户，再配置：

```text
GATEIO_RANGE_API_KEY=
GATEIO_RANGE_API_SECRET=
```

可选：

```text
CRYPTOPANIC_API_KEY=
FRED_API_KEY=
```

## 首次运行安全流程

1. 保持 `config/config.yaml`：

```yaml
runtime:
  execution_mode: mock
  dry_run: true
```

2. 启动服务。
3. 打开控制台确认：

```text
账户余额来源
DeepSeek 已接入
新闻刷新正常
K线加载正常
readiness 无 block
```

4. 跑 Gate 只读对账：

```bash
cd /root/ai-quant-trader
. .venv/bin/activate
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime
```

必须看到：

```text
balance_ok: true
reconciliation.status: ok
issues: []
```

5. 确认无误后再通过控制台或 API 切换 `live`。

## systemd 部署

安装模板：

```bash
cd /root/ai-quant-trader
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-quant-console.service
sudo systemctl enable --now ai-quant-trader.service
sudo systemctl enable --now ai-quant-order-status-worker.service
sudo systemctl enable --now ai-quant-health-watchdog.timer
sudo systemctl enable --now ai-quant-maintenance.timer
```

检查：

```bash
systemctl status ai-quant-console.service --no-pager -l
systemctl status ai-quant-trader.service --no-pager -l
systemctl status ai-quant-order-status-worker.service --no-pager -l
```

健康检查：

```bash
curl -fsS http://127.0.0.1:8090/api/health
curl -fsS http://127.0.0.1:8090/api/system/readiness
```

## 公网访问

推荐方式：

```text
公网域名/IP -> Nginx/反向代理 -> 127.0.0.1:8090
```

控制台可以公开访问，但所有修改运行参数、切换 live、手动交易等 mutating API 必须带 `x-operation-code`。不要把 `.env.runtime` 或 API 密钥暴露到前端。

最低建议：

- 云服务器安全组只开放需要的 HTTP/HTTPS 端口。
- SSH 只允许密钥登录。
- Gate API 开启 IP 白名单。
- Gate API 禁止提现权限。

## 复制后必须跑的验收命令

本地或云端每次迁移后必须跑：

```bash
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
cd console && npm run build
cd ..
python scripts/public_repo_preflight.py
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime
curl -fsS http://127.0.0.1:8090/api/system/readiness
```

合格标准：

```text
pytest 全部通过
frontend build 通过
public_repo_preflight ok=true
Gate readiness status=ok
/api/system/readiness overall=ok
```

## 实盘启动标准

可以切 live 的最低条件：

- `.env.runtime` 配置完整。
- Gate 只读对账 `ok`。
- 当前无幽灵订单。
- 当前本地状态与交易所持仓一致。
- DeepSeek 可调用。
- 新闻缓存和订单流可用。
- `ETH/USDT:USDT` 已授权。
- 开仓未暂停。
- readiness 无 block。

切 live 后系统不会立即乱下单；只有当 1h 策略出现真实信号，且 AI 与 RiskManager 均通过后才会提交 Gate 订单。

## 当前不建议复制的内容

不要复制当前服务器的：

- `.env.runtime`
- `data/trader.sqlite3`
- `data/state_trend.json`
- `logs/audit.jsonl`
- `data/backups/`

这些是当前账户的真实运行状态。给别人安装时必须从干净状态启动。

## 故障排查入口

- 服务状态：`systemctl status ai-quant-console.service --no-pager -l`
- readiness：`curl -fsS http://127.0.0.1:8090/api/system/readiness`
- 余额：`curl -fsS 'http://127.0.0.1:8090/api/account/balance?account_slot=trend'`
- Gate 对账：`python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime`
- 维护备份：`python scripts/runtime_maintenance.py --config config/config.yaml --backup-keep 24 --min-free-ratio 0.05`


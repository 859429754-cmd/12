# AI量化交易系统 Codex 一键部署交接文件

本文档可以直接发给另一台电脑或云服务器上的 Codex，让它按本文完成部署。目标是：除 API、运行账号、公网地址不同外，代码框架、策略逻辑、AI 大脑、控制台、风控和运维脚本全部保持一致。

## 0. 给接手 Codex 的最高指令

你现在接手部署一套 AI 量化交易控制台系统。请严格按本文执行，不要重写架构，不要改变策略逻辑，不要改变 AI 大脑边界，不要绕过风控。

部署目标：

```text
从 GitHub 克隆当前系统
安装 Python / Node 依赖
创建 .env.runtime
填入新用户自己的 DeepSeek / Gate.io API
构建前端
启动 FastAPI 控制台
安装 systemd 服务
跑完整验收
确认 mock 正常
确认 readiness 正常
只在用户明确确认后切 live
```

硬性禁止：

- 不要读取、打印、提交、泄露 `.env.runtime` 的完整内容。
- 不要把 `data/`、`logs/`、SQLite、旧订单状态复制到新机器。
- 不要修改当前 ETH 策略参数。
- 不要把 EMA89 重新加入实盘开仓过滤。
- 不要让 AI 绕过本地策略信号直接开仓。
- 不要跳过 RiskManager。
- 不要跳过 Gate 只读对账。
- 不要在 readiness 有 `block` 时切 live。
- 不要承诺长期盈利，只能交付稳定、可审计、可恢复的交易系统。

如果用户要求“大资金、无人值守、立刻实盘”，必须先完成全部验收，并明确指出：任何自动化交易都有亏损风险，系统只能控制执行风险和风控边界，不能保证盈利。

## 0.1 部署前需要用户提供的信息

接手 Codex 必须向用户索取或让用户在本机安全填写以下内容。不要让用户把完整密钥发到聊天里，优先让用户直接编辑 `.env.runtime`。

```text
DeepSeek API Key
Gate.io 趋势账户 API Key
Gate.io 趋势账户 API Secret
公网 IP 或域名
控制台操作验证码 CONSOLE_OPERATION_CODE
TRADE_PIN
是否仅 mock 运行
是否允许切 live
```

如果未来启用震荡账户，再索取：

```text
Gate.io 震荡账户 API Key
Gate.io 震荡账户 API Secret
```

## 0.2 一键部署执行摘要

如果目标是 Linux 云服务器，接手 Codex 可以按下面顺序执行：

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
sudo systemctl enable --now ai-quant-order-status-worker.service
sudo systemctl enable --now ai-quant-health-watchdog.timer
sudo systemctl enable --now ai-quant-maintenance.timer

curl -s http://127.0.0.1:8090/api/health
curl -s http://127.0.0.1:8090/api/system/readiness
```

如果目标是 Windows 本地开发机，按本文第 7 节执行。

部署完成后，交付给用户：

```text
控制台地址
当前运行模式 mock/live
readiness 状态
Gate 对账状态
DeepSeek 接入状态
当前策略参数
是否有 block/warning
验收命令结果
```

## 1. 仓库与核心入口

- GitHub 仓库：`https://github.com/859429754-cmd/12`
- 主项目目录：`/root/ai-quant-trader`，本地可自定义
- 后端入口：`ai_quant_trader/api/server.py`
- 主交易循环：`ai_quant_trader/app.py`
- 控制台前端：`console/`
- 主配置文件：`config/config.yaml`
- 环境变量模板：`.env.example`
- 真实运行密钥：`.env.runtime`，必须每台机器单独配置
- 详细复制安装手册：`docs/clone-install-runbook.md`
- 云端 systemd 模板：`deploy/systemd/`

## 2. 可以完整复制的内容

这些内容可以从仓库完整复制：

- Python 后端框架
- React 控制台
- ETH 趋势策略逻辑
- DeepSeek AI 五档仓位裁剪逻辑
- RiskManager 硬风控
- Gateway 模拟/实盘隔离
- Gate.io 执行层
- 回测与交割单模块
- 新闻记忆与 48h 新闻上下文
- 订单生命周期状态机
- readiness / watchdog / metrics / backup 运维模块
- systemd 服务模板
- 测试用例和验收命令

## 3. 绝对不能复制的内容

这些内容不能给别人复制，也不能提交：

- `.env.runtime`
- `data/`
- `logs/`
- `output/`
- SQLite 数据库
- 当前账户状态
- 当前订单/持仓/止损状态
- 当前 DeepSeek/Gate API Key
- 当前公网控制台操作验证码
- 当前服务器 SSH Key
- 任何真实密钥截图、日志、备份

原因：这些都是当前账户的真实运行状态。复制到别人机器会造成账户串联、状态污染、误开仓、误判持仓或密钥泄露。

## 4. 新机器必须替换的配置

每台新机器必须重新配置：

```text
DEEPSEEK_API_KEY=
DEEPSEEK_BASE_URL=https://api.deepseek.com

GATEIO_TREND_API_KEY=
GATEIO_TREND_API_SECRET=

TRADE_PIN=
CONSOLE_OPERATION_CODE=
```

如果未来启用震荡策略独立账户，再配置：

```text
GATEIO_RANGE_API_KEY=
GATEIO_RANGE_API_SECRET=
```

还需要按新环境修改：

- 公网 IP / 域名
- Nginx 或反向代理配置
- systemd `WorkingDirectory`
- Gate.io API IP 白名单
- 是否 mock / live
- 最大杠杆上限

## 5. 当前实盘策略合同

当前主策略是 ETH 1h 趋势突破策略。

```text
交易标的：ETH/USDT:USDT
周期：1h
KC中轨：EMA20
KC通道：EMA20 ± ATR14 * 2.8
成交量过滤：Volume > SMA20 * 2.5
KDJ：9, 3, 3
多头过滤：K > D 且 J >= 50
空头过滤：K < D 且 J <= 50
ATR固定止损：1.5 * ATR14
退出：反向穿越 KC 中轨
同向加仓：禁止
反手：允许，先平旧方向再开新方向
```

当前实盘主策略不使用 EMA89 作为开仓过滤。EMA 字段只保留给历史兼容、研究回测或未来重新启用，不应出现在当前主参数面板里影响实盘判断。

## 6. AI 大脑边界

DeepSeek 不是交易方向发动机。当前系统的边界是：

```text
本地策略决定是否有交易信号和方向
DeepSeek 评估信号质量
RiskManager 做最终硬风控
Gateway 执行订单
SQLite + JSONL 做审计
```

AI 只允许做：

- 确认信号
- 降低仓位
- 一票否决
- 输出五分制评分
- 根据新闻、订单流、密集区、形态、4h 背景辅助评估

AI 不允许做：

- 绕过本地策略直接开仓
- 绕过 RiskManager
- 绕过标的授权
- 绕过开仓暂停
- 突破杠杆上限
- 伪造方向
- DeepSeek 输出无效时继续开仓

五档仓位：

```text
block  = 0%
weak   = 25%
normal = 50%
strong = 75%
full   = 100%
```

## 7. 标准安装流程

### Windows 本地开发机

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
```

启动控制台：

```powershell
uvicorn ai_quant_trader.api.server:app --host 127.0.0.1 --port 8090
```

打开：

```text
http://127.0.0.1:8090/
```

### Linux 云服务器

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
```

安装 systemd：

```bash
sudo cp deploy/systemd/*.service deploy/systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-quant-console.service
sudo systemctl enable --now ai-quant-trader.service
sudo systemctl enable --now ai-quant-order-status-worker.service
sudo systemctl enable --now ai-quant-health-watchdog.timer
sudo systemctl enable --now ai-quant-maintenance.timer
```

## 8. 验收命令

每台新机器安装后必须跑：

```bash
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
python scripts/public_repo_preflight.py
```

前端：

```bash
cd console
npm run build
```

Gate 只读对账：

```bash
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime
```

HTTP readiness：

```bash
python scripts/http_readiness_check.py --url http://127.0.0.1:8090/api/system/readiness
```

## 9. 启动 live 前检查

切换 live 前必须确认：

- `.env.runtime` 已正确配置。
- Gate API 使用最小权限，不允许提现。
- Gate API 已配置服务器 IP 白名单。
- DeepSeek API Key 可用。
- readiness 无 `block`。
- Gate 只读对账通过。
- 没有 orphan position。
- 没有 ghost order。
- 没有未知订单生命周期状态。
- 新闻缓存正常。
- K线数据正常。
- 订单流数据质量没有阻断。
- 当前策略参数与本文档一致。
- 控制台公网修改参数接口有操作验证码。

live 模式不会立即乱下单。只有满足以下链路才会下单：

```text
1h 本地策略信号
-> DeepSeek 五分制评估
-> RiskManager 通过
-> Gate 对账新鲜
-> Gateway 提交订单
-> 原生止损/状态机审计
```

## 10. 当前交付状态

当前仓库已经具备复制部署基础，但还不是完全一键安装包。

已具备：

- 可复制源码仓库
- `.env.example`
- 复制安装手册
- systemd 模板
- readiness / watchdog / metrics
- public repo preflight
- 完整测试集

建议下一步补齐：

- `scripts/bootstrap_windows.ps1`
- `scripts/bootstrap_linux.sh`
- `.env.runtime.template`
- 云端一键 systemd 安装脚本
- 安装后自动验收脚本
- 当前生产策略 profile 导出
- 生产部署 checklist 自动检查

## 11. 结论

这套系统可以作为“可复制安装框架”交付给另一台电脑或服务器，但复制时必须只复制代码和模板，不能复制真实运行状态。

正确复制方式：

```text
复制仓库代码
新建 .env.runtime
填新账户 API
重新构建前端
重新跑测试和 readiness
从 mock 开始验收
确认无阻断后再切 live
```

错误复制方式：

```text
直接复制整台服务器目录
连同 data/logs/.env.runtime 一起复制
跳过 readiness
跳过 Gate 对账
直接 live 运行
```

后一种方式会造成实盘事故风险，禁止使用。

## 12. 交给另一个 Codex 时的最短提示词

如果只想给另一个 Codex 一个最短指令，可以复制下面这段：

```text
你现在要部署一套 AI 量化交易控制台系统。请完整阅读并严格执行 docs/ai-quant-system-copy-summary.md。

目标：
1. 从 https://github.com/859429754-cmd/12 克隆项目。
2. 除 API、公网地址、运行账号外，保持策略逻辑、AI 大脑、RiskManager、Gateway、控制台、systemd 运维框架完全一致。
3. 不复制 .env.runtime、data、logs、SQLite、订单状态、当前持仓状态。
4. 让用户在本机安全填写 .env.runtime，不要在聊天里打印完整密钥。
5. 先 mock 验收，再 Gate 只读对账，再 readiness 验收。
6. 只有用户明确确认后才能切 live。
7. 不得改变当前 ETH 1h KC + VOL + KDJ 策略参数。
8. 不得让 AI 绕过本地策略信号和 RiskManager。
9. 部署完成后输出控制台地址、运行模式、readiness、Gate 对账、DeepSeek 接入、策略参数和测试结果。

验收命令必须包含：
python -m compileall ai_quant_trader tests scripts
python -m pytest -q
cd console && npm run build
python scripts/public_repo_preflight.py
python scripts/gate_live_readiness.py --config config/config.yaml --env-file .env.runtime

如果任何验收失败，先修复，不要切 live。
```

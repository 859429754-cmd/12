# AI量化系统开发交接文档

## 0. Project Basics

- Local workspace: `C:\Users\杨\Documents\Codex\2026-05-12\new-chat`
- Local console: `http://127.0.0.1:8090/`
- Cloud server: `root@8.209.200.19`
- Cloud deploy dir: `/root/ai-quant-trader`
- SSH key path: `C:\Users\杨\Documents\Codex\2026-05-12\new-chat\.ssh\aiquant_aliyun`
- Runtime secret file: `.env.runtime`
- Main config: `config/config.yaml`
- Frontend stack: React + TypeScript + Vite + Tailwind + lightweight-charts
- Backend stack: Python 3.11+ / FastAPI / asyncio / Pydantic / SQLite WAL / ccxt async

`.env.runtime` contains DeepSeek, Gate.io and legacy notification secrets. Never print full contents, commit it, or write it to normal logs.

## 1. Core Architecture

The system is an AI-assisted crypto quant trading system.

- Execution venue: Gate.io USDT perpetuals
- Default symbols: `ETH/USDT:USDT`, `BTC/USDT:USDT`, `SOL/USDT:USDT`
- Default timeframe: 1h
- Orderflow data: Binance, OKX, Bybit, Gate public read-only sources
- News data: free public sources first, covering macro finance, politics, central banks, Fed, USD, oil, geopolitics, crypto industry
- Interaction priority: Web console first; legacy DingTalk command/push paths are disabled and must not be reintroduced without an explicit decision.
- Storage: SQLite WAL + JSONL audit logs
- AI: DeepSeek for news, market, orderflow, pattern, dense-zone and state synthesis

## 2. Current Directory Map

Important paths:

- `main.py`: project entrypoint
- `config/config.yaml`: runtime, symbol, risk, strategy, AI, news config
- `.env.example`: safe env template
- `.env.runtime`: real local runtime secrets; do not leak
- `data/`: runtime SQLite/news memory/mock trading state
- `knowledge/`: AI knowledge and trading memory
- `logs/`: runtime logs
- `ai_quant_trader/app.py`: core `TradingApp`
- `ai_quant_trader/api/server.py`: FastAPI console backend
- `ai_quant_trader/brain/deepseek.py`: DeepSeek structured decision layer
- `ai_quant_trader/brain/knowledge.py`: AI knowledge memory
- `ai_quant_trader/brain/wakeup.py`: emergency wakeup logic
- `ai_quant_trader/core/config.py`: config loading and env injection
- `ai_quant_trader/core/control.py`: authorization, pause, reports, hot updates, approvals
- `ai_quant_trader/core/models.py`: global Pydantic models
- `ai_quant_trader/core/secrets.py`: secret hot update, masking, versioning, rollback
- `ai_quant_trader/core/state.py`: runtime state persistence
- `ai_quant_trader/data/market.py`: OHLCV clients and source fallback
- `ai_quant_trader/data/news.py`: news collection, summarization, filtering
- `ai_quant_trader/data/news_memory.py`: news memory
- `ai_quant_trader/data/orderflow.py`: multi-exchange orderflow aggregation
- `ai_quant_trader/execution/gateio.py`: Gate.io real execution client
- `ai_quant_trader/execution/gateway/`: mock/live gateway abstraction
- `ai_quant_trader/features/`: dense zone, orderflow features, patterns
- `ai_quant_trader/interaction/`: legacy interaction surface; no active DingTalk command path in the current runtime
- `ai_quant_trader/monitoring/price.py`: abnormal price wakeup monitor
- `ai_quant_trader/optimizer/`: monthly review and parameter proposals
- `ai_quant_trader/reporting/hourly.py`: hourly reports and push text
- `ai_quant_trader/risk/manager.py`: hard risk controls
- `ai_quant_trader/storage/sqlite.py`: SQLite WAL persistence
- `ai_quant_trader/strategy/`: strategy base, indicators, lab, range, trend
- `console/src/main.tsx`: React console
- `console/src/lib/api.ts`: frontend API client
- `tests/`: regression tests for AI, backtest, control, gateway, news, risk, strategy, console API and encoding

## 3. Current Trend Strategy

The current production strategy is ETH-first 1h trend breakout:

- Keltner Channel: middle EMA20, width ATR14 * 2.8
- Volume filter: SMA(volume, 20) * 2.5
- Momentum filter: KDJ(9, 3, 3)
- EMA89: removed from strategy code, optimizer surface, chart layers, and AI evidence; do not reintroduce without a separate research path and ADR
- ATR stop multiple: 1.5 fixed from entry, not trailing
- Long: previous close <= previous KC upper, current closed close > current KC upper, volume > VMA20 * 2.5, KDJ K > D and J >= 50
- Short: previous close >= previous KC lower, current closed close < current KC lower, volume > VMA20 * 2.5, KDJ K < D and J <= 50
- Long exit: reverse cross below KC middle
- Short exit: reverse cross above KC middle
- No same-direction add-on; reversal closes the old side before opening the new side

Hard rule: do not silently change this current ETH trend strategy definition. Future factor discovery is allowed only as a separate research path and must not replace the production contract without an explicit decision.

## 4. AI and Strategy Boundary

Fixed strategy remains the primary opening tool.

AI may:

- Confirm trades
- Reduce position size
- Veto trades
- Generate candidate plans in `ai_candidate_approval` mode, requiring human approval

AI may not:

- Bypass hard risk controls
- Bypass cold-start lock
- Bypass per-symbol authorization
- Bypass total leverage cap
- Bypass no same-direction duplicate add rule
- Bypass console account RBAC

AI output must be structured Pydantic JSON and include:

- `regime`
- `direction`
- `confidence`
- `news_alignment`
- `orderflow_alignment`
- `dense_zone_position`
- `entry_zone_estimate`
- `tp_estimate`
- `sl_estimate`
- `veto_action`
- `brief_reason`

Full-position conditions are strict:

- Technicals, AI regime, news, orderflow/dense-zone: at least three strong same-direction confirmations
- AI confidence >= 0.75
- Risk manager still clips total exposure to equity * 4

Dense-zone knowledge:

- Repeated tests inside a dense zone imply range behavior
- Breakout from dense zone into vacuum implies trend migration to the next dense zone
- Dense-zone midpoint separates range strength and weakness
- Support/resistance flips after breakout
- Trends often migrate from one dense zone to another, possibly building new dense zones in the vacuum

## 5. Range Strategy Plan

Range strategy is not implemented yet. Reserved directions:

- Bollinger mean reversion
- Grid strategy
- Box high-sell low-buy

Strategy Lab categories should support:

- Trend strategy
- Range strategy
- Risk filter
- AI auxiliary module

## 6. Gateway Mock/Live Isolation

Runtime mode:

- `runtime.execution_mode = "mock" | "live"`

Gateway classes:

- `MockExchangeGateway`: local JSON accounting, no exchange requests
- `GateRealGateway`: real Gate.io gateway, fixed `dry_run=False`
- `create_exchange_gateway(config)`: single factory

Business rule:

- Do not scatter `if dry_run` through trading logic
- Mock/live differences must stay in the Gateway layer

Live switching safety:

- Live to mock: admin-only operation.
- Mock to live: admin-only operation under console account RBAC.
- The old `TRADE_PIN` model is superseded by ADR-0005.
- Public console exposure is forbidden unless account login/RBAC is configured.

Existing endpoint:

- `/api/control/runtime-mode`

Existing UI:

- Console login session with role-scoped permissions.

## 7. Backtesting State and Requirements

Current endpoints:

- `/api/backtest/trend`
- `/api/backtest/trend/job`
- `/api/backtest/jobs/{job_id}`
- `/api/backtest/custom`

Current model includes:

- Taker fee
- Slippage bps
- Background job progress
- Return, max drawdown, trade count, win rate, cost ratio
- Simplified ledger

Industrial requirements still missing:

- FMZ-style pessimistic bar-by-bar matching
- `IntrabarPathModel`
- Bullish candle path: Open -> Low -> High -> Close
- Bearish candle path: Open -> High -> Low -> Close
- If TP and SL hit in same candle, assume SL first
- Full Trade Ledger:
  - entry time
  - side
  - entry price
  - exit time
  - exit price
  - PnL
  - MAE / max adverse excursion
  - fee
  - slippage
  - exit reason
- Paginated OHLCV download and local cache
- Free Binance/OKX/Gate source fallback
- Backtest and live engine should share one strategy signal interface

Professional engine options:

- Quick validation: keep custom engine, add IntrabarPathModel and full ledger
- Production-grade: evaluate Backtrader or vectorbt while preserving shared signal interface

## 8. News System Requirements

News should behave like a timeline feed, not AI-generated macro commentary.

Each news item should preserve:

- Timestamp
- Person/institution
- Concrete action/statement
- Concrete data/fact
- Bullish/bearish/neutral label
- Source

Forbidden:

- Empty macro phrasing
- AI inventing news facts
- Losing original factual detail

Current modules:

- `NewsCollector`
- `NewsMemoryStore`
- `/api/news/latest`
- `/api/news/refresh`
- `/api/news/memory`
- `TradingApp.collect_news_once`
- `TradingApp._news_for_trading_cycle`

Requirements:

- Refresh every 5-15 minutes
- Hourly analysis consumes latest cache
- If news fails, trading degrades to cached news or technical-only mode, not global crash
- Split raw news collection from AI labeling
- AI labels importance/direction; it does not create facts
- Maintain 7-day important news memory
- Clean longer-term memory around 30 days

Possible legal/free sources:

- ForexLive
- Investing
- MarketWatch
- CNBC
- Fed official sources
- White House
- BLS / BEA / FRED
- CoinDesk / Cointelegraph
- Binance / Gate / OKX announcements

## 9. DeepSeek State

Current config model names:

- `decision_model: "deepseek-v4-pro"`
- `report_model: "deepseek-v4-pro"`
- `emergency_screening_model: "deepseek-v4-flash"`
- `emergency_decision_model: "deepseek-v4-pro"`

Use:

- Regular hourly decisions/reports: Pro
- Emergency price/news screening: Flash first, then Pro final decision

DeepSeek must:

- Return Pydantic-validated JSON
- Avoid hallucination and unsupported conclusions
- Include entry, TP, SL estimates for trade suggestions
- Be audited and persisted
- Be constrained by local hard rules

## 10. Web Console State

Implemented:

- Dark high-density three-column layout
- Account summary
- Mock/live mode status
- Opening permission status
- DeepSeek status
- Symbol selector
- Authorization, pause, AI scan, small-entry test, close, close-all
- Strategy Lab
- K-line chart with lightweight-charts
- Trade markers
- Data source selector
- Deep backtest panel and progress
- Simplified ledger
- AI market view
- News feed
- Recent events
- API client with timeout, retry, `ApiError`
- ErrorBoundary
- Console login session

Still missing:

- WebSocket realtime candles/trades
- Chart order dragging
- Multi-chart layout
- Strategy version delete/compare/rollback UI
- Backtest job list
- Saved backtest comparisons
- Equity curve and return curve
- Live orders/fills detail
- Position risk heatmap
- Data-source health
- Exchange connection health
- Traceable AI decision chain

## 11. Current Verification Baseline

Latest reported passing checks:

- `python -m compileall ai_quant_trader tests`
- `python -m pytest -q`
- `cd console && npm.cmd run build`

Reported results:

- 50 tests passed
- Vite build passed

Smoke endpoints:

- `/api/status`: 200
- `/api/account/balance`: 200
- `/api/markets/symbols`: 200
- `/api/strategy-lab/versions`: 200
- `/api/news/latest?limit=1`: 200
- `/api/control/runtime-mode`: protected by console account login/RBAC; admin-only runtime control
- `/api/manual-small-entry/execute`: mock order success
- `/api/backtest/trend/job`: background job completes

Browser automation:

- Page opens
- No infinite connection page
- K-line loads
- Console login session opens/closes
- No frontend render crash found

Do not assume these remain true. Re-run relevant checks after changes.

## 12. High-priority Backlog

### Stabilize Current System

- Scan and fix encoding corruption
- Sync latest local code to cloud
- Add console authentication before any public exposure
- Configure console account passwords and keep `CONSOLE_AUTH_DISABLED` unset in production
- Confirm strong, unique console passwords with `CONSOLE_PASSWORD_STRENGTH_CONFIRMED=1` only after rotation; live readiness blocks unattended capital without this confirmation
- Run under systemd 24/7
- Add cloud health check script
- Add Playwright E2E:
  - page load
  - console login and RBAC-protected runtime control
  - K-line load
  - news refresh
  - backtest start
  - strategy save/validate
  - mock small-entry test

### Industrialize Backtesting

- FMZ pessimistic IntrabarPathModel
- Full Trade Ledger
- Paginated OHLCV cache
- Multi-exchange data fallback quality scoring
- Compare fixed samples against TradingView/FMZ
- Strategy parameter scan
- Full-position trigger probability simulation
- AI proxy comparison:
  - raw trend strategy
  - AI veto
  - AI downsize
  - AI full-position
  - auto rollback when AI worsens results

### Rebuild News Timeline

- Raw factual timeline
- AI labels only; no invented news
- Refresh every 5-15 minutes
- Source health panel
- 7-day important memory
- 30-day cleanup
- Major news wakeup through Flash + Pro

### Industrialize Execution

- Read-only reconciliation before live trading:
  - Gate balance
  - Gate positions
  - local SQLite positions
  - order state
- Ghost order reconciliation
- 429/500 retry and circuit breaker
- 5-minute exchange disconnect degradation
- Idempotent `client_order_id`
- Cancel/replace/fill confirmation
- Live minimum-position acceptance test
- Only then consider larger size

### Professional Quant Terminal

- Realtime WebSocket candles
- Realtime trade stream
- Chart order markers
- Equity curve
- Strategy performance comparison
- Factor discovery
- Strategy plugin system
- Android/PWA wrapper
- Optional Tauri/Electron desktop

## 13. Forbidden Actions

- Bypass Gate.io risk controls
- Let AI privately hot-update strategy parameters
- Let AI increase leverage beyond total 4x cap
- Open unauthorized symbols
- Enable live opening by default after cold start
- Duplicate same-direction add when position exists
- Hardcode ghost frontend symbols
- Store full notification/API secrets in logs or DB
- Call DeepSeek per candle for multi-year backtests
- Ignore fee, slippage, failed fills, rate limits for prettier backtests
- Expose unauthenticated console to public internet
- Treat button clicking as sufficient testing

## 14. Development Constitution

### Defensive Programming

Anything touching these must handle specific exceptions, log, and degrade clearly:

- Network requests
- Exchange APIs
- WebSocket
- File IO
- Database
- AI API
- Notification API
- OHLCV/news/orderflow external data

Trading-related exceptions must block new entries or degrade to read-only mode.

### Strict Types

Python:

- Type hints required
- External inputs validated with Pydantic
- AI output validated as Pydantic JSON
- Orders use structured models such as `OrderRequest` / `OrderResult`
- Avoid unstructured dict plumbing in core paths

TypeScript:

- Define API response interfaces/types
- Avoid `any` in key business data
- Model form payloads explicitly
- Route API errors through `ApiError`

### Logging

Forbidden:

- `print()` in core trading path
- `console.log()` in key frontend path
- full API key / secret / webhook logs

Required logging fields when relevant:

- `symbol`
- `order_id`
- `client_order_id`
- `strategy_id`
- `operator_id`
- `gateway mode`
- `execution_mode`
- `error type`

### Live Safety Priority

Highest priority order:

1. Absolute total position <= total equity * 4
2. Cold start defaults to opening paused
3. Per-symbol authorization
4. Console account RBAC controls live switching and dangerous operations
5. AI may veto
6. Local technical signal must confirm unless human-approved AI candidate mode
7. No duplicate same-direction add
8. Missing data/news/orderflow lowers confidence or blocks entries
9. Invalid DeepSeek output blocks entries
10. Any exception degrades conservatively

## 15. Next Development Order

1. Stabilize current local/cloud system and authentication
2. Industrialize backtesting
3. Rebuild news timeline
4. Industrialize execution
5. Build professional terminal features

Before changing production trading behavior, ask what happens during:

- exchange API outage
- 429/500 burst
- websocket disconnect
- partial fill
- stale OHLCV
- stale news cache
- invalid AI JSON
- SQLite write failure
- clock skew
- cloud reboot

## 16. Current Console Account Model

As of 2026-06-04, console security is account-login based. This supersedes earlier Trade PIN and operation-code designs for console operations.

Authoritative roles:

- `admin`: can switch mock/live mode, update API secrets, change strategy parameters, approve proposals, authorize/pause symbols and use dangerous manual controls.
- `account1`: trend account view; view-only except its own account-level leverage cap.
- `account2`: follower account view; view-only except its own account-level leverage cap.
- `range`: reserved range-strategy account view; view-only except its own account-level leverage cap after the range strategy is implemented.

Trading semantics:

- Strategy direction is generated once by the local ETH trend strategy.
- DeepSeek analysis runs once for the shared strategy signal.
- The final AI/RiskManager decision fans out to configured accounts.
- Account balances, leverage caps, positions, order lifecycle, native stops and gateway health remain account-specific.

Ignore older plans that require Trade PIN, a separate operation code, or using the range account slot as the follower account. ADR-0005 is the current console safety ADR.

## 17. Market Background News Layer

As of 2026-06-18, news input for the trading AI is not a single recent-news summary.

The authoritative news context has two layers:

- `market_background`: deterministic local background built from high-impact events that remain active under a 24-72 hour decay window.
- `news`: realtime news window, usually the latest 1 hour of factual news items.

`market_background` is generated by `MarketNewsContextBuilder` and persisted through:

- `news_events`
- `market_background_snapshots`

Each `NewsEvent` preserves:

- timestamp
- source
- title and factual summary
- direction: bullish / bearish / neutral / unknown
- severity: low / medium / high / critical
- risk score
- confidence
- affected asset scope
- decay window

DeepSeek must interpret realtime news against `market_background`. It must not treat a single latest headline as the full macro context.

Direction semantics remain strategy-relative:

- short signal + bearish news = aligned
- long signal + bullish news = aligned
- short signal + bullish news = conflict
- long signal + bearish news = conflict

This layer does not change the ETH 1h KC + VOL + KDJ production strategy and does not allow AI to invent direction.
## 2026-06-18 新闻、BTC 风向标与 AI 仓位口径

以后以本节为准：AI 对消息面必须拆成“方向”和“执行风险”两类判断，忽略之前把重大新闻风险粗暴等同于否决开仓的方案。

- 本地 ETH 1h 趋势策略仍是唯一自动开仓方向来源，AI 不能发明 long/short。
- `news_alignment` 是相对策略方向的一致性：做空信号遇到利空、做多信号遇到利多为 `aligned`；反向为 `conflict`。
- `news_risk_score` 是事件执行风险、波动风险、流动性风险，不代表新闻方向本身。
- AI 必须额外评估 `crypto_market_impact_score`、`symbol_news_impact_score`、`btc_leader_alignment`、`btc_leader_regime`、`btc_leader_impact_score`、`eth_btc_rotation_score`、`pattern_confirmation_score`。
- BTC 是 ETH 仓位判断的风向标，但不是单一绝对方向闸。系统必须同时看 BTC 1h/4h/24h、ETH 相对 BTC 的 1h/4h 强弱、`btc_leader_regime` 和 `eth_btc_rotation_score`。
- `rotation_lag` / `leader_pullback` 表示 BTC 震荡或轻微回踩但 ETH 相对走强；这类结构可避免 ETH 多头被误判为 BTC 强冲突，但不能单独触发开仓或满仓。
- `leader_downtrend` / `distribution_risk` 表示 BTC 明确破位或分配风险，仍必须限制 ETH 多头仓位。ETH 补涨假设不能覆盖系统性风险。
- 形态确认只用于缩放仓位和解释，不得绕过本地策略信号。
- 高影响同向新闻优先缩仓，不应因为“重大新闻”四个字自动 block；只有方向冲突、订单流冲突、密集区突破质量极差、流动性/监管/交易所风险等执行风险同时恶化时，才允许 block。

## 2026-06-26 因子排序五档仓位口径

以后以本节为准：五档仓位的正向加分顺序按当前 ETH 1h 纯策略、历史订单流 proxy 研究和实时消息面执行约束重排，忽略旧版近似等权、把订单流方向同向直接视为强方向确认、或把低新闻风险误当成利多/利空确认的方案。

当前权重：

- `orderflow_confirmation_score`: 20%
- `technical_signal_score`: 18%
- `news_direction_alignment_score`: 14%
- `pattern_confirmation_score`: 12%
- `range_safety_score`: 11%
- `trend_confirmation_score`: 10%
- `dense_zone_breakout_score`: 8%
- `news_safety_score`: 4%
- `btc_leader_score`: 2%
- `eth_btc_rotation_score`: 1%

订单流确认分的语义是市场参与度、流动性深度、冲击质量和大单活跃度，不是简单 CVD 方向。强订单流可以提高档位，但不能单独满仓；满仓必须同时通过形态、密集区、低震荡风险、置信度和 RiskManager 硬风控。

新闻必须拆成两项：

- `news_direction_alignment_score`：新闻/背景方向相对本地策略方向的确认分，做空+利空或做多+利多才可加分。
- `news_safety_score = 1 - news_risk_score`：执行风险安全度，低权重参与分数，但高 `news_risk_score` 仍通过 cap 降仓或阻断。

新闻缺完整历史归档，因此不能单独作为强统计 alpha；它的生产角色是实时方向确认与事件执行风险约束。

## 2026-06-18 Walk-forward Proposal Boundary

以后以本节为准：参数寻优和 walk-forward 自动学习只能生成可审计提案，不能自动改实盘参数。

- `walk_forward_parameter_proposal` records baseline, validation metrics, proposed parameter diff, acceptance reasons and risks.
- Passing validation creates `status=needs_review`, not `pending`; current approval flow cannot accidentally apply it.
- Failing validation creates `status=rejected` with explicit `acceptance.risks`.
- Console must show these proposals in a dedicated walk-forward module.
- Any future auto-apply path requires a new ADR, a separate approval workflow, small-position forward test, and rollback plan.

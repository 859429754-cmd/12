from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from ai_quant_trader.core.config import load_config
from ai_quant_trader.core.control import RuntimeControlManager
from ai_quant_trader.core.models import NewsDigest, OrderRequest, SecretService, SecretUpdateCommand, Side
from ai_quant_trader.core.secrets import SecretCommandError, SecretUpdateManager
from ai_quant_trader.core.state import RuntimeState
from ai_quant_trader.data.market import MarketDataClient
from ai_quant_trader.execution.gateway import create_exchange_gateway, execution_mode_from_config
from ai_quant_trader.execution.lifecycle import OrderLifecycleManager, OrderRejected, OrderSubmissionUncertain
from ai_quant_trader.monitoring.metrics import collect_runtime_metrics, metrics_to_prometheus
from ai_quant_trader.platform.profiles import build_strategy_profile
from ai_quant_trader.storage.sqlite import SQLiteStore
from ai_quant_trader.strategy.indicators import atr
from ai_quant_trader.strategy.trend_state import TrendStateStore
from ai_quant_trader.strategy.lab import (
    StrategyCodeError,
    activate_strategy,
    backtest_custom_strategy,
    backtest_trend_strategy,
    backtest_trend_strategy_ai_proxy,
    delete_strategy,
    deactivate_strategy,
    get_active_strategy,
    list_strategy_versions,
    optimize_trend_parameters,
    save_strategy_code,
    validate_strategy_code,
)


class ConsoleAction(BaseModel):
    operator_id: str = Field(default="console")
    symbols: list[str] = Field(default_factory=list)


class ProposalAction(BaseModel):
    operator_id: str = Field(default="console")


class ParameterProposalRequest(BaseModel):
    operator_id: str = Field(default="console")
    path: str
    value: float | int
    symbols: list[str] = Field(default_factory=list)


class ManualSmallEntryRequest(BaseModel):
    operator_id: str = Field(default="console")
    symbol: str
    side: Literal["long", "short"]


class TradeModeRequest(BaseModel):
    operator_id: str = Field(default="console")
    mode: Literal["strategy_confirmed", "ai_candidate_approval", "pure_ai_paper"] | None = None
    trade_mode: Literal["strategy_confirmed", "ai_candidate_approval", "pure_ai_paper"] | None = None

    @model_validator(mode="after")
    def normalize_mode(self) -> "TradeModeRequest":
        self.mode = self.mode or self.trade_mode
        if self.mode is None:
            raise ValueError("缺少交易模式")
        return self


class RuntimeModeRequest(BaseModel):
    operator_id: str = Field(default="console")
    dry_run: bool
    trade_pin: str | None = None


class AccountSecretUpdateRequest(BaseModel):
    operator_id: str = Field(default="console")
    account_slot: Literal["trend", "range"]
    api_key: str = Field(min_length=8)
    api_secret: str = Field(min_length=8)
    exchange: Literal["gateio"] = "gateio"


class RunOnceRequest(BaseModel):
    operator_id: str = Field(default="console")
    symbols: list[str] = Field(default_factory=list)
    equity: float = Field(default=10_000.0, gt=0)


class StrategyCodeRequest(BaseModel):
    operator_id: str = Field(default="console")
    name: str
    description: str = ""
    category: Literal["trend", "range", "grid", "risk_filter", "ai", "research"] = "research"
    code: str


class StrategyActivateRequest(BaseModel):
    operator_id: str = Field(default="console")
    strategy_id: str
    symbols: list[str] = Field(default_factory=list)
    live_enabled: bool = True


class CustomBacktestRequest(BaseModel):
    operator_id: str = Field(default="console")
    strategy_id: str
    symbol: str
    timeframe: str = "1h"
    limit: int = Field(default=700, ge=120, le=50_000)
    data_source: Literal["auto", "gateio", "binance", "okx", "cryptocompare"] = "auto"
    start_date: str | None = Field(default=None)
    end_date: str | None = Field(default=None)
    initial_equity: float = Field(default=10_000.0, gt=0)
    fee_rate: float = Field(default=0.0006, ge=0, le=0.01)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)
    warmup: int = Field(default=120, ge=1, le=500)


class BacktestRequest(BaseModel):
    operator_id: str = Field(default="console")
    symbol: str
    timeframe: str = "1h"
    limit: int = Field(default=700, ge=120, le=50_000)
    data_source: Literal["auto", "gateio", "binance", "okx", "cryptocompare"] = "auto"
    start_date: str | None = Field(default=None)
    end_date: str | None = Field(default=None)
    initial_equity: float = Field(default=200.0, gt=0)
    fee_rate: float = Field(default=0.0006, ge=0, le=0.01)
    slippage_bps: float = Field(default=2.0, ge=0, le=100)
    funding_rate_per_8h: float = Field(default=0.0, ge=0, le=0.01)
    min_order_qty: float = Field(default=0.0, ge=0)
    max_volume_participation: float = Field(default=1.0, gt=0, le=1.0)
    leverage: float = Field(default=4.0, gt=0, le=4.0)
    ai_proxy: bool = False


class BacktestOptimizeRequest(BacktestRequest):
    ema_lengths: list[int] = Field(default_factory=lambda: [55, 89, 100, 144], min_length=1, max_length=8)
    kc_lengths: list[int] = Field(default_factory=lambda: [20], min_length=1, max_length=8)
    kc_scalars: list[float] = Field(default_factory=lambda: [2.0, 2.4, 2.8, 3.2], min_length=1, max_length=8)
    atr_lengths: list[int] = Field(default_factory=lambda: [14], min_length=1, max_length=8)
    vma_lengths: list[int] = Field(default_factory=lambda: [20], min_length=1, max_length=8)
    volume_multiples: list[float] = Field(default_factory=lambda: [2.0, 2.2, 2.5, 2.8, 3.0], min_length=1, max_length=8)
    atr_stop_multiples: list[float] = Field(default_factory=lambda: [1.2, 1.5, 1.8, 2.0], min_length=1, max_length=8)
    position_fractions: list[float] = Field(default_factory=lambda: [0.5], min_length=1, max_length=8)
    use_ema_filters: list[bool] = Field(default_factory=lambda: [False], min_length=1, max_length=2)
    use_volume_filters: list[bool] = Field(default_factory=lambda: [True], min_length=1, max_length=2)
    momentum_filters: list[Literal["none", "kdj"]] = Field(
        default_factory=lambda: ["kdj"],
        min_length=1,
        max_length=2,
    )
    kdj_lengths: list[int] = Field(default_factory=lambda: [9], min_length=1, max_length=8)
    validation_ratio: float = Field(default=0.3, ge=0.15, le=0.5)
    min_trades: int = Field(default=20, ge=1, le=500)
    max_candidates: int = Field(default=120, ge=1, le=512)
    top_n: int = Field(default=10, ge=1, le=50)


class ClosePositionRequest(BaseModel):
    operator_id: str = Field(default="console")
    symbol: str | None = None


class ConsoleContext:
    def __init__(self, config_path: str = "config/config.yaml"):
        self.config_path = config_path
        self.store: SQLiteStore | None = None
        self.reload()

    def reload(self) -> None:
        old_store = getattr(self, "store", None)
        if old_store is not None:
            old_store.close()
        self.config = load_config(self.config_path)
        self.store = SQLiteStore(self.config.runtime.database_path, self.config.runtime.audit_log_path)
        self.control = RuntimeControlManager(self.store, self.config_path)

    def close(self) -> None:
        if self.store is not None:
            self.store.close()
            self.store = None

    def configured_symbols(self) -> list[str]:
        return [item.symbol for item in self.config.symbols]

    def runtime_state(self) -> RuntimeState:
        return self.control.load_state(self.configured_symbols())

    def table(self, table: str, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.store is None:
            raise RuntimeError("store_closed")
        return self.store.fetch_payloads(table, limit=limit, symbol=symbol)


def create_app(config_path: str = "config/config.yaml") -> FastAPI:
    @asynccontextmanager
    async def lifespan(api_app: FastAPI):
        api_app.state.ctx = ConsoleContext(config_path)
        try:
            yield
        finally:
            api_app.state.ctx.close()

    app = FastAPI(title="AI Quant Trader Console API", version="0.4.0", lifespan=lifespan)
    app.state.ctx = ConsoleContext(config_path)
    app.state.backtest_jobs = {}
    app.state.news_refresh_lock = asyncio.Lock()

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_console_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def console_basic_auth(request: Request, call_next):
        if not _console_basic_auth_enabled() or request.url.path == "/api/health":
            return await call_next(request)
        if _valid_console_basic_auth(request.headers.get("authorization")):
            return await call_next(request)
        return Response(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="AI Quant Console"'},
        )

    assets_path = Path("console/dist/assets")
    if assets_path.exists():
        app.mount("/assets", StaticFiles(directory=assets_path), name="console-assets")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "service": "ai-quant-console"}

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        symbols = ctx.configured_symbols()
        deepseek_ready = bool(os.getenv("DEEPSEEK_API_KEY"))
        execution_mode = execution_mode_from_config(ctx.config)
        is_mock = execution_mode == "mock"
        return {
            "mode": "模拟运行" if is_mock else "真实运行",
            "dry_run": is_mock,
            "execution_mode": execution_mode,
            "trade_mode": ctx.config.runtime.trade_mode,
            "opening_paused": state.opening_paused,
            "enabled_symbols": sorted(state.enabled_symbols),
            "report_symbols": sorted(state.report_symbols),
            "major_news_only": state.major_news_only,
            "risk": ctx.config.risk.model_dump(mode="json"),
            "ai": {
                **ctx.config.ai.model_dump(mode="json"),
                "api_key_configured": deepseek_ready,
                "status_message": "DeepSeek 已接入" if deepseek_ready else "本地未配置 DeepSeek API，AI 将使用保守降级决策。",
            },
            "symbols": [item.model_dump(mode="json") for item in ctx.config.symbols],
            "latest_decisions": {symbol: ctx.store.fetch_latest("ai_decisions", symbol) for symbol in symbols},
            "latest_orderflow": {symbol: ctx.store.fetch_latest("orderflow_summaries", symbol) for symbol in symbols},
            "exchange_safety": ctx.store.fetch_latest("exchange_health"),
            "latest_order_lifecycle": ctx.store.fetch_latest("order_lifecycle"),
            "latest_data_health": ctx.store.fetch_latest("data_health"),
            "latest_ai_drift": ctx.store.fetch_latest("ai_drift_checks"),
            "latest_news_risk_review": ctx.store.fetch_latest("news_risk_reviews"),
            "latest_worker_heartbeats": _worker_heartbeat_rows(ctx),
            "latest_maintenance": ctx.store.fetch_latest("maintenance_runs"),
        }

    @app.get("/api/system/readiness")
    def system_readiness() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        symbols = ctx.configured_symbols()
        raw_config = ctx.control.read_config()
        profiles = [_strategy_profile(ctx, raw_config, symbol, state) for symbol in symbols]
        enabled_profiles = [profile for profile in profiles if profile.get("enabled")]
        authorized_profiles = [profile for profile in profiles if profile.get("opening_authorized")]
        live_ready_profiles = [profile for profile in profiles if profile.get("live_ready")]
        execution_mode = execution_mode_from_config(ctx.config)
        deepseek_ready = bool(os.getenv("DEEPSEEK_API_KEY"))
        latest_news = ctx.store.fetch_latest("news_summaries")
        latest_backtest = ctx.store.fetch_latest("backtest_runs")
        latest_exchange = ctx.store.fetch_latest("exchange_health")
        latest_reconciliation = ctx.store.fetch_latest("reconciliation_runs")
        latest_order_lifecycle = ctx.store.fetch_latest("order_lifecycle")
        latest_data_health = ctx.store.fetch_latest("data_health")
        latest_ai_drift = ctx.store.fetch_latest("ai_drift_checks")
        latest_news_risk = ctx.store.fetch_latest("news_risk_reviews")
        latest_worker_heartbeats = _worker_heartbeat_rows(ctx)
        latest_maintenance = ctx.store.fetch_latest("maintenance_runs")
        exchange_payload = (latest_exchange or {}).get("payload") or {}
        exchange_status = str(exchange_payload.get("status") or ("ok" if execution_mode == "mock" else "blocked"))
        exchange_ok = execution_mode == "mock" or _latest_exchange_safety_allows_new_entries(ctx)
        reconciliation_ok = execution_mode == "mock" or _latest_payload_status_fresh(ctx, latest_reconciliation, "ok")
        data_health_status = str(((latest_data_health or {}).get("payload") or {}).get("status") or "warn")
        ai_drift_status = str(((latest_ai_drift or {}).get("payload") or {}).get("status") or "warn")
        worker_status, worker_detail = _worker_heartbeat_status(ctx, latest_worker_heartbeats)
        maintenance_status, maintenance_detail = _maintenance_status(latest_maintenance)
        checks = [
            _readiness_check("database", "Database", "ok", "SQLite store is reachable."),
            _readiness_check(
                "strategy_profiles",
                "Strategy profiles",
                "ok" if enabled_profiles else "block",
                f"{len(enabled_profiles)}/{len(profiles)} profiles enabled.",
            ),
            _readiness_check(
                "opening_authorization",
                "Opening authorization",
                "ok" if authorized_profiles else "block",
                f"{len(authorized_profiles)}/{len(profiles)} symbols authorized for opening.",
            ),
            _readiness_check(
                "opening_pause",
                "Opening pause",
                "block" if state.opening_paused else "ok",
                "Opening is paused." if state.opening_paused else "Opening is enabled.",
            ),
            _readiness_check(
                "deepseek",
                "DeepSeek",
                "ok" if deepseek_ready else "warn",
                "DeepSeek API key is configured." if deepseek_ready else "DeepSeek API key is missing; AI decisions will degrade.",
            ),
            _readiness_check(
                "risk",
                "Risk limits",
                "ok" if ctx.config.risk.max_total_leverage <= 4 else "block",
                f"Max total leverage: {ctx.config.risk.max_total_leverage}x.",
            ),
            _readiness_check(
                "news",
                "News cache",
                "ok" if latest_news else "warn",
                _freshness_message(latest_news, "Latest news cache"),
                age_minutes=_row_age_minutes(latest_news),
            ),
            _readiness_check(
                "backtest_audit",
                "Backtest audit",
                "ok" if latest_backtest else "warn",
                _freshness_message(latest_backtest, "Latest backtest run"),
                age_minutes=_row_age_minutes(latest_backtest),
            ),
            _readiness_check(
                "exchange_safety",
                "Exchange safety",
                "ok" if exchange_ok else "block",
                str(exchange_payload.get("reason") or ("Mock gateway does not require private reconciliation." if execution_mode == "mock" else "Exchange private state is not verified.")),
                age_minutes=_row_age_minutes(latest_exchange),
            ),
            _readiness_check(
                "reconciliation",
                "Exchange reconciliation",
                "ok" if reconciliation_ok else "block",
                _freshness_message(latest_reconciliation, "Latest exchange reconciliation"),
                age_minutes=_row_age_minutes(latest_reconciliation),
            ),
            _readiness_check(
                "data_health",
                "Data freshness",
                data_health_status if data_health_status in {"ok", "warn", "block"} else "warn",
                _freshness_message(latest_data_health, "Latest data freshness check"),
                age_minutes=_row_age_minutes(latest_data_health),
            ),
            _readiness_check(
                "ai_drift",
                "AI drift",
                ai_drift_status if ai_drift_status in {"ok", "warn", "block"} else "warn",
                _freshness_message(latest_ai_drift, "Latest AI drift check"),
                age_minutes=_row_age_minutes(latest_ai_drift),
            ),
            _readiness_check(
                "major_news_review",
                "Major news risk review",
                "ok" if latest_news_risk else "warn",
                _freshness_message(latest_news_risk, "Latest major news risk review"),
                age_minutes=_row_age_minutes(latest_news_risk),
            ),
            _readiness_check(
                "order_lifecycle",
                "Order lifecycle",
                "ok" if latest_order_lifecycle else "warn",
                _freshness_message(latest_order_lifecycle, "Latest order lifecycle event"),
                age_minutes=_row_age_minutes(latest_order_lifecycle),
            ),
            _readiness_check(
                "worker_heartbeat",
                "Worker heartbeat",
                worker_status,
                worker_detail,
            ),
            _readiness_check(
                "runtime_maintenance",
                "Runtime maintenance",
                maintenance_status,
                maintenance_detail,
                age_minutes=_row_age_minutes(latest_maintenance),
            ),
        ]
        if execution_mode == "live" and not deepseek_ready:
            checks.append(
                _readiness_check("live_ai_guard", "Live AI guard", "block", "Live mode requires a configured AI key for the current policy.")
            )
        status_rank = {"ok": 0, "warn": 1, "block": 2}
        overall = max(checks, key=lambda item: status_rank.get(item["status"], 0))["status"] if checks else "block"
        return {
            "overall": overall,
            "execution_mode": execution_mode,
            "trade_mode": ctx.config.runtime.trade_mode,
            "configured_symbols": symbols,
            "enabled_symbols": sorted(state.enabled_symbols),
            "profile_count": len(profiles),
            "enabled_profile_count": len(enabled_profiles),
            "authorized_profile_count": len(authorized_profiles),
            "live_ready_profile_count": len(live_ready_profiles),
            "deepseek_ready": deepseek_ready,
            "exchange_safety": latest_exchange,
            "latest_reconciliation": latest_reconciliation,
            "latest_order_lifecycle": latest_order_lifecycle,
            "latest_data_health": latest_data_health,
            "latest_ai_drift": latest_ai_drift,
            "latest_news_risk_review": latest_news_risk,
            "latest_worker_heartbeats": latest_worker_heartbeats,
            "latest_maintenance": latest_maintenance,
            "checks": checks,
        }

    @app.get("/api/system/metrics")
    def system_metrics() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        readiness = system_readiness()
        return collect_runtime_metrics(
            store=ctx.store,
            database_path=ctx.config.runtime.database_path,
            audit_log_path=ctx.config.runtime.audit_log_path,
            readiness=readiness,
        )

    @app.get("/metrics")
    def prometheus_metrics() -> Response:
        ctx = _ctx(app)
        ctx.reload()
        readiness = system_readiness()
        metrics = collect_runtime_metrics(
            store=ctx.store,
            database_path=ctx.config.runtime.database_path,
            audit_log_path=ctx.config.runtime.audit_log_path,
            readiness=readiness,
        )
        return Response(metrics_to_prometheus(metrics), media_type="text/plain; version=0.0.4")

    @app.get("/api/workbench")
    def workbench() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        news_sources = ctx.config.news.model_dump(mode="json")
        return {
            "decision_policy": {
                "name": "AI 五分制仓位分档 + 多策略模块 + 硬风控",
                "technical_weight": None,
                "ai_context_weight": None,
                "score_fields": [
                    "technical_signal_score",
                    "trend_confirmation_score",
                    "range_safety_score",
                    "news_safety_score",
                    "orderflow_confirmation_score",
                    "dense_zone_breakout_score",
                ],
                "position_tiers": {
                    "block": 0.0,
                    "weak": 0.25,
                    "normal": 0.5,
                    "strong": 0.75,
                    "full": 1.0,
                },
                "hard_rules": [
                    "策略确认模式：没有本地技术信号时，AI 不能自动开仓。",
                    "AI 候选审批模式：AI 置信度超过 65% 但策略未触发时，只能生成候选计划等待人工审批。",
                    "纯 AI 纸面模式：只做模拟研究和候选计划，不允许直接实盘下单。",
                    "重大新闻触发独立 AI 风险复评和审计，默认不直接开新仓。",
                    "冷启动锁、逐标的授权、4 倍总杠杆硬上限永远优先。",
                    "同方向已有持仓时禁止重复加仓。",
                ],
                "ai_inputs": [
                    "1 小时 K 线与趋势策略信号",
                    "多交易所订单流聚合",
                    "交易密集区 VPVR/POC/VAH/VAL",
                    "震荡形态与趋势突破形态",
                    "宏观、政治、央行、地缘、美元、美债、原油、黄金、加密消息",
                    "7 天长期影响消息记忆和交易知识库",
                ],
            },
            "model_policy": {
                "hourly_decision_model": ctx.config.ai.decision_model,
                "hourly_report_model": ctx.config.ai.report_model,
                "emergency_screening_model": ctx.config.ai.emergency_screening_model,
                "emergency_decision_model": ctx.config.ai.emergency_decision_model,
            },
            "source_catalog": {
                "rss_sources": news_sources.get("rss_sources", []),
                "scrape_sources": news_sources.get("scrape_sources", []),
                "jin10_enabled": news_sources.get("jin10_enabled", False),
                "jin10_public_url": news_sources.get("jin10_public_url"),
                "orderflow_exchanges": ctx.config.orderflow.exchanges,
                "execution_exchange": "Gate.io USDT 永续",
            },
            "product_blueprint": [
                {"module": "AI 市场状态工作台", "status": "第一版", "goal": "趋势、震荡、混沌、事件风险分层展示。"},
                {"module": "策略工坊", "status": "第一版", "goal": "趋势策略已接入，震荡策略预留布林带、网格、箱体三类。"},
                {"module": "实时唤醒引擎", "status": "骨架", "goal": "价格异动和重大消息触发 Flash 快筛，重要事件升级 Pro。"},
                {"module": "AI 交易知识库", "status": "骨架", "goal": "沉淀交易员思维、策略说明、宏观规则、风控原则。"},
                {"module": "候选交易计划", "status": "骨架", "goal": "高置信 AI 计划进入人工审批，不直接越权。"},
            ],
            "open_source_inspirations": [
                "Freqtrade：REST API、Pair Locks、强制进出场和绩效视图。",
                "Hummingbot：Controller/Executor 分层、策略配置、实例管理。",
                "OpenBB：组件化研究工作台、新闻聚合、研究报告式布局。",
            ],
        }

    @app.get("/api/platform/overview")
    def platform_overview() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        raw_config = ctx.control.read_config()
        profiles = [_strategy_profile(ctx, raw_config, symbol, state) for symbol in ctx.configured_symbols()]
        return {
            "platform": {
                "shell": "quantdinger_style",
                "core": "local_ai_quant_trader",
                "execution_mode": execution_mode_from_config(ctx.config),
                "trade_mode": ctx.config.runtime.trade_mode,
                "notification_channels": [],
                "agent_gateway": _agent_gateway_status(),
            },
            "workspaces": [
                {"id": "dashboard", "label": "Dashboard"},
                {"id": "market", "label": "Market"},
                {"id": "strategy", "label": "Strategy"},
                {"id": "ai", "label": "AI Brain"},
                {"id": "agent", "label": "Agent Gateway"},
                {"id": "execution", "label": "Execution"},
                {"id": "data", "label": "Data Health"},
            ],
            "strategy_channels": _strategy_channels(ctx),
            "strategy_profiles": profiles,
            "latest_backtest_runs": ctx.table("backtest_runs", limit=8),
            "latest_ai_review_runs": ctx.table("ai_review_runs", limit=8),
        }

    @app.get("/api/agent/v1/health")
    def agent_health(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        ctx = _ctx(app)
        agent = _require_agent_scope(ctx, authorization, "R")
        _audit_agent_call(ctx, agent, "GET /api/agent/v1/health", "R", "ok")
        return {
            "ok": True,
            "gateway": "agent/v1",
            "agent_id": agent["agent_id"],
            "paper_only": True,
            "live_trading": "denied",
            "capabilities": ["read", "backtest"],
        }

    @app.get("/api/agent/v1/strategy-profiles")
    def agent_strategy_profiles(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        agent = _require_agent_scope(ctx, authorization, "R")
        state = ctx.runtime_state()
        raw_config = ctx.control.read_config()
        profiles = [_strategy_profile(ctx, raw_config, symbol, state) for symbol in ctx.configured_symbols()]
        _audit_agent_call(ctx, agent, "GET /api/agent/v1/strategy-profiles", "R", "ok")
        return {
            "items": profiles,
            "paper_only": True,
            "live_trading": "denied",
        }

    @app.post("/api/agent/v1/backtests")
    async def agent_start_backtest(
        body: BacktestRequest,
        authorization: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        agent = _require_agent_scope(ctx, authorization, "B")
        if not idempotency_key or len(idempotency_key.strip()) < 8:
            _audit_agent_call(ctx, agent, "POST /api/agent/v1/backtests", "B", "rejected", reason="missing_idempotency_key")
            raise HTTPException(status_code=400, detail="Idempotency-Key is required for agent backtests.")
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        body.operator_id = f"agent:{agent['agent_id']}"
        job_id = _agent_job_id(idempotency_key)
        existing = app.state.backtest_jobs.get(job_id)
        if existing:
            _audit_agent_call(ctx, agent, "POST /api/agent/v1/backtests", "B", "replayed", job_id=job_id, idempotency_key=idempotency_key)
            return {"ok": True, "job_id": job_id, "status": existing["status"], "idempotent_replay": True}
        app.state.backtest_jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "Agent paper backtest queued.",
            "symbol": body.symbol,
            "result": None,
            "error": None,
            "source": "agent_gateway",
            "agent_id": agent["agent_id"],
            "idempotency_key": idempotency_key,
        }
        _audit_agent_call(ctx, agent, "POST /api/agent/v1/backtests", "B", "queued", job_id=job_id, idempotency_key=idempotency_key)
        asyncio.create_task(_run_trend_backtest_job(app, job_id, body))
        return {"ok": True, "job_id": job_id, "paper_only": True, "message": "Agent paper backtest started."}

    @app.get("/api/agent/v1/backtests/{job_id}")
    def agent_get_backtest_job(job_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
        ctx = _ctx(app)
        agent = _require_agent_scope(ctx, authorization, "R")
        job = app.state.backtest_jobs.get(job_id)
        if not job or job.get("source") != "agent_gateway":
            _audit_agent_call(ctx, agent, f"GET /api/agent/v1/backtests/{job_id}", "R", "not_found", job_id=job_id)
            raise HTTPException(status_code=404, detail="Agent backtest job not found.")
        _audit_agent_call(ctx, agent, f"GET /api/agent/v1/backtests/{job_id}", "R", "ok", job_id=job_id)
        return job

    @app.get("/api/strategy/profiles")
    def strategy_profiles() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        raw_config = ctx.control.read_config()
        return {"items": [_strategy_profile(ctx, raw_config, symbol, state) for symbol in ctx.configured_symbols()]}

    @app.get("/api/backtest/runs")
    def backtest_runs(limit: int = Query(default=20, ge=1, le=200), symbol: str | None = None) -> dict[str, Any]:
        ctx = _ctx(app)
        return {"items": ctx.table("backtest_runs", limit=limit, symbol=symbol)}

    @app.get("/api/strategy/config")
    def strategy_config() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        raw = ctx.control.read_config()
        effective = {symbol: ctx.control.effective_symbol_params(raw, symbol) for symbol in ctx.configured_symbols()}
        return {
            "global_trend": ctx.config.strategy.trend.model_dump(mode="json"),
            "symbol_params": raw.get("symbol_params", {}),
            "effective_symbol_params": effective,
            "risk": ctx.config.risk.model_dump(mode="json"),
            "range_strategy": {
                "status": "预留模块",
                "enabled": False,
                "description": "震荡策略入口已预留，后续可接入布林带均值回归、网格策略、箱体高抛低吸。",
                "fields": ["启用条件", "箱体边界", "进场触发", "止损规则", "止盈规则", "最大持仓时间", "适用标的"],
            },
        }

    @app.get("/api/news/latest")
    async def latest_news(
        limit: int = Query(default=20, ge=1, le=100),
        max_age_minutes: int = Query(default=65, ge=15, le=24 * 60),
        auto_refresh: bool = Query(default=True),
    ) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        rows = ctx.table("news_summaries", limit=limit)
        latest = rows[0] if rows else None
        if auto_refresh and _news_row_is_stale(latest, max_age_minutes):
            async with app.state.news_refresh_lock:
                ctx.reload()
                rows = ctx.table("news_summaries", limit=limit)
                latest = rows[0] if rows else None
                if _news_row_is_stale(latest, max_age_minutes):
                    digest = await _collect_news_digest(ctx.config_path)
                    rows = ctx.table("news_summaries", limit=limit)
                    return _news_latest_response(rows, digest, max_age_minutes)
        return _news_latest_response(rows, None, max_age_minutes)

    @app.post("/api/news/refresh")
    async def refresh_news(body: ProposalAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        digest = await _collect_news_digest(ctx.config_path)
        return {
            "ok": True,
            "message": f"已刷新新闻快讯，共 {len(digest.items)} 条。",
            "digest": digest.model_dump(mode="json"),
        }

    @app.get("/api/news/memory")
    def news_memory() -> dict[str, Any]:
        path = Path("data/news_memory.json")
        if not path.exists():
            return {"items": [], "message": "暂未生成 7 天消息面记忆。"}
        import json

        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            items = []
        return {"path": str(path), "items": items[:80]}

    @app.get("/api/markets/symbols")
    def market_symbols() -> dict[str, Any]:
        ctx = _ctx(app)
        configured = ctx.configured_symbols()
        enabled_ai = set(ctx.config.ai.ai_enabled_symbols or configured)
        items = []
        for item in ctx.config.symbols:
            base, _, rest = item.symbol.partition("/")
            quote = rest.split(":", 1)[0] if rest else "USDT"
            items.append(
                {
                    "symbol": item.symbol,
                    "base": base,
                    "quote": quote,
                    "timeframe": item.timeframe,
                    "leverage": item.leverage,
                    "configured": True,
                    "ai_enabled": item.symbol in enabled_ai,
                    "data_enabled": True,
                    "strategy_enabled": True,
                }
            )
        return {"exchange": "Gate.io USDT永续配置标的", "items": items}

    @app.get("/api/market/candles")
    async def market_candles(
        symbol: str,
        timeframe: str = "1h",
        limit: int = Query(default=5000, ge=120, le=20_000),
        source: Literal["auto", "gateio", "binance", "okx", "cryptocompare"] = "auto",
    ) -> dict[str, Any]:
        market = MarketDataClient()
        try:
            candles = await market.fetch_ohlcv(symbol, timeframe, limit=limit, source=source)
        finally:
            await market.close()
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": candles.attrs.get("data_source", "unknown"),
            "warning": candles.attrs.get("data_warning", ""),
            "items": _candles_for_chart(candles, max_points=limit),
        }

    @app.get("/api/account/balance")
    async def account_balance(account_slot: Literal["default", "trend", "range"] = "trend") -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        execution = create_exchange_gateway(ctx.config, account_slot=account_slot)
        try:
            summary = await execution.fetch_balance_summary()
            return {"dry_run": execution_mode_from_config(ctx.config) == "mock", **summary}
        finally:
            await execution.close()

    @app.get("/api/execution/accounts")
    def execution_accounts() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        return {
            "items": _execution_account_slots(ctx),
            "note": "Account slots store masked API-key fingerprints only. Live routing still requires Trade PIN, authorization, and risk checks.",
        }

    @app.get("/api/strategy/channels")
    def strategy_channels() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        return {"items": _strategy_channels(ctx)}

    @app.post("/api/execution/accounts/secret")
    async def update_execution_account_secret(body: AccountSecretUpdateRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        if ctx.store is None:
            raise HTTPException(status_code=500, detail="store_closed")
        service = SecretService.GATEIO_TREND if body.account_slot == "trend" else SecretService.GATEIO_RANGE
        env_key, env_secret = SecretUpdateManager.KEY_MAP[service]
        manager = SecretUpdateManager(
            ctx.store,
            ctx.config.security.runtime_env_path,
            admin_user_ids=ctx.config.security.admin_user_ids,
        )
        command = SecretUpdateCommand(
            service=service,
            values={env_key: body.api_key, env_secret: body.api_secret},
            operator_id=body.operator_id,
        )
        try:
            record = await manager.apply_command(command)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except SecretCommandError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "account_slot": body.account_slot,
            "service": service.value,
            "version": record.version,
            "key_tail": record.key_tail,
            "secret_tail": record.secret_tail,
            "message": f"{_account_slot_label(body.account_slot)} API 已更新，明文只写入运行密钥文件，审计记录已脱敏。",
        }

    @app.get("/api/positions")
    def positions(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        rows = ctx.table("positions_snapshot", limit=limit)
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            symbol = row.get("symbol") or row["payload"].get("symbol")
            if symbol and symbol not in latest:
                latest[symbol] = row
        return {"items": list(latest.values())}

    @app.get("/api/risk/summary")
    def risk_summary() -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        return {
            "dry_run": execution_mode_from_config(ctx.config) == "mock",
            "trade_mode": ctx.config.runtime.trade_mode,
            "opening_paused": state.opening_paused,
            "enabled_symbols": sorted(state.enabled_symbols),
            "report_symbols": sorted(state.report_symbols),
            "max_total_leverage": ctx.config.risk.max_total_leverage,
            "min_confidence_to_trade": ctx.config.risk.min_confidence_to_trade,
            "ai_candidate_min_confidence": ctx.config.risk.ai_candidate_min_confidence,
            "ai_full_size_confidence": ctx.config.risk.ai_full_size_confidence,
            "small_position_mode": ctx.config.risk.small_position_mode,
            "small_position_notional_usdt": ctx.config.risk.small_position_notional_usdt,
            "rules": [
                "总名义仓位不得超过账户权益的 4 倍。",
                "未授权标的不允许新开仓。",
                "策略确认模式下，AI 不可绕过技术信号自动开仓。",
                "AI 候选交易计划必须审批后才能执行。",
                "同方向已有持仓时禁止重复加仓。",
                "数据质量不足时禁止或降级交易。",
            ],
        }

    @app.get("/api/orders")
    def orders(limit: int = Query(default=50, ge=1, le=200), symbol: str | None = None) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        return {"items": ctx.table("orders", limit=limit, symbol=symbol)}

    @app.get("/api/decisions")
    def decisions(limit: int = Query(default=50, ge=1, le=200), symbol: str | None = None) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        return {"items": ctx.table("ai_decisions", limit=limit, symbol=symbol)}

    @app.get("/api/dense-zones/latest")
    def latest_dense_zone(symbol: str) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([symbol], ctx.configured_symbols(), require_any=True)
        latest = ctx.store.fetch_latest("dense_zones", symbol) if ctx.store else None
        return {"item": latest}

    @app.get("/api/reports")
    def reports(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        return {"items": ctx.table("hourly_reports", limit=limit)}

    @app.get("/api/proposals")
    def proposals(
        status: Literal["all", "pending", "approved", "rejected"] = "all",
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        rows = ctx.table("optimization_proposals", limit=limit)
        if status != "all":
            rows = [row for row in rows if row["payload"].get("status") == status]
        return {"items": rows}

    @app.get("/api/strategy-lab/versions")
    def strategy_versions() -> dict[str, Any]:
        return {"items": list_strategy_versions(), "active": get_active_strategy()}

    @app.post("/api/strategy-lab/save")
    def save_strategy(body: StrategyCodeRequest) -> dict[str, Any]:
        try:
            meta = save_strategy_code(body.name, body.code, body.description, body.category)
        except StrategyCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": f"策略版本已保存：{meta['name']}（{meta['category_label']}，{meta['id']}）。当前不会自动接入实盘。", "item": meta}

    @app.post("/api/strategy-lab/validate")
    def validate_strategy(body: StrategyCodeRequest) -> dict[str, Any]:
        try:
            result = validate_strategy_code(body.code)
        except StrategyCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": result["message"], "validation": result}

    @app.post("/api/strategy-lab/activate")
    def activate_lab_strategy(body: StrategyActivateRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols()) or ctx.configured_symbols()
        try:
            active = activate_strategy(body.strategy_id, symbols, body.operator_id, body.live_enabled)
        except StrategyCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": f"已激活策略：{active['name']}。作用标的：{', '.join(symbols)}。", "active": active}

    @app.post("/api/strategy-lab/deactivate")
    def deactivate_lab_strategy() -> dict[str, Any]:
        deactivate_strategy()
        return {"ok": True, "message": "已停用策略实验室自定义策略，恢复默认趋势策略。"}

    @app.delete("/api/strategy-lab/versions/{strategy_id}")
    def delete_lab_strategy(strategy_id: str) -> dict[str, Any]:
        try:
            delete_strategy(strategy_id)
        except StrategyCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": f"已删除策略版本：{strategy_id}。"}

    @app.post("/api/backtest/trend")
    async def run_trend_backtest(body: BacktestRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        market = MarketDataClient()
        try:
            candles = await _fetch_backtest_candles(market, body)
        finally:
            await market.close()
        raw_config = ctx.control.read_config()
        trend_config = ctx.control.trend_config_for_symbol(raw_config, body.symbol)
        runner = backtest_trend_strategy_ai_proxy if body.ai_proxy else backtest_trend_strategy
        result = await asyncio.to_thread(
            runner,
            candles,
            symbol=body.symbol,
            timeframe=body.timeframe,
            config=trend_config,
            initial_equity=body.initial_equity,
            fee_rate=body.fee_rate,
            slippage_bps=body.slippage_bps,
            leverage=body.leverage,
            funding_rate_per_8h=body.funding_rate_per_8h,
            min_order_qty=body.min_order_qty,
            max_volume_participation=body.max_volume_participation,
        )
        result["market_data_source"] = candles.attrs.get("data_source", "unknown")
        result["market_data_warning"] = candles.attrs.get("data_warning", "")
        result["chart_candles"] = _candles_for_chart(candles, max_points=5000)
        _record_backtest_run(ctx, "trend_backtest", body, result, job_id=None)
        ctx.store.insert("hourly_reports", {"type": "backtest", "result": result}, symbol=body.symbol)
        return {"ok": True, "message": f"{body.symbol} 回测完成，交易 {result['trade_count']} 笔。", "result": result}

    @app.post("/api/backtest/trend/job")
    async def start_trend_backtest_job(body: BacktestRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        job_id = uuid.uuid4().hex[:12]
        app.state.backtest_jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "回测任务已进入队列。",
            "symbol": body.symbol,
            "result": None,
            "error": None,
        }
        asyncio.create_task(_run_trend_backtest_job(app, job_id, body))
        return {"ok": True, "job_id": job_id, "message": "深度回测后台任务已启动。"}

    @app.get("/api/backtest/jobs/{job_id}")
    def get_backtest_job(job_id: str) -> dict[str, Any]:
        job = app.state.backtest_jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="回测任务不存在")
        return job

    @app.post("/api/backtest/trend/optimize/job")
    async def start_trend_parameter_optimization_job(body: BacktestOptimizeRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        job_id = uuid.uuid4().hex[:12]
        app.state.backtest_jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "参数寻优任务已进入队列。",
            "symbol": body.symbol,
            "result": None,
            "error": None,
        }
        asyncio.create_task(_run_trend_parameter_optimization_job(app, job_id, body))
        return {"ok": True, "job_id": job_id, "message": "趋势策略参数寻优任务已启动。"}

    @app.post("/api/backtest/custom")
    async def run_custom_backtest(body: CustomBacktestRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        market = MarketDataClient()
        try:
            candles = await _fetch_backtest_candles(market, body)
        finally:
            await market.close()
        try:
            result = backtest_custom_strategy(
                candles,
                symbol=body.symbol,
                timeframe=body.timeframe,
                strategy_id=body.strategy_id,
                initial_equity=body.initial_equity,
                fee_rate=body.fee_rate,
                slippage_bps=body.slippage_bps,
                warmup=body.warmup,
            )
        except StrategyCodeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result["market_data_source"] = candles.attrs.get("data_source", "unknown")
        result["market_data_warning"] = candles.attrs.get("data_warning", "")
        result["chart_candles"] = _candles_for_chart(candles, max_points=5000)
        _record_backtest_run(ctx, "custom_backtest", body, result, job_id=None)
        ctx.store.insert("hourly_reports", {"type": "custom_backtest", "result": result}, symbol=body.symbol)
        return {"ok": True, "message": f"{body.symbol} 自定义策略回测完成，交易 {result['trade_count']} 笔。", "result": result}

    @app.post("/api/proposals/{proposal_id}/approve")
    def approve_proposal(proposal_id: int, body: ProposalAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        try:
            message = ctx.control.approve_proposal(proposal_id, body.operator_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "message": message}

    @app.post("/api/proposals/{proposal_id}/reject")
    def reject_proposal(proposal_id: int, body: ProposalAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        return {"ok": True, "message": ctx.control.reject_proposal(proposal_id, body.operator_id)}

    @app.post("/api/proposals/parameter")
    def create_parameter_proposal(body: ParameterProposalRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols())
        config = ctx.control.read_config()
        changes: dict[str, dict[str, Any]] = {}
        if symbols:
            short_key = body.path.split(".")[-1]
            for symbol in symbols:
                path = f"symbol_params.{symbol}.{short_key}"
                new_value = ctx.control.cast_and_validate(path, body.value)
                changes[path] = {
                    "old": ctx.control.effective_symbol_params(config, symbol).get(short_key),
                    "new": new_value,
                    "reason": "控制台参数热更新",
                }
        else:
            new_value = ctx.control.cast_and_validate(body.path, body.value)
            changes[body.path] = {
                "old": ctx.control.get_config_value(config, body.path),
                "new": new_value,
                "reason": "控制台参数热更新",
            }
        proposal_id = ctx.store.insert(
            "optimization_proposals",
            {
                "type": "parameter_update",
                "status": "pending",
                "operator_id": body.operator_id,
                "summary": "控制台策略参数热更新",
                "changes": changes,
                "source": "console",
            },
            symbol="parameter_update",
        )
        return {"ok": True, "proposal_id": proposal_id, "message": f"已创建参数提案 #{proposal_id}，审批后生效。"}

    @app.post("/api/proposals/manual-small-entry")
    def manual_small_entry(body: ManualSmallEntryRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        proposal_id = ctx.control.create_manual_small_entry_proposal(body.operator_id, body.symbol, body.side)
        side_text = "做多" if body.side == "long" else "做空"
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "message": f"已创建 {body.symbol} 最小仓位{side_text}测试提案 #{proposal_id}。请到审批页同意后执行。",
        }

    @app.post("/api/manual-small-entry/execute")
    async def execute_manual_small_entry(body: ManualSmallEntryRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        _validate_symbols([body.symbol], ctx.configured_symbols(), require_any=True)
        return await _execute_manual_small_entry(ctx, body.symbol, body.side)

    @app.post("/api/control/trade-mode")
    def set_trade_mode(body: TradeModeRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        config = ctx.control.read_config()
        ctx.control.set_config_value(config, "runtime.trade_mode", body.mode)
        ctx.control.write_config(config)
        labels = {
            "strategy_confirmed": "策略确认模式",
            "ai_candidate_approval": "AI 候选审批模式",
            "pure_ai_paper": "纯 AI 纸面研究模式",
        }
        return {"ok": True, "message": f"已切换为{labels[body.mode]}。"}

    @app.post("/api/control/runtime-mode")
    def set_runtime_mode(body: RuntimeModeRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        target_mode = "mock" if body.dry_run else "live"
        if target_mode == "live":
            expected_pin = os.getenv("TRADE_PIN", "").strip()
            if not expected_pin:
                raise HTTPException(status_code=403, detail="后端未配置 TRADE_PIN，禁止从控制台切换真实运行。")
            if str(body.trade_pin or "").strip() != expected_pin:
                raise HTTPException(status_code=403, detail="Trade PIN 校验失败，禁止切换真实运行。")
            if not _account_slot_configured("trend"):
                raise HTTPException(status_code=403, detail="趋势策略账号未配置 Gate API Key/Secret，禁止切换真实运行。")

        config = ctx.control.read_config()
        ctx.control.set_config_value(config, "runtime.execution_mode", target_mode)
        ctx.control.set_config_value(config, "runtime.dry_run", target_mode == "mock")
        if target_mode == "mock":
            ctx.control.set_config_value(config, "risk.small_position_mode", False)
        ctx.control.write_config(config)
        mode_text = "模拟运行" if target_mode == "mock" else "真实运行"
        warning = "" if target_mode == "mock" else "；真实订单仍必须满足逐标的授权、允许开仓、AI否决和4倍硬风控"
        return {"ok": True, "message": f"已切换为{mode_text}{warning}"}

    @app.post("/api/control/pause")
    def pause(body: ConsoleAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols())
        return {"ok": True, "message": ctx.control.pause(state, symbols, body.operator_id)}

    @app.post("/api/control/enable-report")
    def enable_report(body: ConsoleAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols(), require_any=True)
        return {"ok": True, "message": ctx.control.enable_symbol_report(state, symbols, body.operator_id)}

    @app.post("/api/control/disable-report")
    def disable_report(body: ConsoleAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols(), require_any=True)
        return {"ok": True, "message": ctx.control.disable_symbol_report(state, symbols, body.operator_id)}

    @app.post("/api/control/authorize")
    def authorize(body: ConsoleAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols(), require_any=True)
        message = ctx.control.authorize_opening(state, symbols, body.operator_id, execution_mode_from_config(ctx.config) == "mock")
        return {"ok": True, "message": message}

    @app.post("/api/control/news-mode")
    def news_mode(enabled: bool, body: ProposalAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        return {"ok": True, "message": ctx.control.set_major_news_only(state, enabled, body.operator_id)}

    @app.post("/api/runtime/run-once")
    async def run_once_now(body: RunOnceRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        symbols = _validate_symbols(body.symbols, ctx.configured_symbols()) or ctx.configured_symbols()
        from ai_quant_trader.app import TradingApp

        trading_app = TradingApp(ctx.config_path)
        try:
            for symbol in symbols:
                trading_app.state.enable_report(symbol)
            trading_app.control.save_state(trading_app.state, body.operator_id, "console_run_once_refresh")
            await trading_app.run_once(equity=body.equity)
        finally:
            await trading_app.close()
        return {"ok": True, "message": f"已完成 {', '.join(symbols)} 的即时AI行情判断和消息面抓取。"}

    @app.post("/api/control/close-position")
    async def close_position(body: ClosePositionRequest) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        symbols = [body.symbol] if body.symbol else ctx.configured_symbols()
        _validate_symbols(symbols, ctx.configured_symbols(), require_any=True)
        execution = create_exchange_gateway(ctx.config, account_slot="trend")
        try:
            orders = await execution.close_positions(symbols, reason=f"console_close_{body.operator_id}")
            await _cancel_trend_native_stops(execution, symbols, ctx.store, execution_mode_from_config(ctx.config))
            for order in orders:
                ctx.store.insert("orders", order, order.symbol)
            if not orders:
                return {"ok": True, "message": "没有检测到需要平仓的 Gate 持仓。", "orders": []}
            target = "全部标的" if body.symbol is None else body.symbol
            return {"ok": True, "message": f"已提交 {target} 平仓请求，共 {len(orders)} 笔。", "orders": [o.model_dump(mode="json") for o in orders]}
        finally:
            await execution.close()

    @app.post("/api/control/panic-close")
    async def panic_close(body: ConsoleAction) -> dict[str, Any]:
        ctx = _ctx(app)
        ctx.reload()
        state = ctx.runtime_state()
        ctx.control.pause(state, [], body.operator_id)
        execution = create_exchange_gateway(ctx.config, account_slot="trend")
        try:
            orders = await execution.close_positions(ctx.configured_symbols(), reason=f"console_panic_{body.operator_id}")
            await _cancel_trend_native_stops(execution, ctx.configured_symbols(), ctx.store, execution_mode_from_config(ctx.config))
            for order in orders:
                ctx.store.insert("orders", order, order.symbol)
            return {"ok": True, "message": f"已暂停全部新开仓，并提交一键全平请求 {len(orders)} 笔。", "orders": [o.model_dump(mode="json") for o in orders]}
        finally:
            await execution.close()

    @app.get("/")
    def index() -> FileResponse:
        index_path = Path("console/dist/index.html")
        if not index_path.exists():
            raise HTTPException(status_code=404, detail="前端尚未构建，请先运行 console 前端构建。")
        return FileResponse(index_path)

    return app


def _strategy_profile(ctx: ConsoleContext, raw_config: dict[str, Any], symbol: str, state: RuntimeState) -> dict[str, Any]:
    cfg = ctx.control.trend_config_for_symbol(raw_config, symbol)
    return build_strategy_profile(symbol=symbol, config=cfg, state=state)


def _agent_job_id(idempotency_key: str) -> str:
    import hashlib

    return "agt_" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]


def _agent_gateway_status() -> dict[str, Any]:
    scopes = [
        item.strip().upper()
        for item in os.getenv("AGENT_GATEWAY_SCOPES", "R,B").replace(";", ",").split(",")
        if item.strip()
    ]
    return {
        "enabled": bool(os.getenv("AGENT_GATEWAY_TOKEN", "").strip()),
        "version": "agent/v1",
        "scopes": scopes,
        "paper_only": True,
        "live_trading": "denied",
    }


def _account_slot_configured(slot: str) -> bool:
    env_map = {
        "trend": ("GATEIO_TREND_API_KEY", "GATEIO_TREND_API_SECRET"),
        "range": ("GATEIO_RANGE_API_KEY", "GATEIO_RANGE_API_SECRET"),
        "default": ("GATEIO_API_KEY", "GATEIO_API_SECRET"),
    }
    pair = env_map.get(slot)
    if pair is None:
        return False
    key_env, secret_env = pair
    configured = bool(os.getenv(key_env, "").strip()) and bool(os.getenv(secret_env, "").strip())
    if configured:
        return True
    if slot == "trend":
        fallback_key, fallback_secret = env_map["default"]
        return bool(os.getenv(fallback_key, "").strip()) and bool(os.getenv(fallback_secret, "").strip())
    return False


def _strategy_channels(ctx: ConsoleContext) -> list[dict[str, Any]]:
    state = ctx.runtime_state()
    raw_config = ctx.control.read_config()
    accounts = {item["slot"]: item for item in _execution_account_slots(ctx)}
    symbols = ctx.configured_symbols()
    profiles = [_strategy_profile(ctx, raw_config, symbol, state) for symbol in symbols]
    trend_enabled = [profile for profile in profiles if profile.get("strategy_type") == "trend" and profile.get("enabled")]
    trend_authorized = [profile for profile in trend_enabled if profile.get("opening_authorized")]
    trend_account = accounts.get("trend", {})
    range_account = accounts.get("range", {})
    return [
        {
            "channel": "trend",
            "label": "趋势策略运行",
            "strategy_type": "trend",
            "account_slot": "trend",
            "account_label": trend_account.get("label", _account_slot_label("trend")),
            "enabled": bool(trend_enabled),
            "executable": True,
            "status": "ready" if trend_enabled else "blocked",
            "mode": execution_mode_from_config(ctx.config),
            "opening_paused": state.opening_paused,
            "authorized_symbols": [profile["symbol"] for profile in trend_authorized],
            "configured_symbols": symbols,
            "account_configured": bool(trend_account.get("configured")),
            "gateway_binding": trend_account.get("gateway_binding", "missing_credentials"),
            "live_ready": bool(trend_enabled and trend_authorized and not state.opening_paused and trend_account.get("configured")),
            "ai_sizing_tiers": _ai_sizing_tiers(),
            "notes": [
                "Strategy signal is mandatory before AI can confirm an entry.",
                "DeepSeek confidence is displayed as a percentage but remains subject to RiskManager clipping.",
                "Live execution is routed to the trend Gate.io account slot.",
            ],
        },
        {
            "channel": "range",
            "label": "震荡策略运行",
            "strategy_type": "range",
            "account_slot": "range",
            "account_label": range_account.get("label", _account_slot_label("range")),
            "enabled": False,
            "executable": False,
            "status": "reserved",
            "mode": execution_mode_from_config(ctx.config),
            "opening_paused": True,
            "authorized_symbols": [],
            "configured_symbols": symbols,
            "account_configured": bool(range_account.get("configured")),
            "gateway_binding": range_account.get("gateway_binding", "missing_credentials"),
            "live_ready": False,
            "ai_sizing_tiers": _ai_sizing_tiers(),
            "notes": [
                "Range strategy is intentionally blocked until its own backtest, risk rules, and account gateway are implemented.",
                "It must not inherit the trend strategy's live readiness.",
                "Future candidates: Bollinger mean reversion, grid, and boxed-range high-sell-low-buy.",
            ],
        },
    ]


def _ai_sizing_tiers() -> list[dict[str, Any]]:
    return [
        {"min_confidence_pct": 0, "max_confidence_pct": 54, "label": "阻断", "position_pct": 0},
        {"min_confidence_pct": 55, "max_confidence_pct": 64, "label": "试探", "position_pct": 30},
        {"min_confidence_pct": 65, "max_confidence_pct": 74, "label": "降仓", "position_pct": 55},
        {"min_confidence_pct": 75, "max_confidence_pct": 84, "label": "标准", "position_pct": 80},
        {"min_confidence_pct": 85, "max_confidence_pct": 100, "label": "满仓候选", "position_pct": 100},
    ]


def _execution_account_slots(ctx: ConsoleContext) -> list[dict[str, Any]]:
    slots = [
        {
            "slot": "trend",
            "label": _account_slot_label("trend"),
            "strategy_type": "trend",
            "service": SecretService.GATEIO_TREND,
            "env_key": "GATEIO_TREND_API_KEY",
            "env_secret": "GATEIO_TREND_API_SECRET",
        },
        {
            "slot": "range",
            "label": _account_slot_label("range"),
            "strategy_type": "range",
            "service": SecretService.GATEIO_RANGE,
            "env_key": "GATEIO_RANGE_API_KEY",
            "env_secret": "GATEIO_RANGE_API_SECRET",
        },
    ]
    output: list[dict[str, Any]] = []
    for slot in slots:
        service = slot["service"]
        latest = ctx.store.fetch_secret_versions(service.value, limit=1) if ctx.store else []
        payload = latest[0]["payload"] if latest else {}
        has_env = bool(os.getenv(slot["env_key"], "").strip()) and bool(os.getenv(slot["env_secret"], "").strip())
        uses_legacy_default = False
        if slot["slot"] == "trend" and not has_env:
            uses_legacy_default = _account_slot_configured("default")
            has_env = uses_legacy_default
        configured = bool(latest or has_env)
        key_tail_value = os.getenv(slot["env_key"], "")
        if uses_legacy_default:
            key_tail_value = os.getenv("GATEIO_API_KEY", "")
        output.append(
            {
                "slot": slot["slot"],
                "label": slot["label"],
                "exchange": "gateio",
                "strategy_type": slot["strategy_type"],
                "configured": configured,
                "version": payload.get("version", 0),
                "key_tail": payload.get("key_tail", "******" + key_tail_value[-6:] if has_env else "-"),
                "secret_tail": payload.get("secret_tail", "-"),
                "gateway_binding": "active" if configured else "missing_credentials",
                "live_routing": "ready_after_trade_pin_and_risk_checks" if configured else "blocked_missing_credentials",
                "credential_source": "legacy_default_gateio" if uses_legacy_default else slot["slot"],
            }
        )
    return output


def _account_slot_label(slot: str) -> str:
    if slot == "trend":
        return "账号1：趋势策略"
    if slot == "range":
        return "账号2：震荡策略"
    return slot


def _require_agent_scope(ctx: ConsoleContext, authorization: str | None, required_scope: str) -> dict[str, Any]:
    token = os.getenv("AGENT_GATEWAY_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=503, detail="Agent Gateway is disabled because AGENT_GATEWAY_TOKEN is not configured.")
    supplied = _bearer_token(authorization)
    if supplied != token:
        raise HTTPException(status_code=401, detail="Invalid Agent Gateway token.")
    scopes = {
        item.strip().upper()
        for item in os.getenv("AGENT_GATEWAY_SCOPES", "R,B").replace(";", ",").split(",")
        if item.strip()
    }
    if required_scope.upper() not in scopes:
        raise HTTPException(status_code=403, detail=f"Agent token lacks required scope {required_scope.upper()}.")
    return {
        "agent_id": os.getenv("AGENT_GATEWAY_AGENT_ID", "local-agent").strip() or "local-agent",
        "scopes": sorted(scopes),
    }


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    prefix = "Bearer "
    if authorization.startswith(prefix):
        return authorization[len(prefix) :].strip()
    return authorization.strip()


def _audit_agent_call(
    ctx: ConsoleContext,
    agent: dict[str, Any],
    route: str,
    scope: str,
    status: str,
    *,
    job_id: str | None = None,
    idempotency_key: str | None = None,
    reason: str | None = None,
) -> None:
    ctx.store.insert(
        "agent_audit_events",
        {
            "agent_id": agent.get("agent_id", "unknown"),
            "route": route,
            "scope": scope,
            "status": status,
            "job_id": job_id,
            "idempotency_key_tail": idempotency_key[-8:] if idempotency_key else None,
            "reason": reason,
            "paper_only": True,
            "live_trading": "denied",
        },
        symbol=str(job_id or agent.get("agent_id") or "agent"),
    )


def _record_backtest_run(
    ctx: ConsoleContext,
    run_type: str,
    body: BacktestRequest | BacktestOptimizeRequest | CustomBacktestRequest,
    result: dict[str, Any],
    job_id: str | None,
) -> None:
    request = body.model_dump(mode="json")
    summary = {
        "total_return_pct": result.get("total_return_pct"),
        "max_drawdown_pct": result.get("max_drawdown_pct"),
        "trade_count": result.get("trade_count"),
        "win_rate_pct": result.get("win_rate_pct"),
        "profit_factor": result.get("profit_factor"),
        "final_equity": result.get("final_equity"),
        "market_data_source": result.get("market_data_source"),
        "market_data_warning": result.get("market_data_warning"),
    }
    if run_type == "parameter_optimization":
        best = result.get("best") or {}
        summary.update(
            {
                "searched_candidates": result.get("searched_candidates"),
                "best_params": best.get("params"),
                "best_validation": best.get("validation"),
            }
        )
    payload = {
        "type": run_type,
        "job_id": job_id,
        "symbol": request.get("symbol"),
        "timeframe": request.get("timeframe"),
        "request": request,
        "summary": summary,
        "result": result,
    }
    ctx.store.insert("backtest_runs", payload, symbol=str(request.get("symbol") or ""))


def _readiness_check(
    check_id: str,
    label: str,
    status: Literal["ok", "warn", "block"],
    detail: str,
    age_minutes: float | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "detail": detail,
        "age_minutes": age_minutes,
    }


def _row_age_minutes(row: dict[str, Any] | None) -> float | None:
    if not row:
        return None
    created_at = str(row.get("created_at") or "")
    if not created_at:
        return None
    try:
        parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds() / 60.0, 0.0)


def _freshness_message(row: dict[str, Any] | None, label: str) -> str:
    age = _row_age_minutes(row)
    if age is None:
        return f"{label} is missing."
    if age < 1:
        return f"{label} was updated less than 1 minute ago."
    return f"{label} was updated {age:.1f} minutes ago."


async def _fetch_backtest_candles(market: MarketDataClient, body: BacktestRequest | CustomBacktestRequest):
    if body.start_date or body.end_date:
        candles = await market.fetch_ohlcv_history(
            symbol=body.symbol,
            timeframe=body.timeframe,
            start=body.start_date or None,
            end=body.end_date or None,
            source=body.data_source,
            max_candles=body.limit,
        )
    else:
        candles = await market.fetch_ohlcv(body.symbol, body.timeframe, limit=body.limit, source=body.data_source)
    if candles.attrs.get("data_source") == "synthetic":
        raise HTTPException(status_code=503, detail=f"真实K线数据不可用，已取消回测。{candles.attrs.get('data_warning', '')}")
    return candles


async def _run_trend_backtest_job(app: FastAPI, job_id: str, body: BacktestRequest) -> None:
    job = app.state.backtest_jobs[job_id]
    try:
        job.update({"status": "running", "progress": 10, "message": "正在读取真实K线数据。"})
        ctx = _ctx(app)
        ctx.reload()
        market = MarketDataClient()
        try:
            candles = await _fetch_backtest_candles(market, body)
        finally:
            await market.close()

        job.update({"progress": 65, "message": "K线读取完成，正在执行策略回测。"})
        raw_config = ctx.control.read_config()
        trend_config = ctx.control.trend_config_for_symbol(raw_config, body.symbol)
        runner = backtest_trend_strategy_ai_proxy if body.ai_proxy else backtest_trend_strategy
        result = runner(
            candles,
            symbol=body.symbol,
            timeframe=body.timeframe,
            config=trend_config,
            initial_equity=body.initial_equity,
            fee_rate=body.fee_rate,
            slippage_bps=body.slippage_bps,
            leverage=body.leverage,
            funding_rate_per_8h=body.funding_rate_per_8h,
            min_order_qty=body.min_order_qty,
            max_volume_participation=body.max_volume_participation,
        )
        result["market_data_source"] = candles.attrs.get("data_source", "unknown")
        result["market_data_warning"] = candles.attrs.get("data_warning", "")
        result["chart_candles"] = _candles_for_chart(candles, max_points=5000)
        _record_backtest_run(ctx, "trend_backtest_job", body, result, job_id=job_id)
        ctx.store.insert("hourly_reports", {"type": "backtest_job", "job_id": job_id, "result": result}, symbol=body.symbol)
        job.update({"status": "completed", "progress": 100, "message": f"{body.symbol} 回测完成，交易 {result['trade_count']} 笔。", "result": result})
    except Exception as exc:  # noqa: BLE001 - 后台任务必须把错误留给前端轮询展示
        job.update({"status": "failed", "progress": 100, "message": "回测任务失败。", "error": str(exc)})


async def _run_trend_parameter_optimization_job(app: FastAPI, job_id: str, body: BacktestOptimizeRequest) -> None:
    job = app.state.backtest_jobs[job_id]
    try:
        job.update({"status": "running", "progress": 10, "message": "正在读取真实K线数据。"})
        ctx = _ctx(app)
        ctx.reload()
        market = MarketDataClient()
        try:
            candles = await _fetch_backtest_candles(market, body)
        finally:
            await market.close()

        job.update({"progress": 45, "message": "K线读取完成，正在执行参数网格寻优。"})
        raw_config = ctx.control.read_config()
        trend_config = ctx.control.trend_config_for_symbol(raw_config, body.symbol)
        result = await asyncio.to_thread(
            optimize_trend_parameters,
            candles,
            symbol=body.symbol,
            timeframe=body.timeframe,
            base_config=trend_config,
            initial_equity=body.initial_equity,
            fee_rate=body.fee_rate,
            slippage_bps=body.slippage_bps,
            leverage=body.leverage,
            ema_lengths=body.ema_lengths,
            kc_lengths=body.kc_lengths,
            kc_scalars=body.kc_scalars,
            atr_lengths=body.atr_lengths,
            vma_lengths=body.vma_lengths,
            volume_multiples=body.volume_multiples,
            atr_stop_multiples=body.atr_stop_multiples,
            position_fractions=body.position_fractions,
            use_ema_filters=body.use_ema_filters,
            use_volume_filters=body.use_volume_filters,
            momentum_filters=body.momentum_filters,
            kdj_lengths=body.kdj_lengths,
            validation_ratio=body.validation_ratio,
            min_trades=body.min_trades,
            max_candidates=body.max_candidates,
            top_n=body.top_n,
        )
        result["market_data_source"] = candles.attrs.get("data_source", "unknown")
        result["market_data_warning"] = candles.attrs.get("data_warning", "")
        _record_backtest_run(ctx, "parameter_optimization", body, result, job_id=job_id)
        ctx.store.insert("hourly_reports", {"type": "parameter_optimization", "job_id": job_id, "result": result}, symbol=body.symbol)
        job.update({"status": "completed", "progress": 100, "message": f"{body.symbol} 参数寻优完成。", "result": result})
    except Exception as exc:  # noqa: BLE001
        job.update({"status": "failed", "progress": 100, "message": "参数寻优任务失败。", "error": str(exc)})


def _candles_for_chart(candles, max_points: int = 5000) -> list[dict[str, Any]]:
    if len(candles) == 0:
        return []
    step = max(1, len(candles) // max_points)
    sampled = candles.iloc[::step].tail(max_points)
    output = []
    for _, row in sampled.iterrows():
        timestamp = row.get("timestamp")
        output.append(
            {
                "time": str(timestamp),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        )
    return output


async def _execute_manual_small_entry(ctx: ConsoleContext, symbol: str, side: str) -> dict[str, Any]:
    state = ctx.runtime_state()
    dry_run = execution_mode_from_config(ctx.config) == "mock"
    if not dry_run and not state.can_open(symbol):
        raise HTTPException(status_code=400, detail="当前是实盘模式，但该标的未授权或开仓锁仍开启。")

    if not dry_run and not _latest_exchange_safety_allows_new_entries(ctx):
        raise HTTPException(status_code=409, detail="exchange_reconciliation_required_manual_gate_only")

    execution = create_exchange_gateway(ctx.config, account_slot="trend")
    lifecycle = OrderLifecycleManager(ctx.store, gateway_mode=execution_mode_from_config(ctx.config))
    market = MarketDataClient()
    try:
        positions = await execution.fetch_positions([symbol])
        expected_side = Side.LONG if side == "long" else Side.SHORT
        if any(pos.symbol == symbol and pos.side == expected_side and abs(pos.qty) > 0 for pos in positions):
            raise HTTPException(status_code=400, detail="检测到同方向已有持仓，禁止重复加仓。")

        symbol_cfg = next((item for item in ctx.config.symbols if item.symbol == symbol), None)
        timeframe = symbol_cfg.timeframe if symbol_cfg else "1h"
        candles = await market.fetch_ohlcv(symbol, timeframe)
        price = float(candles["close"].iloc[-1])
        atr_value = float(atr(candles, ctx.config.strategy.trend.atr_length).dropna().iloc[-1])
        atr_stop_multiple = float(ctx.config.strategy.trend.atr_stop_multiple)
        stop_price = price - atr_value * atr_stop_multiple if side == "long" else price + atr_value * atr_stop_multiple
        min_exchange_amount = await execution.minimum_order_amount(symbol, price)
        contract_size = await execution.contract_size(symbol)
        amount = min_exchange_amount * contract_size
        entry_request = OrderRequest(
            symbol=symbol,
            side="buy" if side == "long" else "sell",
            amount=amount,
            reduce_only=False,
            client_order_id=f"aiq_web_{uuid.uuid4().hex[:16]}",
            reason="web_manual_small_entry_test",
        )
        try:
            order = await lifecycle.submit_market_order(execution, entry_request)
        except OrderRejected as exc:
            raise HTTPException(status_code=502, detail="entry_order_rejected") from exc
        except OrderSubmissionUncertain as exc:
            raise HTTPException(status_code=502, detail="entry_order_state_unknown_manual_gate_required") from exc
        ctx.store.insert("orders", order, symbol)
        trend_state = TrendStateStore()
        stop_order_id = None
        stop_state = trend_state.record_entry(
            symbol,
            Side.LONG if side == "long" else Side.SHORT,
            price,
            atr_value,
            atr_stop_multiple,
        )
        try:
            stop_request = OrderRequest(
                symbol=symbol,
                side="sell" if side == "long" else "buy",
                amount=amount,
                reduce_only=True,
                client_order_id=f"aiq_web_stop_{uuid.uuid4().hex[:12]}",
                reason="web_manual_small_entry_native_stop",
            )
            stop_order = await lifecycle.submit_stop_loss_order(
                execution,
                stop_request,
                stop_price,
            )
            stop_order_id = stop_order.exchange_order_id
            ctx.store.insert("orders", stop_order, symbol)
            stop_state = trend_state.set_native_stop_order_id(symbol, stop_order_id) or stop_state
        except (OrderRejected, OrderSubmissionUncertain) as exc:
            if not dry_run:
                raise HTTPException(status_code=502, detail="native_stop_submit_failed_manual_gate_required") from exc
            close_order = await execution.close_position(symbol, reason="manual_small_entry_stop_failed_emergency_close")
            if close_order:
                ctx.store.insert("orders", close_order, symbol)
            trend_state.clear(symbol)
            raise HTTPException(status_code=502, detail="native_stop_submit_failed_emergency_closed") from exc
        except Exception as exc:  # noqa: BLE001
            if not dry_run:
                raise HTTPException(status_code=502, detail="native_stop_submit_error_manual_gate_required") from exc
            close_order = await execution.close_position(symbol, reason="manual_small_entry_stop_failed_emergency_close")
            if close_order:
                ctx.store.insert("orders", close_order, symbol)
            trend_state.clear(symbol)
            raise HTTPException(status_code=502, detail="native_stop_submit_failed_emergency_closed") from exc
        side_text = "做多" if side == "long" else "做空"
        mode_text = "模拟订单" if order.dry_run else "真实订单"
        return {
            "ok": True,
            "message": f"小仓测试已执行：{symbol} {side_text}，{mode_text}，状态 {order.status}，数量 {order.amount:.8g}。",
            "order": order.model_dump(mode="json"),
            "native_stop_order_id": stop_order_id,
            "stop_state": stop_state.__dict__,
        }
    finally:
        await market.close()
        await execution.close()


def _latest_exchange_safety_allows_new_entries(ctx: ConsoleContext) -> bool:
    latest = ctx.store.fetch_latest("exchange_health")
    if latest is None:
        return False
    payload = latest.get("payload") or {}
    if payload.get("status") != "ok" or payload.get("can_open_new_entries") is not True:
        return False
    checked_at = _parse_readiness_datetime(str(payload.get("checked_at") or latest.get("created_at") or ""))
    if checked_at is None:
        return False
    if datetime.now(UTC) - checked_at > timedelta(seconds=ctx.config.risk.stale_data_seconds):
        return False
    return True


def _worker_heartbeat_rows(ctx: ConsoleContext) -> dict[str, dict[str, Any] | None]:
    return {worker: ctx.store.fetch_latest("worker_heartbeats", worker) for worker in _expected_worker_intervals(ctx)}


def _expected_worker_intervals(ctx: ConsoleContext) -> dict[str, int]:
    price_interval = int(ctx.config.runtime.price_monitor_interval_seconds)
    news_interval = int(ctx.config.news.refresh_interval_minutes * 60)
    return {
        "trading_worker": 3900,
        "news_worker": max(news_interval * 2, 60),
        "price_monitor_worker": max(price_interval * 3, 60),
        "order_status_worker": max(price_interval * 3, 60),
    }


def _worker_heartbeat_status(
    ctx: ConsoleContext,
    rows: dict[str, dict[str, Any] | None],
) -> tuple[Literal["ok", "warn", "block"], str]:
    execution_mode = execution_mode_from_config(ctx.config)
    stale: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    now = datetime.now(UTC)
    intervals = _expected_worker_intervals(ctx)
    for worker, expected_seconds in intervals.items():
        row = rows.get(worker)
        if row is None:
            missing.append(worker)
            continue
        payload = row.get("payload") or {}
        if payload.get("status") == "block":
            failed.append(worker)
        checked_at = _parse_readiness_datetime(str(payload.get("checked_at") or row.get("created_at") or ""))
        if checked_at is None or now - checked_at > timedelta(seconds=max(expected_seconds, ctx.config.risk.stale_data_seconds)):
            stale.append(worker)

    problems = []
    if missing:
        problems.append(f"missing={','.join(missing)}")
    if stale:
        problems.append(f"stale={','.join(stale)}")
    if failed:
        problems.append(f"failed={','.join(failed)}")
    if not problems:
        return "ok", "All runtime worker heartbeats are fresh."
    status: Literal["ok", "warn", "block"] = "block" if execution_mode == "live" else "warn"
    return status, "Worker heartbeat problem: " + "; ".join(problems)


def _maintenance_status(row: dict[str, Any] | None) -> tuple[Literal["ok", "warn", "block"], str]:
    if row is None:
        return "warn", "Runtime maintenance has not run yet."
    payload = row.get("payload") or {}
    if payload.get("disk_status") == "block":
        return "block", "Disk space is below the configured floor."
    if payload.get("sqlite_backup_integrity") not in {None, "ok"}:
        return "block", "Latest SQLite backup failed integrity verification."
    warnings = payload.get("warnings") or []
    if warnings:
        return "warn", "Runtime maintenance warnings: " + ",".join(str(item) for item in warnings)
    return "ok", "Runtime maintenance completed without warnings."


def _latest_payload_status_fresh(ctx: ConsoleContext, row: dict[str, Any] | None, expected_status: str) -> bool:
    if row is None:
        return False
    payload = row.get("payload") or {}
    if payload.get("status") != expected_status:
        return False
    checked_at = _parse_readiness_datetime(str(payload.get("checked_at") or row.get("created_at") or ""))
    if checked_at is None:
        return False
    return datetime.now(UTC) - checked_at <= timedelta(seconds=ctx.config.risk.stale_data_seconds)


def _parse_readiness_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _validate_symbols(symbols: list[str], configured: list[str], require_any: bool = False) -> list[str]:
    if require_any and not symbols:
        raise HTTPException(status_code=400, detail="请至少选择一个标的。")
    unknown = [symbol for symbol in symbols if symbol not in configured]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知标的：{', '.join(unknown)}")
    return symbols


def _console_cors_origins() -> list[str]:
    configured = os.getenv("CONSOLE_CORS_ORIGINS", "").strip()
    if configured:
        return [origin.strip() for origin in configured.split(",") if origin.strip()]
    return [
        "http://127.0.0.1:8090",
        "http://localhost:8090",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]


def _console_basic_auth_enabled() -> bool:
    return bool(os.getenv("CONSOLE_BASIC_USER", "").strip() and os.getenv("CONSOLE_BASIC_PASSWORD", "").strip())


def _valid_console_basic_auth(authorization: str | None) -> bool:
    if not authorization:
        return False
    prefix = "Basic "
    if not authorization.startswith(prefix):
        return False
    try:
        decoded = base64.b64decode(authorization[len(prefix) :], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    username, separator, password = decoded.partition(":")
    if separator != ":":
        return False
    expected_user = os.getenv("CONSOLE_BASIC_USER", "").strip()
    expected_password = os.getenv("CONSOLE_BASIC_PASSWORD", "").strip()
    return secrets.compare_digest(username, expected_user) and secrets.compare_digest(password, expected_password)


async def _cancel_trend_native_stops(execution, symbols: list[str], store: SQLiteStore | None = None, gateway_mode: str = "unknown") -> None:
    trend_state = TrendStateStore()
    lifecycle = OrderLifecycleManager(store, gateway_mode=gateway_mode) if store is not None else None
    for symbol in symbols:
        state = trend_state.get(symbol)
        if state is None or not state.native_stop_order_id:
            continue
        if lifecycle is None:
            await execution.cancel_order(symbol, state.native_stop_order_id, trigger=True)
        else:
            await lifecycle.cancel_order(
                execution,
                symbol=symbol,
                order_id=state.native_stop_order_id,
                client_order_id=f"aiq_web_cancel_{uuid.uuid4().hex[:10]}",
                trigger=True,
                gateway_mode=gateway_mode,
            )
        trend_state.clear(symbol)


async def _collect_news_digest(config_path: str) -> NewsDigest:
    from ai_quant_trader.app import TradingApp

    trading_app = TradingApp(config_path)
    try:
        return await trading_app.collect_news_once(notify=False)
    finally:
        await trading_app.close()


def _news_latest_response(rows: list[dict[str, Any]], fresh_digest: NewsDigest | None, max_age_minutes: int) -> dict[str, Any]:
    latest_payload = fresh_digest.model_dump(mode="json") if fresh_digest else ((rows[0].get("payload") if rows else None) or {})
    generated_at = latest_payload.get("generated_at") or (rows[0].get("created_at") if rows else None)
    age_minutes = _minutes_since(generated_at)
    timeline = [_sanitize_news_item(item) for item in (latest_payload.get("items") or [])]
    warnings = latest_payload.get("warnings") or []
    if timeline and any(str(item.get("title") or item.get("summary") or "").strip() for item in timeline):
        warnings = [warning for warning in warnings if not str(warning).startswith(("rss_error:", "scrape_error:"))]
    return {
        "items": rows,
        "timeline": timeline,
        "latest_digest": latest_payload,
        "generated_at": generated_at,
        "age_minutes": age_minutes,
        "stale": age_minutes is None or age_minutes > max_age_minutes,
        "warnings": warnings,
        "summary": _repair_mojibake_text(str(latest_payload.get("summary") or "")),
    }


def _sanitize_news_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    output = dict(item)
    for key in ("title", "summary", "raw_title", "raw_summary", "source"):
        if key in output and output[key] is not None:
            output[key] = _repair_mojibake_text(str(output[key]))
    return output


def _repair_mojibake_text(value: str) -> str:
    repaired = value
    for _ in range(3):
        has_cjk = any("\u4e00" <= ch <= "\u9fff" for ch in repaired)
        has_latin1_bytes = any(0x80 <= ord(ch) <= 0xFF for ch in repaired)
        if not (
            any(token in repaired for token in ("Ã", "Â", "氓", "莽", "猫", "忙", "茫", "芒"))
            or (has_latin1_bytes and not has_cjk)
        ):
            break
        try:
            candidate = repaired.encode("latin1").decode("utf-8")
        except UnicodeError:
            break
        if candidate == repaired:
            break
        repaired = candidate
    return repaired.replace("\u00a0", " ").strip()


def _news_row_is_stale(row: dict[str, Any] | None, max_age_minutes: int) -> bool:
    if row is None:
        return True
    payload = row.get("payload") or {}
    if _news_payload_needs_detail_refresh(payload):
        return True
    generated_at = payload.get("generated_at") or row.get("created_at")
    age_minutes = _minutes_since(generated_at)
    return age_minutes is None or age_minutes > max_age_minutes


def _news_payload_needs_detail_refresh(payload: dict[str, Any]) -> bool:
    items = payload.get("items") or []
    if not items:
        return True
    if not any(item.get("raw_title") or item.get("raw_summary") for item in items[:8]):
        return True
    generic_markers = (
        "发布宏观金融相关快讯",
        "发布政治或地缘风险快讯",
        "发布加密市场相关快讯",
        "消息涉及利率、通胀、美元、美债或经济数据变化",
        "消息涉及地缘政治、政府政策、制裁或关税风险",
        "消息涉及加密市场资金流、监管、交易所或链上风险",
        "利率路径和美联储政策预期变化",
        "美国经济数据影响风险资产定价",
        "美元和美债收益率扰动流动性预期",
        "政治或地缘风险可能改变避险情绪",
        "宏观金融消息更新",
        "政治或地缘风险消息更新",
        "加密市场消息更新",
    )
    for item in items[:8]:
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        if any(marker in text for marker in generic_markers):
            return True
        if _news_visible_text_has_untranslated_english(text):
            return True
    return False


def _news_visible_text_has_untranslated_english(text: str) -> bool:
    allowed = {
        "CPI", "PPI", "PCE", "GDP", "DXY", "ETF", "USDT", "BTC", "ETH", "SOL", "WTI",
        "CNBC", "OKX",
    }
    words = re.findall(r"\b[A-Za-z]{4,}\b", text)
    unresolved = [word for word in words if word.upper() not in allowed]
    return len(unresolved) >= 3


def _minutes_since(value: Any) -> float | None:
    if not value:
        return None
    try:
        raw = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max((datetime.now(UTC) - dt.astimezone(UTC)).total_seconds() / 60, 0.0)


def _ctx(app: FastAPI) -> ConsoleContext:
    return app.state.ctx


app = create_app()

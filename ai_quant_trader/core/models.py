from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class Side(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalAction(StrEnum):
    LONG = "long"
    SHORT = "short"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    HOLD = "hold"


class MarketRegime(StrEnum):
    TREND = "trend"
    RANGE = "range"
    UNCERTAIN = "uncertain"


class Alignment(StrEnum):
    ALIGNED = "aligned"
    CONFLICT = "conflict"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class NewsDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class NewsSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class VetoAction(StrEnum):
    ALLOW = "allow"
    REDUCE = "reduce"
    BLOCK = "block"


class ExchangeConnectionStatus(StrEnum):
    OK = "ok"
    DEGRADED_READONLY = "degraded_readonly"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    BLOCKED = "blocked"


class OrderLifecycleStatus(StrEnum):
    INTENT_RECORDED = "intent_recorded"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELLED = "cancelled"
    CANCEL_FAILED = "cancel_failed"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    BLOCKED = "blocked"


class HealthStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    BLOCK = "block"


class SymbolConfig(BaseModel):
    symbol: str
    timeframe: str = "1h"
    enabled_on_boot: bool = False
    leverage: int = 4


class RuntimeConfig(BaseModel):
    dry_run: bool = True
    execution_mode: Literal["mock", "live"] = "mock"
    hourly_report_minute: int = Field(default=0, ge=0, le=59)
    database_path: str = "data/trader.sqlite3"
    audit_log_path: str = "logs/audit.jsonl"
    allow_live_orders_after_restart: bool = False
    trade_mode: Literal["strategy_confirmed", "ai_candidate_approval", "pure_ai_paper"] = "strategy_confirmed"
    price_monitor_interval_seconds: int = Field(default=60, ge=30, le=300)
    price_wakeup_threshold_pct: float = Field(default=1.0, gt=0)
    price_wakeup_volatility_multiplier: float = Field(default=1.8, ge=1.0, le=10.0)


MAX_CONFIGURABLE_LEVERAGE = 20.0


class FollowerAccountConfig(BaseModel):
    enabled: bool = False
    account_slot: Literal["follower", "range"] = "follower"
    label: str = "账号2：趋势跟随账户"
    follow_ratio: float = Field(default=1.0, ge=0, le=10)
    max_leverage: float = Field(default=4.0, gt=0, le=MAX_CONFIGURABLE_LEVERAGE)
    mirror_entries: bool = True
    mirror_exits: bool = True


class RiskConfig(BaseModel):
    max_total_leverage: float = Field(default=4.0, gt=0, le=MAX_CONFIGURABLE_LEVERAGE)
    ai_full_size_confidence: float = Field(default=0.75, ge=0, le=1)
    min_confidence_to_trade: float = Field(default=0.55, ge=0, le=1)
    ai_candidate_min_confidence: float = Field(default=0.65, ge=0, le=1)
    ai_dynamic_position_sizing: bool = True
    stale_data_seconds: int = Field(default=300, gt=0)
    small_position_mode: bool = False
    small_position_notional_usdt: float = Field(default=20.0, gt=0)


class TrendStrategyConfig(BaseModel):
    enabled: bool = True
    profile_name: str = "default"
    variant: Literal["with_volume", "no_volume"] = "with_volume"
    kc_length: int = 20
    kc_scalar: float = 2.8
    vma_length: int = 20
    atr_length: int = 14
    atr_stop_multiple: float = Field(default=1.5, gt=0, le=20)
    volume_multiple: float = 2.0
    position_fraction: float = Field(default=0.5, gt=0, le=1)
    use_volume_filter: bool = True
    momentum_filter: Literal["none", "kdj"] = "kdj"
    kdj_length: int = Field(default=9, gt=1, le=100)
    kdj_k_smooth: int = Field(default=3, gt=1, le=20)
    kdj_d_smooth: int = Field(default=3, gt=1, le=20)

    @model_validator(mode="after")
    def normalize_variant_filters(self) -> "TrendStrategyConfig":
        self.use_volume_filter = self.variant == "with_volume"
        return self


class StrategyConfig(BaseModel):
    trend: TrendStrategyConfig = Field(default_factory=TrendStrategyConfig)


class OrderflowConfig(BaseModel):
    exchanges: list[str] = Field(default_factory=lambda: ["binance", "okx", "bybit"])
    weights: dict[str, float] = Field(
        default_factory=lambda: {"binance": 1.0, "okx": 1.0, "bybit": 1.0}
    )


class NewsConfig(BaseModel):
    rss_sources: list[str] = Field(default_factory=list)
    scrape_sources: list[str] = Field(default_factory=list)
    jin10_enabled: bool = False
    jin10_public_url: str = "https://www.jin10.com/"
    entity_refresh_hours: int = Field(default=24, gt=0)
    refresh_interval_minutes: int = Field(default=10, ge=5, le=15)
    max_age_hours: int = Field(default=6, gt=0, le=48)


class AiConfig(BaseModel):
    decision_model: str = "deepseek-v4-pro"
    report_model: str = "deepseek-v4-pro"
    emergency_screening_model: str = "deepseek-v4-flash"
    emergency_decision_model: str = "deepseek-v4-pro"
    base_url: str = "https://api.deepseek.com"
    backup_api_key_env: str = "DEEPSEEK_BACKUP_API_KEY"
    candidate_trade_min_confidence: float = Field(default=0.65, ge=0, le=1)
    ai_enabled_symbols: list[str] = Field(default_factory=list)
    symbol_prompt_weights: dict[str, dict[str, float]] = Field(default_factory=dict)
    call_budget_enabled: bool = True
    max_calls_per_hour: int = Field(default=8, ge=1, le=200)
    max_calls_per_day: int = Field(default=60, ge=1, le=2000)
    max_major_news_reviews_per_hour: int = Field(default=3, ge=1, le=200)
    max_major_news_reviews_per_day: int = Field(default=24, ge=1, le=1000)
    event_dedupe_hours: int = Field(default=48, ge=1, le=168)
    failure_cooldown_minutes: int = Field(default=20, ge=0, le=1440)


class SecurityConfig(BaseModel):
    admin_user_ids: list[str] = Field(default_factory=list)
    runtime_env_path: str = ".env.runtime"


class AppConfig(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    symbols: list[SymbolConfig] = Field(default_factory=list)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    symbol_params: dict[str, dict[str, Any]] = Field(default_factory=dict)
    orderflow: OrderflowConfig = Field(default_factory=OrderflowConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    ai: AiConfig = Field(default_factory=AiConfig)
    followers: list[FollowerAccountConfig] = Field(default_factory=list)
    security: SecurityConfig = Field(default_factory=SecurityConfig)


class StrategySignal(BaseModel):
    symbol: str
    timeframe: str
    action: SignalAction
    current_price: float
    suggested_qty: float = 0.0
    signal_strength: float = Field(default=0.0, ge=0, le=1)
    technical_evidence: dict[str, Any] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)


class OrderflowSummary(BaseModel):
    symbol: str
    exchange: str
    timestamp: datetime = Field(default_factory=utc_now)
    bid_ask_imbalance: float = Field(default=0.0, ge=-1, le=1)
    active_buy_sell_ratio: float = Field(default=1.0, ge=0)
    cvd_delta: float = 0.0
    spread_bps: float = Field(default=0.0, ge=0)
    depth_usd: float = Field(default=0.0, ge=0)
    large_trade_events: int = Field(default=0, ge=0)
    liquidity_shift: float = Field(default=0.0, ge=-1, le=1)
    data_quality: float = Field(default=1.0, ge=0, le=1)


class AggregatedOrderflow(BaseModel):
    symbol: str
    timestamp: datetime = Field(default_factory=utc_now)
    bid_ask_imbalance: float = Field(default=0.0, ge=-1, le=1)
    active_buy_sell_ratio: float = Field(default=1.0, ge=0)
    cvd_delta: float = 0.0
    spread_bps: float = Field(default=0.0, ge=0)
    depth_usd: float = Field(default=0.0, ge=0)
    large_trade_events: int = Field(default=0, ge=0)
    liquidity_shift: float = Field(default=0.0, ge=-1, le=1)
    alignment_hint: Alignment = Alignment.UNKNOWN
    data_quality: float = Field(default=0.0, ge=0, le=1)
    source_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class DenseZone(BaseModel):
    symbol: str
    timestamp: datetime = Field(default_factory=utc_now)
    poc: float
    vah: float
    val: float
    hvn: list[float] = Field(default_factory=list)
    lvn: list[float] = Field(default_factory=list)
    support: float | None = None
    resistance: float | None = None
    current_position: Literal["above_value", "inside_value", "below_value", "unknown"] = "unknown"
    strength: float = Field(default=0.0, ge=0, le=1)
    zone_low: float | None = None
    zone_high: float | None = None
    zone_mid: float | None = None
    previous_zone_low: float | None = None
    previous_zone_high: float | None = None
    next_zone_low: float | None = None
    next_zone_high: float | None = None
    vacuum_low: float | None = None
    vacuum_high: float | None = None
    breakout_status: Literal[
        "inside_zone",
        "breakout_up",
        "breakout_down",
        "retest_support",
        "retest_resistance",
        "failed_breakout",
        "vacuum_travel",
        "unknown",
    ] = "unknown"
    retest_status: Literal[
        "none",
        "support_retest",
        "resistance_retest",
        "failed_retest",
        "unknown",
    ] = "unknown"
    touch_count_vah: int = Field(default=0, ge=0)
    touch_count_val: int = Field(default=0, ge=0)
    trend_score: float = Field(default=0.0, ge=0, le=1)
    range_score: float = Field(default=0.0, ge=0, le=1)
    structure_label: str = "未知结构"


class PatternCandidate(BaseModel):
    symbol: str
    pattern_type: str = "unknown"
    pattern_family: str = "unknown"
    confidence: float = Field(default=0.0, ge=0, le=1)
    upper_boundary: float | None = None
    lower_boundary: float | None = None
    breakout_direction: Side | None = None
    invalidation_price: float | None = None
    upper_slope: float | None = None
    lower_slope: float | None = None
    width_ratio: float | None = None
    upper_touches: int = Field(default=0, ge=0)
    lower_touches: int = Field(default=0, ge=0)
    sample_bars: int = Field(default=0, ge=0)
    evidence_codes: list[str] = Field(default_factory=list)


class RegimePattern(BaseModel):
    symbol: str
    regime_candidate: Literal["trend", "range", "transition", "high_risk", "unknown"] = "unknown"
    strategy_allowed: Literal["trend", "range", "none"] = "none"
    pattern_family: str = "unknown"
    pattern_name: str = "unknown"
    breakout_quality: Literal["strong", "weak", "failed", "pending", "none", "unknown"] = "unknown"
    trend_score: float = Field(default=0.0, ge=0, le=1)
    range_score: float = Field(default=0.0, ge=0, le=1)
    risk_score: float = Field(default=0.0, ge=0, le=1)
    position_context: str = "unknown"
    reason_codes: list[str] = Field(default_factory=list)
    notes: str = ""


class NewsItem(BaseModel):
    title: str
    source: str
    url: str | None = None
    published_at: datetime = Field(default_factory=utc_now)
    category: str = "crypto"
    credibility: float = Field(default=0.5, ge=0, le=1)
    important: bool = False
    summary: str = ""
    raw_title: str = ""
    raw_summary: str = ""


class NewsEvent(BaseModel):
    event_id: str
    title: str
    source: str
    published_at: datetime
    category: str = "macro"
    direction: NewsDirection = NewsDirection.UNKNOWN
    severity: NewsSeverity = NewsSeverity.LOW
    risk_score: float = Field(default=0.0, ge=0, le=1)
    confidence: float = Field(default=0.0, ge=0, le=1)
    asset_scope: list[str] = Field(default_factory=list)
    summary: str = ""
    decay_until: datetime
    source_item_key: str = ""


class MarketBackgroundSnapshot(BaseModel):
    generated_at: datetime = Field(default_factory=utc_now)
    lookback_hours: int = 48
    realtime_minutes: int = 60
    background_direction: NewsDirection = NewsDirection.UNKNOWN
    risk_level: Literal["low", "medium", "high", "critical", "unknown"] = "unknown"
    active_events: list[NewsEvent] = Field(default_factory=list)
    realtime_events: list[NewsEvent] = Field(default_factory=list)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)


class MarketLeaderContext(BaseModel):
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    generated_at: datetime = Field(default_factory=utc_now)
    available: bool = False
    price: float | None = None
    change_1h_pct: float | None = None
    change_4h_pct: float | None = None
    change_24h_pct: float | None = None
    relative_strength_1h_pct: float | None = None
    relative_strength_4h_pct: float | None = None
    market_direction: NewsDirection = NewsDirection.UNKNOWN
    strategy_alignment_hint: Alignment = Alignment.UNKNOWN
    leader_regime: Literal[
        "leader_uptrend",
        "rotation_lag",
        "leader_pullback",
        "distribution_risk",
        "leader_downtrend",
        "unknown",
    ] = "unknown"
    eth_btc_rotation_score: float = Field(default=0.0, ge=0, le=1)
    impact_score: float = Field(default=0.0, ge=0, le=1)
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)


class NewsDigest(BaseModel):
    generated_at: datetime = Field(default_factory=utc_now)
    items: list[NewsItem] = Field(default_factory=list)
    macro_risk_level: Literal["low", "medium", "high", "unknown"] = "unknown"
    news_direction: NewsDirection = NewsDirection.UNKNOWN
    # Legacy coarse market-direction hint. Historically ALIGNED meant bullish
    # and CONFLICT meant bearish. Keep it for old payloads, but new code should
    # use news_direction for absolute news direction and Alignment for
    # strategy-relative agreement.
    crypto_sentiment: Alignment = Alignment.UNKNOWN
    summary: str = ""
    warnings: list[str] = Field(default_factory=list)
    active_news_events: list[NewsEvent] = Field(default_factory=list)
    market_background: MarketBackgroundSnapshot | None = None

    @model_validator(mode="after")
    def normalize_news_direction(self) -> "NewsDigest":
        if self.news_direction == NewsDirection.UNKNOWN and self.crypto_sentiment != Alignment.UNKNOWN:
            self.news_direction = {
                Alignment.ALIGNED: NewsDirection.BULLISH,
                Alignment.CONFLICT: NewsDirection.BEARISH,
                Alignment.NEUTRAL: NewsDirection.NEUTRAL,
                Alignment.UNKNOWN: NewsDirection.UNKNOWN,
            }[self.crypto_sentiment]
        elif self.crypto_sentiment == Alignment.UNKNOWN and self.news_direction != NewsDirection.UNKNOWN:
            self.crypto_sentiment = {
                NewsDirection.BULLISH: Alignment.ALIGNED,
                NewsDirection.BEARISH: Alignment.CONFLICT,
                NewsDirection.NEUTRAL: Alignment.NEUTRAL,
                NewsDirection.UNKNOWN: Alignment.UNKNOWN,
            }[self.news_direction]
        return self


class ExchangeSafetyState(BaseModel):
    status: ExchangeConnectionStatus = ExchangeConnectionStatus.BLOCKED
    can_open_new_entries: bool = False
    reason: str = ""
    manual_action: str = "请到 Gate 官方端人工处理，本系统在降级状态不发送应急平仓单。"
    last_success_at: datetime | None = None
    checked_at: datetime = Field(default_factory=utc_now)
    stale_after_seconds: int = 300
    failures: list[str] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    status: ExchangeConnectionStatus = ExchangeConnectionStatus.RECONCILIATION_REQUIRED
    symbol_count: int = 0
    balance_ok: bool = False
    positions_ok: bool = False
    open_orders_ok: bool = False
    native_stops_ok: bool = False
    local_state_ok: bool = False
    issues: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


class DataHealthCheck(BaseModel):
    name: str
    status: HealthStatus = HealthStatus.OK
    reason: str = ""
    age_seconds: float | None = None
    quality: float | None = None


class DataHealthReport(BaseModel):
    symbol: str
    status: HealthStatus = HealthStatus.OK
    can_open_new_entries: bool = True
    reason: str = ""
    checks: list[DataHealthCheck] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


class AiDriftReport(BaseModel):
    symbol: str
    status: HealthStatus = HealthStatus.OK
    reason: str = "ai_drift_ok"
    drift_score: float = Field(default=0.0, ge=0, le=1)
    sample_size: int = 0
    latest_confidence: float = Field(default=0.0, ge=0, le=1)
    baseline_confidence: float | None = None
    latest_direction: Side = Side.FLAT
    baseline_direction: Side | None = None
    score_deltas: dict[str, float] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=utc_now)


class WorkerHeartbeat(BaseModel):
    worker: str
    status: HealthStatus = HealthStatus.OK
    reason: str = "worker_ok"
    interval_seconds: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    last_success_at: datetime | None = None
    checked_at: datetime = Field(default_factory=utc_now)


class AiDecision(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    symbol: str
    regime: MarketRegime
    direction: Side = Side.FLAT
    confidence: float = Field(ge=0, le=1)
    multiplier: float = Field(ge=0.5, le=1.5)
    news_alignment: Alignment = Alignment.UNKNOWN
    orderflow_alignment: Alignment = Alignment.UNKNOWN
    btc_leader_alignment: Alignment = Alignment.UNKNOWN
    btc_leader_regime: str = "unknown"
    dense_zone_position: str = "unknown"
    pattern_type: str = "unknown"
    trend_confirmation_score: float = Field(default=0.0, ge=0, le=1)
    range_risk_score: float = Field(default=0.0, ge=0, le=1)
    news_risk_score: float = Field(default=0.0, ge=0, le=1)
    crypto_market_impact_score: float = Field(default=0.0, ge=0, le=1)
    btc_leader_impact_score: float = Field(default=0.0, ge=0, le=1)
    eth_btc_rotation_score: float = Field(default=0.0, ge=0, le=1)
    symbol_news_impact_score: float = Field(default=0.0, ge=0, le=1)
    pattern_confirmation_score: float = Field(default=0.5, ge=0, le=1)
    orderflow_confirmation_score: float = Field(default=0.0, ge=0, le=1)
    dense_zone_breakout_score: float = Field(default=0.0, ge=0, le=1)
    entry_zone_estimate: float | None = None
    tp_estimate: float | None = None
    sl_estimate: float | None = None
    action_suggestion: str = "hold"
    veto_action: VetoAction = VetoAction.BLOCK
    brief_reason: str = ""
    reason_codes: list[str] = Field(default_factory=list)
    data_quality_warnings: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("action_suggestion")
    @classmethod
    def safe_action_suggestion(cls, value: str) -> str:
        value = value.strip().lower()
        allowed = {"open_long", "open_short", "reduce", "close", "hold", "block"}
        return value if value in allowed else "hold"


class AiCandidateTradePlan(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    symbol: str
    direction: Side
    confidence: float = Field(ge=0, le=1)
    entry_zone_low: float
    entry_zone_high: float
    tp_estimate: float
    sl_estimate: float
    suggested_multiplier: float = Field(default=0.5, ge=0.1, le=1.0)
    expected_regime: MarketRegime
    trigger_evidence: list[str] = Field(default_factory=list)
    invalidation_conditions: list[str] = Field(default_factory=list)
    news_impact: str = ""
    orderflow_impact: str = ""
    dense_zone_context: str = ""
    approval_required: bool = True
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    created_at: datetime = Field(default_factory=utc_now)


class WakeupSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class WakeupEvent(BaseModel):
    event_type: Literal["price_move", "news", "orderflow", "dense_zone", "manual"]
    severity: WakeupSeverity = WakeupSeverity.LOW
    symbol: str | None = None
    title: str
    summary: str = ""
    source: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    should_escalate_to_pro: bool = False
    created_at: datetime = Field(default_factory=utc_now)


class MacroEntity(BaseModel):
    name: str
    role: str
    region: str = "US"
    source: str
    observed_at: datetime = Field(default_factory=utc_now)
    confidence: float = Field(default=0.5, ge=0, le=1)


class PositionSnapshot(BaseModel):
    symbol: str
    side: Side = Side.FLAT
    qty: float = 0.0
    entry_price: float = 0.0
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0

    @property
    def notional(self) -> float:
        return abs(self.qty * self.mark_price)


class RiskDecision(BaseModel):
    allowed: bool
    action: SignalAction
    symbol: str
    target_qty: float = 0.0
    clipped_qty: float = 0.0
    target_notional: float = 0.0
    strategy_baseline_notional: float = 0.0
    ai_desired_notional: float = 0.0
    sizing_basis: Literal["strategy_signal", "account_risk_cap"] = "strategy_signal"
    max_total_notional: float = 0.0
    remaining_notional: float = 0.0
    decision_score: float = Field(default=0.0, ge=0, le=1)
    position_scale: float = Field(default=0.0, ge=0, le=1)
    position_tier: Literal["block", "weak", "normal", "strong", "full"] = "block"
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)


class OrderRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    amount: float
    reduce_only: bool = False
    client_order_id: str
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrderResult(BaseModel):
    symbol: str
    side: str
    amount: float
    price: float | None = None
    status: str
    dry_run: bool
    exchange_order_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class OrderLifecycleEvent(BaseModel):
    client_order_id: str
    symbol: str
    status: OrderLifecycleStatus
    account_slot: str = "default"
    order_type: Literal["market", "stop_loss", "cancel"] = "market"
    side: str | None = None
    amount: float | None = None
    reduce_only: bool = False
    gateway_mode: str = "unknown"
    reason: str = ""
    exchange_order_id: str | None = None
    order_status: str | None = None
    order: dict[str, Any] | None = None
    error_type: str | None = None
    error_message: str | None = None
    recoverable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)


class SecretService(StrEnum):
    DEEPSEEK = "deepseek"
    GATEIO = "gateio"
    GATEIO_TREND = "gateio_trend"
    GATEIO_RANGE = "gateio_range"
    GATEIO_FOLLOWER = "gateio_follower"


class SecretUpdateCommand(BaseModel):
    service: SecretService
    values: dict[str, str]
    operator_id: str
    raw_message_id: str | None = None


class SecretVersionRecord(BaseModel):
    service: SecretService
    version: int
    operator_id: str
    key_fingerprint: str
    key_tail: str
    secret_fingerprint: str | None = None
    secret_tail: str | None = None
    status: Literal["applied", "rolled_back", "failed"] = "applied"
    created_at: datetime = Field(default_factory=utc_now)
